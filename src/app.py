import sys
import serial
import numpy as np
import pyqtgraph as pg
from PyQt5.QtWidgets import (QApplication, QMainWindow, QPushButton, QVBoxLayout, 
                             QHBoxLayout, QWidget, QComboBox, QLabel, QLineEdit, QFileDialog, QStatusBar)
from PyQt5.QtCore import QThread, pyqtSignal, Qt

# =====================================================================
# THREAD XỬ LÝ SERIAL
# =====================================================================
class SerialWorker(QThread):
    log_msg = pyqtSignal(str)
    data_ready = pyqtSignal(np.ndarray, bytes)
    finished_task = pyqtSignal()

    def __init__(self, port, speed_cmd, mode_cmd, samples=60000):
        super().__init__()
        self.port = port
        self.speed_cmd = speed_cmd
        self.mode_cmd = mode_cmd
        self.samples = samples # Đã cập nhật thành 60.000 theo code C++

    def run(self):
        try:
            self.log_msg.emit(f"Đang kết nối {self.port}...")
            # Timeout 15s để bạn có đủ thời gian kích hoạt trigger trên mạch
            with serial.Serial(self.port, 115200, timeout=15) as ser:
                ser.reset_input_buffer()
                
                ser.write(self.speed_cmd.encode())
                self.msleep(100)
                ser.write(self.mode_cmd.encode())
                self.msleep(100)
                
                self.log_msg.emit("Đang chờ tín hiệu Trigger...")
                
                raw_bytes = ser.read(self.samples)
                
                if len(raw_bytes) == self.samples:
                    self.log_msg.emit(f"✓ Đã capture {self.samples} mẫu thành công")
                    raw_data = np.frombuffer(raw_bytes, dtype=np.uint8)
                    self.data_ready.emit(raw_data, raw_bytes)
                else:
                    self.log_msg.emit(f"Lỗi: Chỉ nhận {len(raw_bytes)}/{self.samples} byte (Hết thời gian chờ).")
                    
        except Exception as e:
            self.log_msg.emit(f"Lỗi cổng: {e}")
        finally:
            self.finished_task.emit()

# =====================================================================
# GIAO DIỆN CHÍNH
# =====================================================================
class LogicAnalyzerApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Logic Analyzer")
        self.resize(1280, 720)
        self.SAMPLE_COUNT = 60000
        self.raw_bytes_cache = None # Dùng để lưu file .bin

        self.setup_ui()

    def setup_ui(self):
        # --- STYLESHEET (Giao diện Dark Mode) ---
        self.setStyleSheet("""
            QMainWindow { background-color: #0b111a; }
            QLabel { color: #64748b; font-size: 11px; font-weight: bold; }
            QPushButton { 
                background-color: #1e293b; color: #cbd5e1; 
                border: 1px solid #334155; padding: 6px 12px; border-radius: 3px; font-weight: bold;
            }
            QPushButton:hover { background-color: #334155; }
            QPushButton#capture { 
                background-color: #059669; color: white; border: 1px solid #047857; 
            }
            QPushButton#capture:hover { background-color: #10b981; }
            QPushButton#capture:disabled { background-color: #b45309; color: white; border: none; }
            QComboBox, QLineEdit { 
                background-color: #1e293b; color: white; 
                border: 1px solid #334155; padding: 4px; border-radius: 3px;
            }
            QStatusBar { background-color: #0f172a; color: #64748b; }
        """)

        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        main_layout = QVBoxLayout()
        main_layout.setContentsMargins(10, 10, 10, 5)
        main_widget.setLayout(main_layout)

        # --- TOP TOOLBAR ---
        toolbar = QHBoxLayout()
        
        # Logo / Title
        title = QLabel("◆ LOGIC ANALYZER")
        title.setStyleSheet("color: #10b981; font-size: 14px; margin-right: 15px;")
        toolbar.addWidget(title)

        # Cổng COM
        self.port_input = QLineEdit("COM6")
        self.port_input.setFixedWidth(60)
        toolbar.addWidget(self.port_input)

        toolbar.addSpacing(15)

        # RATE
        toolbar.addWidget(QLabel("RATE"))
        self.speed_combo = QComboBox()
        self.speed_combo.addItems(["100k", "500k", "1M", "2M"])
        self.speed_combo.setCurrentIndex(2) # Default 1M
        toolbar.addWidget(self.speed_combo)

        toolbar.addSpacing(10)

        # MODE
        toolbar.addWidget(QLabel("MODE"))
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("Pre-trig (10k)", "P")
        self.mode_combo.addItem("Full Post", "F")
        self.mode_combo.setCurrentIndex(1)
        toolbar.addWidget(self.mode_combo)

        toolbar.addSpacing(15)

        # NÚT CAPTURE
        self.btn_capture = QPushButton("▶ CAPTURE")
        self.btn_capture.setObjectName("capture")
        self.btn_capture.clicked.connect(self.start_capture)
        toolbar.addWidget(self.btn_capture)

        toolbar.addStretch()

        # ZOOM CONTROLS
        toolbar.addWidget(QLabel("ZOOM"))
        btn_zoom_in = QPushButton("+")
        btn_zoom_in.setFixedWidth(30)
        btn_zoom_in.clicked.connect(lambda: self.plot_widget.getViewBox().scaleBy(x=0.5))
        
        btn_zoom_out = QPushButton("-")
        btn_zoom_out.setFixedWidth(30)
        btn_zoom_out.clicked.connect(lambda: self.plot_widget.getViewBox().scaleBy(x=2.0))
        
        btn_fit = QPushButton("Fit")
        btn_fit.clicked.connect(self.fit_view)

        toolbar.addWidget(btn_zoom_in)
        toolbar.addWidget(btn_zoom_out)
        toolbar.addWidget(btn_fit)

        toolbar.addSpacing(15)

        # NÚT SAVE BIN
        btn_save = QPushButton("↓ .bin")
        btn_save.clicked.connect(self.save_bin)
        toolbar.addWidget(btn_save)

        main_layout.addLayout(toolbar)

        # --- ĐỒ THỊ PYQTGRAPH ---
        pg.setConfigOption('background', '#0b111a')
        pg.setConfigOption('foreground', '#334155')
        self.plot_widget = pg.PlotWidget()
        self.plot_widget.showGrid(x=True, y=True, alpha=0.2)
        self.plot_widget.setMouseEnabled(x=True, y=False) # Khóa trục Y, chỉ zoom trục X
        
        # Ẩn số liệu mặc định của trục Y và tạo nhãn text Custom
        ay = self.plot_widget.getAxis('left')
        ay.setTicks([[(6.5, 'CH0\nPA0'), (4.5, 'CH1\nPA1'), (2.5, 'CH2\nPA2'), (0.5, 'CH3\nPA3')]])
        ay.setStyle(tickTextOffset=10)
        
        main_layout.addWidget(self.plot_widget, stretch=1)

        # --- STATUS BAR ---
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_label = QLabel(f"Samples: {self.SAMPLE_COUNT}   |   Sẵn sàng")
        self.status_label.setStyleSheet("color: #10b981; font-weight: normal;")
        self.status_bar.addWidget(self.status_label)

    # =====================================================================
    # LOGIC CHỨC NĂNG
    # =====================================================================
    def fit_view(self):
        self.plot_widget.setXRange(0, self.SAMPLE_COUNT, padding=0)
        self.plot_widget.setYRange(-1, 8, padding=0)

    def save_bin(self):
        if not self.raw_bytes_cache:
            self.status_label.setText("Chưa có dữ liệu để lưu!")
            self.status_label.setStyleSheet("color: #ef4444;")
            return
            
        options = QFileDialog.Options()
        file_name, _ = QFileDialog.getSaveFileName(self, "Lưu file Binary", "logic_data.bin", "Binary Files (*.bin)", options=options)
        if file_name:
            with open(file_name, 'wb') as f:
                f.write(self.raw_bytes_cache)
            self.status_label.setText(f"Đã lưu file: {file_name}")

    def start_capture(self):
        port = self.port_input.text().strip()
        speed_idx = self.speed_combo.currentIndex()
        mode_cmd = self.mode_combo.currentData()
        speed_cmd = str(speed_idx + 1) # index 0,1,2,3 -> '1','2','3','4'

        self.btn_capture.setEnabled(False)
        self.btn_capture.setText("⏳ WAITING...")
        self.status_label.setText("Đang chờ trigger từ phần cứng...")
        self.status_label.setStyleSheet("color: #f59e0b;") # Màu vàng cảnh báo

        self.worker = SerialWorker(port, speed_cmd, mode_cmd, samples=self.SAMPLE_COUNT)
        self.worker.log_msg.connect(self.update_status)
        self.worker.data_ready.connect(self.draw_signals)
        self.worker.finished_task.connect(self.reset_ui)
        self.worker.start()

    def update_status(self, msg):
        self.status_label.setText(msg)
        if "Lỗi" in msg:
            self.status_label.setStyleSheet("color: #ef4444;") # Đỏ
        elif "thành công" in msg:
            self.status_label.setStyleSheet("color: #10b981;") # Xanh lá

    def draw_signals(self, raw_data, raw_bytes):
        self.raw_bytes_cache = raw_bytes # Lưu lại để xuất ra file .bin
        self.plot_widget.clear()

        # Giải mã bit (PA0 -> PA3)
        ch0 = (raw_data & 0x01)
        ch1 = (raw_data & 0x02) >> 1
        ch2 = (raw_data & 0x04) >> 2
        ch3 = (raw_data & 0x08) >> 3

        # Vẽ 4 kênh với màu chuẩn của PulseView/LogicAnalyzer
        # Kết nối bằng 'finite' hoặc mặc định đều rất nhanh với pg
        self.plot_widget.plot(ch0 + 6, pen=pg.mkPen('#4ade80', width=1.5), name="CH0") # Xanh lá
        self.plot_widget.plot(ch1 + 4, pen=pg.mkPen('#38bdf8', width=1.5), name="CH1") # Xanh dương
        self.plot_widget.plot(ch2 + 2, pen=pg.mkPen('#fb923c', width=1.5), name="CH2") # Cam
        self.plot_widget.plot(ch3 + 0, pen=pg.mkPen('#c084fc', width=1.5), name="CH3") # Tím
        
        self.fit_view()

    def reset_ui(self):
        self.btn_capture.setEnabled(True)
        self.btn_capture.setText("▶ CAPTURE")

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = LogicAnalyzerApp()
    window.show()
    sys.exit(app.exec_())