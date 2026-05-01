package com.mylinetelecom.softphone.sip

/**
 * GSM 06.10 Full Rate codec (RPE-LTP)
 * Encodes 160 PCM 16-bit samples (20ms at 8kHz) into 33 bytes (260 bits).
 *
 * This is a pure Kotlin implementation based on the public domain
 * Toast (GSM 06.10) reference implementation by Jutta Degener
 * and Carsten Bormann, Technische Universität Berlin.
 */
class GsmCodec {

    // Encoder state
    private val dp0 = ShortArray(280)
    private var z1 = 0
    private var L_z2 = 0L
    private var mp = 0
    private val u = IntArray(8)
    private val LARpp = Array(2) { ShortArray(8) }
    private var j = 0 // LARpp toggle

    // Decoder state
    private var msr = 0
    private val dLARpp = Array(2) { ShortArray(8) }
    private var dj = 0
    private val v = ShortArray(9)
    private val nrp = intArrayOf(40)

    fun encode(pcmIn: ShortArray, length: Int): ByteArray {
        val s = ShortArray(160)
        for (i in 0 until minOf(length, 160)) {
            s[i] = pcmIn[i]
        }

        val LARc = IntArray(8)
        val Nc = IntArray(4)
        val bc = IntArray(4)
        val Mc = IntArray(4)
        val xmaxc = IntArray(4)
        val xMc = Array(4) { IntArray(13) }

        // Pre-processing
        preprocess(s)

        // LPC analysis
        lpcAnalysis(s, LARc)

        // Short-term analysis filtering + sub-frame processing
        val oldLARpp = LARpp[j]
        j = j xor 1
        val newLARpp = LARpp[j]
        decodeLAR(LARc, newLARpp)

        val so = ShortArray(160)
        shortTermAnalysis(oldLARpp, newLARpp, s, so)

        for (k in 0 until 4) {
            val d = ShortArray(40)
            for (i in 0 until 40) d[i] = so[k * 40 + i]

            val result = longTermAnalysis(d, k)
            Nc[k] = result[0]
            bc[k] = result[1]

            val dpp = ShortArray(40)
            longTermSynthesis(Nc[k], bc[k], d, dpp)

            val e = ShortArray(40)
            for (i in 0 until 40) e[i] = (d[i] - dpp[i]).toShort()

            val rpeResult = rpeEncoding(e)
            Mc[k] = rpeResult[0]
            xmaxc[k] = rpeResult[1]
            for (i in 0 until 13) xMc[k][i] = rpeResult[2 + i]

            // Update dp0 buffer
            val rpeDecoded = rpeDecode(Mc[k], xmaxc[k], xMc[k])
            for (i in 0 until 40) {
                dp0[120 + i] = (dpp[i] + rpeDecoded[i]).toShort()
            }
            System.arraycopy(dp0, 40, dp0, 0, 240)
        }

        // Pack into 33 bytes
        return packFrame(LARc, Nc, bc, Mc, xmaxc, xMc)
    }

    fun decode(gsmData: ByteArray): ShortArray {
        val LARc = IntArray(8)
        val Nc = IntArray(4)
        val bc = IntArray(4)
        val Mc = IntArray(4)
        val xmaxc = IntArray(4)
        val xMc = Array(4) { IntArray(13) }

        unpackFrame(gsmData, LARc, Nc, bc, Mc, xmaxc, xMc)

        val oldLARpp = dLARpp[dj]
        dj = dj xor 1
        val newLARpp = dLARpp[dj]
        decodeLAR(LARc, newLARpp)

        val sr = ShortArray(160)

        for (k in 0 until 4) {
            val erp = rpeDecode(Mc[k], xmaxc[k], xMc[k])

            val wt = ShortArray(40)
            longTermSynthesisDecode(Nc[k], bc[k], erp, wt)

            val interpolated = ShortArray(8)
            for (i in 0 until 8) {
                val f = k * 13 + i  // subframe interpolation factor
                interpolated[i] = when {
                    k == 0 && f < 13 -> ((oldLARpp[i] * (13 - f) + newLARpp[i] * f) / 13).toShort()
                    k == 0 -> newLARpp[i]
                    k == 1 -> ((oldLARpp[i] + newLARpp[i] * 3) / 4).toShort()
                    k == 2 -> ((oldLARpp[i] + newLARpp[i]) / 2).toShort()
                    else -> newLARpp[i]
                }
            }

            shortTermSynthesisFilter(interpolated, wt)
            postProcess(wt)

            for (i in 0 until 40) sr[k * 40 + i] = wt[i]
        }

        return sr
    }

    // ============ Preprocessing ============

    private fun preprocess(s: ShortArray) {
        for (i in 0 until 160) {
            val so = s[i].toLong() shl 1 // offset compensation simplified
            // Pre-emphasis: s'(i) = s(i) - z1 * 28180/32768
            val sof = so - ((z1.toLong() * 28180L) shr 15)
            z1 = so.toInt()
            // De-emphasis using L_z2
            val tmp = sof + L_z2
            L_z2 = tmp - ((tmp shr 15) shl 15) // fractional part
            s[i] = saturate((tmp shr 1).toInt()).toShort()
        }
    }

    // ============ LPC Analysis ============

    private fun lpcAnalysis(s: ShortArray, LARc: IntArray) {
        val acf = LongArray(9)
        // Autocorrelation with windowing
        for (k in 0..8) {
            var sum = 0L
            for (i in k until 160) {
                sum += s[i].toLong() * s[i - k].toLong()
            }
            acf[k] = sum
        }

        if (acf[0] == 0L) {
            for (i in 0..7) LARc[i] = 0
            return
        }

        // Schur recursion for reflection coefficients
        val P = LongArray(9) { acf[it] }
        val K = LongArray(9) { acf[it] }

        val r = IntArray(8)
        for (n in 0..7) {
            if (P[0] < Math.abs(K[n + 1])) {
                for (i in n..7) r[i] = 0
                break
            }
            r[n] = if (P[0] == 0L) 0 else ((K[n + 1].toDouble() / P[0].toDouble()) * 32768.0).toInt()
            r[n] = maxOf(-32768, minOf(32767, r[n]))

            if (n < 7) {
                val newK = LongArray(9)
                val newP = LongArray(9)
                for (m in n + 2..8) {
                    newK[m] = K[m] + (r[n].toLong() * P[m - n - 1] + 16384) / 32768
                    newP[m - n - 1] = P[m - n - 1] + (r[n].toLong() * K[m] + 16384) / 32768
                }
                for (m in n + 2..8) {
                    K[m] = newK[m]
                    P[m - n - 1] = newP[m - n - 1]
                }
            }
        }

        // Convert reflection coefficients to LAR
        val B = intArrayOf(0, 0, 2048, -2560, 94, -1792, -341, -1144)
        val MIC = intArrayOf(-32, -32, -16, -16, -8, -8, -4, -4)
        val MAC = intArrayOf(31, 31, 15, 15, 7, 7, 3, 3)
        val A = intArrayOf(20480, 20480, 20480, 20480, 13964, 15360, 8534, 9036)

        for (i in 0..7) {
            val temp = ((r[i].toLong() * A[i] + 16384) shr 15).toInt() + (B[i] shr 1)
            LARc[i] = maxOf(MIC[i], minOf(MAC[i], temp))
        }
    }

    // ============ LAR Decoding ============

    private fun decodeLAR(LARc: IntArray, LARpp: ShortArray) {
        val B = intArrayOf(0, 0, 2048, -2560, 94, -1792, -341, -1144)
        val MIC = intArrayOf(-32, -32, -16, -16, -8, -8, -4, -4)
        val INVA = intArrayOf(13107, 13107, 13107, 13107, 19223, 17476, 31454, 29708)

        for (i in 0..7) {
            var temp = (maxOf(MIC[i].toLong(), minOf(MIC[i].toLong().inv(), LARc[i].toLong())) shl 10).toInt()
            temp -= B[i] shl 1
            temp = ((temp.toLong() * INVA[i] + 16384) shr 15).toInt()
            LARpp[i] = saturate(temp).toShort()
        }
    }

    // ============ Short Term Analysis ============

    private fun shortTermAnalysis(oldLARpp: ShortArray, newLARpp: ShortArray, s: ShortArray, so: ShortArray) {
        val rrp = ShortArray(8)
        for (k in 0 until 4) {
            // Interpolation
            for (i in 0..7) {
                rrp[i] = when (k) {
                    0 -> ((oldLARpp[i].toInt() * 3 + newLARpp[i].toInt()) / 4).toShort()
                    1 -> ((oldLARpp[i].toInt() + newLARpp[i].toInt()) / 2).toShort()
                    2 -> ((oldLARpp[i].toInt() + newLARpp[i].toInt() * 3) / 4).toShort()
                    else -> newLARpp[i]
                }
            }
            for (i in 0 until 40) {
                var di = s[k * 40 + i].toInt()
                for (m in 0..7) {
                    val ui = di + ((rrp[m].toLong() * u[m] + 16384) shr 15).toInt()
                    u[m] = (di + ((rrp[m].toLong() * ui + 16384) shr 15).toInt())
                    di = saturate(ui)
                }
                so[k * 40 + i] = di.toShort()
            }
        }
    }

    // ============ Long Term Analysis (LTP) ============

    private fun longTermAnalysis(d: ShortArray, subframe: Int): IntArray {
        var bestNc = 40
        var bestPower = 0L

        for (lambda in 40..120) {
            var power = 0L
            for (i in 0 until 40) {
                val idx = 120 + i - lambda
                if (idx >= 0 && idx < dp0.size) {
                    power += d[i].toLong() * dp0[idx].toLong()
                }
            }
            if (power > bestPower) {
                bestPower = power
                bestNc = lambda
            }
        }

        // Compute bc (LTP gain)
        var denom = 0L
        for (i in 0 until 40) {
            val idx = 120 + i - bestNc
            if (idx >= 0 && idx < dp0.size) {
                denom += dp0[idx].toLong() * dp0[idx].toLong()
            }
        }

        val bc = when {
            denom == 0L || bestPower <= 0 -> 0
            bestPower >= denom -> 3
            bestPower * 2 >= denom -> 2
            bestPower * 4 >= denom -> 1
            else -> 0
        }

        return intArrayOf(bestNc, bc)
    }

    private fun longTermSynthesis(Nc: Int, bc: Int, d: ShortArray, dpp: ShortArray) {
        val qltp = intArrayOf(3277, 11469, 21299, 32767)
        val gain = qltp[bc]

        for (i in 0 until 40) {
            val idx = 120 + i - Nc
            val dpVal = if (idx >= 0 && idx < dp0.size) dp0[idx].toLong() else 0L
            dpp[i] = saturate(((gain.toLong() * dpVal + 16384) shr 15).toInt()).toShort()
        }
    }

    // ============ RPE Encoding ============

    private fun rpeEncoding(e: ShortArray): IntArray {
        // Find grid position with maximum energy
        var bestMc = 0
        var bestEnergy = 0L

        for (m in 0 until 4) {
            var energy = 0L
            for (i in 0 until 13) {
                val idx = m + i * 3
                if (idx < 40) {
                    energy += e[idx].toLong() * e[idx].toLong()
                }
            }
            if (energy > bestEnergy) {
                bestEnergy = energy
                bestMc = m
            }
        }

        // Extract 13 samples at grid position
        val xM = IntArray(13)
        for (i in 0 until 13) {
            val idx = bestMc + i * 3
            xM[i] = if (idx < 40) e[idx].toInt() else 0
        }

        // Find maximum magnitude
        var xmax = 0
        for (i in 0 until 13) {
            val abs = if (xM[i] < 0) -xM[i] else xM[i]
            if (abs > xmax) xmax = abs
        }

        // Quantize xmax to 6 bits
        val xmaxc = minOf(63, xmax shr 9)

        // Normalize and quantize to 3 bits
        val xMc = IntArray(13)
        val shift = if (xmaxc == 0) 4 else maxOf(0, (xmaxc shr 3) - 1)
        for (i in 0 until 13) {
            xMc[i] = ((xM[i] shr shift) + 4).coerceIn(0, 7)
        }

        return intArrayOf(bestMc, xmaxc) + xMc
    }

    // ============ RPE Decoding ============

    private fun rpeDecode(Mc: Int, xmaxc: Int, xMc: IntArray): ShortArray {
        val erp = ShortArray(40)

        val exp = maxOf(0, (xmaxc shr 3) - 1)
        val mant = xmaxc and 0x07

        for (i in 0 until 13) {
            val xMp = (xMc[i] - 4) shl (exp + 1)
            val idx = Mc + i * 3
            if (idx < 40) {
                erp[idx] = saturate(xMp).toShort()
            }
        }

        return erp
    }

    // ============ Decoder Functions ============

    private fun longTermSynthesisDecode(Nc: Int, bc: Int, erp: ShortArray, wt: ShortArray) {
        val qltp = intArrayOf(3277, 11469, 21299, 32767)
        val gain = qltp[bc]

        for (i in 0 until 40) {
            val nrpVal = nrp[0]
            val idx = if (nrpVal + i - 40 >= 0) nrpVal + i - 40 else 0
            val drpVal = if (idx < v.size) v[idx].toLong() else 0L
            val tmp = erp[i].toLong() + ((gain.toLong() * drpVal + 16384) shr 15)
            wt[i] = saturate(tmp.toInt()).toShort()
        }

        // Update v buffer
        for (i in 0 until minOf(Nc, v.size)) {
            v[i] = wt[i % 40]
        }
        nrp[0] = Nc
    }

    private fun shortTermSynthesisFilter(LARpp: ShortArray, wt: ShortArray) {
        val rrp = ShortArray(8)
        for (i in 0..7) rrp[i] = LARpp[i]

        val vv = IntArray(9)
        for (i in 0 until 40) {
            var sri = wt[i].toInt()
            for (m in 7 downTo 0) {
                sri -= ((rrp[m].toLong() * vv[m] + 16384) shr 15).toInt()
                sri = saturate(sri)
                vv[m + 1] = vv[m] + ((rrp[m].toLong() * sri + 16384) shr 15).toInt()
                vv[m] = sri
            }
            wt[i] = saturate(sri).toShort()
        }
    }

    private fun postProcess(s: ShortArray) {
        for (i in 0 until s.size) {
            val tmp = s[i].toInt() + ((msr.toLong() * 28180L) shr 15).toInt()
            msr = saturate(tmp)
            s[i] = msr.toShort()
        }
    }

    // ============ Bit Packing ============

    private fun packFrame(
        LARc: IntArray, Nc: IntArray, bc: IntArray, Mc: IntArray,
        xmaxc: IntArray, xMc: Array<IntArray>
    ): ByteArray {
        val frame = ByteArray(33)
        var bitPos = 0

        fun putBits(value: Int, nBits: Int) {
            var v = value and ((1 shl nBits) - 1)
            var remaining = nBits
            while (remaining > 0) {
                val byteIdx = bitPos / 8
                val bitIdx = bitPos % 8
                val space = 8 - bitIdx
                val toWrite = minOf(remaining, space)
                val shift = remaining - toWrite
                val bits = (v shr shift) and ((1 shl toWrite) - 1)
                frame[byteIdx] = (frame[byteIdx].toInt() or (bits shl (space - toWrite))).toByte()
                remaining -= toWrite
                bitPos += toWrite
                v = v and ((1 shl remaining) - 1)
            }
        }

        // GSM magic nibble
        putBits(0xD, 4)

        // LARc[0..7]: 6,6,5,5,4,4,3,3 bits
        val larBits = intArrayOf(6, 6, 5, 5, 4, 4, 3, 3)
        for (i in 0..7) putBits(LARc[i], larBits[i])

        // 4 sub-frames
        for (k in 0 until 4) {
            putBits(Nc[k], 7)
            putBits(bc[k], 2)
            putBits(Mc[k], 2)
            putBits(xmaxc[k], 6)
            for (i in 0 until 13) putBits(xMc[k][i], 3)
        }

        return frame
    }

    private fun unpackFrame(
        frame: ByteArray, LARc: IntArray, Nc: IntArray, bc: IntArray,
        Mc: IntArray, xmaxc: IntArray, xMc: Array<IntArray>
    ) {
        var bitPos = 0

        fun getBits(nBits: Int): Int {
            var value = 0
            var remaining = nBits
            while (remaining > 0) {
                val byteIdx = bitPos / 8
                if (byteIdx >= frame.size) return 0
                val bitIdx = bitPos % 8
                val available = 8 - bitIdx
                val toRead = minOf(remaining, available)
                val shift = available - toRead
                val bits = (frame[byteIdx].toInt() shr shift) and ((1 shl toRead) - 1)
                value = (value shl toRead) or bits
                remaining -= toRead
                bitPos += toRead
            }
            return value
        }

        // Skip magic nibble
        getBits(4)

        // LARc
        val larBits = intArrayOf(6, 6, 5, 5, 4, 4, 3, 3)
        for (i in 0..7) LARc[i] = getBits(larBits[i])

        // 4 sub-frames
        for (k in 0 until 4) {
            Nc[k] = getBits(7)
            bc[k] = getBits(2)
            Mc[k] = getBits(2)
            xmaxc[k] = getBits(6)
            for (i in 0 until 13) xMc[k][i] = getBits(3)
        }
    }

    // ============ Utility ============

    private fun saturate(value: Int): Int {
        return maxOf(-32768, minOf(32767, value))
    }
}
