import Foundation
import Combine

enum AppTab: Hashable {
    case home
    case sftp
    case cluster(UUID)
    case terminal(UUID)
}

struct TerminalDescriptor: Identifiable, Hashable {
    let id: UUID
    let host: SSHHost
    let session: TerminalSession

    static func == (lhs: TerminalDescriptor, rhs: TerminalDescriptor) -> Bool { lhs.id == rhs.id }
    func hash(into hasher: inout Hasher) { hasher.combine(id) }
}

/// App-wide registry of live cluster services. Each SwiftUI cluster window is
/// opened with a service `UUID` and looks the live `SlurmService` up here, so
/// the connection survives independently of any particular view.
@MainActor
final class ConnectionManager: ObservableObject {

    @Published private(set) var services: [UUID: SlurmService] = [:]
    @Published private(set) var connectionIDs: [UUID] = []
    @Published private(set) var hosts: [SSHHost] = []
    @Published private(set) var terminals: [UUID: TerminalDescriptor] = [:]
    @Published private(set) var terminalIDs: [UUID] = []
    @Published var selectedTab: AppTab = .home
    let sftpTransfers = SFTPTransferQueue()

    init() {
        loadSavedHosts()
    }

    private var hostsURL: URL {
        let dir = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("Slurmboard", isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir.appendingPathComponent("hosts.json")
    }

    private func loadSavedHosts() {
        guard let data = try? Data(contentsOf: hostsURL),
              let saved = try? JSONDecoder().decode([SSHHost].self, from: data) else {
            hosts = []
            return
        }
        hosts = saved
    }

    private func saveHosts() {
        guard let data = try? JSONEncoder().encode(hosts) else { return }
        try? data.write(to: hostsURL, options: .atomic)
    }

    func addHost(_ host: SSHHost) {
        if let index = hosts.firstIndex(where: { $0.alias == host.alias }) { hosts[index] = host }
        else { hosts.append(host) }
        saveHosts()
    }

    func importHosts(_ imported: [SSHHost]) {
        for host in imported where !hosts.contains(where: { $0.alias == host.alias }) {
            hosts.append(host)
        }
        saveHosts()
    }

    func removeHosts(at offsets: IndexSet) {
        for index in offsets.sorted(by: >) where hosts.indices.contains(index) {
            hosts.remove(at: index)
        }
        saveHosts()
    }

    /// Create, register, and connect a service. Returns its id so the caller
    /// can open a window bound to it.
    func connect(host: SSHHost) -> UUID {
        if let existing = connectionIDs.first(where: { services[$0]?.host.id == host.id }) {
            selectedTab = .cluster(existing)
            return existing
        }
        let svc = SlurmService(host: host)
        services[svc.id] = svc
        connectionIDs.append(svc.id)
        selectedTab = .cluster(svc.id)
        svc.connect()
        return svc.id
    }

    func service(for id: UUID) -> SlurmService? { services[id] }

    func openTerminal(host: SSHHost) {
        if let id = terminalIDs.first(where: { terminals[$0]?.host.id == host.id }) {
            selectedTab = .terminal(id)
            return
        }
        let item = TerminalDescriptor(id: UUID(), host: host, session: TerminalSession())
        terminals[item.id] = item
        terminalIDs.append(item.id)
        selectedTab = .terminal(item.id)
    }

    func terminal(for id: UUID) -> TerminalDescriptor? { terminals[id] }

    /// Tear down and forget a service (called when its window closes).
    func close(_ id: UUID) {
        services[id]?.disconnect()
        services.removeValue(forKey: id)
        connectionIDs.removeAll { $0 == id }
        if selectedTab == .cluster(id) { selectedTab = .home }
    }

    func closeTerminal(_ id: UUID) {
        terminals[id]?.session.close()
        terminals.removeValue(forKey: id)
        terminalIDs.removeAll { $0 == id }
        if selectedTab == .terminal(id) { selectedTab = .home }
    }

    var hasActiveWork: Bool {
        sftpTransfers.isActive || terminals.values.contains { $0.session.connected }
    }

    var activeWorkDescription: String {
        var parts: [String] = []
        if sftpTransfers.isActive { parts.append("one or more SFTP transfers") }
        let count = terminals.values.filter { $0.session.connected }.count
        if count > 0 { parts.append("\(count) active terminal session\(count == 1 ? "" : "s")") }
        return parts.joined(separator: " and ")
    }

    /// Disconnect everything — used on app quit so no ssh master lingers.
    func shutdownAll() {
        sftpTransfers.cancelAll()
        for svc in services.values { svc.disconnect() }
        for terminal in terminals.values { terminal.session.close() }
        services.removeAll()
        connectionIDs.removeAll()
        terminals.removeAll()
        terminalIDs.removeAll()
    }
}
