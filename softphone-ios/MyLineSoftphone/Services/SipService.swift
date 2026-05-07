import Foundation
import CallKit
import PushKit
import AVFoundation
import Combine
import UserNotifications
import UIKit
import os.log

/// Top-level service that owns the `SipHandler` singleton and bridges iOS system
/// integrations (`CallKit`, `PushKit`, audio session routing, network reachability).
///
/// Replaces Android's `SipService.kt` foreground service. On iOS there is no
/// equivalent to a foreground service — app lifetime is controlled by:
/// * `UIBackgroundModes = audio` — keeps us alive during an active call.
/// * `UIBackgroundModes = voip` — (legacy; mostly for PushKit entitlement).
/// * **PushKit VoIP push** — wakes the app on incoming calls when suspended.
///
/// For the first iteration without a push server, incoming calls only work while
/// the app is foregrounded or in a live call. This matches the v1 behavior we
/// discussed.
@MainActor
final class SipService: NSObject, ObservableObject {
    static let shared = SipService()

    private static let log = Logger(subsystem: "com.mylinetelecom.softphone", category: "SipService")

    let sipHandler = SipHandler()

    // Republished so ContentView (which observes SipService) re-renders on call/registration changes.
    @Published private(set) var callState: CallState = .idle
    @Published private(set) var registrationState: RegistrationState = .unregistered

    // CallKit
    private let provider: CXProvider
    private let callController = CXCallController()
    private var activeCallUUID: UUID?
    private var isOutgoingCall: Bool = false

    // PushKit
    private let pushRegistry = PKPushRegistry(queue: .main)

    private var cancellables = Set<AnyCancellable>()

    // Ringback tone played locally on outgoing calls before the remote party answers.
    private var ringbackPlayer: AVAudioPlayer?
    // Busy tone played locally when the remote party is busy.
    private var busyPlayer: AVAudioPlayer?

    // Silent-audio keepalive removed — see handleAppBackground() comment.
    // Background incoming calls require PushKit (server-side bridge needs deploy).

    override init() {
        let config = CXProviderConfiguration(localizedName: "MyLine")
        config.supportsVideo = false
        config.maximumCallsPerCallGroup = 1
        config.supportedHandleTypes = [.phoneNumber, .generic]
        config.includesCallsInRecents = false    // Keep SIP calls out of Phone app's recents
        self.provider = CXProvider(configuration: config)
        super.init()
        self.provider.setDelegate(self, queue: nil)

        pushRegistry.delegate = self
        pushRegistry.desiredPushTypes = [.voIP]

        // Bridge SipHandler state changes into CallKit.
        sipHandler.onCallStateChanged = { [weak self] state, number, name in
            Task { @MainActor in
                self?.handleCallStateChanged(state: state, number: number, name: name)
            }
        }

        // Forward registration state so SettingsScreen re-renders.
        sipHandler.onRegistrationChanged = { [weak self] state in
            Task { @MainActor in
                self?.registrationState = state
            }
        }

        // Save incoming SIP MESSAGE to DB and show a local notification.
        // Mirrors Android SipService.kt onMessageReceived block.
        sipHandler.onMessageReceived = { from, body in
            Task {
                let msgType = from.hasPrefix("+") ? "whatsapp" : "sms"
                let msg = ChatMessage(
                    remoteNumber: from,
                    body: body,
                    isOutgoing: false,
                    timestamp: Date(),
                    status: .received,
                    messageType: msgType
                )

                // Dedup: skip if identical inbound arrived within last 5 seconds.
                let dao = ChatMessageDao()
                let since = Date(timeIntervalSinceNow: -5)
                let dupes = (try? dao.countRecentDuplicates(from: from, body: body, since: since)) ?? 0
                guard dupes == 0 else {
                    Self.log.warning("Duplicate inbound message from \(from, privacy: .public) — ignored")
                    return
                }

                _ = try? dao.insert(msg)
                Self.log.info("Saved incoming \(msgType) message from \(from, privacy: .public)")
                Self.showMessageNotification(from: from, body: body, msgType: msgType)
            }
        }
    }

    // MARK: - Service lifecycle

    /// Call from the app's `.onAppear` / `@main` struct after loading `SipConfig`.
    func start(with config: SipConfig) {
        sipHandler.configure(config)
        sipHandler.start()
    }

    func stop() {
        sipHandler.stop()
    }

    /// Toggle earpiece ↔ speaker during an active call.
    /// Uses AVAudioSession.overrideOutputAudioPort which works while CallKit
    /// owns the session (.voiceChat defaults to earpiece; speaker overrides it).
    func setSpeaker(_ on: Bool) {
        // Delegate to SipHandler/RtpSession on the ioQueue so the audio engine
        // (which lives on that queue) can be restarted on the SAME thread right
        // after the route override.  Calling overrideOutputAudioPort on the main
        // thread without restarting the engine kills audio because vpio reconfigures.
        sipHandler.setSpeakerOutput(on)
        Self.log.info("Speaker request: \(on ? "ON" : "OFF (earpiece)", privacy: .public)")
    }

    // MARK: - App lifecycle (background / foreground)

    /// Call when `scenePhase` becomes `.background`.
    ///
    /// Background keepalive via silent audio was REMOVED — having a second
    /// AVAudioEngine/AVAudioPlayer hold the global AVAudioSession in .playback
    /// mode left iOS unable to cleanly hand the session to the RTP engine when
    /// a call started, breaking voice audio entirely.
    ///
    /// Background incoming calls require PushKit (already wired in this file)
    /// + server-side voip_push_bridge.php + freeswitch_push.lua dialplan hook.
    /// When deployed, FreeSWITCH wakes the suspended app within ~200 ms via APNs,
    /// the app re-registers, and CallKit answers the INVITE.
    func handleAppBackground() {
        // No-op.  iOS will suspend the app shortly; PushKit will wake it on call.
    }

    /// Call when `scenePhase` becomes `.active`.
    /// Re-registers if the SIP stack died while we were suspended.
    func handleAppForeground() {
        guard registrationState != .registered && registrationState != .registering else { return }
        let config = SettingsRepository.shared.load()
        guard config.isValid else { return }
        Self.log.info("App foregrounded — restarting SIP stack after background loss")
        sipHandler.restartForNetworkChange()
    }

    // MARK: - Outbound calls via CallKit

    func startOutgoingCall(number: String) {
        let uuid = UUID()
        activeCallUUID = uuid
        isOutgoingCall = true
        // Immediately update callState so ContentView shows InCallScreen right away
        // before the async CallKit transaction completes.
        callState = .calling
        let handle = CXHandle(type: .phoneNumber, value: number)
        let action = CXStartCallAction(call: uuid, handle: handle)
        let transaction = CXTransaction(action: action)
        callController.request(transaction) { [weak self] error in
            if let error = error {
                Self.log.error("CXStartCallAction failed: \(error.localizedDescription, privacy: .public)")
                return
            }
            self?.sipHandler.makeCall(number)
        }
    }

    // MARK: - SipHandler → CallKit bridge

    private func handleCallStateChanged(state: CallState, number: String, name: String) {
        // Re-publish so ContentView observes the change and updates showInCall.
        callState = state

        switch state {
        case .calling, .ringing:
            // Play a local ringback tone so the caller hears ringing while waiting.
            if isOutgoingCall { startRingback() }
        case .incoming:
            reportIncomingCall(number: number, name: name)
        case .confirmed:
            stopRingback()
            stopBusy()
            if let uuid = activeCallUUID, isOutgoingCall {
                provider.reportOutgoingCall(with: uuid, connectedAt: Date())
            }
        case .busy:
            stopRingback()
            startBusy()
            if let uuid = activeCallUUID {
                provider.reportCall(with: uuid, endedAt: Date(), reason: .remoteEnded)
                activeCallUUID = nil
            }
        case .disconnected, .rejected:
            stopRingback()
            stopBusy()
            if let uuid = activeCallUUID {
                provider.reportCall(with: uuid, endedAt: Date(), reason: .remoteEnded)
                activeCallUUID = nil
            }
        case .idle:
            // SipHandler reset after busy/rejected — stop any lingering tones.
            stopBusy()
            stopRingback()
        default:
            break
        }
    }

    // MARK: - Ringback tone

    /// Starts the local NANP ringback tone (440 + 480 Hz, 2 s on / 4 s off).
    /// Harmless no-op if already playing.
    private func startRingback() {
        guard ringbackPlayer == nil else { return }
        let wav = buildRingbackWav()
        do {
            try AVAudioSession.sharedInstance().setCategory(.playback, mode: .default, options: .mixWithOthers)
            try AVAudioSession.sharedInstance().setActive(true)
            let player = try AVAudioPlayer(data: wav, fileTypeHint: AVFileType.wav.rawValue)
            player.numberOfLoops = -1   // loop until stopped
            player.volume = 0.7
            player.play()
            ringbackPlayer = player
        } catch {
            Self.log.warning("Ringback start failed: \(error.localizedDescription, privacy: .public)")
        }
    }

    private func stopRingback() {
        ringbackPlayer?.stop()
        ringbackPlayer = nil
        // Do NOT call setActive(false) — Apple rule: never deactivate the session
        // in response to it being activated. RtpSession.configureAudioSession()
        // switches the category to .playAndRecord / .voiceChat on the active session.
    }

    /// Builds a one-cycle (6 s) NANP ringback as a raw PCM WAV:
    /// 440 Hz + 480 Hz mixed, 2 s audible / 4 s silence.
    private func buildRingbackWav() -> Data {
        let rate = 44100
        let onFrames  = rate * 2   // 2 s ring
        let offFrames = rate * 4   // 4 s silence
        let total     = onFrames + offFrames

        var samples = [Int16](repeating: 0, count: total)
        for i in 0..<onFrames {
            let t = Double(i) / Double(rate)
            let v = 0.28 * (sin(2 * .pi * 440 * t) + sin(2 * .pi * 480 * t))
            let clamped = max(-32767, min(32767, Int(v * 32767)))
            samples[i] = Int16(clamped)
        }

        func le32(_ v: UInt32) -> [UInt8] {
            [UInt8(v & 0xFF), UInt8((v >> 8) & 0xFF), UInt8((v >> 16) & 0xFF), UInt8((v >> 24) & 0xFF)]
        }
        func le16(_ v: UInt16) -> [UInt8] { [UInt8(v & 0xFF), UInt8((v >> 8) & 0xFF)] }

        let dataBytes = UInt32(total * 2)
        var wav = Data()
        wav += "RIFF".utf8;  wav += le32(36 + dataBytes)
        wav += "WAVE".utf8;  wav += "fmt ".utf8; wav += le32(16)
        wav += le16(1)                              // PCM
        wav += le16(1)                              // mono
        wav += le32(UInt32(rate))                   // sample rate
        wav += le32(UInt32(rate * 2))               // byte rate
        wav += le16(2)                              // block align
        wav += le16(16)                             // bits per sample
        wav += "data".utf8; wav += le32(dataBytes)
        wav += samples.withUnsafeBytes { Data($0) }
        return wav
    }

    // MARK: - Busy tone

    /// Plays the standard US busy signal (480 + 620 Hz, 0.5 s on / 0.5 s off).
    /// Stops automatically when the call resets to .idle.
    private func startBusy() {
        guard busyPlayer == nil else { return }
        let wav = buildBusyWav()
        do {
            try AVAudioSession.sharedInstance().setCategory(.playback, mode: .default, options: .mixWithOthers)
            try AVAudioSession.sharedInstance().setActive(true)
            let player = try AVAudioPlayer(data: wav, fileTypeHint: AVFileType.wav.rawValue)
            player.numberOfLoops = -1   // loop until SipHandler resets to .idle
            player.volume = 0.7
            player.play()
            busyPlayer = player
        } catch {
            Self.log.warning("Busy tone start failed: \(error.localizedDescription, privacy: .public)")
        }
    }

    private func stopBusy() {
        busyPlayer?.stop()
        busyPlayer = nil
    }

    /// Builds a 1-second US busy-signal cycle (480 + 620 Hz, 0.5 s on / 0.5 s off).
    private func buildBusyWav() -> Data {
        let rate     = 44100
        let onFrames = rate / 2    // 0.5 s ring
        let offFrames = rate / 2   // 0.5 s silence
        let total    = onFrames + offFrames

        var samples = [Int16](repeating: 0, count: total)
        for i in 0..<onFrames {
            let t = Double(i) / Double(rate)
            let v = 0.28 * (sin(2 * .pi * 480 * t) + sin(2 * .pi * 620 * t))
            let clamped = max(-32767, min(32767, Int(v * 32767)))
            samples[i] = Int16(clamped)
        }

        func le32(_ v: UInt32) -> [UInt8] {
            [UInt8(v & 0xFF), UInt8((v >> 8) & 0xFF), UInt8((v >> 16) & 0xFF), UInt8((v >> 24) & 0xFF)]
        }
        func le16(_ v: UInt16) -> [UInt8] { [UInt8(v & 0xFF), UInt8((v >> 8) & 0xFF)] }

        let dataBytes = UInt32(total * 2)
        var wav = Data()
        wav += "RIFF".utf8;  wav += le32(36 + dataBytes)
        wav += "WAVE".utf8;  wav += "fmt ".utf8; wav += le32(16)
        wav += le16(1)                              // PCM
        wav += le16(1)                              // mono
        wav += le32(UInt32(rate))                   // sample rate
        wav += le32(UInt32(rate * 2))               // byte rate
        wav += le16(2)                              // block align
        wav += le16(16)                             // bits per sample
        wav += "data".utf8; wav += le32(dataBytes)
        wav += samples.withUnsafeBytes { Data($0) }
        return wav
    }

    private func reportIncomingCall(number: String, name: String) {
        let uuid = UUID()
        activeCallUUID = uuid
        isOutgoingCall = false
        let update = CXCallUpdate()
        update.remoteHandle = CXHandle(type: .phoneNumber, value: number)
        update.localizedCallerName = name.isEmpty ? number : name
        update.hasVideo = false
        provider.reportNewIncomingCall(with: uuid, update: update) { error in
            if let error = error {
                Self.log.error("reportNewIncomingCall failed: \(error.localizedDescription, privacy: .public)")
            }
        }
    }
}

// MARK: - CXProviderDelegate

extension SipService: CXProviderDelegate {
    nonisolated func providerDidReset(_ provider: CXProvider) {
        Task { @MainActor in
            self.sipHandler.hangup()
        }
    }

    nonisolated func provider(_ provider: CXProvider, perform action: CXStartCallAction) {
        // Outbound dialing initiated via CallKit. SipHandler.makeCall was already
        // called from startOutgoingCall(); just inform CallKit the call is connecting.
        provider.reportOutgoingCall(with: action.callUUID, startedConnectingAt: Date())
        action.fulfill()
    }

    nonisolated func provider(_ provider: CXProvider, perform action: CXAnswerCallAction) {
        Task { @MainActor in
            self.sipHandler.answerCall()
            action.fulfill()
            // When answered from the lock screen iOS does not automatically bring the
            // app to foreground — request scene activation so InCallScreen is visible.
            if #available(iOS 13, *) {
                UIApplication.shared.connectedScenes
                    .compactMap { $0 as? UIWindowScene }
                    .first.map { UIApplication.shared.requestSceneSessionActivation(
                        $0.session, userActivity: nil, options: nil, errorHandler: nil) }
            }
        }
    }

    nonisolated func provider(_ provider: CXProvider, perform action: CXEndCallAction) {
        Task { @MainActor in
            self.sipHandler.hangup()
            self.activeCallUUID = nil
            action.fulfill()
        }
    }

    nonisolated func provider(_ provider: CXProvider, perform action: CXSetMutedCallAction) {
        Task { @MainActor in
            self.sipHandler.setMuted(action.isMuted)
            action.fulfill()
        }
    }

    nonisolated func provider(_ provider: CXProvider, perform action: CXSetHeldCallAction) {
        Task { @MainActor in
            self.sipHandler.setHold(action.isOnHold)
            action.fulfill()
        }
    }

    nonisolated func provider(_ provider: CXProvider, perform action: CXPlayDTMFCallAction) {
        Task { @MainActor in
            for ch in action.digits { self.sipHandler.sendDtmf(ch) }
            action.fulfill()
        }
    }

    /// CallKit has activated the shared audio session — safe to start RTP audio now.
    /// RtpSession.configureAudioSession() does the full setup (setCategory + setActive)
    /// inside handleAudioActivation — splitting it across two places/threads caused
    /// partial configurations that left inputNode.inputFormat returning zero sampleRate.
    nonisolated func provider(_ provider: CXProvider, didActivate audioSession: AVAudioSession) {
        Task { @MainActor in
            self.stopRingback()
            self.sipHandler.handleAudioActivation()
        }
    }

    /// CallKit deactivated the audio session — call ended or interrupted.
    nonisolated func provider(_ provider: CXProvider, didDeactivate audioSession: AVAudioSession) {
        Task { @MainActor in
            self.sipHandler.handleAudioDeactivation()
        }
    }
}

// MARK: - PushKit (VoIP push)

extension SipService: PKPushRegistryDelegate {
    nonisolated func pushRegistry(_ registry: PKPushRegistry,
                                  didUpdate pushCredentials: PKPushCredentials,
                                  for type: PKPushType) {
        let token = pushCredentials.token.map { String(format: "%02x", $0) }.joined()
        Self.log.info("VoIP push token: \(token, privacy: .public)")
        // Pass the token to SipHandler so it is included in every REGISTER request
        // as X-Push-Token. FreeSWITCH stores it alongside the registration and uses
        // it to wake the app via APNs when an INVITE arrives while we are suspended.
        Task { @MainActor in
            self.sipHandler.setVoipPushToken(token)
        }
    }

    nonisolated func pushRegistry(_ registry: PKPushRegistry,
                                  didReceiveIncomingPushWith payload: PKPushPayload,
                                  for type: PKPushType,
                                  completion: @escaping () -> Void) {
        // Apple requires us to report an incoming call *before* this completion
        // handler returns, otherwise the app is terminated.
        Task { @MainActor in
            let caller = payload.dictionaryPayload["caller"] as? String ?? "Unknown"
            let callerName = payload.dictionaryPayload["callerName"] as? String ?? ""
            self.reportIncomingCall(number: caller, name: callerName)
            // Ensure SIP stack is up so the pending INVITE can be processed.
            self.sipHandler.start()
            completion()
        }
    }
}

// MARK: - Local notification helpers

extension SipService {
    static func requestNotificationPermission() {
        UNUserNotificationCenter.current()
            .requestAuthorization(options: [.alert, .sound, .badge]) { _, _ in }
    }

    static func showMessageNotification(from: String, body: String, msgType: String) {
        let content = UNMutableNotificationContent()
        content.title = "Message from \(from)"
        content.body = body
        content.sound = .default
        content.userInfo = ["chat_number": from, "chat_type": msgType]

        let request = UNNotificationRequest(
            identifier: "msg_\(from)_\(Date().timeIntervalSince1970)",
            content: content,
            trigger: nil
        )
        UNUserNotificationCenter.current().add(request, withCompletionHandler: nil)
    }
}
