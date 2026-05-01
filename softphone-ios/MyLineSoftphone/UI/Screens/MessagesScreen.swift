import SwiftUI
import Combine

/// Mirrors Android `MessagesScreen.kt` — two-tab conversation list (SMS / WhatsApp).
struct MessagesScreen: View {
    @State private var selectedTab = 0
    @State private var smsConvos: [ChatMessage] = []
    @State private var waConvos:  [ChatMessage] = []
    @State private var contacts: [Contact] = []
    @State private var cancellables = Set<AnyCancellable>()

    private let msgDao = ChatMessageDao()
    private let contactsDao = ContactsDao()

    private static let whatsAppGreen = Color(red: 0.145, green: 0.827, blue: 0.400)

    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                // Tab picker
                Picker("", selection: $selectedTab) {
                    Label("SMS", systemImage: "message.fill").tag(0)
                    Label("WhatsApp", systemImage: "phone.bubble.left.fill").tag(1)
                }
                .pickerStyle(.segmented)
                .padding(.horizontal)
                .padding(.vertical, 8)
                .tint(selectedTab == 0 ? .accentColor : Self.whatsAppGreen)

                Divider()

                let convos      = selectedTab == 0 ? smsConvos : waConvos
                let msgType     = selectedTab == 0 ? "sms" : "whatsapp"
                let accentColor = selectedTab == 0 ? Color.accentColor : Self.whatsAppGreen
                let contactMap  = Dictionary(contacts.map { ($0.number, $0.name) }, uniquingKeysWith: { a, _ in a })

                if convos.isEmpty {
                    Spacer()
                    VStack(spacing: 8) {
                        Image(systemName: selectedTab == 0 ? "message" : "phone.bubble.left")
                            .font(.system(size: 48))
                            .foregroundColor(accentColor.opacity(0.4))
                        Text(selectedTab == 0 ? "No SMS conversations yet" : "No WhatsApp conversations yet")
                            .foregroundColor(.secondary)
                    }
                    Spacer()
                } else {
                    List {
                        ForEach(convos) { last in
                            NavigationLink {
                                ChatDetailScreen(remoteNumber: last.remoteNumber,
                                                 messageType: msgType)
                            } label: {
                                ConversationRow(last: last,
                                               contactName: contactMap[last.remoteNumber],
                                               accentColor: accentColor)
                            }
                            .swipeActions(edge: .trailing) {
                                Button(role: .destructive) {
                                    try? msgDao.deleteConversation(remoteNumber: last.remoteNumber,
                                                                   messageType: msgType)
                                } label: {
                                    Label("Delete", systemImage: "trash")
                                }
                            }
                        }
                    }
                    .listStyle(.plain)
                }
            }
            .navigationTitle("Messages")
            .toolbar {
                ToolbarItem(placement: .navigationBarTrailing) {
                    NavigationLink {
                        NewMessageView(defaultType: selectedTab == 0 ? "sms" : "whatsapp")
                    } label: {
                        Image(systemName: "square.and.pencil")
                    }
                }
            }
            .onAppear { subscribe() }
            .onDisappear { cancellables.removeAll() }
        }
    }

    private func subscribe() {
        cancellables.removeAll()

        msgDao.observeConversations(forType: "sms")
            .replaceError(with: [])
            .receive(on: DispatchQueue.main)
            .sink { smsConvos = $0 }
            .store(in: &cancellables)

        msgDao.observeConversations(forType: "whatsapp")
            .replaceError(with: [])
            .receive(on: DispatchQueue.main)
            .sink { waConvos = $0 }
            .store(in: &cancellables)

        if let all = try? contactsDao.all() { contacts = all }
    }
}

// MARK: - Conversation row

private struct ConversationRow: View {
    let last: ChatMessage
    let contactName: String?
    let accentColor: Color

    var body: some View {
        HStack(spacing: 12) {
            // Avatar
            ZStack {
                Circle()
                    .fill(accentColor.opacity(0.15))
                    .frame(width: 42, height: 42)
                Text(displayName.prefix(1).uppercased())
                    .font(.headline)
                    .foregroundColor(accentColor)
            }

            VStack(alignment: .leading, spacing: 2) {
                Text(displayName)
                    .font(.body)
                    .foregroundColor(.primary)
                Text((last.isOutgoing ? "You: " : "") + last.body)
                    .font(.caption)
                    .foregroundColor(.secondary)
                    .lineLimit(1)
            }

            Spacer()

            Text(formatTime(last.timestamp))
                .font(.caption2)
                .foregroundColor(.secondary)
        }
    }

    private var displayName: String { contactName ?? last.remoteNumber }

    private func formatTime(_ date: Date) -> String {
        let cal = Calendar.current
        if cal.isDateInToday(date) {
            return date.formatted(date: .omitted, time: .shortened)
        }
        return date.formatted(.dateTime.month(.abbreviated).day())
    }
}

// MARK: - New message composer

private struct NewMessageView: View {
    @EnvironmentObject var service: SipService
    @Environment(\.dismiss) private var dismiss
    let defaultType: String
    @State private var number = ""
    @State private var text   = ""
    @State private var type: String

    init(defaultType: String) {
        self.defaultType = defaultType
        _type = State(initialValue: defaultType)
    }

    var body: some View {
        Form {
            Section("To") {
                TextField("Phone number", text: $number)
                    .keyboardType(.phonePad)
            }
            Section("Channel") {
                Picker("Type", selection: $type) {
                    Text("SMS").tag("sms")
                    Text("WhatsApp").tag("whatsapp")
                }
                .pickerStyle(.segmented)
            }
            Section("Message") {
                TextField("Type a message...", text: $text, axis: .vertical)
                    .lineLimit(4...8)
            }
        }
        .navigationTitle("New Message")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .confirmationAction) {
                Button("Send") {
                    guard !number.isEmpty, !text.isEmpty else { return }
                    service.sipHandler.sendMessage(to: number, text: text)
                    let msg = ChatMessage(remoteNumber: number, body: text,
                                         isOutgoing: true, timestamp: Date(),
                                         status: .sent, messageType: type)
                    _ = try? ChatMessageDao().insert(msg)
                    dismiss()
                }
                .disabled(number.isEmpty || text.isEmpty)
            }
        }
    }
}
