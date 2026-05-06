import Foundation
import AVFoundation
import Darwin
import os.log

/// RTP audio session — captures microphone, encodes PCMU/PCMA/GSM/G.729, sends RTP
/// datagrams, receives and plays back.
///
/// Port of Android `RtpSession.kt`. Uses `AVAudioEngine` with an `AVAudioConverter`
/// to move between the hardware's native format and the 8 kHz / Int16 / mono
/// format that the G.711 / GSM / G.729 codecs expect. The socket is shared with
/// the SIP handler where possible to keep the NAT pinhole stable.
final class RtpSession {
    private static let log = Logger(subsystem: "com.mylinetelecom.softphone", category: "RtpSession")

    static let pcmuPayloadType: UInt8 = 0
    static let pcmaPayloadType: UInt8 = 8
    static let gsmPayloadType: UInt8 = 3
    static let g729PayloadType: UInt8 = 18
    static let dtmfPayloadType: UInt8 = 101

    private static let sampleRate: Double = 8000
    private static let frameSize: Int = 160 // 20 ms @ 8 kHz
    private static let rtpHeaderSize: Int = 12
    private static let gsmFrameSize: Int = 33
    private static let g729FrameSamples: Int = 80
    private static let g729FrameBytes: Int = 10

    // MARK: - Configuration

    private let localPort: UInt16
    private let remoteHost: String
    private let remotePort: UInt16
    private let existingSocketFD: Int32
    private var codecType: UInt8

    // MARK: - State

    private var socketFD: Int32 = -1
    private var ownsSocket: Bool = false
    private var remoteSockaddr: sockaddr_in = sockaddr_in()

    private let engine = AVAudioEngine()
    private var player: AVAudioPlayerNode?
    private var playbackFormat: AVAudioFormat?

    /// Converter from hardware input format → 8 kHz mono Float32.
    private var captureConverter: AVAudioConverter?
    private var captureRemainder: [Int16] = []

    private var receiveThread: Thread?
    private var stopped: Bool = false

    private var sequenceNumber: UInt16 = 0
    private var timestamp: UInt32 = 0
    private var ssrc: UInt32 = UInt32.random(in: 0...UInt32.max)
    private var sendingDtmf: Bool = false

    var isMuted: Bool = false

    // Codecs (lazy)
    private var gsmEncoder: GsmCodec?
    private var gsmDecoder: GsmCodec?
    private var g729Codec: G729Codec?

    // MARK: - Init

    init(
        localPort: UInt16,
        remoteHost: String,
        remotePort: UInt16,
        existingSocketFD: Int32 = -1,
        codecType: UInt8 = RtpSession.pcmuPayloadType
    ) {
        self.localPort = localPort
        self.remoteHost = remoteHost
        self.remotePort = remotePort
        self.existingSocketFD = existingSocketFD
        self.codecType = codecType
    }

    // MARK: - Lifecycle

    func start() {
        configureAudioSession()

        if existingSocketFD >= 0 {
            socketFD = existingSocketFD
            ownsSocket = false
        } else {
            socketFD = createBoundUdpSocket(port: localPort)
            ownsSocket = true
        }
        guard socketFD >= 0 else {
            Self.log.error("RTP socket bind failed")
            return
        }
        guard resolveRemote() else {
            Self.log.error("RTP remote resolve failed")
            return
        }

        switch codecType {
        case Self.gsmPayloadType:
            gsmEncoder = GsmCodec(); gsmDecoder = GsmCodec()
        case Self.g729PayloadType:
            g729Codec = G729Codec()
            if g729Codec?.open() != true {
                Self.log.warning("G.729 unavailable, falling back to PCMU")
                g729Codec = nil
                codecType = Self.pcmuPayloadType
            }
        default: break
        }

        startAudioEngine()
        startReceive()
        Self.log.info("RTP started port=\(self.localPort) -> \(self.remoteHost):\(self.remotePort)")
    }

    func stop() {
        stopped = true
        receiveThread?.cancel(); receiveThread = nil

        engine.inputNode.removeTap(onBus: 0)
        player?.stop()
        engine.stop()
        player = nil

        g729Codec?.close(); g729Codec = nil
        gsmEncoder = nil; gsmDecoder = nil

        if ownsSocket && socketFD >= 0 {
            Darwin.close(socketFD)
        }
        socketFD = -1
        Self.log.info("RTP stopped")
    }

    func setCodec(_ payloadType: UInt8) {
        codecType = payloadType
    }

    func mute(_ muted: Bool) {
        isMuted = muted
    }

    // MARK: - Audio session + engine

    private func configureAudioSession() {
        // SipService.provider(_:didActivate:) already configured the session on the
        // main thread before this runs on ioQueue.  We do a lightweight ensure-active
        // call only — no category change (would fight CallKit) and no .defaultToSpeaker
        // (CallKit manages routing; forcing speaker here breaks lock-screen answer).
        let session = AVAudioSession.sharedInstance()
        do {
            try session.setActive(true)
        } catch {
            Self.log.error("AVAudioSession setActive failed: \(String(describing: error), privacy: .public)")
        }
    }

    private func startAudioEngine() {
        // Playback: create an AVAudioPlayerNode running at 8 kHz Float32 mono.
        // Connecting through the main mixer lets the engine resample to hardware rate.
        let playerNode = AVAudioPlayerNode()
        guard let pbFormat = AVAudioFormat(commonFormat: .pcmFormatFloat32,
                                           sampleRate: Self.sampleRate,
                                           channels: 1,
                                           interleaved: false) else {
            Self.log.error("Cannot create playback format")
            return
        }
        playbackFormat = pbFormat
        engine.attach(playerNode)
        engine.connect(playerNode, to: engine.mainMixerNode, format: pbFormat)
        player = playerNode

        // Capture: tap the input at its native format and convert to 8 kHz Int16 mono.
        let input = engine.inputNode
        let hwFormat = input.inputFormat(forBus: 0)
        guard hwFormat.sampleRate > 0 else {
            Self.log.error("Input format has zero sample rate (no mic permission?)")
            return
        }
        guard let captureOut = AVAudioFormat(commonFormat: .pcmFormatInt16,
                                             sampleRate: Self.sampleRate,
                                             channels: 1,
                                             interleaved: true) else { return }
        captureConverter = AVAudioConverter(from: hwFormat, to: captureOut)

        input.installTap(onBus: 0, bufferSize: 1024, format: hwFormat) { [weak self] buffer, _ in
            self?.handleCapturedBuffer(buffer, outputFormat: captureOut)
        }

        do {
            try engine.start()
            playerNode.play()
        } catch {
            Self.log.error("AVAudioEngine start failed: \(String(describing: error), privacy: .public)")
        }
    }

    // MARK: - Capture → encode → send

    private func handleCapturedBuffer(_ inBuffer: AVAudioPCMBuffer, outputFormat: AVAudioFormat) {
        guard let converter = captureConverter else { return }
        // Estimate output capacity based on rate ratio.
        let ratio = outputFormat.sampleRate / inBuffer.format.sampleRate
        let outCapacity = AVAudioFrameCount(Double(inBuffer.frameLength) * ratio + 64)
        guard let outBuffer = AVAudioPCMBuffer(pcmFormat: outputFormat, frameCapacity: outCapacity) else { return }

        var consumed = false
        var error: NSError?
        let status = converter.convert(to: outBuffer, error: &error) { _, outStatus in
            if consumed {
                outStatus.pointee = .noDataNow
                return nil
            }
            consumed = true
            outStatus.pointee = .haveData
            return inBuffer
        }
        guard status != .error, let ch = outBuffer.int16ChannelData else { return }
        let count = Int(outBuffer.frameLength)
        if count == 0 { return }
        let ptr = ch[0]
        captureRemainder.append(contentsOf: UnsafeBufferPointer(start: ptr, count: count))

        // Drain 160-sample frames.
        while captureRemainder.count >= Self.frameSize {
            var frame = [Int16](captureRemainder.prefix(Self.frameSize))
            captureRemainder.removeFirst(Self.frameSize)
            sendFrame(&frame)
        }
    }

    private func sendFrame(_ pcm: inout [Int16]) {
        if sendingDtmf { return }
        if isMuted {
            // Advance sequence/timestamp to match Kotlin behavior (no RTP on the wire).
            sequenceNumber &+= 1
            timestamp &+= UInt32(Self.frameSize)
            return
        }
        let payload: [UInt8]
        switch codecType {
        case Self.gsmPayloadType:
            payload = gsmEncoder?.encode(pcm) ?? [UInt8](repeating: 0, count: Self.gsmFrameSize)
        case Self.g729PayloadType where g729Codec != nil:
            let f1 = g729Codec!.encode(frame: pcm, offset: 0) ?? [UInt8](repeating: 0, count: Self.g729FrameBytes)
            let f2 = g729Codec!.encode(frame: pcm, offset: Self.g729FrameSamples) ?? [UInt8](repeating: 0, count: Self.g729FrameBytes)
            payload = f1 + f2
        case Self.pcmaPayloadType:
            payload = G711.encodePCMA(pcm)
        default:
            payload = G711.encodePCMU(pcm)
        }
        let packet = buildRtpPacket(payloadType: codecType, payload: payload, marker: false)
        sendPacket(packet)
        sequenceNumber &+= 1
        timestamp &+= UInt32(Self.frameSize)
    }

    private func sendPacket(_ packet: [UInt8]) {
        guard socketFD >= 0 else { return }
        var addr = remoteSockaddr
        _ = packet.withUnsafeBufferPointer { buf in
            withUnsafePointer(to: &addr) { saPtr -> Int in
                saPtr.withMemoryRebound(to: sockaddr.self, capacity: 1) { raw in
                    Darwin.sendto(socketFD, buf.baseAddress, buf.count, 0, raw,
                                  socklen_t(MemoryLayout<sockaddr_in>.size))
                }
            }
        }
    }

    // MARK: - Receive → decode → play

    private func startReceive() {
        let thread = Thread { [weak self] in self?.receiveLoop() }
        thread.name = "RtpReceiver"
        thread.start()
        receiveThread = thread
    }

    private func receiveLoop() {
        var buffer = [UInt8](repeating: 0, count: 2048)
        while !stopped, socketFD >= 0 {
            let n = buffer.withUnsafeMutableBufferPointer { buf in
                Darwin.recv(socketFD, buf.baseAddress, buf.count, 0)
            }
            if n <= Self.rtpHeaderSize { continue }
            let pt = buffer[1] & 0x7F
            let payloadLen = Int(n) - Self.rtpHeaderSize
            switch pt {
            case Self.pcmuPayloadType:
                let pcm = G711.decodePCMU(buffer, offset: Self.rtpHeaderSize, length: payloadLen)
                enqueuePlayback(pcm)
            case Self.pcmaPayloadType:
                let pcm = G711.decodePCMA(buffer, offset: Self.rtpHeaderSize, length: payloadLen)
                enqueuePlayback(pcm)
            case Self.gsmPayloadType:
                if payloadLen >= Self.gsmFrameSize, let dec = gsmDecoder {
                    let chunk = Array(buffer[Self.rtpHeaderSize..<(Self.rtpHeaderSize + Self.gsmFrameSize)])
                    let pcm = dec.decode(chunk)
                    if !pcm.isEmpty { enqueuePlayback(pcm) }
                }
            case Self.g729PayloadType:
                if let g729 = g729Codec, payloadLen >= Self.g729FrameBytes {
                    let frames = payloadLen / Self.g729FrameBytes
                    var combined: [Int16] = []
                    for f in 0..<frames {
                        let start = Self.rtpHeaderSize + f * Self.g729FrameBytes
                        let chunk = Array(buffer[start..<(start + Self.g729FrameBytes)])
                        if let pcm = g729.decode(chunk) { combined.append(contentsOf: pcm) }
                    }
                    if !combined.isEmpty { enqueuePlayback(combined) }
                }
            default:
                break // DTMF and others ignored on receive
            }
        }
    }

    private func enqueuePlayback(_ pcm: [Int16]) {
        guard let player = player, let format = playbackFormat else { return }
        guard let buf = AVAudioPCMBuffer(pcmFormat: format,
                                         frameCapacity: AVAudioFrameCount(pcm.count)) else { return }
        buf.frameLength = AVAudioFrameCount(pcm.count)
        guard let ch = buf.floatChannelData?[0] else { return }
        for i in 0..<pcm.count {
            ch[i] = Float(pcm[i]) / 32768.0
        }
        player.scheduleBuffer(buf, completionHandler: nil)
    }

    // MARK: - DTMF (RFC 2833)

    func sendDtmf(_ digit: Character) {
        let event: UInt8
        switch digit {
        case "0": event = 0; case "1": event = 1; case "2": event = 2
        case "3": event = 3; case "4": event = 4; case "5": event = 5
        case "6": event = 6; case "7": event = 7; case "8": event = 8
        case "9": event = 9; case "*": event = 10; case "#": event = 11
        default: return
        }
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
            self.sendingDtmf = true
            let dtmfTimestamp = self.timestamp
            let total = 8
            for i in 0..<total {
                let duration = UInt16(Self.frameSize * (i + 1))
                let payload: [UInt8] = [event, 10, UInt8(duration >> 8), UInt8(duration & 0xFF)]
                let saved = self.timestamp
                self.timestamp = dtmfTimestamp
                let packet = self.buildRtpPacket(payloadType: Self.dtmfPayloadType, payload: payload, marker: i == 0)
                self.timestamp = saved
                self.sendPacket(packet)
                self.sequenceNumber &+= 1
                Thread.sleep(forTimeInterval: 0.02)
            }
            let finalDuration = UInt16(Self.frameSize * total)
            for _ in 0..<3 {
                let payload: [UInt8] = [event, 0x80 | 10,
                                        UInt8(finalDuration >> 8), UInt8(finalDuration & 0xFF)]
                let saved = self.timestamp
                self.timestamp = dtmfTimestamp
                let packet = self.buildRtpPacket(payloadType: Self.dtmfPayloadType, payload: payload, marker: false)
                self.timestamp = saved
                self.sendPacket(packet)
                self.sequenceNumber &+= 1
                Thread.sleep(forTimeInterval: 0.02)
            }
            self.timestamp &+= UInt32(finalDuration)
            self.sendingDtmf = false
        }
    }

    // MARK: - RTP packet

    private func buildRtpPacket(payloadType: UInt8, payload: [UInt8], marker: Bool) -> [UInt8] {
        var packet = [UInt8](repeating: 0, count: Self.rtpHeaderSize + payload.count)
        packet[0] = 0x80
        packet[1] = (marker ? 0x80 : 0) | (payloadType & 0x7F)
        packet[2] = UInt8(truncatingIfNeeded: sequenceNumber >> 8)
        packet[3] = UInt8(truncatingIfNeeded: sequenceNumber)
        packet[4] = UInt8(truncatingIfNeeded: timestamp >> 24)
        packet[5] = UInt8(truncatingIfNeeded: timestamp >> 16)
        packet[6] = UInt8(truncatingIfNeeded: timestamp >> 8)
        packet[7] = UInt8(truncatingIfNeeded: timestamp)
        packet[8] = UInt8(truncatingIfNeeded: ssrc >> 24)
        packet[9] = UInt8(truncatingIfNeeded: ssrc >> 16)
        packet[10] = UInt8(truncatingIfNeeded: ssrc >> 8)
        packet[11] = UInt8(truncatingIfNeeded: ssrc)
        if !payload.isEmpty {
            packet.replaceSubrange(Self.rtpHeaderSize..<(Self.rtpHeaderSize + payload.count), with: payload)
        }
        return packet
    }

    // MARK: - Socket helpers

    private func createBoundUdpSocket(port: UInt16) -> Int32 {
        let fd = Darwin.socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP)
        guard fd >= 0 else { return -1 }
        var reuse: Int32 = 1
        setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &reuse, socklen_t(MemoryLayout<Int32>.size))
        var addr = sockaddr_in()
        addr.sin_family = sa_family_t(AF_INET)
        addr.sin_port = port.bigEndian
        addr.sin_addr.s_addr = INADDR_ANY.bigEndian
        let rc = withUnsafePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                Darwin.bind(fd, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
            }
        }
        if rc != 0 { Darwin.close(fd); return -1 }
        return fd
    }

    private func resolveRemote() -> Bool {
        var hints = addrinfo(ai_flags: 0, ai_family: AF_INET, ai_socktype: SOCK_DGRAM,
                             ai_protocol: IPPROTO_UDP, ai_addrlen: 0,
                             ai_canonname: nil, ai_addr: nil, ai_next: nil)
        var result: UnsafeMutablePointer<addrinfo>?
        let rc = getaddrinfo(remoteHost, String(remotePort), &hints, &result)
        guard rc == 0, let first = result else { return false }
        defer { freeaddrinfo(result) }
        let addrPtr = first.pointee.ai_addr!
        addrPtr.withMemoryRebound(to: sockaddr_in.self, capacity: 1) { sin in
            self.remoteSockaddr = sin.pointee
        }
        return true
    }
}

// MARK: - G.711 μ-law / A-law

enum G711 {
    private static let ulawExponentTable: [Int] = [
        0,0,1,1,2,2,2,2,3,3,3,3,3,3,3,3,
        4,4,4,4,4,4,4,4,4,4,4,4,4,4,4,4,
        5,5,5,5,5,5,5,5,5,5,5,5,5,5,5,5,
        5,5,5,5,5,5,5,5,5,5,5,5,5,5,5,5,
        6,6,6,6,6,6,6,6,6,6,6,6,6,6,6,6,
        6,6,6,6,6,6,6,6,6,6,6,6,6,6,6,6,
        6,6,6,6,6,6,6,6,6,6,6,6,6,6,6,6,
        6,6,6,6,6,6,6,6,6,6,6,6,6,6,6,6,
        7,7,7,7,7,7,7,7,7,7,7,7,7,7,7,7,
        7,7,7,7,7,7,7,7,7,7,7,7,7,7,7,7,
        7,7,7,7,7,7,7,7,7,7,7,7,7,7,7,7,
        7,7,7,7,7,7,7,7,7,7,7,7,7,7,7,7,
        7,7,7,7,7,7,7,7,7,7,7,7,7,7,7,7,
        7,7,7,7,7,7,7,7,7,7,7,7,7,7,7,7,
        7,7,7,7,7,7,7,7,7,7,7,7,7,7,7,7,
        7,7,7,7,7,7,7,7,7,7,7,7,7,7,7,7
    ]
    private static let ulawDecodeTable: [Int] = [0, 132, 396, 924, 1980, 4092, 8316, 16764]

    static func encodePCMU(_ pcm: [Int16]) -> [UInt8] {
        var out = [UInt8](repeating: 0, count: pcm.count)
        for i in 0..<pcm.count { out[i] = linearToUlaw(Int(pcm[i])) }
        return out
    }

    static func decodePCMU(_ data: [UInt8], offset: Int, length: Int) -> [Int16] {
        var out = [Int16](repeating: 0, count: length)
        for i in 0..<length { out[i] = ulawToLinear(data[offset + i]) }
        return out
    }

    static func encodePCMA(_ pcm: [Int16]) -> [UInt8] {
        var out = [UInt8](repeating: 0, count: pcm.count)
        for i in 0..<pcm.count { out[i] = linearToAlaw(Int(pcm[i])) }
        return out
    }

    static func decodePCMA(_ data: [UInt8], offset: Int, length: Int) -> [Int16] {
        var out = [Int16](repeating: 0, count: length)
        for i in 0..<length { out[i] = alawToLinear(data[offset + i]) }
        return out
    }

    private static func linearToUlaw(_ sample: Int) -> UInt8 {
        let BIAS = 0x84
        let CLIP = 32635
        var pcmVal = sample
        let sign = (pcmVal >> 8) & 0x80
        if sign != 0 { pcmVal = -pcmVal }
        if pcmVal > CLIP { pcmVal = CLIP }
        pcmVal += BIAS
        let exponent = ulawExponentTable[(pcmVal >> 7) & 0xFF]
        let mantissa = (pcmVal >> (exponent + 3)) & 0x0F
        let ulawByte = ~(sign | (exponent << 4) | mantissa)
        return UInt8(truncatingIfNeeded: ulawByte)
    }

    private static func ulawToLinear(_ byte: UInt8) -> Int16 {
        let mulaw = Int(~byte & 0xFF)
        let sign = mulaw & 0x80
        let exponent = (mulaw >> 4) & 0x07
        let mantissa = mulaw & 0x0F
        var sample = ulawDecodeTable[exponent] + (mantissa << (exponent + 3))
        if sign != 0 { sample = -sample }
        return Int16(clamping: sample)
    }

    // A-law per ITU-T G.711
    private static func linearToAlaw(_ sample: Int) -> UInt8 {
        var pcmVal = sample
        let sign = ((~pcmVal) >> 8) & 0x80
        if sign == 0 { pcmVal = -pcmVal }
        if pcmVal > 32635 { pcmVal = 32635 }
        let aval: Int
        if pcmVal >= 256 {
            let exponent = Int(log2(Double(pcmVal >> 8)))
            let mantissa = (pcmVal >> (exponent + 3)) & 0x0F
            aval = (exponent << 4) | mantissa
        } else {
            aval = pcmVal >> 4
        }
        return UInt8(truncatingIfNeeded: (aval | sign) ^ 0x55)
    }

    private static func alawToLinear(_ byte: UInt8) -> Int16 {
        let a = Int(byte) ^ 0x55
        let sign = a & 0x80
        let exponent = (a >> 4) & 0x07
        let mantissa = a & 0x0F
        var sample: Int
        if exponent == 0 {
            sample = (mantissa << 4) + 8
        } else {
            sample = ((mantissa << 4) + 0x108) << (exponent - 1)
        }
        if sign == 0 { sample = -sample }
        return Int16(clamping: sample)
    }
}

// GsmCodec is implemented in GsmCodec.swift (pure Swift GSM 06.10 RPE-LTP).

/// G.729 — 80 samples → 10 bytes. Thin Swift wrapper over the bcg729 C library
/// bridged via BCG729-Bridging-Header.h / Codecs/g729_wrapper.{h,c}.
final class G729Codec {
    private var ctx: UnsafeMutableRawPointer?

    func open() -> Bool {
        ctx = g729_create()
        return ctx != nil
    }

    func close() {
        if let c = ctx { g729_destroy(c); ctx = nil }
    }

    func encode(frame: [Int16], offset: Int) -> [UInt8]? {
        guard let ctx else { return nil }
        let end = min(offset + 80, frame.count)
        var slice = [Int16](frame[offset..<end])
        if slice.count < 80 { slice += [Int16](repeating: 0, count: 80 - slice.count) }
        var out = [UInt8](repeating: 0, count: 10)
        let n = slice.withUnsafeBufferPointer { pcmPtr in
            out.withUnsafeMutableBufferPointer { bsPtr in
                g729_encode(ctx, pcmPtr.baseAddress, bsPtr.baseAddress)
            }
        }
        return n == 10 ? out : nil
    }

    func decode(_ bytes: [UInt8]) -> [Int16]? {
        guard let ctx, bytes.count >= 10 else { return nil }
        var pcm = [Int16](repeating: 0, count: 80)
        _ = bytes.withUnsafeBufferPointer { bsPtr in
            pcm.withUnsafeMutableBufferPointer { pcmPtr in
                g729_decode(ctx, bsPtr.baseAddress, pcmPtr.baseAddress)
            }
        }
        return pcm
    }
}
