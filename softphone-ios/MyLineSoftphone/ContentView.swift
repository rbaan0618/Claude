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
        .onChange(of: service.sipHandler.callState) { state in
            if state != .idle {
                // Any active call state → show the in-call screen
                showInCall = true
            } else {
                // Only dismiss when fully idle (resetCallState fired)
                showInCall = false
            }
        }
    }
}
