# Slurmboard.app — macOS client

Slurmboard.app is a native macOS shell for Slurmboard. Hosts, tabs, SFTP, and
terminal sessions use the existing SwiftUI app, while each cluster dashboard is
the bundled Python Slurmboard page displayed in a `WKWebView` through an SSH
tunnel.

The app maintains its own copy of [`slurmboard.py`](slurmboard.py). That file is
packaged into the app at build time and is updated manually with the app source;
the app never downloads dashboard code at runtime.

## How it works

When you open a dashboard, the app:

1. Reads the bundled `slurmboard.py`.
2. Starts the system `/usr/bin/ssh` client with a local port forward.
3. Streams the Python source over SSH standard input; it does not install or
   permanently upload a file to the login node.
4. Runs the streamed source with the remote `python3` interpreter, bound only to
   `127.0.0.1` on a temporary remote port.
5. Waits for the dashboard health endpoint and opens the forwarded local URL in
   a `WKWebView` tab.
6. Terminates the remote process and SSH tunnel when the dashboard is closed or
   the app quits.

```text
bundled slurmboard.py
        │ stdin
        ▼
macOS app ── /usr/bin/ssh ── login node: python3 -
        │                         │
        └── local port forward ◀──┘
                    │
                    ▼
                 WKWebView
```

## Features

- Host cards imported from `~/.ssh/config` or added manually.
- Manual hosts can be entered as a complete SSH command or as connection
  fields: hostname, username, port, identity file, `ProxyJump`, and extra SSH
  arguments.
- Host cards can be edited or removed; the add and edit screens share the same
  connection form.
- Optional SSH passwords are stored in the user's macOS Keychain, not in
  `hosts.json`, command arguments, logs, or the repository.
- One tab per Slurm dashboard, rendered by the bundled web UI.
- Native terminal and SFTP tabs.
- Dashboard lists initially render at most 100 rows and append more rows when
  scrolled to the bottom. There are no pagination or “load more” controls.
- Partition job counts, GPU allocation summaries, Active Queue, and seven-day
  History load automatically. Large node and job detail lists remain
  request-driven and incrementally rendered.
- Storage quota discovery is optional. If the cluster has no supported quota
  command, the Storage Quota section is hidden.

## Requirements

### Local Mac

- macOS 14 or later.
- Xcode Command Line Tools or Xcode with Swift 5.9 or later.
- System OpenSSH (`/usr/bin/ssh`, included with macOS).

Install the command-line developer tools if needed:

```bash
xcode-select --install
swift --version
```

### Remote cluster

- SSH access to a Slurm login or submit node.
- Python 3.7 or later available as `python3` or `python3.7`–`python3.13`.
- `sinfo`, `scontrol`, and `squeue` available in `PATH`.
- `sacct` is required for job history.
- Quota commands are optional.

The remote account does not need Slurmboard installed. The app streams its
bundled source for every dashboard connection.

## Build from source

Clone the macOS app branch and build a release app bundle:

```bash
git clone -b feature/macos-native-app \
  https://github.com/zhangdoudou/slurmboard.git
cd slurmboard/SlurmboardApp
./build_app.sh --release
open Slurmboard.app
```

`build_app.sh` performs a Swift release build, assembles
`Slurmboard.app/Contents`, embeds `slurmboard.py`, and applies an ad-hoc local
signature.

For a faster development build:

```bash
./build_app.sh --debug
open Slurmboard.app
```

You can also run the Swift package directly during development:

```bash
swift run
```

## Add or import a host

You can import concrete aliases from `~/.ssh/config`:

```ssh-config
Host lumi
    HostName lumi.csc.fi
    User my-user
    IdentityFile ~/.ssh/id_ed25519
    ProxyJump my-bastion
```

Wildcard-only entries such as `Host *` are defaults and are not shown as host
cards.

Alternatively, click **Add Host** and choose one of these input methods:

- **SSH Command** — for example, `ssh -J user@jump.example.org user@login.example.org`.
- **Connection Fields** — enter the host, user, port, identity file,
  `ProxyJump`, extra arguments, and optional password separately.

When editing a host, leaving the password field empty preserves the saved
Keychain password. Use the clear-password control to delete it.

Before connecting for the first time, accept the remote host key in Terminal if
your SSH policy does not allow an interactive host-key prompt inside the app:

```bash
ssh lumi
```

## Local data and security

- Host definitions are stored in
  `~/Library/Application Support/Slurmboard/hosts.json`.
- Passwords are stored as macOS Keychain generic-password items.
- For password-authenticated dashboard SSH, a permission-restricted temporary
  askpass helper is created and removed after authentication.
- Private keys and passwords are not added to the repository or app bundle.
- The dashboard HTTP server listens on remote loopback, and its local forwarded
  endpoint listens on `127.0.0.1`.

## Project layout

```text
SlurmboardApp/
  Package.swift
  Info.plist
  build_app.sh
  slurmboard.py                 embedded dashboard backend and web UI
  Sources/SlurmboardApp/
    SlurmboardApp.swift
    Models/SSHConfig.swift
    Services/
      ConnectionManager.swift
      CredentialStore.swift     macOS Keychain integration
      DashboardService.swift    SSH streaming, tunnel, and lifecycle
    Views/
      HostPickerView.swift      host cards and shared add/edit form
      ClusterWindowView.swift   WKWebView dashboard tab
      TerminalView.swift
      SFTPView.swift
```

Some earlier native dashboard models and views remain in the source tree, but
the current Slurm dashboard path uses `DashboardService` and `WKWebView`.

## Troubleshooting

### `swift build` reports an SDK/compiler mismatch

Reinstall or select a matching Xcode/Command Line Tools installation, then
confirm the active toolchain:

```bash
xcode-select -p
swift --version
```

### The dashboard cannot connect

First test the same host with the system SSH client:

```bash
ssh <host-alias>
```

Then verify `python3`, `sinfo`, `scontrol`, and `squeue` are available on the
login node. SSH options from the saved host, including identity files and
`ProxyJump`, are passed to `/usr/bin/ssh`.

### Storage Quota is absent

This is expected when the cluster does not expose a supported quota command.
The rest of the dashboard remains available.
