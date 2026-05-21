import sys
import numpy as np
import serial
import serial.tools.list_ports
import pyqtgraph as pg
from PyQt5.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
                             QHBoxLayout, QPushButton, QComboBox, QLabel, QMessageBox,
                             QRadioButton, QGroupBox,QSplitter)
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
                
                # App tự động âm thầm gửi ký tự cấu hình xuống STM32
                ser.write(self.speed_cmd.encode())
                ser.write(self.mode_cmd.encode())
                
                # Xóa sạch rác
                ser.reset_input_buffer() 
                self.status_signal.emit("Trạng thái: Cấu hình xong! Đang rình mồi chờ tín hiệu Trigger...")

                # Đọc đúng 60.000 mẫu
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

        except Exception as e:
            self.error_signal.emit(f"Lỗi cổng {self.port}: Mạch bị lỏng cáp hoặc cổng COM đang bị phần mềm khác chiếm!")

    def stop(self):
        self.running = False
# GIAO DIỆN CHÍNH
class LogicAnalyzerApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("HUST Logic Analyzer - Bảng điều khiển trung tâm")
        self.resize(1050, 700)
        self.thread = None

        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        main_layout = QVBoxLayout(main_widget)
#  Thêm QSplitter chia màn hình 
        self.splitter = QSplitter(Qt.Vertical)
        main_layout.addWidget(self.splitter)

# Gói toàn bộ Bảng điều khiển vào một Panel phía trên
        top_panel = QWidget()
        layout = QVBoxLayout(top_panel)
        layout.setContentsMargins(0, 0, 0, 0)
        # KHU VỰC 1: BẢNG ĐIỀU KHIỂN BẰNG NÚT TÍCH (RADIO BUTTONS)
        control_layout = QHBoxLayout()

        #  Ô 1: Chọn cổng COM 
        com_group = QGroupBox("1. Kết nối (Cổng COM)")
        com_layout = QVBoxLayout()
        self.port_combo = QComboBox()
        self.refresh_ports()
        
        btn_refresh = QPushButton("🔄 Quét lại COM")
        btn_refresh.clicked.connect(self.refresh_ports)
        
        com_layout.addWidget(self.port_combo)
        com_layout.addWidget(btn_refresh)
        com_group.setLayout(com_layout)
        control_layout.addWidget(com_group)

        #  Ô 2: Tích chọn Tốc độ 
        speed_group = QGroupBox("2. Tốc độ lấy mẫu (Speed)")
        speed_layout = QVBoxLayout()
        self.rb_100k = QRadioButton("100 kHz")
        self.rb_500k = QRadioButton("500 kHz")
        self.rb_1m = QRadioButton("1 MHz")
        self.rb_2m = QRadioButton("2 MHz")
        
        self.rb_1m.setChecked(True)  # Mặc định tích chọn 1MHz
        
        speed_layout.addWidget(self.rb_100k)
        speed_layout.addWidget(self.rb_500k)
        speed_layout.addWidget(self.rb_1m)
        speed_layout.addWidget(self.rb_2m)
        speed_group.setLayout(speed_layout)
        control_layout.addWidget(speed_group)

        #  Ô 3: Tích chọn Kiểu lấy mẫu 
        mode_group = QGroupBox("3. Kiểu lấy mẫu (Trigger Mode)")
        mode_layout = QVBoxLayout()
        self.rb_pre = QRadioButton("Bắt cả Quá khứ (Pre-Trigger)")
        self.rb_post = QRadioButton("Chỉ bắt Tương lai (Full Post-Trigger)")
        
        self.rb_pre.setChecked(True) # Mặc định tích chọn Pre-trigger
        
        mode_layout.addWidget(self.rb_pre)
        mode_layout.addWidget(self.rb_post)
        mode_layout.addStretch() # Đẩy các nút lên trên cho đẹp
        mode_group.setLayout(mode_layout)
        control_layout.addWidget(mode_group)
        #  Ô 4: CHỌN BỘ GIẢI MÃ 
        decode_group = QGroupBox("4. Giải mã tín hiệu")
        decode_layout = QVBoxLayout()
        self.decode_combo = QComboBox()
        self.decode_combo.addItems([
            "Không giải mã", 
            "PWM (Kênh CH 0)", 
            "UART (Kênh CH 0, 115200)", 
            "SPI (CLK: CH0, MOSI: CH1)",
            "I2C (SCL: CH0, SDA: CH1)"
        ])
        decode_layout.addWidget(self.decode_combo)
        decode_layout.addStretch()
        decode_group.setLayout(decode_layout)
        control_layout.addWidget(decode_group)
        layout.addLayout(control_layout)

        # KHU VỰC 2: NÚT BẤM ĐO & TRẠNG THÁI
        action_layout = QHBoxLayout()
        
        self.btn_capture = QPushButton("▶ BẤM ĐỂ ĐO TÍN HIỆU NGAY")
        self.btn_capture.setStyleSheet("""
            QPushButton {
                background-color: #d9534f; color: white; 
                font-size: 16px; font-weight: bold; padding: 10px; border-radius: 5px;
            }
            QPushButton:hover { background-color: #c9302c; }
            QPushButton:disabled { background-color: #cccccc; color: #666666; }
        """)
        self.btn_capture.clicked.connect(self.start_capture)
        
        self.status_label = QLabel("Trạng thái: Sẵn sàng")
        self.status_label.setStyleSheet("color: blue; font-size: 14px; font-weight: bold;")
        
        action_layout.addWidget(self.btn_capture, stretch=1)
        action_layout.addWidget(self.status_label, stretch=2)
        layout.addLayout(action_layout)
        self.splitter.addWidget(top_panel) # : Nạp phần điều khiển vào Splitter
        # KHU VỰC 3: MÀN HÌNH ĐỒ THỊ 
        bottom_panel = QWidget() # Gói đồ thị vào panel dưới để dễ quản lý
        plot_layout = QVBoxLayout(bottom_panel)
        plot_layout.setContentsMargins(0, 0, 0, 0)
        pg.setConfigOption('background', '#f5f5f5') # Màu nền xám nhạt dịu mắt
        pg.setConfigOption('foreground', 'k')
        self.plot_widget = pg.PlotWidget()
        self.plot_widget.showGrid(x=True, y=True, alpha=0.3)
        self.plot_widget.setLabel('bottom', 'Thời gian (ms)')
        plot_layout.addWidget(self.plot_widget) #  Add vào plot_layout 
        self.splitter.addWidget(bottom_panel) #  Nạp đồ thị vào Splitter 
        self.splitter.setSizes([150, 550]) # Ép tỷ lệ bảng điều khiển luôn nhỏ  khi Fullscreen 
        # Tạo sẵn 8 đường ngang chờ dữ liệu
        self.curves = []
        yticks = []
        self.x_axis = np.arange(60001) # Trục X cố định từ 0 đến 60000
        for i in range(8):
            color = pg.intColor(i, hues=8, values=1, maxHue=360, alpha=255)
           
            curve = self.plot_widget.plot(pen=pg.mkPen(color, width=2.5), stepMode="center")
            self.curves.append(curve)
            yticks.append((i * 2 + 0.5, f"CH {i}")) 

        self.plot_widget.getAxis('left').setTicks([yticks])
        # Mở rộng dải Y xuống -1 để  không bị trục X đè lên
        self.plot_widget.setLimits(yMin=-1, yMax=16) # Khóa giới hạn
        self.plot_widget.setYRange(-1, 16, padding=0) 
        self.plot_widget.setMouseEnabled(y=False) # Cấm cuộn dọc, chỉ cho phép cuộn ngang
    # CÁC HÀM XỬ LÝ SỰ KIỆN 
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
      
       # Tự động dịch các nút Tích chọn thành Mã Lệnh cho STM32 VÀ lưu Tần số
        speed_cmd = '3'
        self.current_fs = 1000000 # Mặc định là 1 MHz (1.000.000 Hz)

        if self.rb_100k.isChecked(): 
            speed_cmd = '1'
            self.current_fs = 100000   # 100 kHz
        elif self.rb_500k.isChecked(): 
            speed_cmd = '2'
            self.current_fs = 500000   # 500 kHz
        elif self.rb_1m.isChecked(): 
            speed_cmd = '3'
            self.current_fs = 1000000  # 1 MHz
        elif self.rb_2m.isChecked(): 
            speed_cmd = '4'
            self.current_fs = 2000000  # 2 MHz

        mode_cmd = 'P' if self.rb_pre.isChecked() else 'F'

        # Đổi giao diện để báo hiệu đang chờ
        self.btn_capture.setEnabled(False)
        self.btn_capture.setText("⏳ ĐANG CHỜ TRIGGER...")
        self.btn_capture.setStyleSheet("background-color: #f0ad4e; color: white; font-size: 16px; font-weight: bold; padding: 10px;")
        
        # Bắn luồng xuống để xử lý
        self.thread = CaptureThread(port, speed_cmd, mode_cmd)
        self.thread.data_ready.connect(self.update_plot)
        self.thread.status_signal.connect(self.status_label.setText)
        self.thread.error_signal.connect(self.show_error)
        self.thread.start()

    def update_plot(self, data):
    # Tính toán chu kỳ của một mẫu (đơn vị mili-giây)
     # Công thức: T = (1 / Tần số) * 1000
        Ts_ms = (1.0 / self.current_fs) * 1000.0
        
    # Nhân toàn bộ mảng 0->60000 với chu kỳ để tạo mảng thời gian
        time_axis = self.x_axis * Ts_ms
     #  Khởi tạo mảng lưu dữ liệu thô 
        raw_channels = []

    #  Xóa đồ thị cũ và vẽ lại khung lưới 
        self.plot_widget.clear()
        self.curves = []
        yticks = []
        #  TẠO DANH SÁCH 8 MÀU TÙY Ý (Dùng mã HEX hoặc tên màu)
        # Ông có thể lên mạng gõ "Hex color picker" để lấy mã màu ưng ý nhé
        custom_colors = [
            '#FF0000',  # CH 0: Đỏ tươi (Red)
            '#00FF00',  # CH 1: Xanh lá cây (Green)
            '#0000FF',  # CH 2: Xanh dương (Blue)
            '#FF8C00',  # CH 3: Cam sậm (Dark Orange)
            '#FF00FF',  # CH 4: Tím hồng (Magenta)
            '#00CED1',  # CH 5: Xanh ngọc bích (Dark Turquoise)
            '#FFD700',  # CH 6: Vàng ánh kim (Gold)
            '#8B4513'   # CH 7: Nâu gỗ (Saddle Brown)
        ]

        for i in range(8):
            #  lấy màu trực tiếp từ mảng trên
            color = custom_colors[i] 
            curve = self.plot_widget.plot(pen=pg.mkPen(color, width=2.5), stepMode="center")
            self.curves.append(curve)
            yticks.append((i * 2 + 0.5, f"CH {i}"))
        self.plot_widget.getAxis('left').setTicks([yticks])
        # Thuật toán tách 8 kênh 
        for i in range(8):
            bit_array = (data >> i) & 1
            raw_channels.append(bit_array) #  Lưu data thô cho từng kênh vào mảng 
            y_offset = bit_array * 1.0 + (i * 2)
            self.curves[i].setData(x=time_axis, y=y_offset) #  nạp cả trục x và trục y cùng lúc 
        
        #  CHẠY BỘ GIẢI MÃ & VẼ CHỮ LÊN MÀN HÌNH
        decode_sel = self.decode_combo.currentIndex()
        packets = []
        
        try:
            if decode_sel == 1: # PWM ở CH0
                packets = decoders.decode_pwm(raw_channels[0], self.current_fs)
            elif decode_sel == 2: # UART ở CH0 (mặc định 115200)
                packets = decoders.decode_uart(raw_channels[0], self.current_fs, 115200)
            elif decode_sel == 3: # SPI với CLK=CH0, MOSI=CH1
                packets = decoders.decode_spi(raw_channels[0], raw_channels[1], self.current_fs)
            elif decode_sel == 4: # I2C với SCL=CH0, SDA=CH1
                packets = decoders.decode_i2c(raw_channels[0], raw_channels[1], self.current_fs)
                
            # Duyệt qua các gói tin và gắn chữ lên đồ thị
            for pkt in packets:
                text_item = pg.TextItem(text=pkt['text'], color=(0, 0, 0), anchor=(0.5, 1))
                text_item.fill = pg.mkBrush(255, 255, 0, 150) # Nền vàng mờ
                
                # Tính toán tọa độ đặt chữ
                mid_idx = int((pkt['start'] + pkt['end']) / 2)
                x_pos = mid_idx * Ts_ms
                y_pos = 1.2 # Đặt chữ nổi phía trên đường baseline của CH0
                
                text_item.setPos(x_pos, y_pos)
                self.plot_widget.addItem(text_item)
                
        except Exception as e:
            print("Lỗi giải mã:", e) # Báo lỗi ra terminal, không làm sập App
        self.status_label.setText("✅ VẼ XONG! Kéo chuột để di chuyển, Lăn chuột để Zoom.")
        self.reset_button_ui()
    #  Chỉ cho auto zoom trục X để nhìn thấy sóng, KHÓA CHẶT trục Y
        self.plot_widget.enableAutoRange(axis='x')
        self.plot_widget.enableAutoRange(axis='y', enable=False)
        self.plot_widget.setYRange(-1, 16, padding=0) # Ép lại Y một lần nữa cho chắc
    def show_error(self, msg):
        self.status_label.setText(f"❌ LỖI: {msg}")
        QMessageBox.critical(self, "Cảnh báo", msg)
        self.reset_button_ui()

    def reset_button_ui(self):
        self.btn_capture.setEnabled(True)
        self.btn_capture.setText("▶ BẤM ĐỂ ĐO TÍN HIỆU NGAY")
        self.btn_capture.setStyleSheet("""
            QPushButton { background-color: #d9534f; color: white; font-size: 16px; font-weight: bold; padding: 10px; border-radius: 5px;}
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