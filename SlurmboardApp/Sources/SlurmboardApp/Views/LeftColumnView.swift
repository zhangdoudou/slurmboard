import SwiftUI

private struct GpuTableWidthKey: PreferenceKey {
    static var defaultValue: CGFloat = 360
    static func reduce(value: inout CGFloat, nextValue: () -> CGFloat) { value = nextValue() }
}

/// Left column: cluster summary cards + GPUs-by-type table.
struct LeftColumnView: View {
    let summary: ClusterSummary
    @EnvironmentObject var theme: Theme

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 8) {
                SectionTitle("Cluster summary")
                SummaryCards(s: summary)

                SectionTitle("GPUs by type").padding(.top, 8)
                GpuByTypeTable(byType: summary.gpuByType)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.trailing, 12)
            .padding(.bottom, 16)
        }
    }
}

struct SectionTitle: View {
    let text: String
    @EnvironmentObject var theme: Theme
    init(_ text: String) { self.text = text }
    var body: some View {
        Text(text)
            .font(.system(size: 13, weight: .semibold))
            .textCase(.uppercase)
            .foregroundStyle(theme.p.muted)
            .padding(.vertical, 2)
    }
}

// MARK: - Summary cards

private struct SummaryCards: View {
    let s: ClusterSummary
    @EnvironmentObject var theme: Theme

    var body: some View {
        let cpuIdle = pct(s.cpuTotal - s.cpuAlloc, s.cpuTotal)
        let memIdle = pct(s.memTotalMB - s.memAllocMB, s.memTotalMB)
        let gpuIdle = pct(s.gpuTotal - s.gpuAlloc, s.gpuTotal)
        let states = s.nodeStates.sorted { $0.value > $1.value }
            .map { "\($0.value) \($0.key.lowercased())" }.joined(separator: ", ")

        VStack(spacing: 6) {
            card(label: "Nodes", value: "\(s.nodeCount)", sub: states.isEmpty ? "—" : states, bar: nil)
            card(label: "CPUs", value: "\(s.cpuTotal - s.cpuAlloc) / \(s.cpuTotal)",
                 sub: "\(cpuIdle)% idle", bar: cpuIdle)
            card(label: "Memory", value: "\(fmtMem(s.memTotalMB - s.memAllocMB)) / \(fmtMem(s.memTotalMB))",
                 sub: "\(memIdle)% idle", bar: memIdle)
            card(label: "GPUs", value: "\(s.gpuTotal - s.gpuAlloc) idle / \(s.gpuTotal)",
                 sub: "\(gpuIdle)% idle", bar: gpuIdle)
        }
    }

    @ViewBuilder
    private func card(label: String, value: String, sub: String, bar: Int?) -> some View {
        let pal = theme.p
        VStack(alignment: .leading, spacing: 3) {
            Text(label).font(.system(size: 11)).textCase(.uppercase).foregroundStyle(pal.muted)
            Text(value).font(.system(size: 15, weight: .bold)).foregroundStyle(pal.text)
            if let bar {
                IdleBar(percent: bar, palette: pal, height: 5)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.horizontal, 12).padding(.vertical, 8)
        .background(pal.panel)
        .overlay(RoundedRectangle(cornerRadius: 10).stroke(pal.border))
        .clipShape(RoundedRectangle(cornerRadius: 10))
    }
}

// MARK: - GPUs by type

private struct GpuByTypeTable: View {
    let byType: [String: GpuTypeBucket]
    @EnvironmentObject var theme: Theme
    @State private var sortKey = "total"
    @State private var sortDir = -1
    @State private var expanded: Set<String> = []
    @State private var tableWidth: CGFloat = 360

    private struct Widths {
        let type, alloc, idle, total, idlePercent, nodes: CGFloat
    }

    private func widths(for width: CGFloat) -> Widths {
        // 16 pt horizontal padding, 16 pt disclosure control and six 4 pt gaps.
        let available = max(220, width - 56)
        return Widths(type: available * 0.30, alloc: available * 0.11,
                      idle: available * 0.10, total: available * 0.11,
                      idlePercent: available * 0.28, nodes: available * 0.10)
    }

    private var rows: [GpuTypeBucket] {
        let list = Array(byType.values)
        return list.sorted { a, b in
            let cmp: Int
            switch sortKey {
            case "type":     cmp = a.type.localizedCompare(b.type) == .orderedAscending ? -1 : 1
            case "alloc":    cmp = a.alloc - b.alloc
            case "idle":     cmp = a.idle - b.idle
            case "total":    cmp = a.total - b.total
            case "idle_pct": cmp = pct(a.idle, a.total) - pct(b.idle, b.total)
            case "nodes":    cmp = a.nodes - b.nodes
            default:         cmp = 0
            }
            return sortDir * cmp < 0
        }
    }

    var body: some View {
        let pal = theme.p
        let columns = widths(for: tableWidth)
        VStack(spacing: 0) {
            headerRow(pal, columns)
            if byType.isEmpty {
                Text("No GPUs detected.")
                    .font(.system(size: 12)).foregroundStyle(pal.muted)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(8)
            } else {
                ForEach(rows) { b in
                    gpuRow(b, pal, columns)
                    if expanded.contains(b.type) {
                        partitionBreakdown(b, pal, widths(for: tableWidth - 36))
                    }
                }
            }
        }
        .background(pal.panel)
        .overlay(RoundedRectangle(cornerRadius: 10).stroke(pal.border))
        .clipShape(RoundedRectangle(cornerRadius: 10))
        .background(GeometryReader { proxy in
            Color.clear.preference(key: GpuTableWidthKey.self, value: proxy.size.width)
        })
        .onPreferenceChange(GpuTableWidthKey.self) { tableWidth = $0 }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func headerRow(_ pal: Palette, _ w: Widths) -> some View {
        HStack(spacing: 4) {
            Button {
                let anyOpen = byType.keys.contains { expanded.contains($0) }
                expanded = anyOpen ? [] : Set(byType.keys)
            } label: {
                Text(byType.keys.contains { expanded.contains($0) } ? "▼" : "▶")
                    .font(.system(size: 10)).foregroundStyle(pal.muted).frame(width: 16)
            }.buttonStyle(.plain)
            sortableHeader("type", "Type", pal, width: w.type, alignment: .leading)
            sortableHeader("alloc", "Alloc", pal, width: w.alloc)
            sortableHeader("idle", "Idle", pal, width: w.idle)
            sortableHeader("total", "Total", pal, width: w.total)
            sortableHeader("idle_pct", "Idle%", pal, width: w.idlePercent, alignment: .leading)
            sortableHeader("nodes", "N", pal, width: w.nodes)
        }
        .padding(.horizontal, 8).padding(.vertical, 6)
        .background(pal.accent.opacity(0.06))
    }

    private func sortableHeader(_ key: String, _ label: String, _ pal: Palette,
                                width: CGFloat, alignment: Alignment = .trailing) -> some View {
        let arrow = sortKey == key ? (sortDir > 0 ? " ↑" : " ↓") : ""
        return Button {
            if sortKey == key { sortDir *= -1 } else { sortKey = key; sortDir = -1 }
        } label: {
            Text(label + arrow)
                .font(.system(size: 11, weight: .semibold)).textCase(.uppercase)
                .foregroundStyle(pal.muted)
                .lineLimit(1)
                .minimumScaleFactor(0.8)
                .frame(width: width, alignment: alignment)
        }.buttonStyle(.plain)
    }

    private func gpuRow(_ b: GpuTypeBucket, _ pal: Palette, _ w: Widths) -> some View {
        let idleP = pct(b.idle, b.total)
        return HStack(spacing: 4) {
            Button { toggle(b.type) } label: {
                Text(expanded.contains(b.type) ? "▼" : "▶")
                    .font(.system(size: 10)).foregroundStyle(pal.muted).frame(width: 16)
            }.buttonStyle(.plain)
            Button { toggle(b.type) } label: {
                Text(b.type).font(.system(size: 12, weight: .bold)).foregroundStyle(pal.text)
                    .frame(width: w.type, alignment: .leading)
            }.buttonStyle(.plain)
            Text("\(b.alloc)").frame(width: w.alloc, alignment: .trailing)
            Text("\(b.idle)").frame(width: w.idle, alignment: .trailing)
            Text("\(b.total)").frame(width: w.total, alignment: .trailing)
            HStack(spacing: 4) { MiniBar(percent: idleP, palette: pal); Text("\(idleP)%") }
                .frame(width: w.idlePercent, alignment: .leading)
            Text("\(b.nodes)").frame(width: w.nodes, alignment: .trailing)
        }
        .font(.system(size: 12)).foregroundStyle(pal.text)
        .singleLineTable()
        .padding(.horizontal, 8).padding(.vertical, 5)
        .overlay(Rectangle().frame(height: 1).foregroundStyle(pal.border), alignment: .bottom)
    }

    private func partitionBreakdown(_ b: GpuTypeBucket, _ pal: Palette, _ w: Widths) -> some View {
        let parts = b.partitions.sorted { $0.value.total > $1.value.total }
        return VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 4) {
                Text("Partition").frame(width: w.type + 16, alignment: .leading)
                Text("Alloc").frame(width: w.alloc, alignment: .trailing)
                Text("Idle").frame(width: w.idle, alignment: .trailing)
                Text("Total").frame(width: w.total, alignment: .trailing)
                Text("Idle%").frame(width: w.idlePercent, alignment: .leading)
                Spacer().frame(width: w.nodes)
            }
            .font(.system(size: 11, weight: .semibold))
            .textCase(.uppercase)
            .foregroundStyle(pal.muted)
            .padding(.horizontal, 8).padding(.vertical, 6)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(pal.accent.opacity(0.06))
            ForEach(parts, id: \.key) { name, pv in
                let idleP = pct(pv.idle, pv.total)
                HStack(spacing: 4) {
                    Text(name).font(.system(size: 11, weight: .bold)).foregroundStyle(pal.text)
                        .frame(width: w.type + 16, alignment: .leading)
                    Text("\(pv.alloc)").frame(width: w.alloc, alignment: .trailing)
                    Text("\(pv.idle)").frame(width: w.idle, alignment: .trailing)
                    Text("\(pv.total)").frame(width: w.total, alignment: .trailing)
                    HStack(spacing: 4) { MiniBar(percent: idleP, palette: pal); Text("\(idleP)%") }
                        .frame(width: w.idlePercent, alignment: .leading)
                    Spacer().frame(width: w.nodes)
                }
                .font(.system(size: 11)).foregroundStyle(pal.muted)
                .singleLineTable()
                .padding(.horizontal, 8).padding(.vertical, 4)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
        .padding(.leading, 36).padding(.top, 6).padding(.bottom, 10)
        .background(theme.p.bg)
        .overlay(Rectangle().frame(width: 3).foregroundStyle(pal.border), alignment: .leading)
    }

    private func toggle(_ type: String) {
        if expanded.contains(type) { expanded.remove(type) } else { expanded.insert(type) }
    }
}
