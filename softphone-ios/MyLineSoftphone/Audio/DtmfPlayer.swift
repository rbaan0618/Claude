import Foundation
import AVFoundation

/// Plays the standard NANP DTMF tone for a keypad digit so the user gets
/// audible feedback while dialling — the same `boop` a real phone makes.
///
/// Each digit is a pair of sine waves (one "low" + one "high"); we generate
/// the WAV in memory the first time the digit is pressed, cache it, and
/// re-use it for every subsequent press.  Using `AVAudioPlayer` with no
/// session-category override keeps us out of CallKit's way — these tones
/// only play while the user is on the dial-pad screen, before any call
/// transaction has been requested.
///
/// DTMF frequencies (ITU-T Q.23):
/// ```
///         1209 Hz  1336 Hz  1477 Hz
/// 697 Hz    1        2        3
/// 770 Hz    4        5        6
/// 852 Hz    7        8        9
/// 941 Hz    *        0        #
/// ```
final class DtmfPlayer {
    static let shared = DtmfPlayer()

    private static let frequencies: [Character: (low: Double, high: Double)] = [
        "1": (697, 1209), "2": (697, 1336), "3": (697, 1477),
        "4": (770, 1209), "5": (770, 1336), "6": (770, 1477),
        "7": (852, 1209), "8": (852, 1336), "9": (852, 1477),
        "*": (941, 1209), "0": (941, 1336), "#": (941, 1477),
    ]

    private var cache: [Character: Data] = [:]
    /// Hold strong refs while playing so AVAudioPlayer isn't dealloc'd mid-tone.
    private var activePlayers: [AVAudioPlayer] = []
    private let queue = DispatchQueue(label: "dtmf-player")

    func play(_ digit: Character) {
        queue.async { [weak self] in
            guard let self else { return }
            guard let pair = DtmfPlayer.frequencies[digit] else { return }
            let wav: Data
            if let cached = self.cache[digit] {
                wav = cached
            } else {
                wav = DtmfPlayer.makeWav(low: pair.low, high: pair.high)
                self.cache[digit] = wav
            }
            do {
                let player = try AVAudioPlayer(data: wav, fileTypeHint: AVFileType.wav.rawValue)
                player.volume = 1.0
                player.prepareToPlay()
                player.play()
                self.activePlayers.append(player)
                // Release the reference shortly after the tone finishes.
                self.queue.asyncAfter(deadline: .now() + 0.3) {
                    self.activePlayers.removeAll { !$0.isPlaying }
                }
            } catch {
                // Silent best-effort — UI feedback isn't worth crashing for.
            }
        }
    }

    private static func makeWav(low: Double, high: Double) -> Data {
        let rate = 22050
        let durationMs = 150
        let durationFrames = rate * durationMs / 1000
        let fadeFrames = rate * 5 / 1000   // 5 ms attack/release to avoid clicks
        var samples = [Int16](repeating: 0, count: durationFrames)
        for i in 0..<durationFrames {
            let t = Double(i) / Double(rate)
            var envelope = 1.0
            if i < fadeFrames {
                envelope = Double(i) / Double(fadeFrames)
            } else if i > durationFrames - fadeFrames {
                envelope = Double(durationFrames - i) / Double(fadeFrames)
            }
            // Sum of two sines; 0.4 amplitude keeps peaks under clipping.
            let v = 0.4 * envelope * (sin(2 * .pi * low * t) + sin(2 * .pi * high * t))
            let clamped = max(-32767.0, min(32767.0, v * 32767.0))
            samples[i] = Int16(clamped)
        }
        return wavWrap(samples: samples, sampleRate: rate)
    }

    private static func wavWrap(samples: [Int16], sampleRate: Int) -> Data {
        func le32(_ v: UInt32) -> [UInt8] {
            [UInt8(v & 0xFF), UInt8((v >> 8) & 0xFF),
             UInt8((v >> 16) & 0xFF), UInt8((v >> 24) & 0xFF)]
        }
        func le16(_ v: UInt16) -> [UInt8] {
            [UInt8(v & 0xFF), UInt8((v >> 8) & 0xFF)]
        }
        let dataBytes = UInt32(samples.count * 2)
        var d = Data()
        d += "RIFF".utf8;       d += le32(36 + dataBytes)
        d += "WAVE".utf8
        d += "fmt ".utf8;       d += le32(16)
        d += le16(1)            // PCM
        d += le16(1)            // mono
        d += le32(UInt32(sampleRate))
        d += le32(UInt32(sampleRate * 2))
        d += le16(2)            // block align
        d += le16(16)           // bits per sample
        d += "data".utf8;       d += le32(dataBytes)
        d += samples.withUnsafeBytes { Data($0) }
        return d
    }
}
