import SwiftUI
import AppKit
import Foundation

@MainActor
final class TerminalSession: ObservableObject {
    @Published var output = ""
    @Published var connected = false
    private var process: Process?
    private var input: Pipe?

    func connect(to host: SSHHost) {
        guard process == nil else { return }
        output = "Connecting to \(host.alias)…\n"
        let proc = Process(), stdin = Pipe(), stdout = Pipe()
        proc.executableURL = URL(fileURLWithPath: "/usr/bin/script")
        proc.arguments = ["-q", "/dev/null", "/usr/bin/ssh", "-o", "BatchMode=yes", "-o", "ServerAliveInterval=30"] + host.connectionArguments
        proc.standardInput = stdin
        proc.standardOutput = stdout
        proc.standardError = stdout
        stdout.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty else { return }
            let text = String(decoding: data, as: UTF8.self)
            Task { @MainActor in self?.append(text) }
        }
        proc.terminationHandler = { [weak self] process in
            Task { @MainActor in
                self?.connected = false
                self?.output.append("\n[connection closed: \(process.terminationStatus)]\n")
                self?.process = nil
                self?.input = nil
            }
        }
        do {
            try proc.run()
            process = proc
            input = stdin
            connected = true
        } catch {
            output.append("Failed to start ssh: \(error.localizedDescription)\n")
        }
    }

    func send(_ text: String) {
        guard connected, let data = text.data(using: .utf8) else { return }
        input?.fileHandleForWriting.write(data)
    }

    func close() {
        input?.fileHandleForWriting.closeFile()
        process?.terminate()
        process = nil
        input = nil
        connected = false
    }

    private func append(_ raw: String) {
        let ansiPattern = #"\u{001B}(?:\[[0-?]*[ -/]*[@-~]|\][^\u{0007}]*(?:\u{0007}|\u{001B}\\))"#
        let clean = raw.replacingOccurrences(of: ansiPattern, with: "", options: .regularExpression)
            .replacingOccurrences(of: "\r\n", with: "\n")
            .replacingOccurrences(of: "\r", with: "")
        output.append(clean)
        if output.count > 300_000 { output.removeFirst(output.count - 250_000) }
    }
}

private final class InteractiveTerminalTextView: NSTextView {
    var send: ((String) -> Void)?
    override var acceptsFirstResponder: Bool { true }

    override func mouseDown(with event: NSEvent) {
        window?.makeFirstResponder(self)
        super.mouseDown(with: event)
    }

    override func keyDown(with event: NSEvent) {
        let control = event.modifierFlags.intersection(.deviceIndependentFlagsMask).contains(.control)
        let plain = event.charactersIgnoringModifiers?.lowercased()
        if control, let character = plain?.unicodeScalars.first, character.value >= 64, character.value <= 95 {
            send?(String(UnicodeScalar(character.value - 64)!))
            return
        }
        switch event.keyCode {
        case 36, 76: send?("\r")
        case 48: send?("\t")
        case 51: send?("\u{7f}")
        case 53: send?("\u{1b}")
        case 123: send?("\u{1b}[D")
        case 124: send?("\u{1b}[C")
        case 125: send?("\u{1b}[B")
        case 126: send?("\u{1b}[A")
        default:
            if let characters = event.characters, !characters.isEmpty { send?(characters) }
        }
    }
}

private struct TerminalCanvas: NSViewRepresentable {
    let text: String
    let send: (String) -> Void

    func makeNSView(context: Context) -> NSScrollView {
        let scroll = NSScrollView()
        scroll.hasVerticalScroller = true
        scroll.autohidesScrollers = false
        scroll.drawsBackground = true
        scroll.backgroundColor = NSColor(red: 0.059, green: 0.067, blue: 0.082, alpha: 1)
        let view = InteractiveTerminalTextView()
        view.send = send
        view.isEditable = false
        view.isSelectable = true
        view.isRichText = false
        view.drawsBackground = true
        view.backgroundColor = scroll.backgroundColor
        view.textColor = NSColor(red: 0.90, green: 0.91, blue: 0.94, alpha: 1)
        view.font = NSFont.monospacedSystemFont(ofSize: 13, weight: .regular)
        view.textContainerInset = NSSize(width: 8, height: 8)
        view.autoresizingMask = [.width]
        view.isVerticallyResizable = true
        view.isHorizontallyResizable = false
        view.textContainer?.widthTracksTextView = true
        scroll.documentView = view
        DispatchQueue.main.async { view.window?.makeFirstResponder(view) }
        return scroll
    }

    func updateNSView(_ scroll: NSScrollView, context: Context) {
        guard let view = scroll.documentView as? InteractiveTerminalTextView else { return }
        view.send = send
        guard view.string != text else { return }
        let wasAtBottom = view.visibleRect.maxY >= view.bounds.maxY - 30
        view.string = text
        if wasAtBottom || text.isEmpty { view.scrollToEndOfDocument(nil) }
    }
}

struct TerminalView: View {
    let host: SSHHost
    @ObservedObject var session: TerminalSession

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Image(systemName: "terminal")
                Text(host.alias).font(.headline)
                Circle().fill(session.connected ? Color.green : Color.secondary).frame(width: 8, height: 8)
                Spacer()
                Button("Reconnect") { session.close(); session.connect(to: host) }
            }
            .padding(10)
            .background(Color(nsColor: .windowBackgroundColor))
            TerminalCanvas(text: session.output, send: session.send)
        }
        .onAppear { session.connect(to: host) }
    }
}
