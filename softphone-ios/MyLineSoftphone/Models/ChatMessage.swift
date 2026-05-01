import Foundation
import GRDB

enum MessageStatus: String, Codable {
    case sending
    case sent
    case failed
    case received
}

/// Mirrors Android `ChatMessage.kt`.
struct ChatMessage: Codable, FetchableRecord, MutablePersistableRecord, Identifiable, Equatable {
    static let databaseTableName = "chat_messages"

    var id: Int64?
    var remoteNumber: String
    var body: String
    var isOutgoing: Bool
    var timestamp: Date = Date()
    var status: MessageStatus = .sent

    mutating func didInsert(_ inserted: InsertionSuccess) {
        id = inserted.rowID
    }
}
