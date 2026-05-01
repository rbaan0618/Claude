import Foundation
import Combine
import GRDB

/// Mirrors Android `ContactsDao.kt`.
struct ContactsDao {
    let dbQueue: DatabaseQueue

    init(dbQueue: DatabaseQueue = AppDatabase.shared.dbQueue) {
        self.dbQueue = dbQueue
    }

    func upsert(_ contact: Contact) throws -> Int64 {
        var c = contact
        try dbQueue.write { db in
            try c.save(db)
        }
        return c.id ?? 0
    }

    func delete(_ contact: Contact) throws {
        _ = try dbQueue.write { db in
            try contact.delete(db)
        }
    }

    func all() throws -> [Contact] {
        try dbQueue.read { db in
            try Contact.order(Column("name")).fetchAll(db)
        }
    }

    func observeAll() -> AnyPublisher<[Contact], Error> {
        ValueObservation
            .tracking { db in try Contact.order(Column("name")).fetchAll(db) }
            .publisher(in: dbQueue)
            .eraseToAnyPublisher()
    }
}
