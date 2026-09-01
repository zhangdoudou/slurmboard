import SwiftUI
import AppKit
import SwiftTerm

@MainActor
final class TerminalSession: NSObject, ObservableObject, @preconcurrency LocalProcessTerminalViewDelegate {
    @Published private(set) var connected = false

    let terminalView: LocalProcessTerminalView
    private var currentHost: SSHHost?
    private var reconnectHost: SSHHost?
    private var closing = false

    override init() {
        terminalView = LocalProcessTerminalView(frame: .zero)
        super.init()
        terminalView.processDelegate = self
        terminalView.font = NSFont.monospacedSystemFont(ofSize: 13, weight: .regular)
        terminalView.nativeForegroundColor = NSColor(
            red: 0.90, green: 0.91, blue: 0.94, alpha: 1
        )
        terminalView.nativeBackgroundColor = NSColor(
            red: 0.059, green: 0.067, blue: 0.082, alpha: 1
        )
        terminalView.disableFullRedrawOnAnyChanges = true
    }

    func connect(to host: SSHHost) {
        guard !terminalView.process.running else { return }
        currentHost = host
        closing = false
        startSSH(to: host)
    }

    func reconnect(to host: SSHHost) {
        currentHost = host
        closing = false
        if terminalView.process.running {
            reconnectHost = host
            terminalView.terminate()
        } else {
            startSSH(to: host)
        }
    }

    func close() {
        closing = true
        reconnectHost = nil
        connected = false
        if terminalView.process.running {
            terminalView.terminate()
        }
    }

    private func startSSH(to host: SSHHost) {
        let arguments = [
            "-o", "ServerAliveInterval=30",
            "-o", "ServerAliveCountMax=3"
        ] + host.connectionArguments
        terminalView.startProcess(
            executable: "/usr/bin/ssh",
            args: arguments,
            execName: "ssh"
        )
        connected = terminalView.process.running
        DispatchQueue.main.async { [weak self] in
            guard let terminalView = self?.terminalView else { return }
            terminalView.window?.makeFirstResponder(terminalView)
        }
    }

    func sizeChanged(source: LocalProcessTerminalView, newCols: Int, newRows: Int) {}

    func setTerminalTitle(source: LocalProcessTerminalView, title: String) {}

    func hostCurrentDirectoryUpdate(source: SwiftTerm.TerminalView, directory: String?) {}

    func processTerminated(source: SwiftTerm.TerminalView, exitCode: Int32?) {
        Task { @MainActor [weak self] in
            guard let self else { return }
            connected = false
            if !closing, let host = reconnectHost {
                reconnectHost = nil
                startSSH(to: host)
            }
        }
    }
}

private struct TerminalCanvas: NSViewRepresentable {
    @ObservedObject var session: TerminalSession

    func makeNSView(context: Context) -> LocalProcessTerminalView {
        session.terminalView
    }

    func updateNSView(_ terminalView: LocalProcessTerminalView, context: Context) {}
}

struct TerminalView: View {
    let host: SSHHost
    @ObservedObject var session: TerminalSession

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Image(systemName: "terminal")
                Text(host.alias).font(.headline)
                Circle()
                    .fill(session.connected ? Color.green : Color.secondary)
                    .frame(width: 8, height: 8)
                Spacer()
                Button("Reconnect") { session.reconnect(to: host) }
            }
            .padding(10)
            .background(Color(nsColor: .windowBackgroundColor))

            TerminalCanvas(session: session)
        }
        .onAppear { session.connect(to: host) }
    }
}
