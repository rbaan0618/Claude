import SwiftUI

/// Busy Lamp Field — monitored extensions with presence. Mirrors Android `BlfScreen.kt`.
struct BlfScreen: View {
    @EnvironmentObject var service: SipService
    @State private var entries: [BlfEntry] = []

    var body: some View {
        NavigationStack {
            List(entries) { entry in
                HStack {
                    Circle()
                        .fill(color(for: entry.state))
                        .frame(width: 14, height: 14)
                    VStack(alignment: .leading) {
                        Text(entry.displayName).font(.body)
                        Text(entry.extension_).font(.caption).foregroundColor(.secondary)
                    }
                    Spacer()
                    Button {
                        service.startOutgoingCall(number: entry.extension_)
                    } label: { Image(systemName: "phone") }.buttonStyle(.plain)
                }
            }
            .navigationTitle("BLF")
        }
    }

    private func color(for s: BlfState) -> Color {
        switch s {
        case .idle: return .green
        case .ringing: return .yellow
        case .busy: return .red
        case .unknown: return .gray
        case .offline: return .gray.opacity(0.4)
        }
    }
}
