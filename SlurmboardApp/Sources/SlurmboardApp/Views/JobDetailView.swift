import SwiftUI

/// Job detail sheet — the native equivalent of the `/job/<id>` page.
struct JobDetailView: View {
    let jobID: String
    @ObservedObject var service: SlurmService
    let onClose: () -> Void
    @EnvironmentObject var theme: Theme

    @State private var detail: JobDetail?
    @State private var error: String?
    @State private var loading = true

    var body: some View {
        let pal = theme.p
        VStack(alignment: .leading, spacing: 0) {
            HStack {
                Text("Job \(jobID)").font(.system(size: 18, weight: .bold)).foregroundStyle(pal.text)
                if let d = detail, let state = d.value("state") {
                    Pill(text: state, fg: statePillColor(state, pal))
                }
                Spacer()
                Button(action: onClose) {
                    Image(systemName: "xmark")
                        .font(.system(size: 12, weight: .bold))
                        .foregroundStyle(pal.text)
                        .frame(width: 28, height: 28)
                        .background(pal.panel)
                        .clipShape(Circle())
                        .overlay(Circle().stroke(pal.border))
                        .contentShape(Circle())
                }
                .buttonStyle(.plain)
                .keyboardShortcut(.cancelAction)
                .help("Close job details")
                .accessibilityLabel("Close job details")
            }
            .padding()
            Divider()

            if loading {
                ProgressView("Loading…").frame(maxWidth: .infinity, maxHeight: .infinity)
            } else if let error {
                VStack(spacing: 8) {
                    Image(systemName: "exclamationmark.triangle").font(.largeTitle).foregroundStyle(pal.bad)
                    Text(error).foregroundStyle(pal.bad).multilineTextAlignment(.center)
                }.frame(maxWidth: .infinity, maxHeight: .infinity).padding()
            } else if let d = detail {
                ScrollView { content(d, pal).padding() }
            }
        }
        .frame(width: 620, height: 560)
        .background(pal.bg)
        .task(id: jobID) {
            loading = true; error = nil; detail = nil
            switch await service.fetchJobDetail(jobID) {
            case .success(let d): detail = d
            case .failure(let e): error = e.message
            }
            loading = false
        }
    }

    @ViewBuilder
    private func content(_ d: JobDetail, _ pal: Palette) -> some View {
        if let name = d.value("job_name") {
            Text(name).font(.system(size: 14)).foregroundStyle(pal.muted).padding(.bottom, 4)
        }
        if let reason = d.value("reason"), reason != "None" {
            Text("Reason: \(reason)")
                .font(.system(size: 13)).foregroundStyle(pal.warn)
                .padding(8).frame(maxWidth: .infinity, alignment: .leading)
                .background(pal.warn.opacity(0.1))
                .clipShape(RoundedRectangle(cornerRadius: 6))
                .padding(.bottom, 8)
        }
        let gpu = d.gpuCount > 0 ? "\(d.gpuCount)× \(d.gpuType ?? "gpu")" : nil
        LazyVGrid(columns: [GridItem(.adaptive(minimum: 260), spacing: 14)], alignment: .leading, spacing: 14) {
            card("Identity", pal, [
                ("User", d.value("user")), ("Account", d.value("account")),
                ("QOS", d.value("qos")), ("Partition", d.value("partition")),
                ("Priority", d.value("priority")), ("Exit code", d.value("exit_code")),
            ])
            card("Resources", pal, [
                ("Nodes", d.value("num_nodes")), ("CPUs", d.value("num_cpus")),
                ("Tasks", d.value("num_tasks")), ("CPUs / task", d.value("cpus_task")),
                ("GPUs", gpu), ("Memory", d.value("mem_cpu") ?? d.value("mem_node")),
                ("TRES", d.value("tres")),
            ])
            card("Timing", pal, [
                ("Submit", d.value("submit_time")), ("Start", d.value("start_time")),
                ("End", d.value("end_time")), ("Run time", d.value("runtime")),
                ("Time limit", d.value("timelimit")),
            ])
            card("Nodes", pal, [
                ("Node list", d.value("nodelist")), ("Batch host", d.value("batch_host")),
            ])
        }
        card("Paths", pal, [
            ("Work dir", d.value("workdir")), ("Command", d.value("command")),
            ("Stdout", d.value("stdout")), ("Stderr", d.value("stderr")),
        ], mono: true).padding(.top, 14)
    }

    private func card(_ title: String, _ pal: Palette, _ rows: [(String, String?)], mono: Bool = false) -> some View {
        let present = rows.filter { $0.1 != nil }
        return Group {
            if present.isEmpty { EmptyView() } else {
                VStack(alignment: .leading, spacing: 6) {
                    Text(title).font(.system(size: 11, weight: .semibold)).textCase(.uppercase)
                        .foregroundStyle(pal.muted)
                    ForEach(present, id: \.0) { label, value in
                        HStack(alignment: .top, spacing: 8) {
                            Text(label).font(.system(size: 12)).foregroundStyle(pal.muted)
                                .frame(width: 90, alignment: .leading)
                            Text(value ?? "—")
                                .font(.system(size: 12, design: mono ? .monospaced : .default))
                                .foregroundStyle(mono ? pal.accent : pal.text)
                                .textSelection(.enabled)
                                .frame(maxWidth: .infinity, alignment: .leading)
                        }
                    }
                }
                .padding(14)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(pal.panel)
                .overlay(RoundedRectangle(cornerRadius: 10).stroke(pal.border))
                .clipShape(RoundedRectangle(cornerRadius: 10))
            }
        }
    }

    private func statePillColor(_ state: String, _ pal: Palette) -> Color {
        let s = state.lowercased()
        if s.contains("running") || s.contains("completing") { return pal.warn }
        if s.contains("pending") { return pal.accent }
        if s.contains("completed") { return pal.good }
        if s.contains("fail") || s.contains("cancel") || s.contains("timeout") { return pal.bad }
        return pal.muted
    }
}
