import SwiftUI

/// Mirrors Android `SettingsScreen.kt`.
struct SettingsScreen: View {
    @EnvironmentObject var service: SipService
    @State private var config: SipConfig = SettingsRepository.shared.load()

    var body: some View {
        NavigationStack {
            Form {
                Section("Account") {
                    TextField("Display name", text: $config.displayName)
                    TextField("Username", text: $config.username)
                        .autocapitalization(.none)
                        .disableAutocorrection(true)
                    SecureField("Password", text: $config.password)
                }
                Section("Server") {
                    TextField("Server", text: $config.server)
                        .autocapitalization(.none)
                        .disableAutocorrection(true)
                    Stepper("Port: \(config.port)", value: $config.port, in: 1...65535)
                    Stepper("Local port: \(config.localPort)", value: $config.localPort, in: 1024...65535)
                    Picker("Transport", selection: $config.transport) {
                        Text("UDP").tag("UDP")
                        Text("TCP").tag("TCP")
                    }
                    Toggle("rport", isOn: $config.rport)
                }
                Section("Status") {
                    LabeledContent("Registration", value: service.registrationState.rawValue.capitalized)
                }
                Section {
                    Button("Save & Reconnect") {
                        SettingsRepository.shared.save(config)
                        service.stop()
                        service.start(with: config)
                    }
                    .disabled(!config.isValid)
                }
            }
            .navigationTitle("Settings")
        }
    }
}
