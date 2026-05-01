import Foundation
import GRDB

/// Mirrors Android `Contact.kt`.
struct Contact: Codable, FetchableRecord, MutablePersistableRecord, Identifiable, Equatable {
    static let databaseTableName = "contacts"

    var id: Int64?
    var name: String
    var number: String
    var isFavorite: Bool = false

    mutating func didInsert(_ inserted: InsertionSuccess) {
        id = inserted.rowID
    }
}
