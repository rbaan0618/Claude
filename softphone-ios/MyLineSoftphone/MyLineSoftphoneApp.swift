import SwiftUI

/// App entry point. Mirrors Android `SoftphoneApp.kt` + `MainActivity.kt`.
@main
struct MyLineSoftphoneApp: App {
    @StateObject private var service = SipService.shared

    init() {
        // Ensure DB is created up front so DAOs can observe immediately.
        _ = AppDatabase.shared
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
    }
}
