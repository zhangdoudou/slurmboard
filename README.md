# slurmboard

A lightweight, dependency-free web dashboard for Slurm clusters.

Deploy directly on the Slurm login node — no SSH tunneling, no extra packages, just Python 3 stdlib. Reload the page to get a fresh snapshot; no background polling.

## Features

- **Cluster summary** — nodes, CPUs, memory, GPUs (alloc vs idle)
- **GPU breakdown by type** — v100 / a100 / h100 / h200 / b300 …
- **Partition table** with:
  - Multi-column sort (click = primary, Shift+click = secondary) — e.g. sort by VRAM ↓ then idle GPUs ↓
  - Filter by minimum VRAM and/or "idle GPUs only"
  - Running / pending job counts per partition
  - Click any row to expand its nodes inline
- **Per-node detail** inside each partition — state, CPU, memory, GPU idle/total, VRAM, load

## Requirements

- Python ≥ 3.7 (stdlib only, no pip installs)
- Running on a node with `sinfo`, `scontrol`, `squeue` in `$PATH` (i.e. a Slurm login or submit node)

## Usage

```bash
# default: bind 0.0.0.0:8000
./slurmboard.py

# custom port / bind address
./slurmboard.py --port 9000 --host 127.0.0.1
```

Then open `http://<login-node>:8000` in your browser. Reload the page to refresh the data.

## How it works

Each page load shells out to:

```
scontrol -o show node   # per-node CPU / memory / GPU (gres) state
sinfo -h -o "%P|%a|%l"  # partition availability and time limits
squeue -h -o "%P|%T"    # running / pending job counts per partition
```

The results are parsed with regex, serialised as JSON, and embedded directly into the returned HTML. The frontend is vanilla JS — no framework, no build step.

## Typical workflow

1. You need N GPUs with at least X GB VRAM.
2. Open slurmboard and sort partitions by **VRAM ↓** then **GPU idle ↓**.
3. Check the **Jobs (run/pend)** column to gauge queue pressure.
4. Click the best partition to expand its nodes and pick the least loaded one.

## Inspiration

Motivated by [slurmmanager](https://github.com/paulgavrikov/slurmmanager); built to run without SSH access to compute nodes.
