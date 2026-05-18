import serial
import sys
import os
import time
COM_PORT = 'COM6' 
SAMPLE_COUNT = 60000 

print("=== MENU CÀI ĐẶT LOGIC ANALYZER ===")
print("Tốc độ: [1]=100k, [2]=500k, [3]=1M, [4]=2M")
print("Chế độ: [F]=Full 60k Tương lai, [P]=Có 10k Quá khứ")
cmd_speed = input("Chọn tốc độ (1/2/3/4) : ")
cmd_mode = input("Chọn chế độ (F/P) : ")
try:
    # Lấy đường dẫn tuyệt đối của file 
    file_name = 'logic_data.bin'
    abs_path = os.path.abspath(file_name)
    print(f"Đang kết nối {COM_PORT}...")
    ser = serial.Serial(COM_PORT,115200, timeout=None) 
    ser.reset_input_buffer()
    if cmd_speed in ['1', '2', '3', '4']:
        ser.write(cmd_speed.encode())
        time.sleep(0.1) # Chờ STM32 set up
        
    if cmd_mode in ['F', 'P', 'f', 'p']:
        ser.write(cmd_mode.upper().encode())
        time.sleep(0.1)
    print("Đang chờ tín hiệu Trigger từ STM32...")
    # Đọc chính xác 60.000 byte
    data = ser.read(SAMPLE_COUNT) 
    
    if len(data) == SAMPLE_COUNT:
        with open(file_name, 'wb') as f:
            f.write(data)
        print("Đã chụp và lưu thành công 60,000 mẫu vào file logic_data.bin!")
        print("COPY ĐƯỜNG DẪN DƯỚI ĐÂY VÀO PULSEVIEW:")
        print(abs_path)
    else:
        print(f"Lỗi: Chỉ nhận được {len(data)} byte. Mạch chưa gửi hết.")
        
except Exception as e:
    print(f"Có lỗi xảy ra: {e}")