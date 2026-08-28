import Foundation
import Combine

enum ConnectionState: Equatable {
    case connecting
    case connected
    case failed(String)
    case disconnected

    var label: String {
        switch self {
        case .connecting:      return "Connecting…"
        case .connected:       return "Connected"
        case .failed(let m):   return "Failed: \(m)"
        case .disconnected:    return "Disconnected"
        }
    }
}

/// Drives one cluster: runs Slurm commands over SSH (via `SSHRunner`), parses
/// the output (`SlurmParser`), and publishes typed snapshots to the SwiftUI
/// dashboard. This is the native replacement for the remote HTTP server —
/// nothing runs on the login node except the Slurm CLIs themselves.
@MainActor
final class SlurmService: ObservableObject {

    let id = UUID()
    let host: SSHHost
    private let runner: SSHRunner

    @Published private(set) var state: ConnectionState = .connecting
    @Published private(set) var snapshot = ClusterSnapshot()

    // Fine-grained in-flight flags so individual refresh spinners work like the
    // web UI's per-section ↻ buttons.
    @Published private(set) var refreshingCluster = false
    @Published private(set) var refreshingActive = false
    @Published private(set) var refreshingHistory = false
    @Published private(set) var refreshingPartitions: Set<String> = []

    init(host: SSHHost) {
        self.host = host
        self.runner = SSHRunner(host: host)
    }

    // MARK: Slurm command strings (mirror slurmboard.py)

    private enum Cmd {
        static let nodes   = "scontrol -o show node"
        static let limits  = "sinfo -h -o " + shellQuote("%P|%l")
        static let queue   = "squeue -h -o " + shellQuote("%P|%i|%u|%j|%T|%M|%C|%b|%R|%l|%V")
        static func active(_ user: String) -> String {
            "squeue -u \(shellQuote(user)) -h -o " + shellQuote("%i|%j|%T|%P|%M|%V|%C|%b")
        }
        static func history(_ user: String, days: Int) -> String {
            let start = Self.utcStart(daysAgo: days)
            return "sacct -u \(shellQuote(user)) --starttime \(start) --noheader --parsable2 "
                 + "--format=JobID,JobName,State,Submit,Elapsed,AllocCPUS,AllocTRES,Partition"
        }
        static func jobDetail(_ jobID: String) -> String {
            "scontrol show job \(shellQuote(jobID))"
        }
        static func accountingDetail(_ jobID: String) -> String {
            "sacct -X -j \(shellQuote(jobID)) --noheader --parsable2 "
              + "--format=JobIDRaw,JobName,User,Account,QOS,State,Partition,AllocNodes,AllocCPUS,Elapsed,Timelimit,Submit,Start,End,NodeList,ExitCode,AllocTRES"
        }
        private static func utcStart(daysAgo: Int) -> String {
            let df = DateFormatter()
            df.locale = Locale(identifier: "en_US_POSIX")
            df.timeZone = TimeZone(identifier: "UTC")
            df.dateFormat = "yyyy-MM-dd'T'HH:mm:ss"
            return df.string(from: Date().addingTimeInterval(-Double(daysAgo) * 86400))
        }
    }

    private static func nowStamp() -> String {
        let df = DateFormatter()
        df.locale = Locale(identifier: "en_US_POSIX")
        df.dateFormat = "yyyy-MM-dd HH:mm:ss"
        return df.string(from: Date())
    }

    // MARK: - Lifecycle

    func connect() {
        state = .connecting
        Task { await self.loadEverything(initial: true) }
    }

    private func loadEverything(initial: Bool) async {
        do {
            let user = try await runner.remoteUser()
            let (nodes, queue, limits) = try await (
                runner.run(Cmd.nodes),
                runner.run(Cmd.queue),
                runner.run(Cmd.limits)
            )
            var snap = SlurmParser.buildClusterSnapshot(
                nodesText: nodes, jobCountsText: queue, limitsText: limits,
                generatedAt: Self.nowStamp())
            snap.currentUser = user
            snap.activeQueue = SlurmParser.parseActiveQueue(try await runner.run(Cmd.active(user)))
            self.snapshot = snap
            self.state = .connected
            // History is slower (sacct); load it after the board is up.
            await refreshHistory()
        } catch {
            self.state = .failed(describe(error))
        }
    }

    // MARK: - Refreshers

    /// Full cluster refresh (all partitions, summary, GPU table).
    func refreshCluster() {
        guard !refreshingCluster else { return }
        refreshingCluster = true
        Task {
            defer { self.refreshingCluster = false }
            do {
                let user = self.snapshot.currentUser
                let (nodes, queue, limits) = try await (
                    runner.run(Cmd.nodes), runner.run(Cmd.queue), runner.run(Cmd.limits))
                var snap = SlurmParser.buildClusterSnapshot(
                    nodesText: nodes, jobCountsText: queue, limitsText: limits,
                    generatedAt: Self.nowStamp())
                snap.currentUser = user
                snap.activeQueue = self.snapshot.activeQueue
                snap.userJobs = self.snapshot.userJobs
                self.snapshot = snap
                self.state = .connected
            } catch {
                self.state = .failed(describe(error))
            }
        }
    }

    /// Refresh a single partition's numbers (and its nodes) in place, without
    /// disturbing the rest — mirrors the web UI's per-row ↻.
    func refreshPartition(_ name: String) {
        guard !refreshingPartitions.contains(name) else { return }
        refreshingPartitions.insert(name)
        Task {
            defer { self.refreshingPartitions.remove(name) }
            do {
                let (nodes, queue, limits) = try await (
                    runner.run(Cmd.nodes), runner.run(Cmd.queue), runner.run(Cmd.limits))
                let fresh = SlurmParser.buildClusterSnapshot(
                    nodesText: nodes, jobCountsText: queue, limitsText: limits,
                    generatedAt: Self.nowStamp())
                if let newPart = fresh.partitions.first(where: { $0.name == name }),
                   let idx = self.snapshot.partitions.firstIndex(where: { $0.name == name }) {
                    self.snapshot.partitions[idx] = newPart
                }
                // Update this partition's nodes too.
                let freshNodes = fresh.nodes.filter { $0.partitions.contains(name) }
                for fn in freshNodes {
                    if let i = self.snapshot.nodes.firstIndex(where: { $0.name == fn.name }) {
                        self.snapshot.nodes[i] = fn
                    }
                }
            } catch { /* keep old data; per-row refresh failures are non-fatal */ }
        }
    }

    func refreshActiveQueue() {
        guard !refreshingActive else { return }
        refreshingActive = true
        Task {
            defer { self.refreshingActive = false }
            let user = self.snapshot.currentUser
            if let out = try? await runner.run(Cmd.active(user)) {
                self.snapshot.activeQueue = SlurmParser.parseActiveQueue(out)
            }
        }
    }

    func refreshHistory() async {
        guard !refreshingHistory else { return }
        refreshingHistory = true
        defer { refreshingHistory = false }
        let user = snapshot.currentUser
        if let out = try? await runner.run(Cmd.history(user, days: 7)) {
            snapshot.userJobs = SlurmParser.parseUserJobs(out)
        }
    }

    func refreshHistoryButton() { Task { await refreshHistory() } }

    // MARK: - Job detail

    struct DetailError: Error { let message: String }

    func fetchJobDetail(_ jobID: String) async -> Result<JobDetail, DetailError> {
        guard jobID.range(of: #"^\d+(_\d+)?$"#, options: .regularExpression) != nil else {
            return .failure(DetailError(message: "Invalid job ID: \(jobID)"))
        }
        var failures: [String] = []
        do {
            let out = try await runner.run(Cmd.jobDetail(jobID))
            return .success(SlurmParser.parseJobDetail(out, jobID: jobID))
        } catch {
            failures.append(describe(error))
        }

        // Array elements and jobs which have just left squeue may no longer be
        // addressable through scontrol, while their accounting row is already
        // available through sacct.
        do {
            let out = try await runner.run(Cmd.accountingDetail(jobID))
            if let detail = SlurmParser.parseAccountingJobDetail(out, jobID: jobID) {
                return .success(detail)
            }
            failures.append("sacct returned no row for job \(jobID)")
        } catch {
            failures.append(describe(error))
        }

        // Some Slurm installations expose an array only through its parent.
        if let underscore = jobID.firstIndex(of: "_") {
            let parentID = String(jobID[..<underscore])
            do {
                let out = try await runner.run(Cmd.jobDetail(parentID))
                return .success(SlurmParser.parseJobDetail(out, jobID: jobID))
            } catch {
                failures.append(describe(error))
            }
        }
        return .failure(DetailError(message: failures.joined(separator: "\n\n")))
    }

    func retry() { connect() }

    func disconnect() {
        state = .disconnected
        let runner = self.runner
        Task.detached { await runner.close() }
    }

    private func describe(_ error: Error) -> String {
        if let e = error as? SSHRunner.RemoteError { return e.description }
        return error.localizedDescription
    }
}
