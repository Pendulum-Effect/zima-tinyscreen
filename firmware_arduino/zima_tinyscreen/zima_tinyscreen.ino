// Tiny Screen firmware -- supports:
//   Board 0: Waveshare ESP32-S3-LCD-1.3        (square 240x240, ST7789V2, no touch)
//   Board 1: Waveshare ESP32-S3-Touch-LCD-1.69 (240x280, ST7789V2, CST816T touch)
//
// This is ONE firmware binary for all boards. Which board, which stat
// pages to show, whether to auto-cycle through them, and screen brightness
// are all runtime-configurable -- set via a JSON command sent over the same
// USB-serial connection used for stats data, persisted to NVS (flash), and
// applied immediately (or after a quick self-restart if the board model
// itself changed, since that changes which GPIO pins get initialized).
//
// IMPORTANT: Board 1 (1.69") uses the ESP32-S3's native USB peripheral
// directly (no separate CH343P-style UART bridge chip like board 0 has),
// so it needs "USB CDC On Boot: Enabled" in Arduino IDE (or
// ARDUINO_USB_CDC_ON_BOOT=1 in PlatformIO) -- the OPPOSITE setting from
// board 0. This is a build-time setting per physical board you're
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

// Bump this string whenever a firmware change is meaningful enough for a
// user to want to know it happened -- shown in the settings dashboard's
// "Software Version" field via the get_config command below. No
// auto-update-checking mechanism exists yet (that's a separate, not-yet
// -built feature) -- this just answers "what's currently on my device."
#define FIRMWARE_VERSION "1.3.0"

// Note: screen dimensions are NOT fixed -- board 1 (1.69") is 240x280,
// taller than board 0's 240x240. See screenW/screenH globals, set from
// the active BoardProfile in initDisplay(), and the SY() helper below
// used to scale the Y-axis layout proportionally across boards.

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
  int colOffset1, rowOffset1, colOffset2, rowOffset2; // ST7789 GRAM alignment
};

const BoardProfile BOARD_PROFILES[] = {
  // Board 0: ESP32-S3-LCD-1.3 (square, no touch) -- pins from schematic PDF
  { "ESP32-S3-LCD-1.3 (square, no touch)", false, 240, 240,
    /*cs*/39, /*dc*/38, /*sck*/40, /*mosi*/41, /*rst*/42, /*bl*/20,
    /*sda*/-1, /*scl*/-1, /*tprst*/-1, /*gc9a01*/false,
    /*offsets*/ 0, 0, 0, 0 },
  // Board 1: ESP32-S3-Touch-LCD-1.69 (240x280, touch) -- pins from schematic PDF.
  // Note: this board uses the ESP32-S3's NATIVE USB peripheral (no separate
  // CH343P-style UART bridge chip), so it needs "USB CDC On Boot: Enabled"
  // in Arduino IDE / ARDUINO_USB_CDC_ON_BOOT=1 in PlatformIO -- the OPPOSITE
  // setting from board 0.
  //
  // Row offset of 20 is REQUIRED here, confirmed via Waveshare's own docs
  // and Arduino_GFX's GitHub for this exact panel: the ST7789V2 controller's
  // native addressable RAM is 240x320, but this physical panel is only
  // 240x280 -- a smaller window into that RAM. Without this offset, the
  // display's addressing is misaligned by 20 rows, which showed up as
  // graphical artifacts along the bottom edge.
  { "ESP32-S3-Touch-LCD-1.69 (240x280, touch)", true, 240, 280,
    /*cs*/5, /*dc*/4, /*sck*/6, /*mosi*/7, /*rst*/8, /*bl*/15,
    /*sda*/11, /*scl*/10, /*tprst*/13, /*gc9a01*/false,
    /*offsets*/ 0, 20, 0, 20 },
};
const int NUM_BOARD_PROFILES = sizeof(BOARD_PROFILES) / sizeof(BOARD_PROFILES[0]);
#define TOUCH_I2C_ADDR 0x15  // shared address convention for CST816-family touch chips

// ---------------------------------------------------------------------
// Config (persisted to NVS via Preferences)
// ---------------------------------------------------------------------

// Known page ids, in canonical order
const char *ALL_PAGE_IDS[] = {"cpu", "ram", "mmc", "net", "temp", "nas"};
const int NUM_ALL_PAGES = 6;

struct Config {
  bool configured = false; // false until the first set_config command ever arrives
  int boardId = 0;
  char pages[6][8] = {"temp"};  // up to 6 slots, page id strings
  int numPages = 1;
  bool autoCycle = false;
  int cycleSeconds = 10;
  int brightness = 100; // 0-100

  // Night mode: between nightStartMin and nightEndMin (minutes since
  // local midnight, window may wrap past midnight), use nightBrightness
  // instead of brightness. 0 = backlight fully off. Local time is
  // computed from host-supplied UTC (see utc_min in the stats payload)
  // plus tzOffsetMin, which the dashboard captures from the browser at
  // save time -- the board itself has no clock and no timezone database.
  bool nightEnabled = false;
  int nightStartMin = 1320;   // 22:00
  int nightEndMin = 420;      //  7:00
  int nightBrightness = 10;   // 0-100, 0 = screen off
  int tzOffsetMin = 0;        // local = UTC + offset (e.g. -300 for UTC-5)

  // Screensaver: after saverMinutes with no touch input, show a
  // screensaver until the next touch. Touch-triggered, so only
  // meaningful on boards with a touch panel; ignored otherwise.
  bool saverEnabled = false;
  int saverMinutes = 5;
  char saverStyle[8] = "clock"; // "clock" (drifting time) or "blank" (screen off)

  // Display orientation in degrees (0/90/180/270) for sideways or
  // upside-down mounting, and square-fit: render everything into a
  // centered square (min dimension) on non-square panels, for people
  // who want a 1:1 face on the 240x280 display. Both require a display
  // re-init, so changes trigger the same restart path as a board change.
  int rotation = 0;
  bool squareFit = false;
} config;

Preferences prefs;

void loadConfig() {
  prefs.begin("tinyscreen", true); // read-only
  config.configured = prefs.getBool("configured", false);
  config.boardId = prefs.getInt("boardId", 0);
  config.autoCycle = prefs.getBool("autoCycle", false);
  config.cycleSeconds = prefs.getInt("cycleSec", 10);
  config.brightness = prefs.getInt("brightness", 100);
  // Night mode / screensaver (added in 1.1.0) -- the getX defaults mean a
  // device upgrading from 1.0.0 NVS data just gets both features off,
  // exactly as if freshly configured.
  config.nightEnabled = prefs.getBool("nightEn", false);
  config.nightStartMin = prefs.getInt("nightStart", 1320);
  config.nightEndMin = prefs.getInt("nightEnd", 420);
  config.nightBrightness = prefs.getInt("nightBri", 10);
  config.tzOffsetMin = prefs.getInt("tzOffset", 0);
  config.saverEnabled = prefs.getBool("saverEn", false);
  config.saverMinutes = prefs.getInt("saverMin", 5);
  String saverStyle = prefs.getString("saverStyle", "clock");
  saverStyle.toCharArray(config.saverStyle, 8);
  config.rotation = prefs.getInt("rotation", 0);
  config.squareFit = prefs.getBool("squareFit", false);
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
  if (config.boardId < 0 || config.boardId >= NUM_BOARD_PROFILES) {
    // A saved boardId that no longer exists (e.g. the board list changed
    // between firmware versions) is a sign this NVS data is stale, not
    // just a number to clamp. Silently falling back to board 0's pins
    // while still treating the device as "configured" is exactly what
    // caused a real GPIO conflict once already (board 0's default
    // backlight pin collided with another board's native USB data line).
    // Go back to the safe hands-off state instead and wait for a fresh,
    // deliberate set_config command.
    config.boardId = 0;
    config.configured = false;
  }
}

void saveConfig() {
  prefs.begin("tinyscreen", false); // read-write
  prefs.putBool("configured", config.configured);
  prefs.putInt("boardId", config.boardId);
  prefs.putBool("autoCycle", config.autoCycle);
  prefs.putInt("cycleSec", config.cycleSeconds);
  prefs.putInt("brightness", config.brightness);
  prefs.putBool("nightEn", config.nightEnabled);
  prefs.putInt("nightStart", config.nightStartMin);
  prefs.putInt("nightEnd", config.nightEndMin);
  prefs.putInt("nightBri", config.nightBrightness);
  prefs.putInt("tzOffset", config.tzOffsetMin);
  prefs.putBool("saverEn", config.saverEnabled);
  prefs.putInt("saverMin", config.saverMinutes);
  prefs.putString("saverStyle", config.saverStyle);
  prefs.putInt("rotation", config.rotation);
  prefs.putBool("squareFit", config.squareFit);
  String pagesCsv = "";
  for (int i = 0; i < config.numPages; i++) {
    if (i > 0) pagesCsv += ",";
    pagesCsv += config.pages[i];
  }
  prefs.putString("pages", pagesCsv);
  prefs.end();
}

// ---------------------------------------------------------------------
// Wall-clock time (host-supplied) + night mode + screensaver state
//
// The board has no RTC: the collector includes "utc_min" (minutes since
// UTC midnight) in every stats payload (~1/sec), and we extrapolate
// between updates with millis(). Worst case without any data the clock
// simply isn't known and night mode stays out of the way (full normal
// brightness) -- fail-safe by construction.
//
// minutesInWindow()/extrapolateLocalMin() are deliberately PURE
// functions (no globals) so the exact shipped code can be compiled and
// unit-tested on a host machine -- see tests/test_firmware_logic.cpp.
// ---------------------------------------------------------------------

int lastUtcMin = -1;              // -1 = no time received yet
unsigned long lastUtcAtMs = 0;
// Preferred time source (1.3.0+): the collector computes minutes since
// LOCAL midnight using the real timezone database (DST included) and
// sends it as "local_min". When present it wins over the older
// utc_min + fixed-offset math, which can drift an hour across a DST
// change until re-saved.
int lastLocalMin = -1;
unsigned long lastLocalAtMs = 0;

unsigned long lastTouchMs = 0;    // any finger contact, not just swipes
bool saverActive = false;
bool swallowGesture = false;      // eat the swipe that wakes the saver
int lastAppliedBrightness = -1;   // pct; -1 forces first application
unsigned long lastBrightnessCheckMs = 0;

// True when nowMin lies inside [startMin, endMin), handling windows that
// wrap past midnight (start > end, e.g. 22:00-07:00). start == end is
// treated as an empty window, not 24h -- a zero-length schedule almost
// certainly means a misconfigured form, and "night mode never engages"
// is the recoverable failure mode (the screen stays visible).
bool minutesInWindow(int nowMin, int startMin, int endMin) {
  if (nowMin < 0 || startMin == endMin) return false;
  if (startMin < endMin) return nowMin >= startMin && nowMin < endMin;
  return nowMin >= startMin || nowMin < endMin;  // wraps midnight
}

// Current local time in minutes since midnight, extrapolated from the
// last host-supplied UTC reading; -1 if no reading yet. Extrapolation
// keeps working across a data outage (millis() keeps counting), and the
// double-mod handles negative results from negative tz offsets.
int extrapolateLocalMin(int lastUtc, unsigned long lastAtMs,
                        unsigned long nowMs, int tzOffset) {
  if (lastUtc < 0) return -1;
  long elapsedMin = (long)((nowMs - lastAtMs) / 60000UL);
  long local = ((long)lastUtc + elapsedMin + (long)tzOffset) % 1440L;
  return (int)((local + 1440L) % 1440L);
}

// Best-available local time: DST-aware host local_min when we have it,
// otherwise UTC + the browser-captured fixed offset.
int localNowMin() {
  if (lastLocalMin >= 0) {
    return extrapolateLocalMin(lastLocalMin, lastLocalAtMs, millis(), 0);
  }
  return extrapolateLocalMin(lastUtcMin, lastUtcAtMs, millis(),
                             config.tzOffsetMin);
}

int effectiveBrightnessPct() {
  int nowLocal = localNowMin();
  if (config.nightEnabled &&
      minutesInWindow(nowLocal, config.nightStartMin, config.nightEndMin)) {
    return config.nightBrightness;
  }
  return config.brightness;
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
int screenW = 240;   // physical panel size AFTER rotation
int screenH = 240;
// Layout box: where pages actually render. Same as the physical screen
// normally; with square-fit on a non-square panel it's a centered
// min-dimension square (e.g. 240x240 letterboxed inside 240x280). All
// drawing goes through LX/LY/LW/LH so pages don't care which mode
// they're in. computeLayoutBox is pure for host-side unit testing.
int LX = 0, LY = 0, LW = 240, LH = 240;
void computeLayoutBox(int physW, int physH, bool squareFit,
                      int *lx, int *ly, int *lw, int *lh) {
  if (squareFit && physW != physH) {
    int side = physW < physH ? physW : physH;
    *lx = (physW - side) / 2;
    *ly = (physH - side) / 2;
    *lw = side;
    *lh = side;
  } else {
    *lx = 0; *ly = 0; *lw = physW; *lh = physH;
  }
}

// Horizontal swipe delta in DISPLAY space from raw touch deltas -- the
// touch controller reports panel-native coordinates, so a rotated
// display swaps/flips which raw axis means "left/right". Pure for
// host-side unit testing.
int mapSwipeDeltaX(int rawDx, int rawDy, int rotationDeg) {
  switch (rotationDeg) {
    case 90:  return rawDy;
    case 180: return -rawDx;
    case 270: return -rawDy;
    default:  return rawDx;
  }
}

// Layout constants below were tuned against a 240x240 reference screen.
// SY() scales a Y-coordinate proportionally for taller/shorter screens
// (e.g. board 1's 240x280 panel) so the layout doesn't just run off the
// bottom or leave a big gap -- X doesn't need this since all boards so
// far share the same 240 width.
int SY(int y) { return LY + y * LH / 240; }
int SYB(int fromBottom) { return LY + LH - fromBottom * LH / 240; }
int CX() { return LX + LW / 2; }

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
  // Arduino_GFX takes rotation as quarter-turns (0-3) and handles the
  // panel offset bookkeeping per rotation itself; we pass the PANEL
  // native dims and swap our own working dims for 90/270.
  int rot = ((config.rotation % 360) + 360) % 360 / 90;
  bool swapped = (rot == 1 || rot == 3);
  screenW = swapped ? p.height : p.width;
  screenH = swapped ? p.width : p.height;
  computeLayoutBox(screenW, screenH, config.squareFit, &LX, &LY, &LW, &LH);
  bus = new Arduino_ESP32SPI(p.lcd_dc, p.lcd_cs, p.lcd_sck, p.lcd_mosi, -1 /* no MISO */);
  if (p.driverIsGC9A01) {
    gfx = new Arduino_GC9A01(bus, p.lcd_rst, rot, true /* IPS */);
  } else {
    gfx = new Arduino_ST7789(bus, p.lcd_rst, rot, true /* IPS */, p.width, p.height,
                              p.colOffset1, p.rowOffset1, p.colOffset2, p.rowOffset2);
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
  // Night mode aware: what actually reaches the backlight is the
  // effective brightness (night window may substitute a dimmer value),
  // and a blank-style active screensaver forces the backlight off
  // entirely regardless of everything else.
  int pct = effectiveBrightnessPct();
  if (saverActive && strcmp(config.saverStyle, "blank") == 0) pct = 0;
  pwmWriteBacklight(BOARD_PROFILES[config.boardId].lcd_bl, map(pct, 0, 100, 0, 255));
  lastAppliedBrightness = pct;
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
  float mmc_total_gb = 0;
  float mmc_pct = 0;
  float net_rx_mbps = 0;
  float net_tx_mbps = 0;
  bool nas_available = false;
  float nas_total_gb = 0;
  float nas_pct = 0;
  unsigned long last_update_ms = 0;
} stats;

bool haveData = false;

// ---------------------------------------------------------------------
// Carousel state
// ---------------------------------------------------------------------

int currentPageIdx = 0;              // index into config.pages
unsigned long lastDrawMs = 0;
unsigned long lastCycleMs = 0;
unsigned long lastGesturePollMs = 0;
const unsigned long FRAME_INTERVAL_MS = 200;
const unsigned long GESTURE_POLL_MS = 20; // touch feels much more responsive polled this often

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
  canvas->setCursor(CX() - w / 2, SY(24));
  canvas->print(msg);
}

void drawFooterDots() {
  if (config.numPages <= 1) return;
  int totalW = config.numPages * 12;
  int startX = CX() - totalW / 2;
  int y = SYB(14);
  for (int i = 0; i < config.numPages; i++) {
    uint16_t c = (i == currentPageIdx) ? COL_TEAL : COL_RING_BG;
    canvas->fillCircle(startX + i * 12 + 6, y, 3, c);
  }
}

void drawPageCPU() {
  char big[16];
  snprintf(big, sizeof(big), "%d%%", (int)round(stats.cpu_pct));
  drawRingGauge(CX(), SY(105), 88, 74, stats.cpu_pct, COL_TEAL, big, "CPU LOAD");
  canvas->setTextColor(COL_TEXT);
  canvas->setTextSize(2);
  char watts[16];
  snprintf(watts, sizeof(watts), "%.1f W", stats.cpu_watts);
  int16_t x1, y1; uint16_t w, h;
  canvas->getTextBounds(watts, 0, 0, &x1, &y1, &w, &h);
  canvas->setCursor(CX() - w / 2, SY(172));
  canvas->print(watts);
}

void drawPageRAM() {
  char big[16];
  snprintf(big, sizeof(big), "%d%%", (int)round(stats.ram_pct));
  drawRingGauge(CX(), SY(105), 88, 74, stats.ram_pct, COL_TEAL_2, big, "RAM USED");
  canvas->setTextColor(COL_TEXT);
  canvas->setTextSize(2);
  char total[24];
  snprintf(total, sizeof(total), "%.1f GB", stats.ram_total_gb);
  int16_t x1, y1; uint16_t w, h;
  canvas->getTextBounds(total, 0, 0, &x1, &y1, &w, &h);
  canvas->setCursor(CX() - w / 2, SY(172));
  canvas->print(total);
}

void drawPageMMC() {
  char big[16];
  snprintf(big, sizeof(big), "%d%%", (int)round(stats.mmc_pct));
  drawRingGauge(CX(), SY(105), 88, 74, stats.mmc_pct, 0x7B9F, big, "MMC USED");
  canvas->setTextColor(COL_TEXT);
  canvas->setTextSize(2);
  char total[24];
  snprintf(total, sizeof(total), "%.0f GB", stats.mmc_total_gb);
  int16_t x1, y1; uint16_t w, h;
  canvas->getTextBounds(total, 0, 0, &x1, &y1, &w, &h);
  canvas->setCursor(CX() - w / 2, SY(172));
  canvas->print(total);
}

void drawPageNAS() {
  if (!stats.nas_available) {
    canvas->setTextColor(COL_SUBTEXT);
    canvas->setTextSize(1);
    const char *msg = "No NAS pool detected";
    int16_t x1, y1; uint16_t w, h;
    canvas->getTextBounds(msg, 0, 0, &x1, &y1, &w, &h);
    canvas->setCursor(CX() - w / 2, SY(115));
    canvas->print(msg);
    return;
  }
  char big[16];
  snprintf(big, sizeof(big), "%d%%", (int)round(stats.nas_pct));
  drawRingGauge(CX(), SY(105), 88, 74, stats.nas_pct, 0x9F7BC0, big, "NAS USED");
  canvas->setTextColor(COL_TEXT);
  canvas->setTextSize(2);
  char total[24];
  snprintf(total, sizeof(total), "%.0f GB", stats.nas_total_gb);
  int16_t x1, y1; uint16_t w, h;
  canvas->getTextBounds(total, 0, 0, &x1, &y1, &w, &h);
  canvas->setCursor(CX() - w / 2, SY(172));
  canvas->print(total);
}

void drawPageNet() {
  canvas->setTextColor(COL_SUBTEXT);
  canvas->setTextSize(1);
  canvas->setCursor(CX() - 28, SY(50));
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
  canvas->setCursor(CX() - 20, SY(90));
  canvas->print("DOWN");
  canvas->setTextColor(COL_TEXT);
  canvas->setTextSize(2);
  canvas->getTextBounds(rx, 0, 0, &x1, &y1, &w, &h);
  canvas->setCursor(CX() - w / 2, SY(105));
  canvas->print(rx);

  canvas->setTextColor(COL_TEAL_2);
  canvas->setTextSize(1);
  canvas->setCursor(CX() - 12, SY(150));
  canvas->print("UP");
  canvas->setTextColor(COL_TEXT);
  canvas->setTextSize(2);
  canvas->getTextBounds(tx, 0, 0, &x1, &y1, &w, &h);
  canvas->setCursor(CX() - w / 2, SY(165));
  canvas->print(tx);
}

void drawPageTemp() {
  float pct = constrain(stats.cpu_temp_c, 0.0f, 100.0f);
  uint16_t color = stats.cpu_temp_c >= 75 ? COL_WARN : COL_TEAL;
  char big[16];
  snprintf(big, sizeof(big), "%.0fC", stats.cpu_temp_c);
  drawRingGauge(CX(), SY(105), 88, 74, pct, color, big, "CPU TEMP");
}

void drawPage(const char *pageId) {
  if (strcmp(pageId, "cpu") == 0) drawPageCPU();
  else if (strcmp(pageId, "ram") == 0) drawPageRAM();
  else if (strcmp(pageId, "mmc") == 0) drawPageMMC();
  else if (strcmp(pageId, "net") == 0) drawPageNet();
  else if (strcmp(pageId, "nas") == 0) drawPageNAS();
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
// Touch (CST816-family) -- only polled when the active board profile has
// touch. Deliberately NOT using the chip's own built-in gesture
// recognition (GestureID register) -- testing showed it holds its last
// value for several seconds after a swipe completes, which is far too
// long and undocumented to build reliable timing around. Instead we
// track raw touch-down -> touch-up X coordinates ourselves and compute
// the swipe direction on release, giving full control over sensitivity.
// ---------------------------------------------------------------------

#define GESTURE_NONE  0
#define GESTURE_LEFT  1
#define GESTURE_RIGHT 2

bool touchIsDown = false;
int touchStartX = 0;
int touchLastX = 0;
int touchStartY = 0;
int touchLastY = 0;
const int SWIPE_THRESHOLD_PX = 40;

// Returns GESTURE_LEFT/GESTURE_RIGHT exactly once, at the moment a swipe
// completes (finger release) -- inherently edge-triggered by
// construction, so callers don't need their own debounce/edge logic.
int pollTouchSwipe() {
  Wire.beginTransmission(TOUCH_I2C_ADDR);
  Wire.write(0x02); // FingerNum register; X/Y registers follow immediately after
  if (Wire.endTransmission(false) != 0) return GESTURE_NONE;
  if (Wire.requestFrom(TOUCH_I2C_ADDR, 5) != 5) return GESTURE_NONE;
  uint8_t fingerNum = Wire.read();
  uint8_t xh = Wire.read();
  uint8_t xl = Wire.read();
  uint8_t yh = Wire.read();
  uint8_t yl = Wire.read();

  bool isDown = fingerNum > 0;
  // Both axes now: the touch chip reports PANEL-native coordinates, so
  // when the display is mounted rotated, "left/right" on the visible
  // screen may be the panel's Y axis. mapSwipeDeltaX() sorts that out.
  int x = ((xh & 0x0F) << 8) | xl;
  int y = ((yh & 0x0F) << 8) | yl;
  if (isDown) lastTouchMs = millis();  // screensaver idle timer

  int result = GESTURE_NONE;
  if (isDown && !touchIsDown) {
    // Finger just touched down -- start of a possible swipe
    touchIsDown = true;
    touchStartX = x;
    touchLastX = x;
    touchStartY = y;
    touchLastY = y;
  } else if (isDown && touchIsDown) {
    // Still touching -- track the latest position
    touchLastX = x;
    touchLastY = y;
  } else if (!isDown && touchIsDown) {
    // Finger just released -- decide if it was a swipe
    touchIsDown = false;
    int deltaX = mapSwipeDeltaX(touchLastX - touchStartX,
                                touchLastY - touchStartY, config.rotation);
    if (deltaX <= -SWIPE_THRESHOLD_PX) result = GESTURE_LEFT;
    else if (deltaX >= SWIPE_THRESHOLD_PX) result = GESTURE_RIGHT;
  }
  return result;
}

// ---------------------------------------------------------------------
// Screensaver drawing -- "clock" style shows the local time, drifting to
// a different position each minute so no pixel stays lit (burn-in
// protection, the whole reason a screensaver exists). "blank" style
// never reaches here: applyBrightness() cuts the backlight instead and
// loop() skips drawing entirely.
// ---------------------------------------------------------------------

void drawScreensaver() {
  canvas->fillScreen(COL_BG);
  int nowLocal = localNowMin();
  if (nowLocal >= 0) {
    char timeStr[6];
    snprintf(timeStr, sizeof(timeStr), "%d:%02d", nowLocal / 60, nowLocal % 60);
    canvas->setTextColor(COL_SUBTEXT);
    canvas->setTextSize(3);
    int16_t x1, y1; uint16_t w, h;
    canvas->getTextBounds(timeStr, 0, 0, &x1, &y1, &w, &h);
    // Drift: derive the position from the minute value itself, keeping
    // the text fully on screen. A simple hash walks it around.
    int spanX = LW - (int)w - 8;
    int spanY = LH - (int)h - 8;
    int px = LX + 4 + (nowLocal * 37) % (spanX > 0 ? spanX : 1);
    int py = LY + 4 + (nowLocal * 53) % (spanY > 0 ? spanY : 1);
    canvas->setCursor(px, py + h);
    canvas->print(timeStr);
  }
  // No time known yet -> stays a dark screen, which is a perfectly fine
  // screensaver too.
  canvas->flush();
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

  // Night mode / screensaver (1.1.0). Same only-if-present convention as
  // every field above, so older senders never clobber these.
  if (doc["night_enabled"].is<bool>())   config.nightEnabled = doc["night_enabled"];
  if (doc["night_start_min"].is<int>())  config.nightStartMin = constrain((int)doc["night_start_min"], 0, 1439);
  if (doc["night_end_min"].is<int>())    config.nightEndMin = constrain((int)doc["night_end_min"], 0, 1439);
  if (doc["night_brightness"].is<int>()) config.nightBrightness = constrain((int)doc["night_brightness"], 0, 100);
  if (doc["tz_offset_min"].is<int>())    config.tzOffsetMin = constrain((int)doc["tz_offset_min"], -840, 840);
  if (doc["saver_enabled"].is<bool>())   config.saverEnabled = doc["saver_enabled"];
  if (doc["saver_minutes"].is<int>())    config.saverMinutes = constrain((int)doc["saver_minutes"], 1, 240);
  if (doc["saver_style"].is<const char *>()) {
    const char *s = doc["saver_style"];
    if (strcmp(s, "clock") == 0 || strcmp(s, "blank") == 0) strcpy(config.saverStyle, s);
  }
  bool displayGeomChanged = false;
  if (doc["rotation"].is<int>()) {
    int r = doc["rotation"];
    if ((r == 0 || r == 90 || r == 180 || r == 270) && r != config.rotation) {
      config.rotation = r;
      displayGeomChanged = true;
    }
  }
  if (doc["square_fit"].is<bool>()) {
    bool sq = doc["square_fit"];
    if (sq != config.squareFit) {
      config.squareFit = sq;
      displayGeomChanged = true;
    }
  }
  // Settings may have changed what the backlight should be doing right
  // now (e.g. night mode just enabled mid-window, or saver turned off
  // while active) -- recompute immediately rather than waiting for the
  // once-a-second check in loop().
  if (!boardChanged && wasConfigured) {
    if (!config.saverEnabled) saverActive = false;
    applyBrightness();
  }

  config.configured = true;
  saveConfig();

  // Ack so the settings page can confirm success
  Serial.println("{\"ack\":\"set_config\",\"ok\":true}");

  if (boardChanged || displayGeomChanged || !wasConfigured) {
    // Pins/driver differ per board, and rotation / square-fit need the
    // display and canvas re-created with different dimensions -- cleanest
    // to just restart into setup() with the new settings rather than
    // trying to hot-swap display objects.
    // Also always restart on the very first-ever config, since an
    // unconfigured device never called initDisplay() at all (see setup()).
    Serial.println("{\"info\":\"restarting to apply configuration\"}");
    pendingRestart = true;
    restartAtMs = millis() + 300; // let the serial message flush first
  }
}

// Reports the current saved config back over Serial -- previously this
// whole protocol was write-only (settings.html/onboard.html/wizard.html
// could only ever SEND a config, never ask "what's currently set?").
// Needed for the settings dashboard to show real current state instead
// of just being another blind form.
void handleGetConfig() {
  JsonDocument doc;
  doc["ack"] = "get_config";
  doc["configured"] = config.configured;
  doc["board"] = config.boardId;
  doc["board_name"] = BOARD_PROFILES[config.boardId].name;
  JsonArray pages = doc["pages"].to<JsonArray>();
  for (int i = 0; i < config.numPages; i++) {
    pages.add(config.pages[i]);
  }
  doc["cycle_mode"] = config.autoCycle ? "auto" : "static";
  doc["cycle_seconds"] = config.cycleSeconds;
  doc["brightness"] = config.brightness;
  doc["night_enabled"] = config.nightEnabled;
  doc["night_start_min"] = config.nightStartMin;
  doc["night_end_min"] = config.nightEndMin;
  doc["night_brightness"] = config.nightBrightness;
  doc["tz_offset_min"] = config.tzOffsetMin;
  doc["saver_enabled"] = config.saverEnabled;
  doc["saver_minutes"] = config.saverMinutes;
  doc["saver_style"] = config.saverStyle;
  doc["rotation"] = config.rotation;
  doc["square_fit"] = config.squareFit;
  doc["has_touch"] = BOARD_PROFILES[config.boardId].hasTouch;
  doc["firmware_version"] = FIRMWARE_VERSION;

  serializeJson(doc, Serial);
  Serial.println();
}

// Factory-reset the stored configuration: wipe the NVS namespace and
// restart. loadConfig() after the reboot finds nothing and leaves the
// device in the deliberate hands-off unconfigured state (no display
// init, no GPIO touched -- see setup()), exactly like a fresh flash,
// waiting for the first-time wizard's set_config. The dashboard's Reset
// Device button drives this via POST /api/reset_device.
void handleClearConfig() {
  prefs.begin("tinyscreen", false);
  prefs.clear();
  prefs.end();
  Serial.println("{\"ack\":\"clear_config\",\"ok\":true}");
  Serial.println("{\"info\":\"configuration erased -- restarting into setup mode\"}");
  pendingRestart = true;
  restartAtMs = millis() + 300; // let the ack flush first, same as set_config
}

void handleLine(const String &line) {
  JsonDocument doc;
  DeserializationError err = deserializeJson(doc, line);
  if (err) {
    return;
  }

  if (doc["cmd"].is<const char *>() && strcmp(doc["cmd"], "set_config") == 0) {
    handleSetConfig(doc);
    return;
  }
  if (doc["cmd"].is<const char *>() && strcmp(doc["cmd"], "get_config") == 0) {
    handleGetConfig();
    return;
  }
  if (doc["cmd"].is<const char *>() && strcmp(doc["cmd"], "clear_config") == 0) {
    handleClearConfig();
    return;
  }

  // Otherwise treat as a stats update
  stats.cpu_name       = doc["cpu_name"] | stats.cpu_name;
  stats.cpu_pct        = doc["cpu_pct"] | stats.cpu_pct;
  stats.cpu_temp_c     = doc["cpu_temp_c"] | stats.cpu_temp_c;
  stats.cpu_watts      = doc["cpu_watts"] | stats.cpu_watts;
  stats.ram_total_gb   = doc["ram_total_gb"] | stats.ram_total_gb;
  stats.ram_pct        = doc["ram_pct"] | stats.ram_pct;
  stats.mmc_total_gb   = doc["mmc_total_gb"] | stats.mmc_total_gb;
  stats.mmc_pct        = doc["mmc_pct"] | stats.mmc_pct;
  stats.net_rx_mbps    = doc["net_rx_mbps"] | stats.net_rx_mbps;
  stats.net_tx_mbps    = doc["net_tx_mbps"] | stats.net_tx_mbps;
  stats.nas_available  = doc["nas_available"] | stats.nas_available;
  stats.nas_total_gb   = doc["nas_total_gb"] | stats.nas_total_gb;
  stats.nas_pct        = doc["nas_pct"] | stats.nas_pct;
  // Host wall-clock time (minutes since UTC midnight), for night mode and
  // the clock screensaver -- the board has no RTC of its own. Only update
  // when actually present, so older collectors keep working unchanged.
  if (doc["utc_min"].is<int>()) {
    int m = doc["utc_min"];
    if (m >= 0 && m < 1440) {
      lastUtcMin = m;
      lastUtcAtMs = millis();
    }
  }
  if (doc["local_min"].is<int>()) {
    int m = doc["local_min"];
    if (m >= 0 && m < 1440) {
      lastLocalMin = m;
      lastLocalAtMs = millis();
    }
  }
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
  // Root-caused via a payload-size isolation test: a ~270-byte write
  // (this project's real stats payload, spanning multiple 64-byte
  // full-speed USB packets) consistently failed to reach the firmware,
  // while a ~50-byte write (fitting in a single packet) always
  // succeeded -- same board, same connection, same open/write
  // mechanism, only the size differed. pollSerial() below only drains
  // incoming bytes once per loop() iteration, and loop() also does
  // display drawing and touch polling -- if a multi-packet burst
  // arrives while loop() is momentarily busy elsewhere, the default USB
  // CDC receive buffer can plausibly overflow and silently drop data.
  // Setting a much larger buffer here, BEFORE begin(), gives it enough
  // headroom to absorb a burst even if loop() is briefly delayed.
  Serial.setRxBufferSize(2048);
  Serial.begin(115200);
  delay(500); // give native USB CDC a moment to enumerate before printing
  loadConfig();
  if (config.configured) {
    // Only initialize display/backlight pins once we actually know which
    // physical board this is -- an unconfigured device stays fully
    // hands-off on GPIO (see handleSetConfig() for why: board 0's default
    // pins can collide with another board's USB data lines).
    initDisplay();
  }
  serialBuf.reserve(256);
}

void loop() {
  pollSerial();

  if (pendingRestart) {
    // A restart is scheduled to let the ack message finish sending before
    // rebooting into initDisplay() with the new profile. Don't touch any
    // board-specific hardware (touch I2C, display) in this window --
    // config.boardId may already reflect a NEW profile whose I2C/display
    // bus was never actually begin()'d yet (that only happens inside
    // initDisplay(), which runs fresh after the reboot below). Polling
    // touch here crashed with a Wire/I2C NULL-pointer panic since the bus
    // was never initialized for the new board.
    if (millis() >= restartAtMs) {
      ESP.restart();
    }
    return;
  }

  if (!config.configured) {
    // Nothing to draw yet -- just keep listening on Serial for the first
    // set_config command from webflasher/settings.html.
    return;
  }

  unsigned long now = millis();

  if (BOARD_PROFILES[config.boardId].hasTouch && now - lastGesturePollMs > GESTURE_POLL_MS) {
    lastGesturePollMs = now;
    bool wasSaverActive = saverActive;
    // pollTouchSwipe() only ever returns non-NONE once per completed
    // swipe (on finger release), so no separate edge-detection needed
    // here -- see its own comment for why we compute this ourselves
    // instead of trusting the touch chip's built-in gesture recognition.
    int g = pollTouchSwipe();
    // Waking the screensaver: pollTouchSwipe() just refreshed lastTouchMs
    // if a finger is down, so an active saver ends here -- and the touch
    // that woke it must NOT also act as a page swipe (nobody expects the
    // wake-up tap to navigate). swallowGesture eats the swipe that
    // completes from that same finger contact.
    if (wasSaverActive && touchIsDown) {
      saverActive = false;
      swallowGesture = true;
      applyBrightness();          // restore from blank-style backlight-off
      drawCurrentScreen();        // don't leave the saver frame up
      lastDrawMs = now;
    }
    if (g != GESTURE_NONE && swallowGesture) {
      swallowGesture = false;
    } else if (g == GESTURE_LEFT) advancePage(1);
    else if (g == GESTURE_RIGHT) advancePage(-1);
  }

  // Once a second: does the screensaver need to engage, and has the
  // effective brightness changed (night window opening/closing, or the
  // extrapolated clock ticking past a boundary)? Backlight writes only
  // happen when the value actually changed.
  if (now - lastBrightnessCheckMs > 1000UL) {
    lastBrightnessCheckMs = now;
    if (BOARD_PROFILES[config.boardId].hasTouch && config.saverEnabled &&
        !saverActive &&
        now - lastTouchMs > (unsigned long)config.saverMinutes * 60000UL) {
      saverActive = true;
      applyBrightness();  // blank style cuts the backlight here
    }
    int want = effectiveBrightnessPct();
    if (saverActive && strcmp(config.saverStyle, "blank") == 0) want = 0;
    if (want != lastAppliedBrightness) applyBrightness();
  }

  if (config.autoCycle && !saverActive &&
      now - lastCycleMs > (unsigned long)config.cycleSeconds * 1000UL) {
    advancePage(1);
  }

  if (now - lastDrawMs > FRAME_INTERVAL_MS) {
    if (saverActive) {
      // blank style: backlight is off, skip drawing entirely; clock
      // style: redraw (the time text drifts once a minute).
      if (strcmp(config.saverStyle, "clock") == 0) drawScreensaver();
    } else {
      drawCurrentScreen();
    }
    lastDrawMs = now;
    pollSerial(); // drain anything that arrived during the draw (the
                  // slowest step here) as soon as possible, rather than
                  // waiting for the next full loop() iteration -- see
                  // setup()'s RX buffer size comment for the failure
                  // this is defending against.
  }
}
