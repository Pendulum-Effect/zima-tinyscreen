// Tiny Screen firmware for Waveshare ESP32-S3-Touch-LCD-1.28
//
// Reads line-delimited JSON stats over USB serial (sent by
// collector/stats_collector.py running on the ZimaBlade/ZimaBoard) and
// renders them on the 240x240 round GC9A01A display. Swipe left/right on
// the CST816S touch panel to move between 5 screens.
//
// Libraries (install via PlatformIO, see platformio.ini):
//   - moononournation/GFX Library for Arduino ("Arduino_GFX")
//   - bblanchon/ArduinoJson

#include <Arduino.h>
#include <Wire.h>
#include <Arduino_GFX_Library.h>
#include <ArduinoJson.h>
#include "pins.h"

// ---------------------------------------------------------------------
// Display setup (GC9A01A, 4-wire SPI)
// ---------------------------------------------------------------------

Arduino_DataBus *bus = new Arduino_ESP32SPI(
    PIN_LCD_DC, PIN_LCD_CS, PIN_LCD_SCK, PIN_LCD_MOSI, PIN_LCD_MISO);
Arduino_GFX *gfx = new Arduino_GC9A01(bus, PIN_LCD_RST, 0 /* rotation */, true /* IPS */);

// Offscreen canvas for flicker-free redraws
Arduino_Canvas *canvas = new Arduino_Canvas(SCREEN_W, SCREEN_H, gfx);

// ---------------------------------------------------------------------
// Color palette (chill / calm theme)
// ---------------------------------------------------------------------

#define COL_BG       0x0000  // black
#define COL_RING_BG  0x2104  // dark slate
#define COL_ACCENT   0x0553  // deep teal-ish text
#define COL_TEAL     0x4E5A  // primary gauge color -> RGB565(80,200,180) approx
#define COL_TEAL_2   0x2E9A
#define COL_WARN     0xFC80  // amber for hot/high values
#define COL_TEXT     0xFFFF
#define COL_SUBTEXT  0x9CD3

static uint16_t rgb(uint8_t r, uint8_t g, uint8_t b) {
  return ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3);
}

// ---------------------------------------------------------------------
// System stats model (latest values received over serial)
// ---------------------------------------------------------------------

struct SystemStats {
  String cpu_name = "--";
  float cpu_pct = 0;
  float cpu_temp_c = 0;
  float cpu_watts = 0;
  float ram_total_gb = 0;
  float ram_pct = 0;
  float ssd_total_gb = 0;
  float ssd_pct = 0;
  float net_rx_mbps = 0;
  float net_tx_mbps = 0;
  unsigned long last_update_ms = 0;
} stats;

bool haveData = false;

// ---------------------------------------------------------------------
// Touch (CST816S) — polled gesture register over I2C
// ---------------------------------------------------------------------

enum Gesture { GESTURE_NONE, GESTURE_LEFT, GESTURE_RIGHT };

Gesture readTouchGesture() {
  Wire.beginTransmission(CST816S_ADDR);
  Wire.write(0x01); // GestureID register
  if (Wire.endTransmission(false) != 0) return GESTURE_NONE;
  if (Wire.requestFrom(CST816S_ADDR, 1) != 1) return GESTURE_NONE;
  uint8_t gid = Wire.read();
  switch (gid) {
    case 0x03: return GESTURE_LEFT;   // slide left -> next screen
    case 0x04: return GESTURE_RIGHT;  // slide right -> prev screen
    default:   return GESTURE_NONE;
  }
}

void touchReset() {
  pinMode(PIN_TP_RST, OUTPUT);
  digitalWrite(PIN_TP_RST, LOW);
  delay(20);
  digitalWrite(PIN_TP_RST, HIGH);
  delay(50);
}

// ---------------------------------------------------------------------
// Screen state
// ---------------------------------------------------------------------

const int NUM_SCREENS = 5;
int currentScreen = 0;
unsigned long lastDrawMs = 0;
unsigned long lastGestureMs = 0;
const unsigned long GESTURE_DEBOUNCE_MS = 350;
const unsigned long FRAME_INTERVAL_MS = 200; // ~5 fps, plenty for a stats dial

// ---------------------------------------------------------------------
// Drawing helpers
// ---------------------------------------------------------------------

void drawRingGauge(int cx, int cy, int rOuter, int rInner, float pct,
                    uint16_t color, const char *bigText, const char *label) {
  pct = constrain(pct, 0.0f, 100.0f);
  int sweep = (int)(pct * 3.6f); // degrees, 0-360

  // Background ring (full circle, dim)
  canvas->fillArc(cx, cy, rOuter, rInner, 0, 360, COL_RING_BG);
  // Foreground ring (value), starting at top (-90deg), clockwise
  if (sweep > 0) {
    canvas->fillArc(cx, cy, rOuter, rInner, -90, -90 + sweep, color);
  }

  // Big centered value text
  canvas->setTextColor(COL_TEXT);
  canvas->setTextSize(3);
  int16_t x1, y1; uint16_t w, h;
  canvas->getTextBounds(bigText, 0, 0, &x1, &y1, &w, &h);
  canvas->setCursor(cx - w / 2, cy - h / 2 - 6);
  canvas->print(bigText);

  // Label below
  canvas->setTextColor(COL_SUBTEXT);
  canvas->setTextSize(1);
  canvas->getTextBounds(label, 0, 0, &x1, &y1, &w, &h);
  canvas->setCursor(cx - w / 2, cy + 20);
  canvas->print(label);
}

void drawFooterDots() {
  int totalW = NUM_SCREENS * 12;
  int startX = SCREEN_W / 2 - totalW / 2;
  int y = SCREEN_H - 14;
  for (int i = 0; i < NUM_SCREENS; i++) {
    uint16_t c = (i == currentScreen) ? COL_TEAL : COL_RING_BG;
    canvas->fillCircle(startX + i * 12 + 6, y, 3, c);
  }
}

void drawStaleBanner() {
  if (haveData && (millis() - stats.last_update_ms) < 5000) return;
  canvas->setTextColor(COL_WARN);
  canvas->setTextSize(1);
  const char *msg = haveData ? "no data..." : "waiting for host...";
  int16_t x1, y1; uint16_t w, h;
  canvas->getTextBounds(msg, 0, 0, &x1, &y1, &w, &h);
  canvas->setCursor(SCREEN_W / 2 - w / 2, 30);
  canvas->print(msg);
}

// Screen 0: CPU utilization + wattage
void drawScreenCPU() {
  char big[16];
  snprintf(big, sizeof(big), "%d%%", (int)round(stats.cpu_pct));
  drawRingGauge(120, 110, 92, 78, stats.cpu_pct, COL_TEAL, big, "CPU LOAD");

  canvas->setTextColor(COL_TEXT);
  canvas->setTextSize(2);
  char watts[16];
  snprintf(watts, sizeof(watts), "%.1f W", stats.cpu_watts);
  int16_t x1, y1; uint16_t w, h;
  canvas->getTextBounds(watts, 0, 0, &x1, &y1, &w, &h);
  canvas->setCursor(120 - w / 2, 175);
  canvas->print(watts);

  canvas->setTextColor(COL_SUBTEXT);
  canvas->setTextSize(1);
  canvas->getTextBounds(stats.cpu_name.c_str(), 0, 0, &x1, &y1, &w, &h);
  if (w > 200) canvas->setTextSize(1); // long CPU names still fit at size 1
  canvas->setCursor(120 - min((int)w, 200) / 2, 196);
  canvas->print(stats.cpu_name.substring(0, 26));
}

// Screen 1: RAM
void drawScreenRAM() {
  char big[16];
  snprintf(big, sizeof(big), "%d%%", (int)round(stats.ram_pct));
  drawRingGauge(120, 110, 92, 78, stats.ram_pct, COL_TEAL_2, big, "RAM USED");

  canvas->setTextColor(COL_TEXT);
  canvas->setTextSize(2);
  char total[24];
  snprintf(total, sizeof(total), "%.1f GB total", stats.ram_total_gb);
  int16_t x1, y1; uint16_t w, h;
  canvas->getTextBounds(total, 0, 0, &x1, &y1, &w, &h);
  canvas->setCursor(120 - w / 2, 180);
  canvas->print(total);
}

// Screen 2: SSD
void drawScreenSSD() {
  char big[16];
  snprintf(big, sizeof(big), "%d%%", (int)round(stats.ssd_pct));
  drawRingGauge(120, 110, 92, 78, stats.ssd_pct, rgb(120, 170, 220), big, "SSD USED");

  canvas->setTextColor(COL_TEXT);
  canvas->setTextSize(2);
  char total[24];
  snprintf(total, sizeof(total), "%.0f GB total", stats.ssd_total_gb);
  int16_t x1, y1; uint16_t w, h;
  canvas->getTextBounds(total, 0, 0, &x1, &y1, &w, &h);
  canvas->setCursor(120 - w / 2, 180);
  canvas->print(total);
}

// Screen 3: Network (no ring gauge, since it's unbounded — two bars instead)
void drawScreenNetwork() {
  canvas->setTextColor(COL_SUBTEXT);
  canvas->setTextSize(1);
  canvas->setCursor(70, 55);
  canvas->print("NETWORK");

  auto fmtRate = [](float mbps, char *out, size_t n) {
    if (mbps >= 1000.0f) snprintf(out, n, "%.2f Gbps", mbps / 1000.0f);
    else if (mbps < 1.0f) snprintf(out, n, "%.0f kbps", mbps * 1000.0f);
    else snprintf(out, n, "%.1f Mbps", mbps);
  };

  char rx[20], tx[20];
  fmtRate(stats.net_rx_mbps, rx, sizeof(rx));
  fmtRate(stats.net_tx_mbps, tx, sizeof(tx));

  int16_t x1, y1; uint16_t w, h;

  canvas->setTextColor(COL_TEAL);
  canvas->setTextSize(1);
  canvas->setCursor(60, 90);
  canvas->print("DOWN");
  canvas->setTextColor(COL_TEXT);
  canvas->setTextSize(2);
  canvas->getTextBounds(rx, 0, 0, &x1, &y1, &w, &h);
  canvas->setCursor(120 - w / 2, 105);
  canvas->print(rx);

  canvas->setTextColor(COL_TEAL_2);
  canvas->setTextSize(1);
  canvas->setCursor(65, 150);
  canvas->print("UP");
  canvas->setTextColor(COL_TEXT);
  canvas->setTextSize(2);
  canvas->getTextBounds(tx, 0, 0, &x1, &y1, &w, &h);
  canvas->setCursor(120 - w / 2, 165);
  canvas->print(tx);
}

// Screen 4: CPU temperature
void drawScreenTemp() {
  // Map temperature 0-100C onto the gauge for a nice visual, clamp for display
  float pct = constrain(stats.cpu_temp_c, 0.0f, 100.0f);
  uint16_t color = stats.cpu_temp_c >= 75 ? COL_WARN : COL_TEAL;
  char big[16];
  snprintf(big, sizeof(big), "%.0fC", stats.cpu_temp_c);
  drawRingGauge(120, 110, 92, 78, pct, color, big, "CPU TEMP");
}

void drawCurrentScreen() {
  canvas->fillScreen(COL_BG);
  switch (currentScreen) {
    case 0: drawScreenCPU(); break;
    case 1: drawScreenRAM(); break;
    case 2: drawScreenSSD(); break;
    case 3: drawScreenNetwork(); break;
    case 4: drawScreenTemp(); break;
  }
  drawStaleBanner();
  drawFooterDots();
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

  stats.cpu_name      = doc["cpu_name"] | stats.cpu_name;
  stats.cpu_pct        = doc["cpu_pct"] | stats.cpu_pct;
  stats.cpu_temp_c     = doc["cpu_temp_c"] | stats.cpu_temp_c;
  stats.cpu_watts      = doc["cpu_watts"] | stats.cpu_watts;
  stats.ram_total_gb   = doc["ram_total_gb"] | stats.ram_total_gb;
  stats.ram_pct        = doc["ram_pct"] | stats.ram_pct;
  stats.ssd_total_gb   = doc["ssd_total_gb"] | stats.ssd_total_gb;
  stats.ssd_pct        = doc["ssd_pct"] | stats.ssd_pct;
  stats.net_rx_mbps    = doc["net_rx_mbps"] | stats.net_rx_mbps;
  stats.net_tx_mbps    = doc["net_tx_mbps"] | stats.net_tx_mbps;
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

  Wire.begin(PIN_TP_SDA, PIN_TP_SCL);
  touchReset();

  serialBuf.reserve(256);
}

void loop() {
  pollSerial();

  unsigned long now = millis();

  if (now - lastGestureMs > GESTURE_DEBOUNCE_MS) {
    Gesture g = readTouchGesture();
    if (g == GESTURE_LEFT) {
      currentScreen = (currentScreen + 1) % NUM_SCREENS;
      lastGestureMs = now;
    } else if (g == GESTURE_RIGHT) {
      currentScreen = (currentScreen - 1 + NUM_SCREENS) % NUM_SCREENS;
      lastGestureMs = now;
    }
  }

  if (now - lastDrawMs > FRAME_INTERVAL_MS) {
    drawCurrentScreen();
    lastDrawMs = now;
  }
}
