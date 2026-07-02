#pragma once
// Waveshare ESP32-S3-Touch-LCD-1.28 pin map
// Source: https://www.waveshare.com/wiki/ESP32-S3-Touch-LCD-1.28

// --- Display (GC9A01A, 4-wire SPI, 240x240 round) ---
#define PIN_LCD_CS    9
#define PIN_LCD_DC    8
#define PIN_LCD_SCK   10
#define PIN_LCD_MOSI  11
#define PIN_LCD_MISO  12
#define PIN_LCD_RST   14
#define PIN_LCD_BL    2

// --- Touch (CST816S, I2C) ---
#define PIN_TP_SDA    6
#define PIN_TP_SCL    7
#define PIN_TP_RST    13
#define CST816S_ADDR  0x15

// --- Display geometry ---
#define SCREEN_W 240
#define SCREEN_H 240
