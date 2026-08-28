import Foundation

/// Runs Slurm commands on a login node over the system `ssh`, reusing a single
/// multiplexed connection (ControlMaster) so the many short commands a refresh
/// needs don't each pay a full SSH handshake.
///
/// The first `run(...)` opens the master connection; subsequent ones ride the
/// same socket. `ControlPersist` keeps it warm between refreshes; `close()`
/// tears it down explicitly.
actor SSHRunner {

    struct RemoteError: Error, CustomStringConvertible {
        let command: String
        let status: Int32
        let stdout: String
        let stderr: String
        var description: String {
            let err = stderr.trimmingCharacters(in: .whitespacesAndNewlines)
            let out = stdout.trimmingCharacters(in: .whitespacesAndNewlines)
            let message = !err.isEmpty ? err : (!out.isEmpty ? out : "No error output")
            return "`\(command)` failed (exit \(status)): \(message)"
        }
    }

    let host: String
    private let connectionArguments: [String]
    private let controlPath: String

    init(host: SSHHost) {
        self.host = host.alias
        self.connectionArguments = host.connectionArguments
        // Keep the socket path short — macOS caps AF_UNIX paths at ~104 chars.
        let token = String(UUID().uuidString.prefix(8)).lowercased()
        self.controlPath = "/tmp/slurmboard-\(token).sock"
    }

    private var sharedOptions: [String] {
        [
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=15",
            "-o", "ControlMaster=auto",
            "-o", "ControlPath=\(controlPath)",
            "-o", "ControlPersist=120",
        ]
    }

    /// Run a remote command line (already shell-quoted as needed) and return
    /// stdout. Throws `RemoteError` on non-zero exit.
    func run(_ remoteCommand: String) async throws -> String {
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/usr/bin/ssh")
        proc.arguments = sharedOptions + connectionArguments + [remoteCommand]

        let outPipe = Pipe()
        let errPipe = Pipe()
        proc.standardOutput = outPipe
        proc.standardError = errPipe

        try proc.run()

        // Read both pipes before waiting to avoid deadlock on large output
        // (scontrol show node can be hundreds of KB).
        let outData = outPipe.fileHandleForReading.readDataToEndOfFile()
        let errData = errPipe.fileHandleForReading.readDataToEndOfFile()
        proc.waitUntilExit()

        guard proc.terminationStatus == 0 else {
            throw RemoteError(command: remoteCommand,
                              status: proc.terminationStatus,
                              stdout: String(data: outData, encoding: .utf8) ?? "",
                              stderr: String(data: errData, encoding: .utf8) ?? "")
        }
        return String(data: outData, encoding: .utf8) ?? ""
    }

    /// Whoami on the login node — used as the Slurm user for squeue/sacct.
    func remoteUser() async throws -> String {
        let out = try await run("whoami")
        return out.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    /// Tear down the master connection so no ssh process lingers.
    func close() {
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/usr/bin/ssh")
        proc.arguments = ["-o", "ControlPath=\(controlPath)", "-O", "exit"] + connectionArguments
        proc.standardOutput = FileHandle.nullDevice
        proc.standardError = FileHandle.nullDevice
        try? proc.run()
        proc.waitUntilExit()
    }
}

/// Minimal shell single-quoting for embedding format strings safely in a
/// remote command (e.g. squeue `-o` arguments containing `|` and `%`).
func shellQuote(_ s: String) -> String {
    "'" + s.replacingOccurrences(of: "'", with: "'\\''") + "'"
}
