import SwiftUI
import AppKit

/// Applies the dashboard palette only to native scrollbars in the content
/// view. Unlike `preferredColorScheme`, this deliberately leaves the window
/// title bar and toolbar tabs under macOS system appearance control.
struct ScrollBarTheme: NSViewRepresentable {
    let isLight: Bool

    final class Coordinator {
        private weak var styledWindow: NSWindow?
        private var styledLight: Bool?
        private var updateScheduled = false

        func applyIfNeeded(from view: NSView, isLight: Bool) {
            guard !updateScheduled else { return }
            updateScheduled = true
            DispatchQueue.main.async { [weak self, weak view] in
                guard let self else { return }
                self.updateScheduled = false
                guard let window = view?.window, let root = window.contentView else { return }
                guard self.styledWindow !== window || self.styledLight != isLight else { return }
                self.styledWindow = window
                self.styledLight = isLight
                self.apply(to: root, isLight: isLight)
            }
        }

        private func apply(to view: NSView, isLight: Bool) {
            if let scrollView = view as? NSScrollView {
                let style: NSScroller.KnobStyle = isLight ? .dark : .light
                scrollView.verticalScroller?.knobStyle = style
                scrollView.horizontalScroller?.knobStyle = style
                scrollView.verticalScroller?.needsDisplay = true
                scrollView.horizontalScroller?.needsDisplay = true
            }
            for child in view.subviews { apply(to: child, isLight: isLight) }
        }
    }

    func makeCoordinator() -> Coordinator { Coordinator() }

    func makeNSView(context: Context) -> NSView {
        let view = NSView(frame: .zero)
        context.coordinator.applyIfNeeded(from: view, isLight: isLight)
        return view
    }

    func updateNSView(_ nsView: NSView, context: Context) {
        context.coordinator.applyIfNeeded(from: nsView, isLight: isLight)
    }
}

// MARK: - Formatting helpers (ports of the JS helpers)

func pct(_ a: Int, _ t: Int) -> Int { t > 0 ? Int((Double(a) / Double(t) * 100).rounded()) : 0 }

func fmtMem(_ mb: Int) -> String {
    if mb >= 1024 * 1024 { return String(format: "%.1f TB", Double(mb) / (1024 * 1024)) }
    if mb >= 1024        { return String(format: "%.1f GB", Double(mb) / 1024) }
    return "\(mb) MB"
}

/// For idle-ratio bars: low idle = warn/crit, healthy idle = green.
func idleBarColor(_ p: Int, _ pal: Palette) -> Color {
    p <= 10 ? pal.bad : p <= 30 ? pal.warn : pal.good
}

/// Format a Slurm submit timestamp ("2026-08-25T14:03:11") as "MM-DD HH:MM".
func fmtDate(_ s: String?) -> String {
    guard let s, s != "N/A", s != "Unknown", !s.isEmpty else { return "—" }
    let df = DateFormatter()
    df.locale = Locale(identifier: "en_US_POSIX")
    df.dateFormat = "yyyy-MM-dd'T'HH:mm:ss"
    if let d = df.date(from: s) {
        let out = DateFormatter()
        out.locale = Locale(identifier: "en_US_POSIX")
        out.dateFormat = "MM-dd HH:mm"
        return out.string(from: d)
    }
    return String(s.prefix(10))
}

let stateAbbr: [String: String] = [
    "RUNNING": "R", "COMPLETING": "CG", "PENDING": "PD", "COMPLETED": "CD",
    "FAILED": "F", "CANCELLED": "CA", "TIMEOUT": "TO", "NODE_FAIL": "NF", "PREEMPTED": "PR",
]

// MARK: - Bars

/// Full-width idle-ratio bar (summary cards).
struct IdleBar: View {
    let percent: Int
    let palette: Palette
    var height: CGFloat = 8
    var body: some View {
        GeometryReader { geo in
            ZStack(alignment: .leading) {
                RoundedRectangle(cornerRadius: height / 2).fill(palette.barTrack)
                RoundedRectangle(cornerRadius: height / 2)
                    .fill(idleBarColor(percent, palette))
                    .frame(width: geo.size.width * CGFloat(percent) / 100)
            }
        }
        .frame(height: height)
    }
}

/// Inline 60px green mini-bar used inside table cells.
struct MiniBar: View {
    let percent: Int
    let palette: Palette
    var body: some View {
        ZStack(alignment: .leading) {
            RoundedRectangle(cornerRadius: 3).fill(palette.barTrack)
            RoundedRectangle(cornerRadius: 3)
                .fill(palette.good)
                .frame(width: 60 * CGFloat(min(max(percent, 0), 100)) / 100)
        }
        .frame(width: 60, height: 6)
    }
}

/// Data tables should behave like their HTML counterparts: one compact line
/// per record.  Applying this once at a container prevents SwiftUI from
/// wrapping numbers into unreadable vertical stacks when a split divider is
/// dragged.
struct SingleLineTable: ViewModifier {
    func body(content: Content) -> some View {
        content
            .lineLimit(1)
            .fixedSize(horizontal: false, vertical: true)
    }
}

extension View {
    func singleLineTable() -> some View { modifier(SingleLineTable()) }
}

// MARK: - Pills

struct Pill: View {
    let text: String
    let fg: Color
    var body: some View {
        Text(text)
            .font(.system(size: 11, weight: .semibold))
            .textCase(.uppercase)
            .padding(.horizontal, 7).padding(.vertical, 2)
            .background(fg.opacity(0.15))
            .foregroundStyle(fg)
            .clipShape(Capsule())
    }
}

/// Node/partition state pill, colour chosen like the JS `statePill`.
struct StatePill: View {
    let state: String
    let palette: Palette
    var body: some View {
        let s = state.lowercased()
        let color: Color =
            s.contains("idle") ? palette.good
            : (s.contains("mix") || s.contains("alloc")) ? palette.warn
            : (s.contains("down") || s.contains("drain") || s.contains("fail") || s.contains("maint")) ? palette.bad
            : s.contains("up") ? palette.good
            : palette.muted
        Pill(text: state, fg: color)
    }
}

/// Small monospaced job-state abbreviation (My Jobs tables).
struct JobStateLabel: View {
    let state: String
    let done: Bool
    let palette: Palette
    var body: some View {
        let abbr = stateAbbr[state] ?? String(state.prefix(2))
        let color: Color = done ? palette.muted
            : (state == "RUNNING" || state == "COMPLETING") ? palette.good
            : state == "PENDING" ? palette.warn : palette.muted
        Text(abbr)
            .font(.system(size: 11, weight: .bold))
            .foregroundStyle(color)
    }
}

/// A clickable, sortable column header with sort arrow / badge.
struct SortHeader: View {
    let label: String
    let arrow: String     // "", " ↑", " ↓"
    let badge: String     // "", " ①" …
    let palette: Palette
    let action: () -> Void
    var body: some View {
        Button(action: action) {
            Text(label + arrow + badge)
                .font(.system(size: 12, weight: .semibold))
                .textCase(.uppercase)
                .foregroundStyle(palette.muted)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
        .buttonStyle(.plain)
    }
}
