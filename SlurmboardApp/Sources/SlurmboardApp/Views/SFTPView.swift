import SwiftUI
import Foundation
import AppKit

private final class TarListingThrottle: @unchecked Sendable {
    private let lock = NSLock()
    private var remainder = ""
    private var lastEmission = Date.distantPast
    private let emit: @Sendable (String) -> Void

    init(emit: @escaping @Sendable (String) -> Void) { self.emit = emit }

    func consume(_ data: Data) {
        guard !data.isEmpty else { return }
        var valueToEmit: String?
        lock.lock()
        remainder += String(decoding: data, as: UTF8.self)
        let lines = remainder.components(separatedBy: .newlines)
        remainder = lines.last ?? ""
        if remainder.count > 16_384 { remainder = String(remainder.suffix(16_384)) }
        if let latest = lines.dropLast().last, !latest.isEmpty,
           Date().timeIntervalSince(lastEmission) >= 0.25 {
            lastEmission = Date()
            valueToEmit = latest
        }
        lock.unlock()
        if let valueToEmit { emit(valueToEmit) }
    }
}

struct FileItem: Identifiable, Hashable {
    let name: String
    let isDirectory: Bool
    let size: String
    let modified: String
    var id: String { name }
    var numericSize: Int64 { Int64(size) ?? (isDirectory ? -1 : 0) }
    var kind: String { isDirectory ? "folder" : "file" }
}

@MainActor
final class SFTPBrowser: ObservableObject {
    @Published var items: [FileItem] = []
    @Published var path = "."
    @Published var loading = false
    @Published var message = "Select a host"
    var host: SSHHost?
    private let controlPath = "/tmp/slurmboard-sftp-\(UUID().uuidString.prefix(8).lowercased()).sock"

    func connect(_ host: SSHHost, initialPath: String? = nil) {
        self.host = host
        path = initialPath?.isEmpty == false ? initialPath! : "."
        refresh()
    }
    func go(to newPath: String) { path = newPath.isEmpty ? "." : newPath; refresh() }
    func open(_ item: FileItem) { guard item.isDirectory else { return }; path = joined(path, item.name); refresh() }
    func up() { guard path != "." else { return }; path = (path as NSString).deletingLastPathComponent; if path.isEmpty { path = "." }; refresh() }
    func refresh() {
        guard let host else { return }
        loading = true; message = "Loading…"
        let currentPath = path
        Task.detached {
            let result = Self.batch(host: host, controlPath: self.controlPath,
                                    commands: ["pwd", "ls -la \(Self.quoted(currentPath))"])
            let parsed = Self.parseListing(result.output)
            let absolute = Self.parseWorkingDirectory(result.output)
            await MainActor.run {
                if currentPath == ".", let absolute { self.path = absolute }
                self.items = parsed; self.loading = false
                self.message = result.status == 0 ? "" : result.output
            }
        }
    }
    func download(_ item: FileItem, to local: URL) {
        guard let host else { return }
        message = "Downloading \(item.name)…"
        let remote = joined(path, item.name)
        Task.detached {
            let recursive = item.isDirectory ? "-r " : ""
            let r = Self.batch(host: host, controlPath: self.controlPath, commands: ["get \(recursive)\(Self.quoted(remote)) \(Self.quoted(local.appendingPathComponent(item.name).path))"])
            await MainActor.run { self.message = r.status == 0 ? "Downloaded \(item.name)" : r.output }
        }
    }
    func upload(_ local: URL) {
        guard let host else { return }
        message = "Uploading \(local.lastPathComponent)…"
        let remotePath = path
        Task.detached {
            var isDirectory: ObjCBool = false
            _ = FileManager.default.fileExists(atPath: local.path, isDirectory: &isDirectory)
            let recursive = isDirectory.boolValue ? "-r " : ""
            let r = Self.batch(host: host, controlPath: self.controlPath, commands: ["put \(recursive)\(Self.quoted(local.path)) \(Self.quoted(remotePath))"])
            await MainActor.run { self.message = r.status == 0 ? "Uploaded \(local.lastPathComponent)" : r.output; self.refresh() }
        }
    }
    func rename(_ item: FileItem, to name: String) { run("rename \(Self.quoted(joined(path, item.name))) \(Self.quoted(joined(path, name)))") }
    func delete(_ item: FileItem) { run("\(item.isDirectory ? "rmdir" : "rm") \(Self.quoted(joined(path, item.name)))") }
    func newFolder(_ name: String) { run("mkdir \(Self.quoted(joined(path, name)))") }
    func chmod(_ item: FileItem, mode: String) { run("chmod \(mode) \(Self.quoted(joined(path, item.name)))") }
    func fullPath(_ item: FileItem) -> String { joined(path, item.name) }
    private func run(_ command: String) {
        guard let host else { return }; message = "Working…"; let socket = controlPath
        Task.detached {
            let r = Self.batch(host: host, controlPath: socket, commands: [command])
            await MainActor.run { self.message = r.status == 0 ? "" : r.output; self.refresh() }
        }
    }
    private func joined(_ base: String, _ name: String) -> String { base == "." ? name : (base as NSString).appendingPathComponent(name) }
    nonisolated private static func quoted(_ s: String) -> String { "\"" + s.replacingOccurrences(of: "\"", with: "\\\"") + "\"" }
    nonisolated private static func batch(host: SSHHost, controlPath: String, commands: [String]) -> (status: Int32, output: String) {
        let proc = Process(), input = Pipe(), output = Pipe()
        proc.executableURL = URL(fileURLWithPath: "/usr/bin/sftp")
        var args = host.connectionArguments
        if let i = args.firstIndex(of: "-p") { args[i] = "-P" }
        proc.arguments = ["-q", "-b", "-", "-o", "ControlMaster=auto", "-o", "ControlPersist=120", "-o", "ControlPath=\(controlPath)"] + args
        proc.standardInput = input; proc.standardOutput = output; proc.standardError = output
        do { try proc.run() } catch { return (-1, error.localizedDescription) }
        input.fileHandleForWriting.write((commands.joined(separator: "\n") + "\n").data(using: .utf8)!)
        input.fileHandleForWriting.closeFile()
        let data = output.fileHandleForReading.readDataToEndOfFile(); proc.waitUntilExit()
        return (proc.terminationStatus, String(data: data, encoding: .utf8) ?? "")
    }
    nonisolated private static func parseListing(_ text: String) -> [FileItem] {
        let lines = text.split(separator: "\n")
        return lines.compactMap { raw -> FileItem? in
            let pieces = raw.split(maxSplits: 8, whereSeparator: { $0.isWhitespace })
            let fields = pieces.map { String($0) }
            guard fields.count >= 9, fields[0].first == "d" || fields[0].first == "-" else { return nil }
            let name = (fields[8] as NSString).lastPathComponent
            guard name != "." && name != ".." && !name.isEmpty else { return nil }
            return FileItem(name: name, isDirectory: fields[0].first == "d", size: fields[4],
                            modified: fields[5...7].joined(separator: " "))
        }.sorted { $0.isDirectory != $1.isDirectory ? $0.isDirectory : $0.name.localizedCaseInsensitiveCompare($1.name) == .orderedAscending }
    }
    nonisolated private static func parseWorkingDirectory(_ text: String) -> String? {
        for line in text.components(separatedBy: .newlines) {
            let prefix = "Remote working directory: "
            if line.hasPrefix(prefix) { return String(line.dropFirst(prefix.count)) }
        }
        return nil
    }
}

@MainActor
final class SFTPTransferController: ObservableObject, Identifiable {
    let id = UUID()
    private struct ManifestEntry {
        let path: String
        let type: Character
        let size: Int64
        let modified: Double
    }

    private struct TransferPlan {
        let paths: [String]
        let bytes: Int64
        let skipped: Int
    }
    @Published var isActive = false
    @Published var progress = 0.0
    @Published var label = ""
    @Published var byteLabel = ""
    @Published var currentItem = ""
    @Published var speedLabel = "0 B/s"
    @Published var etaLabel = "ETA —"
    private var sourceProcess: Process?
    private var targetProcess: Process?
    private var cancelled = false
    private var transferStartedAt = Date()
    private(set) var isRunning = false
    var finishHandler: ((SFTPTransferController) -> Void)?

    func markQueued(item: FileItem) {
        isActive = true
        isRunning = false
        label = "Queued · \(item.name)"
        byteLabel = "Waiting for a transfer slot"
        currentItem = item.isDirectory ? "Folder: \(item.name)" : "File: \(item.name)"
        speedLabel = "—"
        etaLabel = "ETA —"
        progress = 0
    }

    func begin(item: FileItem, source: SFTPBrowser, target: SFTPBrowser) {
        guard let sourceHost = source.host, let targetHost = target.host else { finish(); return }
        isActive = true
        isRunning = true
        cancelled = false
        progress = 0
        label = "Preparing \(item.name)…"
        byteLabel = "Comparing files"
        currentItem = item.isDirectory ? "Folder: \(item.name)" : "File: \(item.name)"
        speedLabel = "0 B/s"
        etaLabel = "ETA —"
        transferStartedAt = Date()
        let sourceDirectory = source.path, targetDirectory = target.path

        Task.detached { [self, target] in
            let plan = Self.makePlan(sourceHost: sourceHost, sourceDirectory: sourceDirectory,
                                     itemName: item.name, targetHost: targetHost,
                                     targetDirectory: targetDirectory, status: { title, detail, progress in
                Task { @MainActor in
                    self.label = title
                    self.currentItem = detail
                    self.progress = progress
                }
            })
            let shouldCancel = await MainActor.run { self.cancelled }
            if shouldCancel {
                await MainActor.run { self.label = "Preparation cancelled"; self.isActive = false }
                await MainActor.run { self.finish() }
                return
            }
            await MainActor.run {
                self.label = "Copying \(item.name) · \(plan.paths.count) changed, \(plan.skipped) skipped"
                self.byteLabel = plan.bytes > 0 ? "0 B / \(Self.formatted(plan.bytes))" : "No changed data"
                self.transferStartedAt = Date()
            }
            guard !plan.paths.isEmpty else {
                await MainActor.run {
                    self.progress = 1
                    self.label = "Already up to date · \(plan.skipped) skipped"
                    self.isActive = false
                    self.finish()
                }
                return
            }
            let result = Self.transfer(sourceHost: sourceHost, sourceDirectory: sourceDirectory,
                                       paths: plan.paths, targetHost: targetHost,
                                       targetDirectory: targetDirectory,
                                       register: { sourceProcess, targetProcess in
                Task { @MainActor in self.register(source: sourceProcess, target: targetProcess) }
            }, advanced: { bytes in
                Task { @MainActor in self.update(bytes: bytes, total: plan.bytes) }
            }, currentItem: { path in
                Task { @MainActor in self.currentItem = path }
            })
            await MainActor.run {
                let wasCancelled = self.cancelled
                self.sourceProcess = nil
                self.targetProcess = nil
                self.label = wasCancelled ? "Transfer cancelled" : (result == 0 ? "Copy complete" : "Copy failed")
                if result == 0 { self.progress = 1; target.refresh() }
                self.isActive = false
                self.finish()
            }
        }
    }

    func cancel() {
        guard isActive else { return }
        cancelled = true
        label = "Cancelling…"
        if !isRunning {
            isActive = false
            finish()
            return
        }
        sourceProcess?.terminate()
        targetProcess?.terminate()
    }

    private func finish() {
        isRunning = false
        finishHandler?(self)
    }

    private func register(source: Process, target: Process) {
        sourceProcess = source
        targetProcess = target
        if cancelled { source.terminate(); target.terminate() }
    }

    private func update(bytes: Int64, total: Int64) {
        byteLabel = total > 0 ? "\(Self.formatted(bytes)) / \(Self.formatted(total))" : Self.formatted(bytes)
        let elapsed = max(0.1, Date().timeIntervalSince(transferStartedAt))
        let bytesPerSecond = Double(bytes) / elapsed
        speedLabel = "\(Self.formatted(Int64(bytesPerSecond)))/s"
        if total > bytes, bytesPerSecond > 0 {
            etaLabel = "ETA \(Self.formattedDuration(Double(total - bytes) / bytesPerSecond))"
        } else {
            etaLabel = "ETA <1s"
        }
        if total > 0 { progress = min(0.99, Double(bytes) / Double(total)) }
    }

    nonisolated private static func makePlan(sourceHost: SSHHost, sourceDirectory: String, itemName: String,
                                             targetHost: SSHHost, targetDirectory: String,
                                             status: @escaping @Sendable (String, String, Double) -> Void) -> TransferPlan {
        let sourceRoot = (sourceDirectory as NSString).appendingPathComponent(itemName)
        let targetRoot = (targetDirectory as NSString).appendingPathComponent(itemName)
        status("Scanning source…", sourceRoot, 0.10)
        let source = manifest(host: sourceHost, root: sourceRoot)
        status("Scanning target…", targetRoot, 0.40)
        let target = manifest(host: targetHost, root: targetRoot)
        status("Comparing files…", "\(source.count) source items · \(target.count) target items", 0.70)
        var selected: [ManifestEntry] = []
        var skipped = 0
        for entry in source.values {
            let existing = target[entry.path]
            let changed: Bool
            if let existing {
                changed = existing.type != entry.type || existing.size != entry.size
                    || abs(existing.modified - entry.modified) >= 1.0
            } else {
                changed = true
            }
            if changed { selected.append(entry) } else { skipped += 1 }
        }
        status("Building transfer list…", "Sorting \(selected.count) changed items alphabetically", 0.90)
        selected.sort {
            let comparison = $0.path.localizedCaseInsensitiveCompare($1.path)
            return comparison == .orderedSame ? $0.path < $1.path : comparison == .orderedAscending
        }
        let paths = selected.map { entry in
            entry.path == "." ? "./\(itemName)" : "./\(itemName)/\(entry.path)"
        }
        let bytes = selected.filter { $0.type == "f" }.reduce(Int64(0)) { $0 + $1.size }
        return TransferPlan(paths: paths, bytes: bytes, skipped: skipped)
    }

    nonisolated private static func manifest(host: SSHHost, root: String) -> [String: ManifestEntry] {
        let process = Process(), output = Pipe()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/ssh")
        let command = "LC_ALL=C find \(shellQuoted(root)) -printf '%y\\t%s\\t%T@\\t%P\\0'"
        process.arguments = ["-o", "BatchMode=yes"] + host.connectionArguments + [command]
        process.standardOutput = output
        process.standardError = FileHandle.nullDevice
        do { try process.run() } catch { return [:] }
        let data = output.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        var result: [String: ManifestEntry] = [:]
        for record in String(decoding: data, as: UTF8.self).split(separator: "\0", omittingEmptySubsequences: true) {
            let fields = record.split(separator: "\t", maxSplits: 3, omittingEmptySubsequences: false)
            guard fields.count == 4, let type = fields[0].first,
                  let size = Int64(fields[1]), let modified = Double(fields[2]) else { continue }
            let path = fields[3].isEmpty ? "." : String(fields[3])
            result[path] = ManifestEntry(path: path, type: type, size: size, modified: modified)
        }
        return result
    }

    nonisolated private static func transfer(sourceHost: SSHHost, sourceDirectory: String, paths: [String],
                                             targetHost: SSHHost, targetDirectory: String,
                                             register: @escaping @Sendable (Process, Process) -> Void,
                                             advanced: @escaping @Sendable (Int64) -> Void,
                                             currentItem: @escaping @Sendable (String) -> Void) -> Int32 {
        let sourceOutput = Pipe(), sourceListing = Pipe(), sourceList = Pipe(), targetInput = Pipe()
        let source = Process(), target = Process()
        source.executableURL = URL(fileURLWithPath: "/usr/bin/ssh")
        target.executableURL = URL(fileURLWithPath: "/usr/bin/ssh")
        // Receive the complete selection list before tar starts writing archive
        // bytes. Feeding `tar -T -` directly can deadlock on large manifests:
        // the app blocks writing stdin while tar blocks writing stdout.
        let sourceCommand = "list=$(mktemp) || exit 1; "
            + "trap 'rm -f \"$list\"' EXIT; "
            + "cat > \"$list\"; "
            + "tar -C \(shellQuoted(sourceDirectory)) --no-recursion --null -T \"$list\" -cvf -"
        source.arguments = ["-o", "BatchMode=yes", "-o", "ServerAliveInterval=30"] + sourceHost.connectionArguments
            + [sourceCommand]
        target.arguments = ["-o", "BatchMode=yes", "-o", "ServerAliveInterval=30"] + targetHost.connectionArguments
            + ["tar -C \(shellQuoted(targetDirectory)) -xf -"]
        source.standardOutput = sourceOutput
        source.standardError = sourceListing
        source.standardInput = sourceList
        target.standardInput = targetInput
        target.standardOutput = FileHandle.nullDevice
        target.standardError = FileHandle.nullDevice
        do {
            let listingThrottle = TarListingThrottle(emit: currentItem)
            sourceListing.fileHandleForReading.readabilityHandler = { handle in
                let data = handle.availableData
                guard !data.isEmpty else { handle.readabilityHandler = nil; return }
                listingThrottle.consume(data)
            }
            try target.run()
            try source.run()
            register(source, target)
            var listData = Data(paths.joined(separator: "\0").utf8)
            listData.append(0)
            try sourceList.fileHandleForWriting.write(contentsOf: listData)
            try sourceList.fileHandleForWriting.close()
            var transferred: Int64 = 0
            var keepReading = true
            var lastProgressUpdate = Date.distantPast
            while keepReading {
                autoreleasepool {
                    let data = sourceOutput.fileHandleForReading.availableData
                    guard !data.isEmpty else { keepReading = false; return }
                    do {
                        try targetInput.fileHandleForWriting.write(contentsOf: data)
                        transferred += Int64(data.count)
                        if Date().timeIntervalSince(lastProgressUpdate) >= 0.25 {
                            lastProgressUpdate = Date()
                            advanced(transferred)
                        }
                    } catch {
                        keepReading = false
                    }
                }
            }
            advanced(transferred)
            try? targetInput.fileHandleForWriting.close()
            source.waitUntilExit()
            target.waitUntilExit()
            sourceListing.fileHandleForReading.readabilityHandler = nil
            return source.terminationStatus == 0 ? target.terminationStatus : source.terminationStatus
        } catch {
            if source.isRunning { source.terminate() }
            if target.isRunning { target.terminate() }
            return -1
        }
    }

    nonisolated private static func shellQuoted(_ value: String) -> String {
        "'" + value.replacingOccurrences(of: "'", with: "'\\''") + "'"
    }

    nonisolated private static func formatted(_ bytes: Int64) -> String {
        ByteCountFormatter.string(fromByteCount: bytes, countStyle: .file)
    }

    nonisolated private static func formattedDuration(_ seconds: Double) -> String {
        let value = max(0, Int(seconds.rounded()))
        let hours = value / 3600
        let minutes = (value % 3600) / 60
        let remainder = value % 60
        if hours > 0 { return "\(hours)h \(minutes)m" }
        if minutes > 0 { return "\(minutes)m \(remainder)s" }
        return "\(remainder)s"
    }
}

@MainActor
final class SFTPTransferQueue: ObservableObject {
    private struct Pending {
        let controller: SFTPTransferController
        let item: FileItem
        let source: SFTPBrowser
        let target: SFTPBrowser
    }

    @Published private(set) var transfers: [SFTPTransferController] = []
    private var pending: [Pending] = []
    private var runningIDs: Set<UUID> = []
    let maximumConcurrentTransfers = 5

    var isActive: Bool { !transfers.isEmpty }

    func start(item: FileItem, source: SFTPBrowser, target: SFTPBrowser) {
        guard source.host != nil, target.host != nil else { return }
        let controller = SFTPTransferController()
        controller.markQueued(item: item)
        controller.finishHandler = { [weak self] controller in self?.finished(controller) }
        transfers.append(controller)
        pending.append(Pending(controller: controller, item: item, source: source, target: target))
        pump()
    }

    func cancelAll() {
        for controller in transfers { controller.cancel() }
    }

    private func pump() {
        while runningIDs.count < maximumConcurrentTransfers, !pending.isEmpty {
            let request = pending.removeFirst()
            guard request.controller.isActive else { continue }
            runningIDs.insert(request.controller.id)
            request.controller.begin(item: request.item, source: request.source, target: request.target)
        }
    }

    private func finished(_ controller: SFTPTransferController) {
        runningIDs.remove(controller.id)
        pending.removeAll { $0.controller.id == controller.id }
        transfers.removeAll { $0.id == controller.id }
        pump()
    }
}

struct SFTPView: View {
    private let unselectedSource = "__slurmboard_unselected__"
    private enum Side { case local, leftRemote, rightRemote }
    private enum PromptKind { case rename, newFolder, permissions }
    private struct Prompt: Identifiable { let id = UUID(); let side: Side; let kind: PromptKind; let item: FileItem? }
    private struct DeleteRequest: Identifiable { let id = UUID(); let side: Side; let item: FileItem }
    @EnvironmentObject private var manager: ConnectionManager
    @StateObject private var remote = SFTPBrowser()
    @StateObject private var leftRemote = SFTPBrowser()
    @State private var localPath = FileManager.default.homeDirectoryForCurrentUser
    @State private var localItems: [FileItem] = []
    @State private var selectedLocal: Set<FileItem.ID> = []
    @State private var selectedRemote: Set<FileItem.ID> = []
    @State private var selectedHost: String? = "__slurmboard_unselected__"
    @State private var leftHost: String?
    @State private var selectedLeftRemote: Set<FileItem.ID> = []
    @State private var localSortOrder = [KeyPathComparator(\FileItem.name)]
    @State private var leftRemoteSortOrder = [KeyPathComparator(\FileItem.name)]
    @State private var rightRemoteSortOrder = [KeyPathComparator(\FileItem.name)]
    @State private var localPathText = FileManager.default.homeDirectoryForCurrentUser.path
    @State private var prompt: Prompt?
    @State private var promptValue = ""
    @State private var deleteRequest: DeleteRequest?
    private let defaultsKey = "sftpDefaultPaths"

    var body: some View {
        VStack(spacing: 0) {
            GeometryReader { geometry in
                let paneWidth = geometry.size.width / 2
                HStack(spacing: 0) {
                Group {
                    if leftHost == nil {
                        filePane(source: $leftHost, path: $localPathText, items: localItems, selection: $selectedLocal,
                                 sortOrder: $localSortOrder,
                                 side: .local, up: localUp, go: localGo, open: localOpen,
                                 setDefault: { saveDefaultPath(localPathText, source: nil) })
                    } else {
                        filePane(source: $leftHost, path: $leftRemote.path, items: leftRemote.items, selection: $selectedLeftRemote,
                                 sortOrder: $leftRemoteSortOrder,
                                 side: .leftRemote, up: leftRemote.up, go: leftRemote.go, open: leftRemote.open,
                                 setDefault: { saveDefaultPath(leftRemote.path, source: leftHost) })
                    }
                }
                .frame(width: paneWidth, height: geometry.size.height)
                .overlay(alignment: .trailing) { Divider() }

                Group {
                    if selectedHost == unselectedSource {
                        emptyFilePane(source: $selectedHost)
                    } else if selectedHost == nil {
                        filePane(source: $selectedHost, path: $localPathText, items: localItems, selection: $selectedLocal,
                                 sortOrder: $localSortOrder,
                                 side: .local, up: localUp, go: localGo, open: localOpen,
                                 setDefault: { saveDefaultPath(localPathText, source: nil) })
                    } else {
                        filePane(source: $selectedHost, path: $remote.path, items: remote.items,
                                 selection: $selectedRemote, sortOrder: $rightRemoteSortOrder,
                                 side: .rightRemote, up: remote.up,
                                 go: remote.go, open: remote.open,
                                 setDefault: { saveDefaultPath(remote.path, source: selectedHost) })
                    }
                }
                    .frame(width: paneWidth, height: geometry.size.height)
                }
            }
            SFTPTransferList(queue: manager.sftpTransfers)
        }
        .onChange(of: leftHost) { _, alias in sourceChanged(alias, browser: leftRemote) }
        .onChange(of: selectedHost) { _, alias in sourceChanged(alias, browser: remote) }
        .onAppear {
            let initial = defaultPath(for: nil) ?? FileManager.default.homeDirectoryForCurrentUser.path
            localGo(initial)
        }
        .alert(promptTitle, isPresented: Binding(get: { prompt != nil }, set: { if !$0 { prompt = nil } })) {
            TextField(promptPlaceholder, text: $promptValue)
            Button("Cancel", role: .cancel) { prompt = nil }
            Button("OK") { performPrompt() }
        }
        .alert("Delete item?", isPresented: Binding(get: { deleteRequest != nil }, set: { if !$0 { deleteRequest = nil } })) {
            Button("Cancel", role: .cancel) { deleteRequest = nil }
            Button("Delete", role: .destructive) { performDelete() }
        } message: { Text(deleteRequest?.item.name ?? "") }
    }

    private func filePane(source: Binding<String?>, path: Binding<String>, items: [FileItem], selection: Binding<Set<FileItem.ID>>,
                          sortOrder: Binding<[KeyPathComparator<FileItem>]>,
                          side: Side, up: @escaping () -> Void, go: @escaping (String) -> Void,
                          open: @escaping (FileItem) -> Void, setDefault: @escaping () -> Void) -> some View {
        VStack(spacing: 0) {
            HStack {
                Picker("", selection: source) {
                    Text("Local").tag(String?.none)
                    ForEach(manager.hosts) { Text($0.alias).tag(String?.some($0.alias)) }
                }.labelsHidden().frame(width: 160)
                Button(action: up) { Image(systemName: "chevron.left") }
                TextField("Path", text: path).textFieldStyle(.roundedBorder).onSubmit { go(path.wrappedValue) }
                Button("Set Default", action: setDefault)
                    .help("Use this directory when opening this host")
                Button { refresh(side) } label: { Image(systemName: "arrow.clockwise") }
            }.padding(10)
            Table(items.sorted(using: sortOrder.wrappedValue), selection: selection, sortOrder: sortOrder) {
                TableColumn("Name", value: \FileItem.name) { item in
                    HStack { Image(systemName: item.isDirectory ? "folder.fill" : "doc").foregroundStyle(item.isDirectory ? .blue : .secondary); Text(item.name) }
                        .contentShape(Rectangle()).onTapGesture(count: 2) { open(item) }
                }
                TableColumn("Date Modified", value: \.modified)
                TableColumn("Size", value: \FileItem.numericSize) { Text($0.size) }.width(80)
                TableColumn("Kind", value: \FileItem.kind).width(80)
            }
            .contextMenu(forSelectionType: FileItem.ID.self) { ids in
                if let id = ids.first, let item = items.first(where: { $0.id == id }) {
                    itemMenu(item, side: side)
                }
            }
        }
    }

    private func emptyFilePane(source: Binding<String?>) -> some View {
        VStack(spacing: 0) {
            HStack {
                Picker("", selection: source) {
                    Text("Select Host…").tag(String?.some(unselectedSource))
                    Text("Local").tag(String?.none)
                    ForEach(manager.hosts) { Text($0.alias).tag(String?.some($0.alias)) }
                }
                .labelsHidden()
                .frame(width: 160)
                Spacer()
            }
            .padding(10)
            Divider()
            VStack(spacing: 10) {
                Image(systemName: "externaldrive.connected.to.line.below")
                    .font(.largeTitle).foregroundStyle(.secondary)
                Text("Select a host").font(.headline)
                Text("Choose Local or a saved SSH host to browse files.")
                    .font(.callout).foregroundStyle(.secondary)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
    }

    @ViewBuilder private func itemMenu(_ item: FileItem, side: Side) -> some View {
        Button("Copy to target directory") { side == .local ? copyLocal(item) : copyRemote(item, side: side) }
        Button("Rename") { beginPrompt(.rename, side: side, item: item, initial: item.name) }
        Button("Delete", role: .destructive) { deleteRequest = DeleteRequest(side: side, item: item) }
        Divider()
        Button("Refresh") { refresh(side) }
        Button("New Folder") { beginPrompt(.newFolder, side: side, item: nil, initial: "New Folder") }
        Button("Edit Permissions") { beginPrompt(.permissions, side: side, item: item, initial: "755") }
        Divider()
        Button("Copy Path") { copyPath(item, side: side) }
    }
    private func browser(for side: Side) -> SFTPBrowser? {
        switch side { case .local: return nil; case .leftRemote: return leftRemote; case .rightRemote: return remote }
    }
    private func sourceChanged(_ alias: String?, browser: SFTPBrowser) {
        if alias == unselectedSource { return }
        guard let alias else {
            localGo(defaultPath(for: nil) ?? FileManager.default.homeDirectoryForCurrentUser.path)
            return
        }
        guard let host = manager.hosts.first(where: { $0.alias == alias }) else { return }
        browser.connect(host, initialPath: defaultPath(for: alias))
    }
    private func defaultPath(for source: String?) -> String? {
        guard let data = UserDefaults.standard.data(forKey: defaultsKey),
              let values = try? JSONDecoder().decode([String: String].self, from: data) else { return nil }
        return values[source ?? "__local__"]
    }
    private func saveDefaultPath(_ path: String, source: String?) {
        guard !path.isEmpty else { return }
        var values: [String: String] = [:]
        if let data = UserDefaults.standard.data(forKey: defaultsKey),
           let saved = try? JSONDecoder().decode([String: String].self, from: data) { values = saved }
        values[source ?? "__local__"] = path
        if let data = try? JSONEncoder().encode(values) {
            UserDefaults.standard.set(data, forKey: defaultsKey)
        }
    }
    private func refresh(_ side: Side) { if side == .local { loadLocal() } else { browser(for: side)?.refresh() } }
    private func loadLocal() {
        let keys: Set<URLResourceKey> = [.isDirectoryKey, .fileSizeKey, .contentModificationDateKey]
        let urls = (try? FileManager.default.contentsOfDirectory(at: localPath, includingPropertiesForKeys: Array(keys), options: [])) ?? []
        localItems = urls.compactMap { url in let v = try? url.resourceValues(forKeys: keys); return FileItem(name: url.lastPathComponent, isDirectory: v?.isDirectory ?? false, size: v?.isDirectory == true ? "—" : "\(v?.fileSize ?? 0)", modified: v?.contentModificationDate?.formatted(date: .abbreviated, time: .shortened) ?? "—") }.sorted { $0.isDirectory != $1.isDirectory ? $0.isDirectory : $0.name < $1.name }
    }
    private func localOpen(_ item: FileItem) { guard item.isDirectory else { return }; localPath.appendPathComponent(item.name); localPathText = localPath.path; selectedLocal.removeAll(); loadLocal() }
    private func localUp() { localPath.deleteLastPathComponent(); localPathText = localPath.path; selectedLocal.removeAll(); loadLocal() }
    private func localGo(_ path: String) { let url = URL(fileURLWithPath: path).standardizedFileURL; var isDir: ObjCBool = false; if FileManager.default.fileExists(atPath: url.path, isDirectory: &isDir), isDir.boolValue { localPath = url; localPathText = url.path; loadLocal() } }
    private func upload() { guard let id = selectedLocal.first, let item = localItems.first(where: { $0.id == id }) else { return }; remote.upload(localPath.appendingPathComponent(item.name)) }
    private func download() { guard let id = selectedRemote.first, let item = remote.items.first(where: { $0.id == id }) else { return }; remote.download(item, to: localPath); loadLocal() }
    private func copyLocal(_ item: FileItem) {
        let target = selectedHost != nil && selectedHost != unselectedSource ? remote : (leftHost != nil ? leftRemote : nil)
        target?.upload(localPath.appendingPathComponent(item.name))
    }
    private func copyRemote(_ item: FileItem, side: Side) {
        guard let source = browser(for: side) else { return }
        let target: SFTPBrowser?
        switch side {
        case .leftRemote: target = selectedHost == nil || selectedHost == unselectedSource ? nil : remote
        case .rightRemote: target = leftHost == nil ? nil : leftRemote
        case .local: target = nil
        }
        if let target {
            manager.sftpTransfers.start(item: item, source: source, target: target)
        } else {
            source.download(item, to: localPath)
        }
    }
    private func beginPrompt(_ kind: PromptKind, side: Side, item: FileItem?, initial: String) { promptValue = initial; prompt = Prompt(side: side, kind: kind, item: item) }
    private var promptTitle: String { guard let prompt else { return "" }; switch prompt.kind { case .rename: return "Rename"; case .newFolder: return "New Folder"; case .permissions: return "Permissions (octal)" } }
    private var promptPlaceholder: String { promptTitle }
    private func performPrompt() {
        guard let p = prompt else { return }; defer { prompt = nil }
        switch (p.side, p.kind) {
        case (.local, .rename):
            if let item = p.item { try? FileManager.default.moveItem(at: localPath.appendingPathComponent(item.name), to: localPath.appendingPathComponent(promptValue)); loadLocal() }
        case (.leftRemote, .rename), (.rightRemote, .rename): if let item = p.item { browser(for: p.side)?.rename(item, to: promptValue) }
        case (.local, .newFolder): try? FileManager.default.createDirectory(at: localPath.appendingPathComponent(promptValue), withIntermediateDirectories: false); loadLocal()
        case (.leftRemote, .newFolder), (.rightRemote, .newFolder): browser(for: p.side)?.newFolder(promptValue)
        case (.local, .permissions):
            if let item = p.item, let mode = Int(promptValue, radix: 8) { try? FileManager.default.setAttributes([.posixPermissions: mode], ofItemAtPath: localPath.appendingPathComponent(item.name).path); loadLocal() }
        case (.leftRemote, .permissions), (.rightRemote, .permissions): if let item = p.item { browser(for: p.side)?.chmod(item, mode: promptValue) }
        }
    }
    private func performDelete() {
        guard let request = deleteRequest else { return }; defer { deleteRequest = nil }
        if request.side != .local { browser(for: request.side)?.delete(request.item) }
        else { try? FileManager.default.trashItem(at: localPath.appendingPathComponent(request.item.name), resultingItemURL: nil); loadLocal() }
    }
    private func copyPath(_ item: FileItem, side: Side) {
        let value = side == .local ? localPath.appendingPathComponent(item.name).path : (browser(for: side)?.fullPath(item) ?? item.name)
        NSPasteboard.general.clearContents(); NSPasteboard.general.setString(value, forType: .string)
    }
}

private struct SFTPTransferList: View {
    @ObservedObject var queue: SFTPTransferQueue

    var body: some View {
        if !queue.transfers.isEmpty {
            ScrollView {
                LazyVStack(spacing: 0) {
                    ForEach(queue.transfers) { SFTPTransferBar(transfer: $0) }
                }
            }
            .frame(maxHeight: 250)
        }
    }
}

private struct SFTPTransferBar: View {
    @ObservedObject var transfer: SFTPTransferController

    var body: some View {
        if transfer.isActive {
            Divider()
            HStack(spacing: 12) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(transfer.label).lineLimit(1)
                    Text(transfer.currentItem)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                        .truncationMode(.middle)
                }
                .frame(minWidth: 220, maxWidth: 420, alignment: .leading)
                ProgressView(value: transfer.progress).progressViewStyle(.linear)
                Text(transfer.byteLabel)
                    .font(.caption.monospacedDigit()).foregroundStyle(.secondary).fixedSize()
                Text(transfer.speedLabel)
                    .font(.caption.monospacedDigit())
                    .frame(minWidth: 90, alignment: .trailing)
                Text(transfer.etaLabel)
                    .font(.caption.monospacedDigit())
                    .foregroundStyle(.secondary)
                    .frame(minWidth: 82, alignment: .trailing)
                Button("Cancel", role: .cancel) { transfer.cancel() }
            }
            .padding(.horizontal, 12)
            .frame(height: 50)
        }
    }
}
