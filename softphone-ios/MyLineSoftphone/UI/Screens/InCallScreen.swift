import SwiftUI

/// In-call UI: remote party, timer, mute/hold/keypad/transfer/hangup.
/// Mirrors Android `InCallScreen.kt`.
struct InCallScreen: View {
    @EnvironmentObject var service: SipService
    @State private var muted = false
    @State private var held = false
    @State private var speakerOn = false
    @State private var showTransfer = false

    var body: some View {
        VStack(spacing: 24) {
            Spacer().frame(height: 40)
            Text(service.sipHandler.remoteName.isEmpty ? service.sipHandler.remoteNumber : service.sipHandler.remoteName)
                .font(.system(size: 32, weight: .light))
            Text(service.sipHandler.callState.rawValue.capitalized)
                .font(.system(size: 16))
                .foregroundColor(.secondary)

            Spacer()

            // Row 1: Mute · Speaker · Hold
            HStack(spacing: 32) {
                CallActionButton(icon: muted ? "mic.slash.fill" : "mic.fill",
                                 label: "Mute", active: muted) {
                    muted.toggle()
                    service.sipHandler.setMuted(muted)
                }
                CallActionButton(icon: speakerOn ? "speaker.wave.3.fill" : "speaker.fill",
                                 label: "Speaker", active: speakerOn) {
                    speakerOn.toggle()
                    service.setSpeaker(speakerOn)
                }
                CallActionButton(icon: "pause.fill",
                                 label: "Hold", active: held) {
                    held.toggle()
                    service.sipHandler.setHold(held)
                }
            }

            // Row 2: Transfer
            HStack(spacing: 32) {
                CallActionButton(icon: "arrow.uturn.forward",
                                 label: "Transfer", active: false) {
                    showTransfer = true
                }
            }

            Button {
                service.sipHandler.hangup()
            } label: {
                Image(systemName: "phone.down.fill")
                    .font(.system(size: 30))
                    .foregroundColor(.white)
                    .frame(width: 84, height: 84)
                    .background(Color.red)
                    .clipShape(Circle())
            }

            Spacer().frame(height: 32)
        }
        .padding()
        .sheet(isPresented: $showTransfer) {
            TransferDialog()
        }
    }
}

private struct CallActionButton: View {
    let icon: String
    let label: String
    let active: Bool
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            VStack(spacing: 8) {
                Image(systemName: icon)
                    .font(.system(size: 24))
                    .frame(width: 64, height: 64)
                    .background(active ? Color.accentColor : Color.gray.opacity(0.2))
                    .foregroundColor(active ? .white : .primary)
                    .clipShape(Circle())
                Text(label).font(.caption)
            }
        }
    }
}
