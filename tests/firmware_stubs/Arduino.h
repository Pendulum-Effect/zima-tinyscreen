// Minimal host-side stub of Arduino.h -- just enough surface for
// firmware/src/main.cpp to COMPILE on a desktop g++ and for its pure
// logic functions (minutesInWindow, extrapolateLocalMin, ...) to run
// under real unit tests. Behavior of hardware-facing calls is
// deliberately inert; anything the tests need to control (millis) is
// backed by a settable variable.
#pragma once
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <cstdlib>
#include <cmath>
#include <string>
using std::round;

// -- GPIO ----------------------------------------------------------------
#define OUTPUT 1
#define LOW 0
#define HIGH 1
inline void pinMode(int, int) {}
inline void digitalWrite(int, int) {}

// -- test control knobs ------------------------------------------------
extern unsigned long g_fake_millis;
inline unsigned long millis() { return g_fake_millis; }
inline void delay(unsigned long) {}

// -- Arduino macros/helpers --------------------------------------------
template <typename T> T constrain(T v, T lo, T hi) { return v < lo ? lo : (v > hi ? hi : v); }
inline long map(long x, long in_min, long in_max, long out_min, long out_max) {
  return (x - in_min) * (out_max - out_min) / (in_max - in_min) + out_min;
}

// -- String (tiny std::string wrapper) ----------------------------------
class String {
 public:
  std::string s;
  String() {}
  String(const char *c) : s(c ? c : "") {}
  String(const std::string &x) : s(x) {}
  unsigned int length() const { return (unsigned int)s.size(); }
  char operator[](int i) const { return s[(size_t)i]; }
  String substring(int a, int b) const { return String(s.substr((size_t)a, (size_t)(b - a))); }
  void toCharArray(char *buf, unsigned int n) const { snprintf(buf, n, "%s", s.c_str()); }
  const char *c_str() const { return s.c_str(); }
  String &operator+=(char c) { s += c; return *this; }
  String &operator+=(const char *c) { s += c; return *this; }
  String &operator+=(const String &o) { s += o.s; return *this; }
  bool operator==(const char *c) const { return s == c; }
  void reserve(unsigned int) {}
};
inline String operator+(String a, const String &b) { a += b; return a; }

// -- Serial --------------------------------------------------------------
struct StubSerial {
  void begin(long) {}
  void setRxBufferSize(int) {}
  int available() { return 0; }
  int read() { return -1; }
  template <typename T> void print(T) {}
  template <typename T> void println(T) {}
  void println() {}
  size_t write(uint8_t) { return 1; }
  size_t write(const uint8_t *, size_t n) { return n; }
};
extern StubSerial Serial;

// -- ESP -----------------------------------------------------------------
struct StubESP { void restart() {} };
extern StubESP ESP;

// -- LEDC (both the core-3.x pin-based and legacy channel-based APIs,
// since main.cpp version-guards between them) ---------------------------
inline void ledcAttach(int, int, int) {}
inline void ledcSetup(int, int, int) {}
inline void ledcAttachPin(int, int) {}
inline void ledcWrite(int, int) {}
