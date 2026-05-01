import Foundation
import GRDB

/// GRDB-based database — replaces Android Room.
///
/// Mirrors the Android `AppDatabase.kt` schema: `call_history`, `contacts`,
/// `chat_messages`. Stored at `Library/Application Support/softphone.sqlite`.
final class AppDatabase {
    static let shared: AppDatabase = {
        do { return try AppDatabase() }
        catch { fatalError("Database init failed: \(error)") }
    }()

    let dbQueue: DatabaseQueue

    init() throws {
        let fm = FileManager.default
        let appSupport = try fm.url(for: .applicationSupportDirectory,
                                    in: .userDomainMask,
                                    appropriateFor: nil,
                                    create: true)
        let url = appSupport.appendingPathComponent("softphone.sqlite")
        dbQueue = try DatabaseQueue(path: url.path)
        try migrator.migrate(dbQueue)
    }

    private var migrator: DatabaseMigrator {
        var migrator = DatabaseMigrator()

        migrator.registerMigration("v1") { db in
            try db.create(table: "call_history") { t in
                t.autoIncrementedPrimaryKey("id")
                t.column("direction", .text).notNull()
                t.column("remoteNumber", .text).notNull()
                t.column("remoteName", .text).notNull().defaults(to: "")
                t.column("status", .text).notNull().defaults(to: "")
                t.column("startedAt", .datetime).notNull()
                t.column("answeredAt", .datetime)
                t.column("endedAt", .datetime)
                t.column("durationSeconds", .integer).notNull().defaults(to: 0)
            }

            try db.create(table: "contacts") { t in
                t.autoIncrementedPrimaryKey("id")
                t.column("name", .text).notNull()
                t.column("number", .text).notNull()
                t.column("isFavorite", .boolean).notNull().defaults(to: false)
            }

            try db.create(table: "chat_messages") { t in
                t.autoIncrementedPrimaryKey("id")
                t.column("remoteNumber", .text).notNull().indexed()
                t.column("body", .text).notNull()
                t.column("isOutgoing", .boolean).notNull()
                t.column("timestamp", .datetime).notNull()
                t.column("status", .text).notNull()
            }
        }

        migrator.registerMigration("v2") { db in
            try db.alter(table: "chat_messages") { t in
                t.add(column: "messageType", .text).notNull().defaults(to: "sms")
            }
        }

        return migrator
    }
}
