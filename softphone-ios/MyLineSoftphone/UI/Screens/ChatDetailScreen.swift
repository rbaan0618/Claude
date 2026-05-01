import SwiftUI
import Combine

/// Mirrors Android `ChatDetailScreen.kt` — per-conversation message thread.
struct ChatDetailScreen: View {
    @EnvironmentObject var service: SipService
    let remoteNumber: String
    let messageType: String          // "sms" or "whatsapp"

    @State private var messages: [ChatMessage] = []
    @State private var draft = ""
    @State private var cancellable: AnyCancellable?

    private let dao = ChatMessageDao()

    private var isWhatsApp: Bool { messageType == "whatsapp" }
    private var channelColor: Color {
        isWhatsApp ? Color(red: 0.145, green: 0.827, blue: 0.400) : .accentColor
    }

    var body: some View {
        VStack(spacing: 0) {
            // Message list — auto-scrolls to bottom when new messages arrive.
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(spacing: 4) {
                        ForEach(messages) { m in
                            MessageBubble(message: m, channelColor: channelColor)
                                .id(m.id)
                        }
                    }
                    .padding(.horizontal, 16)
                    .padding(.vertical, 8)
                }
                .onChange(of: messages.count) { _ in
                    if let last = messages.last?.id {
                        withAnimation { proxy.scrollTo(last, anchor: .bottom) }
                    }
                }
            }

            Divider()

            // Input bar
            HStack(spacing: 8) {
                TextField(isWhatsApp ? "WhatsApp message..." : "SMS message...",
                          text: $draft, axis: .vertical)
                    .textFieldStyle(.roundedBorder)
                    .lineLimit(1...4)

                Button {
                    send()
                } label: {
                    Image(systemName: "paperplane.fill")
                        .foregroundColor(draft.isEmpty ? .secondary : channelColor)
                }
                .disabled(draft.isEmpty)
            }
            .padding(8)
        }
        .navigationTitle(remoteNumber)
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            // Channel badge
            ToolbarItem(placement: .principal) {
                VStack(spacing: 1) {
                    Text(remoteNumber).font(.headline)
                    Label(isWhatsApp ? "WhatsApp" : "SMS",
                          systemImage: isWhatsApp ? "phone.bubble.left.fill" : "message.fill")
                        .font(.caption2)
                        .foregroundColor(channelColor)
                }
            }
            // Call button
            ToolbarItem(placement: .navigationBarTrailing) {
                Button {
                    service.startOutgoingCall(number: remoteNumber)
                } label: {
                    Image(systemName: "phone.fill")
                        .foregroundColor(.green)
                }
            }
        }
        .onAppear {
            cancellable = dao.observeConversation(with: remoteNumber, messageType: messageType)
                .replaceError(with: [])
                .receive(on: DispatchQueue.main)
                .sink { messages = $0 }
        }
        .onDisappear { cancellable = nil }
    }

    private func send() {
        let text = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }
        service.sipHandler.sendMessage(to: remoteNumber, text: text)
        let msg = ChatMessage(remoteNumber: remoteNumber, body: text,
                              isOutgoing: true, timestamp: Date(),
                              status: .sent, messageType: messageType)
        _ = try? dao.insert(msg)
        draft = ""
    }
}

// MARK: - Message bubble

private struct MessageBubble: View {
    let message: ChatMessage
    let channelColor: Color

    var body: some View {
        HStack {
            if message.isOutgoing { Spacer(minLength: 60) }

            VStack(alignment: message.isOutgoing ? .trailing : .leading, spacing: 2) {
                Text(message.body)
                    .padding(.horizontal, 12)
                    .padding(.vertical, 8)
                    .background(message.isOutgoing ? channelColor : Color(.systemGray5))
                    .foregroundColor(message.isOutgoing ? .white : .primary)
                    .clipShape(RoundedRectangle(cornerRadius: 16, style: .continuous))

                Text(formatTime(message.timestamp))
                    .font(.caption2)
                    .foregroundColor(.secondary)
                    .padding(.horizontal, 4)
            }

            if !message.isOutgoing { Spacer(minLength: 60) }
        }
    }

    private func formatTime(_ date: Date) -> String {
        date.formatted(date: .omitted, time: .shortened)
    }
}
