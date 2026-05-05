import Foundation
import Network
import Combine
import CryptoKit
import Darwin
import os.log

/// Core SIP dialog and state machine.
///
/// Direct port of Android `SipHandler.kt` (~2300 lines). State and side effects
/// run on a dedicated serial dispatch queue (`ioQueue`), and `@Published`
/// properties are updated via `DispatchQueue.main.async` so SwiftUI views can
/// observe them directly.
///
/// Design notes specific to iOS:
///  - Uses a raw POSIX UDP socket (`Darwin.socket`) rather than `Network.framework`
///    so we can share it with `StunClient` (same public port) and `recvfrom` from
///    any remote address on one bound local port.
///  - The audio session (`.voiceChat` mode) is configured by `RtpSession`, which
///    uses `AVAudioEngine` at 8 kHz mono — hardware AEC comes for free.
///  - Background behavior: active calls keep the app alive via `UIBackgroundModes
///    = audio`. When fully suspended, iOS kills UDP sockets — use PushKit to wake
///    on incoming calls (wired up in `SipService`).
final class SipHandler: ObservableObject {
    private static let log = Logger(subsystem: "com.mylinetelecom.softphone", category: "SipHandler")

    // MARK: - Constants

    private static let registerExpiresSeconds = 300
    private static let keepaliveIntervalSeconds: Int = 15
    private static let reRegisterBeforeMs: Int = 30_000

    // MARK: - Published state

    @Published private(set) var registrationState: RegistrationState = .unregistered
    @Published private(set) var callState: CallState = .idle
    @Published private(set) var remoteNumber: String = ""
    @Published private(set) var remoteName: String = ""
    @Published private(set) var blfStates: [String: BlfState] = [:]
    @Published private(set) var isConsulting: Bool = false
    @Published private(set) var consultState: CallState = .idle
    @Published private(set) var consultNumber: String = ""

    // MARK: - Callbacks

    var onCallStateChanged: ((CallState, _ number: String, _ name: String) -> Void)?
    var onRegistrationChanged: ((RegistrationState) -> Void)?
    var onMessageReceived: ((_ from: String, _ body: String) -> Void)?

    // MARK: - Serial IO queue

    private let ioQueue = DispatchQueue(label: "com.mylinetelecom.sip.io")

    // MARK: - Socket / network

    private var config = SipConfig()
    private var socketFD: Int32 = -1
    private var localIp: String = "0.0.0.0"
    private var publicIp: String = ""
    private var publicPort: UInt16 = 0

    // MARK: - Registration state

    private var registerCallId: String = ""
    private var registerFromTag: String = ""
    private var registerCseq: Int = 1

    // MARK: - Digest auth state

    private var authRealm: String = ""
    private var authNonce: String = ""
    private var authOpaque: String = ""
    private var authAlgorithm: String = "MD5"
    private var authQop: String = ""
    private var authNonceCount: Int = 0
    private var authCNonce: String = ""
    private var inviteAuthAttempted: Bool = false

    // MARK: - Primary call dialog

    private var currentCallId: String = ""
    private var currentLocalTag: String = ""
    private var currentRemoteTag: String = ""
    private var currentRemoteUri: String = ""
    private var currentCallDirection: String = "" // "inbound" / "outbound"
    private var currentCSeq: Int = 1
    private var currentInviteBranch: String = ""
    private var incomingInviteMsg: String?
    private var pendingHoldMode: Bool = false
    private var pendingReferTarget: String = ""
    private var referAuthAttempted: Bool = false

    // MARK: - RTP (primary leg)

    private var rtpSession: RtpSession?
    private var rtpSocketFD: Int32 = -1
    private var localRtpPort: Int = 0
    private var remoteRtpHost: String = ""
    private var remoteRtpPort: Int = 0
    private var negotiatedCodec: Int = 0

    // MARK: - Consultation dialog (attended transfer)

    private var consultCallId: String = ""
    private var consultLocalTag: String = ""
    private var consultRemoteTag: String = ""
    private var consultRemoteUri: String = ""
    private var consultCSeq: Int = 1
    private var consultInviteBranch: String = ""
    private var consultAuthAttempted: Bool = false
    private var consultRtpPort: Int = 0
    private var consultRtpSocketFD: Int32 = -1
    private var consultRtpSession: RtpSession?
    private var consultRemoteRtpHost: String = ""
    private var consultRemoteRtpPort: Int = 0
    private var consultNegotiatedCodec: Int = 0

    // MARK: - BLF subscription tracking

    private struct BlfSubscription {
        let ext: String
        let callId: String
        var fromTag: String
        var cseq: Int = 1
        var authAttempted: Bool = false
    }
    private var blfSubscriptions: [String: BlfSubscription] = [:]
    private var rejectedBlfCallIds: Set<String> = []

    // MARK: - Background jobs

    private var receiverThread: Thread?
    private var keepaliveTimer: DispatchSourceTimer?
    private var reRegisterTimer: DispatchSourceTimer?
    private var pendingKeepalives: Int = 0
    private var stopping = false
    private var restarting = false

    // MARK: - Public API

    func configure(_ sipConfig: SipConfig) {
        ioQueue.async { [weak self] in
            self?.config = sipConfig
        }
    }

    func start() {
        ioQueue.async { [weak self] in
            self?.startInternal()
        }
    }

    func stop() {
        ioQueue.async { [weak self] in
            self?.stopBlocking()
        }
    }

    func restartForNetworkChange() {
        ioQueue.async { [weak self] in
            guard let self, !self.restarting else { return }
            self.restarting = true
            defer { self.restarting = false }
            Self.log.info("Network change — restarting SIP stack")

            self.receiverThread?.cancel()
            self.keepaliveTimer?.cancel()
            self.reRegisterTimer?.cancel()
            if self.socketFD >= 0 { Darwin.close(self.socketFD); self.socketFD = -1 }

            self.rtpSession?.stop(); self.rtpSession = nil
            if self.rtpSocketFD >= 0 { Darwin.close(self.rtpSocketFD); self.rtpSocketFD = -1 }
            self.consultRtpSession?.stop(); self.consultRtpSession = nil
            if self.consultRtpSocketFD >= 0 { Darwin.close(self.consultRtpSocketFD); self.consultRtpSocketFD = -1 }

            self.updateRegistration(.unregistered)
            self.publicIp = ""
            self.publicPort = 0
            self.authNonce = ""
            self.authRealm = ""
            self.authOpaque = ""
            self.authQop = ""
            self.authNonceCount = 0
            self.authCNonce = ""
            self.blfSubscriptions.removeAll()
            self.rejectedBlfCallIds.removeAll()
            self.stopping = false

            if self.callState != .idle && self.callState != .disconnected {
                Self.log.warning("Active call dropped due to network change")
                let num = self.remoteNumber, name = self.remoteName
                self.resetCallState()
                self.setCallState(.disconnected, number: num, name: name)
            }

            if self.config.isValid {
                self.startInternal()
            }
        }
    }

    func makeCall(_ number: String) {
        ioQueue.async { [weak self] in
            guard let self else { return }
            guard self.callState == .idle else {
                Self.log.warning("makeCall ignored — state \(self.callState.rawValue, privacy: .public)")
                return
            }
            guard self.registrationState == .registered else {
                Self.log.warning("makeCall ignored — not registered")
                return
            }

            self.currentCallId = self.generateCallId()
            self.currentLocalTag = self.generateTag()
            self.currentRemoteTag = ""
            self.currentCallDirection = "outbound"
            self.currentCSeq = 1
            self.inviteAuthAttempted = false
            self.pendingHoldMode = false
            self.currentRemoteUri = "sip:\(number)@\(self.config.domain)"

            self.setCallState(.calling, number: number, name: "")

            self.localRtpPort = self.allocateRtpPort()
            let sdp = self.buildSdp(rtpPort: self.localRtpPort, holdMode: false)
            let contactAddr = self.contactAddress()
            let branch = self.generateBranch()
            self.currentInviteBranch = branch

            let extraHeaders: [(String, String)] = [
                ("Contact", "<sip:\(self.config.username)@\(contactAddr)>"),
                ("Content-Type", "application/sdp"),
                ("Allow", "INVITE,ACK,BYE,CANCEL,OPTIONS,NOTIFY,REFER"),
            ]

            let request = self.buildRequest(
                method: "INVITE",
                requestUri: self.currentRemoteUri,
                toUri: self.currentRemoteUri,
                fromUri: "sip:\(self.config.username)@\(self.config.domain)",
                callId: self.currentCallId,
                cseq: self.currentCSeq,
                fromTag: self.currentLocalTag,
                toTag: "",
                extraHeaders: extraHeaders,
                body: sdp,
                viaBranch: branch
            )
            self.sendSip(request)
        }
    }

    func answerCall() {
        ioQueue.async { [weak self] in
            guard let self, self.callState == .incoming, let invite = self.incomingInviteMsg else { return }

            if self.localRtpPort == 0 { self.localRtpPort = self.allocateRtpPort() }
            let sdp = self.buildSdp(rtpPort: self.localRtpPort, holdMode: false)
            let contactAddr = self.contactAddress()

            let viaHeader = Self.extractHeader(invite, name: "Via") ?? ""
            let fromHeader = Self.extractHeader(invite, name: "From") ?? ""
            let toHeader = Self.extractHeader(invite, name: "To") ?? ""
            let cseqHeader = Self.extractHeader(invite, name: "CSeq") ?? ""

            let toWithTag = toHeader.contains("tag=") ? toHeader : "\(toHeader);tag=\(self.currentLocalTag)"

            let sdpBytes = sdp.data(using: .utf8)?.count ?? 0
            let response =
                "SIP/2.0 200 OK\r\n" +
                "Via: \(viaHeader)\r\n" +
                "From: \(fromHeader)\r\n" +
                "To: \(toWithTag)\r\n" +
                "Call-ID: \(self.currentCallId)\r\n" +
                "CSeq: \(cseqHeader)\r\n" +
                "Contact: <sip:\(self.config.username)@\(contactAddr)>\r\n" +
                "Content-Type: application/sdp\r\n" +
                "User-Agent: MyLineTelecom-iOS/1.0\r\n" +
                "Content-Length: \(sdpBytes)\r\n\r\n" +
                sdp

            self.sendSip(response)
            self.startRtp()
            self.setCallState(.confirmed, number: self.remoteNumber, name: self.remoteName)
        }
    }

    func hangup() {
        ioQueue.async { [weak self] in
            guard let self else { return }
            let state = self.callState
            if state == .idle || state == .disconnected { return }

            switch state {
            case .calling, .ringing:
                // CANCEL — must use same Via branch as original INVITE.
                let req = self.buildRequest(
                    method: "CANCEL",
                    requestUri: self.currentRemoteUri,
                    toUri: self.currentRemoteUri,
                    fromUri: "sip:\(self.config.username)@\(self.config.domain)",
                    callId: self.currentCallId,
                    cseq: self.currentCSeq,
                    fromTag: self.currentLocalTag,
                    toTag: self.currentRemoteTag,
                    extraHeaders: [],
                    body: "",
                    viaBranch: self.currentInviteBranch
                )
                self.sendSip(req)
            case .incoming:
                if let invite = self.incomingInviteMsg {
                    let resp = self.buildMirroredResponse(code: 486, reason: "Busy Here", request: invite, toTag: self.currentLocalTag)
                    self.sendSip(resp)
                }
            default:
                self.currentCSeq += 1
                let req = self.buildRequest(
                    method: "BYE",
                    requestUri: self.currentRemoteUri,
                    toUri: self.currentRemoteUri,
                    fromUri: "sip:\(self.config.username)@\(self.config.domain)",
                    callId: self.currentCallId,
                    cseq: self.currentCSeq,
                    fromTag: self.currentLocalTag,
                    toTag: self.currentRemoteTag,
                    extraHeaders: [],
                    body: "",
                    viaBranch: nil
                )
                self.sendSip(req)
            }

            self.rtpSession?.stop()
            self.rtpSession = nil

            // If a consultation leg is active, BYE it too.
            if self.isConsulting && !self.consultCallId.isEmpty && self.consultState == .confirmed {
                self.consultCSeq += 1
                let byeConsult = self.buildRequest(
                    method: "BYE",
                    requestUri: self.consultRemoteUri,
                    toUri: self.consultRemoteUri,
                    fromUri: "sip:\(self.config.username)@\(self.config.domain)",
                    callId: self.consultCallId,
                    cseq: self.consultCSeq,
                    fromTag: self.consultLocalTag,
                    toTag: self.consultRemoteTag,
                    extraHeaders: [],
                    body: "",
                    viaBranch: nil
                )
                self.sendSip(byeConsult)
            }

            self.setCallState(.disconnected, number: self.remoteNumber, name: self.remoteName)
            self.ioQueue.asyncAfter(deadline: .now() + 1.0) { [weak self] in
                self?.resetCallState()
            }
        }
    }

    func toggleMute() -> Bool {
        var result = false
        ioQueue.sync {
            guard let rtp = self.rtpSession else { return }
            rtp.isMuted.toggle()
            result = rtp.isMuted
        }
        return result
    }

    func setMuted(_ muted: Bool) {
        ioQueue.async { [weak self] in self?.rtpSession?.isMuted = muted }
    }

    func toggleHold() {
        ioQueue.async { [weak self] in
            guard let self else { return }
            if self.callState == .confirmed {
                self.pendingHoldMode = true
                self.inviteAuthAttempted = false
                self.currentCSeq += 1
                let sdp = self.buildSdp(rtpPort: self.localRtpPort, holdMode: true)
                let req = self.buildRequest(
                    method: "INVITE",
                    requestUri: self.currentRemoteUri,
                    toUri: self.currentRemoteUri,
                    fromUri: "sip:\(self.config.username)@\(self.config.domain)",
                    callId: self.currentCallId,
                    cseq: self.currentCSeq,
                    fromTag: self.currentLocalTag,
                    toTag: self.currentRemoteTag,
                    extraHeaders: [
                        ("Contact", "<sip:\(self.config.username)@\(self.contactAddress())>"),
                        ("Content-Type", "application/sdp"),
                        ("Allow", "INVITE,ACK,BYE,CANCEL,OPTIONS,NOTIFY,REFER"),
                    ],
                    body: sdp,
                    viaBranch: nil
                )
                self.sendSip(req)
                self.rtpSession?.isMuted = true
                self.setCallState(.hold, number: self.remoteNumber, name: self.remoteName)
            } else if self.callState == .hold {
                self.pendingHoldMode = false
                self.inviteAuthAttempted = false
                self.currentCSeq += 1
                let sdp = self.buildSdp(rtpPort: self.localRtpPort, holdMode: false)
                let req = self.buildRequest(
                    method: "INVITE",
                    requestUri: self.currentRemoteUri,
                    toUri: self.currentRemoteUri,
                    fromUri: "sip:\(self.config.username)@\(self.config.domain)",
                    callId: self.currentCallId,
                    cseq: self.currentCSeq,
                    fromTag: self.currentLocalTag,
                    toTag: self.currentRemoteTag,
                    extraHeaders: [
                        ("Contact", "<sip:\(self.config.username)@\(self.contactAddress())>"),
                        ("Content-Type", "application/sdp"),
                        ("Allow", "INVITE,ACK,BYE,CANCEL,OPTIONS,NOTIFY,REFER"),
                    ],
                    body: sdp,
                    viaBranch: nil
                )
                self.sendSip(req)
                self.rtpSession?.isMuted = false
                self.setCallState(.confirmed, number: self.remoteNumber, name: self.remoteName)
            }
        }
    }

    /// SwiftUI convenience: set hold to a specific value (matches CallKit CXSetHeldCallAction).
    func setHold(_ hold: Bool) {
        ioQueue.async { [weak self] in
            guard let self else { return }
            if hold && self.callState == .confirmed { self.toggleHold() }
            else if !hold && self.callState == .hold { self.toggleHold() }
        }
    }

    func sendDtmf(_ digit: Character) {
        ioQueue.async { [weak self] in self?.rtpSession?.sendDtmf(digit) }
    }

    /// Called by SipService when CallKit activates the audio session.
    /// If RTP hasn't started yet (audio session was not ready earlier), start it now.
    func handleAudioActivation() {
        ioQueue.async { [weak self] in
            guard let self else { return }
            if (self.callState == .confirmed || self.callState == .hold) && self.rtpSession == nil {
                Self.log.info("Audio activated by CallKit — starting RTP")
                self.startRtp()
            }
        }
    }

    /// Called by SipService when CallKit deactivates the audio session.
    func handleAudioDeactivation() {
        ioQueue.async { [weak self] in
            self?.rtpSession?.stop()
        }
    }

    func sendMessage(to recipient: String, text: String, messageType: String = "sms") {
        ioQueue.async { [weak self] in
            guard let self, self.registrationState == .registered else { return }
            // FreeSWITCH strips the '+' prefix, so routing is determined by digit count:
            //   WhatsApp → 11 digits with leading country code 1 (e.g. 13059684280)
            //   SMS      → 10 digits, no country code        (e.g.  3059684280)
            // Mirrors Android MainActivity.kt sendMessage routing logic.
            let digits = recipient.replacingOccurrences(of: "[^0-9]", with: "", options: .regularExpression)
            let routedRecipient: String
            if messageType == "whatsapp" {
                routedRecipient = digits.count == 10 ? "1\(digits)" : digits
            } else {
                routedRecipient = (digits.count == 11 && digits.hasPrefix("1")) ? String(digits.dropFirst()) : digits
            }
            let msgCallId = self.generateCallId()
            let fromTag = self.generateTag()
            let contactAddr = self.contactAddress()
            let req = self.buildRequest(
                method: "MESSAGE",
                requestUri: "sip:\(routedRecipient)@\(self.config.domain)",
                toUri: "sip:\(routedRecipient)@\(self.config.domain)",
                fromUri: "sip:\(self.config.username)@\(self.config.domain)",
                callId: msgCallId,
                cseq: 1,
                fromTag: fromTag,
                toTag: "",
                extraHeaders: [
                    ("Contact", "<sip:\(self.config.username)@\(contactAddr)>"),
                    ("Content-Type", "text/plain"),
                ],
                body: text,
                viaBranch: nil
            )
            self.sendSip(req)
            Self.log.info("Sent \(messageType) MESSAGE to \(routedRecipient, privacy: .public)")
        }
    }

    func blindTransfer(target: String) {
        ioQueue.async { [weak self] in
            guard let self else { return }
            if self.callState != .confirmed && self.callState != .hold { return }
            self.pendingReferTarget = target
            self.referAuthAttempted = false
            self.currentCSeq += 1
            self.sendRefer(target: target, authHeader: nil)
        }
    }

    func startConsultation(target: String) {
        // Mirrors `attendedTransferStart` in Kotlin.
        ioQueue.async { [weak self] in
            guard let self else { return }
            if self.callState != .confirmed && self.callState != .hold { return }

            // Step 1: put primary on hold if not already.
            if self.callState == .confirmed {
                self.pendingHoldMode = true
                self.inviteAuthAttempted = false
                self.currentCSeq += 1
                let sdp = self.buildSdp(rtpPort: self.localRtpPort, holdMode: true)
                let req = self.buildRequest(
                    method: "INVITE",
                    requestUri: self.currentRemoteUri,
                    toUri: self.currentRemoteUri,
                    fromUri: "sip:\(self.config.username)@\(self.config.domain)",
                    callId: self.currentCallId,
                    cseq: self.currentCSeq,
                    fromTag: self.currentLocalTag,
                    toTag: self.currentRemoteTag,
                    extraHeaders: [
                        ("Contact", "<sip:\(self.config.username)@\(self.contactAddress())>"),
                        ("Content-Type", "application/sdp"),
                        ("Allow", "INVITE,ACK,BYE,CANCEL,OPTIONS,NOTIFY,REFER"),
                    ],
                    body: sdp,
                    viaBranch: nil
                )
                self.sendSip(req)
                self.rtpSession?.isMuted = true
                self.setCallState(.hold, number: self.remoteNumber, name: self.remoteName)
            }

            // Step 2: after a brief pause, start consultation call.
            self.ioQueue.asyncAfter(deadline: .now() + 0.5) { [weak self] in
                guard let self else { return }
                self.consultCallId = self.generateCallId()
                self.consultLocalTag = self.generateTag()
                self.consultRemoteTag = ""
                self.consultRemoteUri = "sip:\(target)@\(self.config.domain)"
                self.consultCSeq = 1
                self.consultAuthAttempted = false
                DispatchQueue.main.async {
                    self.consultNumber = target
                    self.consultState = .calling
                    self.isConsulting = true
                }

                self.consultRtpPort = self.allocateConsultRtpPort()
                let sdp = self.buildSdp(rtpPort: self.consultRtpPort, holdMode: false)
                let branch = self.generateBranch()
                self.consultInviteBranch = branch

                let req = self.buildRequest(
                    method: "INVITE",
                    requestUri: self.consultRemoteUri,
                    toUri: self.consultRemoteUri,
                    fromUri: "sip:\(self.config.username)@\(self.config.domain)",
                    callId: self.consultCallId,
                    cseq: self.consultCSeq,
                    fromTag: self.consultLocalTag,
                    toTag: "",
                    extraHeaders: [
                        ("Contact", "<sip:\(self.config.username)@\(self.contactAddress())>"),
                        ("Content-Type", "application/sdp"),
                        ("Allow", "INVITE,ACK,BYE,CANCEL,OPTIONS,NOTIFY,REFER"),
                    ],
                    body: sdp,
                    viaBranch: branch
                )
                self.sendSip(req)
                Self.log.info("Consultation call started to \(target, privacy: .public)")
            }
        }
    }

    func completeAttendedTransfer() {
        ioQueue.async { [weak self] in
            guard let self, self.isConsulting, self.consultState == .confirmed else { return }
            let encodedCallId = self.consultCallId.replacingOccurrences(of: "@", with: "%40")
            let replacesValue = "\(encodedCallId)%3Bto-tag%3D\(self.consultRemoteTag)%3Bfrom-tag%3D\(self.consultLocalTag)"
            let referTo = "<sip:\(self.consultNumber)@\(self.config.domain)?Replaces=\(replacesValue)>"

            self.pendingReferTarget = self.consultNumber
            self.referAuthAttempted = false
            self.currentCSeq += 1

            let req = self.buildRequest(
                method: "REFER",
                requestUri: self.currentRemoteUri,
                toUri: self.currentRemoteUri,
                fromUri: "sip:\(self.config.username)@\(self.config.domain)",
                callId: self.currentCallId,
                cseq: self.currentCSeq,
                fromTag: self.currentLocalTag,
                toTag: self.currentRemoteTag,
                extraHeaders: [
                    ("Refer-To", referTo),
                    ("Referred-By", "<sip:\(self.config.username)@\(self.config.domain)>"),
                    ("Contact", "<sip:\(self.config.username)@\(self.contactAddress())>"),
                    ("Allow", "INVITE,ACK,BYE,CANCEL,OPTIONS,NOTIFY,REFER"),
                ],
                body: "",
                viaBranch: nil
            )
            self.sendSip(req)
            Self.log.info("Attended transfer REFER with Replaces sent")
        }
    }

    func subscribeBlf(_ ext: String) {
        ioQueue.async { [weak self] in
            guard let self else { return }
            let subCallId = self.generateCallId()
            let fromTag = self.generateTag()
            let sub = BlfSubscription(ext: ext, callId: subCallId, fromTag: fromTag, cseq: 1, authAttempted: false)
            self.blfSubscriptions[subCallId] = sub
            let req = self.buildRequest(
                method: "SUBSCRIBE",
                requestUri: "sip:\(ext)@\(self.config.domain)",
                toUri: "sip:\(ext)@\(self.config.domain)",
                fromUri: "sip:\(self.config.username)@\(self.config.domain)",
                callId: subCallId,
                cseq: 1,
                fromTag: fromTag,
                toTag: "",
                extraHeaders: [
                    ("Event", "dialog"),
                    ("Accept", "application/dialog-info+xml"),
                    ("Expires", "3600"),
                    ("Contact", "<sip:\(self.config.username)@\(self.contactAddress())>"),
                ],
                body: "",
                viaBranch: nil
            )
            self.sendSip(req)
        }
    }

    // MARK: - Start / stop / socket

    private func startInternal() {
        dispatchPrecondition(condition: .onQueue(ioQueue))
        guard config.isValid else {
            Self.log.warning("Invalid SIP config — cannot start")
            return
        }
        if socketFD >= 0 { Darwin.close(socketFD); socketFD = -1 }
        socketFD = Self.createBoundUdpSocket(port: UInt16(config.localPort), timeoutSec: 5)
        guard socketFD >= 0 else {
            Self.log.error("Failed to bind UDP \(self.config.localPort)")
            updateRegistration(.failed)
            return
        }
        localIp = LocalAddress.primaryIPv4() ?? "0.0.0.0"
        Self.log.info("SIP started on \(self.localIp, privacy: .public):\(self.config.localPort)")

        if let m = StunClient.discover(socketFD: socketFD) {
            publicIp = m.ip
            publicPort = m.port
            Self.log.info("STUN: \(self.publicIp, privacy: .public):\(self.publicPort)")
        } else {
            Self.log.warning("STUN discovery failed — will learn from server Via")
        }

        startReceiverLoop()
        register()
    }

    private func stopBlocking() {
        dispatchPrecondition(condition: .onQueue(ioQueue))
        guard !stopping else { return }
        stopping = true
        defer { stopping = false }

        if registrationState == .registered {
            keepaliveTimer?.cancel(); keepaliveTimer = nil
            reRegisterTimer?.cancel(); reRegisterTimer = nil
            receiverThread?.cancel(); receiverThread = nil
            unregister()
            // Synchronously wait for 200 OK or auth challenge, briefly.
            readUnregisterResponse()
        }

        receiverThread?.cancel(); receiverThread = nil
        keepaliveTimer?.cancel(); keepaliveTimer = nil
        reRegisterTimer?.cancel(); reRegisterTimer = nil
        rtpSession?.stop(); rtpSession = nil
        if rtpSocketFD >= 0 { Darwin.close(rtpSocketFD); rtpSocketFD = -1 }
        if socketFD >= 0 { Darwin.close(socketFD); socketFD = -1 }
        blfSubscriptions.removeAll()
        updateRegistration(.unregistered)
    }

    private func readUnregisterResponse() {
        guard socketFD >= 0 else { return }
        var tv = timeval(tv_sec: 2, tv_usec: 0)
        setsockopt(socketFD, SOL_SOCKET, SO_RCVTIMEO, &tv, socklen_t(MemoryLayout<timeval>.size))
        var buf = [UInt8](repeating: 0, count: 4096)
        let n = buf.withUnsafeMutableBufferPointer { Darwin.recv(socketFD, $0.baseAddress, $0.count, 0) }
        guard n > 0, let resp = String(bytes: buf[0..<n], encoding: .utf8) else { return }
        if resp.contains("401") || resp.contains("407") {
            parseAuthChallenge(resp)
            unregister()
        }
    }

    private static func createBoundUdpSocket(port: UInt16, timeoutSec: Int) -> Int32 {
        let fd = Darwin.socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP)
        guard fd >= 0 else { return -1 }
        var reuse: Int32 = 1
        setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &reuse, socklen_t(MemoryLayout<Int32>.size))
        setsockopt(fd, SOL_SOCKET, SO_REUSEPORT, &reuse, socklen_t(MemoryLayout<Int32>.size))
        var tv = timeval(tv_sec: timeoutSec, tv_usec: 0)
        setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, socklen_t(MemoryLayout<timeval>.size))

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

    // MARK: - Receiver loop

    private func startReceiverLoop() {
        let thread = Thread { [weak self] in
            self?.receiveLoop()
        }
        thread.name = "SipReceiver"
        thread.start()
        receiverThread = thread
    }

    private func receiveLoop() {
        var buffer = [UInt8](repeating: 0, count: 8192)
        while let thread = receiverThread, !thread.isCancelled {
            let fd = socketFD
            if fd < 0 { return }
            var from = sockaddr_storage()
            var fromLen = socklen_t(MemoryLayout<sockaddr_storage>.size)
            let n = buffer.withUnsafeMutableBufferPointer { buf -> Int in
                withUnsafeMutablePointer(to: &from) { addrPtr in
                    addrPtr.withMemoryRebound(to: sockaddr.self, capacity: 1) { saddr in
                        Darwin.recvfrom(fd, buf.baseAddress, buf.count, 0, saddr, &fromLen)
                    }
                }
            }
            if n <= 0 {
                // Timeout or error — loop and check cancellation.
                continue
            }
            guard let msg = String(bytes: buffer[0..<n], encoding: .utf8), !msg.isEmpty else { continue }
            ioQueue.async { [weak self] in
                guard let self else { return }
                if msg.hasPrefix("SIP/2.0") {
                    self.handleResponse(msg)
                } else {
                    self.handleRequest(msg)
                }
            }
        }
    }

    // MARK: - Response dispatch

    private func handleResponse(_ message: String) {
        guard let statusLine = message.split(separator: "\r\n").first else { return }
        let parts = statusLine.split(separator: " ", maxSplits: 2).map(String.init)
        guard parts.count >= 2, let statusCode = Int(parts[1]) else { return }
        guard let cseqLine = Self.extractHeader(message, name: "CSeq") else { return }
        let method = cseqLine.split(separator: " ").last.map(String.init) ?? ""
        let msgCallId = Self.extractHeader(message, name: "Call-ID") ?? ""

        switch method {
        case "REGISTER" where msgCallId == registerCallId:
            handleRegisterResponse(statusCode: statusCode, message: message)
        case "INVITE" where !consultCallId.isEmpty && msgCallId == consultCallId:
            handleConsultInviteResponse(statusCode: statusCode, message: message)
        case "INVITE" where msgCallId == currentCallId:
            handleInviteResponse(statusCode: statusCode, message: message)
        case "BYE":
            if statusCode == 200 { Self.log.info("BYE acknowledged") }
        case "REFER" where msgCallId == currentCallId:
            handleReferResponse(statusCode: statusCode, message: message)
        case "SUBSCRIBE" where blfSubscriptions[msgCallId] != nil:
            handleSubscribeResponse(statusCode: statusCode, message: message, callId: msgCallId)
        case "OPTIONS" where statusCode == 200:
            pendingKeepalives = 0
            checkNatChanged(message)
        default:
            break
        }
    }

    private func handleRegisterResponse(statusCode: Int, message: String) {
        switch statusCode {
        case 200:
            if stopping { return }
            let hadPublicAddr = !publicIp.isEmpty && publicPort > 0
            learnPublicAddressFromResponse(message)
            if !hadPublicAddr && !publicIp.isEmpty && publicPort > 0 {
                // Re-register with corrected Contact (now that we know our public address).
                Self.log.info("Re-registering with public address")
                registerCseq += 1
                let contactAddr = contactAddress()
                var headers: [(String, String)] = [
                    ("Contact", "<sip:\(config.username)@\(contactAddr);transport=\(config.transport.lowercased())>"),
                    ("Expires", String(Self.registerExpiresSeconds)),
                ]
                if !authNonce.isEmpty {
                    headers.append(("Authorization", buildAuthHeader(method: "REGISTER", uri: "sip:\(config.domain)")))
                }
                let req = buildRequest(
                    method: "REGISTER",
                    requestUri: "sip:\(config.domain)",
                    toUri: "sip:\(config.username)@\(config.domain)",
                    fromUri: "sip:\(config.username)@\(config.domain)",
                    callId: registerCallId,
                    cseq: registerCseq,
                    fromTag: registerFromTag,
                    toTag: "",
                    extraHeaders: headers,
                    body: "",
                    viaBranch: nil
                )
                sendSip(req)
                return
            }

            Self.log.info("Registered successfully")
            pendingKeepalives = 0
            updateRegistration(.registered)
            startKeepalive()
            scheduleReRegister()
        case 401, 407:
            parseAuthChallenge(message)
            registerWithAuth(isProxy: statusCode == 407)
        default:
            Self.log.warning("Registration failed: \(statusCode)")
            updateRegistration(.failed)
        }
    }

    private func handleInviteResponse(statusCode: Int, message: String) {
        // Ignore retransmitted responses for old CSeq.
        guard let cseqLine = Self.extractHeader(message, name: "CSeq") else { return }
        let cseqNum = Int(cseqLine.split(separator: " ").first.map(String.init) ?? "") ?? 0
        if cseqNum < currentCSeq {
            Self.log.debug("Ignoring retransmitted INVITE response for CSeq \(cseqNum)")
            return
        }

        switch statusCode {
        case 100:
            break
        case 180, 183:
            if callState == .calling {
                currentRemoteTag = Self.extractTag(message, headerName: "To") ?? currentRemoteTag
                setCallState(.ringing, number: remoteNumber, name: remoteName)
            }
        case 200:
            currentRemoteTag = Self.extractTag(message, headerName: "To") ?? currentRemoteTag
            parseSdp(message)
            sendAck(responseMessage: message, isNon2xx: false)
            inviteAuthAttempted = false
            if callState == .calling || callState == .ringing {
                startRtp()
                setCallState(.confirmed, number: remoteNumber, name: remoteName)
            }
        case 401, 407:
            if inviteAuthAttempted {
                Self.log.warning("INVITE auth failed repeatedly")
                sendAck(responseMessage: message, isNon2xx: true)
                if callState == .calling || callState == .ringing {
                    setCallState(.rejected, number: remoteNumber, name: remoteName)
                    ioQueue.asyncAfter(deadline: .now() + 1.5) { [weak self] in self?.resetCallState() }
                }
                return
            }
            inviteAuthAttempted = true
            parseAuthChallenge(message)
            sendAck(responseMessage: message, isNon2xx: true)
            currentCSeq += 1
            resendInviteWithAuth(isProxy: statusCode == 407)
        case 486, 600:
            sendAck(responseMessage: message, isNon2xx: true)
            setCallState(.busy, number: remoteNumber, name: remoteName)
            ioQueue.asyncAfter(deadline: .now() + 1.5) { [weak self] in self?.resetCallState() }
        case 400...699:
            Self.log.warning("Call rejected: \(statusCode)")
            sendAck(responseMessage: message, isNon2xx: true)
            setCallState(.rejected, number: remoteNumber, name: remoteName)
            ioQueue.asyncAfter(deadline: .now() + 1.5) { [weak self] in self?.resetCallState() }
        default:
            break
        }
    }

    private func handleConsultInviteResponse(statusCode: Int, message: String) {
        guard let cseqLine = Self.extractHeader(message, name: "CSeq") else { return }
        let cseqNum = Int(cseqLine.split(separator: " ").first.map(String.init) ?? "") ?? 0
        if cseqNum < consultCSeq { return }

        switch statusCode {
        case 100:
            break
        case 180, 183:
            if consultState == .calling {
                consultRemoteTag = Self.extractTag(message, headerName: "To") ?? consultRemoteTag
                DispatchQueue.main.async { self.consultState = .ringing }
            }
        case 200:
            consultRemoteTag = Self.extractTag(message, headerName: "To") ?? consultRemoteTag
            parseConsultSdp(message)
            sendConsultAck(responseMessage: message, isNon2xx: false)
            consultAuthAttempted = false
            if consultState == .calling || consultState == .ringing {
                startConsultRtp()
                DispatchQueue.main.async { self.consultState = .confirmed }
            }
        case 401, 407:
            if consultAuthAttempted {
                sendConsultAck(responseMessage: message, isNon2xx: true)
                cleanupConsultation()
                return
            }
            consultAuthAttempted = true
            parseAuthChallenge(message)
            sendConsultAck(responseMessage: message, isNon2xx: true)
            consultCSeq += 1
            let headerName = statusCode == 407 ? "Proxy-Authorization" : "Authorization"
            let authHeader = buildAuthHeader(method: "INVITE", uri: consultRemoteUri)
            let sdp = buildSdp(rtpPort: consultRtpPort, holdMode: false)
            let branch = generateBranch()
            consultInviteBranch = branch
            let req = buildRequest(
                method: "INVITE",
                requestUri: consultRemoteUri,
                toUri: consultRemoteUri,
                fromUri: "sip:\(config.username)@\(config.domain)",
                callId: consultCallId,
                cseq: consultCSeq,
                fromTag: consultLocalTag,
                toTag: "",
                extraHeaders: [
                    ("Contact", "<sip:\(config.username)@\(contactAddress())>"),
                    ("Content-Type", "application/sdp"),
                    ("Allow", "INVITE,ACK,BYE,CANCEL,OPTIONS,NOTIFY,REFER"),
                    (headerName, authHeader),
                ],
                body: sdp,
                viaBranch: branch
            )
            sendSip(req)
        case 400...699:
            sendConsultAck(responseMessage: message, isNon2xx: true)
            cleanupConsultation()
        default:
            break
        }
    }

    private func handleReferResponse(statusCode: Int, message: String) {
        switch statusCode {
        case 200, 202:
            Self.log.info("Transfer accepted (\(statusCode))")
        case 401, 407:
            if referAuthAttempted { return }
            referAuthAttempted = true
            parseAuthChallenge(message)
            currentCSeq += 1
            let headerName = statusCode == 407 ? "Proxy-Authorization" : "Authorization"
            let headerValue = buildAuthHeader(method: "REFER", uri: currentRemoteUri)

            if isConsulting && !consultCallId.isEmpty {
                let encodedCallId = consultCallId.replacingOccurrences(of: "@", with: "%40")
                let replacesValue = "\(encodedCallId)%3Bto-tag%3D\(consultRemoteTag)%3Bfrom-tag%3D\(consultLocalTag)"
                let referTo = "<sip:\(consultNumber)@\(config.domain)?Replaces=\(replacesValue)>"
                let req = buildRequest(
                    method: "REFER",
                    requestUri: currentRemoteUri,
                    toUri: currentRemoteUri,
                    fromUri: "sip:\(config.username)@\(config.domain)",
                    callId: currentCallId,
                    cseq: currentCSeq,
                    fromTag: currentLocalTag,
                    toTag: currentRemoteTag,
                    extraHeaders: [
                        ("Refer-To", referTo),
                        ("Referred-By", "<sip:\(config.username)@\(config.domain)>"),
                        ("Contact", "<sip:\(config.username)@\(contactAddress())>"),
                        ("Allow", "INVITE,ACK,BYE,CANCEL,OPTIONS,NOTIFY,REFER"),
                        (headerName, headerValue),
                    ],
                    body: "",
                    viaBranch: nil
                )
                sendSip(req)
            } else {
                sendRefer(target: pendingReferTarget, authHeader: (headerName, headerValue))
            }
        default:
            Self.log.warning("Transfer rejected: \(statusCode)")
        }
    }

    private func handleSubscribeResponse(statusCode: Int, message: String, callId: String) {
        guard var sub = blfSubscriptions[callId] else { return }
        switch statusCode {
        case 200, 202:
            sub.authAttempted = false
            blfSubscriptions[callId] = sub
        case 401, 407:
            if sub.authAttempted {
                blfSubscriptions.removeValue(forKey: callId)
                return
            }
            sub.authAttempted = true
            parseAuthChallenge(message)
            sub.cseq += 1
            blfSubscriptions[callId] = sub
            let authHeader = buildAuthHeader(method: "SUBSCRIBE", uri: "sip:\(sub.ext)@\(config.domain)")
            let headerName = statusCode == 407 ? "Proxy-Authorization" : "Authorization"
            let req = buildRequest(
                method: "SUBSCRIBE",
                requestUri: "sip:\(sub.ext)@\(config.domain)",
                toUri: "sip:\(sub.ext)@\(config.domain)",
                fromUri: "sip:\(config.username)@\(config.domain)",
                callId: callId,
                cseq: sub.cseq,
                fromTag: sub.fromTag,
                toTag: "",
                extraHeaders: [
                    ("Event", "dialog"),
                    ("Accept", "application/dialog-info+xml"),
                    ("Expires", "3600"),
                    ("Contact", "<sip:\(config.username)@\(contactAddress())>"),
                    (headerName, authHeader),
                ],
                body: "",
                viaBranch: nil
            )
            sendSip(req)
        default:
            blfSubscriptions.removeValue(forKey: callId)
        }
    }

    // MARK: - Request dispatch

    private func handleRequest(_ message: String) {
        let method = message.split(separator: " ").first.map(String.init) ?? ""
        let msgCallId = Self.extractHeader(message, name: "Call-ID") ?? ""

        switch method {
        case "INVITE":
            if msgCallId == currentCallId && (callState == .confirmed || callState == .hold) {
                handleReInvite(message)
            } else if msgCallId == currentCallId && callState == .incoming {
                // INVITE retransmission — re-send 180 Ringing.
                let resp = buildMirroredResponse(code: 180, reason: "Ringing", request: message, toTag: currentLocalTag)
                sendSip(resp)
            } else if !consultCallId.isEmpty && msgCallId == consultCallId {
                handleConsultReInvite(message)
            } else {
                handleIncomingInvite(message)
            }
        case "ACK":
            break
        case "BYE":
            let byeResp = buildMirroredResponse(code: 200, reason: "OK", request: message, toTag: "")
            sendSip(byeResp)
            if !consultCallId.isEmpty && msgCallId == consultCallId {
                cleanupConsultation()
            } else if msgCallId == currentCallId && callState != .idle {
                handleIncomingBye(message)
            }
        case "CANCEL":
            if msgCallId == currentCallId {
                handleIncomingCancel(message)
            }
        case "OPTIONS":
            let resp = buildMirroredResponse(code: 200, reason: "OK", request: message, toTag: "")
            sendSip(resp)
        case "NOTIFY":
            handleNotify(message)
        case "MESSAGE":
            handleIncomingMessage(message)
        default:
            break
        }
    }

    private func handleIncomingInvite(_ message: String) {
        if callState != .idle {
            let resp = buildMirroredResponse(code: 486, reason: "Busy Here", request: message, toTag: generateTag())
            sendSip(resp)
            return
        }
        incomingInviteMsg = message
        currentCallId = Self.extractHeader(message, name: "Call-ID") ?? ""
        currentLocalTag = generateTag()
        currentRemoteTag = Self.extractTag(message, headerName: "From") ?? ""
        currentCallDirection = "inbound"
        currentCSeq = 1

        let from = Self.extractHeader(message, name: "From") ?? ""
        let number = Self.extractNumberFromUri(from)
        let name = Self.extractNameFromHeader(from)

        let contactHeader = Self.extractHeader(message, name: "Contact") ?? ""
        if let match = contactHeader.range(of: #"<(sip:[^>]+)>"#, options: .regularExpression) {
            let full = String(contactHeader[match])
            currentRemoteUri = String(full.dropFirst().dropLast())
        } else {
            currentRemoteUri = "sip:\(number)@\(config.domain)"
        }

        parseSdp(message)
        localRtpPort = allocateRtpPort()

        setCallState(.incoming, number: number, name: name)

        let ringing = buildMirroredResponse(code: 180, reason: "Ringing", request: message, toTag: currentLocalTag)
        sendSip(ringing)
    }

    private func handleReInvite(_ message: String) {
        parseSdp(message)
        let sdp = buildSdp(rtpPort: localRtpPort, holdMode: callState == .hold)
        let viaHeader = Self.extractHeader(message, name: "Via") ?? ""
        let fromHeader = Self.extractHeader(message, name: "From") ?? ""
        let toHeader = Self.extractHeader(message, name: "To") ?? ""
        let cseqHeader = Self.extractHeader(message, name: "CSeq") ?? ""
        let toWithTag = (!toHeader.contains("tag=") && !currentLocalTag.isEmpty)
            ? "\(toHeader);tag=\(currentLocalTag)" : toHeader
        let sdpBytes = sdp.data(using: .utf8)?.count ?? 0
        let response =
            "SIP/2.0 200 OK\r\n" +
            "Via: \(viaHeader)\r\n" +
            "From: \(fromHeader)\r\n" +
            "To: \(toWithTag)\r\n" +
            "Call-ID: \(currentCallId)\r\n" +
            "CSeq: \(cseqHeader)\r\n" +
            "Contact: <sip:\(config.username)@\(contactAddress())>\r\n" +
            "Content-Type: application/sdp\r\n" +
            "User-Agent: MyLineTelecom-iOS/1.0\r\n" +
            "Content-Length: \(sdpBytes)\r\n\r\n" +
            sdp
        sendSip(response)
    }

    private func handleConsultReInvite(_ message: String) {
        parseConsultSdp(message)
        let sdp = buildSdp(rtpPort: consultRtpPort, holdMode: false)
        let viaHeader = Self.extractHeader(message, name: "Via") ?? ""
        let fromHeader = Self.extractHeader(message, name: "From") ?? ""
        let toHeader = Self.extractHeader(message, name: "To") ?? ""
        let cseqHeader = Self.extractHeader(message, name: "CSeq") ?? ""
        let toWithTag = (!toHeader.contains("tag=") && !consultLocalTag.isEmpty)
            ? "\(toHeader);tag=\(consultLocalTag)" : toHeader
        let sdpBytes = sdp.data(using: .utf8)?.count ?? 0
        let response =
            "SIP/2.0 200 OK\r\n" +
            "Via: \(viaHeader)\r\n" +
            "From: \(fromHeader)\r\n" +
            "To: \(toWithTag)\r\n" +
            "Call-ID: \(consultCallId)\r\n" +
            "CSeq: \(cseqHeader)\r\n" +
            "Contact: <sip:\(config.username)@\(contactAddress())>\r\n" +
            "Content-Type: application/sdp\r\n" +
            "User-Agent: MyLineTelecom-iOS/1.0\r\n" +
            "Content-Length: \(sdpBytes)\r\n\r\n" +
            sdp
        sendSip(response)
    }

    private func handleIncomingBye(_ message: String) {
        rtpSession?.stop()
        rtpSession = nil
        setCallState(.disconnected, number: remoteNumber, name: remoteName)
        ioQueue.asyncAfter(deadline: .now() + 1.0) { [weak self] in self?.resetCallState() }
    }

    private func handleIncomingCancel(_ message: String) {
        let cancelResp = buildMirroredResponse(code: 200, reason: "OK", request: message, toTag: "")
        sendSip(cancelResp)
        if let invite = incomingInviteMsg {
            let inviteResp = buildMirroredResponse(code: 487, reason: "Request Terminated", request: invite, toTag: currentLocalTag)
            sendSip(inviteResp)
        }
        setCallState(.disconnected, number: remoteNumber, name: remoteName)
        ioQueue.asyncAfter(deadline: .now() + 1.0) { [weak self] in self?.resetCallState() }
    }

    private func handleIncomingMessage(_ message: String) {
        let resp = buildMirroredResponse(code: 200, reason: "OK", request: message, toTag: "")
        sendSip(resp)
        let fromHeader = Self.extractHeader(message, name: "From") ?? ""
        let fromUser: String = {
            if let r = fromHeader.range(of: #"sip:([^@]+)@"#, options: .regularExpression) {
                let s = String(fromHeader[r])
                return String(s.dropFirst(4).dropLast())
            }
            return "unknown"
        }()
        let body = Self.extractSipBody(message)
        if body.isEmpty { return }
        Self.log.info("Received MESSAGE from \(fromUser, privacy: .public)")
        DispatchQueue.main.async { self.onMessageReceived?(fromUser, body) }
    }

    private func handleNotify(_ message: String) {
        let event = Self.extractHeader(message, name: "Event") ?? ""
        let notifyCallId = Self.extractHeader(message, name: "Call-ID") ?? ""

        if event.contains("refer") {
            let resp = buildMirroredResponse(code: 200, reason: "OK", request: message, toTag: "")
            sendSip(resp)
            let body = Self.extractSipBody(message)
            if body.contains("SIP/2.0 200") {
                Self.log.info("Transfer completed — hanging up our leg")
                ioQueue.asyncAfter(deadline: .now() + 0.5) { [weak self] in self?.hangup() }
            }
            return
        }

        if event.contains("dialog") && blfSubscriptions[notifyCallId] == nil {
            let resp = buildMirroredResponse(code: 481, reason: "Subscription does not exist", request: message, toTag: "")
            sendSip(resp)
            rejectedBlfCallIds.insert(notifyCallId)
            return
        }

        let resp = buildMirroredResponse(code: 200, reason: "OK", request: message, toTag: "")
        sendSip(resp)

        if !event.contains("dialog") { return }
        let body = Self.extractSipBody(message)
        if body.isEmpty { return }

        // Extract extension + state from dialog-info XML.
        guard let entMatch = body.range(of: #"entity="sip:([^@]+)@"#, options: .regularExpression) else { return }
        let entitySubstr = String(body[entMatch])
        guard let atRange = entitySubstr.range(of: "@") else { return }
        let ext = String(entitySubstr[entitySubstr.index(entitySubstr.startIndex, offsetBy: "entity=\"sip:".count)..<atRange.lowerBound])

        var bestState: BlfState = .idle
        // Rough dialog parsing — the full XML can have multiple <dialog> blocks.
        let dialogPattern = #"<dialog\s[^>]*>(.*?)</dialog>"#
        let regex = try? NSRegularExpression(pattern: dialogPattern, options: [.dotMatchesLineSeparators])
        let nsbody = body as NSString
        let matches = regex?.matches(in: body, range: NSRange(location: 0, length: nsbody.length)) ?? []
        if matches.isEmpty {
            bestState = .idle
        } else {
            for m in matches {
                let content = nsbody.substring(with: m.range)
                let stateLower = Self.captureGroup(content, pattern: #"<state>([^<]+)</state>"#)?.lowercased()
                let attrLower = Self.captureGroup(content, pattern: #"<dialog\s[^>]+state="([^"]+)""#)?.lowercased()
                if attrLower == "terminated" || stateLower == "terminated" { continue }

                let thisState: BlfState
                switch stateLower {
                case "confirmed", "trying":
                    thisState = .busy
                case "early", "proceeding":
                    thisState = .ringing
                default:
                    switch attrLower {
                    case "confirmed", "trying": thisState = .busy
                    case "early": thisState = .ringing
                    default: thisState = .idle
                    }
                }
                if thisState == .busy || (thisState == .ringing && bestState != .busy) {
                    bestState = thisState
                }
            }
        }

        let newStates = blfStates.merging([ext: bestState]) { _, new in new }
        DispatchQueue.main.async { self.blfStates = newStates }
    }

    // MARK: - REGISTER

    private func register() {
        dispatchPrecondition(condition: .onQueue(ioQueue))
        registerCallId = generateCallId()
        registerFromTag = generateTag()
        registerCseq = 1
        let contactAddr = contactAddress()
        let req = buildRequest(
            method: "REGISTER",
            requestUri: "sip:\(config.domain)",
            toUri: "sip:\(config.username)@\(config.domain)",
            fromUri: "sip:\(config.username)@\(config.domain)",
            callId: registerCallId,
            cseq: registerCseq,
            fromTag: registerFromTag,
            toTag: "",
            extraHeaders: [
                ("Contact", "<sip:\(config.username)@\(contactAddr);transport=\(config.transport.lowercased())>"),
                ("Expires", String(Self.registerExpiresSeconds)),
            ],
            body: "",
            viaBranch: nil
        )
        updateRegistration(.registering)
        sendSip(req)
    }

    private func unregister() {
        registerCseq += 1
        let contactAddr = contactAddress()
        var headers: [(String, String)] = [
            ("Contact", "<sip:\(config.username)@\(contactAddr);transport=\(config.transport.lowercased())>"),
            ("Expires", "0"),
        ]
        if !authNonce.isEmpty {
            headers.append(("Authorization", buildAuthHeader(method: "REGISTER", uri: "sip:\(config.domain)")))
        }
        let req = buildRequest(
            method: "REGISTER",
            requestUri: "sip:\(config.domain)",
            toUri: "sip:\(config.username)@\(config.domain)",
            fromUri: "sip:\(config.username)@\(config.domain)",
            callId: registerCallId,
            cseq: registerCseq,
            fromTag: registerFromTag,
            toTag: "",
            extraHeaders: headers,
            body: "",
            viaBranch: nil
        )
        sendSip(req)
    }

    private func registerWithAuth(isProxy: Bool) {
        registerCseq += 1
        let contactAddr = contactAddress()
        let authHeader = buildAuthHeader(method: "REGISTER", uri: "sip:\(config.domain)")
        let headerName = isProxy ? "Proxy-Authorization" : "Authorization"
        let req = buildRequest(
            method: "REGISTER",
            requestUri: "sip:\(config.domain)",
            toUri: "sip:\(config.username)@\(config.domain)",
            fromUri: "sip:\(config.username)@\(config.domain)",
            callId: registerCallId,
            cseq: registerCseq,
            fromTag: registerFromTag,
            toTag: "",
            extraHeaders: [
                ("Contact", "<sip:\(config.username)@\(contactAddr);transport=\(config.transport.lowercased())>"),
                ("Expires", String(Self.registerExpiresSeconds)),
                (headerName, authHeader),
            ],
            body: "",
            viaBranch: nil
        )
        sendSip(req)
    }

    // MARK: - REFER helper

    private func sendRefer(target: String, authHeader: (String, String)?) {
        let referTo = "sip:\(target)@\(config.domain)"
        var headers: [(String, String)] = [
            ("Refer-To", "<\(referTo)>"),
            ("Referred-By", "<sip:\(config.username)@\(config.domain)>"),
            ("Contact", "<sip:\(config.username)@\(contactAddress())>"),
            ("Allow", "INVITE,ACK,BYE,CANCEL,OPTIONS,NOTIFY,REFER"),
        ]
        if let a = authHeader { headers.append(a) }
        let req = buildRequest(
            method: "REFER",
            requestUri: currentRemoteUri,
            toUri: currentRemoteUri,
            fromUri: "sip:\(config.username)@\(config.domain)",
            callId: currentCallId,
            cseq: currentCSeq,
            fromTag: currentLocalTag,
            toTag: currentRemoteTag,
            extraHeaders: headers,
            body: "",
            viaBranch: nil
        )
        sendSip(req)
    }

    // MARK: - ACK

    private func sendAck(responseMessage: String, isNon2xx: Bool) {
        let toHeader = Self.extractHeader(responseMessage, name: "To") ?? ""
        let remoteTag = Self.tagInHeaderValue(toHeader) ?? currentRemoteTag
        let requestUri = currentRemoteUri.isEmpty ? "sip:\(config.domain)" : currentRemoteUri
        let branch = isNon2xx ? (Self.extractViaBranch(responseMessage) ?? "") : nil
        let req = buildRequest(
            method: "ACK",
            requestUri: requestUri,
            toUri: requestUri,
            fromUri: "sip:\(config.username)@\(config.domain)",
            callId: currentCallId,
            cseq: currentCSeq,
            fromTag: currentLocalTag,
            toTag: remoteTag,
            extraHeaders: [],
            body: "",
            viaBranch: branch
        )
        sendSip(req)
    }

    private func sendConsultAck(responseMessage: String, isNon2xx: Bool) {
        let toHeader = Self.extractHeader(responseMessage, name: "To") ?? ""
        let remoteTag = Self.tagInHeaderValue(toHeader) ?? consultRemoteTag
        let branch = isNon2xx ? (Self.extractViaBranch(responseMessage) ?? "") : nil
        let req = buildRequest(
            method: "ACK",
            requestUri: consultRemoteUri,
            toUri: consultRemoteUri,
            fromUri: "sip:\(config.username)@\(config.domain)",
            callId: consultCallId,
            cseq: consultCSeq,
            fromTag: consultLocalTag,
            toTag: remoteTag,
            extraHeaders: [],
            body: "",
            viaBranch: branch
        )
        sendSip(req)
    }

    private func resendInviteWithAuth(isProxy: Bool) {
        let contactAddr = contactAddress()
        let authHeader = buildAuthHeader(method: "INVITE", uri: currentRemoteUri)
        let headerName = isProxy ? "Proxy-Authorization" : "Authorization"
        let sdp = buildSdp(rtpPort: localRtpPort, holdMode: pendingHoldMode)
        let branch = generateBranch()
        currentInviteBranch = branch
        let req = buildRequest(
            method: "INVITE",
            requestUri: currentRemoteUri,
            toUri: currentRemoteUri,
            fromUri: "sip:\(config.username)@\(config.domain)",
            callId: currentCallId,
            cseq: currentCSeq,
            fromTag: currentLocalTag,
            toTag: currentRemoteTag,
            extraHeaders: [
                ("Contact", "<sip:\(config.username)@\(contactAddr)>"),
                ("Content-Type", "application/sdp"),
                ("Allow", "INVITE,ACK,BYE,CANCEL,OPTIONS,NOTIFY,REFER"),
                (headerName, authHeader),
            ],
            body: sdp,
            viaBranch: branch
        )
        sendSip(req)
    }

    // MARK: - Request / response builders

    private func buildRequest(
        method: String,
        requestUri: String,
        toUri: String,
        fromUri: String,
        callId: String,
        cseq: Int,
        fromTag: String,
        toTag: String,
        extraHeaders: [(String, String)],
        body: String,
        viaBranch: String?
    ) -> String {
        let branch = (viaBranch?.isEmpty == false) ? viaBranch! : generateBranch()
        var via = "SIP/2.0/\(config.transport) \(localIp):\(config.localPort);branch=\(branch)"
        if config.rport { via += ";rport" }
        let displayName = config.displayName.isEmpty ? "" : "\"\(config.displayName)\" "
        let toTagStr = toTag.isEmpty ? "" : ";tag=\(toTag)"

        var sb = ""
        sb += "\(method) \(requestUri) SIP/2.0\r\n"
        sb += "Via: \(via)\r\n"
        sb += "Max-Forwards: 70\r\n"
        sb += "From: \(displayName)<\(fromUri)>;tag=\(fromTag)\r\n"
        sb += "To: <\(toUri)>\(toTagStr)\r\n"
        sb += "Call-ID: \(callId)\r\n"
        sb += "CSeq: \(cseq) \(method)\r\n"
        sb += "User-Agent: MyLineTelecom-iOS/1.0\r\n"
        for (k, v) in extraHeaders {
            sb += "\(k): \(v)\r\n"
        }
        let bodyBytes = body.data(using: .utf8)?.count ?? 0
        sb += "Content-Length: \(bodyBytes)\r\n\r\n"
        if !body.isEmpty { sb += body }
        return sb
    }

    private func buildMirroredResponse(code: Int, reason: String, request: String, toTag: String) -> String {
        let via = Self.extractHeader(request, name: "Via") ?? ""
        let from = Self.extractHeader(request, name: "From") ?? ""
        let to = Self.extractHeader(request, name: "To") ?? ""
        let callId = Self.extractHeader(request, name: "Call-ID") ?? ""
        let cseq = Self.extractHeader(request, name: "CSeq") ?? ""
        let toWithTag = (!toTag.isEmpty && !to.contains("tag=")) ? "\(to);tag=\(toTag)" : to
        return
            "SIP/2.0 \(code) \(reason)\r\n" +
            "Via: \(via)\r\n" +
            "From: \(from)\r\n" +
            "To: \(toWithTag)\r\n" +
            "Call-ID: \(callId)\r\n" +
            "CSeq: \(cseq)\r\n" +
            "User-Agent: MyLineTelecom-iOS/1.0\r\n" +
            "Content-Length: 0\r\n\r\n"
    }

    // MARK: - SDP

    private func buildSdp(rtpPort: Int, holdMode: Bool) -> String {
        let ip = publicIp.isEmpty ? localIp : publicIp
        let sessionId = Int(Date().timeIntervalSince1970)
        let mode = holdMode ? "a=sendonly" : "a=sendrecv"
        return
            "v=0\r\n" +
            "o=\(config.username) \(sessionId) \(sessionId) IN IP4 \(ip)\r\n" +
            "s=MyLineTelecom\r\n" +
            "c=IN IP4 \(ip)\r\n" +
            "t=0 0\r\n" +
            "m=audio \(rtpPort) RTP/AVP 0 18 3 101\r\n" +
            "a=rtpmap:0 PCMU/8000\r\n" +
            "a=rtpmap:18 G729/8000\r\n" +
            "a=fmtp:18 annexb=no\r\n" +
            "a=rtpmap:3 GSM/8000\r\n" +
            "a=rtpmap:101 telephone-event/8000\r\n" +
            "a=fmtp:101 0-16\r\n" +
            "a=ptime:20\r\n" +
            "\(mode)\r\n"
    }

    private func parseSdp(_ message: String) {
        let body = Self.extractSipBody(message)
        if body.isEmpty { return }
        for line in body.split(separator: "\r\n").map(String.init) + body.split(separator: "\n").map(String.init) {
            if line.hasPrefix("c=IN IP4 ") {
                remoteRtpHost = String(line.dropFirst("c=IN IP4 ".count)).trimmingCharacters(in: .whitespaces)
            } else if line.hasPrefix("m=audio ") {
                let parts = line.split(separator: " ").map(String.init)
                if parts.count >= 2 { remoteRtpPort = Int(parts[1]) ?? 0 }
                if parts.count >= 4 {
                    for i in 3..<parts.count {
                        if let pt = Int(parts[i].trimmingCharacters(in: .whitespaces)), [0, 3, 8, 18].contains(pt) {
                            negotiatedCodec = pt
                            break
                        }
                    }
                }
            }
        }
    }

    private func parseConsultSdp(_ message: String) {
        let body = Self.extractSipBody(message)
        if body.isEmpty { return }
        for line in body.split(separator: "\r\n").map(String.init) {
            if line.hasPrefix("c=IN IP4 ") {
                consultRemoteRtpHost = String(line.dropFirst("c=IN IP4 ".count)).trimmingCharacters(in: .whitespaces)
            } else if line.hasPrefix("m=audio ") {
                let parts = line.split(separator: " ").map(String.init)
                if parts.count >= 2 { consultRemoteRtpPort = Int(parts[1]) ?? 0 }
                if parts.count >= 4 {
                    for i in 3..<parts.count {
                        if let pt = Int(parts[i]), [0, 3, 8, 18].contains(pt) {
                            consultNegotiatedCodec = pt
                            break
                        }
                    }
                }
            }
        }
    }

    // MARK: - RTP launch

    private func startRtp() {
        if remoteRtpHost.isEmpty || remoteRtpPort == 0 {
            Self.log.error("Cannot start RTP: no remote endpoint")
            return
        }
        rtpSession?.stop()
        let existing = rtpSocketFD
        rtpSocketFD = -1 // transfer ownership to RtpSession
        rtpSession = RtpSession(
            localPort: UInt16(localRtpPort),
            remoteHost: remoteRtpHost,
            remotePort: UInt16(remoteRtpPort),
            existingSocketFD: existing,
            codecType: UInt8(negotiatedCodec)
        )
        rtpSession?.start()
        Self.log.info("RTP started: local \(self.localRtpPort) -> \(self.remoteRtpHost, privacy: .public):\(self.remoteRtpPort)")
    }

    private func startConsultRtp() {
        if consultRemoteRtpHost.isEmpty || consultRemoteRtpPort == 0 { return }
        consultRtpSession?.stop()
        let existing = consultRtpSocketFD
        consultRtpSocketFD = -1
        consultRtpSession = RtpSession(
            localPort: UInt16(consultRtpPort),
            remoteHost: consultRemoteRtpHost,
            remotePort: UInt16(consultRemoteRtpPort),
            existingSocketFD: existing,
            codecType: UInt8(consultNegotiatedCodec)
        )
        consultRtpSession?.start()
    }

    // MARK: - Auth (RFC 2617 digest)

    private func parseAuthChallenge(_ message: String) {
        guard let header = Self.extractHeader(message, name: "WWW-Authenticate")
                ?? Self.extractHeader(message, name: "Proxy-Authenticate") else { return }

        authRealm = Self.extractQuotedParam(header, param: "realm")
        authNonce = Self.extractQuotedParam(header, param: "nonce")
        authOpaque = Self.extractQuotedParam(header, param: "opaque")
        let algo = Self.extractUnquotedParam(header, param: "algorithm")
        authAlgorithm = algo.isEmpty ? "MD5" : algo
        let qopQuoted = Self.extractQuotedParam(header, param: "qop")
        authQop = qopQuoted.isEmpty ? Self.extractUnquotedParam(header, param: "qop") : qopQuoted
        if !authQop.isEmpty {
            authNonceCount = 1
            authCNonce = String(Int.random(in: 100000...999999))
        }
    }

    private func buildAuthHeader(method: String, uri: String) -> String {
        let ha1 = Self.md5Hex("\(config.username):\(authRealm):\(config.password)")
        let ha2 = Self.md5Hex("\(method):\(uri)")
        let response: String
        let nc = String(format: "%08x", authNonceCount)
        if !authQop.isEmpty {
            response = Self.md5Hex("\(ha1):\(authNonce):\(nc):\(authCNonce):auth:\(ha2)")
            authNonceCount += 1
        } else {
            response = Self.md5Hex("\(ha1):\(authNonce):\(ha2)")
        }

        var sb = "Digest username=\"\(config.username)\", realm=\"\(authRealm)\", nonce=\"\(authNonce)\", uri=\"\(uri)\", response=\"\(response)\""
        if !authOpaque.isEmpty { sb += ", opaque=\"\(authOpaque)\"" }
        sb += ", algorithm=\(authAlgorithm)"
        if !authQop.isEmpty {
            let lastNc = String(format: "%08x", authNonceCount - 1)
            sb += ", qop=auth, nc=\(lastNc), cnonce=\"\(authCNonce)\""
        }
        return sb
    }

    // MARK: - Networking send

    private func sendSip(_ message: String) {
        dispatchPrecondition(condition: .onQueue(ioQueue))
        guard socketFD >= 0 else {
            Self.log.error("sendSip: no socket")
            return
        }
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
        guard getaddrinfo(config.domain, String(config.port), &hints, &res) == 0, let info = res else {
            Self.log.error("getaddrinfo failed for \(self.config.domain, privacy: .public)")
            return
        }
        defer { freeaddrinfo(res) }

        let data = Data(message.utf8)
        _ = data.withUnsafeBytes { raw in
            sendto(socketFD, raw.baseAddress, data.count, 0, info.pointee.ai_addr, info.pointee.ai_addrlen)
        }
    }

    // MARK: - STUN / NAT helpers

    private func learnPublicAddressFromResponse(_ message: String) {
        guard let via = Self.extractHeader(message, name: "Via") else { return }
        guard let ip = Self.captureGroup(via, pattern: #"received=([^;\s]+)"#) else { return }
        guard let portStr = Self.captureGroup(via, pattern: #"rport=([0-9]+)"#),
              let port = UInt16(portStr) else { return }
        if publicIp.isEmpty || publicPort == 0 {
            publicIp = ip
            publicPort = port
            Self.log.info("Learned public address from server: \(ip, privacy: .public):\(port)")
        }
    }

    private func checkNatChanged(_ message: String) {
        guard let via = Self.extractHeader(message, name: "Via") else { return }
        guard let receivedIp = Self.captureGroup(via, pattern: #"received=([^;\s]+)"#) else { return }
        guard let rport = Self.captureGroup(via, pattern: #"rport=([0-9]+)"#).flatMap(UInt16.init) else { return }
        if !publicIp.isEmpty && (receivedIp != publicIp || rport != publicPort) {
            Self.log.warning("NAT mapping changed — restarting")
            restartForNetworkChange()
        }
    }

    // MARK: - Keepalive / re-register

    private func startKeepalive() {
        keepaliveTimer?.cancel()
        pendingKeepalives = 0
        let timer = DispatchSource.makeTimerSource(queue: ioQueue)
        timer.schedule(deadline: .now() + .seconds(Self.keepaliveIntervalSeconds),
                       repeating: .seconds(Self.keepaliveIntervalSeconds))
        timer.setEventHandler { [weak self] in
            guard let self, self.registrationState == .registered else { return }
            if self.pendingKeepalives >= 2 {
                Self.log.warning("Keepalive unanswered — restarting")
                self.restartForNetworkChange()
                return
            }
            self.pendingKeepalives += 1
            let req = self.buildRequest(
                method: "OPTIONS",
                requestUri: "sip:\(self.config.domain)",
                toUri: "sip:\(self.config.domain)",
                fromUri: "sip:\(self.config.username)@\(self.config.domain)",
                callId: self.generateCallId(),
                cseq: 1,
                fromTag: self.generateTag(),
                toTag: "",
                extraHeaders: [],
                body: "",
                viaBranch: nil
            )
            self.sendSip(req)
        }
        timer.resume()
        keepaliveTimer = timer
    }

    private func scheduleReRegister() {
        reRegisterTimer?.cancel()
        let delayMs = Self.registerExpiresSeconds * 1000 - Self.reRegisterBeforeMs
        let timer = DispatchSource.makeTimerSource(queue: ioQueue)
        timer.schedule(deadline: .now() + .milliseconds(delayMs))
        timer.setEventHandler { [weak self] in
            guard let self, self.registrationState == .registered else { return }
            Self.log.info("Re-registering")
            self.registerWithAuth(isProxy: false)
        }
        timer.resume()
        reRegisterTimer = timer
    }

    // MARK: - Helpers: state + IDs + addresses

    private func resetCallState() {
        currentCallId = ""
        currentLocalTag = ""
        currentRemoteTag = ""
        currentRemoteUri = ""
        currentCallDirection = ""
        currentCSeq = 1
        currentInviteBranch = ""
        incomingInviteMsg = nil
        inviteAuthAttempted = false
        pendingHoldMode = false
        pendingReferTarget = ""
        referAuthAttempted = false
        localRtpPort = 0
        remoteRtpHost = ""
        remoteRtpPort = 0
        negotiatedCodec = 0
        rtpSession?.stop(); rtpSession = nil
        if rtpSocketFD >= 0 { Darwin.close(rtpSocketFD); rtpSocketFD = -1 }
        cleanupConsultation()
        setCallState(.idle, number: "", name: "")
    }

    private func cleanupConsultation() {
        consultRtpSession?.stop(); consultRtpSession = nil
        if consultRtpSocketFD >= 0 { Darwin.close(consultRtpSocketFD); consultRtpSocketFD = -1 }
        consultCallId = ""
        consultLocalTag = ""
        consultRemoteTag = ""
        consultRemoteUri = ""
        consultCSeq = 1
        consultInviteBranch = ""
        consultAuthAttempted = false
        consultRtpPort = 0
        consultRemoteRtpHost = ""
        consultRemoteRtpPort = 0
        consultNegotiatedCodec = 0
        DispatchQueue.main.async {
            self.isConsulting = false
            self.consultState = .idle
            self.consultNumber = ""
        }
    }

    private func contactAddress() -> String {
        let ip = publicIp.isEmpty ? localIp : publicIp
        let port = publicPort > 0 ? Int(publicPort) : config.localPort
        return "\(ip):\(port)"
    }

    private func allocateRtpPort() -> Int {
        if rtpSocketFD >= 0 { Darwin.close(rtpSocketFD); rtpSocketFD = -1 }
        for port in stride(from: 10000, through: 20000, by: 2) {
            let fd = Self.createBoundUdpSocket(port: UInt16(port), timeoutSec: 0)
            if fd >= 0 { rtpSocketFD = fd; return port }
        }
        // Fallback: let the OS pick.
        let fd = Self.createBoundUdpSocket(port: 0, timeoutSec: 0)
        rtpSocketFD = fd
        var addr = sockaddr_in()
        var len = socklen_t(MemoryLayout<sockaddr_in>.size)
        withUnsafeMutablePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                _ = getsockname(fd, $0, &len)
            }
        }
        return Int(UInt16(bigEndian: addr.sin_port))
    }

    private func allocateConsultRtpPort() -> Int {
        if consultRtpSocketFD >= 0 { Darwin.close(consultRtpSocketFD); consultRtpSocketFD = -1 }
        for port in stride(from: 10002, through: 20000, by: 2) {
            if port == localRtpPort { continue }
            let fd = Self.createBoundUdpSocket(port: UInt16(port), timeoutSec: 0)
            if fd >= 0 { consultRtpSocketFD = fd; return port }
        }
        let fd = Self.createBoundUdpSocket(port: 0, timeoutSec: 0)
        consultRtpSocketFD = fd
        var addr = sockaddr_in()
        var len = socklen_t(MemoryLayout<sockaddr_in>.size)
        withUnsafeMutablePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                _ = getsockname(fd, $0, &len)
            }
        }
        return Int(UInt16(bigEndian: addr.sin_port))
    }

    private func generateCallId() -> String {
        "\(Int.random(in: 100000...999999))@\(localIp)"
    }

    private func generateTag() -> String {
        String(Int.random(in: 100000...999999))
    }

    private func generateBranch() -> String {
        "z9hG4bK\(Int.random(in: 100000...999999))"
    }

    private func setCallState(_ state: CallState, number: String, name: String) {
        DispatchQueue.main.async {
            self.callState = state
            self.remoteNumber = number
            self.remoteName = name
            self.onCallStateChanged?(state, number, name)
        }
    }

    private func updateRegistration(_ state: RegistrationState) {
        DispatchQueue.main.async {
            self.registrationState = state
            self.onRegistrationChanged?(state)
        }
    }

    // MARK: - Static parsers / regex helpers

    static func extractHeader(_ message: String, name: String) -> String? {
        let prefix = name.lowercased() + ":"
        for raw in message.split(separator: "\r\n") {
            let line = String(raw)
            if line.lowercased().hasPrefix(prefix) {
                return String(line.dropFirst(prefix.count)).trimmingCharacters(in: .whitespaces)
            }
        }
        return nil
    }

    static func extractTag(_ message: String, headerName: String) -> String? {
        guard let h = extractHeader(message, name: headerName) else { return nil }
        return tagInHeaderValue(h)
    }

    static func tagInHeaderValue(_ value: String) -> String? {
        captureGroup(value, pattern: #"tag=([^;>\s]+)"#)
    }

    static func extractNumberFromUri(_ text: String) -> String {
        if let m = captureGroup(text, pattern: #"sip:([^@>]+)@"#) { return m }
        if let m = captureGroup(text, pattern: #"sip:([^>]+)"#) { return m }
        return ""
    }

    static func extractNameFromHeader(_ header: String) -> String {
        captureGroup(header, pattern: #""([^"]+)""#) ?? ""
    }

    static func extractQuotedParam(_ header: String, param: String) -> String {
        captureGroup(header, pattern: "\(param)=\"([^\"]+)\"") ?? ""
    }

    static func extractUnquotedParam(_ header: String, param: String) -> String {
        captureGroup(header, pattern: "\(param)=([^,\\s]+)") ?? ""
    }

    static func extractViaBranch(_ message: String) -> String? {
        guard let via = extractHeader(message, name: "Via") else { return nil }
        return captureGroup(via, pattern: #"branch=([^;,\s]+)"#)
    }

    static func extractSipBody(_ message: String) -> String {
        if let r = message.range(of: "\r\n\r\n") { return String(message[r.upperBound...]) }
        if let r = message.range(of: "\n\n") { return String(message[r.upperBound...]) }
        return ""
    }

    static func captureGroup(_ input: String, pattern: String) -> String? {
        guard let regex = try? NSRegularExpression(pattern: pattern, options: [.dotMatchesLineSeparators]) else {
            return nil
        }
        let ns = input as NSString
        guard let match = regex.firstMatch(in: input, range: NSRange(location: 0, length: ns.length)),
              match.numberOfRanges >= 2,
              match.range(at: 1).location != NSNotFound else {
            return nil
        }
        return ns.substring(with: match.range(at: 1))
    }

    static func md5Hex(_ input: String) -> String {
        Insecure.MD5.hash(data: Data(input.utf8)).map { String(format: "%02x", $0) }.joined()
    }
}

// MARK: - Primary IPv4 address helper

enum LocalAddress {
    /// Best-effort primary IPv4 address — prefers Wi-Fi (en0), falls back to cellular (pdp_ip0).
    static func primaryIPv4() -> String? {
        var wifi: String?
        var cellular: String?
        var fallback: String?
        var ifaddr: UnsafeMutablePointer<ifaddrs>?
        guard getifaddrs(&ifaddr) == 0, let first = ifaddr else { return nil }
        defer { freeifaddrs(ifaddr) }

        var ptr: UnsafeMutablePointer<ifaddrs>? = first
        while let cur = ptr {
            let flags = Int32(cur.pointee.ifa_flags)
            let isUp = (flags & IFF_UP) == IFF_UP
            let isLoopback = (flags & IFF_LOOPBACK) == IFF_LOOPBACK
            if isUp && !isLoopback,
               let saddr = cur.pointee.ifa_addr,
               saddr.pointee.sa_family == UInt8(AF_INET) {
                let name = String(cString: cur.pointee.ifa_name)
                var host = [CChar](repeating: 0, count: Int(NI_MAXHOST))
                getnameinfo(saddr, socklen_t(saddr.pointee.sa_len),
                            &host, socklen_t(host.count),
                            nil, 0, NI_NUMERICHOST)
                let ip = String(cString: host)
                if name == "en0" || name == "en1" {
                    wifi = ip
                } else if name.hasPrefix("pdp_ip") {
                    cellular = ip
                } else if fallback == nil {
                    fallback = ip
                }
            }
            ptr = cur.pointee.ifa_next
        }
        return wifi ?? cellular ?? fallback
    }
}
