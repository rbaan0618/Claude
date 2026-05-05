import SwiftUI

/// App entry point. Mirrors Android `SoftphoneApp.kt` + `MainActivity.kt`.
@main
struct MyLineSoftphoneApp: App {
    @StateObject private var service = SipService.shared
    @Environment(\.scenePhase) private var scenePhase

    init() {
        _ = AppDatabase.shared
        SipService.requestNotificationPermission()
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
