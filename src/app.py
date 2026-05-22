import sys
import numpy as np
import serial
import serial.tools.list_ports
import pyqtgraph as pg
from PyQt5.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
                             QHBoxLayout, QPushButton, QComboBox, QLabel, QMessageBox,
                             QRadioButton, QGroupBox, QSplitter, QScrollArea, QFrame)
from PyQt5.QtCore import QThread, pyqtSignal, Qt
import decoders
# THREAD XỬ LÝ SERIAL (Chạy ngầm, không đơ App)
class CaptureThread(QThread):
    data_ready = pyqtSignal(np.ndarray)
    error_signal = pyqtSignal(str)
    status_signal = pyqtSignal(str)

    def __init__(self, port, speed_cmd, mode_cmd):
        super().__init__()
        self.port = port
        self.speed_cmd = speed_cmd
        self.mode_cmd = mode_cmd
        self.running = True

    def run(self):
        try:
            with serial.Serial(self.port, 115200, timeout=1) as ser:
                self.status_signal.emit(f"Đã kết nối {self.port}. Đang gửi lệnh cấu hình xuống STM32...")
                ser.write(self.speed_cmd.encode())
                ser.write(self.mode_cmd.encode())
                ser.reset_input_buffer()
                self.status_signal.emit("Trạng thái: Cấu hình xong! Đang rình mồi chờ tín hiệu Trigger...")

                buffer = bytearray()
                while self.running and len(buffer) < 60000:
                    chunk = ser.read(60000 - len(buffer))
                    if chunk:
                        buffer.extend(chunk)

                if len(buffer) == 60000:
                    self.status_signal.emit("Trạng thái: Vừa hốt trọn 60.000 mẫu! Đang vẽ đồ thị...")
                    data = np.frombuffer(buffer, dtype=np.uint8)
                    self.data_ready.emit(data)
                else:
                    if self.running:
                        self.error_signal.emit("Đã hủy hoặc bị quá thời gian chờ!")
        except Exception:
            self.error_signal.emit(f"Lỗi cổng {self.port}: Mạch bị lỏng cáp hoặc cổng COM đang bị phần mềm khác chiếm!")

    def stop(self):
        self.running = False

 # WIDGET MỘT DÒNG GIẢI MÃ 
class DecodeConfigWidget(QWidget):
    """Mỗi instance = 1 bộ giải mã độc lập với loại + phân công kênh riêng."""
    removed = pyqtSignal(object)  # báo cho parent biết cần xóa widget này

    # Định nghĩa tên chân và kênh mặc định cho từng loại
    PIN_DEFS = {
        "PWM":  [("Data", 0)],
        "UART": [("RX",   0)],
        "SPI":  [("CLK",  0), ("MOSI", 1), ("MISO", 2)],
        "I2C":  [("SCL",  0), ("SDA",  1)],
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        outer = QHBoxLayout(self)
        outer.setContentsMargins(2, 3, 2, 3)
        outer.setSpacing(4)

        # ── Combo chọn loại ──────────────────────────────────
        self.type_combo = QComboBox()
        self.type_combo.setMinimumWidth(75)
        self.type_combo.addItems(["Chọn...", "PWM", "UART", "SPI", "I2C"])
        self.type_combo.currentIndexChanged.connect(self._rebuild_pins)
        outer.addWidget(self.type_combo)

        # ── Container các combo chọn kênh (sinh động theo loại) ──
        self.pin_container = QWidget()
        self.pin_layout = QHBoxLayout(self.pin_container)
        self.pin_layout.setContentsMargins(0, 0, 0, 0)
        self.pin_layout.setSpacing(3)
        outer.addWidget(self.pin_container)

        # ── Nút xóa hàng này ─────────────────────────────────
        btn_rm = QPushButton("✕")
        btn_rm.setFixedSize(22, 22)
        btn_rm.setStyleSheet("color: #c0392b; font-weight: bold; border: none;")
        btn_rm.setToolTip("Xóa bộ giải mã này")
        btn_rm.clicked.connect(lambda: self.removed.emit(self))
        outer.addWidget(btn_rm)

        outer.addStretch()
        self.pin_widgets = []  # list of (pin_name: str, QComboBox)

    def _rebuild_pins(self):
        """Xóa các combo kênh cũ rồi tạo lại theo loại vừa chọn."""
        # Dọn sạch
        while self.pin_layout.count():
            item = self.pin_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.pin_widgets = []

        type_name = self.type_combo.currentText()
        if type_name not in self.PIN_DEFS:
            return

        ch_options = [f"CH {i}" for i in range(8)]
        for pin_name, default_ch in self.PIN_DEFS[type_name]:
            lbl = QLabel(f"{pin_name}:")
            lbl.setStyleSheet("font-size: 11px; color: #333;")
            cb = QComboBox()
            cb.addItems(ch_options)
            cb.setCurrentIndex(default_ch)
            cb.setMaximumWidth(58)
            self.pin_layout.addWidget(lbl)
            self.pin_layout.addWidget(cb)
            self.pin_widgets.append((pin_name, cb))

    def get_config(self):
        """Trả về dict config hoặc None nếu chưa chọn loại."""
        type_name = self.type_combo.currentText()
        if type_name == "Chọn...":
            return None
        return {
            "type": type_name,
            "channels": {name: cb.currentIndex() for name, cb in self.pin_widgets}
        }

# GIAO DIỆN CHÍNH
class LogicAnalyzerApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("HUST Logic Analyzer - Bảng điều khiển trung tâm")
        self.resize(1150, 700)
        self.thread = None
        self.last_raw_channels = None
        self.last_ts_ms = None
        self.decode_text_items = []
        self.decode_config_widgets = []   # danh sách các DecodeConfigWidget đang hiển thị

        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        main_layout = QVBoxLayout(main_widget)

        self.splitter = QSplitter(Qt.Vertical)
        main_layout.addWidget(self.splitter)

        #  TOP PANEL 
        top_panel = QWidget()
        layout = QVBoxLayout(top_panel)
        layout.setContentsMargins(0, 0, 0, 0)
        control_layout = QHBoxLayout()

        # Ô 1: Chọn cổng COM
        com_group = QGroupBox("1. Kết nối (Cổng COM)")
        com_layout = QVBoxLayout()
        self.port_combo = QComboBox()
        self.refresh_ports()
        btn_refresh = QPushButton("🔄 Quét lại COM")
        btn_refresh.clicked.connect(self.refresh_ports)
        com_layout.addWidget(self.port_combo)
        com_layout.addWidget(btn_refresh)
        com_group.setLayout(com_layout)

        # Ô 2: Tốc độ lấy mẫu
        speed_group = QGroupBox("2. Tốc độ lấy mẫu (Speed)")
        speed_layout = QVBoxLayout()
        self.rb_100k = QRadioButton("100 kHz")
        self.rb_500k = QRadioButton("500 kHz")
        self.rb_1m   = QRadioButton("1 MHz")
        self.rb_2m   = QRadioButton("2 MHz")
        self.rb_1m.setChecked(True)
        for rb in (self.rb_100k, self.rb_500k, self.rb_1m, self.rb_2m):
            speed_layout.addWidget(rb)
        speed_group.setLayout(speed_layout)

        # Ô 3: Trigger Mode
        mode_group = QGroupBox("3. Kiểu lấy mẫu (Trigger Mode)")
        mode_layout = QVBoxLayout()
        self.rb_pre  = QRadioButton("Bắt cả Quá khứ (Pre-Trigger)")
        self.rb_post = QRadioButton("Chỉ bắt Tương lai (Full Post-Trigger)")
        self.rb_pre.setChecked(True)
        mode_layout.addWidget(self.rb_pre)
        mode_layout.addWidget(self.rb_post)
        mode_layout.addStretch()
        mode_group.setLayout(mode_layout)

        #  Ô 4: GIẢI MÃ TÍN HIỆU 
        decode_group = QGroupBox("4. Giải mã tín hiệu  (có thể chạy nhiều bộ cùng lúc)")
        decode_outer = QVBoxLayout()
        decode_outer.setSpacing(4)

        # Hướng dẫn nhỏ
        hint = QLabel("① Chọn loại  →  ② Gán kênh cho từng chân")
        hint.setStyleSheet("color: #555; font-size: 11px; font-style: italic;")
        hint.setFixedHeight(16)
        decode_outer.addWidget(hint)

        # Vùng cuộn chứa danh sách bộ giải mã
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setMinimumHeight(80)

        self.decode_list_widget = QWidget()
        self.decode_list_layout = QVBoxLayout(self.decode_list_widget)
        self.decode_list_layout.setContentsMargins(0, 0, 0, 0)
        self.decode_list_layout.setSpacing(2)
        self.decode_list_layout.addStretch()   # đẩy lên trên
        scroll.setWidget(self.decode_list_widget)
        decode_outer.addWidget(scroll, stretch=1)

        # Nút thêm bộ giải mã
        btn_add = QPushButton("＋  Thêm bộ giải mã")
        btn_add.setStyleSheet("""
            QPushButton { background-color: #27ae60; color: white;
                          font-weight: bold; border-radius: 4px; padding: 4px 8px; }
            QPushButton:hover { background-color: #219a52; }
        """)
        btn_add.setMaximumWidth(220)
        btn_add.clicked.connect(self.add_decode_config)

        self.btn_decode_now = QPushButton("Giai ma")
        self.btn_decode_now.setStyleSheet("""
            QPushButton { background-color: #2f80ed; color: white;
                          font-weight: bold; border-radius: 4px; padding: 4px 10px; }
            QPushButton:hover { background-color: #1f6fd6; }
        """)
        self.btn_decode_now.setMaximumWidth(120)
        self.btn_decode_now.clicked.connect(self.decode_last_capture)

        decode_button_row = QHBoxLayout()
        decode_button_row.addWidget(btn_add)
        decode_button_row.addWidget(self.btn_decode_now)
        decode_button_row.addStretch()
        decode_outer.addLayout(decode_button_row)

        decode_group.setLayout(decode_outer)
        control_layout.addWidget(com_group, stretch=2)  # tỉ lệ các ô
        control_layout.addWidget(speed_group, stretch=2)
        control_layout.addWidget(mode_group, stretch=3)
        control_layout.addWidget(decode_group, stretch=5)
        layout.addLayout(control_layout)

        # KHU VỰC 2: NÚT ĐO & TRẠNG THÁI
        action_layout = QHBoxLayout()
        self.btn_capture = QPushButton("▶ BẤM ĐỂ ĐO TÍN HIỆU NGAY")
        self.btn_capture.setStyleSheet("""
            QPushButton { background-color: #d9534f; color: white;
                          font-size: 16px; font-weight: bold; padding: 10px; border-radius: 5px; }
            QPushButton:hover { background-color: #c9302c; }
            QPushButton:disabled { background-color: #cccccc; color: #666666; }
        """)
        self.btn_capture.clicked.connect(self.start_capture)
        self.status_label = QLabel("Trạng thái: Sẵn sàng")
        self.status_label.setStyleSheet("color: blue; font-size: 14px; font-weight: bold;")
        action_layout.addWidget(self.btn_capture, stretch=1)
        action_layout.addWidget(self.status_label, stretch=2)
        layout.addLayout(action_layout)

        self.splitter.addWidget(top_panel)

        #  BOTTOM PANEL: ĐỒ THỊ
        bottom_panel = QWidget()
        plot_layout = QVBoxLayout(bottom_panel)
        plot_layout.setContentsMargins(0, 0, 0, 0)
        pg.setConfigOption('background', '#f5f5f5')
        pg.setConfigOption('foreground', 'k')
        self.plot_widget = pg.PlotWidget()
        self.plot_widget.showGrid(x=True, y=True, alpha=0.3)
        self.plot_widget.setLabel('bottom', 'Thời gian (ms)')
        plot_layout.addWidget(self.plot_widget)
        self.splitter.addWidget(bottom_panel)
        self.splitter.setSizes([160, 540])

        # Tạo sẵn 8 đường ngang
        self.curves = []
        yticks = []
        self.x_axis = np.arange(60001)
        for i in range(8):
            color = pg.intColor(i, hues=8, values=1, maxHue=360, alpha=255)
            curve = self.plot_widget.plot(pen=pg.mkPen(color, width=2.5), stepMode="center")
            self.curves.append(curve)
            yticks.append((i * 2 + 0.5, f"CH {i}"))
        self.plot_widget.getAxis('left').setTicks([yticks])
        self.plot_widget.setLimits(yMin=-1, yMax=16)
        self.plot_widget.setYRange(-1, 16, padding=0)
        self.plot_widget.setMouseEnabled(y=False)

    #  QUẢN LÝ DANH SÁCH BỘ GIẢI MÃ
    def add_decode_config(self):
        """Thêm một hàng DecodeConfigWidget mới vào danh sách."""
        w = DecodeConfigWidget()
        w.removed.connect(self.remove_decode_config)
        self.decode_config_widgets.append(w)
        # Chèn trước stretch ở cuối
        idx = self.decode_list_layout.count() - 1
        self.decode_list_layout.insertWidget(idx, w)

    def remove_decode_config(self, w):
        """Xóa một hàng giải mã khỏi danh sách."""
        if w in self.decode_config_widgets:
            self.decode_config_widgets.remove(w)
        self.decode_list_layout.removeWidget(w)
        w.deleteLater()

    def clear_decode_annotations(self):
        for item in self.decode_text_items:
            try:
                self.plot_widget.removeItem(item)
            except Exception:
                pass
        self.decode_text_items = []

    def decode_last_capture(self):
        if self.last_raw_channels is None or self.last_ts_ms is None:
            QMessageBox.information(self, "Chua co du lieu", "Hay chup tin hieu truoc.")
            return
        count = self.apply_decoders(self.last_raw_channels, self.last_ts_ms)
        self.status_label.setText(f"Da giai ma lai tin hieu vua chup: {count} nhan.")

    def apply_decoders(self, raw_channels, Ts_ms):
        self.clear_decode_annotations()
        decode_funcs = {
            "PWM": lambda cfg, rc: (
                decoders.decode_pwm(rc[cfg["channels"]["Data"]], self.current_fs),
                cfg["channels"]["Data"]
            ),
            "UART": lambda cfg, rc: (
                decoders.decode_uart(rc[cfg["channels"]["RX"]], self.current_fs, 115200),
                cfg["channels"]["RX"]
            ),
            "SPI": lambda cfg, rc: (
                decoders.decode_spi(
                    rc[cfg["channels"]["CLK"]],
                    rc[cfg["channels"]["MOSI"]],
                    rc[cfg["channels"]["MISO"]],
                    self.current_fs
                ),
                cfg["channels"]["MOSI"]
            ),
            "I2C": lambda cfg, rc: (
                decoders.decode_i2c(rc[cfg["channels"]["SCL"]], rc[cfg["channels"]["SDA"]], self.current_fs),
                cfg["channels"]["SDA"]
            ),
        }

        label_count = 0
        for cfg_widget in self.decode_config_widgets:
            cfg = cfg_widget.get_config()
            if cfg is None:
                continue
            t = cfg["type"]
            if t not in decode_funcs:
                continue
            try:
                packets, ref_ch = decode_funcs[t](cfg, raw_channels)
                for pkt in packets:
                    text_item = pg.TextItem(text=pkt["text"], color=(0, 0, 0), anchor=(0.5, 1))
                    text_item.fill = pg.mkBrush(255, 255, 0, 150)
                    mid_idx = int((pkt["start"] + pkt["end"]) / 2)
                    x_pos = mid_idx * Ts_ms
                    y_pos = ref_ch * 2 + 1.2
                    text_item.setPos(x_pos, y_pos)
                    self.plot_widget.addItem(text_item)
                    self.decode_text_items.append(text_item)
                    label_count += 1
            except Exception as e:
                print(f"Loi giai ma {t}:", e)
        return label_count

    # CÁC HÀM GỐC GIỮ NGUYÊN 
    def refresh_ports(self):
        self.port_combo.clear()
        ports = serial.tools.list_ports.comports()
        if ports:
            for p in ports:
                self.port_combo.addItem(p.device)
        else:
            self.port_combo.addItem("Không thấy mạch!")

    def start_capture(self):
        port = self.port_combo.currentText().strip()
        if port == "Không thấy mạch!" or not port:
            QMessageBox.warning(self, "Lỗi", "Không tìm thấy cổng COM ")
            return

        speed_cmd = '3'
        self.current_fs = 1000000
        if self.rb_100k.isChecked(): speed_cmd = '1'; self.current_fs = 100000
        elif self.rb_500k.isChecked(): speed_cmd = '2'; self.current_fs = 500000
        elif self.rb_1m.isChecked():   speed_cmd = '3'; self.current_fs = 1000000
        elif self.rb_2m.isChecked():   speed_cmd = '4'; self.current_fs = 2000000

        mode_cmd = 'P' if self.rb_pre.isChecked() else 'F'

        self.btn_capture.setEnabled(False)
        self.btn_capture.setText("⏳ ĐANG CHỜ TRIGGER...")
        self.btn_capture.setStyleSheet(
            "background-color: #f0ad4e; color: white; font-size: 16px; font-weight: bold; padding: 10px;")

        self.thread = CaptureThread(port, speed_cmd, mode_cmd)
        self.thread.data_ready.connect(self.update_plot)
        self.thread.status_signal.connect(self.status_label.setText)
        self.thread.error_signal.connect(self.show_error)
        self.thread.start()

    def update_plot(self, data):
        Ts_ms = (1.0 / self.current_fs) * 1000.0
        time_axis = self.x_axis * Ts_ms

        self.plot_widget.clear()
        self.curves = []
        yticks = []
        custom_colors = [
            '#FF0000', '#00FF00', '#0000FF', '#FF8C00',
            '#FF00FF', '#00CED1', '#FFD700', '#8B4513'
        ]
        for i in range(8):
            curve = self.plot_widget.plot(
                pen=pg.mkPen(custom_colors[i], width=2.5), stepMode="center")
            self.curves.append(curve)
            yticks.append((i * 2 + 0.5, f"CH {i}"))
        self.plot_widget.getAxis('left').setTicks([yticks])
        # Tách 8 kênh và vẽ sóng
        raw_channels = []
        for i in range(8):
            bit_array = ((data >> i) & 1).astype(np.int8)
            raw_channels.append(bit_array)
            y_offset = bit_array * 1.0 + (i * 2)
            self.curves[i].setData(x=time_axis, y=y_offset)

        self.last_raw_channels = raw_channels
        self.last_ts_ms = Ts_ms
        label_count = self.apply_decoders(raw_channels, Ts_ms)

        self.status_label.setText(f"Ve xong! Da gan {label_count} nhan giai ma.")
        self.reset_button_ui()
        self.plot_widget.enableAutoRange(axis='x')
        self.plot_widget.enableAutoRange(axis='y', enable=False)
        self.plot_widget.setYRange(-1, 16, padding=0)
        return

        #  CHẠY TẤT CẢ CÁC BỘ GIẢI MÃ ĐANG CÀI 
        decode_funcs = {
        "PWM":  lambda cfg, rc: (decoders.decode_pwm(rc[cfg["channels"]["Data"]], self.current_fs), cfg["channels"]["Data"]),
        "UART": lambda cfg, rc: (decoders.decode_uart(rc[cfg["channels"]["RX"]], self.current_fs, 115200), cfg["channels"]["RX"]),
        "SPI":  lambda cfg, rc: (decoders.decode_spi(rc[cfg["channels"]["CLK"]], rc[cfg["channels"]["MOSI"]], rc[cfg["channels"]["MISO"]], self.current_fs,  cfg["channels"]["MISO"]),  cfg["channels"]["MOSI"]),  # dùng MOSI làm kênh tham chiếu để đặt text
        "I2C":  lambda cfg, rc: (decoders.decode_i2c(rc[cfg["channels"]["SCL"]], rc[cfg["channels"]["SDA"]], self.current_fs), cfg["channels"]["SDA"]),
    }

        for cfg_widget in self.decode_config_widgets:
            cfg = cfg_widget.get_config()
            if cfg is None:continue
            t = cfg["type"]
            if t not in decode_funcs: continue
            try:
                packets = decode_funcs[t](cfg, raw_channels)
                for pkt in packets:
                    text_item = pg.TextItem(text=pkt['text'], color=(0, 0, 0), anchor=(0.5, 1))
                    text_item.fill = pg.mkBrush(255, 255, 0, 150)
                    mid_idx = int((pkt['start'] + pkt['end']) / 2)
                    x_pos = mid_idx * Ts_ms
                    y_pos = pkt['channel'] * 2 + 1.2   # nổi ngay trên kênh tham chiếu
                    text_item.setPos(x_pos, y_pos)
                    self.plot_widget.addItem(text_item)
            except Exception as e:
                print(f"Lỗi giải mã {t}:", e)

        self.status_label.setText("✅ VẼ XONG! Kéo chuột để di chuyển, Lăn chuột để Zoom.")
        self.reset_button_ui()
        self.plot_widget.enableAutoRange(axis='x')
        self.plot_widget.enableAutoRange(axis='y', enable=False)
        self.plot_widget.setYRange(-1, 16, padding=0)

    def show_error(self, msg):
        self.status_label.setText(f"❌ LỖI: {msg}")
        QMessageBox.critical(self, "Cảnh báo", msg)
        self.reset_button_ui()

    def reset_button_ui(self):
        self.btn_capture.setEnabled(True)
        self.btn_capture.setText("▶ BẤM ĐỂ ĐO TÍN HIỆU NGAY")
        self.btn_capture.setStyleSheet("""
            QPushButton { background-color: #d9534f; color: white;
                          font-size: 16px; font-weight: bold; padding: 10px; border-radius: 5px; }
            QPushButton:hover { background-color: #c9302c; }
        """)

    def closeEvent(self, event):
        if self.thread and self.thread.isRunning():
            self.thread.stop()
            self.thread.wait()
        event.accept()

if __name__ == '__main__':
    app = QApplication(sys.argv)
    window = LogicAnalyzerApp()
    window.show()
    sys.exit(app.exec_())
