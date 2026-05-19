import Foundation
import Combine

/// In-app ring-buffer log so we can diagnose issues on iPhones without
/// access to a Mac / Console.app.  Anywhere in the codebase that wants to
/// surface a diagnostic line can call `DebugLog.shared.write("...")` —
/// the latest ~500 lines are kept in memory and rendered by the
/// "Diagnostic Logs" screen reachable from Settings.
///
/// This is in addition to the normal `os_log` Self.log.info(...) calls —
/// it does NOT replace them, just makes the diagnostic visible to a user
/// who can't connect to a Mac.
final class DebugLog: ObservableObject {
    static let shared = DebugLog()

    struct Entry: Identifiable, Equatable {
        let id: Int
        let timestamp: Date
        let category: String
        let message: String
    }

    @Published private(set) var entries: [Entry] = []
    private let lock = NSLock()
    private var nextId = 0
    private let cap = 500

    /// Append a line to the in-app log.  Thread-safe.
    func write(_ category: String, _ message: String) {
        lock.lock()
        let entry = Entry(
            id: nextId,
            timestamp: Date(),
            category: category,
            message: message
        )
        nextId += 1
        var snapshot = entries
        snapshot.append(entry)
        if snapshot.count > cap {
            snapshot.removeFirst(snapshot.count - cap)
        }
        lock.unlock()
        DispatchQueue.main.async { [weak self] in
            self?.entries = snapshot
        }
    }

    func clear() {
        DispatchQueue.main.async { [weak self] in
            self?.entries.removeAll()
        }
    }

    /// Plain-text dump of every entry currently in the buffer, suitable
    /// for copy-paste / share-sheet export.
    func textDump() -> String {
        let formatter = DateFormatter()
        formatter.dateFormat = "HH:mm:ss.SSS"
        return entries.map { entry in
            "\(formatter.string(from: entry.timestamp)) [\(entry.category)] \(entry.message)"
        }.joined(separator: "\n")
    }
}
