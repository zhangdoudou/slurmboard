import SwiftUI

struct HostPickerView: View {
    @EnvironmentObject private var manager: ConnectionManager
    @State private var filter = ""
    @State private var showingAdd = false
    @State private var showingImport = false

    private var filteredHosts: [SSHHost] {
        guard !filter.isEmpty else { return manager.hosts }
        let needle = filter.lowercased()
        return manager.hosts.filter {
            $0.alias.lowercased().contains(needle)
            || ($0.hostName?.lowercased().contains(needle) ?? false)
            || ($0.sshCommand?.lowercased().contains(needle) ?? false)
        }
    }

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 10) {
                Image(systemName: "server.rack").foregroundStyle(.tint)
                Text("Hosts").font(.headline)
                Spacer()
                Button { showingImport = true } label: {
                    Label("Import SSH Config", systemImage: "square.and.arrow.down")
                }
                Button { showingAdd = true } label: { Label("Add Host", systemImage: "plus") }
                    .buttonStyle(.borderedProminent)
            }.padding()

            TextField("Filter hosts…", text: $filter)
                .textFieldStyle(.roundedBorder).padding(.horizontal).padding(.bottom, 8)

            if manager.hosts.isEmpty {
                VStack(spacing: 10) {
                    Spacer()
                    Image(systemName: "server.rack").font(.largeTitle).foregroundStyle(.secondary)
                    Text("No saved hosts").font(.headline)
                    Text("Add an SSH connection or import selected entries from ~/.ssh/config.")
                        .font(.callout).foregroundStyle(.secondary)
                    Spacer()
                }.frame(maxWidth: .infinity)
            } else {
                List {
                    ForEach(filteredHosts) { host in
                        HStack(spacing: 12) {
                            VStack(alignment: .leading, spacing: 2) {
                                Text(host.alias).font(.body.weight(.medium)).foregroundStyle(.primary)
                                if !host.subtitle.isEmpty {
                                    Text(host.subtitle).font(.caption).foregroundStyle(.secondary)
                                } else if let command = host.sshCommand {
                                    Text(command).font(.caption.monospaced()).foregroundStyle(.secondary)
                                }
                            }.frame(maxWidth: .infinity, alignment: .leading)
                            Button { _ = manager.connect(host: host) } label: {
                                Label("SlurmBoard", systemImage: "chart.bar")
                                    .frame(minWidth: 110)
                            }.buttonStyle(.borderedProminent)
                            Button { manager.openTerminal(host: host) } label: {
                                Label("Terminal", systemImage: "terminal")
                                    .frame(minWidth: 100)
                            }.buttonStyle(.bordered)
                        }
                        .padding(.vertical, 5)
                    }.onDelete(perform: deleteFiltered)
                }
            }
        }
        .sheet(isPresented: $showingAdd) { AddHostSheet { manager.addHost($0) } }
        .sheet(isPresented: $showingImport) {
            ImportHostsSheet(existing: Set(manager.hosts.map(\.alias))) { manager.importHosts($0) }
        }
    }

    private func deleteFiltered(at offsets: IndexSet) {
        let aliases = offsets.compactMap { filteredHosts.indices.contains($0) ? filteredHosts[$0].alias : nil }
        let indices = IndexSet(manager.hosts.indices.filter { aliases.contains(manager.hosts[$0].alias) })
        manager.removeHosts(at: indices)
    }
}

private struct AddHostSheet: View {
    @Environment(\.dismiss) private var dismiss
    @State private var name = ""
    @State private var command = "ssh "
    let onSave: (SSHHost) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("Add SSH Host").font(.title2.weight(.semibold))
            TextField("Display name (for example roihu-gpu)", text: $name)
            TextField("SSH command (for example ssh -J jump user@login.example.org)", text: $command)
                .font(.system(.body, design: .monospaced))
            Text("Options such as -p, -i and -J are supported. Slurmboard appends each remote Slurm command.")
                .font(.caption).foregroundStyle(.secondary)
            HStack {
                Spacer(); Button("Cancel") { dismiss() }
                Button("Save") {
                    let label = name.trimmingCharacters(in: .whitespacesAndNewlines)
                    onSave(SSHHost(alias: label, hostName: nil, proxyJump: nil,
                                   sshCommand: command.trimmingCharacters(in: .whitespacesAndNewlines)))
                    dismiss()
                }
                .buttonStyle(.borderedProminent)
                .disabled(name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
                          || command.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            }
        }.padding(24).frame(width: 620)
    }
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
                Spacer(); Button("Cancel") { dismiss() }
                Button("Import") {
                    onImport(candidates.filter { selected.contains($0.alias) }); dismiss()
                }.buttonStyle(.borderedProminent).disabled(selected.isEmpty)
            }
        }
        .padding(20).frame(width: 680, height: 620)
        .onAppear { candidates = SSHConfigParser.loadHosts() }
    }
}
