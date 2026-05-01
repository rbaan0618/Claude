#pragma once
#include <stdint.h>

void *g729_create(void);
void  g729_destroy(void *ctx);
int   g729_encode(void *ctx, const int16_t *pcm80, uint8_t *bitstream10);
int   g729_decode(void *ctx, const uint8_t *bitstream10, int16_t *pcm80);
