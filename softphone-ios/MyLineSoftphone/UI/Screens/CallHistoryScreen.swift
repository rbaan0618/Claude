import SwiftUI
import Combine

/// Mirrors Android `CallHistoryScreen.kt`.
struct CallHistoryScreen: View {
    @EnvironmentObject var service: SipService
    @State private var records: [CallRecord] = []
    @State private var cancellable: AnyCancellable?

    private let dao = CallHistoryDao()

    var body: some View {
        List(records) { record in
            HStack(spacing: 12) {
                Image(systemName: icon(for: record))
                    .foregroundColor(color(for: record))
                    .frame(width: 24)
                VStack(alignment: .leading, spacing: 2) {
                    Text(record.remoteName.isEmpty ? record.remoteNumber : record.remoteName)
                        .font(.body)
                    Text(record.startedAt, style: .relative)
                        .font(.caption)
                        .foregroundColor(.secondary)
                }
                Spacer()
                Button {
                    service.startOutgoingCall(number: record.remoteNumber)
                } label: {
                    Image(systemName: "phone")
                }
                .buttonStyle(.plain)
                .foregroundColor(.accentColor)
            }
        }
        .listStyle(.plain)
        .onAppear {
            cancellable = dao.observeAll()
                .replaceError(with: [])
                .receive(on: DispatchQueue.main)
                .sink { records = $0 }
        }
    }

    private func icon(for r: CallRecord) -> String {
        switch r.direction {
        case .inbound: return "phone.arrow.down.left"
        case .outbound: return "phone.arrow.up.right"
        }
    }

    private func color(for r: CallRecord) -> Color {
        r.status == "missed" ? .red : .primary
    }
}
