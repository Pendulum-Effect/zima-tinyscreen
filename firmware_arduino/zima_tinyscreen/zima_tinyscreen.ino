// Tiny Screen firmware -- supports:
//   Board 0: Waveshare ESP32-S3-LCD-1.3        (square 240x240, ST7789V2, no touch)
//   Board 1: Waveshare ESP32-S3-Touch-LCD-1.28 (round  240x240, GC9A01A, CST816S touch)
//   Board 2: Waveshare ESP32-S3-Touch-LCD-1.69 (240x280, ST7789V2, CST816T touch)
//
// This is ONE firmware binary for all boards. Which board, which stat
// pages to show, whether to auto-cycle through them, and screen brightness
// are all runtime-configurable -- set via a JSON command sent over the same
// USB-serial connection used for stats data, persisted to NVS (flash), and
// applied immediately (or after a quick self-restart if the board model
// itself changed, since that changes which GPIO pins get initialized).
//
// IMPORTANT: Board 2 (1.69") uses the ESP32-S3's native USB peripheral
// directly (no separate CH343P-style UART bridge chip like boards 0/1
// have), so it needs "USB CDC On Boot: Enabled" in Arduino IDE (or
// ARDUINO_USB_CDC_ON_BOOT=1 in PlatformIO) -- the OPPOSITE setting from
// boards 0 and 1. This is a build-time setting per physical board you're
// currently flashing, not something this source file controls.
//
// See webflasher/settings.html for the browser-side configurator that
// sends this command right after flashing.
//
// Libraries (install via PlatformIO, see platformio.ini, or Arduino
// Library Manager if using the firmware_arduino/ sketch):
//   - moononournation/GFX Library for Arduino ("Arduino_GFX")
//   - bblanchon/ArduinoJson
//   - Preferences (bundled with the ESP32 Arduino core)

#include <Arduino.h>
#include <Wire.h>
#include <Preferences.h>
#include <Arduino_GFX_Library.h>
#include <ArduinoJson.h>

// Note: screen dimensions are NOT fixed -- board 2 (1.69") is 240x280,
// taller than the two 240x240 boards. See screenW/screenH globals, set
// from the active BoardProfile in initDisplay(), and the SY() helper
// below used to scale the Y-axis layout proportionally across boards.

// ---------------------------------------------------------------------
// Board profiles -- pin maps for each supported physical board
// ---------------------------------------------------------------------

struct BoardProfile {
  const char *name;
  bool hasTouch;
  int width, height;
  int lcd_cs, lcd_dc, lcd_sck, lcd_mosi, lcd_rst, lcd_bl;
  int tp_sda, tp_scl, tp_rst; // -1 if no touch
  bool driverIsGC9A01;        // false = ST7789
};

const BoardProfile BOARD_PROFILES[] = {
  // Board 0: ESP32-S3-LCD-1.3 (square, no touch) -- pins from schematic PDF
  { "ESP32-S3-LCD-1.3 (square, no touch)", false, 240, 240,
    /*cs*/39, /*dc*/38, /*sck*/40, /*mosi*/41, /*rst*/42, /*bl*/20,
    /*sda*/-1, /*scl*/-1, /*tprst*/-1, /*gc9a01*/false },
  // Board 1: ESP32-S3-Touch-LCD-1.28 (round, touch) -- pins from Waveshare wiki
  { "ESP32-S3-Touch-LCD-1.28 (round, touch)", true, 240, 240,
    /*cs*/9, /*dc*/8, /*sck*/10, /*mosi*/11, /*rst*/14, /*bl*/2,
    /*sda*/6, /*scl*/7, /*tprst*/13, /*gc9a01*/true },
  // Board 2: ESP32-S3-Touch-LCD-1.69 (240x280, touch) -- pins from schematic PDF.
  // Note: this board uses the ESP32-S3's NATIVE USB peripheral (no separate
  // CH343P-style UART bridge chip), so it needs "USB CDC On Boot: Enabled"
  // in Arduino IDE / ARDUINO_USB_CDC_ON_BOOT=1 in PlatformIO -- the OPPOSITE
  // setting from boards 0 and 1.
  { "ESP32-S3-Touch-LCD-1.69 (240x280, touch)", true, 240, 280,
    /*cs*/5, /*dc*/4, /*sck*/6, /*mosi*/7, /*rst*/8, /*bl*/15,
    /*sda*/11, /*scl*/10, /*tprst*/13, /*gc9a01*/false },
};
const int NUM_BOARD_PROFILES = sizeof(BOARD_PROFILES) / sizeof(BOARD_PROFILES[0]);
#define CST816S_ADDR 0x15

// ---------------------------------------------------------------------
// Config (persisted to NVS via Preferences)
// ---------------------------------------------------------------------

// Known page ids, in canonical order
const char *ALL_PAGE_IDS[] = {"cpu", "ram", "ssd", "net", "temp"};
const int NUM_ALL_PAGES = 5;

struct Config {
  bool configured = false; // false until the first set_config command ever arrives
  int boardId = 0;
  char pages[6][8] = {"temp"};  // up to 6 slots, page id strings
  int numPages = 1;
  bool autoCycle = false;
  int cycleSeconds = 10;
  int brightness = 100; // 0-100
} config;

Preferences prefs;

void loadConfig() {
  prefs.begin("tinyscreen", true); // read-only
  config.configured = prefs.getBool("configured", false);
  config.boardId = prefs.getInt("boardId", 0);
  config.autoCycle = prefs.getBool("autoCycle", false);
  config.cycleSeconds = prefs.getInt("cycleSec", 10);
  config.brightness = prefs.getInt("brightness", 100);
  String pagesCsv = prefs.getString("pages", "temp");
  prefs.end();

  config.numPages = 0;
  int start = 0;
  for (int i = 0; i <= (int)pagesCsv.length() && config.numPages < 6; i++) {
    if (i == (int)pagesCsv.length() || pagesCsv[i] == ',') {
      String token = pagesCsv.substring(start, i);
      if (token.length() > 0 && token.length() < 8) {
        token.toCharArray(config.pages[config.numPages], 8);
        config.numPages++;
      }
      start = i + 1;
    }
  }
  if (config.numPages == 0) {
    strcpy(config.pages[0], "temp");
    config.numPages = 1;
  }
  if (config.boardId < 0 || config.boardId >= NUM_BOARD_PROFILES) config.boardId = 0;
}

void saveConfig() {
  prefs.begin("tinyscreen", false); // read-write
  prefs.putBool("configured", config.configured);
  prefs.putInt("boardId", config.boardId);
  prefs.putBool("autoCycle", config.autoCycle);
  prefs.putInt("cycleSec", config.cycleSeconds);
  prefs.putInt("brightness", config.brightness);
  String pagesCsv = "";
  for (int i = 0; i < config.numPages; i++) {
    if (i > 0) pagesCsv += ",";
    pagesCsv += config.pages[i];
  }
  prefs.putString("pages", pagesCsv);
  prefs.end();
}

// Note: intentionally no activeProfile() helper function here -- a
// function returning a custom struct type (BoardProfile&) hits the same
// Arduino auto-prototype-generation bug as the Gesture enum did above.
// Call sites just index BOARD_PROFILES[config.boardId] directly instead.

// ---------------------------------------------------------------------
// Display (constructed at runtime in setup(), once we know the board)
// ---------------------------------------------------------------------

Arduino_DataBus *bus = nullptr;
Arduino_GFX *gfx = nullptr;
Arduino_Canvas *canvas = nullptr;
int screenW = 240;
int screenH = 240;

// Layout constants below were tuned against a 240x240 reference screen.
// SY() scales a Y-coordinate proportionally for taller/shorter screens
// (e.g. board 2's 240x280 panel) so the layout doesn't just run off the
// bottom or leave a big gap -- X doesn't need this since all boards so
// far share the same 240 width.
int SY(int y) { return y * screenH / 240; }

// ---------------------------------------------------------------------
// LEDC (backlight PWM) -- arduino-esp32 core 3.x replaced the old
// channel-based API (ledcSetup/ledcAttachPin/ledcWrite(channel,...))
// with a pin-based one (ledcAttach(pin,...)/ledcWrite(pin,...)); the two
// are mutually exclusive, not just renamed, so plain code can only ever
// compile against ONE of them. Arduino IDE and PlatformIO have been
// observed pulling different actual core versions for this same project,
// so we detect at compile time via ESP_ARDUINO_VERSION_MAJOR (the
// version macro Espressif added specifically to support this migration)
// rather than assuming either one.
#define TINYSCREEN_BACKLIGHT_LEDC_CHANNEL 0

void pwmAttachBacklight(int pin) {
#if defined(ESP_ARDUINO_VERSION_MAJOR) && ESP_ARDUINO_VERSION_MAJOR >= 3
  ledcAttach(pin, 5000 /* Hz */, 8 /* bit resolution */);
#else
  ledcSetup(TINYSCREEN_BACKLIGHT_LEDC_CHANNEL, 5000 /* Hz */, 8 /* bit resolution */);
  ledcAttachPin(pin, TINYSCREEN_BACKLIGHT_LEDC_CHANNEL);
#endif
}

void pwmWriteBacklight(int pin, int duty) {
#if defined(ESP_ARDUINO_VERSION_MAJOR) && ESP_ARDUINO_VERSION_MAJOR >= 3
  ledcWrite(pin, duty);
#else
  (void)pin; // old API addresses by channel, not pin
  ledcWrite(TINYSCREEN_BACKLIGHT_LEDC_CHANNEL, duty);
#endif
}

void initDisplay() {
  const BoardProfile &p = BOARD_PROFILES[config.boardId];
  screenW = p.width;
  screenH = p.height;
  bus = new Arduino_ESP32SPI(p.lcd_dc, p.lcd_cs, p.lcd_sck, p.lcd_mosi, -1 /* no MISO */);
  if (p.driverIsGC9A01) {
    gfx = new Arduino_GC9A01(bus, p.lcd_rst, 0 /* rotation */, true /* IPS */);
  } else {
    gfx = new Arduino_ST7789(bus, p.lcd_rst, 0 /* rotation */, true /* IPS */, screenW, screenH);
  }
  canvas = new Arduino_Canvas(screenW, screenH, gfx);

  pinMode(p.lcd_bl, OUTPUT);
  pwmAttachBacklight(p.lcd_bl);
  pwmWriteBacklight(p.lcd_bl, map(config.brightness, 0, 100, 0, 255));

  gfx->begin();
  canvas->begin();
  canvas->fillScreen(0x0000);
  canvas->flush();

  if (p.hasTouch) {
    Wire.begin(p.tp_sda, p.tp_scl);
    pinMode(p.tp_rst, OUTPUT);
    digitalWrite(p.tp_rst, LOW);
    delay(20);
    digitalWrite(p.tp_rst, HIGH);
    delay(50);
  }
}

void applyBrightness() {
  pwmWriteBacklight(BOARD_PROFILES[config.boardId].lcd_bl, map(config.brightness, 0, 100, 0, 255));
}

// ---------------------------------------------------------------------
// Color palette (chill / calm theme)
// ---------------------------------------------------------------------

#define COL_BG       0x0000
#define COL_RING_BG  0x2104
#define COL_TEAL     0x4E5A
#define COL_TEAL_2   0x2E9A
#define COL_WARN     0xFC80
#define COL_TEXT     0xFFFF
#define COL_SUBTEXT  0x9CD3

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
// Carousel state
// ---------------------------------------------------------------------

int currentPageIdx = 0;              // index into config.pages
unsigned long lastDrawMs = 0;
unsigned long lastCycleMs = 0;
unsigned long lastGestureMs = 0;
const unsigned long FRAME_INTERVAL_MS = 200;
const unsigned long GESTURE_DEBOUNCE_MS = 350;

void advancePage(int dir) {
  if (config.numPages <= 1) return;
  currentPageIdx = (currentPageIdx + dir + config.numPages) % config.numPages;
  lastCycleMs = millis();
}

// ---------------------------------------------------------------------
// Drawing helpers
// ---------------------------------------------------------------------

void drawRingGauge(int cx, int cy, int rOuter, int rInner, float pct,
                    uint16_t color, const char *bigText, const char *label) {
  pct = constrain(pct, 0.0f, 100.0f);
  int sweep = (int)(pct * 3.6f);

  canvas->fillArc(cx, cy, rOuter, rInner, 0, 360, COL_RING_BG);
  if (sweep > 0) {
    canvas->fillArc(cx, cy, rOuter, rInner, -90, -90 + sweep, color);
  }

  canvas->setTextColor(COL_TEXT);
  canvas->setTextSize(3);
  int16_t x1, y1; uint16_t w, h;
  canvas->getTextBounds(bigText, 0, 0, &x1, &y1, &w, &h);
  canvas->setCursor(cx - w / 2, cy - h / 2 - 6);
  canvas->print(bigText);

  canvas->setTextColor(COL_SUBTEXT);
  canvas->setTextSize(1);
  canvas->getTextBounds(label, 0, 0, &x1, &y1, &w, &h);
  canvas->setCursor(cx - w / 2, cy + 22);
  canvas->print(label);
}

void drawStaleBanner() {
  if (haveData && (millis() - stats.last_update_ms) < 5000) return;
  canvas->setTextColor(COL_WARN);
  canvas->setTextSize(1);
  const char *msg = haveData ? "no data..." : "waiting for host...";
  int16_t x1, y1; uint16_t w, h;
  canvas->getTextBounds(msg, 0, 0, &x1, &y1, &w, &h);
  canvas->setCursor(screenW / 2 - w / 2, SY(24));
  canvas->print(msg);
}

void drawFooterDots() {
  if (config.numPages <= 1) return;
  int totalW = config.numPages * 12;
  int startX = screenW / 2 - totalW / 2;
  int y = screenH - SY(14);
  for (int i = 0; i < config.numPages; i++) {
    uint16_t c = (i == currentPageIdx) ? COL_TEAL : COL_RING_BG;
    canvas->fillCircle(startX + i * 12 + 6, y, 3, c);
  }
}

void drawPageCPU() {
  char big[16];
  snprintf(big, sizeof(big), "%d%%", (int)round(stats.cpu_pct));
  drawRingGauge(screenW / 2, SY(105), 88, 74, stats.cpu_pct, COL_TEAL, big, "CPU LOAD");
  canvas->setTextColor(COL_TEXT);
  canvas->setTextSize(2);
  char watts[16];
  snprintf(watts, sizeof(watts), "%.1f W", stats.cpu_watts);
  int16_t x1, y1; uint16_t w, h;
  canvas->getTextBounds(watts, 0, 0, &x1, &y1, &w, &h);
  canvas->setCursor(screenW / 2 - w / 2, SY(172));
  canvas->print(watts);
}

void drawPageRAM() {
  char big[16];
  snprintf(big, sizeof(big), "%d%%", (int)round(stats.ram_pct));
  drawRingGauge(screenW / 2, SY(105), 88, 74, stats.ram_pct, COL_TEAL_2, big, "RAM USED");
  canvas->setTextColor(COL_TEXT);
  canvas->setTextSize(2);
  char total[24];
  snprintf(total, sizeof(total), "%.1f GB", stats.ram_total_gb);
  int16_t x1, y1; uint16_t w, h;
  canvas->getTextBounds(total, 0, 0, &x1, &y1, &w, &h);
  canvas->setCursor(screenW / 2 - w / 2, SY(172));
  canvas->print(total);
}

void drawPageSSD() {
  char big[16];
  snprintf(big, sizeof(big), "%d%%", (int)round(stats.ssd_pct));
  drawRingGauge(screenW / 2, SY(105), 88, 74, stats.ssd_pct, 0x7B9F, big, "SSD USED");
  canvas->setTextColor(COL_TEXT);
  canvas->setTextSize(2);
  char total[24];
  snprintf(total, sizeof(total), "%.0f GB", stats.ssd_total_gb);
  int16_t x1, y1; uint16_t w, h;
  canvas->getTextBounds(total, 0, 0, &x1, &y1, &w, &h);
  canvas->setCursor(screenW / 2 - w / 2, SY(172));
  canvas->print(total);
}

void drawPageNet() {
  canvas->setTextColor(COL_SUBTEXT);
  canvas->setTextSize(1);
  canvas->setCursor(screenW / 2 - 28, SY(50));
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
  canvas->setCursor(screenW / 2 - 20, SY(90));
  canvas->print("DOWN");
  canvas->setTextColor(COL_TEXT);
  canvas->setTextSize(2);
  canvas->getTextBounds(rx, 0, 0, &x1, &y1, &w, &h);
  canvas->setCursor(screenW / 2 - w / 2, SY(105));
  canvas->print(rx);

  canvas->setTextColor(COL_TEAL_2);
  canvas->setTextSize(1);
  canvas->setCursor(screenW / 2 - 12, SY(150));
  canvas->print("UP");
  canvas->setTextColor(COL_TEXT);
  canvas->setTextSize(2);
  canvas->getTextBounds(tx, 0, 0, &x1, &y1, &w, &h);
  canvas->setCursor(screenW / 2 - w / 2, SY(165));
  canvas->print(tx);
}

void drawPageTemp() {
  float pct = constrain(stats.cpu_temp_c, 0.0f, 100.0f);
  uint16_t color = stats.cpu_temp_c >= 75 ? COL_WARN : COL_TEAL;
  char big[16];
  snprintf(big, sizeof(big), "%.0fC", stats.cpu_temp_c);
  drawRingGauge(screenW / 2, SY(105), 88, 74, pct, color, big, "CPU TEMP");
}

void drawPage(const char *pageId) {
  if (strcmp(pageId, "cpu") == 0) drawPageCPU();
  else if (strcmp(pageId, "ram") == 0) drawPageRAM();
  else if (strcmp(pageId, "ssd") == 0) drawPageSSD();
  else if (strcmp(pageId, "net") == 0) drawPageNet();
  else drawPageTemp();
}

void drawCurrentScreen() {
  canvas->fillScreen(COL_BG);
  drawPage(config.pages[currentPageIdx]);
  drawStaleBanner();
  drawFooterDots();
  canvas->flush();
}

// ---------------------------------------------------------------------
// Touch (CST816S) -- only polled when the active board profile has touch
// ---------------------------------------------------------------------

#define GESTURE_NONE  0
#define GESTURE_LEFT  1
#define GESTURE_RIGHT 2

int readTouchGesture() {
  Wire.beginTransmission(CST816S_ADDR);
  Wire.write(0x01);
  if (Wire.endTransmission(false) != 0) return GESTURE_NONE;
  if (Wire.requestFrom(CST816S_ADDR, 1) != 1) return GESTURE_NONE;
  uint8_t gid = Wire.read();
  if (gid == 0x03) return GESTURE_LEFT;
  if (gid == 0x04) return GESTURE_RIGHT;
  return GESTURE_NONE;
}

// ---------------------------------------------------------------------
// Serial protocol: stats updates AND config commands share one line-based
// JSON stream. A line with a "cmd" field is a command; otherwise it's
// treated as a stats update (same as before).
// ---------------------------------------------------------------------

String serialBuf;
bool pendingRestart = false;
unsigned long restartAtMs = 0;

void handleSetConfig(JsonDocument &doc) {
  bool wasConfigured = config.configured;
  bool boardChanged = false;

  if (doc["board"].is<int>()) {
    int newBoard = doc["board"];
    if (newBoard >= 0 && newBoard < NUM_BOARD_PROFILES && newBoard != config.boardId) {
      config.boardId = newBoard;
      boardChanged = true;
    }
  }

  if (doc["pages"].is<JsonArray>()) {
    JsonArray arr = doc["pages"];
    int n = 0;
    for (JsonVariant v : arr) {
      if (n >= 6) break;
      const char *s = v.as<const char *>();
      if (s && strlen(s) < 8) {
        strcpy(config.pages[n], s);
        n++;
      }
    }
    if (n > 0) {
      config.numPages = n;
      currentPageIdx = 0;
    }
  }

  if (doc["cycle_mode"].is<const char *>()) {
    const char *mode = doc["cycle_mode"];
    config.autoCycle = (strcmp(mode, "auto") == 0);
  }
  if (doc["cycle_seconds"].is<int>()) {
    config.cycleSeconds = doc["cycle_seconds"];
  }
  if (doc["brightness"].is<int>()) {
    config.brightness = constrain((int)doc["brightness"], 0, 100);
    if (!boardChanged && wasConfigured) applyBrightness();
  }

  config.configured = true;
  saveConfig();

  // Ack so the settings page can confirm success
  Serial.println("{\"ack\":\"set_config\",\"ok\":true}");

  if (boardChanged || !wasConfigured) {
    // Pins/driver differ per board -- cleanest to just restart into setup()
    // with the new profile rather than trying to hot-swap display objects.
    // Also always restart on the very first-ever config, since an
    // unconfigured device never called initDisplay() at all (see setup()).
    Serial.println("{\"info\":\"restarting to apply configuration\"}");
    pendingRestart = true;
    restartAtMs = millis() + 300; // let the serial message flush first
  }
}

void handleLine(const String &line) {
  Serial.print("[debug] RX line: ");
  Serial.println(line);

  JsonDocument doc;
  DeserializationError err = deserializeJson(doc, line);
  if (err) {
    Serial.print("[debug] JSON parse error: ");
    Serial.println(err.c_str());
    return;
  }

  if (doc["cmd"].is<const char *>() && strcmp(doc["cmd"], "set_config") == 0) {
    handleSetConfig(doc);
    return;
  }

  // Otherwise treat as a stats update
  stats.cpu_name       = doc["cpu_name"] | stats.cpu_name;
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
      if (serialBuf.length() > 800) serialBuf = "";
    }
  }
}

// ---------------------------------------------------------------------
// Setup / loop
// ---------------------------------------------------------------------

void setup() {
  Serial.begin(115200);
  delay(500); // give native USB CDC a moment to enumerate before printing
  loadConfig();
  Serial.print("[debug] BOOT OK, configured=");
  Serial.print(config.configured ? "true" : "false");
  Serial.print(", boardId=");
  Serial.println(config.boardId);
  if (config.configured) {
    // Only initialize display/backlight pins once we actually know which
    // physical board this is -- an unconfigured device stays fully
    // hands-off on GPIO (see handleSetConfig() for why: board 0's default
    // pins can collide with another board's USB data lines).
    initDisplay();
    Serial.println("[debug] initDisplay() completed");
  }
  serialBuf.reserve(256);
}

void loop() {
  pollSerial();

  if (pendingRestart && millis() >= restartAtMs) {
    ESP.restart();
  }

  if (!config.configured) {
    // Nothing to draw yet -- just keep listening on Serial for the first
    // set_config command from webflasher/settings.html.
    return;
  }

  unsigned long now = millis();

  if (BOARD_PROFILES[config.boardId].hasTouch && now - lastGestureMs > GESTURE_DEBOUNCE_MS) {
    int g = readTouchGesture();
    if (g == GESTURE_LEFT) { advancePage(1); lastGestureMs = now; }
    else if (g == GESTURE_RIGHT) { advancePage(-1); lastGestureMs = now; }
  }

  if (config.autoCycle && now - lastCycleMs > (unsigned long)config.cycleSeconds * 1000UL) {
    advancePage(1);
  }

  if (now - lastDrawMs > FRAME_INTERVAL_MS) {
    drawCurrentScreen();
    lastDrawMs = now;
  }
}
