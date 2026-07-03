#pragma once
// Waveshare ESP32-S3-LCD-1.3 pin map (square 240x240, ST7789V2, no touch)
//
// Source: parsed directly from Waveshare's own schematic PDF
// (files.waveshare.com/wiki/ESP32-S3-LCD-1.3/ESP32S3_1.3inch.pdf),
// cross-checked against known ESP32-S3 hardware facts (e.g. the netlist's
// GPIO43/U0TXD pairing matches the chip's real default UART0 TX pin).
//
// Not yet flash-tested against physical hardware -- if the display stays
// blank on first boot, double check backlight (GPIO20) is driving HIGH
// and that RST (GPIO42) is being toggled during gfx->begin().

// --- Display (ST7789V2, 4-wire SPI, 240x240 square) ---
#define PIN_LCD_CS    39
#define PIN_LCD_DC    38
#define PIN_LCD_SCK   40
#define PIN_LCD_MOSI  41
#define PIN_LCD_MISO  -1  // display is write-only, no MISO/SDO pin on the connector
#define PIN_LCD_RST   42
#define PIN_LCD_BL    20

// --- IMU (QMI8658, I2C) -- not used by this firmware yet, here for reference ---
#define PIN_IMU_SDA   47
#define PIN_IMU_SCL   48

// --- Display geometry ---
#define SCREEN_W 240
#define SCREEN_H 240
