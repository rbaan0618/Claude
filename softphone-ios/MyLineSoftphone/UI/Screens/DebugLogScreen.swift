import SwiftUI
import UIKit

/// In-app log viewer for diagnosing CallKit / Speaker / SIP issues
/// without a Mac.  Reads from DebugLog.shared.entries (a 500-line ring
/// buffer that key code paths write into).  Tap "Share" to copy the
/// dump to clipboard so it can be pasted/sent to support.
struct DebugLogScreen: View {
    @ObservedObject private var log = DebugLog.shared
    @State private var copiedFlash = false

    var body: some View {
        VStack(spacing: 0) {
            // Toolbar
            HStack {
                Button(action: { log.clear() }) {
                    Label("Clear", systemImage: "trash")
                }
                Spacer()
                Text("\(log.entries.count) entries")
                    .font(.caption)
                    .foregroundColor(.secondary)
                Spacer()
                Button(action: copyAll) {
                    Label(copiedFlash ? "Copied!" : "Copy", systemImage: "doc.on.doc")
                }
            }
            .padding(.horizontal)
            .padding(.vertical, 8)
            .background(Color(.systemGray6))

            // Log table
            ScrollViewReader { scroller in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 0) {
                        ForEach(log.entries) { entry in
                            HStack(alignment: .top, spacing: 6) {
                                Text(formatTime(entry.timestamp))
                                    .font(.system(size: 10, design: .monospaced))
                                    .foregroundColor(.secondary)
                                    .frame(width: 80, alignment: .leading)
                                Text(entry.category)
                                    .font(.system(size: 10, weight: .semibold, design: .monospaced))
                                    .foregroundColor(categoryColor(entry.category))
                                    .frame(width: 60, alignment: .leading)
                                Text(entry.message)
                                    .font(.system(size: 11, design: .monospaced))
                                    .foregroundColor(.primary)
                                    .frame(maxWidth: .infinity, alignment: .leading)
                            }
                            .padding(.horizontal, 8)
                            .padding(.vertical, 3)
                            .background(entry.id % 2 == 0 ? Color.clear : Color(.systemGray6).opacity(0.5))
                            .id(entry.id)
                        }
                    }
                }
                .onChange(of: log.entries.last?.id) { _, newId in
                    if let id = newId {
                        withAnimation { scroller.scrollTo(id, anchor: .bottom) }
                    }
                }
                .onAppear {
                    if let id = log.entries.last?.id {
                        scroller.scrollTo(id, anchor: .bottom)
                    }
                }
            }
        }
        .navigationTitle("Diagnostic Logs")
        .navigationBarTitleDisplayMode(.inline)
    }

    private func copyAll() {
        UIPasteboard.general.string = log.textDump()
        copiedFlash = true
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.2) { copiedFlash = false }
    }

    private func formatTime(_ date: Date) -> String {
        let f = DateFormatter()
        f.dateFormat = "HH:mm:ss.SSS"
        return f.string(from: date)
    }

    private func categoryColor(_ category: String) -> Color {
        switch category {
        case "SIP":      return .blue
        case "CallKit":  return .purple
        case "Speaker":  return .orange
        case "RTP":      return .green
        default:         return .gray
        }
    }
}
