import Foundation

/// Lightweight SIP response/request parser used by `SipHandler`.
/// Only extracts what the dialog state machine needs — header lookups, status line,
/// auth challenges, SDP media lines.
enum SipMessageParser {

    /// Returns `(statusCode, reason)` for a response, or `nil` for a request.
    static func statusLine(of message: String) -> (code: Int, reason: String)? {
        guard let firstLine = message.split(separator: "\r\n", maxSplits: 1).first else { return nil }
        let parts = firstLine.split(separator: " ", maxSplits: 2).map(String.init)
        guard parts.count >= 3, parts[0].hasPrefix("SIP/") else { return nil }
        guard let code = Int(parts[1]) else { return nil }
        return (code, parts[2])
    }

    /// Returns the SIP request method (INVITE, BYE, ...) if `message` is a request.
    static func requestMethod(of message: String) -> String? {
        guard let firstLine = message.split(separator: "\r\n", maxSplits: 1).first else { return nil }
        let parts = firstLine.split(separator: " ", maxSplits: 2).map(String.init)
        guard parts.count >= 3, !parts[0].hasPrefix("SIP/") else { return nil }
        return parts[0]
    }

    /// First header value for `name` (case-insensitive), or `nil`.
    static func header(_ name: String, in message: String) -> String? {
        let lower = name.lowercased() + ":"
        for raw in message.split(separator: "\r\n") {
            let line = String(raw)
            if line.lowercased().hasPrefix(lower) {
                return String(line.dropFirst(lower.count)).trimmingCharacters(in: .whitespaces)
            }
        }
        return nil
    }

    /// Parse a WWW-Authenticate or Proxy-Authenticate header into a digest challenge.
    static func parseAuthChallenge(from headerValue: String) -> SipMessageBuilder.DigestChallenge {
        var challenge = SipMessageBuilder.DigestChallenge()
        let body = headerValue.replacingOccurrences(of: "Digest ", with: "", options: .caseInsensitive)
        // Rough parser — robust enough for comma-separated key=value/key="value" pairs.
        var i = body.startIndex
        while i < body.endIndex {
            while i < body.endIndex && (body[i] == " " || body[i] == ",") { i = body.index(after: i) }
            guard let eq = body[i...].firstIndex(of: "=") else { break }
            let key = String(body[i..<eq]).trimmingCharacters(in: .whitespaces).lowercased()
            var j = body.index(after: eq)
            var value = ""
            if j < body.endIndex && body[j] == "\"" {
                j = body.index(after: j)
                while j < body.endIndex && body[j] != "\"" {
                    value.append(body[j])
                    j = body.index(after: j)
                }
                if j < body.endIndex { j = body.index(after: j) }
            } else {
                while j < body.endIndex && body[j] != "," {
                    value.append(body[j])
                    j = body.index(after: j)
                }
                value = value.trimmingCharacters(in: .whitespaces)
            }
            switch key {
            case "realm":     challenge.realm = value
            case "nonce":     challenge.nonce = value
            case "opaque":    challenge.opaque = value
            case "algorithm": challenge.algorithm = value
            case "qop":       challenge.qop = value
            default: break
            }
            i = j
        }
        return challenge
    }

    /// Extract the message body (everything after the first empty line).
    static func body(of message: String) -> String {
        guard let range = message.range(of: "\r\n\r\n") else { return "" }
        return String(message[range.upperBound...])
    }

    /// SDP: extract "c=" IP and the audio "m=" port + payload types.
    struct SdpInfo {
        var host: String = ""
        var audioPort: Int = 0
        var payloadTypes: [Int] = []
    }

    static func parseSdp(_ sdp: String) -> SdpInfo {
        var info = SdpInfo()
        for raw in sdp.split(separator: "\r\n") {
            let line = String(raw)
            if line.hasPrefix("c=IN IP4 ") {
                info.host = String(line.dropFirst("c=IN IP4 ".count)).trimmingCharacters(in: .whitespaces)
            } else if line.hasPrefix("m=audio ") {
                let parts = line.split(separator: " ").map(String.init)
                if parts.count >= 4 {
                    info.audioPort = Int(parts[1]) ?? 0
                    info.payloadTypes = parts.dropFirst(3).compactMap { Int($0) }
                }
            }
        }
        return info
    }
}
