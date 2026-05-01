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

    // MARK: - Deduplication (mirrors Android countRecentDuplicates)

    func countRecentDuplicates(from number: String, body: String, since: Date) throws -> Int {
        try dbQueue.read { db in
            try Int.fetchOne(db,
                sql: "SELECT COUNT(*) FROM chat_messages WHERE remoteNumber = ? AND body = ? AND isOutgoing = 0 AND timestamp > ?",
                arguments: [number, body, since]) ?? 0
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
