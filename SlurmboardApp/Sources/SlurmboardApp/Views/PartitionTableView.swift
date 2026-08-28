import SwiftUI
import AppKit

private struct SortSpec: Equatable { var key: String; var dir: Int }

/// Center column: the partition table with filtering, multi-column sort, and
/// per-row expansion into nodes / running / pending sub-tables.
struct PartitionTableView: View {
    @ObservedObject var service: SlurmService
    let onSelectJob: (String) -> Void
    @EnvironmentObject var theme: Theme

    @State private var sortList: [SortSpec] = [SortSpec(key: "name", dir: 1)]
    @State private var expand: [String: String] = [:]     // part -> "nodes"|"running"|"pending"
    @State private var vramMin: String = ""
    @State private var idleOnly = false

    private let badges = ["①", "②", "③", "④", "⑤"]

    private var visiblePartitions: [Partition] {
        let minV = Int(vramMin) ?? 0
        let sorted = multiSort(service.snapshot.partitions, sortList)
        return sorted.filter { p in
            if idleOnly && p.gpuIdle <= 0 { return false }
            if minV > 0 && (p.gpuVramGB == nil || p.gpuVramGB! < minV) { return false }
            return true
        }
    }

    var body: some View {
        let pal = theme.p
        VStack(alignment: .leading, spacing: 8) {
            SectionTitle("Partitions")
            Text("Click a row to expand its nodes · Click a header to sort · Shift-click to add a secondary key")
                .font(.system(size: 12)).foregroundStyle(pal.muted)

            filterBar(pal)
            // The web dashboard uses one continuous table: the header and
            // body share a single outer border and corner radius.
            VStack(spacing: 0) {
                headerRow(pal)
                ScrollView {
                    LazyVStack(spacing: 0) {
                        ForEach(visiblePartitions) { p in
                            PartitionRow(p: p,
                                         expandKind: expand[p.name],
                                         refreshing: service.refreshingPartitions.contains(p.name) || service.refreshingCluster,
                                         nodes: service.snapshot.nodes,
                                         onToggleTriangle: { toggleTriangle(p.name) },
                                         onToggleKind: { kind in toggleKind(p.name, kind) },
                                         onRefresh: { service.refreshPartition(p.name) },
                                         onSelectJob: onSelectJob)
                                .environmentObject(theme)
                        }
                    }
                }
            }
            .background(pal.panel)
            .overlay(RoundedRectangle(cornerRadius: 10).stroke(pal.border))
            .clipShape(RoundedRectangle(cornerRadius: 10))
        }
        .padding(.horizontal, 12)
        .padding(.bottom, 16)
    }

    // MARK: filter bar

    private func filterBar(_ pal: Palette) -> some View {
        let total = service.snapshot.partitions.count
        let shown = visiblePartitions.count
        return HStack(spacing: 16) {
            HStack(spacing: 5) {
                Text("Min VRAM").font(.system(size: 13)).foregroundStyle(pal.text)
                TextField("GB", text: $vramMin)
                    .textFieldStyle(.roundedBorder).frame(width: 64)
            }
            Button { idleOnly.toggle() } label: {
                HStack(spacing: 6) {
                    ZStack {
                        RoundedRectangle(cornerRadius: 3)
                            .fill(idleOnly ? pal.accent : pal.panel)
                        RoundedRectangle(cornerRadius: 3)
                            .stroke(idleOnly ? pal.accent : pal.muted, lineWidth: 1.5)
                        if idleOnly {
                            Image(systemName: "checkmark")
                                .font(.system(size: 9, weight: .bold))
                                .foregroundStyle(Color.white)
                        }
                    }
                    .frame(width: 16, height: 16)
                    Text("Idle GPUs only").font(.system(size: 13)).foregroundStyle(pal.text)
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Idle GPUs only")
            .accessibilityValue(idleOnly ? "On" : "Off")
            Text(shown == total ? "\(total) partitions" : "\(shown) / \(total) partitions")
                .font(.system(size: 12)).foregroundStyle(pal.muted)
            Spacer()
        }
    }

    // MARK: header

    private func headerRow(_ pal: Palette) -> some View {
        GeometryReader { geo in
            let w = PartCols.contentWidth(geo.size.width)
            HStack(spacing: PartCols.spacing) {
                HStack(spacing: 7) {
                    Button { toggleAll() } label: {
                        Text(anyExpanded ? "▼" : "▶").font(.system(size: 10)).foregroundStyle(pal.muted)
                    }.buttonStyle(.plain)
                    Button { service.refreshCluster() } label: {
                        Text("↻").font(.system(size: 12)).foregroundStyle(pal.muted)
                    }
                    .buttonStyle(.plain)
                    .help(service.refreshingCluster ? "Refreshing…" : "Refresh all partitions")
                }
                .frame(width: PartCols.width(.toggle, in: w), alignment: .center)

                header("name", "Partition", pal, PartCols.width(.name, in: w))
                header("avail", "Avail", pal, PartCols.width(.avail, in: w))
                header("timelimit", "Time limit", pal, PartCols.width(.timelimit, in: w))
                header("nodes", "Nodes", pal, PartCols.width(.nodes, in: w))
                header("jobs_pending", "Jobs (run/pend)", pal, PartCols.width(.jobs, in: w))
                header("cpu_total", "CPU (idle/total)", pal, PartCols.width(.cpu, in: w))
                header("gpu_vram_gb", "VRAM (GB)", pal, PartCols.width(.vram, in: w))
                header("gpu_idle", "GPU (idle/total)", pal, PartCols.width(.gpu, in: w))
            }
            .padding(.horizontal, 8)
        }
        .frame(height: 28)
        .background(pal.panel)
        .overlay(Rectangle().frame(height: 1).foregroundStyle(pal.border), alignment: .bottom)
    }

    private func header(_ key: String, _ label: String, _ pal: Palette, _ width: CGFloat, flexible: Bool = false) -> some View {
        let idx = sortList.firstIndex { $0.key == key }
        var suffix = ""
        if let idx {
            suffix += sortList[idx].dir > 0 ? " ↑" : " ↓"
            if sortList.count > 1 { suffix += " " + (idx < badges.count ? badges[idx] : "\(idx + 1)") }
        }
        return Button { headerClicked(key) } label: {
            Text(label + suffix)
                .font(.system(size: 11, weight: .semibold)).textCase(.uppercase)
                .foregroundStyle(pal.muted)
                .frame(width: flexible ? nil : width, alignment: .leading)
                .frame(maxWidth: flexible ? .infinity : nil, alignment: .leading)
        }.buttonStyle(.plain)
    }

    // MARK: interactions

    private var anyExpanded: Bool { visiblePartitions.contains { expand[$0.name] != nil } }

    private func headerClicked(_ key: String) {
        let shift = NSEvent.modifierFlags.contains(.shift)
        let idx = sortList.firstIndex { $0.key == key }
        if shift {
            if let idx { sortList[idx].dir *= -1 }
            else { sortList.append(SortSpec(key: key, dir: 1)) }
        } else {
            let prevDir = (idx == 0 && sortList.count == 1) ? sortList[0].dir : 1
            sortList = [SortSpec(key: key, dir: idx == 0 ? prevDir * -1 : 1)]
        }
    }

    private func toggleTriangle(_ name: String) {
        if expand[name] != nil { expand[name] = nil } else { expand[name] = "nodes" }
    }
    private func toggleKind(_ name: String, _ kind: String) {
        if expand[name] == kind { expand[name] = nil } else { expand[name] = kind }
    }
    private func toggleAll() {
        if anyExpanded { for p in visiblePartitions { expand[p.name] = nil } }
        else { for p in visiblePartitions { expand[p.name] = "nodes" } }
    }

    // MARK: multi-sort (port of JS multiSort)

    private func multiSort(_ rows: [Partition], _ list: [SortSpec]) -> [Partition] {
        guard !list.isEmpty else { return rows }
        return rows.sorted { a, b in
            for spec in list {
                let c = compare(a, b, key: spec.key)
                if c != 0 { return (spec.dir > 0 ? c : -c) < 0 }
            }
            return false
        }
    }

    /// -1 / 0 / 1 for a<b / a==b / a>b on the given column.
    private func compare(_ a: Partition, _ b: Partition, key: String) -> Int {
        func cmpInt(_ x: Int, _ y: Int) -> Int { x < y ? -1 : x > y ? 1 : 0 }
        func cmpStr(_ x: String, _ y: String) -> Int {
            let r = x.lowercased().compare(y.lowercased())
            return r == .orderedAscending ? -1 : r == .orderedDescending ? 1 : 0
        }
        switch key {
        case "name":         return cmpStr(a.name, b.name)
        case "avail":        return cmpStr(a.avail, b.avail)
        case "timelimit":    return cmpStr(a.timelimit, b.timelimit)
        case "nodes":        return cmpInt(a.nodes, b.nodes)
        case "jobs_pending": return cmpInt(a.jobsPending, b.jobsPending)
        case "cpu_total":    return cmpInt(a.cpuTotal, b.cpuTotal)
        case "gpu_idle":     return cmpInt(a.gpuIdle, b.gpuIdle)
        case "gpu_vram_gb":
            // nil sorts lowest, like -Infinity in the JS.
            return cmpInt(a.gpuVramGB ?? Int.min, b.gpuVramGB ?? Int.min)
        default:             return 0
        }
    }
}

// Shared column widths for header + rows.
enum PartColumn { case toggle, name, avail, timelimit, nodes, jobs, cpu, vram, gpu }

enum PartCols {
    static let spacing: CGFloat = 6
    static func contentWidth(_ total: CGFloat) -> CGFloat { max(total - 64, 0) }
    static func width(_ column: PartColumn, in total: CGFloat) -> CGFloat {
        let ratio: CGFloat
        switch column {
        case .toggle: ratio = 0.035
        case .name: ratio = 0.285
        case .avail: ratio = 0.055
        case .timelimit: ratio = 0.085
        case .nodes: ratio = 0.055
        case .jobs: ratio = 0.14
        case .cpu: ratio = 0.16
        case .vram: ratio = 0.075
        case .gpu: ratio = 0.11
        }
        return total * ratio
    }
}
