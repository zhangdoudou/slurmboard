import SwiftUI
import WebKit

struct ClusterWindowView: View {
    @EnvironmentObject private var manager: ConnectionManager
    let connectionID: UUID

    var body: some View {
        Group {
            if let service = manager.service(for: connectionID) {
                DashboardWorkspace(service: service)
            } else {
                ContentUnavailableView(
                    "Connection closed",
                    systemImage: "bolt.slash",
                    description: Text("This cluster connection is no longer active.")
                )
            }
        }
        .frame(minWidth: 1420, minHeight: 680)
    }
}

private struct DashboardWorkspace: View {
    @ObservedObject var service: DashboardService

    var body: some View {
        ZStack {
            if let url = service.dashboardURL {
                DashboardWebView(url: url)
            } else {
                switch service.state {
                case .connecting:
                    statusView(title: "Connecting…",
                               detail: "Starting the remote dashboard on \(service.host.alias)…",
                               error: false)
                case .failed(let message):
                    statusView(title: "Connection failed", detail: message, error: true)
                case .disconnected:
                    statusView(title: "Disconnected", detail: nil, error: false)
                case .connected:
                    statusView(title: "Loading dashboard…", detail: nil, error: false)
                }
            }
        }
    }

    private func statusView(title: String, detail: String?, error: Bool) -> some View {
        VStack(spacing: 12) {
            if error {
                Image(systemName: "exclamationmark.triangle.fill")
                    .font(.largeTitle).foregroundStyle(.red)
            } else {
                ProgressView()
            }
            Text(title).font(.headline)
            if let detail {
                Text(detail).font(.callout).foregroundStyle(.secondary)
                    .multilineTextAlignment(.center).textSelection(.enabled)
            }
            if error || service.state == .disconnected {
                Button("Retry") { service.retry() }.buttonStyle(.borderedProminent)
            }
        }
        .padding(32).frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

private struct DashboardWebView: NSViewRepresentable {
    let url: URL

    final class Coordinator { var loadedURL: URL? }

    func makeCoordinator() -> Coordinator { Coordinator() }

    func makeNSView(context: Context) -> WKWebView {
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .nonPersistent()
        let view = WKWebView(frame: .zero, configuration: configuration)
        view.allowsMagnification = true
        load(in: view, coordinator: context.coordinator)
        return view
    }

    func updateNSView(_ view: WKWebView, context: Context) {
        load(in: view, coordinator: context.coordinator)
    }

    private func load(in view: WKWebView, coordinator: Coordinator) {
        guard coordinator.loadedURL != url else { return }
        coordinator.loadedURL = url
        view.load(URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData))
    }
}
