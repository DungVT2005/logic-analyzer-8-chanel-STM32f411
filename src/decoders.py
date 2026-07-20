import numpy as np

def _as_bits(samples):
    """Chuyển đổi bất kỳ mảng số nào thành danh sách các số nguyên 0/1."""
    if samples is None:
        return None
    return [int(v) & 1 for v in samples]


def _edges(bits):
    """Trả về các chỉ số (index) nơi tín hiệu thay đổi mức logic (sườn lên/xuống)."""
    return [i for i in range(1, len(bits)) if bits[i] != bits[i - 1]]


def _packet(start, end, text, mosi_text=None, miso_text=None):
    pkt = {"start": int(start), "end": int(max(end, start + 1)), "text": str(text)}
    if mosi_text is not None:
        pkt["mosi_text"] = str(mosi_text)
    if miso_text is not None:
        pkt["miso_text"] = str(miso_text)
    return pkt


def _fmt_time(t_sec):
    """Định dạng giá trị thời gian thành chuỗi dễ đọc với các đơn vị phù hợp."""
    if t_sec <= 0:
        return "0s"
    if t_sec >= 1:
        return f"{t_sec:.3f}s"
    if t_sec >= 1e-3:
        return f"{t_sec * 1e3:.3f}ms"
    if t_sec >= 1e-6:
        return f"{t_sec * 1e6:.1f}µs"
    return f"{t_sec * 1e9:.1f}ns"


def _fmt_freq(freq_hz):
    """Định dạng tần số với đơn vị phù hợp."""
    if freq_hz >= 1e6:
        return f"{freq_hz / 1e6:.3f}MHz"
    if freq_hz >= 1e3:
        return f"{freq_hz / 1e3:.3f}kHz"
    return f"{freq_hz:.1f}Hz"


# ── UART ──────────────────────────────────────────────────────────────────────
_STANDARD_BAUDRATES = [
    300, 600, 1200, 2400, 4800, 9600,
    14400, 19200, 28800, 38400, 57600,
    76800, 115200, 230400, 460800, 921600,
]

def _autodetect_baudrate(bits, fs):
    """Tự động dò tốc độ Baud dựa trên 10% các khoảng chuyển sườn ngắn nhất."""
    edges = _edges(bits)
    if len(edges) < 8:
        return None

    intervals = [edges[i + 1] - edges[i] for i in range(len(edges) - 1)]
    intervals.sort()
    short_count = max(1, len(intervals) // 10)
    min_interval = sum(intervals[:short_count]) / short_count

    if min_interval < 1:
        return None

    raw_baud = fs / min_interval
    best = min(_STANDARD_BAUDRATES, key=lambda b: abs(b - raw_baud))

    # Dung sai cho phép là 25% so với baud chuẩn
    if abs(best - raw_baud) / best > 0.25:
        return None

    return best


def _decode_uart_stream(channel_data, fs, baudrate, parity, line_name):
    """Hàm nội bộ giải mã cho 1 đường dây đơn lẻ (TX hoặc RX)."""
    bits = _as_bits(channel_data)
    if bits is None or len(bits) < 12 or fs <= 0:
        return []

    bit_samples = fs / float(baudrate)
    if bit_samples < 2:
        return []

    all_edges = _edges(bits)
    falling_edges = [e for e in all_edges if bits[e - 1] == 1 and bits[e] == 0]

    packets = []
    skip_until = 0

    for start in falling_edges:
        if start < skip_until or start >= len(bits) - int(10 * bit_samples):
            continue

        value = 0
        ok = True
        for bit_no in range(8):
            sample_at = int(round(start + (1.5 + bit_no) * bit_samples))
            if sample_at >= len(bits):
                ok = False
                break
            value |= bits[sample_at] << bit_no

        if not ok:
            break

        parity_err = ""
        stop_bit_offset = 9.5
        if parity in ('E', 'O'):
            parity_idx = int(round(start + 9.5 * bit_samples))
            if parity_idx < len(bits):
                p_bit = bits[parity_idx]
                ones_count = bin(value).count('1')
                expected_p = (ones_count % 2) if parity == 'E' else ((ones_count + 1) % 2)
                if p_bit != expected_p:
                    parity_err = " PE"
            stop_bit_offset = 10.5

        stop_at = int(round(start + stop_bit_offset * bit_samples))
        char = chr(value) if 32 <= value <= 126 else "."
        frame_err = ""
        stop_ok = True
        if stop_at < len(bits):
            stop_ok = (bits[stop_at] == 1)
            frame_err = "" if stop_ok else " FE"
            
        err_str = f"{frame_err}{parity_err}"
        
        # Đóng gói kết quả và gắn thẻ luồng (TX hoặc RX)
        pkt = _packet(start, stop_at, f"0x{value:02X} '{char}'{err_str}")
        pkt["line"] = line_name
        packets.append(pkt)
        
        skip_until = int(start + (stop_bit_offset + 0.5) * bit_samples)
        if len(packets) >= 120:
            break

    return packets


def decode_uart(tx, rx, fs, baudrate=None, parity='N'):
    """
    Giải mã UART hỗ trợ đồng thời TX và RX.
    Mặc định hệ thống luôn tự động đo Baudrate từ tín hiệu thực tế.
    """
    packets = []
    
    # 1. Tự dò Baudrate và giải mã ĐỘC LẬP cho dây TX
    if tx is not None:
        baud_tx = _autodetect_baudrate(_as_bits(tx), fs)
        if baud_tx:
            for p in _decode_uart_stream(tx, fs, baud_tx, parity, "TX"):
                p["text"] = f"TX [{baud_tx}]: {p['text']}"
                packets.append(p)
            
    # 2. Tự dò Baudrate và giải mã ĐỘC LẬP cho dây RX
    if rx is not None:
        baud_rx = _autodetect_baudrate(_as_bits(rx), fs)
        if baud_rx:
            for p in _decode_uart_stream(rx, fs, baud_rx, parity, "RX"):
                p["text"] = f"RX [{baud_rx}]: {p['text']}"
                packets.append(p)

    if not packets:
        return [_packet(0, 1, "UART: Không phát hiện được tín hiệu hoặc Baudrate")]

    # 3. Sắp xếp lại theo thời gian thực
    packets.sort(key=lambda p: p["start"])
    return packets
# ── SPI ───────────────────────────────────────────────────────────────────────
def decode_spi(clk, mosi, miso, fs, mode=0, cs=None):
    """
    Giải mã SPI có hỗ trợ chân CS (Chip Select - Active Low).
    Tối ưu nhảy sườn bằng _edges(clk).
    Tách biệt mosi_text và miso_text để không in trùng lặp trên GUI.
    """
    clk_bits  = _as_bits(clk)
    mosi_bits = _as_bits(mosi)
    miso_bits = _as_bits(miso)
    cs_bits   = _as_bits(cs) if cs is not None else None

    n = min(len(clk_bits), len(mosi_bits), len(miso_bits))
    if cs_bits is not None:
        n = min(n, len(cs_bits))
    if n < 2:
        return []

    cpol = (mode >> 1) & 1
    cpha = mode & 1
    if cpol == 0:
        leading_rise  = (0, 1)
        trailing_rise = (1, 0)
    else:
        leading_rise  = (1, 0)
        trailing_rise = (0, 1)

    sample_on = leading_rise if cpha == 0 else trailing_rise

    # TỐI ƯU: Tìm toàn bộ sườn CLK thay vì lặp từng mẫu i từ 1 -> n
    clk_edges = _edges(clk_bits)
    sample_edges = [e for e in clk_edges if e < n and (clk_bits[e - 1], clk_bits[e]) == sample_on]

    packets = []
    word_start = None
    mosi_val   = 0
    miso_val   = 0
    bit_count  = 0

    for idx in sample_edges:
        # TỐI ƯU CHÂN CS: Nếu CS được gán và đang ở mức CAO (1) -> Slave không được chọn -> Bỏ qua
        if cs_bits is not None and cs_bits[idx] == 1:
            bit_count = 0
            word_start = None
            continue

        if bit_count == 0:
            word_start = idx
            mosi_val   = 0
            miso_val   = 0

        mosi_val = (mosi_val << 1) | mosi_bits[idx]
        miso_val = (miso_val << 1) | miso_bits[idx]
        bit_count += 1

        if bit_count == 8:
            # Tách riêng nhãn MOSI và MISO để App hiển thị đúng kênh
            packets.append(_packet(
                word_start, idx,
                f"SPI MOSI:0x{mosi_val:02X} MISO:0x{miso_val:02X}",
                mosi_text=f"MOSI: 0x{mosi_val:02X}",
                miso_text=f"MISO: 0x{miso_val:02X}"
            ))
            bit_count = 0
            if len(packets) >= 120:
                break

    return packets


# ── I2C ───────────────────────────────────────────────────────────────────────
def decode_i2c(scl, sda, fs):
    edges_scl = np.where(np.diff(scl) != 0)[0] + 1
    edges_sda = np.where(np.diff(sda) != 0)[0] + 1
    all_edges = np.unique(np.sort(np.concatenate((edges_scl, edges_sda))))
    
    packets = []
    state = "IDLE"
    bit_count = 0
    current_byte = 0
    byte_start_idx = -1
    is_read_op = False

    for i in range(len(all_edges)):
        idx = all_edges[i]
        
        # START / STOP
        if idx in edges_sda and scl[idx] == 1:
            if sda[idx] == 0:  
                packets.append({"text": "S", "start": idx, "end": idx})
                state = "ADDR"
                bit_count = 0
                current_byte = 0
            elif sda[idx] == 1: 
                packets.append({"text": "P", "start": idx, "end": idx})
                state = "IDLE"
                
        # Đọc bit tại sườn lên SCL
        elif idx in edges_scl and scl[idx] == 1:
            if state != "IDLE":
                if bit_count == 0:
                    byte_start_idx = idx
                    
                bit_val = sda[idx]
                if bit_count < 8:
                    current_byte = (current_byte << 1) | bit_val
                    bit_count += 1
                elif bit_count == 8:
                    ack_text = "A" if bit_val == 0 else "N"
                    if state == "ADDR":
                        addr_7bit = current_byte >> 1
                        is_read_op = (current_byte & 1) == 1
                        rw_text = "R" if is_read_op else "W"
                        byte_text = f"Addr {rw_text}: 0x{addr_7bit:02X}"
                        state = "DATA"
                    else:
                        rw_text = "R" if is_read_op else "W"
                        byte_text = f"Data {rw_text}: 0x{current_byte:02X}"
                        
                    packets.append({"text": byte_text, "start": byte_start_idx, "end": idx})
                    packets.append({"text": ack_text, "start": idx, "end": idx})
                    bit_count = 0
                    current_byte = 0

    return packets


# ── PWM ───────────────────────────────────────────────────────────────────────
def decode_pwm(channel_data, fs):
    bits = _as_bits(channel_data)
    if len(bits) < 3 or fs <= 0:
        return []

    all_edges = _edges(bits)
    rising = [i for i in all_edges if bits[i - 1] == 0 and bits[i] == 1]

    cycles = []
    for a, b in zip(rising, rising[1:]):
        falling = next((e for e in all_edges if a < e < b and bits[e - 1] == 1 and bits[e] == 0), None)
        if falling is None:
            continue
        period  = b - a
        high    = falling - a
        low     = period - high
        duty    = 100.0 * high / period
        freq    = fs / period
        cycles.append((a, b, duty, freq, high, low, period))
        if len(cycles) >= 80:
            break

    if not cycles:
        return []

    duties = [c[2] for c in cycles]
    freqs  = [c[3] for c in cycles]
    duty_min, duty_max = min(duties), max(duties)
    freq_min, freq_max = min(freqs),  max(freqs)
    avg_duty = sum(duties) / len(duties)
    avg_freq = sum(freqs) / len(freqs)

    duty_unstable = (duty_max - duty_min) > 0.05 * avg_duty if avg_duty else False
    freq_unstable = (freq_max - freq_min) > 0.05 * avg_freq if avg_freq else False
    unstable      = duty_unstable or freq_unstable

    packets = []
    for a, b, duty, freq, high, low, period in cycles:
        period_t = period / fs
        high_t   = high   / fs
        low_t    = low    / fs

        if high <= 2:
            text = "PWM ~0%"
        elif low <= 2:
            text = "PWM ~100%"
        else:
            warn = " ⚠ Không ổn định" if unstable else ""
            text = (f"PWM {duty:.1f}% {_fmt_freq(freq)}{warn} | T:{_fmt_time(period_t)} "
                    f"Th:{_fmt_time(high_t)} Tl:{_fmt_time(low_t)}")

        packets.append(_packet(a, b, text))

    return packets


# ── 1-Wire ────────────────────────────────────────────────────────────────────
def decode_1wire(data, fs):
    """
    Giải mã 1-Wire với dung sai mở rộng để chống méo xung khi lấy mẫu ở tần số thấp (100kHz).
    Hiển thị đầy đủ thông số thời gian tiêu tốn (duration_us) trên nhãn.
    """
    Ts_us = 1e6 / fs
    edges = np.where(np.diff(data) != 0)[0] + 1
    if len(edges) == 0:
        return []

    packets = []
    last_edge = edges[0]
    bit_buffer = []
    byte_start_idx = -1
    just_saw_reset = False

    for i in range(1, len(edges)):
        curr_edge = edges[i]
        duration_us = (curr_edge - last_edge) * Ts_us
        state = data[last_edge]

        if state == 0: 
            # DUNG SAI 1: Xung RESET (Chuẩn > 480us, mở rộng ngưỡng xuống >= 350us)
            if duration_us >= 350:
                packets.append({
                    "text": f"RESET ({duration_us:.0f}µs)", 
                    "start": last_edge, "end": curr_edge
                })
                just_saw_reset = True
                bit_buffer = [] 
            
            # DUNG SAI 2: Xung PRESENCE (Chuẩn 60-240us, mở rộng 45-300us)
            elif 45 <= duration_us <= 300 and just_saw_reset:
                packets.append({
                    "text": f"PRESENCE ({duration_us:.0f}µs)", 
                    "start": last_edge, "end": curr_edge
                })
                just_saw_reset = False
            
            # DUNG SAI 3: Đọc Bit 0 và Bit 1
            else:
                just_saw_reset = False
                bit_val = None
                # Bit 1 (Chuẩn 1-15us -> Mở rộng 1-30us)
                if 1 <= duration_us <= 30:
                    bit_val = 1
                # Bit 0 (Chuẩn 60-120us -> Mở rộng 35-150us)
                elif 35 <= duration_us <= 150:
                    bit_val = 0
                    
                if bit_val is not None:
                    if len(bit_buffer) == 0:
                        byte_start_idx = last_edge
                    
                    bit_buffer.append(bit_val)
                    packets.append({
                        "text": f"Bit {bit_val} ({duration_us:.0f}µs)", 
                        "start": last_edge, "end": curr_edge
                    })

            if len(bit_buffer) == 8:
                byte_val = 0
                for bit_index, bit in enumerate(bit_buffer):
                    byte_val |= (bit << bit_index)
                byte_duration_us = (curr_edge - byte_start_idx) * Ts_us
                
                # HIỂN THỊ THỜI GIAN TIÊU TỐN TRÊN NHÃN
                packets.append({
                    "text": f"0x{byte_val:02X} ({byte_duration_us:.0f}µs)", 
                    "start": byte_start_idx, "end": curr_edge
                })
                bit_buffer = []
        last_edge = curr_edge
    return packets