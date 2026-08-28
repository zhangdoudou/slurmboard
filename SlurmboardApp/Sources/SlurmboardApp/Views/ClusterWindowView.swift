import SwiftUI
import AppKit

private final class InitialRatioSplitView: NSSplitView {
    private var positioned = false
    var dashboardDividerColor = NSColor(calibratedWhite: 0.22, alpha: 1) {
        didSet { needsDisplay = true }
    }

    override var dividerColor: NSColor { dashboardDividerColor }
    override var dividerThickness: CGFloat { 2 }

    override func layout() {
        super.layout()
        guard !positioned, bounds.width > 0, arrangedSubviews.count == 3 else { return }
        positioned = true
        setPosition(bounds.width * 0.20, ofDividerAt: 0)
        setPosition(bounds.width * 0.80, ofDividerAt: 1)
    }
}

private struct AdjustableThreeColumnSplit: NSViewRepresentable {
    let service: SlurmService
    let theme: Theme
    let onSelectJob: (String) -> Void

    final class Coordinator {
        var serviceID: UUID?
    }

    func makeCoordinator() -> Coordinator { Coordinator() }

    private func paneRoots() -> [AnyView] {
        [
            AnyView(DashboardLeftPane(service: service)
                .environmentObject(theme)
                .textSelection(.enabled)
                .frame(minWidth: 260, maxWidth: .infinity, maxHeight: .infinity)),
            AnyView(PartitionTableView(service: service, onSelectJob: onSelectJob)
                .environmentObject(theme)
                .textSelection(.enabled)
                .frame(minWidth: 600, maxWidth: .infinity, maxHeight: .infinity)),
            AnyView(MyJobsView(service: service, onSelectJob: onSelectJob)
                .environmentObject(theme)
                .textSelection(.enabled)
                .frame(minWidth: 260, maxWidth: .infinity, maxHeight: .infinity)),
        ]
    }

    func makeNSView(context: Context) -> NSSplitView {
        let split = InitialRatioSplitView()
        split.isVertical = true
        split.dividerStyle = .thin
        split.dashboardDividerColor = theme.isLight
            ? NSColor(calibratedWhite: 0.82, alpha: 1)
            : NSColor(calibratedWhite: 0.22, alpha: 1)
        for root in paneRoots() {
            split.addArrangedSubview(NSHostingView(rootView: root))
        }
        context.coordinator.serviceID = service.id
        split.setHoldingPriority(.defaultHigh, forSubviewAt: 0)
        split.setHoldingPriority(.defaultLow, forSubviewAt: 1)
        split.setHoldingPriority(.defaultHigh, forSubviewAt: 2)
        return split
    }

    func updateNSView(_ split: NSSplitView, context: Context) {
        if let split = split as? InitialRatioSplitView {
            split.dashboardDividerColor = theme.isLight
                ? NSColor(calibratedWhite: 0.82, alpha: 1)
                : NSColor(calibratedWhite: 0.22, alpha: 1)
        }
        // Each hosted pane observes SlurmService and Theme itself. Keeping the
        // hosting roots stable lets SwiftUI diff only the changed rows instead
        // of replacing all three pane trees for every published value.
        // A different dashboard tab can reuse this representable, however; in
        // that case rebind the roots once to the newly selected service.
        guard context.coordinator.serviceID != service.id,
              split.arrangedSubviews.count == 3 else { return }
        let roots = paneRoots()
        for (index, root) in roots.enumerated() {
            (split.arrangedSubviews[index] as? NSHostingView<AnyView>)?.rootView = root
        }
        context.coordinator.serviceID = service.id
    }
}

private struct DashboardLeftPane: View {
    @ObservedObject var service: SlurmService

    var body: some View {
        LeftColumnView(summary: service.snapshot.summary)
    }
}

/// One cluster tab: header + three draggable columns (summary / partitions /
/// my jobs), backed by a live `SlurmService`. Data is fetched natively over SSH
/// — nothing runs on the login node but the Slurm CLIs.
struct ClusterWindowView: View {
    @EnvironmentObject private var manager: ConnectionManager
    @StateObject private var theme = Theme()
    let connectionID: UUID

    @State private var selectedJob: String?

    var body: some View {
        Group {
            if let svc = manager.service(for: connectionID) {
                ClusterContent(service: svc, selectedJob: $selectedJob)
            } else {
                VStack(spacing: 8) {
                    Image(systemName: "bolt.slash").font(.largeTitle).foregroundStyle(.secondary)
                    Text("This connection is no longer active.").foregroundStyle(.secondary)
                }.frame(minWidth: 500, minHeight: 320)
            }
        }
        .environmentObject(theme)
    }
}

private struct ClusterContent: View {
    @ObservedObject var service: SlurmService
    @Binding var selectedJob: String?
    @EnvironmentObject var theme: Theme

    var body: some View {
        let pal = theme.p
        VStack(spacing: 0) {
            header(pal)
            Divider()
            content(pal)
            Divider()
            footer(pal)
        }
        // Keep the same information density as the browser dashboard.  The
        // individual pane minima below add up to this value; allowing a
        // smaller window makes HSplitView silently compress fixed-width table
        // columns until labels overlap and values wrap vertically.
        .frame(minWidth: 1420, minHeight: 680)
        .background(pal.bg)
        .foregroundStyle(pal.text)
        // Style only scrollbars; the dashboard theme must not recolour the
        // macOS title bar or the workspace tabs.
        .background(ScrollBarTheme(isLight: theme.isLight).frame(width: 0, height: 0))
        .textSelection(.enabled)
        .navigationTitle("")
        .sheet(item: Binding(get: { selectedJob.map { JobID(id: $0) } },
                             set: { selectedJob = $0?.id })) { jid in
            JobDetailView(jobID: jid.id, service: service,
                          onClose: { selectedJob = nil })
                .environmentObject(theme)
        }
    }

    private func header(_ pal: Palette) -> some View {
        HStack(spacing: 14) {
            Text("⚙ Slurm Dashboard").font(.system(size: 18, weight: .semibold))
            Text(service.host.alias).font(.system(size: 13)).foregroundStyle(pal.accent)
            connectionBadge(pal)
            if !service.snapshot.generatedAt.isEmpty {
                Text("snapshot \(service.snapshot.generatedAt)")
                    .font(.system(size: 12)).foregroundStyle(pal.muted)
            }
            Spacer()
            Button { theme.isLight.toggle() } label: {
                Text(theme.isLight ? "🌙" : "☀️")
            }.buttonStyle(.borderless).help("Toggle light/dark")
            Button { service.refreshCluster() } label: {
                Label("Refresh", systemImage: "arrow.clockwise")
            }
            .buttonStyle(.borderedProminent)
        }
        .padding(.horizontal, 20).padding(.vertical, 12)
    }

    @ViewBuilder
    private func connectionBadge(_ pal: Palette) -> some View {
        switch service.state {
        case .connecting:
            HStack(spacing: 5) { ProgressView().controlSize(.small).scaleEffect(0.6)
                Text("Connecting…").font(.system(size: 12)).foregroundStyle(pal.muted) }
        case .connected:
            EmptyView()
        case .failed:
            HStack(spacing: 5) { Circle().fill(pal.bad).frame(width: 8, height: 8)
                Text("Connection error").font(.system(size: 12)).foregroundStyle(pal.bad) }
        case .disconnected:
            HStack(spacing: 5) { Circle().fill(pal.muted).frame(width: 8, height: 8)
                Text("Disconnected").font(.system(size: 12)).foregroundStyle(pal.muted) }
        }
    }

    @ViewBuilder
    private func content(_ pal: Palette) -> some View {
        ZStack {
            AdjustableThreeColumnSplit(
                service: service,
                theme: theme,
                onSelectJob: { selectedJob = $0 }
            )
            .padding(.top, 12)

            if case .connecting = service.state, service.snapshot.generatedAt.isEmpty {
                overlay(icon: nil, tint: pal.accent, title: "Connecting…",
                        detail: "Running Slurm commands on \(service.host.alias) over SSH…",
                        action: nil, pal: pal)
            } else if case .failed(let msg) = service.state {
                overlay(icon: "exclamationmark.triangle.fill", tint: pal.bad,
                        title: "Connection failed", detail: msg,
                        action: ("Retry", { service.retry() }), pal: pal)
            }
        }
    }

    private func footer(_ pal: Palette) -> some View {
        Text("slurmboard · data sourced live from sinfo / scontrol on \(service.host.alias) · reload to refresh")
            .font(.system(size: 11)).foregroundStyle(pal.muted)
            .frame(maxWidth: .infinity).padding(.vertical, 6)
    }

    private func overlay(icon: String?, tint: Color, title: String, detail: String?,
                         action: (String, () -> Void)?, pal: Palette) -> some View {
        VStack(spacing: 12) {
            if let icon { Image(systemName: icon).font(.largeTitle).foregroundStyle(tint) }
            else { ProgressView() }
            Text(title).font(.headline)
            if let detail {
                Text(detail).font(.callout).foregroundStyle(pal.muted).multilineTextAlignment(.center)
            }
            if let action { Button(action.0, action: action.1).buttonStyle(.borderedProminent) }
        }
        .padding(32).frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(.regularMaterial)
    }
}

/// Identifiable wrapper so a job id can drive `.sheet(item:)`.
private struct JobID: Identifiable { let id: String }
