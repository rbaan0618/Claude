package com.mylinetelecom.softphone.sip

import android.util.Log

/**
 * G.729A codec wrapper using native bcg729 library via JNI.
 *
 * G.729A encodes 10ms frames (80 samples at 8kHz) into 10 bytes (8 kbps).
 * This provides excellent compression for voice over narrow bandwidth.
 *
 * Setup: Run setup_g729.bat (Windows) or setup_g729.sh (Linux/Mac) before building
 * to download the bcg729 open-source library.
 */
class G729Codec {
    companion object {
        private const val TAG = "G729Codec"
        const val FRAME_SAMPLES = 80   // 10ms at 8kHz
        const val FRAME_BYTES = 10     // Compressed frame size
        const val PAYLOAD_TYPE = 18    // RTP payload type for G.729

        private var libraryLoaded = false
        private var codecFunctional = false

        init {
            try {
                System.loadLibrary("g729codec")
                libraryLoaded = true
                Log.i(TAG, "G.729 native library loaded")

                // Test that the codec actually works (not just stubs)
                val testCodec = G729Codec()
                if (testCodec.openInternal()) {
                    codecFunctional = true
                    testCodec.close()
                    Log.i(TAG, "G.729 codec verified functional")
                } else {
                    Log.w(TAG, "G.729 library loaded but codec init failed (stub only)")
                }
            } catch (e: UnsatisfiedLinkError) {
                Log.w(TAG, "G.729 native library not available: ${e.message}")
                libraryLoaded = false
            } catch (e: Exception) {
                Log.w(TAG, "G.729 codec test failed: ${e.message}")
            }
        }

        fun isAvailable(): Boolean = codecFunctional
    }

    private var nativeHandle: Long = 0

    /** Internal open used by availability check — no logging noise */
    internal fun openInternal(): Boolean {
        if (!libraryLoaded) return false
        nativeHandle = nativeCreate()
        return nativeHandle != 0L
    }

    fun open(): Boolean {
        if (!codecFunctional) {
            Log.w(TAG, "Cannot open G.729 - codec not functional")
            return false
        }
        nativeHandle = nativeCreate()
        if (nativeHandle == 0L) {
            Log.e(TAG, "Failed to create G.729 context")
            return false
        }
        return true
    }

    fun close() {
        if (nativeHandle != 0L) {
            nativeDestroy(nativeHandle)
            nativeHandle = 0
        }
    }

    /**
     * Encode 80 PCM samples (10ms) into 10 bytes.
     * For 20ms frames (160 samples), call twice with each half.
     */
    fun encode(pcm: ShortArray, offset: Int = 0): ByteArray? {
        if (nativeHandle == 0L) return null
        val frame = if (offset == 0 && pcm.size == FRAME_SAMPLES) {
            pcm
        } else {
            ShortArray(FRAME_SAMPLES).also {
                val len = minOf(FRAME_SAMPLES, pcm.size - offset)
                System.arraycopy(pcm, offset, it, 0, len)
            }
        }
        val encoded = ByteArray(FRAME_BYTES)
        val result = nativeEncode(nativeHandle, frame, encoded)
        return if (result == FRAME_BYTES) encoded else null
    }

    /**
     * Decode 10 bytes into 80 PCM samples (10ms).
     */
    fun decode(data: ByteArray, offset: Int = 0): ShortArray? {
        if (nativeHandle == 0L) return null
        val frame = if (offset == 0 && data.size == FRAME_BYTES) {
            data
        } else {
            ByteArray(FRAME_BYTES).also {
                val len = minOf(FRAME_BYTES, data.size - offset)
                System.arraycopy(data, offset, it, 0, len)
            }
        }
        val pcm = ShortArray(FRAME_SAMPLES)
        val result = nativeDecode(nativeHandle, frame, pcm)
        return if (result == FRAME_SAMPLES) pcm else null
    }

    // JNI native methods
    private external fun nativeCreate(): Long
    private external fun nativeDestroy(handle: Long)
    private external fun nativeEncode(handle: Long, pcm: ShortArray, encoded: ByteArray): Int
    private external fun nativeDecode(handle: Long, encoded: ByteArray, pcm: ShortArray): Int
}
