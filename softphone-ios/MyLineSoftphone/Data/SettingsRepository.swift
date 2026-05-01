import Foundation
import Security

/// Persists `SipConfig` across launches.
///
/// Username/server live in `UserDefaults`, the SIP password lives in the **Keychain**
/// (unlike Android DataStore, iOS gives us a real secure store for free).
///
/// Mirrors the behavior of Android `SettingsRepository.kt`.
final class SettingsRepository {
    static let shared = SettingsRepository()

    private enum Keys {
        static let server = "sip.server"
        static let port = "sip.port"
        static let localPort = "sip.localPort"
        static let rport = "sip.rport"
        static let username = "sip.username"
        static let displayName = "sip.displayName"
        static let transport = "sip.transport"
        static let enabled = "sip.enabled"
    }

    private let defaults = UserDefaults.standard
    private let keychainService = "com.mylinetelecom.softphone.password"

    func load() -> SipConfig {
        var config = SipConfig()
        config.server = defaults.string(forKey: Keys.server) ?? ""
        config.port = defaults.object(forKey: Keys.port) as? Int ?? 5060
        config.localPort = defaults.object(forKey: Keys.localPort) as? Int ?? 5060
        config.rport = defaults.object(forKey: Keys.rport) as? Bool ?? true
        config.username = defaults.string(forKey: Keys.username) ?? ""
        config.displayName = defaults.string(forKey: Keys.displayName) ?? ""
        config.transport = defaults.string(forKey: Keys.transport) ?? "UDP"
        config.enabled = defaults.object(forKey: Keys.enabled) as? Bool ?? true
        config.password = readPassword(for: config.username) ?? ""
        return config
    }

    func save(_ config: SipConfig) {
        defaults.set(config.server, forKey: Keys.server)
        defaults.set(config.port, forKey: Keys.port)
        defaults.set(config.localPort, forKey: Keys.localPort)
        defaults.set(config.rport, forKey: Keys.rport)
        defaults.set(config.username, forKey: Keys.username)
        defaults.set(config.displayName, forKey: Keys.displayName)
        defaults.set(config.transport, forKey: Keys.transport)
        defaults.set(config.enabled, forKey: Keys.enabled)
        writePassword(config.password, for: config.username)
    }

    // MARK: - Keychain

    private func writePassword(_ password: String, for account: String) {
        guard !account.isEmpty else { return }
        let data = Data(password.utf8)
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: keychainService,
            kSecAttrAccount as String: account,
        ]
        SecItemDelete(query as CFDictionary)
        var attributes = query
        attributes[kSecValueData as String] = data
        attributes[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlock
        SecItemAdd(attributes as CFDictionary, nil)
    }

    private func readPassword(for account: String) -> String? {
        guard !account.isEmpty else { return nil }
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: keychainService,
            kSecAttrAccount as String: account,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
        ]
        var result: AnyObject?
        guard SecItemCopyMatching(query as CFDictionary, &result) == errSecSuccess,
              let data = result as? Data else { return nil }
        return String(data: data, encoding: .utf8)
    }
}
