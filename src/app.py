import sys
import numpy as np
import serial
import serial.tools.list_ports
import pyqtgraph as pg
from PyQt5.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
                             QHBoxLayout, QPushButton, QComboBox, QLabel, QMessageBox,
                             QRadioButton, QGroupBox, QSplitter, QScrollArea, QFrame,)
from PyQt5.QtCore import QThread, pyqtSignal, Qt,QRectF
# Import bộ giải mã tín hiệu
import Decoders

# Thread chạy ngầm để đọc cổng Serial, giúp giao diện chính không bị đơ khi chờ dữ liệu
class CaptureThread(QThread):
    # Các tín hiệu (signal) để giao tiếp với giao diện chính
    data_ready    = pyqtSignal(np.ndarray)
    error_signal  = pyqtSignal(str)
    status_signal = pyqtSignal(str)

    def __init__(self, port, speed_cmd, mode_cmd):
        super().__init__()
        self.port      = port
        self.speed_cmd = speed_cmd
        self.mode_cmd  = mode_cmd
        self.running   = True

    def run(self):
        try:
            # Mở cổng COM với baudrate 115200, timeout 1s
            with serial.Serial(self.port, 115200, timeout=1) as ser:
                self.status_signal.emit(f"Đã kết nối {self.port}. Đang cấu hình STM32...")
                
                # Gửi cấu hình tốc độ và chế độ lấy mẫu xuống phần cứng
                ser.write(self.speed_cmd.encode())
                ser.write(self.mode_cmd.encode())
                ser.reset_input_buffer()
                self.status_signal.emit("Cấu hình xong! Đang rình mồi chờ tín hiệu Trigger...")

                # Vòng lặp chờ nhận đủ 60.000 byte từ STM32 sau khi có Trigger
                buffer = bytearray()
                while self.running and len(buffer) < 60000:
                    chunk = ser.read(60000 - len(buffer))
                    if chunk:
                        buffer.extend(chunk)

                # Nếu nhận đủ dữ liệu, chuyển thành mảng numpy và báo cho GUI vẽ đồ thị
                if len(buffer) == 60000:
                    self.status_signal.emit("Vừa hốt trọn 60.000 mẫu! Đang vẽ đồ thị...")
                    data = np.frombuffer(buffer, dtype=np.uint8)
                    self.data_ready.emit(data)
                else:
                    if self.running:
                        self.error_signal.emit("Đã hủy hoặc bị quá thời gian chờ!")
        except Exception:
            self.error_signal.emit(f"Lỗi cổng {self.port}: Mạch bị lỏng cáp hoặc COM đang bị chiếm!")

    def stop(self):
        self.running = False


# Widget đại diện cho một thanh cấu hình bộ giải mã trên giao diện
class DecodeConfigWidget(QWidget):
    removed = pyqtSignal(object)

    # Khai báo các chân cần thiết tương ứng với mỗi chuẩn giao tiếp
    # THÊM CHÂN CS VÀO GIAO THỨC SPI (Định nghĩa chân thứ 4)
    PIN_DEFS = {
        "PWM":    [("Data", 0)],
        "UART":   [("TX",   0),("RX",   1)],
        "SPI":    [("CLK",  0), ("MOSI", 1), ("MISO", 2), ("CS", 3)],
        "I2C":    [("SCL",  0), ("SDA",  1)],
        "1-Wire": [("Data", 0)],
    }
    
    # Khai báo các tùy chọn nâng cao (ví dụ SPI Mode 0-3)
    # THÊM TÙY CHỌN PARITY CHO UART
    OPTION_DEFS = {
        "SPI": [
            ("Mode", [
                "0 (CPOL=0 CPHA=0)",
                "1 (CPOL=0 CPHA=1)",
                "2 (CPOL=1 CPHA=0)",
                "3 (CPOL=1 CPHA=1)",
            ], 0),
        ],
        "UART": [
            ("Parity", [
                "None (8N1)",
                "Even (8E1)",
                "Odd (8O1)",
            ], 0),
        ],
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        outer = QHBoxLayout(self)
        outer.setContentsMargins(2, 3, 2, 3)
        outer.setSpacing(4)
        
        # Menu thả xuống (ComboBox) để chọn loại giao thức (UART, SPI...)
        self.type_combo = QComboBox()
        self.type_combo.setMinimumWidth(75)
        self.type_combo.addItems(["Chọn...", "PWM", "UART", "SPI", "I2C", "1-Wire"])
        self.type_combo.currentIndexChanged.connect(self._rebuild)
        outer.addWidget(self.type_combo)
        
        # Khu vực chứa các ComboBox để gán kênh (CH0 -> CH7) cho từng chân
        self.pin_container = QWidget()
        self.pin_layout = QHBoxLayout(self.pin_container)
        self.pin_layout.setContentsMargins(0, 0, 0, 0)
        self.pin_layout.setSpacing(3)
        outer.addWidget(self.pin_container)
        
        # Khu vực chứa các ComboBox cho tùy chọn bổ sung (như SPI Mode, Parity)
        self.opt_container = QWidget()
        self.opt_layout = QHBoxLayout(self.opt_container)
        self.opt_layout.setContentsMargins(0, 0, 0, 0)
        self.opt_layout.setSpacing(3)
        outer.addWidget(self.opt_container)
        
        # Nút xóa bộ giải mã hiện tại khỏi danh sách
        btn_rm = QPushButton("✕")
        btn_rm.setFixedSize(22, 22)
        btn_rm.setStyleSheet("color: #c0392b; font-weight: bold; border: none;")
        btn_rm.clicked.connect(lambda: self.removed.emit(self))
        outer.addWidget(btn_rm)
        outer.addStretch()
        
        self.pin_widgets = []    
        self.opt_widgets = []    

    # Xây dựng lại UI (hiển thị các chân phù hợp) khi người dùng đổi loại giao thức
    def _rebuild(self):
        self._clear_layout(self.pin_layout)
        self._clear_layout(self.opt_layout)
        self.pin_widgets = []
        self.opt_widgets = []
        type_name = self.type_combo.currentText()
        if type_name not in self.PIN_DEFS:
            return
        
        # Tạo danh sách các kênh từ CH 0 đến CH 7
        ch_options = [f"CH {i}" for i in range(8)]
        
        # Tạo UI chọn kênh cho từng chân tương ứng với giao thức đã chọn
        for pin_name, default_ch in self.PIN_DEFS[type_name]:
            lbl = QLabel(f"{pin_name}:")
            lbl.setStyleSheet("font-size: 11px; color: #333;")
            cb = QComboBox()
            # Riêng chân CS cho phép chọn thêm option "Bỏ qua (GND)"
            if pin_name in ("CS", "TX", "RX"):
                cb.addItem("GND")
                cb.addItems(ch_options)
                cb.setCurrentIndex(default_ch + 1) # Mặc định trỏ tới CH 3
            else:
                cb.addItems(ch_options)
                cb.setCurrentIndex(default_ch)
            cb.setMaximumWidth(65 if pin_name in ("CS", "TX", "RX") else 58)
            self.pin_layout.addWidget(lbl)
            self.pin_layout.addWidget(cb)
            self.pin_widgets.append((pin_name, cb))
            
        # Tạo UI cho các tùy chọn nâng cao nếu giao thức có yêu cầu (như SPI, UART)
        sep = QLabel("|")
        sep.setStyleSheet("color: #aaa; font-size: 11px;")
        if type_name in self.OPTION_DEFS:
            self.opt_layout.addWidget(sep)
            for opt_name, choices, default_idx in self.OPTION_DEFS[type_name]:
                lbl = QLabel(f"{opt_name}:")
                lbl.setStyleSheet("font-size: 11px; color: #555;")
                cb = QComboBox()
                cb.addItems(choices)
                cb.setCurrentIndex(default_idx)
                cb.setMaximumWidth(135)
                self.opt_widgets.append((opt_name, cb))
                self.opt_layout.addWidget(lbl)
                self.opt_layout.addWidget(cb)
        

    # Hàm phụ trợ dọn dẹp các Widget cũ trước khi tạo lại
    @staticmethod
    def _clear_layout(layout):
        while layout.count():
            item = layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    # Trích xuất cấu hình hiện tại do người dùng chọn thành định dạng Dictionary
    def get_config(self):
        type_name = self.type_combo.currentText()
        if type_name == "Chọn...":
            return None
        
        # Xử lý mapping kênh cho chân CS (Index 0 = GND -> None, Index 1..8 -> CH0..CH7)
        channels = {}
        for name, cb in self.pin_widgets:
            if name in ("CS", "TX", "RX"):
                idx = cb.currentIndex()
                channels[name] = None if idx == 0 else (idx - 1)
            else:
                channels[name] = cb.currentIndex()
                
        return {
            "type":     type_name,
            "channels": channels,
            "options":  {name: cb.currentIndex() for name, cb in self.opt_widgets},
        }


# Lớp chính khởi tạo toàn bộ giao diện phần mềm
class LogicAnalyzerApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("HUST Logic Analyzer - Bảng điều khiển trung tâm")
        self.resize(1150, 700)
        
        # Khởi tạo các biến quản lý luồng dữ liệu và danh sách bộ giải mã
        self.thread             = None
        self.last_raw_channels  = None
        self.last_ts_ms         = None
        self.decode_text_items  = []
        self.decode_config_widgets = []
        self.current_fs         = 1_000_000
        
        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        main_layout = QVBoxLayout(main_widget)
        
        # Thanh kéo điều chỉnh tỷ lệ giữa bảng điều khiển và đồ thị
        self.splitter = QSplitter(Qt.Vertical)
        main_layout.addWidget(self.splitter)
        
        # --- KHU VỰC BẢNG ĐIỀU KHIỂN (TOP PANEL) ---
        top_panel = QWidget()
        layout    = QVBoxLayout(top_panel)
        layout.setContentsMargins(0, 0, 0, 0)
        control_layout = QHBoxLayout()
        
        # Bảng 1: Chọn và làm mới cổng COM
        com_group = QGroupBox("1. Kết nối (Cổng COM)")
        com_layout = QVBoxLayout()
        self.port_combo = QComboBox()
        self.refresh_ports()
        btn_refresh = QPushButton("🔄 Quét lại COM")
        btn_refresh.clicked.connect(self.refresh_ports)
        com_layout.addWidget(self.port_combo)
        com_layout.addWidget(btn_refresh)
        com_group.setLayout(com_layout)
        
        # Bảng 2: Chọn tần số lấy mẫu (Speed)
        speed_group  = QGroupBox("2. Tốc độ lấy mẫu (Speed)")
        speed_layout = QVBoxLayout()
        self.rb_100k = QRadioButton("100 kHz")
        self.rb_500k = QRadioButton("500 kHz")
        self.rb_1m   = QRadioButton("1 MHz")
        self.rb_2m   = QRadioButton("2 MHz")
        self.rb_1m.setChecked(True)
        for rb in (self.rb_100k, self.rb_500k, self.rb_1m, self.rb_2m):
            speed_layout.addWidget(rb)
        speed_group.setLayout(speed_layout)
        
        # Bảng 3: Chọn chế độ Trigger (Pre-trigger hoặc Post-trigger)
        mode_group  = QGroupBox("3. Kiểu lấy mẫu (Trigger Mode)")
        mode_layout = QVBoxLayout()
        self.rb_pre  = QRadioButton("Bắt cả Quá khứ (Pre-Trigger)")
        self.rb_post = QRadioButton("Chỉ bắt Tương lai (Full Post-Trigger)")
        self.rb_pre.setChecked(True)
        mode_layout.addWidget(self.rb_pre)
        mode_layout.addWidget(self.rb_post)
        mode_layout.addStretch()
        mode_group.setLayout(mode_layout)
        
        # Bảng 4: Quản lý danh sách các bộ giải mã
        decode_group = QGroupBox("4. Giải mã tín hiệu (có thể chạy nhiều bộ cùng lúc)")
        decode_outer = QVBoxLayout()
        decode_outer.setSpacing(4)
        hint = QLabel("① Chọn loại  →  ② Gán kênh  →  ③ Tuỳ chỉnh (nếu có)")
        hint.setStyleSheet("color: #555; font-size: 11px; font-style: italic;")
        hint.setFixedHeight(16)
        decode_outer.addWidget(hint)
        
        # Vùng cuộn (Scroll Area) chứa danh sách các DecodeConfigWidget
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setMinimumHeight(80)
        self.decode_list_widget = QWidget()
        self.decode_list_layout = QVBoxLayout(self.decode_list_widget)
        self.decode_list_layout.setContentsMargins(0, 0, 0, 0)
        self.decode_list_layout.setSpacing(2)
        self.decode_list_layout.addStretch()
        scroll.setWidget(self.decode_list_widget)
        decode_outer.addWidget(scroll, stretch=1)
        
        # Hàng nút Thêm bộ giải mã và Yêu cầu giải mã ngay lập tức
        btn_add = QPushButton("＋  Thêm bộ giải mã")
        btn_add.setStyleSheet("""
            QPushButton { background-color: #27ae60; color: white;
                          font-weight: bold; border-radius: 4px; padding: 4px 8px; }
            QPushButton:hover { background-color: #219a52; }
        """)
        btn_add.setMaximumWidth(220)
        btn_add.clicked.connect(self.add_decode_config)
        
        self.btn_decode_now = QPushButton("Giải mã")
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
        
        # Đưa 4 bảng điều khiển vào Layout chính
        control_layout.addWidget(com_group,    stretch=2)
        control_layout.addWidget(speed_group,  stretch=2)
        control_layout.addWidget(mode_group,   stretch=3)
        control_layout.addWidget(decode_group, stretch=5)
        layout.addLayout(control_layout)
        
        # Nút nhấn kích hoạt việc đo tín hiệu và nhãn trạng thái hiển thị tiến trình
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
        action_layout.addWidget(self.btn_capture,  stretch=1)
        action_layout.addWidget(self.status_label,  stretch=2)
        layout.addLayout(action_layout)
        self.splitter.addWidget(top_panel)
        
        # --- KHU VỰC VẼ ĐỒ THỊ (BOTTOM PANEL) ---
        bottom_panel = QWidget()
        plot_layout  = QVBoxLayout(bottom_panel)
        plot_layout.setContentsMargins(0, 0, 0, 0)
        
        # Cấu hình màu nền cho biểu đồ PyQtGraph
        pg.setConfigOption('background', '#f5f5f5')
        pg.setConfigOption('foreground', 'k')
        self.plot_widget = pg.PlotWidget()
        self.plot_widget.showGrid(x=True, y=True, alpha=0.3)
        self.plot_widget.setLabel('bottom', 'Thời gian (ms)')
        plot_layout.addWidget(self.plot_widget)
        self.splitter.addWidget(bottom_panel)
        self.splitter.setSizes([170, 530])
        
        # Khởi tạo 8 đường kẻ ngang mặc định đại diện cho 8 kênh chưa có dữ liệu
        self.curves  = []
        yticks       = []
        self.x_axis  = np.arange(60001)
        for i in range(8):
            color = pg.intColor(i, hues=8, values=1, maxHue=360, alpha=255)
            curve = self.plot_widget.plot(pen=pg.mkPen(color, width=2.5), stepMode="center")
            self.curves.append(curve)
            yticks.append((i * 2 + 0.5, f"CH {i}"))
            
        # Cấu hình trục Y hiển thị tên các kênh
        self.plot_widget.getAxis('left').setTicks([yticks])
        self.plot_widget.setLimits(yMin=-1, yMax=16)
        self.plot_widget.setYRange(-1, 16, padding=0)
        # Khóa di chuyển/zoom trục Y, chỉ cho phép kéo thả theo trục X
        self.plot_widget.setMouseEnabled(y=False)

    # Thêm một Widget cấu hình bộ giải mã mới vào UI
    def add_decode_config(self):
        w = DecodeConfigWidget()
        w.removed.connect(self.remove_decode_config)
        self.decode_config_widgets.append(w)
        idx = self.decode_list_layout.count() - 1
        self.decode_list_layout.insertWidget(idx, w)

    # Xóa Widget cấu hình giải mã khỏi UI
    def remove_decode_config(self, w):
        if w in self.decode_config_widgets:
            self.decode_config_widgets.remove(w)
        self.decode_list_layout.removeWidget(w)
        w.deleteLater()

    # Dọn dẹp tất cả các nhãn Text (Kết quả giải mã) hiện có trên đồ thị
    def clear_decode_annotations(self):
        for item in self.decode_text_items:
            try:
                self.plot_widget.removeItem(item)
            except Exception:
                pass
        self.decode_text_items = []

    # Hàm gọi giải mã lại tín hiệu đã lưu trữ trong bộ nhớ (mà không cần đo lại)
    def decode_last_capture(self):
        if self.last_raw_channels is None or self.last_ts_ms is None:
            QMessageBox.information(self, "Chưa có dữ liệu", "Hãy chụp tín hiệu trước.")
            return
        count = self.apply_decoders(self.last_raw_channels, self.last_ts_ms)
        self.status_label.setText(f"Đã giải mã lại tín hiệu vừa chụp: {count} nhãn.")

    # TRỌNG TÂM: Hàm cầu nối, đưa dữ liệu từ giao diện vào bộ phân tích Decoders
    def apply_decoders(self, raw_channels, Ts_ms):
        self.clear_decode_annotations()
        
        # Hàm nội bộ lấy mảng dữ liệu logic của kênh tương ứng với tên chân do người dùng cấu hình
        def _ch(cfg, name):
            ch_idx = cfg["channels"].get(name)
            return raw_channels[ch_idx] if ch_idx is not None else None
            
        def _opt(cfg, name, default=0):
            return cfg["options"].get(name, default)
            
        # BẢN ĐỒ KẾT NỐI: TÍCH HỢP PARITY VÀ CHÂN CS VÀO LUỒNG GIẢI MÃ
        parity_map = {0: 'N', 1: 'E', 2: 'O'}
        decode_funcs = {
            "UART": lambda cfg, rc: (
                Decoders.decode_uart(
                    _ch(cfg, "TX"), _ch(cfg, "RX"), self.current_fs, 
                    parity=parity_map.get(_opt(cfg, "Parity", 0), 'N') 
                ),
                [cfg["channels"].get("TX"), cfg["channels"].get("RX")]
            ),
            "PWM": lambda cfg, rc: (
                Decoders.decode_pwm(_ch(cfg, "Data"), self.current_fs),
                cfg["channels"]["Data"]
            ),
            "SPI": lambda cfg, rc: (
                Decoders.decode_spi(
                    _ch(cfg, "CLK"), _ch(cfg, "MOSI"), _ch(cfg, "MISO"),
                    self.current_fs, mode=_opt(cfg, "Mode", default=0),
                    cs=_ch(cfg, "CS") # CẤU HÌNH TRUYỀN CHÂN CS
                ),
                [cfg["channels"]["MOSI"], cfg["channels"]["MISO"]]
            ),
            "I2C": lambda cfg, rc: (
                Decoders.decode_i2c(_ch(cfg, "SCL"), _ch(cfg, "SDA"), self.current_fs),
                cfg["channels"]["SDA"]
            ),
            "1-Wire": lambda cfg, rc: (
                Decoders.decode_1wire(_ch(cfg, "Data"), self.current_fs),
                cfg["channels"]["Data"]
            ),
        }
        
        label_count = 0
        
        # Duyệt qua tất cả các bộ giải mã mà người dùng đang mở trên UI
        for cfg_widget in self.decode_config_widgets:
            cfg = cfg_widget.get_config()
            if cfg is None:
                continue
            t = cfg["type"]
            if t not in decode_funcs:
                continue
            try:
                # Gọi hàm giải mã tương ứng
                packets, ref_ch = decode_funcs[t](cfg, raw_channels)
                ref_channels = ref_ch if isinstance(ref_ch, list) else [ref_ch]
            
                # Duyệt qua các gói để dán nhãn
                for pkt in packets:
                    for ch in ref_channels:
                        if ch is None:
                            continue
                        
                        # TÁCH NHÃN SPI ĐỂ MOSI IN TRÊN KÊNH MOSI, MISO IN TRÊN KÊNH MISO
                        label_str = pkt["text"]
                        if t == "SPI":
                            if ch == cfg["channels"].get("MOSI") and "mosi_text" in pkt:
                                label_str = pkt["mosi_text"]
                            elif ch == cfg["channels"].get("MISO") and "miso_text" in pkt:
                                label_str = pkt["miso_text"]

                        # 1. Xác định các loại nhãn đặc biệt
                        is_boundary = (t == "I2C" and pkt["text"] in ("S", "P")) or \
                                      (t == "1-Wire" and "RESET" in pkt["text"] or "PRESENCE" in pkt["text"])
                        is_ack_nack = (t == "I2C" and pkt["text"] in ("A", "N"))
                        
                        # Màu chữ
                        label_color = (255, 255, 255) if is_boundary else (0, 0, 0)
                        
                        # 2. Tạo nhãn
                        text_item = pg.TextItem(text=label_str, color=label_color, anchor=(0.5, 1))
                        
                        # 3. Phân loại màu nền (Brush) - BẢNG MÀU 4 CẤP ĐỘ
                        if is_boundary:
                            bg_brush = pg.mkBrush(220, 0, 0, 190)      # Đỏ cho Start/Stop/RESET/PRESENCE
                        elif is_ack_nack:
                            bg_brush = pg.mkBrush(255, 165, 0, 190)    # Cam cho ACK/NACK
                        elif "0x" in label_str:                       
                            bg_brush = pg.mkBrush(0, 180, 220, 180)    # Xanh ngọc cho các gói Byte (0x00, 0x1E...)
                        else:
                            bg_brush = pg.mkBrush(255, 255, 0, 150)    # Vàng cho các xung bit lẻ
                            
                        text_item.fill = bg_brush
                        
                        # 4. Tính toán vị trí Y phân tầng thông minh (Tiered Rendering)
                        mid_idx = int((pkt["start"] + pkt["end"]) / 2)
                        x_pos   = mid_idx * Ts_ms
                        y_pos   = ch * 2 + 1.15
                        
                        if t == "1-Wire":
                            # Dùng label_str thay vì display_text, đẩy gói lớn lên tầng cao
                            if any(k in label_str for k in ("0x", "RESET", "PRESENCE")):
                                y_pos += 0.55
                        elif t == "I2C":
                            if "0x" in label_str:
                                y_pos += 0.50
                        elif t == "SPI":
                            if ch == cfg["channels"].get("MOSI") and "mosi_text" in pkt:
                                label_str = pkt["mosi_text"]
                            elif ch == cfg["channels"].get("MISO") and "miso_text" in pkt:
                                label_str = pkt["miso_text"]
                        elif t == "UART":
                            # Đảm bảo dữ liệu TX chỉ vẽ trên dây TX, RX chỉ vẽ trên dây RX
                            if pkt.get("line") == "TX" and ch != cfg["channels"].get("TX"):
                                continue
                            if pkt.get("line") == "RX" and ch != cfg["channels"].get("RX"):
                                continue
                        elif "0x" in label_str or "Bit" in label_str:
                            y_pos += 0.40
                            
                        text_item.setPos(x_pos, y_pos)
                        self.plot_widget.addItem(text_item)
                        self.decode_text_items.append(text_item)
                        label_count += 1
            except Exception as e:
                print(f"Lỗi giải mã {t}:", e)
        return label_count

    # Quét hệ thống máy tính để tìm các cổng USB Serial đang kết nối
    def refresh_ports(self):
        self.port_combo.clear()
        ports = serial.tools.list_ports.comports()
        if ports:
            for p in ports:
                self.port_combo.addItem(p.device)
        else:
            self.port_combo.addItem("Không thấy mạch!")

    # Hàm bắt đầu gửi lệnh yêu cầu đo tới STM32
    def start_capture(self):
        port = self.port_combo.currentText().strip()
        if port == "Không thấy mạch!" or not port:
            QMessageBox.warning(self, "Lỗi", "Không tìm thấy cổng COM")
            return
            
        # Ánh xạ từ RadioButton sang lệnh Command Char cho STM32
        speed_cmd = '3'
        self.current_fs = 1_000_000
        if   self.rb_100k.isChecked(): speed_cmd = '1'; self.current_fs = 100_000
        elif self.rb_500k.isChecked(): speed_cmd = '2'; self.current_fs = 500_000
        elif self.rb_1m.isChecked():   speed_cmd = '3'; self.current_fs = 1_000_000
        elif self.rb_2m.isChecked():   speed_cmd = '4'; self.current_fs = 2_000_000
        mode_cmd = 'P' if self.rb_pre.isChecked() else 'F'
        
        # Khóa nút bấm và khởi tạo luồng chạy ngầm để lấy mẫu
        self.btn_capture.setEnabled(False)
        self.btn_capture.setText("⏳ ĐANG CHỜ TRIGGER...")
        self.btn_capture.setStyleSheet(
            "background-color: #f0ad4e; color: white; "
            "font-size: 16px; font-weight: bold; padding: 10px;")
            
        self.thread = CaptureThread(port, speed_cmd, mode_cmd)
        self.thread.data_ready.connect(self.update_plot)
        self.thread.status_signal.connect(self.status_label.setText)
        self.thread.error_signal.connect(self.show_error)
        self.thread.start()

    # Nhận 60.000 byte dữ liệu từ luồng ngầm, vẽ lại sóng và gọi bộ giải mã
    def update_plot(self, data):
        Ts_ms     = (1.0 / self.current_fs) * 1000.0
        time_axis = self.x_axis * Ts_ms
        self.plot_widget.clear()
        self.curves = []
        yticks      = []
        custom_colors = [
            '#FF0000', '#00FF00', '#0000FF', '#FF8C00',
            '#FF00FF', '#00CED1', '#FFD700', '#8B4513',
        ]
        
        # Vẽ cấu hình lưới đồ thị ban đầu
        for i in range(8):
            curve = self.plot_widget.plot(
                pen=pg.mkPen(custom_colors[i], width=2.5), stepMode="center")
            self.curves.append(curve)
            yticks.append((i * 2 + 0.5, f"CH {i}"))
        self.plot_widget.getAxis('left').setTicks([yticks])
        
        # Tách từng bit trong biến uint8 thành 8 mảng 0/1 riêng biệt (cho 8 kênh logic)
        raw_channels = []
        for i in range(8):
            bit_array = ((data >> i) & 1).astype(np.int8)
            raw_channels.append(bit_array)
            # Nâng đồ thị kênh này lên đúng vị trí của nó trên trục Y
            y_offset = bit_array * 1.0 + (i * 2)
            self.curves[i].setData(x=time_axis, y=y_offset)
            
        self.last_raw_channels = raw_channels
        self.last_ts_ms        = Ts_ms
        
        # Thực thi hàm giải mã và nhãn lên đồ thị
        label_count = self.apply_decoders(raw_channels, Ts_ms)
        self.status_label.setText(f"Vẽ xong! Đã gắn {label_count} nhãn giải mã.")
        self.reset_button_ui()
        self.plot_widget.enableAutoRange(axis='x')
        self.plot_widget.enableAutoRange(axis='y', enable=False)
        self.plot_widget.setYRange(-1, 16, padding=0)

    # Hiển thị lỗi nếu quá trình giao tiếp cổng COM thất bại
    def show_error(self, msg):
        self.status_label.setText(f"❌ LỖI: {msg}")
        QMessageBox.critical(self, "Cảnh báo", msg)
        self.reset_button_ui()

    # Mở khóa nút bấm giao diện để đo lần tiếp theo
    def reset_button_ui(self):
        self.btn_capture.setEnabled(True)
        self.btn_capture.setText("▶ BẤM ĐỂ ĐO TÍN HIỆU NGAY")
        self.btn_capture.setStyleSheet("""
            QPushButton { background-color: #d9534f; color: white;
                          font-size: 16px; font-weight: bold; padding: 10px; border-radius: 5px; }
            QPushButton:hover { background-color: #c9302c; }
        """)

    # Đảm bảo luồng chạy ngầm kết thúc an toàn khi đóng cửa sổ phần mềm
    def closeEvent(self, event):
        if self.thread and self.thread.isRunning():
            self.thread.stop()
            self.thread.wait()
        event.accept()

if __name__ == '__main__':
    app    = QApplication(sys.argv)
    window = LogicAnalyzerApp()
    window.show()
    sys.exit(app.exec_())