import Foundation

/// GSM 06.10 Full Rate codec (RPE-LTP).
/// Encodes 160 PCM 16-bit samples (20 ms @ 8 kHz) into 33 bytes (260 bits).
///
/// Pure Swift port of the public domain Toast (GSM 06.10) reference
/// implementation by Jutta Degener and Carsten Bormann, TU Berlin.
final class GsmCodec {

    // MARK: - Encoder state

    private var dp0 = [Int16](repeating: 0, count: 280)
    private var z1: Int = 0
    private var L_z2: Int = 0
    private var u = [Int](repeating: 0, count: 8)
    private var LARpp = [[Int16]](repeating: [Int16](repeating: 0, count: 8), count: 2)
    private var j: Int = 0

    // MARK: - Decoder state

    private var msr: Int = 0
    private var dLARpp = [[Int16]](repeating: [Int16](repeating: 0, count: 8), count: 2)
    private var dj: Int = 0
    private var v = [Int16](repeating: 0, count: 9)
    private var nrp: Int = 40

    // MARK: - Public API

    func encode(_ frame: [Int16]) -> [UInt8] {
        var s = [Int16](repeating: 0, count: 160)
        for i in 0..<min(frame.count, 160) { s[i] = frame[i] }

        var LARc  = [Int](repeating: 0, count: 8)
        var Nc    = [Int](repeating: 0, count: 4)
        var bc    = [Int](repeating: 0, count: 4)
        var Mc    = [Int](repeating: 0, count: 4)
        var xmaxc = [Int](repeating: 0, count: 4)
        var xMc   = [[Int]](repeating: [Int](repeating: 0, count: 13), count: 4)

        preprocess(&s)
        lpcAnalysis(&s, &LARc)

        let oldLARpp = LARpp[j]
        j ^= 1
        decodeLAR(LARc, &LARpp[j])
        let newLARpp = LARpp[j]

        var so = [Int16](repeating: 0, count: 160)
        shortTermAnalysis(oldLARpp, newLARpp, &s, &so)

        for k in 0..<4 {
            var d = [Int16](repeating: 0, count: 40)
            for i in 0..<40 { d[i] = so[k * 40 + i] }

            let ltResult = longTermAnalysis(d, k)
            Nc[k] = ltResult.0
            bc[k] = ltResult.1

            var dpp = [Int16](repeating: 0, count: 40)
            longTermSynthesis(Nc[k], bc[k], d, &dpp)

            var e = [Int16](repeating: 0, count: 40)
            for i in 0..<40 { e[i] = Int16(clamping: Int(d[i]) - Int(dpp[i])) }

            let rpeResult = rpeEncoding(e)
            Mc[k] = rpeResult.0
            xmaxc[k] = rpeResult.1
            for i in 0..<13 { xMc[k][i] = rpeResult.2[i] }

            let rpeDecoded = rpeDecode(Mc[k], xmaxc[k], xMc[k])
            for i in 0..<40 {
                dp0[120 + i] = Int16(clamping: Int(dpp[i]) + Int(rpeDecoded[i]))
            }
            dp0.removeFirst(40)
            dp0 += [Int16](repeating: 0, count: 40)
        }

        return packFrame(LARc, Nc, bc, Mc, xmaxc, xMc)
    }

    func decode(_ bytes: [UInt8]) -> [Int16] {
        var LARc  = [Int](repeating: 0, count: 8)
        var Nc    = [Int](repeating: 0, count: 4)
        var bc    = [Int](repeating: 0, count: 4)
        var Mc    = [Int](repeating: 0, count: 4)
        var xmaxc = [Int](repeating: 0, count: 4)
        var xMc   = [[Int]](repeating: [Int](repeating: 0, count: 13), count: 4)

        unpackFrame(bytes, &LARc, &Nc, &bc, &Mc, &xmaxc, &xMc)

        let oldLARpp = dLARpp[dj]
        dj ^= 1
        decodeLAR(LARc, &dLARpp[dj])
        let newLARpp = dLARpp[dj]

        var sr = [Int16](repeating: 0, count: 160)

        for k in 0..<4 {
            let erp = rpeDecode(Mc[k], xmaxc[k], xMc[k])

            var wt = [Int16](repeating: 0, count: 40)
            longTermSynthesisDecode(Nc[k], bc[k], erp, &wt)

            var interpolated = [Int16](repeating: 0, count: 8)
            for i in 0..<8 {
                let f = k * 13 + i
                interpolated[i] = Int16(clamping: {
                    switch k {
                    case 0 where f < 13:
                        return (Int(oldLARpp[i]) * (13 - f) + Int(newLARpp[i]) * f) / 13
                    case 0:
                        return Int(newLARpp[i])
                    case 1:
                        return (Int(oldLARpp[i]) + Int(newLARpp[i]) * 3) / 4
                    case 2:
                        return (Int(oldLARpp[i]) + Int(newLARpp[i])) / 2
                    default:
                        return Int(newLARpp[i])
                    }
                }())
            }

            shortTermSynthesisFilter(interpolated, &wt)
            postProcess(&wt)

            for i in 0..<40 { sr[k * 40 + i] = wt[i] }
        }

        return sr
    }

    // MARK: - Preprocessing

    private func preprocess(_ s: inout [Int16]) {
        for i in 0..<160 {
            let so: Int = Int(s[i]) << 1
            let sof: Int = so - ((z1 * 28180) >> 15)
            z1 = so
            let tmp: Int = sof + L_z2
            L_z2 = tmp - ((tmp >> 15) << 15)
            s[i] = Int16(clamping: saturate(tmp >> 1))
        }
    }

    // MARK: - LPC Analysis

    private func lpcAnalysis(_ s: inout [Int16], _ LARc: inout [Int]) {
        var acf = [Int](repeating: 0, count: 9)
        for k in 0...8 {
            var sum = 0
            for i in k..<160 {
                sum += Int(s[i]) * Int(s[i - k])
            }
            acf[k] = sum
        }

        guard acf[0] != 0 else {
            for i in 0...7 { LARc[i] = 0 }
            return
        }

        var P = acf
        var K = acf
        var r = [Int](repeating: 0, count: 8)

        for n in 0...7 {
            let absKn1 = K[n + 1] < 0 ? -K[n + 1] : K[n + 1]
            if P[0] < absKn1 {
                for i in n...7 { r[i] = 0 }
                break
            }
            r[n] = P[0] == 0 ? 0 : max(-32768, min(32767, Int(Double(K[n + 1]) / Double(P[0]) * 32768.0)))

            if n < 7 {
                var newK = [Int](repeating: 0, count: 9)
                var newP = [Int](repeating: 0, count: 9)
                for m in (n + 2)...8 {
                    newK[m] = K[m] + (r[n] * P[m - n - 1] + 16384) / 32768
                    newP[m - n - 1] = P[m - n - 1] + (r[n] * K[m] + 16384) / 32768
                }
                for m in (n + 2)...8 {
                    K[m] = newK[m]
                    P[m - n - 1] = newP[m - n - 1]
                }
            }
        }

        let B:   [Int] = [0, 0, 2048, -2560, 94, -1792, -341, -1144]
        let MIC: [Int] = [-32, -32, -16, -16, -8, -8, -4, -4]
        let MAC: [Int] = [31, 31, 15, 15, 7, 7, 3, 3]
        let A:   [Int] = [20480, 20480, 20480, 20480, 13964, 15360, 8534, 9036]

        for i in 0...7 {
            let temp = (r[i] * A[i] + 16384) >> 15 + (B[i] >> 1)
            LARc[i] = max(MIC[i], min(MAC[i], temp))
        }
    }

    // MARK: - LAR Decoding

    private func decodeLAR(_ LARc: [Int], _ LARpp: inout [Int16]) {
        let B:    [Int] = [0, 0, 2048, -2560, 94, -1792, -341, -1144]
        let MIC:  [Int] = [-32, -32, -16, -16, -8, -8, -4, -4]
        let INVA: [Int] = [13107, 13107, 13107, 13107, 19223, 17476, 31454, 29708]

        for i in 0...7 {
            var temp = max(MIC[i], min(-MIC[i] - 1, LARc[i])) << 10
            temp -= B[i] << 1
            temp = (temp * INVA[i] + 16384) >> 15
            LARpp[i] = Int16(clamping: saturate(temp))
        }
    }

    // MARK: - Short-Term Analysis (encoder)

    private func shortTermAnalysis(_ oldLARpp: [Int16], _ newLARpp: [Int16],
                                   _ s: inout [Int16], _ so: inout [Int16]) {
        var rrp = [Int16](repeating: 0, count: 8)
        for k in 0..<4 {
            for i in 0...7 {
                rrp[i] = Int16(clamping: {
                    switch k {
                    case 0: return (Int(oldLARpp[i]) * 3 + Int(newLARpp[i])) / 4
                    case 1: return (Int(oldLARpp[i]) + Int(newLARpp[i])) / 2
                    case 2: return (Int(oldLARpp[i]) + Int(newLARpp[i]) * 3) / 4
                    default: return Int(newLARpp[i])
                    }
                }())
            }
            for i in 0..<40 {
                var di = Int(s[k * 40 + i])
                for m in 0...7 {
                    let ui = di + (Int(rrp[m]) * u[m] + 16384) >> 15
                    u[m] = di + (Int(rrp[m]) * ui + 16384) >> 15
                    di = saturate(ui)
                }
                so[k * 40 + i] = Int16(clamping: di)
            }
        }
    }

    // MARK: - Long-Term Analysis

    private func longTermAnalysis(_ d: [Int16], _ subframe: Int) -> (Int, Int) {
        var bestNc = 40
        var bestPower = 0

        for lambda in 40...120 {
            var power = 0
            for i in 0..<40 {
                let idx = 120 + i - lambda
                if idx >= 0 && idx < dp0.count {
                    power += Int(d[i]) * Int(dp0[idx])
                }
            }
            if power > bestPower {
                bestPower = power
                bestNc = lambda
            }
        }

        var denom = 0
        for i in 0..<40 {
            let idx = 120 + i - bestNc
            if idx >= 0 && idx < dp0.count {
                denom += Int(dp0[idx]) * Int(dp0[idx])
            }
        }

        let bc: Int
        if denom == 0 || bestPower <= 0 {
            bc = 0
        } else if bestPower >= denom {
            bc = 3
        } else if bestPower * 2 >= denom {
            bc = 2
        } else if bestPower * 4 >= denom {
            bc = 1
        } else {
            bc = 0
        }

        return (bestNc, bc)
    }

    private func longTermSynthesis(_ Nc: Int, _ bc: Int, _ d: [Int16], _ dpp: inout [Int16]) {
        let qltp = [3277, 11469, 21299, 32767]
        let gain = qltp[bc]
        for i in 0..<40 {
            let idx = 120 + i - Nc
            let dpVal = (idx >= 0 && idx < dp0.count) ? Int(dp0[idx]) : 0
            dpp[i] = Int16(clamping: saturate((gain * dpVal + 16384) >> 15))
        }
    }

    // MARK: - RPE Encoding

    private func rpeEncoding(_ e: [Int16]) -> (Int, Int, [Int]) {
        var bestMc = 0
        var bestEnergy = 0

        for m in 0..<4 {
            var energy = 0
            for i in 0..<13 {
                let idx = m + i * 3
                if idx < 40 {
                    energy += Int(e[idx]) * Int(e[idx])
                }
            }
            if energy > bestEnergy { bestEnergy = energy; bestMc = m }
        }

        var xM = [Int](repeating: 0, count: 13)
        for i in 0..<13 {
            let idx = bestMc + i * 3
            xM[i] = idx < 40 ? Int(e[idx]) : 0
        }

        var xmax = 0
        for i in 0..<13 {
            let a = xM[i] < 0 ? -xM[i] : xM[i]
            if a > xmax { xmax = a }
        }

        let xmaxc = min(63, xmax >> 9)
        let shift = xmaxc == 0 ? 4 : max(0, (xmaxc >> 3) - 1)
        var xMc = [Int](repeating: 0, count: 13)
        for i in 0..<13 {
            xMc[i] = max(0, min(7, (xM[i] >> shift) + 4))
        }

        return (bestMc, xmaxc, xMc)
    }

    // MARK: - RPE Decoding

    private func rpeDecode(_ Mc: Int, _ xmaxc: Int, _ xMc: [Int]) -> [Int16] {
        var erp = [Int16](repeating: 0, count: 40)
        let exp = max(0, (xmaxc >> 3) - 1)
        for i in 0..<13 {
            let xMp = (xMc[i] - 4) << (exp + 1)
            let idx = Mc + i * 3
            if idx < 40 { erp[idx] = Int16(clamping: saturate(xMp)) }
        }
        return erp
    }

    // MARK: - Decoder functions

    private func longTermSynthesisDecode(_ Nc: Int, _ bc: Int, _ erp: [Int16], _ wt: inout [Int16]) {
        let qltp = [3277, 11469, 21299, 32767]
        let gain = qltp[bc]
        for i in 0..<40 {
            let idx = nrp + i - 40
            let drpVal = (idx >= 0 && idx < v.count) ? Int(v[idx]) : 0
            let tmp = Int(erp[i]) + (gain * drpVal + 16384) >> 15
            wt[i] = Int16(clamping: saturate(tmp))
        }
        for i in 0..<min(Nc, v.count) {
            v[i] = wt[i % 40]
        }
        nrp = Nc
    }

    private func shortTermSynthesisFilter(_ LARppIn: [Int16], _ wt: inout [Int16]) {
        var rrp = [Int16](repeating: 0, count: 8)
        for i in 0...7 { rrp[i] = LARppIn[i] }
        var vv = [Int](repeating: 0, count: 9)
        for i in 0..<40 {
            var sri = Int(wt[i])
            for m in stride(from: 7, through: 0, by: -1) {
                sri -= (Int(rrp[m]) * vv[m] + 16384) >> 15
                sri = saturate(sri)
                vv[m + 1] = vv[m] + (Int(rrp[m]) * sri + 16384) >> 15
                vv[m] = sri
            }
            wt[i] = Int16(clamping: saturate(sri))
        }
    }

    private func postProcess(_ s: inout [Int16]) {
        for i in 0..<s.count {
            let tmp = Int(s[i]) + (msr * 28180) >> 15
            msr = saturate(tmp)
            s[i] = Int16(clamping: msr)
        }
    }

    // MARK: - Bit packing

    private func packFrame(_ LARc: [Int], _ Nc: [Int], _ bc: [Int], _ Mc: [Int],
                           _ xmaxc: [Int], _ xMc: [[Int]]) -> [UInt8] {
        var frame = [UInt8](repeating: 0, count: 33)
        var bitPos = 0

        func putBits(_ value: Int, _ nBits: Int) {
            var v = value & ((1 << nBits) - 1)
            var remaining = nBits
            while remaining > 0 {
                let byteIdx = bitPos / 8
                let bitIdx = bitPos % 8
                let space = 8 - bitIdx
                let toWrite = min(remaining, space)
                let shift = remaining - toWrite
                let bits = (v >> shift) & ((1 << toWrite) - 1)
                frame[byteIdx] |= UInt8(bits << (space - toWrite))
                remaining -= toWrite
                bitPos += toWrite
                v &= (1 << remaining) - 1
            }
        }

        putBits(0xD, 4)
        let larBits = [6, 6, 5, 5, 4, 4, 3, 3]
        for i in 0...7 { putBits(LARc[i], larBits[i]) }
        for k in 0..<4 {
            putBits(Nc[k], 7)
            putBits(bc[k], 2)
            putBits(Mc[k], 2)
            putBits(xmaxc[k], 6)
            for i in 0..<13 { putBits(xMc[k][i], 3) }
        }
        return frame
    }

    private func unpackFrame(_ frame: [UInt8], _ LARc: inout [Int], _ Nc: inout [Int],
                             _ bc: inout [Int], _ Mc: inout [Int],
                             _ xmaxc: inout [Int], _ xMc: inout [[Int]]) {
        var bitPos = 0

        func getBits(_ nBits: Int) -> Int {
            var value = 0
            var remaining = nBits
            while remaining > 0 {
                let byteIdx = bitPos / 8
                if byteIdx >= frame.count { return 0 }
                let bitIdx = bitPos % 8
                let available = 8 - bitIdx
                let toRead = min(remaining, available)
                let shift = available - toRead
                let bits = Int(frame[byteIdx] >> shift) & ((1 << toRead) - 1)
                value = (value << toRead) | bits
                remaining -= toRead
                bitPos += toRead
            }
            return value
        }

        _ = getBits(4) // magic nibble
        let larBits = [6, 6, 5, 5, 4, 4, 3, 3]
        for i in 0...7 { LARc[i] = getBits(larBits[i]) }
        for k in 0..<4 {
            Nc[k] = getBits(7)
            bc[k] = getBits(2)
            Mc[k] = getBits(2)
            xmaxc[k] = getBits(6)
            for i in 0..<13 { xMc[k][i] = getBits(3) }
        }
    }

    // MARK: - Utility

    private func saturate(_ value: Int) -> Int {
        return max(-32768, min(32767, value))
    }
}
