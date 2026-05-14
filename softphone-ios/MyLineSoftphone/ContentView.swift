import SwiftUI

/// Root navigation. Mirrors Android `MainActivity.kt` bottom-nav layout.
/// When a call is active, the in-call screen is presented as a full-screen cover.
struct ContentView: View {
    @EnvironmentObject var service: SipService
    @State private var showInCall = false

    var body: some View {
        TabView {
            DialpadScreen()
                .tabItem { Label("Keypad", systemImage: "square.grid.3x3.fill") }
            CallHistoryScreen()
                .tabItem { Label("Recents", systemImage: "clock.arrow.circlepath") }
            ContactsScreen()
                .tabItem { Label("Contacts", systemImage: "person.crop.circle") }
            MessagesScreen()
                .tabItem { Label("Messages", systemImage: "message") }
            BlfScreen()
                .tabItem { Label("BLF", systemImage: "lightbulb") }
            SettingsScreen()
                .tabItem { Label("Settings", systemImage: "gearshape") }
        }
        .fullScreenCover(isPresented: $showInCall) {
            InCallScreen().environmentObject(service)
        }
        .onChange(of: service.callState) { state in
            switch state {
            case .incoming:
                // CallKit native UI handles answer/reject for incoming calls.
                // Do NOT show InCallScreen here — it would cover the system
                // call screen and remove the Answer button from view.
                break
            case .idle, .disconnected:
                showInCall = false
            default:
                // .calling, .ringing, .confirmed → call is active, show controls
                showInCall = true
            }
        }
    }
}
