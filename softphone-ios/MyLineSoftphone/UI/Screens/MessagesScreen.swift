import SwiftUI
import Combine

/// Mirrors Android `MessagesScreen.kt` — list of conversations.
struct MessagesScreen: View {
    @State private var messages: [ChatMessage] = []
    @State private var cancellable: AnyCancellable?
    private let dao = ChatMessageDao()

    var conversations: [(number: String, last: ChatMessage)] {
        var seen: [String: ChatMessage] = [:]
        for m in messages {
            if seen[m.remoteNumber] == nil { seen[m.remoteNumber] = m }
        }
        return seen
            .map { (number: $0.key, last: $0.value) }
            .sorted { $0.last.timestamp > $1.last.timestamp }
    }

    var body: some View {
        NavigationStack {
            List(conversations, id: \.number) { convo in
                NavigationLink {
                    ChatDetailScreen(remoteNumber: convo.number)
                } label: {
                    VStack(alignment: .leading) {
                        Text(convo.number).font(.body)
                        Text(convo.last.body).font(.caption).foregroundColor(.secondary).lineLimit(1)
                    }
                }
            }
            .navigationTitle("Messages")
            .onAppear {
                cancellable = dao.observeConversations()
                    .replaceError(with: [])
                    .receive(on: DispatchQueue.main)
                    .sink { messages = $0 }
            }
        }
    }
}
