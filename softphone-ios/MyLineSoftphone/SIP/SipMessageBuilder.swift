import Foundation
import CryptoKit

/// Helpers to construct SIP messages (requests, responses, auth headers, SDP).
/// Keeps `SipHandler` focused on dialog/state machine logic.
///
/// This mirrors the `buildRequest` / `buildAuthHeader` / `buildSdp` private helpers
/// from Android `SipHandler.kt`.
enum SipMessageBuilder {

    // MARK: - Identifier generation

    static func generateCallId() -> String {
        let uuid = UUID().uuidString.replacingOccurrences(of: "-", with: "")
        return String(uuid.prefix(16))
    }

    static func generateTag() -> String {
        String(format: "%08x", UInt32.random(in: 0...UInt32.max))
    }

    static func generateBranch() -> String {
        "z9hG4bK\(Int.random(in: 100000...999999))"
    }

    // MARK: - Request construction

    /// Build a SIP request with the standard header set.
    /// - Parameters left as TODO for future fidelity matching Android string order.
    static func buildRequest(
        method: String,
        requestUri: String,
        toUri: String,
        fromUri: String,
        callId: String,
        cseq: Int,
        fromTag: String,
        toTag: String = "",
        viaBranch: String? = nil,
        contactAddress: String,
        localIp: String,
        localPort: Int,
        publicHost: String?,
        publicPort: Int?,
        rport: Bool,
        username: String,
        transport: String = "UDP",
        extraHeaders: [(String, String)] = [],
        body: String = ""
    ) -> String {
        let branch = viaBranch ?? generateBranch()
        // Via uses public address if discovered via STUN, falling back to local.
        let viaHost = publicHost ?? localIp
        let viaPort = publicPort ?? localPort
        let rportParam = rport ? ";rport" : ""
        let toTagPart = toTag.isEmpty ? "" : ";tag=\(toTag)"

        var lines: [String] = []
        lines.append("\(method) \(requestUri) SIP/2.0")
        lines.append("Via: SIP/2.0/\(transport) \(viaHost):\(viaPort)\(rportParam);branch=\(branch)")
        lines.append("Max-Forwards: 70")
        lines.append("From: <\(fromUri)>;tag=\(fromTag)")
        lines.append("To: <\(toUri)>\(toTagPart)")
        lines.append("Call-ID: \(callId)")
        lines.append("CSeq: \(cseq) \(method)")
        for (k, v) in extraHeaders {
            lines.append("\(k): \(v)")
        }
        lines.append("User-Agent: MyLineTelecom-iOS/1.0")
        let bodyData = body.data(using: .utf8) ?? Data()
        lines.append("Content-Length: \(bodyData.count)")
        lines.append("")
        lines.append(body)
        return lines.joined(separator: "\r\n")
    }

    // MARK: - Digest authentication (RFC 2617)

    struct DigestChallenge {
        var realm: String = ""
        var nonce: String = ""
        var opaque: String = ""
        var algorithm: String = "MD5"
        var qop: String = ""
    }

    static func buildDigestAuthHeader(
        method: String,
        uri: String,
        username: String,
        password: String,
        challenge: DigestChallenge,
        nonceCount: Int,
        cnonce: String,
        isProxy: Bool = false
    ) -> String {
        let ha1 = md5Hex("\(username):\(challenge.realm):\(password)")
        let ha2 = md5Hex("\(method):\(uri)")
        let response: String
        let ncString = String(format: "%08x", nonceCount)
        if challenge.qop.contains("auth") {
            response = md5Hex("\(ha1):\(challenge.nonce):\(ncString):\(cnonce):auth:\(ha2)")
        } else {
            response = md5Hex("\(ha1):\(challenge.nonce):\(ha2)")
        }

        var parts: [String] = [
            "Digest username=\"\(username)\"",
            "realm=\"\(challenge.realm)\"",
            "nonce=\"\(challenge.nonce)\"",
            "uri=\"\(uri)\"",
            "response=\"\(response)\"",
            "algorithm=\(challenge.algorithm)"
        ]
        if !challenge.opaque.isEmpty {
            parts.append("opaque=\"\(challenge.opaque)\"")
        }
        if challenge.qop.contains("auth") {
            parts.append("qop=auth")
            parts.append("nc=\(ncString)")
            parts.append("cnonce=\"\(cnonce)\"")
        }
        return parts.joined(separator: ", ")
    }

    static func md5Hex(_ string: String) -> String {
        let digest = Insecure.MD5.hash(data: Data(string.utf8))
        return digest.map { String(format: "%02x", $0) }.joined()
    }

    // MARK: - SDP

    /// Minimal offer/answer SDP: PCMU + PCMA + GSM + telephone-event DTMF.
    /// G.729 is only advertised if `includeG729` is true (license opt-in).
    static func buildSdp(
        localIp: String,
        rtpPort: Int,
        includeG729: Bool = false,
        holdMode: Bool = false
    ) -> String {
        let sessionId = Int.random(in: 1_000_000_000...9_999_999_999)
        var payloadTypes = ["0", "8", "3"] // PCMU, PCMA, GSM
        if includeG729 { payloadTypes.append("18") }
        payloadTypes.append("101") // telephone-event

        let direction = holdMode ? "sendonly" : "sendrecv"

        var lines: [String] = [
            "v=0",
            "o=- \(sessionId) \(sessionId) IN IP4 \(localIp)",
            "s=MyLineTelecom",
            "c=IN IP4 \(localIp)",
            "t=0 0",
            "m=audio \(rtpPort) RTP/AVP \(payloadTypes.joined(separator: " "))",
            "a=rtpmap:0 PCMU/8000",
            "a=rtpmap:8 PCMA/8000",
            "a=rtpmap:3 GSM/8000",
        ]
        if includeG729 {
            lines.append("a=rtpmap:18 G729/8000")
            lines.append("a=fmtp:18 annexb=no")
        }
        lines.append("a=rtpmap:101 telephone-event/8000")
        lines.append("a=fmtp:101 0-15")
        lines.append("a=ptime:20")
        lines.append("a=\(direction)")
        return lines.joined(separator: "\r\n") + "\r\n"
    }
}
