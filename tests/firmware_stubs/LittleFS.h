#pragma once
// Host stub for LittleFS: enough surface for the custom-logo code to
// compile. No actual storage -- logo paths are exercised on hardware.
#include <cstddef>
#include <cstdint>
struct File {
  bool ok = false;
  size_t read(uint8_t *, size_t) { return 0; }
  size_t write(const uint8_t *, size_t n) { return n; }
  size_t size() { return 0; }
  void close() {}
  operator bool() const { return ok; }
};
struct LittleFSStub {
  bool begin(bool, const char *, int, const char *) { return false; }
  bool exists(const char *) { return false; }
  File open(const char *, const char *) { return File(); }
  bool remove(const char *) { return true; }
  bool rename(const char *, const char *) { return false; }
};
inline LittleFSStub LittleFS;
