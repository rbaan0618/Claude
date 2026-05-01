import Foundation

/// High-level state of the active SIP dialog. Mirrors Android `CallState.kt`.
enum CallState: String, Codable {
    case idle
    case calling
    case ringing
    case incoming
    case confirmed
    case hold
    case disconnected
    case rejected
    case busy
}

enum CallDirection: String, Codable {
    case inbound
    case outbound
}

/// Busy-lamp-field (BLF) presence for a monitored extension.
enum BlfState: String, Codable {
    case idle
    case ringing
    case busy
    case unknown
    case offline
}

enum RegistrationState: String, Codable {
    case unregistered
    case registering
    case registered
    case failed
}
