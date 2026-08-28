import SwiftUI

/// A row shared by the Active Queue and History tables.
struct JobRowData: Identifiable {
    let id: String
    let state: String
    let partition: String
    let time: String
    let submit: String?
    let done: Bool
}

/// Right column: "My Jobs" — active queue on top, history below, each a small
/// sortable table. Uses a native VSplitView so the divider is draggable.
struct MyJobsView: View {
    @ObservedObject var service: SlurmService
    let onSelectJob: (String) -> Void
    @EnvironmentObject var theme: Theme

    private var activeRows: [JobRowData] {
        service.snapshot.activeQueue.map {
            JobRowData(id: $0.id, state: $0.state, partition: $0.partition,
                       time: $0.time, submit: $0.submit, done: false)
        }
    }
    private var historyRows: [JobRowData] {
        service.snapshot.userJobs.filter { $0.done }.map {
            JobRowData(id: $0.id, state: $0.state, partition: $0.partition,
                       time: $0.time, submit: $0.submit, done: true)
        }
    }

    var body: some View {
        let pal = theme.p
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 6) {
                SectionTitle("My Jobs")
                if !service.snapshot.currentUser.isEmpty {
                    Text("(\(service.snapshot.currentUser))")
                        .font(.system(size: 12)).foregroundStyle(pal.muted)
                }
            }
            VSplitView {
                JobTable(title: "Active Queue", rows: activeRows,
                         defaultKey: "state", defaultDir: 1,
                         refreshing: service.refreshingActive,
                         onRefresh: { service.refreshActiveQueue() },
                         emptyText: "No active jobs.",
                         onSelectJob: onSelectJob)
                    .environmentObject(theme)
                JobTable(title: "History", subtitle: "last 7 days", rows: historyRows,
                         defaultKey: "submit", defaultDir: -1,
                         refreshing: service.refreshingHistory,
                         onRefresh: { service.refreshHistoryButton() },
                         emptyText: "No history found.",
                         onSelectJob: onSelectJob)
                    .environmentObject(theme)
            }
        }
        .padding(.leading, 12)
        .padding(.bottom, 12)
    }
}

private struct JobTable: View {
    let title: String
    var subtitle: String? = nil
    let rows: [JobRowData]
    let defaultKey: String
    let defaultDir: Int
    let refreshing: Bool
    let onRefresh: () -> Void
    let emptyText: String
    let onSelectJob: (String) -> Void
    @EnvironmentObject var theme: Theme

    @State private var sortKey: String
    @State private var sortDir: Int

    init(title: String, subtitle: String? = nil, rows: [JobRowData],
         defaultKey: String, defaultDir: Int, refreshing: Bool,
         onRefresh: @escaping () -> Void, emptyText: String,
         onSelectJob: @escaping (String) -> Void) {
        self.title = title; self.subtitle = subtitle; self.rows = rows
        self.defaultKey = defaultKey; self.defaultDir = defaultDir
        self.refreshing = refreshing; self.onRefresh = onRefresh
        self.emptyText = emptyText; self.onSelectJob = onSelectJob
        _sortKey = State(initialValue: defaultKey)
        _sortDir = State(initialValue: defaultDir)
    }

    private var sorted: [JobRowData] {
        rows.sorted { a, b in
            let cmp: Int
            switch sortKey {
            case "id":        cmp = (Int(a.id) ?? 0) - (Int(b.id) ?? 0)
            case "time":      cmp = a.time.compare(b.time).rawInt
            case "partition": cmp = a.partition.compare(b.partition).rawInt
            case "submit":    cmp = (a.submit ?? "").compare(b.submit ?? "").rawInt
            case "state":     cmp = a.state.compare(b.state).rawInt
            default:          cmp = 0
            }
            return sortDir * cmp < 0
        }
    }

    var body: some View {
        let pal = theme.p
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 6) {
                Text(title).font(.system(size: 13, weight: .semibold)).textCase(.uppercase)
                    .foregroundStyle(pal.muted)
                Button(action: onRefresh) {
                    Image(systemName: "arrow.clockwise")
                        .font(.system(size: 11, weight: .semibold))
                        .foregroundStyle(refreshing ? pal.accent : pal.muted)
                        .frame(width: 22, height: 22)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .help(refreshing ? "Refreshing…" : "Refresh")
                .accessibilityLabel("Refresh \(title)")
                if let subtitle { Text(subtitle).font(.system(size: 11)).foregroundStyle(pal.muted) }
            }
            headerRow(pal)
            if sorted.isEmpty {
                Text(emptyText).font(.system(size: 13)).foregroundStyle(pal.muted).padding(.vertical, 4)
                Spacer(minLength: 0)
            } else {
                ScrollView {
                    LazyVStack(spacing: 0) { ForEach(sorted) { row($0, pal) } }
                }
            }
        }
        .frame(minHeight: 80)
        .padding(.vertical, 4)
    }

    private func headerRow(_ pal: Palette) -> some View {
        HStack(spacing: 4) {
            head("id", "ID", 54, pal)
            head("state", "St", 30, pal)
            head("partition", "Partition", nil, pal, flexible: true)
            head("time", "Time", 60, pal)
            head("submit", "Date", 74, pal)
        }
        .padding(.horizontal, 4).padding(.vertical, 4)
        .overlay(Rectangle().frame(height: 1).foregroundStyle(pal.border), alignment: .bottom)
    }

    private func head(_ key: String, _ label: String, _ w: CGFloat?, _ pal: Palette, flexible: Bool = false) -> some View {
        let arrow = sortKey == key ? (sortDir > 0 ? " ↑" : " ↓") : ""
        return Button {
            if sortKey == key { sortDir *= -1 } else { sortKey = key; sortDir = 1 }
        } label: {
            Text(label + arrow).font(.system(size: 11, weight: .semibold))
                .foregroundStyle(pal.muted)
                .frame(width: w, alignment: .leading)
                .frame(maxWidth: flexible ? .infinity : nil, alignment: .leading)
        }.buttonStyle(.plain)
    }

    private func row(_ j: JobRowData, _ pal: Palette) -> some View {
        HStack(spacing: 4) {
            Button { onSelectJob(j.id) } label: {
                Text(j.id).font(.system(size: 11)).foregroundStyle(pal.accent)
            }.buttonStyle(.plain).frame(width: 54, alignment: .leading)
            JobStateLabel(state: j.state, done: j.done, palette: pal).frame(width: 30, alignment: .leading)
            Text(j.partition.isEmpty ? "—" : j.partition).font(.system(size: 11)).foregroundStyle(pal.muted)
                .lineLimit(1).truncationMode(.tail).frame(maxWidth: .infinity, alignment: .leading)
            Text(j.time).font(.system(size: 11)).foregroundStyle(pal.muted).frame(width: 60, alignment: .leading)
            Text(fmtDate(j.submit)).font(.system(size: 11)).foregroundStyle(pal.muted).frame(width: 74, alignment: .leading)
        }
        .opacity(j.done ? 0.6 : 1)
        .singleLineTable()
        .padding(.horizontal, 4).padding(.vertical, 3)
    }
}

private extension ComparisonResult {
    var rawInt: Int { self == .orderedAscending ? -1 : self == .orderedDescending ? 1 : 0 }
}
