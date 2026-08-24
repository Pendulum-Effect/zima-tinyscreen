#pragma once
#include <cstddef>
#include <cstdint>
inline int mbedtls_base64_decode(unsigned char *, size_t, size_t *olen,
                                 const unsigned char *, size_t) {
  *olen = 0; return -1;  // host stub: never decodes
}
