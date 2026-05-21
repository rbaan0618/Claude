import Foundation
import CallKit
import PushKit
import AVFoundation
import Combine
import UserNotifications
import Network
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

    /// In-app toast shown when an incoming SMS / WhatsApp message arrives.
    /// Set to `nil` after a few seconds so the banner auto-dismisses.
    /// This is independent of `UNUserNotificationCenter` so the user still
    /// sees feedback even when they have denied notification permission
    /// or iOS is suppressing foreground banners.
    struct IncomingMessageToast: Equatable, Identifiable {
        let id = UUID()
        let from: String
        let body: String
        let isWhatsApp: Bool
    }
    @Published var incomingMessageToast: IncomingMessageToast?

    // CallKit
    private let provider: CXProvider
    private let callController = CXCallController()
    /// Observes ALL CallKit calls — used to verify whether a UUID we received
    /// from PushKit is actually still alive in CallKit before issuing
    /// `reportCall(updated:)` on it.  Without this check, a stale
    /// `pushPrereportedUUID` (e.g. iOS rate-limited the push and never
    /// displayed the original report) causes reportCall(updated:) to silently
    /// do nothing, leaving the user with no incoming-call UI for what is
    /// otherwise a perfectly-signalled SIP INVITE (180 Ringing sent, etc.).
    private let callObserver = CXCallObserver()
    private var activeCallUUID: UUID?
    private var isOutgoingCall: Bool = false

    // PushKit
    private let pushRegistry = PKPushRegistry(queue: .main)

    private var cancellables = Set<AnyCancellable>()

    // Ringback tone played locally on outgoing calls before the remote party answers.
    private var ringbackPlayer: AVAudioPlayer?
    // Busy tone played locally when the remote party is busy.
    private var busyPlayer: AVAudioPlayer?

    // Network path monitor — detects WiFi ↔ Cellular transitions and triggers
    // SIP re-registration before the UDP NAT binding goes stale.
    private var pathMonitor: NWPathMonitor?
    private var lastInterfaceType: NWInterface.InterfaceType?

    // When a PushKit push pre-reports an incoming call to CallKit, we store the
    // UUID here so the subsequent SIP INVITE does NOT create a second CallKit entry.
    private var pushPrereportedUUID: UUID?

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

        startNetworkMonitor()
        startTerminationObserver()

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
        //
        // Dedup: the server can deliver the same MESSAGE multiple times within
        // milliseconds (SIP retransmissions, multi-contact fanout, push-flow
        // double-delivery). Each delivery fires this closure on a fresh Task,
        // so the dedup MUST be atomic — check-then-insert as two separate
        // transactions races and lets all 5 copies through. The DAO method
        // below does both in a single write transaction.
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

                let dao = ChatMessageDao()
                let insertedId: Int64?
                do {
                    // Dedup window: 300 seconds (5 min).
                    //
                    // Why 5 minutes rather than the original 5 seconds:
                    // The SBC/FusionPBX may retransmit a SIP MESSAGE many
                    // times when it doesn't see our 200 OK (e.g. due to
                    // multi-Via responses, transient packet loss, or
                    // FusionPBX's message_queue service re-polling).
                    // Retransmits we observed were spaced 8-20+ seconds
                    // apart, easily exceeding the original 5-sec window
                    // and creating duplicate chat entries.
                    //
                    // 5 minutes is well past the longest reasonable SIP
                    // transaction timeout (T1*64 = 32 sec for INVITE,
                    // ~32 sec for non-INVITE) and any plausible upstream
                    // re-send loop. Legitimate duplicate user-sent messages
                    // with identical body are rare enough to be acceptable
                    // collateral.
                    insertedId = try dao.insertIfNotDuplicate(msg, dedupWindow: 300)
                } catch {
                    Self.log.error("insertIfNotDuplicate failed: \(error.localizedDescription, privacy: .public)")
                    return
                }
                guard insertedId != nil else {
                    Self.log.warning("Duplicate inbound message from \(from, privacy: .public) — suppressed")
                    return
                }
                Self.log.info("Saved incoming \(msgType) message from \(from, privacy: .public)")
                Self.showMessageNotification(from: from, body: body, msgType: msgType)
                // Always publish an in-app toast — independent of iOS
                // notification permission — so the user sees an alert
                // even when the app is in the foreground or the user
                // has disabled banners for the app.
                await MainActor.run {
                    let toast = IncomingMessageToast(
                        from: from, body: body, isWhatsApp: msgType == "whatsapp"
                    )
                    SipService.shared.incomingMessageToast = toast
                    // Auto-dismiss after 4 s unless replaced by a newer toast.
                    let toastId = toast.id
                    DispatchQueue.main.asyncAfter(deadline: .now() + 4) {
                        if SipService.shared.incomingMessageToast?.id == toastId {
                            SipService.shared.incomingMessageToast = nil
                        }
                    }
                }
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
    ///
    /// Re-register only if the SIP stack appears dead — never blindly
    /// restart, because `restartForNetworkChange()` tears down the UDP
    /// socket and any in-flight INVITE.  Doing it on every foreground
    /// transition was the cause of "iPhone no longer receives calls in
    /// foreground or background" — when the user opened the app in
    /// response to a CallKit-displayed incoming call, the restart
    /// closed the very socket that had just delivered the INVITE.
    ///
    /// If we are in the middle of an active call (.confirmed / .hold /
    /// .incoming / .calling / .ringing) we ALWAYS skip the restart so
    /// the in-flight transaction isn't torn down.
    func handleAppForeground() {
        // Never disturb a live or in-progress call.
        switch callState {
        case .idle, .disconnected:
            break
        default:
            Self.log.info("App foregrounded during active call (\(String(describing: self.callState))) — skipping SIP restart")
            return
        }
        // Already registered/registering on a fresh socket — nothing to do.
        guard registrationState != .registered && registrationState != .registering else { return }
        let config = SettingsRepository.shared.load()
        guard config.isValid else { return }
        Self.log.info("App foregrounded — restarting SIP stack after background loss")
        sipHandler.restartForNetworkChange()
    }

    /// Monitors network path changes (WiFi ↔ Cellular) and triggers SIP
    /// re-registration whenever the interface type changes.
    ///
    /// iOS calls this handler while the app is:
    ///  • Foregrounded (always)
    ///  • In the brief background window before full suspension
    ///  • Running an active audio call (kept alive by the `audio` background mode)
    ///
    /// For fully suspended apps, PushKit is the fallback: the incoming call push
    /// arrives, `sipHandler.start()` re-registers, and the suspended INVITE resumes.
    private func startNetworkMonitor() {
        let monitor = NWPathMonitor()
        monitor.pathUpdateHandler = { [weak self] path in
            guard let self else { return }
            Task { @MainActor in
                guard path.status == .satisfied else { return }

                // Identify the dominant interface type.
                let newType: NWInterface.InterfaceType =
                    path.usesInterfaceType(.wifi)     ? .wifi     :
                    path.usesInterfaceType(.cellular) ? .cellular : .other

                if let last = self.lastInterfaceType, last != newType {
                    Self.log.info(
                        "Network interface changed \(String(describing: last)) → \(String(describing: newType), privacy: .public) — re-registering"
                    )
                    self.sipHandler.restartForNetworkChange()
                }
                self.lastInterfaceType = newType
            }
        }
        monitor.start(queue: .global(qos: .utility))
        pathMonitor = monitor
    }

    /// Registers for `UIApplication.willTerminateNotification` and sends a SIP
    /// unregister before the process is killed.
    ///
    /// This covers the case where the user deliberately force-quits the app.
    /// After the unregister, dSIPRouter forwards `Expires: 0` to FusionPBX with
    /// the sbc.myline.tel contact, so FusionPBX stops routing calls to us.
    ///
    /// Important: `stopBlocking()` in SipHandler uses `DispatchQueue.main.async`
    /// for UI callbacks. Blocking the main thread while waiting for ioQueue would
    /// deadlock. We deliver on a background `OperationQueue` so sleeping there is
    /// safe, and the main thread stays free to process those async callbacks.
    private func startTerminationObserver() {
        let queue = OperationQueue()
        queue.maxConcurrentOperationCount = 1
        NotificationCenter.default.addObserver(
            forName: UIApplication.willTerminateNotification,
            object: nil,
            queue: queue
        ) { [weak self] _ in
            guard let self else { return }
            Self.log.info("App terminating — sending SIP unregister")
            // stop() dispatches stopBlocking() → unregister() to ioQueue asynchronously.
            // Sleep 2 s on this background thread to let it complete before the
            // process is killed. iOS gives ~5 s after willTerminate.
            self.sipHandler.stop()
            Thread.sleep(forTimeInterval: 2.0)
            Self.log.info("Termination unregister window complete")
        }
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
            // A push-prereported call that ended must NOT leave a stale UUID
            // behind: the next inbound call would route through reportCall(updated:)
            // on a dead UUID and silently fail to display in CallKit.
            pushPrereportedUUID = nil
        case .disconnected, .rejected:
            stopRingback()
            stopBusy()
            if let uuid = activeCallUUID {
                provider.reportCall(with: uuid, endedAt: Date(), reason: .remoteEnded)
                activeCallUUID = nil
            }
            pushPrereportedUUID = nil
        case .idle:
            // SipHandler reset after busy/rejected — stop any lingering tones.
            stopBusy()
            stopRingback()
            pushPrereportedUUID = nil
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
            // .playAndRecord + .voiceChat matches what CallKit / RtpSession use
            // once the call connects, so we don't fight CallKit for the session
            // partway through.  .mixWithOthers + .defaultToSpeaker keep the
            // ringback audible on the loudspeaker even when CallKit has staged
            // an outbound call (without this iOS sometimes silences the route
            // until the call actually answers).
            let session = AVAudioSession.sharedInstance()
            try session.setCategory(.playAndRecord,
                                    mode: .voiceChat,
                                    options: [.mixWithOthers, .defaultToSpeaker, .allowBluetooth])
            try session.setActive(true, options: [])
            let player = try AVAudioPlayer(data: wav, fileTypeHint: AVFileType.wav.rawValue)
            player.numberOfLoops = -1   // loop until stopped
            player.volume = 1.0
            player.prepareToPlay()
            player.play()
            ringbackPlayer = player
            Self.log.info("Ringback started — playing=\(player.isPlaying)")
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
        DebugLog.shared.write("CallKit", "reportIncomingCall fired caller=\(number) name=\(name)")
        let update = CXCallUpdate()
        update.remoteHandle = CXHandle(type: .phoneNumber, value: number)
        update.localizedCallerName = name.isEmpty ? number : name
        update.hasVideo = false

        // Always issue a fresh reportNewIncomingCall when a real SIP INVITE
        // arrives — see commit history for rationale.
        if let previousUUID = activeCallUUID,
           callObserver.calls.contains(where: { $0.uuid == previousUUID && !$0.hasEnded }) {
            Self.log.warning("Ending stale CallKit entry \(previousUUID) before reporting fresh incoming call")
            DebugLog.shared.write("CallKit", "ending stale UUID \(previousUUID) before fresh report")
            provider.reportCall(with: previousUUID, endedAt: Date(), reason: .failed)
        }
        pushPrereportedUUID = nil
        activeCallUUID = nil

        let uuid = UUID()
        activeCallUUID = uuid
        isOutgoingCall = false
        Self.log.info("reportNewIncomingCall caller=\(number, privacy: .public) name=\(name, privacy: .public) uuid=\(uuid)")
        DebugLog.shared.write("CallKit", "reportNewIncomingCall uuid=\(uuid.uuidString.prefix(8))")
        provider.reportNewIncomingCall(with: uuid, update: update) { error in
            if let error = error {
                Self.log.error("reportNewIncomingCall failed: \(error.localizedDescription, privacy: .public)")
                DebugLog.shared.write("CallKit", "❌ reportNewIncomingCall FAILED: \(error.localizedDescription)")
            } else {
                Self.log.info("CallKit accepted incoming-call report uuid=\(uuid)")
                DebugLog.shared.write("CallKit", "✓ CallKit accepted report uuid=\(uuid.uuidString.prefix(8))")
            }
        }
    }
}

// MARK: - CXProviderDelegate

extension SipService: CXProviderDelegate {
    nonisolated func providerDidReset(_ provider: CXProvider) {
        Task { @MainActor in
            self.sipHandler.hangup()
            self.activeCallUUID = nil
            self.pushPrereportedUUID = nil
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
            self.pushPrereportedUUID = nil
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
        DebugLog.shared.write("Push", "PushKit delivered token \(String(token.prefix(16)))…")
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
        // Apple's PushKit contract (https://developer.apple.com/documentation/pushkit
        // /pkpushregistrydelegate/2875784-pushregistry):
        //
        //   * The app MUST invoke `reportNewIncomingCall` during this delegate
        //     callback.  If iOS thinks no call was reported, it terminates the
        //     process AND rate-limits future VoIP pushes for the bundle — often
        //     for several hours.  Once that happens, the next dozen test calls
        //     all silently fail even though APNs returns HTTP 200.
        //
        //   * The push completion handler MUST be called only AFTER
        //     reportNewIncomingCall has completed — i.e. nested inside its
        //     callback closure, never before it.  Calling completion() too
        //     early is the most common cause of "push delivered but phone
        //     doesn't ring" in shipping VoIP apps.
        //
        // The previous implementation invoked completion() immediately after
        // `reportNewIncomingCall` without waiting for its async callback, which
        // is exactly the anti-pattern Apple warns about.
        let caller     = payload.dictionaryPayload["caller"]     as? String ?? "Unknown"
        let callerName = payload.dictionaryPayload["callerName"] as? String ?? ""
        Self.log.info("VoIP push received: caller=\(caller, privacy: .public) name=\(callerName, privacy: .public)")
        DebugLog.shared.write("Push", "VoIP push received caller=\(caller) name=\(callerName)")

        let uuid = UUID()
        let update = CXCallUpdate()
        update.remoteHandle = CXHandle(type: .phoneNumber, value: caller)
        update.localizedCallerName = callerName.isEmpty ? caller : callerName
        update.hasVideo = false

        // 1) Report the call.  2) Inside the callback, ack the push.
        DebugLog.shared.write("Push", "calling reportNewIncomingCall uuid=\(uuid.uuidString.prefix(8))")
        provider.reportNewIncomingCall(with: uuid, update: update) { error in
            if let error = error {
                Self.log.error("reportNewIncomingCall failed: \(error.localizedDescription, privacy: .public)")
                DebugLog.shared.write("Push", "❌ reportNewIncomingCall FAILED: \(error.localizedDescription)")
            } else {
                Self.log.info("Push-driven incoming call reported to CallKit (uuid=\(uuid))")
                DebugLog.shared.write("Push", "✓ push-driven call reported to CallKit")
            }
            completion()
        }

        // Store UUID and wake the SIP stack on the main actor.  When the SIP
        // INVITE arrives (after our re-registration triggers route[JOIN] in
        // Kamailio), reportIncomingCall() will detect this pre-reported UUID
        // and update the existing CallKit entry instead of creating a second.
        Task { @MainActor in
            self.activeCallUUID = uuid
            self.isOutgoingCall = false
            self.pushPrereportedUUID = uuid
            // Restart SIP stack — app was suspended, socket is dead.
            self.sipHandler.start()
        }
    }
}

// MARK: - Local notification helpers

extension SipService {
    static func requestNotificationPermission() {
        let center = UNUserNotificationCenter.current()
        // Install our delegate BEFORE requesting authorization so the very
        // first authorized notification — which may fire while the app is
        // still in the foreground (the user is on the chat screen when the
        // SMS/WhatsApp arrives) — gets `willPresent` and is shown as a
        // banner.  Without a delegate, iOS suppresses foreground
        // notifications silently and the user sees nothing.
        center.delegate = SipService.shared
        center.requestAuthorization(options: [.alert, .sound, .badge]) { _, _ in }
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

// MARK: - UNUserNotificationCenterDelegate (foreground banner)

extension SipService: UNUserNotificationCenterDelegate {
    /// Called when a local notification fires while the app is in the
    /// foreground.  Without overriding this, iOS suppresses the banner and
    /// the user has no visible/audible indication that an SMS or WhatsApp
    /// message has arrived while they have the app open.
    nonisolated func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        willPresent notification: UNNotification,
        withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void
    ) {
        if #available(iOS 14.0, *) {
            completionHandler([.banner, .list, .sound])
        } else {
            completionHandler([.alert, .sound])
        }
    }
}
