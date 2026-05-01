import Foundation

/// A monitored extension for busy-lamp-field presence display. Mirrors Android `BlfEntry.kt`.
struct BlfEntry: Codable, Equatable, Identifiable {
    var extension_: String
    var label: String = ""
    var state: BlfState = .unknown

    var id: String { extension_ }

    var displayName: String {
        label.isEmpty ? extension_ : label
    }

    enum CodingKeys: String, CodingKey {
        case extension_ = "extension"
        case label
        case state
    }
}
