import SwiftUI
import Combine

/// Mirrors Android `ChatDetailScreen.kt` — SIP MESSAGE conversation view.
struct ChatDetailScreen: View {
    @EnvironmentObject var service: SipService
    let remoteNumber: String
    @State private var messages: [ChatMessage] = []
    @State private var draft = ""
    @State private var cancellable: AnyCancellable?
    private let dao = ChatMessageDao()

    var body: some View {
        VStack(spacing: 0) {
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 8) {
                    ForEach(messages) { m in
                        HStack {
                            if m.isOutgoing { Spacer() }
                            Text(m.body)
                                .padding(10)
                                .background(m.isOutgoing ? Color.accentColor : Color.gray.opacity(0.2))
                                .foregroundColor(m.isOutgoing ? .white : .primary)
                                .clipShape(RoundedRectangle(cornerRadius: 16))
                            if !m.isOutgoing { Spacer() }
                        }
                    }
                }
                .padding()
            }
            Divider()
            HStack {
                TextField("Message", text: $draft)
                    .textFieldStyle(.roundedBorder)
                Button {
                    guard !draft.isEmpty else { return }
                    service.sipHandler.sendMessage(to: remoteNumber, text: draft)
                    _ = try? dao.insert(ChatMessage(
                        id: nil,
                        remoteNumber: remoteNumber,
                        body: draft,
                        isOutgoing: true,
                        timestamp: Date(),
                        status: .sent
                    ))
                    draft = ""
                } label: {
                    Image(systemName: "paperplane.fill")
                }
                .disabled(draft.isEmpty)
            }
            .padding()
        }
        .navigationTitle(remoteNumber)
        .navigationBarTitleDisplayMode(.inline)
        .onAppear {
            cancellable = dao.observeConversation(with: remoteNumber)
                .replaceError(with: [])
                .receive(on: DispatchQueue.main)
                .sink { messages = $0 }
        }
    }
}
