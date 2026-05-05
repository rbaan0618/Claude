import Foundation
import CallKit
import PushKit
import AVFoundation
import Combine
import UserNotifications
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

    // CallKit
    private let provider: CXProvider
    private let callController = CXCallController()
    private var activeCallUUID: UUID?
    private var isOutgoingCall: Bool = false

    // PushKit
    private let pushRegistry = PKPushRegistry(queue: .main)

    private var cancellables = Set<AnyCancellable>()

    override init() {
        let config = CXProviderConfiguration()
        config.supportsVideo = false
        config.maximumCallsPerCallGroup = 1
        config.supportedHandleTypes = [.phoneNumber, .generic]
        config.includesCallsInRecents = true
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

    // MARK: - Outbound calls via CallKit

    func startOutgoingCall(number: String) {
        let uuid = UUID()
        activeCallUUID = uuid
        isOutgoingCall = true
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
        switch state {
        case .incoming:
            reportIncomingCall(number: number, name: name)
        case .confirmed:
            if let uuid = activeCallUUID, isOutgoingCall {
                provider.reportOutgoingCall(with: uuid, connectedAt: Date())
            }
        case .disconnected, .rejected, .busy:
            if let uuid = activeCallUUID {
                provider.reportCall(with: uuid, endedAt: Date(), reason: .remoteEnded)
                activeCallUUID = nil
            }
        default:
            break
        }
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
    nonisolated func provider(_ provider: CXProvider, didActivate audioSession: AVAudioSession) {
        Task { @MainActor in
            self.sipHandler.handleAudioActivation()
        }
    }

    /// CallKit deactivated the audio session (e.g. interrupted by a native phone call).
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
        // TODO: Upload `pushCredentials.token` to your SIP push bridge so it can
        // send an APNs VoIP push when the server has an incoming INVITE for us.
        let token = pushCredentials.token.map { String(format: "%02x", $0) }.joined()
        Self.log.info("VoIP push token: \(token, privacy: .public)")
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
