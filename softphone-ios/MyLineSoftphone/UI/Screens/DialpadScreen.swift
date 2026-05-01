import SwiftUI

/// Outgoing-call dialpad. Mirrors Android `DialpadScreen.kt`.
struct DialpadScreen: View {
    @EnvironmentObject var service: SipService
    @State private var number: String = ""

    private let rows: [[(String, String?)]] = [
        [("1", nil), ("2", "ABC"), ("3", "DEF")],
        [("4", "GHI"), ("5", "JKL"), ("6", "MNO")],
        [("7", "PQRS"), ("8", "TUV"), ("9", "WXYZ")],
        [("*", nil), ("0", "+"), ("#", nil)],
    ]

    var body: some View {
        VStack(spacing: 24) {
            Text(number.isEmpty ? " " : number)
                .font(.system(size: 36, weight: .light))
                .frame(maxWidth: .infinity)
                .padding(.top, 32)

            ForEach(0..<rows.count, id: \.self) { r in
                HStack(spacing: 24) {
                    ForEach(0..<rows[r].count, id: \.self) { c in
                        let item = rows[r][c]
                        DialpadButton(digit: item.0, letters: item.1) {
                            number.append(item.0)
                        }
                    }
                }
            }

            HStack(spacing: 32) {
                Spacer().frame(width: 72)
                Button {
                    guard !number.isEmpty else { return }
                    service.startOutgoingCall(number: number)
                } label: {
                    Image(systemName: "phone.fill")
                        .font(.system(size: 28))
                        .foregroundColor(.white)
                        .frame(width: 72, height: 72)
                        .background(Color.green)
                        .clipShape(Circle())
                }
                Button {
                    if !number.isEmpty { number.removeLast() }
                } label: {
                    Image(systemName: "delete.left")
                        .font(.system(size: 24))
                        .frame(width: 72, height: 72)
                        .foregroundColor(.primary)
                }
            }

            Spacer()
        }
        .padding()
    }
}
