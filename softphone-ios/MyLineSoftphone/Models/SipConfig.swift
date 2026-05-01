import Foundation

/// SIP account configuration. Mirrors Android `SipConfig.kt`.
struct SipConfig: Codable, Equatable {
    var server: String = ""
    var port: Int = 5060
    var localPort: Int = 5060
    var rport: Bool = true
    var username: String = ""
    var password: String = ""
    var displayName: String = ""
    var transport: String = "UDP"
    var enabled: Bool = true

    var isValid: Bool {
        !server.isEmpty && !username.isEmpty && !password.isEmpty
    }

    /// Full SIP domain — if the server lacks a dot, append the default realm.
    var domain: String {
        server.contains(".") ? server : "\(server).myline.tel"
    }
}
