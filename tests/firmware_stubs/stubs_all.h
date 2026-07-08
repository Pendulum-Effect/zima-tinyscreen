// Host-side stubs for the remaining firmware includes -- see Arduino.h
// stub for the rationale. All in one file, pulled in by tiny per-name
// header shims next to it.
#pragma once
#include "Arduino.h"
#include <map>
#include <vector>
#include <memory>

// ===== Wire.h ============================================================
struct StubWire {
  void begin(int = -1, int = -1) {}
  void beginTransmission(int) {}
  size_t write(uint8_t) { return 1; }
  int endTransmission(bool = true) { return 0; }
  int requestFrom(int, int n) { return n; }
  int read() { return 0; }
};
extern StubWire Wire;

// ===== Preferences.h =====================================================
class Preferences {
  std::map<std::string, std::string> str_;
  std::map<std::string, int> int_;
  std::map<std::string, bool> bool_;
 public:
  bool begin(const char *, bool = false) { return true; }
  void end() {}
  void clear() { str_.clear(); int_.clear(); bool_.clear(); }
  bool getBool(const char *k, bool d) { return bool_.count(k) ? bool_[k] : d; }
  int getInt(const char *k, int d) { return int_.count(k) ? int_[k] : d; }
  String getString(const char *k, const char *d) { return String(str_.count(k) ? str_[k].c_str() : d); }
  void putBool(const char *k, bool v) { bool_[k] = v; }
  void putInt(const char *k, int v) { int_[k] = v; }
  void putString(const char *k, const String &v) { str_[k] = v.s; }
};

// ===== Arduino_GFX_Library.h ============================================
class Arduino_DataBus {};
class Arduino_ESP32SPI : public Arduino_DataBus {
 public: Arduino_ESP32SPI(int, int, int, int, int) {}
};
class Arduino_GFX {
 public:
  virtual ~Arduino_GFX() {}
  bool begin(long = 0) { return true; }
  void fillScreen(uint16_t) {}
  void setTextColor(uint16_t) {}
  void setTextSize(int) {}
  void setCursor(int, int) {}
  template <typename T> void print(T) {}
  void getTextBounds(const char *t, int, int, int16_t *x1, int16_t *y1,
                     uint16_t *w, uint16_t *h) {
    *x1 = 0; *y1 = 0;
    *w = (uint16_t)(6 * 3 * strlen(t)); *h = 24;  // plausible fixed metrics
  }
  void fillArc(int, int, int, int, int, int, uint16_t) {}
  void fillCircle(int, int, int, uint16_t) {}
  void drawCircle(int, int, int, uint16_t) {}
  void fillRect(int, int, int, int, uint16_t) {}
  void drawRect(int, int, int, int, uint16_t) {}
  void drawLine(int, int, int, int, uint16_t) {}
  void drawFastHLine(int, int, int, uint16_t) {}
  void drawFastVLine(int, int, int, uint16_t) {}
  void flush() {}
};
class Arduino_GC9A01 : public Arduino_GFX {
 public:
  Arduino_GC9A01(Arduino_DataBus *, int, int, bool) {}
};
class Arduino_ST7789 : public Arduino_GFX {
 public:
  Arduino_ST7789(Arduino_DataBus *, int, int, bool, int, int, int, int, int, int) {}
};
class Arduino_Canvas : public Arduino_GFX {
 public:
  Arduino_Canvas(int, int, Arduino_GFX *) {}
};

// ===== ArduinoJson.h =====================================================
// A permissive JSON value proxy: supports the exact idioms main.cpp uses
// (doc["k"].is<T>(), doc["k"] | default, assignment, JsonArray add and
// range-for). Backed by a real recursive value store so tests could even
// drive handleLine() if wanted.
class JsonVariantStub;
class JsonArrayStub;

class JsonValue {
 public:
  enum Kind { NUL, BOOL, INT, FLOAT, STR, ARR } kind = NUL;
  bool b = false; long i = 0; double f = 0; std::string s;
  std::vector<std::shared_ptr<JsonValue>> arr;
  std::map<std::string, std::shared_ptr<JsonValue>> obj;
};

class JsonArrayStub {
 public:
  JsonValue *v = nullptr;
  JsonArrayStub() {}
  explicit JsonArrayStub(JsonValue *val) : v(val) { if (v) v->kind = JsonValue::ARR; }
  template <typename T> void add(T x);
  // range-for support
  struct iterator {
    std::vector<std::shared_ptr<JsonValue>>::iterator it;
    bool operator!=(const iterator &o) const { return it != o.it; }
    void operator++() { ++it; }
    JsonVariantStub operator*() const;
  };
  iterator begin();
  iterator end();
};

class JsonVariantStub {
 public:
  std::shared_ptr<JsonValue> v;
  JsonVariantStub() : v(new JsonValue()) {}
  explicit JsonVariantStub(std::shared_ptr<JsonValue> val) : v(val) {}

  template <typename T> bool is() const;
  template <typename T> T as() const;
  template <typename T> JsonArrayStub to();

  // assignment (serializer direction)
  void operator=(bool x) { v->kind = JsonValue::BOOL; v->b = x; }
  void operator=(int x) { v->kind = JsonValue::INT; v->i = x; }
  void operator=(long x) { v->kind = JsonValue::INT; v->i = x; }
  void operator=(double x) { v->kind = JsonValue::FLOAT; v->f = x; }
  void operator=(const char *x) { v->kind = JsonValue::STR; v->s = x ? x : ""; }
  void operator=(const String &x) { v->kind = JsonValue::STR; v->s = x.s; }

  // reads with ArduinoJson's `value | fallback` idiom
  operator int() const { return (int)(v->kind == JsonValue::INT ? v->i : (long)v->f); }
  operator const char *() const { return v->kind == JsonValue::STR ? v->s.c_str() : nullptr; }
  friend String operator|(const JsonVariantStub &a, const String &d) {
    return a.v->kind == JsonValue::STR ? String(a.v->s.c_str()) : d;
  }
  friend float operator|(const JsonVariantStub &a, float d) {
    if (a.v->kind == JsonValue::FLOAT) return (float)a.v->f;
    if (a.v->kind == JsonValue::INT) return (float)a.v->i;
    return d;
  }
  friend bool operator|(const JsonVariantStub &a, bool d) {
    return a.v->kind == JsonValue::BOOL ? a.v->b : d;
  }
  friend unsigned long operator|(const JsonVariantStub &a, unsigned long d) {
    return a.v->kind == JsonValue::INT ? (unsigned long)a.v->i : d;
  }
  operator JsonArrayStub() const { return JsonArrayStub(v.get()); }
};

template <> inline bool JsonVariantStub::is<bool>() const { return v->kind == JsonValue::BOOL; }
template <> inline bool JsonVariantStub::is<int>() const { return v->kind == JsonValue::INT; }
template <> inline bool JsonVariantStub::is<const char *>() const { return v->kind == JsonValue::STR; }
template <> inline bool JsonVariantStub::is<JsonArrayStub>() const { return v->kind == JsonValue::ARR; }
template <> inline const char *JsonVariantStub::as<const char *>() const {
  return v->kind == JsonValue::STR ? v->s.c_str() : nullptr;
}
template <> inline JsonArrayStub JsonVariantStub::to<JsonArrayStub>() { return JsonArrayStub(v.get()); }

template <typename T> void JsonArrayStub::add(T x) {
  auto nv = std::make_shared<JsonValue>();
  JsonVariantStub tmp(nv);
  tmp = x;
  v->arr.push_back(nv);
}
inline JsonArrayStub::iterator JsonArrayStub::begin() { return {v->arr.begin()}; }
inline JsonArrayStub::iterator JsonArrayStub::end() { return {v->arr.end()}; }
inline JsonVariantStub JsonArrayStub::iterator::operator*() const { return JsonVariantStub(*it); }

using JsonArray = JsonArrayStub;
using JsonVariant = JsonVariantStub;

class JsonDocument {
 public:
  std::shared_ptr<JsonValue> root = std::make_shared<JsonValue>();
  JsonVariantStub operator[](const char *k) {
    if (!root->obj.count(k)) root->obj[k] = std::make_shared<JsonValue>();
    return JsonVariantStub(root->obj[k]);
  }
};

struct DeserializationError {
  bool err;
  explicit operator bool() const { return err; }
};
inline DeserializationError deserializeJson(JsonDocument &, const String &) { return {true}; }
template <typename T> void serializeJson(const JsonDocument &, T &) {}
