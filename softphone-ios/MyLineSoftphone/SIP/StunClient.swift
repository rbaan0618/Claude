import Foundation
import Darwin

/// Classic STUN binding request to discover public (external) IP/port.
///
/// Direct 1:1 port of Android `StunClient.kt`. Uses a POSIX `socket()` rather than
/// `Network.framework` because on iOS we want to share the *same* UDP socket we use
/// for SIP signaling — otherwise we'd get a different public port (symmetric NAT) and
/// the server-reflexive address would be useless for RTP/SIP.
enum StunClient {
    private static let stunPort: UInt16 = 3478
    private static let timeoutSeconds: Double = 2.0
    private static let bindingRequest: UInt16 = 0x0001
    private static let bindingResponse: UInt16 = 0x0101
    private static let mappedAddress: UInt16 = 0x0001
    private static let xorMappedAddress: UInt16 = 0x0020
    private static let magicCookie: UInt32 = 0x2112A442

    private static let servers = [
        "stun.l.google.com",
        "stun1.l.google.com",
        "stun2.l.google.com",
        "stun.ekiga.net",
    ]

    struct Mapping {
        let ip: String
        let port: UInt16
    }

    /// Performs a STUN binding request on the given already-bound UDP socket file descriptor.
    /// Returns the server-reflexive address, or `nil` if discovery failed on all servers.
    static func discover(socketFD: Int32) -> Mapping? {
        // Save old recv timeout so we can restore it.
        var oldTv = timeval()
        var oldLen = socklen_t(MemoryLayout<timeval>.size)
        getsockopt(socketFD, SOL_SOCKET, SO_RCVTIMEO, &oldTv, &oldLen)

        var tv = timeval(tv_sec: Int(timeoutSeconds), tv_usec: 0)
        setsockopt(socketFD, SOL_SOCKET, SO_RCVTIMEO, &tv, socklen_t(MemoryLayout<timeval>.size))
        defer {
            setsockopt(socketFD, SOL_SOCKET, SO_RCVTIMEO, &oldTv, socklen_t(MemoryLayout<timeval>.size))
        }

        for server in servers {
            if let result = tryServer(socketFD: socketFD, host: server) {
                return result
            }
        }
        return nil
    }

    private static func tryServer(socketFD: Int32, host: String) -> Mapping? {
        var hints = addrinfo(
            ai_flags: 0,
            ai_family: AF_INET,
            ai_socktype: SOCK_DGRAM,
            ai_protocol: IPPROTO_UDP,
            ai_addrlen: 0,
            ai_canonname: nil,
            ai_addr: nil,
            ai_next: nil
        )
        var res: UnsafeMutablePointer<addrinfo>? = nil
        guard getaddrinfo(host, String(stunPort), &hints, &res) == 0, let info = res else {
            return nil
        }
        defer { freeaddrinfo(res) }

        let request = buildBindingRequest()
        let sent = request.withUnsafeBytes { raw -> Int in
            guard let base = raw.baseAddress else { return -1 }
            return sendto(socketFD, base, request.count, 0, info.pointee.ai_addr, info.pointee.ai_addrlen)
        }
        guard sent > 0 else { return nil }

        var buffer = [UInt8](repeating: 0, count: 512)
        let received = buffer.withUnsafeMutableBytes { raw -> Int in
            guard let base = raw.baseAddress else { return -1 }
            return recv(socketFD, base, raw.count, 0)
        }
        guard received > 0 else { return nil }

        return parseBindingResponse(bytes: buffer, length: received)
    }

    // MARK: - Binding request

    private static func buildBindingRequest() -> Data {
        var data = Data(capacity: 20)
        data.append(UInt8((bindingRequest >> 8) & 0xFF))
        data.append(UInt8(bindingRequest & 0xFF))
        data.append(0); data.append(0) // message length = 0
        data.append(UInt8((magicCookie >> 24) & 0xFF))
        data.append(UInt8((magicCookie >> 16) & 0xFF))
        data.append(UInt8((magicCookie >> 8) & 0xFF))
        data.append(UInt8(magicCookie & 0xFF))
        var txn = [UInt8](repeating: 0, count: 12)
        _ = SecRandomCopyBytes(kSecRandomDefault, 12, &txn)
        data.append(contentsOf: txn)
        return data
    }

    // MARK: - Response parsing

    private static func parseBindingResponse(bytes: [UInt8], length: Int) -> Mapping? {
        guard length >= 20 else { return nil }
        let reader = ByteReader(bytes: bytes, length: length)
        let messageType = reader.readUInt16()
        let messageLength = Int(reader.readUInt16())
        let cookie = reader.readUInt32()
        _ = reader.read(12) // transaction id
        guard messageType == bindingResponse else { return nil }

        var result: Mapping? = nil
        var remaining = messageLength
        while remaining > 4 && reader.remaining >= 4 {
            let attrType = reader.readUInt16()
            let attrLength = Int(reader.readUInt16())
            remaining -= 4
            guard reader.remaining >= attrLength else { break }
            switch attrType {
            case xorMappedAddress:
                result = parseXorMappedAddress(reader: reader, length: attrLength, magicCookie: cookie)
            case mappedAddress:
                if result == nil {
                    result = parseMappedAddress(reader: reader, length: attrLength)
                } else {
                    reader.skip(attrLength)
                }
            default:
                reader.skip(attrLength)
            }
            let padding = (4 - (attrLength % 4)) % 4
            remaining -= attrLength + padding
            if padding > 0 && reader.remaining >= padding {
                reader.skip(padding)
            }
        }
        return result
    }

    private static func parseXorMappedAddress(reader: ByteReader, length: Int, magicCookie: UInt32) -> Mapping? {
        guard length >= 8 else { reader.skip(length); return nil }
        _ = reader.readUInt8() // reserved
        let family = reader.readUInt8()
        let xorPort = reader.readUInt16()
        let port = xorPort ^ UInt16((magicCookie >> 16) & 0xFFFF)
        guard family == 0x01 else { return nil } // IPv4 only
        let xorAddr = reader.readUInt32()
        let addr = xorAddr ^ magicCookie
        let ip = "\((addr >> 24) & 0xFF).\((addr >> 16) & 0xFF).\((addr >> 8) & 0xFF).\(addr & 0xFF)"
        return Mapping(ip: ip, port: port)
    }

    private static func parseMappedAddress(reader: ByteReader, length: Int) -> Mapping? {
        guard length >= 8 else { reader.skip(length); return nil }
        _ = reader.readUInt8() // reserved
        let family = reader.readUInt8()
        let port = reader.readUInt16()
        guard family == 0x01 else { return nil }
        let addr = reader.readUInt32()
        let ip = "\((addr >> 24) & 0xFF).\((addr >> 16) & 0xFF).\((addr >> 8) & 0xFF).\(addr & 0xFF)"
        return Mapping(ip: ip, port: port)
    }
}

/// Small big-endian reader for STUN attributes.
private final class ByteReader {
    private let bytes: [UInt8]
    private let length: Int
    private var position: Int = 0

    init(bytes: [UInt8], length: Int) {
        self.bytes = bytes
        self.length = length
    }

    var remaining: Int { length - position }

    func readUInt8() -> UInt8 {
        defer { position += 1 }
        return bytes[position]
    }

    func readUInt16() -> UInt16 {
        let hi = UInt16(bytes[position])
        let lo = UInt16(bytes[position + 1])
        position += 2
        return (hi << 8) | lo
    }

    func readUInt32() -> UInt32 {
        let b0 = UInt32(bytes[position])
        let b1 = UInt32(bytes[position + 1])
        let b2 = UInt32(bytes[position + 2])
        let b3 = UInt32(bytes[position + 3])
        position += 4
        return (b0 << 24) | (b1 << 16) | (b2 << 8) | b3
    }

    func read(_ n: Int) -> [UInt8] {
        let slice = Array(bytes[position..<(position + n)])
        position += n
        return slice
    }

    func skip(_ n: Int) { position += n }
}
