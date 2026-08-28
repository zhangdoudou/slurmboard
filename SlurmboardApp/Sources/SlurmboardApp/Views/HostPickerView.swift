import SwiftUI

struct HostPickerView: View {
    @EnvironmentObject private var manager: ConnectionManager
    @State private var filter = ""
    @State private var showingAdd = false
    @State private var showingImport = false
    @State private var editingHost: SSHHost?

    private var filteredHosts: [SSHHost] {
        guard !filter.isEmpty else { return manager.hosts }
        let needle = filter.lowercased()
        return manager.hosts.filter {
            $0.alias.lowercased().contains(needle)
            || ($0.hostName?.lowercased().contains(needle) ?? false)
            || ($0.sshCommand?.lowercased().contains(needle) ?? false)
        }
    }

    private let columns = [GridItem(.adaptive(minimum: 300, maximum: 430), spacing: 16)]

    var body: some View {
        VStack(spacing: 0) {
            header
            TextField("Filter hosts…", text: $filter)
                .textFieldStyle(.roundedBorder).padding(.horizontal, 20).padding(.bottom, 14)

            if manager.hosts.isEmpty {
                emptyState
            } else {
                ScrollView {
                    LazyVGrid(columns: columns, alignment: .leading, spacing: 16) {
                        ForEach(filteredHosts) { host in hostCard(host) }
                    }
                    .padding(.horizontal, 20).padding(.bottom, 20)
                }
            }
        }
        .sheet(isPresented: $showingAdd) {
            HostEditorSheet { host, password, _ in manager.addHost(host, password: password) }
        }
        .sheet(item: $editingHost) { host in
            HostEditorSheet(host: host) { updated, password, clearPassword in
                manager.updateHost(updated, replacing: host.id,
                                   password: password, clearPassword: clearPassword)
            }
        }
        .sheet(isPresented: $showingImport) {
            ImportHostsSheet(existing: Set(manager.hosts.map(\.alias))) { manager.importHosts($0) }
        }
    }

    private var header: some View {
        HStack(spacing: 10) {
            Image(systemName: "server.rack").foregroundStyle(.tint)
            Text("Hosts").font(.headline)
            Text("\(manager.hosts.count)").font(.caption).foregroundStyle(.secondary)
            Spacer()
            Button { showingImport = true } label: {
                Label("Import SSH Config", systemImage: "square.and.arrow.down")
            }
            Button { showingAdd = true } label: { Label("Add Host", systemImage: "plus") }
                .buttonStyle(.borderedProminent)
        }.padding(20)
    }

    private var emptyState: some View {
        VStack(spacing: 10) {
            Spacer()
            Image(systemName: "server.rack").font(.largeTitle).foregroundStyle(.secondary)
            Text("No saved hosts").font(.headline)
            Text("Add an SSH connection or import selected entries from ~/.ssh/config.")
                .font(.callout).foregroundStyle(.secondary)
            Spacer()
        }.frame(maxWidth: .infinity)
    }

    private func hostCard(_ host: SSHHost) -> some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack(alignment: .top, spacing: 12) {
                ZStack {
                    RoundedRectangle(cornerRadius: 10).fill(Color.accentColor.opacity(0.12))
                    Image(systemName: "server.rack").foregroundStyle(.tint)
                }.frame(width: 42, height: 42)
                VStack(alignment: .leading, spacing: 4) {
                    Text(host.alias).font(.headline).lineLimit(1)
                    Text(host.subtitle.isEmpty ? (host.sshCommand ?? host.alias) : host.subtitle)
                        .font(.caption).foregroundStyle(.secondary).lineLimit(2)
                }.frame(maxWidth: .infinity, alignment: .leading)
                Menu {
                    Button("Edit", systemImage: "pencil") { editingHost = host }
                    Divider()
                    Button("Delete", systemImage: "trash", role: .destructive) { delete(host) }
                } label: {
                    Image(systemName: "ellipsis").frame(width: 24, height: 24)
                }.menuStyle(.borderlessButton)
            }

            Divider()
            HStack(spacing: 8) {
                Button { _ = manager.connect(host: host) } label: {
                    Label("SlurmBoard", systemImage: "chart.bar")
                        .frame(maxWidth: .infinity)
                }.buttonStyle(.borderedProminent)
                Button { manager.openTerminal(host: host) } label: {
                    Label("Terminal", systemImage: "terminal")
                        .frame(maxWidth: .infinity)
                }.buttonStyle(.bordered)
            }
        }
        .padding(16)
        .background(.background)
        .overlay(RoundedRectangle(cornerRadius: 14).stroke(.separator.opacity(0.7)))
        .clipShape(RoundedRectangle(cornerRadius: 14))
    }

    private func delete(_ host: SSHHost) {
        guard let index = manager.hosts.firstIndex(where: { $0.id == host.id }) else { return }
        manager.removeHosts(at: IndexSet(integer: index))
    }
}

private enum HostInputMode: String, CaseIterable, Identifiable {
    case command = "SSH Command"
    case fields = "Connection Fields"
    var id: Self { self }
}

private struct HostEditorSheet: View {
    @Environment(\.dismiss) private var dismiss
    private let editing: Bool
    @State private var mode: HostInputMode
    @State private var name: String
    @State private var command: String
    @State private var address: String
    @State private var username: String
    @State private var port: String
    @State private var identityFile: String
    @State private var proxyJump: String
    @State private var extraArguments: String
    @State private var password = ""
    @State private var clearPassword = false
    private let hadSavedPassword: Bool
    let onSave: (SSHHost, String?, Bool) -> Void

    init(host: SSHHost? = nil, onSave: @escaping (SSHHost, String?, Bool) -> Void) {
        self.editing = host != nil
        self.hadSavedPassword = host.map { CredentialStore.password(for: $0.id) != nil } ?? false
        self.onSave = onSave
        let inferred = Self.inferFields(from: host)
        _mode = State(initialValue: host?.sshCommand == nil && host != nil ? .fields : .command)
        _name = State(initialValue: host?.alias ?? "")
        _command = State(initialValue: host?.sshCommand ?? (host.map { "ssh \($0.alias)" } ?? "ssh "))
        _address = State(initialValue: inferred.address)
        _username = State(initialValue: inferred.username)
        _port = State(initialValue: inferred.port)
        _identityFile = State(initialValue: inferred.identityFile)
        _proxyJump = State(initialValue: inferred.proxyJump)
        _extraArguments = State(initialValue: inferred.extraArguments)
    }

    private var trimmedName: String { name.trimmingCharacters(in: .whitespacesAndNewlines) }
    private var trimmedCommand: String { command.trimmingCharacters(in: .whitespacesAndNewlines) }
    private var trimmedAddress: String { address.trimmingCharacters(in: .whitespacesAndNewlines) }
    private var validPort: Bool { Int(port).map { (1...65535).contains($0) } ?? false }
    private var canSave: Bool {
        !trimmedName.isEmpty && (mode == .command ? !trimmedCommand.isEmpty : (!trimmedAddress.isEmpty && validPort))
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text(editing ? "Edit SSH Host" : "Add SSH Host").font(.title2.weight(.semibold))
            TextField("Display name", text: $name)
            Picker("Input method", selection: $mode) {
                ForEach(HostInputMode.allCases) { Text($0.rawValue).tag($0) }
            }.pickerStyle(.segmented)

            Group {
                if mode == .command { commandForm } else { fieldsForm }
            }
            .padding(16)
            .background(Color.secondary.opacity(0.06))
            .clipShape(RoundedRectangle(cornerRadius: 12))

            HStack {
                Spacer()
                Button("Cancel") { dismiss() }
                Button(editing ? "Save Changes" : "Save") { save() }
                    .buttonStyle(.borderedProminent).disabled(!canSave)
            }
        }.padding(24).frame(width: 680)
    }

    private var commandForm: some View {
        VStack(alignment: .leading, spacing: 10) {
            Label("SSH command", systemImage: "terminal").font(.headline)
            TextField("ssh -J jump -i ~/.ssh/id_ed25519 user@login.example.org", text: $command)
                .font(.system(.body, design: .monospaced))
            Text("Paste the same command you use in Terminal. Options such as -p, -i, -J and -o are preserved.")
                .font(.caption).foregroundStyle(.secondary)
        }
    }

    private var fieldsForm: some View {
        VStack(alignment: .leading, spacing: 14) {
            Label("Address", systemImage: "network").font(.headline)
            TextField("IP or hostname", text: $address)
            Divider()
            Label("Credentials", systemImage: "key").font(.headline)
            HStack {
                TextField("Username", text: $username)
                TextField("Port", text: $port).frame(width: 90)
            }
            SecureField(hadSavedPassword ? "New password (leave blank to keep saved password)" : "Password (optional)",
                        text: $password)
                .onChange(of: password) { if !password.isEmpty { clearPassword = false } }
            if hadSavedPassword {
                HStack {
                    Label(clearPassword ? "Saved password will be removed" : "A password is saved in Keychain",
                          systemImage: clearPassword ? "trash" : "checkmark.shield")
                        .font(.caption).foregroundStyle(clearPassword ? Color.red : Color.secondary)
                    Spacer()
                    Button(clearPassword ? "Keep Password" : "Clear Password") {
                        clearPassword.toggle()
                        if clearPassword { password = "" }
                    }.buttonStyle(.link)
                }
            }
            TextField("Identity file, e.g. ~/.ssh/id_ed25519", text: $identityFile)
            Divider()
            Label("Connection options", systemImage: "point.3.connected.trianglepath.dotted").font(.headline)
            TextField("ProxyJump, e.g. user@jump.example.org", text: $proxyJump)
            TextField("Extra SSH arguments, e.g. -o ServerAliveInterval=30", text: $extraArguments)
                .font(.system(.body, design: .monospaced))
            Text("Passwords are stored in macOS Keychain and never written to hosts.json or command arguments.")
                .font(.caption).foregroundStyle(.secondary)
        }
    }

    private func save() {
        let sshCommand: String
        if mode == .command {
            sshCommand = trimmedCommand
        } else {
            var parts = ["ssh"]
            if port != "22" { parts += ["-p", port] }
            if !identityFile.trimmed.isEmpty { parts += ["-i", shellToken(identityFile.trimmed)] }
            if !proxyJump.trimmed.isEmpty { parts += ["-J", shellToken(proxyJump.trimmed)] }
            if !extraArguments.trimmed.isEmpty { parts.append(extraArguments.trimmed) }
            let destination = username.trimmed.isEmpty ? trimmedAddress : "\(username.trimmed)@\(trimmedAddress)"
            parts.append(shellToken(destination))
            sshCommand = parts.joined(separator: " ")
        }
        let host = SSHHost(alias: trimmedName,
                           hostName: mode == .fields ? trimmedAddress : nil,
                           proxyJump: mode == .fields && !proxyJump.trimmed.isEmpty ? proxyJump.trimmed : nil,
                           sshCommand: sshCommand)
        onSave(host, password.isEmpty ? nil : password, clearPassword)
        dismiss()
    }

    private func shellToken(_ value: String) -> String {
        guard value.contains(where: { $0.isWhitespace || "'\"\\".contains($0) }) else { return value }
        return "'" + value.replacingOccurrences(of: "'", with: "'\\''") + "'"
    }

    private static func inferFields(from host: SSHHost?) -> (
        address: String, username: String, port: String,
        identityFile: String, proxyJump: String, extraArguments: String
    ) {
        guard let host else { return ("", "", "22", "", "", "") }
        let args = host.connectionArguments
        var port = "22", identity = "", jump = host.proxyJump ?? ""
        var consumed = Set<Int>()
        var index = 0
        while index + 1 < args.count {
            switch args[index] {
            case "-p": port = args[index + 1]; consumed.formUnion([index, index + 1]); index += 2
            case "-i": identity = args[index + 1]; consumed.formUnion([index, index + 1]); index += 2
            case "-J": jump = args[index + 1]; consumed.formUnion([index, index + 1]); index += 2
            default: index += 1
            }
        }
        let destinationIndex = args.indices.reversed().first { !args[$0].hasPrefix("-") && !consumed.contains($0) }
        let destination = destinationIndex.map { args[$0] } ?? host.hostName ?? host.alias
        let split = destination.split(separator: "@", maxSplits: 1).map(String.init)
        let username = split.count == 2 ? split[0] : ""
        let address = split.count == 2 ? split[1] : destination
        if let destinationIndex { consumed.insert(destinationIndex) }
        let extra = args.indices.filter { !consumed.contains($0) }.map { args[$0] }.joined(separator: " ")
        return (address, username, port, identity, jump, extra)
    }
}

private extension String {
    var trimmed: String { trimmingCharacters(in: .whitespacesAndNewlines) }
}

private struct ImportHostsSheet: View {
    @Environment(\.dismiss) private var dismiss
    let existing: Set<String>
    let onImport: ([SSHHost]) -> Void
    @State private var candidates: [SSHHost] = []
    @State private var selected: Set<String> = []

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Import from SSH Config").font(.title2.weight(.semibold))
            Text("Select only the hosts you want Slurmboard to remember.").foregroundStyle(.secondary)
            List(candidates) { host in
                Toggle(isOn: Binding(get: { selected.contains(host.alias) }, set: { checked in
                    if checked { selected.insert(host.alias) } else { selected.remove(host.alias) }
                })) {
                    VStack(alignment: .leading, spacing: 2) {
                        HStack {
                            Text(host.alias).fontWeight(.medium)
                            if existing.contains(host.alias) { Text("Imported").font(.caption).foregroundStyle(.secondary) }
                        }
                        if !host.subtitle.isEmpty { Text(host.subtitle).font(.caption).foregroundStyle(.secondary) }
                    }
                }.toggleStyle(.checkbox).disabled(existing.contains(host.alias))
            }
            HStack {
                Button("Select All") { selected = Set(candidates.filter { !existing.contains($0.alias) }.map(\.alias)) }
                Button("Select None") { selected.removeAll() }
                Spacer()
                Button("Cancel") { dismiss() }
                Button("Import") {
                    onImport(candidates.filter { selected.contains($0.alias) }); dismiss()
                }.buttonStyle(.borderedProminent).disabled(selected.isEmpty)
            }
        }
        .padding(20).frame(width: 680, height: 620)
        .onAppear { candidates = SSHConfigParser.loadHosts() }
    }
}
