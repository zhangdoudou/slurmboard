import Foundation

/// Tiny NSRegularExpression convenience wrapper.
private struct RX {
    let re: NSRegularExpression
    init(_ pattern: String) {
        // Patterns here are all known-good; force-try is fine.
        re = try! NSRegularExpression(pattern: pattern)
    }
    /// First match's capture groups (1...n), nil if no match.
    func first(_ s: String) -> [String]? {
        let range = NSRange(s.startIndex..., in: s)
        guard let m = re.firstMatch(in: s, range: range) else { return nil }
        var groups: [String] = []
        for i in 1..<m.numberOfRanges {
            if let r = Range(m.range(at: i), in: s) {
                groups.append(String(s[r]))
            } else {
                groups.append("")
            }
        }
        return groups
    }
    /// All matches, each as its capture groups.
    func all(_ s: String) -> [[String]] {
        let range = NSRange(s.startIndex..., in: s)
        return re.matches(in: s, range: range).map { m in
            (1..<m.numberOfRanges).map { i in
                Range(m.range(at: i), in: s).map { String(s[$0]) } ?? ""
            }
        }
    }
}

/// Port of slurmboard.py's parsing layer: raw `scontrol`/`sinfo`/`squeue`/`sacct`
/// text in, typed models out. Kept as pure functions so it's trivially testable.
enum SlurmParser {

    // MARK: Node field regexes (mirror _FIELD_RE)
    private static let rxName       = RX(#"NodeName=(\S+)"#)
    private static let rxState      = RX(#"\bState=(\S+)"#)
    private static let rxCpuAlloc   = RX(#"CPUAlloc=(\d+)"#)
    private static let rxCpuTotal   = RX(#"CPUTot=(\d+)"#)
    private static let rxLoad       = RX(#"\bCPULoad=(\S+)"#)
    private static let rxGres       = RX(#"\bGres=(\S+)"#)
    private static let rxPartitions = RX(#"Partitions=(\S+)"#)
    private static let rxRealMem    = RX(#"RealMemory=(\d+)"#)
    private static let rxAllocMem   = RX(#"AllocMem=(\d+)"#)
    private static let rxCfgTres    = RX(#"CfgTRES=(\S+)"#)
    private static let rxAllocTres  = RX(#"AllocTRES=(\S+)"#)

    private static let rxGresGpu      = RX(#"gpu:([a-zA-Z0-9_]+):(\d+)"#)
    private static let rxGresGpuPlain = RX(#"gpu:(\d+)"#)
    private static let rxGresVram     = RX(#"min-vram:no_consume:(\d+)([GM])"#)
    private static let rxTresGpu      = RX(#"gres/gpu=(\d+)"#)
    private static let rxTresGpuTyped = RX(#"gres/gpu:([a-zA-Z0-9_]+)=(\d+)"#)

    private static let gpuVramGB: [String: Int] = [
        "a100": 80, "a100-80": 80, "a100-40": 40, "a40": 48, "a30": 24, "a10": 24,
        "v100": 32, "v100-32": 32, "v100-16": 16, "mi300x": 192, "mi300a": 128,
        "mi250x": 128, "mi250": 128, "mi210": 64, "mi100": 32, "h100": 80, "h200": 141,
    ]

    private static let downStates: Set<String> = [
        "DOWN", "DRAIN", "DRAINING", "DRAINED", "FAIL", "FAILING", "ERROR", "UNKNOWN",
    ]
    static let activeStates: Set<String> = [
        "RUNNING", "PENDING", "COMPLETING", "RESIZING", "SUSPENDED", "REQUEUED",
    ]

    // MARK: - GPU helpers

    private static func gpuTotalFromGres(_ gres: String?) -> (type: String?, total: Int) {
        guard let gres, gres != "(null)", !gres.isEmpty else { return (nil, 0) }
        if let g = rxGresGpu.first(gres) { return (g[0], Int(g[1]) ?? 0) }
        if let g = rxGresGpuPlain.first(gres) { return (nil, Int(g[0]) ?? 0) }
        return (nil, 0)
    }

    private static func gpuCountFromTres(_ tres: String?, gpuType: String?) -> Int {
        guard let tres, !tres.isEmpty else { return 0 }
        if let gpuType {
            for g in rxTresGpuTyped.all(tres) where g[0] == gpuType {
                return Int(g[1]) ?? 0
            }
        }
        if let g = rxTresGpu.first(tres) { return Int(g[0]) ?? 0 }
        return 0
    }

    private static func vramFromGres(_ gres: String?) -> Int? {
        guard let gres else { return nil }
        if let g = rxGresVram.first(gres), let val = Int(g[0]) {
            return g[1] == "G" ? val : val / 1024
        }
        if let g = rxGresGpu.first(gres) {
            let model = g[0].lowercased()
            if let v = gpuVramGB[model] { return v }
            for (key, v) in gpuVramGB where model.hasPrefix(key) || key.hasPrefix(model) {
                return v
            }
        }
        return nil
    }

    // MARK: - Nodes

    static func parseNodes(_ text: String) -> [SlurmNode] {
        var nodes: [SlurmNode] = []
        for rawLine in text.split(separator: "\n", omittingEmptySubsequences: false) {
            let line = rawLine.trimmingCharacters(in: .whitespaces)
            guard line.hasPrefix("NodeName=") else { continue }

            let gres = rxGres.first(line)?.first
            let (gpuType, gresTotal) = gpuTotalFromGres(gres)
            let gpuAlloc = gpuCountFromTres(rxAllocTres.first(line)?.first, gpuType: gpuType)
            let cfgTotal = gpuCountFromTres(rxCfgTres.first(line)?.first, gpuType: gpuType)
            let gpuTotal = cfgTotal != 0 ? cfgTotal : gresTotal

            let cpuAlloc = Int(rxCpuAlloc.first(line)?.first ?? "") ?? 0
            let cpuTotal = Int(rxCpuTotal.first(line)?.first ?? "") ?? 0
            let realMem  = Int(rxRealMem.first(line)?.first ?? "") ?? 0
            let allocMem = Int(rxAllocMem.first(line)?.first ?? "") ?? 0

            let partsRaw = rxPartitions.first(line)?.first
            let partitions = (partsRaw?.isEmpty == false)
                ? partsRaw!.split(separator: ",").map(String.init) : []

            let loadStr = rxLoad.first(line)?.first
            let load = (loadStr != nil && loadStr != "N/A") ? Double(loadStr!) : nil

            nodes.append(SlurmNode(
                name: rxName.first(line)?.first ?? "",
                state: rxState.first(line)?.first ?? "UNKNOWN",
                partitions: partitions,
                cpuAlloc: cpuAlloc,
                cpuIdle: max(cpuTotal - cpuAlloc, 0),
                cpuTotal: cpuTotal,
                load: load,
                memAllocMB: allocMem,
                memTotalMB: realMem,
                gpuType: gpuType,
                gpuAlloc: gpuAlloc,
                gpuIdle: max(gpuTotal - gpuAlloc, 0),
                gpuTotal: gpuTotal,
                gpuVramGB: vramFromGres(gres)
            ))
        }
        return nodes
    }

    // MARK: - Partition time limits (sinfo -h -o "%P|%l")

    static func parsePartitionLimits(_ text: String) -> [String: String] {
        var limits: [String: String] = [:]
        for rawLine in text.split(separator: "\n") {
            let line = rawLine.trimmingCharacters(in: .whitespaces)
            guard let sep = line.firstIndex(of: "|") else { continue }
            let part = String(line[..<sep])
            let tl = String(line[line.index(after: sep)...])
            limits[trimTrailing(part, "*")] = tl
        }
        return limits
    }

    // MARK: - Job counts (squeue -h -o "%P|%i|%u|%j|%T|%M|%C|%b|%R|%l|%V")

    struct JobCounts {
        var running = 0
        var pending = 0
        var timelimit: String?
        var jobs: [PartitionJob] = []
    }

    static func parseJobCounts(_ text: String) -> [String: JobCounts] {
        var counts: [String: JobCounts] = [:]
        for rawLine in text.split(separator: "\n") {
            let line = rawLine.trimmingCharacters(in: .whitespaces)
            if line.isEmpty { continue }
            let parts = splitLimited(line, sep: "|", max: 11)
            if parts.count < 10 { continue }
            let part = parts[0], jid = parts[1], user = parts[2], name = parts[3]
            let state = parts[4], timeUsed = parts[5], cpus = parts[6]
            let gres = parts[7], reason = parts[8], timelimit = parts[9]
            let submit = parts.count > 10 ? parts[10] : nil
            let stateUp = state.uppercased()

            var c = counts[part] ?? JobCounts()
            if stateUp == "RUNNING" { c.running += 1 }
            else if stateUp == "PENDING" { c.pending += 1 }
            if !timelimit.isEmpty, !["INVALID", "NOT_SET"].contains(timelimit) {
                c.timelimit = timelimit
            }
            c.jobs.append(PartitionJob(
                id: jid, user: user, name: name, state: stateUp, time: timeUsed,
                cpus: cpus, gres: (gres.isEmpty || gres == "N/A") ? nil : gres,
                reason: reason, partition: part, submit: submit))
            counts[part] = c
        }
        return counts
    }

    // MARK: - Cluster snapshot assembly (build_cluster_snapshot)

    static func buildClusterSnapshot(nodesText: String,
                                     jobCountsText: String,
                                     limitsText: String,
                                     generatedAt: String) -> ClusterSnapshot {
        let nodes = parseNodes(nodesText)
        let jobCounts = parseJobCounts(jobCountsText)
        let partLimits = parsePartitionLimits(limitsText)

        var summary = ClusterSummary()
        summary.nodeCount = nodes.count
        for n in nodes {
            summary.cpuAlloc += n.cpuAlloc
            summary.cpuTotal += n.cpuTotal
            summary.memAllocMB += n.memAllocMB
            summary.memTotalMB += n.memTotalMB
            summary.gpuAlloc += n.gpuAlloc
            summary.gpuTotal += n.gpuTotal
            summary.nodeStates[n.state, default: 0] += 1
            if n.gpuTotal > 0 {
                let t = n.gpuType ?? "gpu"
                var b = summary.gpuByType[t] ?? GpuTypeBucket(type: t, alloc: 0, total: 0, nodes: 0, partitions: [:])
                b.alloc += n.gpuAlloc
                b.total += n.gpuTotal
                b.nodes += 1
                for p in n.partitions where !p.isEmpty {
                    var pb = b.partitions[p] ?? GpuPartitionCount(alloc: 0, total: 0)
                    pb.alloc += n.gpuAlloc
                    pb.total += n.gpuTotal
                    b.partitions[p] = pb
                }
                summary.gpuByType[t] = b
            }
        }

        // Per-partition aggregation.
        struct Agg {
            var nodes = 0, cpuAlloc = 0, cpuTotal = 0, gpuAlloc = 0, gpuTotal = 0
            var states: [String: Int] = [:]
            var vramVals: [Int] = []
        }
        var partAgg: [String: Agg] = [:]
        for n in nodes {
            for p in n.partitions where !p.isEmpty {
                var a = partAgg[p] ?? Agg()
                a.nodes += 1
                a.cpuAlloc += n.cpuAlloc
                a.cpuTotal += n.cpuTotal
                a.gpuAlloc += n.gpuAlloc
                a.gpuTotal += n.gpuTotal
                a.states[n.state, default: 0] += 1
                if let v = n.gpuVramGB { a.vramVals.append(v) }
                partAgg[p] = a
            }
        }

        var partitions: [Partition] = []
        for name in partAgg.keys.sorted() {
            let a = partAgg[name]!
            let jc = jobCounts[name] ?? JobCounts()
            let hasLive = a.states.keys.contains { !downStates.contains(trimTrailing($0, "*+~").uppercased()) }
            partitions.append(Partition(
                name: name,
                nodes: a.nodes,
                cpuAlloc: a.cpuAlloc,
                cpuTotal: a.cpuTotal,
                gpuAlloc: a.gpuAlloc,
                gpuTotal: a.gpuTotal,
                states: a.states,
                avail: hasLive ? "up" : "down",
                timelimit: partLimits[name] ?? "—",
                gpuIdle: a.gpuTotal - a.gpuAlloc,
                jobsRunning: jc.running,
                jobsPending: jc.pending,
                jobs: jc.jobs,
                gpuVramGB: a.vramVals.max()
            ))
        }

        var snap = ClusterSnapshot()
        snap.generatedAt = generatedAt
        snap.summary = summary
        snap.partitions = partitions
        snap.nodes = nodes
        return snap
    }

    // MARK: - Active queue (squeue -u USER -h -o "%i|%j|%T|%P|%M|%V|%C|%b")

    static func parseActiveQueue(_ text: String) -> [ActiveJob] {
        var jobs: [ActiveJob] = []
        for rawLine in text.split(separator: "\n") {
            let parts = splitLimited(String(rawLine), sep: "|", max: 8)
            if parts.count < 7 { continue }
            let gres = parts.count > 7 ? parts[7] : ""
            jobs.append(ActiveJob(
                id: parts[0], name: parts[1], state: parts[2], partition: parts[3],
                time: parts[4], submit: parts[5], cpus: parts[6],
                gres: gres.isEmpty ? nil : gres))
        }
        return jobs
    }

    // MARK: - User history (sacct ... --parsable2)

    static func parseUserJobs(_ text: String) -> [UserJob] {
        var jobs: [UserJob] = []
        var seen = Set<String>()
        for rawLine in text.split(separator: "\n") {
            let parts = String(rawLine).components(separatedBy: "|")
            if parts.count < 8 { continue }
            let jid = parts[0]
            if jid.contains(".") { continue }        // skip .batch / .extern sub-steps
            if seen.contains(jid) { continue }
            seen.insert(jid)
            let state = parts[2].split(separator: " ").first.map(String.init) ?? parts[2]
            let tres = parts[6]
            var gres = ""
            for item in tres.split(separator: ",") where item.hasPrefix("gres/gpu:") {
                gres = String(item); break
            }
            if gres.isEmpty {
                for item in tres.split(separator: ",")
                where item.hasPrefix("gres/gpu=") && item != "gres/gpu=0" {
                    gres = String(item); break
                }
            }
            jobs.append(UserJob(
                id: jid, name: parts[1], state: state, submit: parts[3],
                time: parts[4], cpus: parts[5],
                gres: gres.isEmpty ? nil : gres,
                partition: parts[7],
                done: !activeStates.contains(state)))
        }
        jobs.sort { ($0.submit ?? "") > ($1.submit ?? "") }
        return jobs
    }

    // MARK: - Job detail (scontrol show job)

    private static let jobFieldRegexes: [(String, RX)] = [
        ("job_name", RX(#"JobName=(\S+)"#)),
        ("user", RX(#"UserId=([^(\s]+)"#)),
        ("account", RX(#"\bAccount=(\S+)"#)),
        ("qos", RX(#"\bQOS=(\S+)"#)),
        ("state", RX(#"JobState=(\S+)"#)),
        ("reason", RX(#"\bReason=(\S+)"#)),
        ("partition", RX(#"\bPartition=(\S+)"#)),
        ("priority", RX(#"Priority=(\d+)"#)),
        ("num_nodes", RX(#"NumNodes=(\d+)"#)),
        ("num_cpus", RX(#"NumCPUs=(\d+)"#)),
        ("num_tasks", RX(#"NumTasks=(\d+)"#)),
        ("cpus_task", RX(#"CPUs/Task=(\d+)"#)),
        ("tres", RX(#"\bTRES=(\S+)"#)),
        ("gres_raw", RX(#"\bGres=(\S+)"#)),
        ("runtime", RX(#"RunTime=(\S+)"#)),
        ("timelimit", RX(#"TimeLimit=(\S+)"#)),
        ("submit_time", RX(#"SubmitTime=(\S+)"#)),
        ("start_time", RX(#"StartTime=(\S+)"#)),
        ("end_time", RX(#"EndTime=(\S+)"#)),
        ("nodelist", RX(#"NodeList=(\S+)"#)),
        ("batch_host", RX(#"BatchHost=(\S+)"#)),
        ("exit_code", RX(#"ExitCode=(\S+)"#)),
        ("mem_cpu", RX(#"MinMemoryCPU=(\S+)"#)),
        ("mem_node", RX(#"MinMemoryNode=(\S+)"#)),
        ("workdir", RX(#"WorkDir=(.+)"#)),
        ("command", RX(#"Command=(.+)"#)),
        ("stdout", RX(#"StdOut=(.+)"#)),
        ("stderr", RX(#"StdErr=(.+)"#)),
    ]

    static func parseJobDetail(_ text: String, jobID: String) -> JobDetail {
        var detail = JobDetail(jobID: jobID)
        for (key, rx) in jobFieldRegexes {
            if let g = rx.first(text)?.first {
                detail.fields[key] = g.trimmingCharacters(in: .whitespaces)
            }
        }
        let tres = detail.fields["tres"] ?? ""
        if let g = rxTresGpuTyped.first(tres) {
            detail.gpuType = g[0]; detail.gpuCount = Int(g[1]) ?? 0
        } else if let g = rxTresGpu.first(tres) {
            detail.gpuCount = Int(g[0]) ?? 0
        }
        let gresRaw = detail.fields["gres_raw"] ?? ""
        if detail.gpuType == nil, !gresRaw.isEmpty, gresRaw != "(null)",
           let g = rxGresGpu.first(gresRaw) {
            detail.gpuType = g[0]
            if detail.gpuCount == 0 { detail.gpuCount = Int(g[1]) ?? 0 }
        }
        return detail
    }

    /// Parse the compact parsable2 row used when an active lookup has raced
    /// with job completion or an array element is unavailable to scontrol.
    static func parseAccountingJobDetail(_ text: String, jobID: String) -> JobDetail? {
        let lines = text.split(whereSeparator: \Character.isNewline).map(String.init)
        var fallback: [String]?
        var exact: [String]?
        for line in lines {
            let columns = line.split(separator: "|", omittingEmptySubsequences: false).map(String.init)
            guard columns.count >= 17 else { continue }
            if fallback == nil { fallback = columns }
            if columns[0] == jobID { exact = columns; break }
        }
        guard let c = exact ?? fallback else { return nil }
        var detail = JobDetail(jobID: jobID)
        let mapping: [(String, Int)] = [
            ("job_name", 1), ("user", 2), ("account", 3), ("qos", 4),
            ("state", 5), ("partition", 6), ("num_nodes", 7),
            ("num_cpus", 8), ("runtime", 9), ("timelimit", 10),
            ("submit_time", 11), ("start_time", 12), ("end_time", 13),
            ("nodelist", 14), ("exit_code", 15), ("tres", 16),
        ]
        for (key, index) in mapping where index < c.count && !c[index].isEmpty {
            detail.fields[key] = c[index]
        }
        let tres = detail.fields["tres"] ?? ""
        if let g = rxTresGpuTyped.first(tres) {
            detail.gpuType = g[0]; detail.gpuCount = Int(g[1]) ?? 0
        } else if let g = rxTresGpu.first(tres) {
            detail.gpuCount = Int(g[0]) ?? 0
        }
        return detail
    }

    // MARK: - small string helpers

    private static func trimTrailing(_ s: String, _ chars: String) -> String {
        var out = Substring(s)
        while let last = out.last, chars.contains(last) { out = out.dropLast() }
        return String(out)
    }

    /// Like Python's str.split(sep, maxsplit): at most `max` pieces, remainder
    /// kept intact in the last field.
    private static func splitLimited(_ s: String, sep: Character, max: Int) -> [String] {
        guard max > 1 else { return [s] }
        var result: [String] = []
        var rest = Substring(s)
        while result.count < max - 1, let idx = rest.firstIndex(of: sep) {
            result.append(String(rest[..<idx]))
            rest = rest[rest.index(after: idx)...]
        }
        result.append(String(rest))
        return result
    }
}
