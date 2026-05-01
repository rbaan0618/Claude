import Foundation
import GRDB

/// Mirrors Android `CallHistoryDao.kt`.
struct CallHistoryDao {
    let dbQueue: DatabaseQueue

    init(dbQueue: DatabaseQueue = AppDatabase.shared.dbQueue) {
        self.dbQueue = dbQueue
    }

    func insert(_ record: CallRecord) throws -> Int64 {
        var record = record
        try dbQueue.write { db in
            try record.insert(db)
        }
        return record.id ?? 0
    }

    func all() throws -> [CallRecord] {
        try dbQueue.read { db in
            try CallRecord.order(Column("startedAt").desc).fetchAll(db)
        }
    }

    func deleteAll() throws {
        _ = try dbQueue.write { db in
            try CallRecord.deleteAll(db)
        }
    }

    func observeAll() -> AnyPublisher<[CallRecord], Error> {
        ValueObservation
            .tracking { db in
                try CallRecord.order(Column("startedAt").desc).fetchAll(db)
            }
            .publisher(in: dbQueue)
            .eraseToAnyPublisher()
    }
}

import Combine
