#include "g729_wrapper.h"
#include "bcg729/encoder.h"
#include "bcg729/decoder.h"
#include <stdlib.h>

#define G729_FRAME_SAMPLES 80
#define G729_FRAME_BYTES   10

typedef struct {
    bcg729EncoderChannelContextStruct *encoder;
    bcg729DecoderChannelContextStruct *decoder;
} G729Context;

void *g729_create(void) {
    G729Context *ctx = calloc(1, sizeof(G729Context));
    if (!ctx) return NULL;
    ctx->encoder = initBcg729EncoderChannel(0); // VAD disabled
    ctx->decoder = initBcg729DecoderChannel();
    if (!ctx->encoder || !ctx->decoder) {
        if (ctx->encoder) closeBcg729EncoderChannel(ctx->encoder);
        if (ctx->decoder) closeBcg729DecoderChannel(ctx->decoder);
        free(ctx);
        return NULL;
    }
    return ctx;
}

void g729_destroy(void *handle) {
    G729Context *ctx = (G729Context *)handle;
    if (!ctx) return;
    if (ctx->encoder) closeBcg729EncoderChannel(ctx->encoder);
    if (ctx->decoder) closeBcg729DecoderChannel(ctx->decoder);
    free(ctx);
}

int g729_encode(void *handle, const int16_t *pcm80, uint8_t *bitstream10) {
    G729Context *ctx = (G729Context *)handle;
    if (!ctx || !ctx->encoder) return -1;
    uint8_t length = 0;
    bcg729Encoder(ctx->encoder, pcm80, bitstream10, &length);
    return (int)length;
}

int g729_decode(void *handle, const uint8_t *bitstream10, int16_t *pcm80) {
    G729Context *ctx = (G729Context *)handle;
    if (!ctx || !ctx->decoder) return -1;
    bcg729Decoder(ctx->decoder, bitstream10, G729_FRAME_BYTES, 0, 0, 0, pcm80);
    return G729_FRAME_SAMPLES;
}
