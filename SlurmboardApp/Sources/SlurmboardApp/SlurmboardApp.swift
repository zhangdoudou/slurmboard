import SwiftUI
import AppKit

@main
struct SlurmboardApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @StateObject private var manager = ConnectionManager()

    init() {
        // Ensure the app shows up as a normal, focusable GUI app even when
        // launched from a terminal via `swift run`.
        NSApplication.shared.setActivationPolicy(.regular)
    }

    var body: some Scene {
        WindowGroup("") {
            WorkspaceView()
                .environmentObject(manager)
                .onAppear {
                    appDelegate.manager = manager
                    appDelegate.fillVisibleScreen()
                }
        }
        .defaultSize(width: 1680, height: 920)
        .windowResizability(.automatic)
    }
}

/// A single-window workspace. Hosts and every connected cluster live in tabs,
/// so opening a dashboard never creates another macOS window.
private struct WorkspaceView: View {
    @EnvironmentObject private var manager: ConnectionManager
    @State private var pendingTerminalClose: UUID?

    var body: some View {
        ZStack {
            // Keep stateful workspaces mounted. Tab changes only switch their
            // visibility and hit testing, just like browser tabs.
            SFTPView()
                .opacity(manager.selectedTab == .sftp ? 1 : 0)
                .allowsHitTesting(manager.selectedTab == .sftp)

            if manager.selectedTab == .home {
                HostPickerView()
            }

            ForEach(manager.connectionIDs, id: \.self) { id in
                ClusterWindowView(connectionID: id)
                    .id(id)
                    .opacity(manager.selectedTab == .cluster(id) ? 1 : 0)
                    .allowsHitTesting(manager.selectedTab == .cluster(id))
                    .accessibilityHidden(manager.selectedTab != .cluster(id))
            }

            if case .terminal(let id) = manager.selectedTab,
               let descriptor = manager.terminal(for: id) {
                TerminalView(host: descriptor.host, session: descriptor.session)
            }
        }
        .toolbar {
            ToolbarItemGroup(placement: .navigation) {
                workspaceTab("Hosts", tab: .home)
                workspaceTab("SFTP", tab: .sftp)
                ForEach(manager.connectionIDs, id: \.self) { id in
                    if let service = manager.service(for: id) {
                        workspaceTab("SlurmBoard · \(service.host.alias)", tab: .cluster(id))
                    }
                }
                ForEach(manager.terminalIDs, id: \.self) { id in
                    if let item = manager.terminal(for: id) {
                        workspaceTab("Terminal · \(item.host.alias)", tab: .terminal(id))
                    }
                }
            }
        }
        .navigationTitle("")
        .frame(minWidth: 1420, minHeight: 680)
        .alert("Close active terminal?", isPresented: Binding(
            get: { pendingTerminalClose != nil },
            set: { if !$0 { pendingTerminalClose = nil } }
        )) {
            Button("Cancel", role: .cancel) { pendingTerminalClose = nil }
            Button("Close Terminal", role: .destructive) {
                if let id = pendingTerminalClose { manager.closeTerminal(id) }
                pendingTerminalClose = nil
            }
        } message: {
            Text("The SSH session may still be running a command. Closing this tab will terminate the session.")
        }
    }

    private func workspaceTab(_ title: String, tab: AppTab) -> some View {
        HStack(spacing: 5) {
            Button { manager.selectedTab = tab } label: {
                Text(title)
                    .font(.system(size: 12, weight: manager.selectedTab == tab ? .semibold : .regular))
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            if case .cluster(let id) = tab {
                Button { manager.close(id) } label: {
                    Image(systemName: "xmark").font(.system(size: 9, weight: .semibold))
                        .foregroundStyle(.secondary)
                }
                .buttonStyle(.plain)
                .help("Close \(title)")
            } else if case .terminal(let id) = tab {
                Button { requestTerminalClose(id) } label: {
                    Image(systemName: "xmark").font(.system(size: 9, weight: .semibold))
                        .foregroundStyle(.secondary)
                }
                .buttonStyle(.plain)
                .help("Close \(title)")
            }
        }
        .frame(minWidth: 140)
        .padding(.horizontal, 10).padding(.vertical, 5)
        .background(manager.selectedTab == tab ? Color.secondary.opacity(0.18) : Color.clear)
        .clipShape(Capsule())
    }

    private func requestTerminalClose(_ id: UUID) {
        if manager.terminal(for: id)?.session.connected == true {
            pendingTerminalClose = id
        } else {
            manager.closeTerminal(id)
        }
    }
}

/// Handles process-wide concerns: bringing the app to the front on launch and
/// closing every SSH master connection on quit so nothing lingers.
final class AppDelegate: NSObject, NSApplicationDelegate {
    weak var manager: ConnectionManager?

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.activate(ignoringOtherApps: true)
    }

    /// Fill the current display's usable frame without entering full-screen
    /// mode (the menu bar/Dock remain in their normal macOS spaces).
    func fillVisibleScreen() {
        DispatchQueue.main.async {
            guard let window = NSApp.keyWindow ?? NSApp.windows.first,
                  let screen = window.screen ?? NSScreen.main else { return }
            window.setFrame(screen.visibleFrame, display: true, animate: false)
        }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        false   // keep running so cluster windows can outlive the picker
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        let active = MainActor.assumeIsolated { manager?.hasActiveWork == true }
        guard active else { return .terminateNow }
        let detail = MainActor.assumeIsolated { manager?.activeWorkDescription ?? "active work" }
        let alert = NSAlert()
        alert.alertStyle = .warning
        alert.messageText = "Quit Slurmboard?"
        alert.informativeText = "There is \(detail). Quitting will interrupt it."
        alert.addButton(withTitle: "Quit Anyway")
        alert.addButton(withTitle: "Cancel")
        return alert.runModal() == .alertFirstButtonReturn ? .terminateNow : .terminateCancel
    }

    func applicationWillTerminate(_ notification: Notification) {
        MainActor.assumeIsolated { manager?.shutdownAll() }
    }
}
