import numpy as np

def decode_pwm(data_ch, fs):
    """Tính toán Tần số và Duty Cycle của kênh PWM"""
    packets = []
    # Tìm tất cả các sườn lên (0 -> 1) và sườn xuống (1 -> 0)
    edges = np.diff(data_ch)
    rising_edges = np.where(edges == 1)[0]
    falling_edges = np.where(edges == -1)[0]

    if len(rising_edges) >= 2 and len(falling_edges) >= 1:
        # Lấy một chu kỳ hoàn chỉnh để phân tích
        start_idx = rising_edges[0]
        end_idx = rising_edges[1]
        
        # Tìm sườn xuống nằm giữa 2 sườn lên
        fall_idx_list = falling_edges[(falling_edges > start_idx) & (falling_edges < end_idx)]
        if len(fall_idx_list) > 0:
            fall_idx = fall_idx_list[0]
            
            period_samples = end_idx - start_idx
            high_samples = fall_idx - start_idx
            
            freq_hz = fs / period_samples
            duty_cycle = (high_samples / period_samples) * 100
            
            # Đóng gói kết quả hiển thị trên đồ thị
            text = f"{freq_hz/1000:.1f}kHz, {duty_cycle:.1f}%"
            packets.append({"text": text, "start": start_idx, "end": end_idx})
            
    return packets

def decode_uart(data_ch, fs, baudrate=115200):
    """Giải mã gói tin UART (8 bit data, 1 stop bit, no parity)"""
    packets = []
    samples_per_bit = fs / baudrate
    
    edges = np.diff(data_ch)
    falling_edges = np.where(edges == -1)[0] # Sườn xuống là Start Bit
    
    idx = 0
    while idx < len(falling_edges):
        start_edge = falling_edges[idx]
        
        # Nhảy vào giữa bit Start (cộng 0.5 bit), rồi nhảy thêm 1 bit nữa để tới giữa Bit 0
        first_data_bit_idx = int(start_edge + 1.5 * samples_per_bit)
        
        if first_data_bit_idx + 8 * samples_per_bit >= len(data_ch):
            break # Hết mảng dữ liệu, thoát
            
        byte_val = 0
        end_idx = first_data_bit_idx
        
        # Đọc 8 bit dữ liệu (LSB first)
        for bit_pos in range(8):
            sample_idx = int(first_data_bit_idx + bit_pos * samples_per_bit)
            bit_val = data_ch[sample_idx]
            byte_val |= (bit_val << bit_pos)
            end_idx = sample_idx
            
        # Kiểm tra Stop Bit (nhảy thêm 1 bit nữa, phải là mức 1)
        stop_bit_idx = int(first_data_bit_idx + 8 * samples_per_bit)
        if stop_bit_idx < len(data_ch) and data_ch[stop_bit_idx] == 1:
            # Nếu Stop bit hợp lệ, dịch ra Hex và ASCII (nếu là chữ in được)
            char_repr = chr(byte_val) if 32 <= byte_val <= 126 else "."
            text = f"0x{byte_val:02X} '{char_repr}'"
            packets.append({"text": text, "start": start_edge, "end": stop_bit_idx})
            
        # Bỏ qua các sườn xuống nằm gọn trong gói dữ liệu vừa giải mã
        idx += 1
        while idx < len(falling_edges) and falling_edges[idx] < stop_bit_idx:
            idx += 1
            
    return packets

def decode_spi(clk_ch, mosi_ch, fs):
    """Giải mã SPI Mode 0 (Đọc dữ liệu tại sườn lên của CLK)"""
    packets = []
    edges = np.diff(clk_ch)
    rising_edges = np.where(edges == 1)[0] # Sườn lên
    
    byte_val = 0
    bit_count = 0
    start_idx = 0
    
    for clk_edge in rising_edges:
        if bit_count == 0:
            start_idx = clk_edge
            
        bit_val = mosi_ch[clk_edge]
        byte_val = (byte_val << 1) | bit_val # SPI thường gửi MSB first
        bit_count += 1
        
        if bit_count == 8:
            char_repr = chr(byte_val) if 32 <= byte_val <= 126 else "."
            text = f"0x{byte_val:02X} '{char_repr}'"
            packets.append({"text": text, "start": start_idx, "end": clk_edge})
            bit_count = 0
            byte_val = 0
            
    return packets

def decode_i2c(scl_ch, sda_ch, fs):
    """Nhận diện điều kiện Start/Stop và Byte của I2C"""
    packets = []
    
    # 1. Tìm các tín hiệu Start (SDA sườn xuống khi SCL = 1)
    sda_edges = np.diff(sda_ch)
    sda_falling = np.where(sda_edges == -1)[0]
    
    for edge in sda_falling:
        if scl_ch[edge] == 1:
            packets.append({"text": "START", "start": edge, "end": edge + 10})
            
    # (Phần dịch Byte data I2C và 1-Wire khá dài do Timing phức tạp, 
    # tạm thời trả về Start/Stop để kiểm chứng logic ghép nối trước)
    return packets