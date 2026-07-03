// Tiny Screen firmware for Waveshare ESP32-S3-LCD-1.3 (square, ST7789V2, no touch)
//
// Reads line-delimited JSON stats over USB serial (sent by
// collector/stats_collector.py running on the ZimaBlade/ZimaBoard) and
// renders CPU temperature full-screen on the 240x240 ST7789V2 display.
//
// This board has NO touchscreen, so there's no screen-switching UI here --
// it's a single always-on temperature gauge. (There's an onboard QMI8658
// 6-axis IMU if you ever want to revisit multi-screen navigation via a
// tap/tilt gesture -- see the git history / previous version of this file
// for the swipeable 5-screen layout built for the touch board, which can
// be adapted once you're on touch hardware.)
//
// Libraries (install via PlatformIO, see platformio.ini):
//   - moononournation/GFX Library for Arduino ("Arduino_GFX")
//   - bblanchon/ArduinoJson

#include <Arduino.h>
#include <Arduino_GFX_Library.h>
#include <ArduinoJson.h>
#include "pins.h"

// ---------------------------------------------------------------------
// Display setup (ST7789V2, 4-wire SPI, 240x240 square)
// ---------------------------------------------------------------------

Arduino_DataBus *bus = new Arduino_ESP32SPI(
    PIN_LCD_DC, PIN_LCD_CS, PIN_LCD_SCK, PIN_LCD_MOSI, PIN_LCD_MISO);
Arduino_GFX *gfx = new Arduino_ST7789(
    bus, PIN_LCD_RST, 0 /* rotation */, true /* IPS */, SCREEN_W, SCREEN_H);

// Offscreen canvas for flicker-free redraws
Arduino_Canvas *canvas = new Arduino_Canvas(SCREEN_W, SCREEN_H, gfx);

// ---------------------------------------------------------------------
// Color palette (chill / calm theme)
// ---------------------------------------------------------------------

#define COL_BG       0x0000  // black
#define COL_RING_BG  0x2104  // dark slate
#define COL_TEAL     0x4E5A  // primary gauge color
#define COL_WARN     0xFC80  // amber for hot values
#define COL_TEXT     0xFFFF
#define COL_SUBTEXT  0x9CD3

// ---------------------------------------------------------------------
// System stats model (latest values received over serial)
// ---------------------------------------------------------------------

struct SystemStats {
  float cpu_temp_c = 0;
  unsigned long last_update_ms = 0;
} stats;

bool haveData = false;

unsigned long lastDrawMs = 0;
const unsigned long FRAME_INTERVAL_MS = 200; // ~5 fps, plenty for a stats dial

// ---------------------------------------------------------------------
// Drawing helpers
// ---------------------------------------------------------------------

void drawRingGauge(int cx, int cy, int rOuter, int rInner, float pct,
                    uint16_t color, const char *bigText, const char *label) {
  pct = constrain(pct, 0.0f, 100.0f);
  int sweep = (int)(pct * 3.6f); // degrees, 0-360

  canvas->fillArc(cx, cy, rOuter, rInner, 0, 360, COL_RING_BG);
  if (sweep > 0) {
    canvas->fillArc(cx, cy, rOuter, rInner, -90, -90 + sweep, color);
  }

  canvas->setTextColor(COL_TEXT);
  canvas->setTextSize(4);
  int16_t x1, y1; uint16_t w, h;
  canvas->getTextBounds(bigText, 0, 0, &x1, &y1, &w, &h);
  canvas->setCursor(cx - w / 2, cy - h / 2 - 6);
  canvas->print(bigText);

  canvas->setTextColor(COL_SUBTEXT);
  canvas->setTextSize(1);
  canvas->getTextBounds(label, 0, 0, &x1, &y1, &w, &h);
  canvas->setCursor(cx - w / 2, cy + 26);
  canvas->print(label);
}

void drawStaleBanner() {
  if (haveData && (millis() - stats.last_update_ms) < 5000) return;
  canvas->setTextColor(COL_WARN);
  canvas->setTextSize(1);
  const char *msg = haveData ? "no data..." : "waiting for host...";
  int16_t x1, y1; uint16_t w, h;
  canvas->getTextBounds(msg, 0, 0, &x1, &y1, &w, &h);
  canvas->setCursor(SCREEN_W / 2 - w / 2, 24);
  canvas->print(msg);
}

void drawScreenTemp() {
  float pct = constrain(stats.cpu_temp_c, 0.0f, 100.0f);
  uint16_t color = stats.cpu_temp_c >= 75 ? COL_WARN : COL_TEAL;
  char big[16];
  snprintf(big, sizeof(big), "%.0fC", stats.cpu_temp_c);
  drawRingGauge(SCREEN_W / 2, SCREEN_H / 2, 100, 84, pct, color, big, "CPU TEMP");
}

void drawCurrentScreen() {
  canvas->fillScreen(COL_BG);
  drawScreenTemp();
  drawStaleBanner();
  canvas->flush();
}

// ---------------------------------------------------------------------
// Serial JSON parsing
// ---------------------------------------------------------------------

String serialBuf;

void handleLine(const String &line) {
  StaticJsonDocument<512> doc;
  DeserializationError err = deserializeJson(doc, line);
  if (err) return; // ignore malformed/partial lines

  stats.cpu_temp_c     = doc["cpu_temp_c"] | stats.cpu_temp_c;
  stats.last_update_ms = millis();
  haveData = true;
}

void pollSerial() {
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\n') {
      handleLine(serialBuf);
      serialBuf = "";
    } else if (c != '\r') {
      serialBuf += c;
      if (serialBuf.length() > 800) serialBuf = ""; // guard against garbage
    }
  }
}

// ---------------------------------------------------------------------
// Setup / loop
// ---------------------------------------------------------------------

void setup() {
  Serial.begin(115200);

  pinMode(PIN_LCD_BL, OUTPUT);
  digitalWrite(PIN_LCD_BL, HIGH);

  gfx->begin();
  canvas->begin();
  canvas->fillScreen(COL_BG);
  canvas->flush();

  serialBuf.reserve(256);
}

void loop() {
  pollSerial();

  unsigned long now = millis();
  if (now - lastDrawMs > FRAME_INTERVAL_MS) {
    drawCurrentScreen();
    lastDrawMs = now;
  }
}
