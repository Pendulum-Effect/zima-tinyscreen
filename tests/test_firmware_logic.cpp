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

  // ---- computeLayoutBox: aspect modes on the 240x280 panel ----
  int lx, ly, lw, lh;
  computeLayoutBox(240, 280, 1, &lx, &ly, &lw, &lh);       // square 1:1
  CHECK(lx == 0 && ly == 20 && lw == 240 && lh == 240);
  computeLayoutBox(280, 240, 1, &lx, &ly, &lw, &lh);       // rotated 90
  CHECK(lx == 20 && ly == 0 && lw == 240 && lh == 240);
  computeLayoutBox(240, 280, 0, &lx, &ly, &lw, &lh);       // full panel
  CHECK(lx == 0 && ly == 0 && lw == 240 && lh == 280);
  computeLayoutBox(240, 240, 1, &lx, &ly, &lw, &lh);       // already square
  CHECK(lx == 0 && ly == 0 && lw == 240 && lh == 240);
  // Compact 1.3" (mode 2): 200px centered square -- the physical size
  // of a 1.3" board's glass, so nothing hides behind a 1.3" cutout.
  computeLayoutBox(240, 280, 2, &lx, &ly, &lw, &lh);
  CHECK(lx == 20 && ly == 40 && lw == 200 && lh == 200);
  computeLayoutBox(280, 240, 2, &lx, &ly, &lw, &lh);       // rotated
  CHECK(lx == 40 && ly == 20 && lw == 200 && lh == 200);
  computeLayoutBox(240, 240, 2, &lx, &ly, &lw, &lh);       // on a 1.3" board
  CHECK(lx == 20 && ly == 20 && lw == 200 && lh == 200);
  computeLayoutBox(160, 160, 2, &lx, &ly, &lw, &lh);       // smaller than 200
  CHECK(lx == 0 && ly == 0 && lw == 160 && lh == 160);

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

    // drive: valid for mmc and nas, not net
    JsonDocument doc9;
    JsonArray pages9 = doc9["pages"].to<JsonArray>();
    pages9.add("nas"); pages9.add("net");
    doc9["layouts"]["nas"] = "drive";
    doc9["layouts"]["net"] = "drive";                   // not for net
    handleSetConfig(doc9);
    CHECK(strcmp(config.layouts[0], "drive") == 0);
    CHECK(strcmp(config.layouts[1], "default") == 0);
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

  // ---- 1.19 screensaver: brightness field + temp style + backlight policy ----
  {
    JsonDocument doc;
    doc["cmd"] = "set_config";
    doc["saver_enabled"] = true;
    doc["saver_style"] = "temp";        // new style must be accepted
    doc["saver_brightness"] = 250;      // clamps to 100
    handleSetConfig(doc);
    CHECK(config.saverEnabled);
    CHECK(strcmp(config.saverStyle, "temp") == 0);
    CHECK(config.saverBrightness == 100);

    JsonDocument doc2;
    doc2["cmd"] = "set_config";
    doc2["saver_style"] = "matrix";     // junk style: whitelist holds
    doc2["saver_brightness"] = -5;      // clamps to 0
    handleSetConfig(doc2);
    CHECK(strcmp(config.saverStyle, "temp") == 0);
    CHECK(config.saverBrightness == 0);

    // Backlight policy: a drawing saver only ever DIMS -- min(effective,
    // saver) -- and blank always cuts to zero. Night mode must win when
    // it is darker than the saver setting.
    config.brightness = 80;
    config.nightEnabled = false;
    config.saverBrightness = 30;
    saverActive = false;
    CHECK(wantedBacklightPct() == 80);              // saver idle: day level
    saverActive = true;
    CHECK(wantedBacklightPct() == 30);              // temp saver: capped
    config.saverBrightness = 100;
    CHECK(wantedBacklightPct() == 80);              // never brightens past day
    strcpy(config.saverStyle, "blank");
    CHECK(wantedBacklightPct() == 0);               // blank: backlight off
    strcpy(config.saverStyle, "temp");
    config.saverBrightness = 30;
    saverActive = false;
  }

  // ---- 1.22: roll gating -- animate at most every cooldown ----
  {
    // page switch always snaps (action 2), even mid-cooldown
    CHECK(decideRollAction(false, true, false, 1000, 900, 4000) == 2);
    // no change / already rolling -> keep (0)
    CHECK(decideRollAction(true, false, false, 9000, 0, 4000) == 0);
    CHECK(decideRollAction(true, true, true, 9000, 0, 4000) == 0);
    // change + cooldown elapsed -> roll (1)
    CHECK(decideRollAction(true, true, false, 5000, 1000, 4000) == 1);
    // change but too soon -> hold (0); rolls once the window passes
    CHECK(decideRollAction(true, true, false, 4999, 1000, 4000) == 0);
    CHECK(decideRollAction(true, true, false, 5001, 1000, 4000) == 1);
    // easing: monotone, endpoints exact
    CHECK(rollEase(0.0f) == 0.0f && rollEase(1.0f) == 1.0f);
    CHECK(rollEase(0.5f) > 0.49f && rollEase(0.5f) < 0.51f);
    CHECK(rollEase(0.25f) < rollEase(0.75f));
  }

  // ---- 1.22: aspect_mode via set_config (legacy square_fit maps) ----
  {
    config.configured = true;
    config.aspectMode = 0; config.squareFit = false;
    JsonDocument doc;
    doc["cmd"] = "set_config";
    doc["aspect_mode"] = 2;
    handleSetConfig(doc);
    CHECK(config.aspectMode == 2);
    CHECK(!config.squareFit);
    JsonDocument doc2;                    // legacy dashboards still work
    doc2["cmd"] = "set_config";
    doc2["square_fit"] = true;
    handleSetConfig(doc2);
    CHECK(config.aspectMode == 1);
    CHECK(config.squareFit);
    JsonDocument doc3;                    // explicit mode wins over legacy
    doc3["cmd"] = "set_config";
    doc3["square_fit"] = false;
    doc3["aspect_mode"] = 2;
    handleSetConfig(doc3);
    CHECK(config.aspectMode == 2);
    pendingRestart = false;
  }

  // ---- 1.31: SL scales lengths without the box offset ----
  {
    int sLX = LX, sLY = LY, sLW = LW, sLH = LH;
    // compact box on the 1.69" panel: the case that exposed the bug
    LX = 20; LY = 40; LW = 200; LH = 200;
    CHECK(SL(76) == 76 * 200 / 240);       // ring radius: 63, not 103
    CHECK(SY(76) == 40 + 76 * 200 / 240);  // SY is a position: offset included
    LY = 0; LH = 240;                      // full-height box: SL == SY - LY
    CHECK(SL(76) == 76 && SY(76) == 76);
    LX = sLX; LY = sLY; LW = sLW; LH = sLH;
  }

  // ---- 1.30: compact-mode companion faces ----
  {
    int savedLW = LW;
    LW = 240;  // full/square: identity
    CHECK(faceFor(&tiny_sans_bold_32) == &tiny_sans_bold_32);
    CHECK(faceFor(&tiny_sans_18) == &tiny_sans_18);
    LW = 200;  // compact: every face steps to its 200/240 companion
    CHECK(faceFor(&tiny_sans_18) == &tiny_sans_15);
    CHECK(faceFor(&tiny_sans_bold_20) == &tiny_sans_bold_17);
    CHECK(faceFor(&tiny_sans_bold_24) == &tiny_sans_bold_20);
    CHECK(faceFor(&tiny_sans_bold_32) == &tiny_sans_bold_27);
    CHECK(faceFor(&tiny_sans_bold_36) == &tiny_sans_bold_30);
    CHECK(faceFor(&tiny_sans_bold_64) == &tiny_sans_bold_53);
    CHECK(faceFor(&tiny_sans_bold_128) == &tiny_sans_bold_107);
    // unknown faces pass through untouched
    CHECK(faceFor(&tiny_sans_bold_107) == &tiny_sans_bold_107);
    LW = savedLW;
  }

  // ---- 1.25: gauge moves in lockstep with its digits ----
  {
    currentPageIdx = 0;
    g_fake_millis = 100000;
    // Fresh page: both snap. Text shows "45%", arc shows 45.
    drawValueTextCentered(0, "45%", 120, 120);
    float a0 = tweenValue(0, 45.0f);
    CHECK(a0 == 45.0f);
    // Value changes immediately (within the digits' cooldown): the
    // digits HOLD -- and the arc must hold WITH them.
    g_fake_millis += 1000;
    drawValueTextCentered(0, "52%", 120, 120);
    float a1 = tweenValue(0, 52.0f);
    CHECK(a1 == 45.0f);
    // Cooldown passes: the digits fire, and on the NEXT frame the arc
    // adopts the same snapshot and starts sweeping.
    g_fake_millis += 4000;
    drawValueTextCentered(0, "52%", 120, 120);   // roll fires here
    tweenValue(0, 52.0f);                        // arc sees the fire
    g_fake_millis += ROLL_MS / 2;                // mid-sweep
    float aMid = tweenValue(0, 52.0f);
    CHECK(aMid > 45.5f && aMid < 51.5f);
    g_fake_millis += ROLL_MS;                    // done
    float aEnd = tweenValue(0, 52.0f);
    CHECK(aEnd == 52.0f);
    // Float wiggle that never changes the string: digits don't fire,
    // arc stays planted.
    g_fake_millis += 60000;
    drawValueTextCentered(0, "52%", 120, 120);
    float aW = tweenValue(0, 52.4f);
    CHECK(aW == 52.0f);
    // Reset shared animation state for any later tests
    for (int i = 0; i < 12; i++) { rollSlots[i] = RollSlot(); tweenSlots[i] = TweenSlot(); }
    activeRolls = 0; activeTweens = 0;
  }

  // ---- 1.24: gauge tween evaluation ----
  {
    bool active;
    // At start: exactly the from value, still active
    CHECK(tweenEval(20.0f, 80.0f, 1000, 1000, 600, &active) == 20.0f);
    CHECK(active);
    // Midpoint of smoothstep: exactly halfway
    float mid = tweenEval(20.0f, 80.0f, 1000, 1300, 600, &active);
    CHECK(mid > 49.5f && mid < 50.5f && active);
    // Monotone toward the target
    float q1 = tweenEval(20.0f, 80.0f, 1000, 1150, 600, &active);
    float q3 = tweenEval(20.0f, 80.0f, 1000, 1450, 600, &active);
    CHECK(q1 < mid && mid < q3);
    // Done: exactly the target, inactive
    CHECK(tweenEval(20.0f, 80.0f, 1000, 1600, 600, &active) == 80.0f);
    CHECK(!active);
    // Downward tween works the same
    float dn = tweenEval(80.0f, 20.0f, 0, 300, 600, &active);
    CHECK(dn > 49.5f && dn < 50.5f && active);
    // Zero duration: instant
    CHECK(tweenEval(0.0f, 5.0f, 0, 0, 0, &active) == 5.0f && !active);
  }

  // ---- 1.21: a device only becomes configured when told its board ----
  {
    config.configured = false;
    config.boardId = 0;
    pendingRestart = false;              // earlier blocks may have armed it
    JsonDocument doc;                    // a layouts-save-shaped command
    doc["cmd"] = "set_config";
    doc["brightness"] = 70;
    handleSetConfig(doc);
    CHECK(!config.configured);           // still waiting for setup
    CHECK(config.brightness == 70);      // ...but the field DID apply
    CHECK(!pendingRestart);              // and no pointless restart

    JsonDocument doc2;                   // proper first-time setup
    doc2["cmd"] = "set_config";
    doc2["board"] = 1;
    handleSetConfig(doc2);
    CHECK(config.configured);
    CHECK(config.boardId == 1);
    CHECK(pendingRestart);               // first config restarts into initDisplay

    pendingRestart = false;
    JsonDocument doc3;                   // boardless saves keep configured
    doc3["cmd"] = "set_config";
    doc3["brightness"] = 90;
    handleSetConfig(doc3);
    CHECK(config.configured);
    pendingRestart = false;
  }

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
