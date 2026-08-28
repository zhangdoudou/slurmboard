import SwiftUI

/// One partition row plus its optional inline expansion (nodes / running /
/// pending job sub-tables).
struct PartitionRow: View {
    let p: Partition
    let expandKind: String?
    let refreshing: Bool
    let nodes: [SlurmNode]
    let onToggleTriangle: () -> Void
    let onToggleKind: (String) -> Void
    let onRefresh: () -> Void
    let onSelectJob: (String) -> Void
    @EnvironmentObject var theme: Theme

    var body: some View {
        let pal = theme.p
        VStack(spacing: 0) {
            mainRow(pal)
            if let kind = expandKind {
                Group {
                    switch kind {
                    case "nodes":   NodeSubTable(partName: p.name, nodes: nodes)
                    case "running": JobSubTable(jobs: p.jobs.filter { $0.state == "RUNNING" }, isPending: false, onSelectJob: onSelectJob)
                    case "pending": JobSubTable(jobs: p.jobs.filter { $0.state == "PENDING" }, isPending: true, onSelectJob: onSelectJob)
                    default:        EmptyView()
                    }
                }
                .environmentObject(theme)
                .padding(.leading, 36).padding(.top, 6).padding(.bottom, 10)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(pal.bg)
            }
        }
        .overlay(Rectangle().frame(height: 1).foregroundStyle(pal.border), alignment: .bottom)
    }

    private func mainRow(_ pal: Palette) -> some View {
        let cpuIdleP = pct(p.cpuTotal - p.cpuAlloc, p.cpuTotal)
        let gpuIdleP = pct(p.gpuIdle, p.gpuTotal)
        return GeometryReader { geo in
          let w = PartCols.contentWidth(geo.size.width)
          HStack(spacing: PartCols.spacing) {
            // toggle + refresh
            HStack(spacing: 4) {
                Button(action: onToggleTriangle) {
                    Text(expandKind != nil ? "▼" : "▶").font(.system(size: 10)).foregroundStyle(pal.muted)
                }.buttonStyle(.plain)
                Button(action: onRefresh) {
                    Text("↻").font(.system(size: 12)).foregroundStyle(pal.muted)
                }
                .buttonStyle(.plain)
                .help(refreshing ? "Refreshing…" : "Refresh partition")
            }.frame(width: PartCols.width(.toggle, in: w), alignment: .center)

            Button(action: onToggleTriangle) {
                Text(p.name).font(.system(size: 12, weight: .bold)).foregroundStyle(pal.text)
                    .frame(width: PartCols.width(.name, in: w), alignment: .leading)
            }.buttonStyle(.plain)

            Text(p.avail).frame(width: PartCols.width(.avail, in: w), alignment: .leading)
                .foregroundStyle(p.avail == "up" ? pal.good : pal.bad)
            Text(p.timelimit).frame(width: PartCols.width(.timelimit, in: w), alignment: .leading).foregroundStyle(pal.muted)
            Text("\(p.nodes)").frame(width: PartCols.width(.nodes, in: w), alignment: .leading)

            // run / pend
            HStack(spacing: 2) {
                Button { onToggleKind("running") } label: {
                    HStack(spacing: 3) {
                        Text("\(p.jobsRunning)").monospacedDigit().frame(width: 24, alignment: .trailing)
                        Text("run")
                    }.foregroundStyle(pal.good)
                }.buttonStyle(.plain)
                Text("·").foregroundStyle(pal.muted)
                Button { onToggleKind("pending") } label: {
                    HStack(spacing: 3) {
                        Text("\(p.jobsPending)").monospacedDigit().frame(width: 24, alignment: .trailing)
                        Text("pend")
                    }.foregroundStyle(pal.warn)
                }.buttonStyle(.plain)
            }.frame(width: PartCols.width(.jobs, in: w), alignment: .leading)

            HStack(spacing: 4) {
                MiniBar(percent: cpuIdleP, palette: pal)
                Text("\(p.cpuTotal - p.cpuAlloc)/\(p.cpuTotal)").font(.system(size: 11))
            }.frame(width: PartCols.width(.cpu, in: w), alignment: .leading)

            Group {
                if let v = p.gpuVramGB { Text("\(v) GB").font(.system(size: 11, weight: .bold)) }
                else { Text("—").foregroundStyle(pal.muted) }
            }.frame(width: PartCols.width(.vram, in: w), alignment: .leading)

            Group {
                if p.gpuTotal > 0 {
                    HStack(spacing: 4) {
                        MiniBar(percent: gpuIdleP, palette: pal)
                        Text("\(p.gpuIdle)/\(p.gpuTotal)").font(.system(size: 11))
                    }
                } else { Text("—").foregroundStyle(pal.muted) }
            }.frame(width: PartCols.width(.gpu, in: w), alignment: .leading)
          }
          .padding(.horizontal, 8)
        }
        .frame(height: 27)
        .font(.system(size: 12)).foregroundStyle(pal.text)
        .singleLineTable()
        .contentShape(Rectangle())
    }
}

// MARK: - Node sub-table

private struct NodeSubTable: View {
    let partName: String
    let nodes: [SlurmNode]
    @EnvironmentObject var theme: Theme

    private var rows: [SlurmNode] {
        nodes.filter { $0.partitions.contains(partName) }
            .sorted { $0.gpuIdle != $1.gpuIdle ? $0.gpuIdle > $1.gpuIdle : $0.name < $1.name }
    }

    var body: some View {
        let pal = theme.p
        if rows.isEmpty {
            Text("No nodes in this partition.").font(.system(size: 12)).foregroundStyle(pal.muted)
        } else {
            VStack(alignment: .leading, spacing: 0) {
                header(pal)
                ForEach(rows) { n in row(n, pal) }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .overlay(Rectangle().frame(width: 3).foregroundStyle(pal.border), alignment: .leading)
        }
    }

    private func header(_ pal: Palette) -> some View {
        GeometryReader { geo in
            let w = max(geo.size.width - 52, 0)
            HStack(spacing: 6) {
                cell("Node", w * 0.10, pal, header: true)
                cell("State", w * 0.19, pal, header: true)
                cell("CPU (idle/total)", w * 0.17, pal, header: true)
                cell("Load", w * 0.08, pal, header: true)
                cell("Memory (idle/total)", w * 0.24, pal, header: true)
                cell("GPU (idle/total)", w * 0.15, pal, header: true)
                cell("VRAM", w * 0.07, pal, header: true)
            }
            .padding(.horizontal, 8)
        }
        .frame(height: 28)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(pal.accent.opacity(0.06))
    }

    private func row(_ n: SlurmNode, _ pal: Palette) -> some View {
        let cpuIdleP = pct(n.cpuTotal - n.cpuAlloc, n.cpuTotal)
        let memIdleP = pct(n.memTotalMB - n.memAllocMB, n.memTotalMB)
        return GeometryReader { geo in
            let w = max(geo.size.width - 52, 0)
            HStack(spacing: 6) {
                Text(n.name).font(.system(size: 11, weight: .bold)).frame(width: w * 0.10, alignment: .leading)
                StatePill(state: n.state, palette: pal).frame(width: w * 0.19, alignment: .leading)
                HStack(spacing: 5) { MiniBar(percent: cpuIdleP, palette: pal)
                    Text("\(n.cpuTotal - n.cpuAlloc) / \(n.cpuTotal)").font(.system(size: 10)) }
                    .frame(width: w * 0.17, alignment: .leading)
                Text(n.load.map { String(format: "%.2f", $0) } ?? "—").font(.system(size: 11))
                    .frame(width: w * 0.08, alignment: .leading)
                HStack(spacing: 5) { MiniBar(percent: memIdleP, palette: pal)
                    Text("\(fmtMem(n.memTotalMB - n.memAllocMB)) / \(fmtMem(n.memTotalMB))").font(.system(size: 10)) }
                    .frame(width: w * 0.24, alignment: .leading)
                Group {
                    if n.gpuTotal > 0 {
                        HStack(spacing: 5) { MiniBar(percent: pct(n.gpuIdle, n.gpuTotal), palette: pal)
                            Text("\(n.gpuIdle) / \(n.gpuTotal)").font(.system(size: 10)) }
                    } else { Text("—").foregroundStyle(pal.muted) }
                }.frame(width: w * 0.15, alignment: .leading)
                Text(n.gpuVramGB.map { "\($0) GB" } ?? "—").font(.system(size: 10)).foregroundStyle(pal.muted)
                    .frame(width: w * 0.07, alignment: .leading)
            }
            .padding(.horizontal, 8)
        }
        .frame(height: 26)
        .foregroundStyle(pal.text)
        .singleLineTable()
        .frame(maxWidth: .infinity, alignment: .leading)
        .overlay(Rectangle().frame(height: 1).foregroundStyle(pal.border), alignment: .bottom)
    }

    private func cell(_ t: String, _ w: CGFloat, _ pal: Palette, header: Bool) -> some View {
        Text(t).font(.system(size: 11, weight: .semibold)).textCase(.uppercase)
            .foregroundStyle(pal.muted).frame(width: w, alignment: .leading)
    }
}

// MARK: - Job sub-table

private struct JobSubTable: View {
    let jobs: [PartitionJob]
    let isPending: Bool
    let onSelectJob: (String) -> Void
    @EnvironmentObject var theme: Theme

    var body: some View {
        let pal = theme.p
        if jobs.isEmpty {
            Text("No jobs.").font(.system(size: 12)).foregroundStyle(pal.muted).padding(.vertical, 4)
        } else {
            VStack(alignment: .leading, spacing: 0) {
                header(pal)
                ForEach(jobs) { j in row(j, pal) }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .overlay(Rectangle().frame(width: 3).foregroundStyle(pal.border), alignment: .leading)
        }
    }

    private func header(_ pal: Palette) -> some View {
        GeometryReader { geo in
            let w = max(geo.size.width - 52, 0)
            HStack(spacing: 6) {
                head("Job ID", w * 0.10, pal); head("User", w * 0.10, pal); head("Name", w * 0.20, pal)
                head("CPUs", w * 0.07, pal); head("GPUs", w * 0.17, pal)
                head(isPending ? "Queued" : "Running", w * 0.10, pal)
                head(isPending ? "Reason" : "Nodes", w * 0.26, pal)
            }
            .padding(.horizontal, 8)
        }
        .frame(height: 28)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(pal.accent.opacity(0.06))
    }

    private func row(_ j: PartitionJob, _ pal: Palette) -> some View {
        GeometryReader { geo in
            let w = max(geo.size.width - 52, 0)
            HStack(spacing: 6) {
                Button { onSelectJob(j.id) } label: {
                    Text(j.id).font(.system(size: 11)).foregroundStyle(pal.accent)
                }.buttonStyle(.plain).frame(width: w * 0.10, alignment: .leading)
                Text(j.user).font(.system(size: 11)).frame(width: w * 0.10, alignment: .leading)
                Text(j.name).font(.system(size: 11)).lineLimit(1).truncationMode(.tail)
                    .frame(width: w * 0.20, alignment: .leading).help(j.name)
                Text(j.cpus).font(.system(size: 11)).frame(width: w * 0.07, alignment: .leading)
                Text(j.gres ?? "—").font(.system(size: 10)).foregroundStyle(pal.muted)
                    .frame(width: w * 0.17, alignment: .leading).lineLimit(1)
                Text(j.time).font(.system(size: 11)).frame(width: w * 0.10, alignment: .leading)
                Text(j.reason).font(.system(size: 10)).foregroundStyle(pal.muted)
                    .frame(width: w * 0.26, alignment: .leading).lineLimit(1).help(j.reason)
            }
            .padding(.horizontal, 8)
        }
        .frame(height: 26)
        .foregroundStyle(pal.text)
        .singleLineTable()
        .frame(maxWidth: .infinity, alignment: .leading)
        .overlay(Rectangle().frame(height: 1).foregroundStyle(pal.border), alignment: .bottom)
    }

    private func head(_ t: String, _ w: CGFloat, _ pal: Palette) -> some View {
        Text(t).font(.system(size: 11, weight: .semibold)).textCase(.uppercase)
            .foregroundStyle(pal.muted).frame(width: w, alignment: .leading)
    }
}
