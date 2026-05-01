import SwiftUI

/// Circular dialpad digit button with main label + optional letter subline.
/// Mirrors Android `DialpadButton.kt`.
struct DialpadButton: View {
    let digit: String
    let letters: String?
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            VStack(spacing: 2) {
                Text(digit)
                    .font(.system(size: 32, weight: .light))
                Text(letters ?? " ")
                    .font(.system(size: 10, weight: .medium))
                    .foregroundColor(.secondary)
                    .tracking(1)
            }
            .frame(width: 72, height: 72)
            .background(Color.gray.opacity(0.15))
            .clipShape(Circle())
            .foregroundColor(.primary)
        }
    }
}
