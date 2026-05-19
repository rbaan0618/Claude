import SwiftUI
import AVFoundation

/// App entry point. Mirrors Android `SoftphoneApp.kt` + `MainActivity.kt`.
@main
struct MyLineSoftphoneApp: App {
    @StateObject private var service = SipService.shared
    @Environment(\.scenePhase) private var scenePhase

    init() {
        _ = AppDatabase.shared
        SipService.requestNotificationPermission()
        // Pre-request microphone permission so CallKit's display service
        // recognises the freshly-installed bundle as an authorised VoIP
        // app BEFORE the first reportNewIncomingCall arrives.  Apple's
        // CallKit docs state the system requires microphone authorisation
        // to be in place before reporting an incoming call; without this
        // pre-request the first inbound call on a fresh install is
        // silently swallowed — no CallKit ring screen even though the SIP
        // INVITE landed and 180 Ringing was sent.
        if #available(iOS 17.0, *) {
            AVAudioApplication.requestRecordPermission { _ in }
        } else {
            AVAudioSession.sharedInstance().requestRecordPermission { _ in }
        }
    }

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environmentObject(service)
                .onAppear {
                    let config = SettingsRepository.shared.load()
                    if config.isValid { service.start(with: config) }
                }
        }
        .onChange(of: scenePhase) { phase in
            switch phase {
            case .background:
                // Request extra execution time so keepalives continue briefly.
                service.handleAppBackground()
            case .active:
                // Re-register if the SIP stack died while we were suspended.
                service.handleAppForeground()
            default:
                break
            }
        }
    }
}
