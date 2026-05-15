import Foundation
import Combine
import GRDB

/// Mirrors Android `ChatMessageDao.kt`.
struct ChatMessageDao {
    let dbQueue: DatabaseQueue

    init(dbQueue: DatabaseQueue = AppDatabase.shared.dbQueue) {
        self.dbQueue = dbQueue
    }

    func insert(_ message: ChatMessage) throws -> Int64 {
        var m = message
        try dbQueue.write { db in try m.insert(db) }
        return m.id ?? 0
    }

    // MARK: - Conversation list (one entry per remoteNumber+messageType)

    func observeConversations(forType type: String) -> AnyPublisher<[ChatMessage], Error> {
        ValueObservation
            .tracking { db in
                // Last message per unique remoteNumber for this type, newest first.
                let sql = """
                    SELECT * FROM chat_messages
                    WHERE id IN (
                        SELECT MAX(id) FROM chat_messages
                        WHERE messageType = ?
                        GROUP BY remoteNumber
                    )
                    ORDER BY timestamp DESC
                    """
                return try ChatMessage.fetchAll(db, sql: sql, arguments: [type])
            }
            .publisher(in: dbQueue)
            .eraseToAnyPublisher()
    }

    // MARK: - Conversation detail

    func observeConversation(with remoteNumber: String, messageType: String) -> AnyPublisher<[ChatMessage], Error> {
        ValueObservation
            .tracking { db in
                try ChatMessage
                    .filter(Column("remoteNumber") == remoteNumber)
                    .filter(Column("messageType") == messageType)
                    .order(Column("timestamp"))
                    .fetchAll(db)
            }
            .publisher(in: dbQueue)
            .eraseToAnyPublisher()
    }

    // MARK: - WhatsApp template guard (mirrors Android countInbound)

    /// Returns the number of inbound messages from `remoteNumber` on a given channel.
    /// Zero means the contact has never messaged us — Meta will reject free-form
    /// messages in this case (error 131047); the caller should send a template instead.
    func countInbound(remoteNumber: String, messageType: String) throws -> Int {
        try dbQueue.read { db in
            try Int.fetchOne(db,
                sql: "SELECT COUNT(*) FROM chat_messages WHERE remoteNumber = ? AND messageType = ? AND isOutgoing = 0",
                arguments: [remoteNumber, messageType]) ?? 0
        }
    }

    // MARK: - Deduplication (mirrors Android countRecentDuplicates)

    func countRecentDuplicates(from number: String, body: String, since: Date) throws -> Int {
        try dbQueue.read { db in
            try Int.fetchOne(db,
                sql: "SELECT COUNT(*) FROM chat_messages WHERE remoteNumber = ? AND body = ? AND isOutgoing = 0 AND timestamp > ?",
                arguments: [number, body, since]) ?? 0
        }
    }

    /// Atomically insert an inbound message ONLY if no identical inbound message
    /// from the same number with the same body exists within `dedupWindow` seconds.
    ///
    /// Returns the inserted row id on success, or `nil` if the message was
    /// suppressed as a duplicate.
    ///
    /// This is the correct way to dedupe SIP MESSAGEs because the server can
    /// retransmit (or fan out via multiple registrations) within milliseconds.
    /// The previous pattern — `countRecentDuplicates` then `insert` as two
    /// separate transactions — races: 5 concurrent Tasks all read `count = 0`
    /// before any of the inserts commits, so all 5 inserts succeed and the
    /// message shows up 5 times in the UI.
    @discardableResult
    func insertIfNotDuplicate(_ message: ChatMessage, dedupWindow seconds: TimeInterval = 5) throws -> Int64? {
        let since = Date(timeIntervalSinceNow: -seconds)
        var m = message
        return try dbQueue.write { db in
            let count = try Int.fetchOne(db,
                sql: "SELECT COUNT(*) FROM chat_messages WHERE remoteNumber = ? AND body = ? AND isOutgoing = 0 AND timestamp > ?",
                arguments: [message.remoteNumber, message.body, since]) ?? 0
            guard count == 0 else { return nil }
            try m.insert(db)
            return m.id
        }
    }

    // MARK: - Delete

    func deleteConversation(remoteNumber: String, messageType: String) throws {
        try dbQueue.write { db in
            try db.execute(sql: "DELETE FROM chat_messages WHERE remoteNumber = ? AND messageType = ?",
                           arguments: [remoteNumber, messageType])
        }
    }

    // Legacy — still used by ChatDetailScreen before messageType is threaded through.
    func observeConversation(with remoteNumber: String) -> AnyPublisher<[ChatMessage], Error> {
        ValueObservation
            .tracking { db in
                try ChatMessage
                    .filter(Column("remoteNumber") == remoteNumber)
                    .order(Column("timestamp"))
                    .fetchAll(db)
            }
            .publisher(in: dbQueue)
            .eraseToAnyPublisher()
    }
}
