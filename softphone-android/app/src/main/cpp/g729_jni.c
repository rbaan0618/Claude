/*
 * JNI wrapper for bcg729 G.729A codec
 *
 * Provides encode/decode functions callable from Kotlin/Java.
 * Links against the bcg729 open-source library.
 */

#include <jni.h>
#include <stdlib.h>
#include <string.h>
#include <android/log.h>

#define LOG_TAG "G729JNI"
#define LOGI(...) __android_log_print(ANDROID_LOG_INFO, LOG_TAG, __VA_ARGS__)
#define LOGE(...) __android_log_print(ANDROID_LOG_ERROR, LOG_TAG, __VA_ARGS__)

/* Include the real bcg729 headers (flat in bcg729/ dir) */
#include "encoder.h"
#include "decoder.h"

#define G729_FRAME_SAMPLES 80   /* 10ms at 8kHz */
#define G729_FRAME_BYTES   10   /* Compressed frame size */

/*
 * Native context holder - stores encoder and decoder state
 */
typedef struct {
    bcg729EncoderChannelContextStruct *encoder;
    bcg729DecoderChannelContextStruct *decoder;
} G729Context;


/* ==================== JNI Functions ==================== */

/*
 * Create a new G.729 codec context (encoder + decoder)
 * Returns a native pointer as a long
 */
JNIEXPORT jlong JNICALL
Java_com_mylinetelecom_softphone_sip_G729Codec_nativeCreate(JNIEnv *env, jobject thiz) {
    G729Context *ctx = (G729Context *)calloc(1, sizeof(G729Context));
    if (!ctx) {
        LOGE("Failed to allocate G729Context");
        return 0;
    }

    /* enableVAD = 0: disable Voice Activity Detection / DTX */
    ctx->encoder = initBcg729EncoderChannel(0);
    ctx->decoder = initBcg729DecoderChannel();

    if (!ctx->encoder || !ctx->decoder) {
        LOGE("Failed to initialize bcg729 encoder/decoder");
        if (ctx->encoder) closeBcg729EncoderChannel(ctx->encoder);
        if (ctx->decoder) closeBcg729DecoderChannel(ctx->decoder);
        free(ctx);
        return 0;
    }

    LOGI("G.729 codec initialized");
    return (jlong)(intptr_t)ctx;
}

/*
 * Destroy the codec context
 */
JNIEXPORT void JNICALL
Java_com_mylinetelecom_softphone_sip_G729Codec_nativeDestroy(JNIEnv *env, jobject thiz, jlong handle) {
    G729Context *ctx = (G729Context *)(intptr_t)handle;
    if (!ctx) return;

    if (ctx->encoder) closeBcg729EncoderChannel(ctx->encoder);
    if (ctx->decoder) closeBcg729DecoderChannel(ctx->decoder);
    free(ctx);
    LOGI("G.729 codec destroyed");
}

/*
 * Encode 80 PCM samples (10ms) into 10 bytes
 *
 * @param handle  Native context pointer
 * @param pcm     Input: 80 int16 samples (passed as short[])
 * @param encoded Output: 10 bytes (passed as byte[])
 * @return number of bytes written (10) or -1 on error
 */
JNIEXPORT jint JNICALL
Java_com_mylinetelecom_softphone_sip_G729Codec_nativeEncode(
        JNIEnv *env, jobject thiz, jlong handle,
        jshortArray pcm, jbyteArray encoded) {

    G729Context *ctx = (G729Context *)(intptr_t)handle;
    if (!ctx || !ctx->encoder) return -1;

    jshort *pcmBuf = (*env)->GetShortArrayElements(env, pcm, NULL);
    jbyte *encBuf = (*env)->GetByteArrayElements(env, encoded, NULL);

    if (!pcmBuf || !encBuf) {
        if (pcmBuf) (*env)->ReleaseShortArrayElements(env, pcm, pcmBuf, 0);
        if (encBuf) (*env)->ReleaseByteArrayElements(env, encoded, encBuf, 0);
        return -1;
    }

    /* bcg729Encoder now requires a bitStreamLength output parameter */
    uint8_t bitStreamLength = 0;
    bcg729Encoder(ctx->encoder, (const int16_t *)pcmBuf, (uint8_t *)encBuf, &bitStreamLength);

    (*env)->ReleaseShortArrayElements(env, pcm, pcmBuf, JNI_ABORT);
    (*env)->ReleaseByteArrayElements(env, encoded, encBuf, 0);

    return (jint)bitStreamLength;
}

/*
 * Decode 10 bytes into 80 PCM samples (10ms)
 *
 * @param handle  Native context pointer
 * @param encoded Input: 10 bytes
 * @param pcm     Output: 80 int16 samples
 * @return number of samples written (80) or -1 on error
 */
JNIEXPORT jint JNICALL
Java_com_mylinetelecom_softphone_sip_G729Codec_nativeDecode(
        JNIEnv *env, jobject thiz, jlong handle,
        jbyteArray encoded, jshortArray pcm) {

    G729Context *ctx = (G729Context *)(intptr_t)handle;
    if (!ctx || !ctx->decoder) return -1;

    jbyte *encBuf = (*env)->GetByteArrayElements(env, encoded, NULL);
    jshort *pcmBuf = (*env)->GetShortArrayElements(env, pcm, NULL);

    if (!encBuf || !pcmBuf) {
        if (encBuf) (*env)->ReleaseByteArrayElements(env, encoded, encBuf, 0);
        if (pcmBuf) (*env)->ReleaseShortArrayElements(env, pcm, pcmBuf, 0);
        return -1;
    }

    /* bitStreamLength=10, frameErasureFlag=0 (good frame),
       SIDFrameFlag=0 (not SID), rfc3389PayloadFlag=0 */
    bcg729Decoder(ctx->decoder, (const uint8_t *)encBuf, G729_FRAME_BYTES,
                  0, 0, 0, (int16_t *)pcmBuf);

    (*env)->ReleaseByteArrayElements(env, encoded, encBuf, JNI_ABORT);
    (*env)->ReleaseShortArrayElements(env, pcm, pcmBuf, 0);

    return G729_FRAME_SAMPLES;
}
