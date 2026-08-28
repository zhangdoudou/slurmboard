import SwiftUI

extension Color {
    /// Hex string like "#0f1115".
    init(hex: String) {
        var s = hex
        if s.hasPrefix("#") { s.removeFirst() }
        let v = UInt64(s, radix: 16) ?? 0
        self.init(.sRGB,
                  red:   Double((v >> 16) & 0xff) / 255,
                  green: Double((v >> 8) & 0xff) / 255,
                  blue:  Double(v & 0xff) / 255,
                  opacity: 1)
    }
}

/// The dark / light colour sets, transcribed from slurmboard.py's CSS `:root`.
struct Palette {
    let bg, panel, border, text, muted, accent, good, warn, bad, barTrack: Color

    static let dark = Palette(
        bg: Color(hex: "0f1115"), panel: Color(hex: "171a21"), border: Color(hex: "2a2f3a"),
        text: Color(hex: "e6e9ef"), muted: Color(hex: "8b93a3"), accent: Color(hex: "4f8cff"),
        good: Color(hex: "3ec97c"), warn: Color(hex: "f0a93f"), bad: Color(hex: "ef5b5b"),
        barTrack: Color(hex: "2a2f3a"))

    static let light = Palette(
        bg: Color(hex: "f4f5f7"), panel: Color(hex: "ffffff"), border: Color(hex: "dde1ea"),
        text: Color(hex: "1a1d23"), muted: Color(hex: "6b7280"), accent: Color(hex: "2563eb"),
        good: Color(hex: "16a34a"), warn: Color(hex: "d97706"), bad: Color(hex: "dc2626"),
        barTrack: Color(hex: "d5d9e3"))
}

/// Observable theme with persisted light/dark preference.
final class Theme: ObservableObject {
    @Published var isLight: Bool {
        didSet { UserDefaults.standard.set(isLight, forKey: "sb_theme_light") }
    }
    init() { isLight = UserDefaults.standard.bool(forKey: "sb_theme_light") }

    var palette: Palette { isLight ? .light : .dark }
}

// Convenience so views can read `theme.p.accent` etc.
extension Theme { var p: Palette { palette } }
