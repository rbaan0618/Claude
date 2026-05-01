import SwiftUI
import Combine

/// Mirrors Android `ContactsScreen.kt`.
struct ContactsScreen: View {
    @EnvironmentObject var service: SipService
    @State private var contacts: [Contact] = []
    @State private var showAdd = false
    @State private var cancellable: AnyCancellable?

    private let dao = ContactsDao()

    var body: some View {
        NavigationStack {
            List(contacts) { contact in
                HStack {
                    VStack(alignment: .leading) {
                        Text(contact.name).font(.body)
                        Text(contact.number).font(.caption).foregroundColor(.secondary)
                    }
                    Spacer()
                    Button {
                        service.startOutgoingCall(number: contact.number)
                    } label: {
                        Image(systemName: "phone")
                    }.buttonStyle(.plain)
                }
            }
            .navigationTitle("Contacts")
            .toolbar {
                ToolbarItem(placement: .primaryAction) {
                    Button { showAdd = true } label: { Image(systemName: "plus") }
                }
            }
            .sheet(isPresented: $showAdd) {
                AddContactSheet { newContact in
                    _ = try? dao.upsert(newContact)
                }
            }
            .onAppear {
                cancellable = dao.observeAll()
                    .replaceError(with: [])
                    .receive(on: DispatchQueue.main)
                    .sink { contacts = $0 }
            }
        }
    }
}

private struct AddContactSheet: View {
    @Environment(\.dismiss) private var dismiss
    @State private var name = ""
    @State private var number = ""
    let onSave: (Contact) -> Void

    var body: some View {
        NavigationStack {
            Form {
                TextField("Name", text: $name)
                TextField("Number", text: $number).keyboardType(.phonePad)
            }
            .navigationTitle("New contact")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Save") {
                        onSave(Contact(id: nil, name: name, number: number))
                        dismiss()
                    }
                    .disabled(name.isEmpty || number.isEmpty)
                }
            }
        }
    }
}
