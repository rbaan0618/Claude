package com.mylinetelecom.softphone.sip

import android.util.Log
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.InetAddress
import java.nio.ByteBuffer

object StunClient {
    private const val TAG = "StunClient"
    private const val STUN_PORT = 3478
    private const val TIMEOUT_MS = 2000
    private const val BINDING_REQUEST = 0x0001
    private const val BINDING_RESPONSE = 0x0101
    private const val MAPPED_ADDRESS = 0x0001
    private const val XOR_MAPPED_ADDRESS = 0x0020
    private const val MAGIC_COOKIE = 0x2112A442.toInt()

    private val STUN_SERVERS = listOf(
        "stun.l.google.com",
        "stun1.l.google.com",
        "stun2.l.google.com",
        "stun.ekiga.net"
    )

    fun discover(socket: DatagramSocket): Pair<String, Int>? {
        val oldTimeout = socket.soTimeout

        for (server in STUN_SERVERS) {
            try {
                socket.soTimeout = TIMEOUT_MS

                val request = buildBindingRequest()
                val serverAddr = InetAddress.getByName(server)
                val sendPacket = DatagramPacket(request, request.size, serverAddr, STUN_PORT)
                socket.send(sendPacket)

                val buffer = ByteArray(512)
                val receivePacket = DatagramPacket(buffer, buffer.size)
                socket.receive(receivePacket)

                val result = parseBindingResponse(buffer, receivePacket.length)
                if (result != null) {
                    Log.i(TAG, "STUN result from $server: ${result.first}:${result.second}")
                    socket.soTimeout = oldTimeout
                    return result
                }
            } catch (e: Exception) {
                Log.w(TAG, "STUN server $server failed: ${e.message}")
            }
        }

        socket.soTimeout = oldTimeout
        return null
    }

    private fun buildBindingRequest(): ByteArray {
        val transactionId = ByteArray(12)
        java.security.SecureRandom().nextBytes(transactionId)

        val buffer = ByteBuffer.allocate(20)
        buffer.putShort(BINDING_REQUEST.toShort()) // Message type
        buffer.putShort(0) // Message length
        buffer.putInt(MAGIC_COOKIE) // Magic cookie
        buffer.put(transactionId) // Transaction ID

        return buffer.array()
    }

    private fun parseBindingResponse(data: ByteArray, length: Int): Pair<String, Int>? {
        if (length < 20) return null

        val buffer = ByteBuffer.wrap(data, 0, length)
        val messageType = buffer.short.toInt() and 0xFFFF
        val messageLength = buffer.short.toInt() and 0xFFFF
        val magicCookie = buffer.int
        val transactionId = ByteArray(12)
        buffer.get(transactionId)

        if (messageType != BINDING_RESPONSE) return null

        var result: Pair<String, Int>? = null
        var remaining = messageLength

        while (remaining > 4 && buffer.remaining() >= 4) {
            val attrType = buffer.short.toInt() and 0xFFFF
            val attrLength = buffer.short.toInt() and 0xFFFF
            remaining -= 4

            if (buffer.remaining() < attrLength) break

            when (attrType) {
                XOR_MAPPED_ADDRESS -> {
                    result = parseXorMappedAddress(buffer, attrLength, magicCookie)
                }
                MAPPED_ADDRESS -> {
                    if (result == null) {
                        result = parseMappedAddress(buffer, attrLength)
                    } else {
                        // Skip
                        val skip = ByteArray(attrLength)
                        buffer.get(skip)
                    }
                }
                else -> {
                    val skip = ByteArray(attrLength)
                    buffer.get(skip)
                }
            }

            // Padding to 4-byte boundary
            val padding = (4 - (attrLength % 4)) % 4
            remaining -= attrLength + padding
            if (padding > 0 && buffer.remaining() >= padding) {
                val pad = ByteArray(padding)
                buffer.get(pad)
            }
        }

        return result
    }

    private fun parseXorMappedAddress(buffer: ByteBuffer, length: Int, magicCookie: Int): Pair<String, Int>? {
        if (length < 8) {
            val skip = ByteArray(length)
            buffer.get(skip)
            return null
        }

        buffer.get() // reserved
        val family = buffer.get().toInt() and 0xFF
        val xorPort = buffer.short.toInt() and 0xFFFF
        val port = xorPort xor ((magicCookie shr 16) and 0xFFFF)

        if (family == 0x01) { // IPv4
            val xorAddr = buffer.int
            val addr = xorAddr xor magicCookie
            val ip = "${(addr shr 24) and 0xFF}.${(addr shr 16) and 0xFF}.${(addr shr 8) and 0xFF}.${addr and 0xFF}"
            return Pair(ip, port)
        }

        return null
    }

    private fun parseMappedAddress(buffer: ByteBuffer, length: Int): Pair<String, Int>? {
        if (length < 8) {
            val skip = ByteArray(length)
            buffer.get(skip)
            return null
        }

        buffer.get() // reserved
        val family = buffer.get().toInt() and 0xFF
        val port = buffer.short.toInt() and 0xFFFF

        if (family == 0x01) { // IPv4
            val addr = buffer.int
            val ip = "${(addr shr 24) and 0xFF}.${(addr shr 16) and 0xFF}.${(addr shr 8) and 0xFF}.${addr and 0xFF}"
            return Pair(ip, port)
        }

        return null
    }
}
