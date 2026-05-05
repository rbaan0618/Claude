import SwiftUI

/// Root navigation. Mirrors Android `MainActivity.kt` bottom-nav layout.
/// When a call is active, the in-call screen is presented as a full-screen cover.
struct ContentView: View {
    @EnvironmentObject var service: SipService

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
        .fullScreenCover(isPresented: .constant(isInCall)) {
            InCallScreen().environmentObject(service)
        }
    }

    private var isInCall: Bool {
        // Only hide the in-call screen when fully idle.
        // Keeping it visible through .disconnected/.rejected/.busy lets the user
        // see the final call state before resetCallState() fires ~1 second later.
        service.sipHandler.callState != .idle
    }
}
