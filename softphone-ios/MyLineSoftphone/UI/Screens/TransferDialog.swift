import SwiftUI

/// Blind / attended transfer picker. Mirrors Android `TransferDialog.kt`.
struct TransferDialog: View {
    @EnvironmentObject var service: SipService
    @Environment(\.dismiss) private var dismiss
    @State private var target: String = ""
    @State private var attended: Bool = false

    var body: some View {
        NavigationStack {
            Form {
                Section("Transfer to") {
                    TextField("Number", text: $target)
                        .keyboardType(.phonePad)
                    Toggle("Attended (consult first)", isOn: $attended)
                }
                Section {
                    Button("Transfer") {
                        if attended {
                            service.sipHandler.startConsultation(target: target)
                        } else {
                            service.sipHandler.blindTransfer(target: target)
                        }
                        dismiss()
                    }
                    .disabled(target.isEmpty)
                }
            }
            .navigationTitle("Transfer")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                }
            }
        }
    }
}
