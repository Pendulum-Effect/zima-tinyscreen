// Host-side unit tests of the firmware's pure logic -- compiles the REAL
// firmware/src/main.cpp against stub Arduino headers (so any syntax/type
// error anywhere in the firmware fails this build, doubling as a full
// compile smoke test the sandbox otherwise can't do), then exercises
// minutesInWindow / extrapolateLocalMin / effectiveBrightnessPct with
// controlled inputs. The real ESP32 build still happens in CI.
#include <cassert>
#include <cstdio>
#include "firmware_stubs/stubs_all.h"

// Globals the stubs declare extern
unsigned long g_fake_millis = 0;
StubSerial Serial;
StubESP ESP;
StubWire Wire;

#include "../firmware/src/main.cpp"

#define CHECK(cond) do { if (!(cond)) { \
  printf("FAIL line %d: %s\n", __LINE__, #cond); return 1; } } while (0)

int main() {
  // ---- minutesInWindow: normal window (09:00-17:00) ----
  CHECK(!minutesInWindow(8 * 60, 540, 1020));
  CHECK(minutesInWindow(9 * 60, 540, 1020));       // start inclusive
  CHECK(minutesInWindow(12 * 60, 540, 1020));
  CHECK(!minutesInWindow(17 * 60, 540, 1020));     // end exclusive
  // ---- wrapping window (22:00-07:00) ----
  CHECK(minutesInWindow(23 * 60, 1320, 420));
  CHECK(minutesInWindow(0, 1320, 420));            // midnight
  CHECK(minutesInWindow(6 * 60 + 59, 1320, 420));
  CHECK(!minutesInWindow(7 * 60, 1320, 420));
  CHECK(!minutesInWindow(12 * 60, 1320, 420));
  CHECK(minutesInWindow(22 * 60, 1320, 420));      // start inclusive
  // ---- degenerate cases ----
  CHECK(!minutesInWindow(-1, 1320, 420));          // no time known
  CHECK(!minutesInWindow(600, 600, 600));          // empty window

  // ---- extrapolateLocalMin ----
  CHECK(extrapolateLocalMin(-1, 0, 999999, 0) == -1);          // no reading
  CHECK(extrapolateLocalMin(720, 1000, 1000, 0) == 720);       // fresh, UTC
  CHECK(extrapolateLocalMin(720, 1000, 1000 + 5 * 60000UL, 0) == 725);   // +5min
  CHECK(extrapolateLocalMin(720, 1000, 1000, -300) == 420);    // UTC-5
  CHECK(extrapolateLocalMin(720, 1000, 1000, 330) == 1050);    // UTC+5:30
  CHECK(extrapolateLocalMin(10, 1000, 1000, -300) == 1150);    // negative wrap
  CHECK(extrapolateLocalMin(1435, 0, 10 * 60000UL, 0) == 5);   // day rollover
  // millis() overflow (~49.7 days): unsigned subtraction stays correct
  CHECK(extrapolateLocalMin(720, 4294967000UL, 4294967000UL + 120000UL, 0) == 722);

  // ---- effectiveBrightnessPct through the real config/globals ----
  config.brightness = 80;
  config.nightEnabled = true;
  config.nightStartMin = 1320; config.nightEndMin = 420;
  config.nightBrightness = 5;
  config.tzOffsetMin = 0;
  lastUtcMin = 23 * 60; lastUtcAtMs = 0; g_fake_millis = 0;
  CHECK(effectiveBrightnessPct() == 5);            // 23:00, inside window
  lastUtcMin = 12 * 60;
  CHECK(effectiveBrightnessPct() == 80);           // noon, outside
  config.nightEnabled = false;
  lastUtcMin = 23 * 60;
  CHECK(effectiveBrightnessPct() == 80);           // disabled
  config.nightEnabled = true;
  lastUtcMin = -1;                                  // never got time
  CHECK(effectiveBrightnessPct() == 80);           // fail-safe: normal
  // tz pushes noon UTC into a 22:00-07:00 local window (UTC+11)
  lastUtcMin = 12 * 60; config.tzOffsetMin = 660;
  CHECK(effectiveBrightnessPct() == 5);

  // ---- clear_config: full reset round-trip through the real code ----
  // Configure -> save -> clear -> reload must land back at factory
  // defaults with configured=false (the hands-off wizard-waiting state).
  config.configured = true;
  config.boardId = 1;
  config.nightEnabled = true;
  config.brightness = 42;
  strcpy(config.pages[0], "cpu"); config.numPages = 1;
  saveConfig();
  pendingRestart = false;
  handleClearConfig();
  CHECK(pendingRestart);                       // restart scheduled
  config = Config{};                           // simulate the reboot's fresh state
  loadConfig();
  CHECK(!config.configured);                   // back to unconfigured
  CHECK(config.boardId == 0);
  CHECK(!config.nightEnabled);
  CHECK(config.brightness == 100);
  CHECK(config.numPages == 1 && strcmp(config.pages[0], "temp") == 0);

  // ---- computeLayoutBox: square fit on the 240x280 panel ----
  int lx, ly, lw, lh;
  computeLayoutBox(240, 280, true, &lx, &ly, &lw, &lh);
  CHECK(lx == 0 && ly == 20 && lw == 240 && lh == 240);   // letterboxed
  computeLayoutBox(280, 240, true, &lx, &ly, &lw, &lh);   // rotated 90
  CHECK(lx == 20 && ly == 0 && lw == 240 && lh == 240);   // pillarboxed
  computeLayoutBox(240, 280, false, &lx, &ly, &lw, &lh);  // native
  CHECK(lx == 0 && ly == 0 && lw == 240 && lh == 280);
  computeLayoutBox(240, 240, true, &lx, &ly, &lw, &lh);   // already square
  CHECK(lx == 0 && ly == 0 && lw == 240 && lh == 240);

  // ---- mapSwipeDeltaX per rotation ----
  CHECK(mapSwipeDeltaX(50, 5, 0) == 50);     // native: raw X is display X
  CHECK(mapSwipeDeltaX(50, 5, 180) == -50);  // upside down: flipped
  CHECK(mapSwipeDeltaX(5, 50, 90) == 50);    // sideways: raw Y is display X
  CHECK(mapSwipeDeltaX(5, 50, 270) == -50);

  // ---- localNowMin prefers DST-aware local_min over utc+offset ----
  g_fake_millis = 0;
  lastLocalMin = -1;
  lastUtcMin = 12 * 60; lastUtcAtMs = 0;
  config.tzOffsetMin = -300;
  CHECK(localNowMin() == 7 * 60);            // fallback: utc-5
  lastLocalMin = 8 * 60; lastLocalAtMs = 0;  // host says 8:00 local (DST shifted)
  CHECK(localNowMin() == 8 * 60);            // local_min wins, offset ignored
  g_fake_millis = 5 * 60000UL;
  CHECK(localNowMin() == 8 * 60 + 5);        // extrapolates too

  // ---- color math: 565 packing, lerp endpoints, temp ramp ----
  CHECK(rgb565(255, 255, 255) == 0xFFFF);
  CHECK(rgb565(0, 0, 0) == 0x0000);
  CHECK(lerpColor565(0x0000, 0xFFFF, 0) == 0x0000);
  CHECK(lerpColor565(0x0000, 0xFFFF, 255) == 0xFFFF);
  uint16_t mistGreen = rgb565(96, 205, 120), mistRed = rgb565(255, 92, 74);
  CHECK(tempColorFor(30.0f) == mistGreen);    // cool -> green
  CHECK(tempColorFor(45.0f) == mistGreen);    // ramp starts above 45
  CHECK(tempColorFor(90.0f) == mistRed);      // hot -> red
  CHECK(tempColorFor(55.0f) != mistGreen);    // mid-ramp is neither
  CHECK(tempColorFor(55.0f) != mistRed);
  // red channel rises monotonically through the ramp
  int r50 = (tempColorFor(50) >> 11), r60 = (tempColorFor(60) >> 11),
      r75 = (tempColorFor(75) >> 11);
  CHECK(r50 <= r60 && r60 <= r75);
  CHECK(dimColor565(0xFFFF, 50, 100) < 0xFFFF);
  CHECK(dimColor565(0xFFFF, 0, 100) == 0x0000);

  // ---- mist particles: respawn near the corner, drifting up/left ----
  uint32_t seed = 42;
  MistParticle mp;
  for (int i = 0; i < 50; i++) {
    mistRespawn(&mp, &seed, 0, 240);   // bottom-LEFT corner
    CHECK(mp.x >= 0 && mp.x < 35);
    CHECK(mp.y > 240 - 35 && mp.y <= 240);
    CHECK(mp.vx >= 0 && mp.vx <= 3 && mp.vy <= 0 && mp.vy >= -3);
    CHECK(mp.vx != 0 || mp.vy != 0);   // never a dead stop
    CHECK(mp.life == mp.maxLife && mp.maxLife >= 20);
  }

  // ---- layouts protocol: set_config -> NVS roundtrip -> whitelist ----
  {
    JsonDocument doc;
    doc["cmd"] = "set_config";
    JsonArray pages = doc["pages"].to<JsonArray>();
    pages.add("temp"); pages.add("cpu");
    doc["layouts"]["temp"] = "mist";
    doc["layouts"]["cpu"] = "mist";   // not valid for cpu -> default
    handleSetConfig(doc);
    CHECK(strcmp(config.pages[0], "temp") == 0);
    CHECK(strcmp(config.layouts[0], "mist") == 0);
    CHECK(strcmp(config.layouts[1], "default") == 0);  // whitelist held
    CHECK(strcmp(layoutForPage("temp"), "mist") == 0);
    CHECK(strcmp(layoutForPage("net"), "default") == 0);
    saveConfig();
    config = Config{};
    loadConfig();
    CHECK(strcmp(config.layouts[0], "mist") == 0);     // survived NVS
    CHECK(strcmp(config.layouts[1], "default") == 0);

    JsonDocument doc2;                                  // bogus id
    doc2["layouts"]["temp"] = "sparkles";
    handleSetConfig(doc2);
    CHECK(strcmp(config.layouts[0], "default") == 0);

    JsonDocument doc3;                                  // mist_anim valid
    doc3["layouts"]["temp"] = "mist_anim";
    handleSetConfig(doc3);
    CHECK(strcmp(config.layouts[0], "mist_anim") == 0);

    // dial: valid for cpu and ram, nowhere else
    JsonDocument doc4;
    doc4["layouts"]["cpu"] = "dial";
    handleSetConfig(doc4);
    CHECK(strcmp(config.layouts[1], "dial") == 0);
    CHECK(strcmp(layoutForPage("cpu"), "dial") == 0);
    JsonDocument doc5;
    doc5["layouts"]["temp"] = "dial";                   // not for temp
    handleSetConfig(doc5);
    CHECK(strcmp(config.layouts[0], "default") == 0);

    // bars: valid for net only
    JsonDocument doc6;
    JsonArray pages6 = doc6["pages"].to<JsonArray>();
    pages6.add("net"); pages6.add("cpu");
    doc6["layouts"]["net"] = "bars";
    doc6["layouts"]["cpu"] = "bars";                    // not for cpu
    handleSetConfig(doc6);
    CHECK(strcmp(config.layouts[0], "bars") == 0);
    CHECK(strcmp(config.layouts[1], "default") == 0);
    CHECK(strcmp(layoutForPage("net"), "bars") == 0);
    JsonDocument doc6b;
    JsonArray pages6b = doc6b["pages"].to<JsonArray>();
    pages6b.add("net"); pages6b.add("ram");
    doc6b["layouts"]["net"] = "graph";
    doc6b["layouts"]["ram"] = "graph";                  // not for ram
    handleSetConfig(doc6b);
    CHECK(strcmp(config.layouts[0], "graph") == 0);
    CHECK(strcmp(config.layouts[1], "default") == 0);

    // dots: valid for mmc and nas only
    JsonDocument doc7;
    JsonArray pages7 = doc7["pages"].to<JsonArray>();
    pages7.add("mmc"); pages7.add("nas"); pages7.add("cpu");
    doc7["layouts"]["mmc"] = "dots";
    doc7["layouts"]["nas"] = "dots";
    doc7["layouts"]["cpu"] = "dots";                    // not for cpu
    handleSetConfig(doc7);
    CHECK(strcmp(config.layouts[0], "dots") == 0);
    CHECK(strcmp(config.layouts[1], "dots") == 0);
    CHECK(strcmp(config.layouts[2], "default") == 0);

    // ring: valid for cpu and ram, not storage
    JsonDocument doc8;
    JsonArray pages8 = doc8["pages"].to<JsonArray>();
    pages8.add("ram"); pages8.add("mmc");
    doc8["layouts"]["ram"] = "ring";
    doc8["layouts"]["mmc"] = "ring";                    // not for mmc
    handleSetConfig(doc8);
    CHECK(strcmp(config.layouts[0], "ring") == 0);
    CHECK(strcmp(config.layouts[1], "default") == 0);
    CHECK(strcmp(layoutForPage("ram"), "ring") == 0);
  }

  // ---- Bars layout helpers: megabit formatting + bar fill ----
  {
    char b[16];
    fmtBitsRate(0.5f, b, sizeof(b));          // browsing trickle
    CHECK(strcmp(b, "500 Kbps") == 0);
    fmtBitsRate(3.25f, b, sizeof(b));
    CHECK(strcmp(b, "3.2 Mbps") == 0);
    fmtBitsRate(950.0f, b, sizeof(b));        // the speedtest that started this
    CHECK(strcmp(b, "950 Mbps") == 0);
    fmtBitsRate(1200.0f, b, sizeof(b));       // 2.5GbE someday
    CHECK(strcmp(b, "1.20 Gbps") == 0);
    fmtBitsRate(950.0f, b, sizeof(b), true);  // compact for the Graph header
    CHECK(strcmp(b, "950 Mb") == 0);
    fmtBitsRate(0.5f, b, sizeof(b), true);
    CHECK(strcmp(b, "500 Kb") == 0);
    CHECK(netBarPct(0.0f) == 0);              // idle -> empty
    CHECK(netBarPct(0.02f) == 0);             // sub-threshold noise -> empty
    CHECK(netBarPct(0.5f) == 2);              // alive -> minimum sliver
    CHECK(netBarPct(500.0f) == 50);           // half a gigabit
    CHECK(netBarPct(950.0f) == 95);           // gigabit speedtest, nearly full
    CHECK(netBarPct(2000.0f) == 100);         // clamped
  }

  // ---- ZimaOS Graph: ring buffer + autoscale ----
  {
    CHECK(netGraphScale() == 0.05f);           // empty history -> floor
    netGraphPush(0.2f, 0.1f);
    netGraphPush(0.6f, 0.3f);
    CHECK(netHistLen == 2);
    CHECK(netHistAt(netHistRx, 0) == 0.6f);    // 0 = newest
    CHECK(netHistAt(netHistRx, 1) == 0.2f);
    CHECK(netGraphScale() == 0.6f);            // tallest of either series
    netGraphPush(0.1f, 5.0f);
    CHECK(netGraphScale() == 5.0f);            // upload can set the scale
    for (int i = 0; i < 300; i++) netGraphPush(1.0f, 1.0f);
    CHECK(netHistLen == NET_HIST);             // ring caps, no overflow
    CHECK(netHistAt(netHistRx, 0) == 1.0f);
    CHECK(netGraphScale() == 1.0f);            // 5.0 spike aged out
    netHistLen = 0; netHistHead = 0;           // leave state clean
  }

  // ---- Zima App Ring: ceiling fill so any usage shows ----
  CHECK(ringDotsLit(0) == 0);
  CHECK(ringDotsLit(6) == 2);                  // the mock's CPU card
  CHECK(ringDotsLit(11) == 3);                 // the mock's RAM card
  CHECK(ringDotsLit(0.5f) == 1);               // a whisper still lights one
  CHECK(ringDotsLit(100) == 20 && ringDotsLit(150) == 20);

  // ---- Dots storage layout: fill count + threshold colors ----
  CHECK(dotsLit(0) == 0 && dotsLit(100) == 48);
  CHECK(dotsLit(50) == 24);
  CHECK(dotsLit(120) == 48 && dotsLit(-5) == 0);   // clamped
  CHECK(dotsColorFor(50) == dotsColorFor(74.9f));  // green zone
  CHECK(dotsColorFor(74.9f) != dotsColorFor(75));  // amber at 75
  CHECK(dotsColorFor(75) == dotsColorFor(94.9f));
  CHECK(dotsColorFor(94.9f) != dotsColorFor(95));  // red at 95
  CHECK(dotsColorFor(95) == dotsColorFor(100));

  // ---- utilization ramp: green floor, red ceiling, warm middle ----
  CHECK(utilColorFor(0) == utilColorFor(60));            // flat green zone
  CHECK(utilColorFor(60) != utilColorFor(70));           // warming
  CHECK(utilColorFor(95) == utilColorFor(100));          // flat red zone
  CHECK(utilColorFor(80) != utilColorFor(95));

  // ---- generated fonts: sane ranges, degree glyph present, digits real ----
  CHECK(tiny_sans_18.first == 32 && tiny_sans_18.last == 176);
  CHECK(tiny_sans_bold_64.first == 32 && tiny_sans_bold_64.last == 176);
  CHECK(tiny_sans_18.yAdvance > 18 && tiny_sans_bold_64.yAdvance > 64);
  const GFXglyph *g5 = &tiny_sans_bold_64_Glyphs['5' - 32];
  CHECK(g5->width > 20 && g5->height > 30 && g5->xAdvance >= g5->width);
  const GFXglyph *gdeg = &tiny_sans_18_Glyphs[176 - 32];
  CHECK(gdeg->width > 0 && gdeg->yOffset < -8);   // degree sign: small, high
  const GFXglyph *gspace = &tiny_sans_18_Glyphs[' ' - 32];
  CHECK(gspace->width == 0 && gspace->xAdvance > 0);
  // jumbo digits-only face: digits real, letters stripped to zero-size
  CHECK(tiny_sans_bold_128.first == 32 && tiny_sans_bold_128.last == 176);
  const GFXglyph *g8 = &tiny_sans_bold_128_Glyphs['8' - 32];
  CHECK(g8->width > 60 && g8->height > 80 && g8->xAdvance >= g8->width);
  const GFXglyph *gA = &tiny_sans_bold_128_Glyphs['A' - 32];
  CHECK(gA->width == 0 && gA->height == 0);

  printf("ALL FIRMWARE LOGIC TESTS PASS\n");
  return 0;
}
