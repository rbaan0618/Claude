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
        .overlay(alignment: .top) {
            if let toast = service.incomingMessageToast {
                IncomingMessageToastView(toast: toast)
                    .padding(.horizontal, 12)
                    .padding(.top, 8)
                    .transition(.move(edge: .top).combined(with: .opacity))
            }
        }
        .animation(.easeInOut(duration: 0.25), value: service.incomingMessageToast?.id)
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

/// Slim banner shown at the top of the app when a new SMS / WhatsApp
/// message arrives.  Independent of iOS notification permissions —
/// always appears in-app for foreground feedback.
private struct IncomingMessageToastView: View {
    let toast: SipService.IncomingMessageToast

    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: toast.isWhatsApp ? "bubble.left.and.bubble.right.fill"
                                                : "message.fill")
                .font(.system(size: 22))
                .foregroundColor(.white)
                .frame(width: 36, height: 36)
                .background(toast.isWhatsApp ? Color.green : Color.blue)
                .clipShape(Circle())
            VStack(alignment: .leading, spacing: 2) {
                Text(toast.isWhatsApp ? "WhatsApp · \(toast.from)"
                                       : "SMS · \(toast.from)")
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundColor(.primary)
                Text(toast.body)
                    .font(.system(size: 13))
                    .foregroundColor(.secondary)
                    .lineLimit(2)
            }
            Spacer(minLength: 0)
        }
        .padding(.vertical, 10)
        .padding(.horizontal, 14)
        .background(
            RoundedRectangle(cornerRadius: 14)
                .fill(.thickMaterial)
                .shadow(color: Color.black.opacity(0.15), radius: 8, y: 2)
        )
    }
}
