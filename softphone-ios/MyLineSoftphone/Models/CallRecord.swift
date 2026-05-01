import Foundation
import GRDB

/// One row in the call history. Mirrors Android `CallRecord.kt` (Room entity).
struct CallRecord: Codable, FetchableRecord, MutablePersistableRecord, Identifiable, Equatable {
    static let databaseTableName = "call_history"

    var id: Int64?
    var direction: CallDirection
    var remoteNumber: String
    var remoteName: String = ""
    var status: String = ""
    var startedAt: Date = Date()
    var answeredAt: Date?
    var endedAt: Date?
    var durationSeconds: Int = 0

    mutating func didInsert(_ inserted: InsertionSuccess) {
        id = inserted.rowID
    }
}
