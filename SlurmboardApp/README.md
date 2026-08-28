# Slurmboard.app — native macOS client

A fully native macOS front-end for [slurmboard](../README.md). It reproduces the
web dashboard's three-column layout **without** a browser, a web server, or port
forwarding: it runs the Slurm CLIs directly on the login node over SSH, parses
the output locally, and renders everything in SwiftUI.

## How it works

```
~/.ssh/config ──▶ SSHConfigParser ──▶ host list (picker)
                                          │ open dashboard
                                          ▼
   SSHRunner  ──  ssh (ControlMaster, one multiplexed connection per cluster)
                                          │  scontrol -o show node
                                          │  sinfo   -h -o "%P|%l"
                                          │  squeue  -h -o "%P|%i|…"
                                          │  squeue  -u USER …      (active queue)
                                          │  sacct   -u USER …      (history)
                                          ▼
   SlurmParser  ──▶  typed models  ──▶  SwiftUI dashboard (one window per cluster)
```

Nothing is installed or left running on the login node — only the standard
Slurm commands are invoked, exactly the ones `slurmboard.py` runs locally. The
parsing layer (`SlurmParser`) is a faithful port of the Python parser; its
output is byte-for-byte identical on the same raw command output.

`SSHRunner` keeps a single **multiplexed** SSH connection per cluster
(`ControlMaster`/`ControlPersist`), so the several commands each refresh needs
don't each pay a full SSH handshake. All `ProxyJump` / jump-host config in
`~/.ssh/config` is honoured because the connection is made by the system `ssh`.

## Features (parity with the web UI)

- **Cluster summary** — nodes / CPUs / memory / GPUs with green idle bars
- **GPUs by type** — sortable, expandable into a per-partition breakdown
- **Partition table** — filter by Min VRAM and "idle GPUs only"; multi-column
  sort (click = primary, ⇧-click = secondary); per-row and full refresh;
  running/pending counts; expand a row into its nodes, running, or pending jobs
- **My Jobs** — Active Queue + 7-day History, each sortable; draggable divider
- **Job detail** — a sheet mirroring the `/job/<id>` page
- **Light / dark theme** toggle (persisted)
- **Native draggable columns** via `HSplitView` / `VSplitView`
- **One window per cluster**, each an independent multiplexed connection

## Requirements

- macOS 14+
- Swift toolchain (Xcode or Command Line Tools — `xcode-select --install`)
- System OpenSSH (`/usr/bin/ssh`, ships with macOS)
- `~/.ssh/config` entries for your clusters; key loaded into `ssh-agent` if it
  has a passphrase (`ssh-add …`), and the host in `known_hosts`
- The login node must have `scontrol` / `sinfo` / `squeue` / `sacct` in `PATH`
  (i.e. it's a Slurm login/submit node). **No Python needed on the cluster.**

## Build & run

Double-clickable app bundle:

```bash
cd SlurmboardApp
./build_app.sh          # produces ./Slurmboard.app (ad-hoc signed)
open Slurmboard.app
```

Run from source during development:

```bash
cd SlurmboardApp
swift run
```

## Usage

1. Launch — the picker lists hosts from `~/.ssh/config`.
2. Select a cluster and click **Open dashboard** (or double-click a host).
3. A native window opens, runs the Slurm commands over SSH, and renders the
   board. Open as many clusters as you like — each gets its own window.
4. Use the ↻ buttons (header, partition rows, My Jobs sections) to refresh.
5. Close a window to drop that cluster's SSH connection; quitting closes all.

## Project layout

```
Sources/SlurmboardApp/
  SlurmboardApp.swift          @main App + lifecycle
  Models/
    SSHConfig.swift            ~/.ssh/config parser
    SlurmModels.swift          typed Slurm structures
  Services/
    SSHRunner.swift            multiplexed ssh command runner
    SlurmParser.swift          raw output → models (port of the Python parser)
    SlurmService.swift         per-cluster ObservableObject (fetch + publish)
    ConnectionManager.swift    registry of live services across windows
  Views/
    Theme.swift, Components.swift
    HostPickerView.swift
    ClusterWindowView.swift     header + 3 columns + overlays
    LeftColumnView.swift        summary + GPUs-by-type
    PartitionTableView.swift    filter/sort/expand
    PartitionRow.swift          row + node/job sub-tables
    MyJobsView.swift            active queue + history
    JobDetailView.swift         job detail sheet
```

## Notes / limitations

- `ssh` runs with `BatchMode=yes`: it never blocks on a prompt. Passphrase keys
  must be in `ssh-agent`; host keys must already be in `known_hosts`.
- Per-command latency is dominated by the login node's shell startup (e.g. a
  heavy `.bashrc` / conda init), not by SSH — multiplexing removes the handshake
  cost but not remote shell startup.
- The bundle is **ad-hoc signed** for local use. Distributing it elsewhere
  requires a Developer ID signature + notarization.
