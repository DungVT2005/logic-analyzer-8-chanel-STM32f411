#include <Arduino.h>
#define SAMPLE_COUNT 60000 
#define POST_TRIGGER_SAMPLES 50000 

uint8_t logic_buffer[SAMPLE_COUNT];
volatile bool triggered = false;
volatile uint16_t trigger_ndtr = 0;
uint32_t current_arr = SystemCoreClock/1000000 -1; // Mặc định 1MHz
uint32_t current_post_trigger = 50000; // Mặc định Pre-trigger 10k
// HÀM XỬ LÝ NGẮT  
void handle_trigger() {
    if (!triggered) {
        trigger_ndtr = DMA2_Stream5->NDTR; 
        triggered = true;
        // Tắt ngắt 
        EXTI->IMR &= ~0x00FF; //    8 kênh ~0x00FF
    }
}

void init_dma_timer() {
    //bật GPIOA và DMA2, TIM1
    RCC->AHB1ENR |= RCC_AHB1ENR_GPIOAEN;
    RCC->AHB1ENR |= RCC_AHB1ENR_DMA2EN;
    RCC->APB2ENR |= RCC_APB2ENR_TIM1EN;
    
    //  LÀM SẠCH HỆ THỐNG TRƯỚC KHI CẤU HÌNH:
    TIM1->SR = 0; // Xóa cờ ngắt của Timer 1
    DMA2->HIFCR = DMA_HIFCR_CTCIF5 | DMA_HIFCR_CHTIF5 | DMA_HIFCR_CTEIF5 | DMA_HIFCR_CDMEIF5 | DMA_HIFCR_CFEIF5; // Xóa sạch cờ DMA

    GPIOA->MODER &= ~(0x0000FFFF );  // xác định chân iput    8 kênh 0x0000FFFF 
    GPIOA->PUPDR &= ~(0x0000FFFF );   // Cấu hình PA0-PA3 là input floating 
    // cấu hình DMA
    DMA2_Stream5->CR = 0; 
    while(DMA2_Stream5->CR & DMA_SxCR_EN); 

    DMA2_Stream5->PAR = (uint32_t)&(GPIOA->IDR);
    DMA2_Stream5->M0AR = (uint32_t)logic_buffer; 
    
    // Bật bit CIRC (Quay vòng)
    DMA2_Stream5->CR = (6 << 25) | (2 << 16) | (1 << 10) | DMA_SxCR_CIRC;
    DMA2_Stream5->NDTR = SAMPLE_COUNT;
    // cấu hình timer
    TIM1->PSC = 0; 
    TIM1->ARR = current_arr;                   
    TIM1->DIER |= TIM_DIER_UDE; 
    
}

void start_capture_mode() {
    triggered = false;
    
    // Bật DMA và Timer để liên tục ghi hình "Quá khứ" (Pre-trigger)
    DMA2_Stream5->CR |= DMA_SxCR_EN; 
    // Reset bộ đếm Timer về 0 trước khi bắt đầu đếm để đảm bảo mẫu đầu tiên
    TIM1->CNT = 0;
    TIM1->CR1 |= TIM_CR1_CEN; 
    if (current_post_trigger < SAMPLE_COUNT) {
        // Chế độ Pre-trigger 
        // Tốc độ lấy mẫu = SystemCoreClock / (current_arr + 1)
        // Thời gian chờ (ms) = (Số mẫu Pre-trigger * 1000) / Tốc độ lấy mẫu
        uint32_t pre_samples = SAMPLE_COUNT - current_post_trigger;
        uint32_t sample_rate = SystemCoreClock / (current_arr + 1);
        uint32_t wait_time_ms = (pre_samples * 1000) / sample_rate;
        
        delay(wait_time_ms + 2); // Cộng thêm 2ms buffer cho chắc chắn
    } else {
        // Chế độ Full Post-trigger 
    }

    // 3. Lau sạch nòng súng: Xóa bỏ mọi ngắt giả/nhiễu vô tình lọt vào trong lúc nạp đạn
    EXTI->PR = 0x00FF; 
    
    // 4. Mở chốt an toàn: Cho phép 8 kênh (PA0-PA7) chính thức rình mồi!
    EXTI->IMR |= 0x00FF;
}

void setup() {
    Serial.begin(115200);
    uint32_t t = millis();
    while(!Serial) { if (millis() - t > 2000) break; }
    for (int i = 0; i <= 7; i++) {
        attachInterrupt(digitalPinToInterrupt(i), handle_trigger, CHANGE);
    }
    EXTI->IMR &= ~0x00FF;
    init_dma_timer();
    start_capture_mode(); // Sẵn sàng chụp
}

void loop() {
    if (Serial.available() > 0) {
        char command = Serial.read();
        bool changed = false;

        // Đổi tốc độ
        if (command == '1') { current_arr = SystemCoreClock/100000 -1; changed = true; } // 100k
        if (command == '2') { current_arr = SystemCoreClock/500000 -1; changed = true; } // 500k
        if (command == '3') { current_arr = SystemCoreClock/1000000 -1; changed = true;  } // 1M
        if (command == '4') { current_arr = SystemCoreClock/2000000 -1; changed = true;  } // 2M
        
        // Đổi chế độ Trigger
        if (command == 'F') { current_post_trigger = 60000; changed = true; } // Full Post
        if (command == 'P') { current_post_trigger = 50000; changed = true; } // Pre-trigger

        // Nếu có lệnh thay đổi, ta phải DỪNG mạch lại và Setup từ đầu
        if (changed) {
            TIM1->CR1 &= ~TIM_CR1_CEN;     // Tắt Timer
            DMA2_Stream5->CR &= ~DMA_SxCR_EN; // Tắt DMA
            EXTI->IMR &= ~0x00FF;          // Khóa cò súng an toàn
            
            init_dma_timer();              // Nạp lại cấu hình mới
            start_capture_mode();          // Rình mồi lại với thông số mới
            
        }
    }
    if (triggered) {
        // CHỜ GHI ĐỦ MẪU 
        int32_t remaining = current_post_trigger;
        uint16_t prev_ndtr = trigger_ndtr;
        
        while (remaining > 0) {
            uint16_t curr_ndtr = DMA2_Stream5->NDTR;
            if (curr_ndtr != prev_ndtr) {
                uint16_t step = (prev_ndtr > curr_ndtr) ? (prev_ndtr - curr_ndtr) : (prev_ndtr + SAMPLE_COUNT - curr_ndtr);
                remaining -= step;
                prev_ndtr = curr_ndtr;
            }
        }

        //  DỪNG LẤY MẪU 
        TIM1->CR1 &= ~TIM_CR1_CEN; 
        DMA2_Stream5->CR &= ~DMA_SxCR_EN;
        while(DMA2_Stream5->CR & DMA_SxCR_EN);

        //  ĐẨY LÊN MT
        uint16_t split_index = (SAMPLE_COUNT - DMA2_Stream5->NDTR) % SAMPLE_COUNT;

        Serial.write(&logic_buffer[split_index], SAMPLE_COUNT - split_index);
        if (split_index > 0) {
            Serial.write(logic_buffer, split_index);
        }
        Serial.flush();
        
        // khởi động lại để sẵn sàng cho lần  tiếp theo
        start_capture_mode();
    }
}