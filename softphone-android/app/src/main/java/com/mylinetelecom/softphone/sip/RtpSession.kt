package com.mylinetelecom.softphone.sip

import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.AudioTrack
import android.media.MediaRecorder
import android.util.Log
import kotlinx.coroutines.*
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.InetAddress

class RtpSession(
    private val localPort: Int,
    private val remoteHost: String,
    private val remotePort: Int,
    private val existingSocket: DatagramSocket? = null,
    private var codecType: Int = PCMU_PAYLOAD_TYPE // 0=PCMU, 3=GSM, 18=G729
) {
    companion object {
        private const val TAG = "RtpSession"
        private const val SAMPLE_RATE = 8000
        private const val FRAME_SIZE = 160 // 20ms at 8kHz = 160 samples
        private const val RTP_HEADER_SIZE = 12
        private const val DTMF_PAYLOAD_TYPE = 101
        const val PCMU_PAYLOAD_TYPE = 0
        const val GSM_PAYLOAD_TYPE = 3
        const val G729_PAYLOAD_TYPE = 18

        // GSM 06.10 Full Rate: 160 samples → 33 bytes (260 bits)
        private const val GSM_FRAME_SIZE = 33
        // G.729: 10ms = 80 samples → 10 bytes; we send 2 frames per 20ms packet = 20 bytes
        private const val G729_FRAME_SAMPLES = 80
        private const val G729_FRAME_BYTES = 10

        private val ULAW_EXPONENT_TABLE = intArrayOf(
            0, 0, 1, 1, 2, 2, 2, 2, 3, 3, 3, 3, 3, 3, 3, 3,
            4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4,
            5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5,
            5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5,
            6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6,
            6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6,
            6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6,
            6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6,
            7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7,
            7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7,
            7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7,
            7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7,
            7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7,
            7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7,
            7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7,
            7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7
        )

        private val ULAW_DECODE_TABLE = intArrayOf(
            0, 132, 396, 924, 1980, 4092, 8316, 16764
        )
    }

    private var socket: DatagramSocket? = null
    private var sendJob: Job? = null
    private var receiveJob: Job? = null
    private val scope = CoroutineScope(Dispatchers.IO + SupervisorJob())

    private var sequenceNumber = 0
    private var timestamp = 0L
    private var ssrc = (Math.random() * Int.MAX_VALUE).toInt()

    var isMuted = false
        private set

    @Volatile
    private var sendingDtmf = false

    private var audioRecord: AudioRecord? = null
    private var audioTrack: AudioTrack? = null

    // GSM codec (lazy init)
    private var gsmEncoder: GsmCodec? = null
    private var gsmDecoder: GsmCodec? = null

    // G.729 codec (native, lazy init)
    private var g729Codec: G729Codec? = null

    // Local DTMF tone playback
    private var dtmfToneTrack: AudioTrack? = null

    // DTMF frequency pairs (ITU-T standard)
    private val dtmfFrequencies = mapOf(
        '1' to Pair(697, 1209), '2' to Pair(697, 1336), '3' to Pair(697, 1477),
        '4' to Pair(770, 1209), '5' to Pair(770, 1336), '6' to Pair(770, 1477),
        '7' to Pair(852, 1209), '8' to Pair(852, 1336), '9' to Pair(852, 1477),
        '*' to Pair(941, 1209), '0' to Pair(941, 1336), '#' to Pair(941, 1477)
    )

    fun start() {
        try {
            socket = existingSocket ?: DatagramSocket(localPort)

            // Initialize codec if needed
            when (codecType) {
                GSM_PAYLOAD_TYPE -> {
                    gsmEncoder = GsmCodec()
                    gsmDecoder = GsmCodec()
                }
                G729_PAYLOAD_TYPE -> {
                    if (G729Codec.isAvailable()) {
                        g729Codec = G729Codec().also {
                            if (!it.open()) {
                                Log.w(TAG, "G.729 init failed, falling back to PCMU")
                                g729Codec = null
                                codecType = PCMU_PAYLOAD_TYPE
                            }
                        }
                    } else {
                        Log.w(TAG, "G.729 native library not available, falling back to PCMU")
                        codecType = PCMU_PAYLOAD_TYPE
                    }
                }
            }

            initAudio()
            startSending()
            startReceiving()
            val codecName = when (codecType) {
                GSM_PAYLOAD_TYPE -> "GSM"
                G729_PAYLOAD_TYPE -> if (g729Codec != null) "G.729" else "PCMU(G.729 unavail)"
                else -> "PCMU"
            }
            Log.i(TAG, "RTP started on port $localPort -> $remoteHost:$remotePort codec=$codecName")
        } catch (e: Exception) {
            Log.e(TAG, "Failed to start RTP", e)
        }
    }

    fun stop() {
        sendJob?.cancel()
        receiveJob?.cancel()
        scope.cancel()

        try {
            audioRecord?.stop()
            audioRecord?.release()
        } catch (_: Exception) { }

        try {
            audioTrack?.stop()
            audioTrack?.release()
        } catch (_: Exception) { }

        g729Codec?.close()
        g729Codec = null

        try {
            dtmfToneTrack?.stop()
            dtmfToneTrack?.release()
        } catch (_: Exception) { }
        dtmfToneTrack = null

        socket?.close()
        Log.i(TAG, "RTP stopped")
    }

    fun mute(muted: Boolean) {
        isMuted = muted
    }

    fun sendDtmf(digit: Char) {
        scope.launch {
            val event = when (digit) {
                '0' -> 0; '1' -> 1; '2' -> 2; '3' -> 3; '4' -> 4
                '5' -> 5; '6' -> 6; '7' -> 7; '8' -> 8; '9' -> 9
                '*' -> 10; '#' -> 11
                else -> return@launch
            }

            Log.i(TAG, "Sending DTMF digit: $digit (event=$event)")

            // Play local tone so user hears feedback
            playLocalDtmfTone(digit)

            val remoteAddr = InetAddress.getByName(remoteHost)

            // Pause audio sending during DTMF to avoid sequence/timestamp conflicts
            sendingDtmf = true

            // RFC 2833: All packets for one DTMF event share the same timestamp
            val dtmfTimestamp = timestamp

            // Send start + continuation packets (duration increases each time)
            // Total tone duration ~160ms (8 packets × 20ms)
            val totalPackets = 8
            for (i in 0 until totalPackets) {
                val duration = FRAME_SIZE * (i + 1) // 160, 320, 480... samples
                val payload = byteArrayOf(
                    event.toByte(),
                    10, // volume = 10
                    (duration shr 8).toByte(),
                    (duration and 0xFF).toByte()
                )
                val savedTimestamp = timestamp
                timestamp = dtmfTimestamp // Use fixed timestamp for DTMF event
                val packet = buildRtpPacket(DTMF_PAYLOAD_TYPE, payload, marker = i == 0)
                timestamp = savedTimestamp
                socket?.send(DatagramPacket(packet, packet.size, remoteAddr, remotePort))
                sequenceNumber++
                delay(20)
            }

            // Send 3 end packets (with End flag set, same final duration)
            val finalDuration = FRAME_SIZE * totalPackets
            repeat(3) {
                val payload = byteArrayOf(
                    event.toByte(),
                    (0x80 or 10).toByte(), // end flag + volume
                    (finalDuration shr 8).toByte(),
                    (finalDuration and 0xFF).toByte()
                )
                val savedTimestamp = timestamp
                timestamp = dtmfTimestamp
                val packet = buildRtpPacket(DTMF_PAYLOAD_TYPE, payload)
                timestamp = savedTimestamp
                socket?.send(DatagramPacket(packet, packet.size, remoteAddr, remotePort))
                sequenceNumber++
                delay(20)
            }

            timestamp += finalDuration.toLong()
            sendingDtmf = false
            Log.i(TAG, "DTMF digit $digit sent")
        }
    }

    private fun initAudio() {
        val minBufSize = AudioRecord.getMinBufferSize(
            SAMPLE_RATE,
            AudioFormat.CHANNEL_IN_MONO,
            AudioFormat.ENCODING_PCM_16BIT
        )
        val bufSize = maxOf(minBufSize, FRAME_SIZE * 2 * 4)

        audioRecord = AudioRecord(
            MediaRecorder.AudioSource.VOICE_COMMUNICATION,
            SAMPLE_RATE,
            AudioFormat.CHANNEL_IN_MONO,
            AudioFormat.ENCODING_PCM_16BIT,
            bufSize
        )

        audioTrack = AudioTrack.Builder()
            .setAudioAttributes(
                AudioAttributes.Builder()
                    .setUsage(AudioAttributes.USAGE_VOICE_COMMUNICATION)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                    .build()
            )
            .setAudioFormat(
                AudioFormat.Builder()
                    .setSampleRate(SAMPLE_RATE)
                    .setChannelMask(AudioFormat.CHANNEL_OUT_MONO)
                    .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                    .build()
            )
            .setBufferSizeInBytes(bufSize)
            .setTransferMode(AudioTrack.MODE_STREAM)
            .build()

        audioRecord?.startRecording()
        audioTrack?.play()
    }

    private fun startSending() {
        sendJob = scope.launch {
            val pcmBuffer = ShortArray(FRAME_SIZE)
            val remoteAddr = InetAddress.getByName(remoteHost)

            while (isActive) {
                try {
                    val read = audioRecord?.read(pcmBuffer, 0, FRAME_SIZE) ?: 0
                    if (read > 0 && !sendingDtmf) {
                        if (isMuted) {
                            // When muted, don't send any RTP — true silence
                            sequenceNumber++
                            timestamp += FRAME_SIZE
                        } else when {
                            codecType == G729_PAYLOAD_TYPE && g729Codec != null -> {
                                // G.729: 160 samples = 2 × 80-sample frames → 2 × 10 bytes = 20 bytes
                                val frame1 = g729Codec!!.encode(pcmBuffer, 0) ?: ByteArray(G729_FRAME_BYTES)
                                val frame2 = g729Codec!!.encode(pcmBuffer, G729_FRAME_SAMPLES) ?: ByteArray(G729_FRAME_BYTES)
                                val payload = frame1 + frame2
                                val packet = buildRtpPacket(G729_PAYLOAD_TYPE, payload)
                                socket?.send(DatagramPacket(packet, packet.size, remoteAddr, remotePort))
                                sequenceNumber++
                                timestamp += FRAME_SIZE
                            }
                            codecType == GSM_PAYLOAD_TYPE -> {
                                // GSM encode: 160 samples → 33 bytes
                                val payload = gsmEncoder?.encode(pcmBuffer, read) ?: ByteArray(GSM_FRAME_SIZE)
                                val packet = buildRtpPacket(GSM_PAYLOAD_TYPE, payload)
                                socket?.send(DatagramPacket(packet, packet.size, remoteAddr, remotePort))
                                sequenceNumber++
                                timestamp += FRAME_SIZE
                            }
                            else -> {
                                // PCMU encode: 160 samples → 160 bytes
                                val payload = encodeUlaw(pcmBuffer, read)
                                val packet = buildRtpPacket(PCMU_PAYLOAD_TYPE, payload)
                                socket?.send(DatagramPacket(packet, packet.size, remoteAddr, remotePort))
                                sequenceNumber++
                                timestamp += FRAME_SIZE
                            }
                        }
                    }
                } catch (e: Exception) {
                    if (isActive) Log.e(TAG, "Send error", e)
                }
            }
        }
    }

    private fun startReceiving() {
        receiveJob = scope.launch {
            val buffer = ByteArray(2048)

            while (isActive) {
                try {
                    val packet = DatagramPacket(buffer, buffer.size)
                    socket?.receive(packet)

                    if (packet.length > RTP_HEADER_SIZE) {
                        val payloadType = (buffer[1].toInt() and 0x7F)
                        val payloadOffset = RTP_HEADER_SIZE
                        val payloadLength = packet.length - payloadOffset

                        when (payloadType) {
                            PCMU_PAYLOAD_TYPE -> {
                                val pcm = decodeUlaw(buffer, payloadOffset, payloadLength)
                                audioTrack?.write(pcm, 0, pcm.size)
                            }
                            GSM_PAYLOAD_TYPE -> {
                                if (payloadLength >= GSM_FRAME_SIZE) {
                                    val gsmData = ByteArray(GSM_FRAME_SIZE)
                                    System.arraycopy(buffer, payloadOffset, gsmData, 0, GSM_FRAME_SIZE)
                                    val pcm = gsmDecoder?.decode(gsmData) ?: ShortArray(FRAME_SIZE)
                                    audioTrack?.write(pcm, 0, pcm.size)
                                }
                            }
                            G729_PAYLOAD_TYPE -> {
                                // G.729: may contain 1 or 2 frames (10 or 20 bytes)
                                val g729 = g729Codec
                                if (g729 != null && payloadLength >= G729_FRAME_BYTES) {
                                    val numFrames = payloadLength / G729_FRAME_BYTES
                                    for (f in 0 until numFrames) {
                                        val frameData = ByteArray(G729_FRAME_BYTES)
                                        System.arraycopy(buffer, payloadOffset + f * G729_FRAME_BYTES, frameData, 0, G729_FRAME_BYTES)
                                        val pcm = g729.decode(frameData)
                                        if (pcm != null) {
                                            audioTrack?.write(pcm, 0, pcm.size)
                                        }
                                    }
                                }
                            }
                            // DTMF and other payload types are ignored on receive
                        }
                    }
                } catch (e: Exception) {
                    if (isActive) Log.e(TAG, "Receive error", e)
                }
            }
        }
    }

    private fun buildRtpPacket(payloadType: Int, payload: ByteArray, marker: Boolean = false): ByteArray {
        val packet = ByteArray(RTP_HEADER_SIZE + payload.size)

        // Version 2, no padding, no extension, no CSRC
        packet[0] = 0x80.toByte()

        // Marker + payload type
        packet[1] = ((if (marker) 0x80 else 0) or (payloadType and 0x7F)).toByte()

        // Sequence number (big-endian)
        packet[2] = (sequenceNumber shr 8).toByte()
        packet[3] = (sequenceNumber and 0xFF).toByte()

        // Timestamp (big-endian)
        packet[4] = (timestamp shr 24).toByte()
        packet[5] = (timestamp shr 16).toByte()
        packet[6] = (timestamp shr 8).toByte()
        packet[7] = (timestamp and 0xFF).toByte()

        // SSRC (big-endian)
        packet[8] = (ssrc shr 24).toByte()
        packet[9] = (ssrc shr 16).toByte()
        packet[10] = (ssrc shr 8).toByte()
        packet[11] = (ssrc and 0xFF).toByte()

        // Payload
        System.arraycopy(payload, 0, packet, RTP_HEADER_SIZE, payload.size)

        return packet
    }

    // ==================== Local DTMF tone ====================

    private fun playLocalDtmfTone(digit: Char) {
        try {
            // Stop any previous tone
            dtmfToneTrack?.let {
                try { it.stop(); it.release() } catch (_: Exception) { }
            }

            val freqs = dtmfFrequencies[digit] ?: return
            val toneSampleRate = 8000
            val durationMs = 150 // 150ms tone
            val numSamples = toneSampleRate * durationMs / 1000
            val buffer = ShortArray(numSamples)

            for (i in 0 until numSamples) {
                val t = i.toDouble() / toneSampleRate
                val sample = (kotlin.math.sin(2.0 * Math.PI * freqs.first * t) +
                        kotlin.math.sin(2.0 * Math.PI * freqs.second * t)) * 4000
                buffer[i] = sample.toInt().coerceIn(-32768, 32767).toShort()
            }

            val track = AudioTrack.Builder()
                .setAudioAttributes(
                    AudioAttributes.Builder()
                        .setUsage(AudioAttributes.USAGE_VOICE_COMMUNICATION)
                        .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                        .build()
                )
                .setAudioFormat(
                    AudioFormat.Builder()
                        .setSampleRate(toneSampleRate)
                        .setChannelMask(AudioFormat.CHANNEL_OUT_MONO)
                        .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                        .build()
                )
                .setBufferSizeInBytes(buffer.size * 2)
                .setTransferMode(AudioTrack.MODE_STATIC)
                .build()

            track.write(buffer, 0, buffer.size)
            track.play()
            dtmfToneTrack = track
        } catch (e: Exception) {
            Log.e(TAG, "Error playing DTMF tone", e)
        }
    }

    // ==================== G.711 u-law codec ====================

    private fun encodeUlaw(pcm: ShortArray, length: Int): ByteArray {
        val ulaw = ByteArray(length)
        for (i in 0 until length) {
            ulaw[i] = linearToUlaw(pcm[i].toInt())
        }
        return ulaw
    }

    private fun decodeUlaw(data: ByteArray, offset: Int, length: Int): ShortArray {
        val pcm = ShortArray(length)
        for (i in 0 until length) {
            pcm[i] = ulawToLinear(data[offset + i])
        }
        return pcm
    }

    private fun linearToUlaw(sample: Int): Byte {
        val BIAS = 0x84
        val CLIP = 32635

        var pcmVal = sample
        val sign = (pcmVal shr 8) and 0x80
        if (sign != 0) pcmVal = -pcmVal
        if (pcmVal > CLIP) pcmVal = CLIP
        pcmVal += BIAS

        val exponent = ULAW_EXPONENT_TABLE[(pcmVal shr 7) and 0xFF]
        val mantissa = (pcmVal shr (exponent + 3)) and 0x0F

        val ulawByte = (sign or (exponent shl 4) or mantissa).inv()
        return ulawByte.toByte()
    }

    private fun ulawToLinear(ulawByte: Byte): Short {
        var mulaw = ulawByte.toInt().inv() and 0xFF
        val sign = mulaw and 0x80
        val exponent = (mulaw shr 4) and 0x07
        var mantissa = mulaw and 0x0F

        var sample = ULAW_DECODE_TABLE[exponent] + (mantissa shl (exponent + 3))
        if (sign != 0) sample = -sample

        return sample.toShort()
    }
}
