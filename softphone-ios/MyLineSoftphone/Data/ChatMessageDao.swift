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

    func conversation(with remoteNumber: String) throws -> [ChatMessage] {
        try dbQueue.read { db in
            try ChatMessage
                .filter(Column("remoteNumber") == remoteNumber)
                .order(Column("timestamp"))
                .fetchAll(db)
        }
    }

    /// One entry per conversation — the latest message for each unique remote number.
    func observeConversations() -> AnyPublisher<[ChatMessage], Error> {
        ValueObservation
            .tracking { db in
                try ChatMessage
                    .order(Column("timestamp").desc)
                    .fetchAll(db)
            }
            .publisher(in: dbQueue)
            .eraseToAnyPublisher()
    }

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
