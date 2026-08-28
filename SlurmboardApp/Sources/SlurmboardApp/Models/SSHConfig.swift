import Foundation

/// A single `Host` entry parsed from an OpenSSH client config file.
///
/// We only surface information useful for picking a cluster to connect to.
/// The actual connection is made by handing the `alias` straight to the
/// system `ssh` binary, which re-reads `~/.ssh/config` itself — so any
/// `ProxyJump` / `IdentityFile` / `User` directives keep working without us
/// re-implementing them.
struct SSHHost: Identifiable, Hashable, Codable {
    /// The alias as written after `Host` (e.g. `triton`). Used as the
    /// argument to `ssh`.
    let alias: String
    /// `HostName` if present, shown for context in the picker.
    let hostName: String?
    /// `ProxyJump` chain if present, shown so the user can see a jump host
    /// (e.g. Roihu → Triton via Kosh) is involved.
    let proxyJump: String?
    /// Optional complete ssh invocation for app-created entries, e.g.
    /// `ssh -J jump user@login.example.org`. Imported config aliases leave
    /// this nil so OpenSSH continues to resolve ~/.ssh/config itself.
    let sshCommand: String?

    init(alias: String, hostName: String?, proxyJump: String?, sshCommand: String? = nil) {
        self.alias = alias
        self.hostName = hostName
        self.proxyJump = proxyJump
        self.sshCommand = sshCommand
    }

    var id: String { alias }

    /// A human-friendly one-line summary for the picker list.
    var subtitle: String {
        var parts: [String] = []
        if let hostName, hostName != alias {
            parts.append(hostName)
        }
        if let proxyJump {
            parts.append("via \(proxyJump)")
        }
        return parts.joined(separator: "  ·  ")
    }

    var connectionArguments: [String] {
        guard let sshCommand, !sshCommand.trimmingCharacters(in: .whitespaces).isEmpty else {
            return [alias]
        }
        var words = Self.shellWords(sshCommand)
        if words.first == "ssh" || words.first?.hasSuffix("/ssh") == true { words.removeFirst() }
        return words.isEmpty ? [alias] : words
    }

    private static func shellWords(_ value: String) -> [String] {
        var result: [String] = [], current = ""
        var quote: Character?, escaped = false
        for ch in value {
            if escaped { current.append(ch); escaped = false; continue }
            if ch == "\\" && quote != "'" { escaped = true; continue }
            if let q = quote {
                if ch == q { quote = nil } else { current.append(ch) }
            } else if ch == "\"" || ch == "'" {
                quote = ch
            } else if ch.isWhitespace {
                if !current.isEmpty { result.append(current); current = "" }
            } else { current.append(ch) }
        }
        if !current.isEmpty { result.append(current) }
        return result
    }
}

/// Minimal parser for `~/.ssh/config`.
///
/// Supports the subset we care about: `Host` blocks, `HostName`, `ProxyJump`
/// and `Include`. Wildcard-only host patterns (e.g. `Host *`) are skipped
/// because they are not concrete connect targets.
enum SSHConfigParser {

    static var defaultConfigURL: URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".ssh/config")
    }

    /// Parse the user's SSH config (following `Include` directives) and return
    /// the concrete, non-wildcard hosts in file order.
    static func loadHosts(from url: URL = defaultConfigURL) -> [SSHHost] {
        var seen = Set<String>()
        var hosts: [SSHHost] = []
        parse(url: url, seenFiles: &seen, into: &hosts)
        // De-duplicate by alias, keeping first occurrence.
        var byAlias = Set<String>()
        return hosts.filter { byAlias.insert($0.alias).inserted }
    }

    private static func parse(url: URL,
                              seenFiles: inout Set<String>,
                              into hosts: inout [SSHHost]) {
        let path = url.standardizedFileURL.path
        guard seenFiles.insert(path).inserted else { return }        // avoid Include loops
        guard let raw = try? String(contentsOf: url, encoding: .utf8) else { return }

        // Aliases in the current Host block, plus collected sub-values.
        var currentAliases: [String] = []
        var currentHostName: String?
        var currentProxyJump: String?

        func flush() {
            for alias in currentAliases where !alias.contains("*") && !alias.contains("?") && alias != "!" {
                hosts.append(SSHHost(alias: alias,
                                     hostName: currentHostName,
                                     proxyJump: currentProxyJump))
            }
            currentAliases = []
            currentHostName = nil
            currentProxyJump = nil
        }

        for line in raw.components(separatedBy: .newlines) {
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            if trimmed.isEmpty || trimmed.hasPrefix("#") { continue }

            // Split "Keyword rest-of-line". Keyword may be followed by '=' or
            // whitespace per ssh_config(5).
            let (keyword, value) = splitDirective(trimmed)
            switch keyword.lowercased() {
            case "host":
                flush()
                currentAliases = tokenize(value)
            case "hostname":
                if currentHostName == nil { currentHostName = value }
            case "proxyjump":
                if currentProxyJump == nil { currentProxyJump = value }
            case "include":
                // ssh expands these relative to ~/.ssh, supports globs.
                flush()
                for expanded in expandInclude(value) {
                    parse(url: expanded, seenFiles: &seenFiles, into: &hosts)
                }
            default:
                break
            }
        }
        flush()
    }

    /// Split a config line into (keyword, value), handling both
    /// `Key value` and `Key = value` forms.
    private static func splitDirective(_ line: String) -> (String, String) {
        if let eq = line.firstIndex(of: "=") {
            // Only treat '=' as a separator if it comes before any space,
            // i.e. `Key=value` or `Key = value`.
            let head = line[..<eq].trimmingCharacters(in: .whitespaces)
            if !head.contains(" ") && !head.contains("\t") {
                let tail = line[line.index(after: eq)...].trimmingCharacters(in: .whitespaces)
                return (head, tail)
            }
        }
        guard let spaceRange = line.rangeOfCharacter(from: .whitespaces) else {
            return (line, "")
        }
        let head = String(line[..<spaceRange.lowerBound])
        let tail = line[spaceRange.upperBound...].trimmingCharacters(in: .whitespaces)
        return (head, String(tail))
    }

    /// Split a value into whitespace-separated tokens, honouring simple
    /// double-quoted groups (`Host "my server" other`).
    private static func tokenize(_ value: String) -> [String] {
        var tokens: [String] = []
        var current = ""
        var inQuotes = false
        for ch in value {
            if ch == "\"" {
                inQuotes.toggle()
            } else if ch.isWhitespace && !inQuotes {
                if !current.isEmpty { tokens.append(current); current = "" }
            } else {
                current.append(ch)
            }
        }
        if !current.isEmpty { tokens.append(current) }
        return tokens
    }

    /// Resolve an `Include` value (space-separated, may contain `~` and globs)
    /// into concrete file URLs.
    private static func expandInclude(_ value: String) -> [URL] {
        let home = FileManager.default.homeDirectoryForCurrentUser
        let sshDir = home.appendingPathComponent(".ssh")
        var results: [URL] = []
        for token in tokenize(value) {
            var pattern = token
            if pattern.hasPrefix("~/") {
                pattern = home.appendingPathComponent(String(pattern.dropFirst(2))).path
            } else if !pattern.hasPrefix("/") {
                pattern = sshDir.appendingPathComponent(pattern).path
            }
            for match in glob(pattern) {
                results.append(URL(fileURLWithPath: match))
            }
        }
        return results
    }

    /// Thin wrapper over POSIX glob(3).
    private static func glob(_ pattern: String) -> [String] {
        var g = glob_t()
        defer { globfree(&g) }
        guard Darwin.glob(pattern, 0, nil, &g) == 0 else { return [] }
        var paths: [String] = []
        for i in 0..<Int(g.gl_pathc) {
            if let cStr = g.gl_pathv[i] {
                paths.append(String(cString: cStr))
            }
        }
        return paths
    }
}
