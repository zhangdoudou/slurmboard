import Foundation

// Swift mirrors of the structures slurmboard.py parses out of scontrol / sinfo /
// squeue / sacct. Field names and semantics follow the Python original so the
// native UI reproduces the web dashboard exactly.

/// A single compute node (`scontrol -o show node`).
struct SlurmNode: Identifiable, Hashable {
    let name: String
    let state: String
    let partitions: [String]
    let cpuAlloc: Int
    let cpuIdle: Int
    let cpuTotal: Int
    let load: Double?
    let memAllocMB: Int
    let memTotalMB: Int
    let gpuType: String?
    let gpuAlloc: Int
    let gpuIdle: Int
    let gpuTotal: Int
    let gpuVramGB: Int?

    var id: String { name }
}

/// A job as seen in a partition's queue (`squeue`).
struct PartitionJob: Identifiable, Hashable {
    let id: String
    let user: String
    let name: String
    let state: String
    let time: String
    let cpus: String
    let gres: String?
    let reason: String
    let partition: String
    let submit: String?
}

/// Aggregated per-partition view.
struct Partition: Identifiable, Hashable {
    let name: String
    var nodes: Int
    var cpuAlloc: Int
    var cpuTotal: Int
    var gpuAlloc: Int
    var gpuTotal: Int
    var states: [String: Int]
    var avail: String            // "up" | "down"
    var timelimit: String
    var gpuIdle: Int
    var jobsRunning: Int
    var jobsPending: Int
    var jobs: [PartitionJob]
    var gpuVramGB: Int?

    var id: String { name }
    var cpuIdle: Int { max(cpuTotal - cpuAlloc, 0) }
}

/// GPU roll-up for one GPU model, with a per-partition breakdown.
struct GpuTypeBucket: Identifiable, Hashable {
    let type: String
    var alloc: Int
    var total: Int
    var nodes: Int
    var partitions: [String: GpuPartitionCount]   // partition -> alloc/total

    var id: String { type }
    var idle: Int { total - alloc }
}

struct GpuPartitionCount: Hashable {
    var alloc: Int
    var total: Int
    var idle: Int { total - alloc }
}

/// Cluster-wide totals (left column summary).
struct ClusterSummary: Hashable {
    var cpuAlloc = 0
    var cpuTotal = 0
    var memAllocMB = 0
    var memTotalMB = 0
    var gpuAlloc = 0
    var gpuTotal = 0
    var nodeCount = 0
    var nodeStates: [String: Int] = [:]
    var gpuByType: [String: GpuTypeBucket] = [:]
}

/// A job in the user's active queue (`squeue -u`).
struct ActiveJob: Identifiable, Hashable {
    let id: String
    let name: String
    let state: String
    let partition: String
    let time: String
    let submit: String?
    let cpus: String
    let gres: String?
}

/// A historical job from `sacct` (My Jobs → History).
struct UserJob: Identifiable, Hashable {
    let id: String
    let name: String
    let state: String
    let submit: String?
    let time: String
    let cpus: String
    let gres: String?
    let partition: String
    let done: Bool
}

/// Everything shown for one cluster at a point in time.
struct ClusterSnapshot {
    var generatedAt: String = ""
    var summary = ClusterSummary()
    var partitions: [Partition] = []
    var nodes: [SlurmNode] = []
    var currentUser: String = ""
    var activeQueue: [ActiveJob] = []
    var userJobs: [UserJob] = []
}

/// Full `scontrol show job <id>` detail.
struct JobDetail {
    let jobID: String
    var fields: [String: String] = [:]      // raw keyed values, "—" when absent
    var gpuCount: Int = 0
    var gpuType: String?

    func value(_ key: String) -> String? {
        guard let v = fields[key], !v.isEmpty,
              v != "(null)", v != "N/A" else { return nil }
        return v
    }
}
