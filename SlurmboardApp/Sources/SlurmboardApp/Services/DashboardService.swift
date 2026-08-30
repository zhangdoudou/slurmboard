import Foundation
import Combine
import Darwin

/// Owns one SSH process that streams slurmboard.py to the login node and
/// forwards its loopback HTTP port back to the native app.
@MainActor
final class DashboardService: ObservableObject {
    let id = UUID()
    let host: SSHHost
    private let password: String?

    @Published private(set) var state: ConnectionState = .connecting
    @Published private(set) var dashboardURL: URL?

    private var process: Process?
    private var connectTask: Task<Void, Never>?
    private var authDirectory: URL?

    init(host: SSHHost, password: String?) {
        self.host = host
        self.password = password
    }

    func connect() {
        disconnect()
        state = .connecting
        connectTask = Task { await startTunnel() }
    }

    func retry() { connect() }

    func disconnect() {
        connectTask?.cancel()
        connectTask = nil
        dashboardURL = nil
        if let process, process.isRunning { process.terminate() }
        process = nil
        cleanupAuth()
        if state != .connecting { state = .disconnected }
    }

    private func startTunnel() async {
        var diagnosticURL: URL?
        var diagnosticHandle: FileHandle?
        defer {
            try? diagnosticHandle?.close()
            if let diagnosticURL { try? FileManager.default.removeItem(at: diagnosticURL) }
        }
        do {
            let source = try Self.dashboardSource()
            let localPort = Int.random(in: 49152...65535)
            let remotePort = Int.random(in: 49152...65535)
            let remoteCommand = Self.remoteCommand(port: remotePort)

            let process = Process()
            process.executableURL = URL(fileURLWithPath: "/usr/bin/ssh")
            var arguments = [
                "-T",
                "-o", "BatchMode=\(password == nil ? "yes" : "no")",
                "-o", "ExitOnForwardFailure=yes",
                "-o", "ConnectTimeout=15",
                "-o", "ServerAliveInterval=30",
                "-o", "ServerAliveCountMax=3",
                "-L", "127.0.0.1:\(localPort):127.0.0.1:\(remotePort)",
            ]
            if password != nil { arguments += ["-o", "NumberOfPasswordPrompts=1"] }
            process.arguments = arguments + host.connectionArguments + [remoteCommand]
            if let password { process.environment = try prepareAskPass(password: password) }

            let input = Pipe()
            let errorURL = FileManager.default.temporaryDirectory
                .appendingPathComponent("slurmboard-ssh-\(UUID().uuidString).log")
            guard FileManager.default.createFile(atPath: errorURL.path, contents: nil) else {
                throw DashboardError(message: "Could not create a temporary SSH diagnostic log.")
            }
            let errorHandle = try FileHandle(forWritingTo: errorURL)
            diagnosticURL = errorURL
            diagnosticHandle = errorHandle
            process.standardInput = input
            process.standardOutput = FileHandle.nullDevice
            process.standardError = errorHandle
            try process.run()
            self.process = process
            let inputWriter = input.fileHandleForWriting
            // If an unreachable host makes SSH close stdin early, turn EPIPE
            // into a normal write error rather than a process-killing SIGPIPE.
            _ = Darwin.fcntl(inputWriter.fileDescriptor, F_SETNOSIGPIPE, 1)
            try inputWriter.write(contentsOf: source)
            try inputWriter.close()

            let url = URL(string: "http://127.0.0.1:\(localPort)/")!
            try await waitUntilReady(url: url, process: process, diagnosticURL: errorURL)
            guard !Task.isCancelled, process.isRunning else { cleanupAuth(); return }
            cleanupAuth()
            dashboardURL = url
            state = .connected

            process.terminationHandler = { [weak self] process in
                Task { @MainActor [weak self] in
                    guard let self, self.process === process else { return }
                    self.dashboardURL = nil
                    if self.state == .connected {
                        self.state = .failed("SSH connection closed (status \(process.terminationStatus)).")
                    }
                }
            }
        } catch is CancellationError {
            cleanupAuth()
            return
        } catch {
            dashboardURL = nil
            if let process, process.isRunning { process.terminate() }
            process = nil
            try? diagnosticHandle?.synchronize()
            let message = Self.connectionErrorMessage(fallback: error, diagnosticURL: diagnosticURL)
            cleanupAuth()
            if !Task.isCancelled {
                state = .failed(message)
            }
        }
    }

    private func prepareAskPass(password: String) throws -> [String: String] {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent("slurmboard-askpass-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: false)
        let passwordFile = directory.appendingPathComponent("password")
        let helperFile = directory.appendingPathComponent("askpass")
        try Data(password.utf8).write(to: passwordFile, options: .atomic)
        try Data("#!/bin/sh\nexec /bin/cat \"$SLURMBOARD_ASKPASS_FILE\"\n".utf8)
            .write(to: helperFile, options: .atomic)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: passwordFile.path)
        try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: helperFile.path)
        authDirectory = directory
        var environment = ProcessInfo.processInfo.environment
        environment["SSH_ASKPASS"] = helperFile.path
        environment["SSH_ASKPASS_REQUIRE"] = "force"
        environment["DISPLAY"] = environment["DISPLAY"] ?? ":0"
        environment["SLURMBOARD_ASKPASS_FILE"] = passwordFile.path
        return environment
    }

    private func cleanupAuth() {
        guard let authDirectory else { return }
        try? FileManager.default.removeItem(at: authDirectory)
        self.authDirectory = nil
    }

    private func waitUntilReady(url: URL, process: Process, diagnosticURL: URL) async throws {
        let healthURL = url.appendingPathComponent("health")
        let deadline = Date().addingTimeInterval(30)
        while Date() < deadline {
            try Task.checkCancellation()
            if !process.isRunning {
                let message = Self.readDiagnostic(at: diagnosticURL)
                throw DashboardError(message: message?.isEmpty == false
                                     ? message! : "SSH exited before the dashboard became ready.")
            }
            var request = URLRequest(url: healthURL)
            request.timeoutInterval = 0.5
            if let (_, response) = try? await URLSession.shared.data(for: request),
               (response as? HTTPURLResponse)?.statusCode == 200 {
                return
            }
            try await Task.sleep(for: .milliseconds(250))
        }
        process.terminate()
        throw DashboardError(message: "The remote dashboard did not become ready within 30 seconds.")
    }

    private static func readDiagnostic(at url: URL) -> String? {
        guard let data = try? Data(contentsOf: url), !data.isEmpty else { return nil }
        return String(decoding: data, as: UTF8.self)
            .trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private static func connectionErrorMessage(fallback: Error, diagnosticURL: URL?) -> String {
        if let diagnosticURL, let message = readDiagnostic(at: diagnosticURL), !message.isEmpty {
            return message
        }
        return fallback.localizedDescription
    }

    private static func dashboardSource() throws -> Data {
        let manager = FileManager.default
        var candidates: [URL] = []
        if let bundled = Bundle.main.url(forResource: "slurmboard", withExtension: "py") {
            candidates.append(bundled)
        }
        let cwd = URL(fileURLWithPath: manager.currentDirectoryPath, isDirectory: true)
        candidates.append(cwd.appendingPathComponent("SlurmboardApp/slurmboard.py"))
        candidates.append(cwd.appendingPathComponent("slurmboard.py"))
        for candidate in candidates where manager.fileExists(atPath: candidate.path) {
            return try Data(contentsOf: candidate)
        }
        throw DashboardError(message: "The bundled slurmboard.py resource is missing.")
    }

    private static func remoteCommand(port: Int) -> String {
        "exec \"$(command -v python3.13 || command -v python3.12 || command -v python3.11 || "
        + "command -v python3.10 || command -v python3.9 || command -v python3.8 || "
        + "command -v python3.7 || echo python3)\" - --host 127.0.0.1 --port \(port) --log-level warning"
    }
}

private struct DashboardError: LocalizedError {
    let message: String
    var errorDescription: String? { message }
}
