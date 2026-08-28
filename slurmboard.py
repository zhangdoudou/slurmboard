#!/bin/sh
''''exec $(command -v python3.13 || command -v python3.12 || command -v python3.11 || command -v python3.10 || command -v python3.9 || command -v python3.8 || command -v python3.7 || echo python3) -- "$0" "$@" # '''
"""
slurmboard - a tiny, dependency-free web dashboard for a Slurm cluster.

Run directly on the Slurm login/submit node or access it through SSH forwarding.
The initial page uses a compact `sinfo` partition summary. Smaller clusters get
cached resource aggregates and queue counts automatically; larger systems keep
exact node and queue details request-driven. This makes the dashboard suitable
for both small clusters and machines with thousands of nodes. There is no
server-side background polling and no third-party dependency - stdlib only.

Usage:
    python3 slurmboard.py [--port PORT] [--host 0.0.0.0]
"""

import sys
if sys.version_info < (3, 7):
    sys.exit(f"slurmboard requires Python 3.7+, got {sys.version}")

import argparse
import errno
import glob
import html as _html
import json
import logging
import os
import random
import re
import secrets
import shlex
import shutil
import socket
import subprocess
import tempfile
import getpass
import threading
import time
import webbrowser
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

log = logging.getLogger("slurmboard")

_SLURM_QUERY_LOCK = threading.RLock()
_CACHE_LOCK = threading.RLock()
_CACHE = {}
_PARTITION_NODELISTS = {}
_QUOTA_COMMAND = None
_NODE_ENRICH_LIMIT = 1000
_NODE_ENRICH_TTL = 60
_PARTITION_JOBS_TTL = 20

# ---------------------------------------------------------------------------
# Data collection
# ---------------------------------------------------------------------------

_FIELD_RE = {
    "name":       re.compile(r"NodeName=(\S+)"),
    "state":      re.compile(r"\bState=(\S+)"),
    "cpu_alloc":  re.compile(r"CPUAlloc=(\d+)"),
    "cpu_total":  re.compile(r"CPUTot=(\d+)"),
    "load":       re.compile(r"CPULoad=(\S+)"),
    "gres":       re.compile(r"\bGres=(\S+)"),
    "partitions": re.compile(r"Partitions=(\S+)"),
    "real_mem":   re.compile(r"RealMemory=(\d+)"),
    "alloc_mem":  re.compile(r"AllocMem=(\d+)"),
    "cfg_tres":   re.compile(r"CfgTRES=(\S+)"),
    "alloc_tres": re.compile(r"AllocTRES=(\S+)"),
}

_GRES_GPU_RE        = re.compile(r"gpu:([a-zA-Z0-9_]+):(\d+)")
_GRES_GPU_PLAIN_RE  = re.compile(r"gpu:(\d+)")
_GRES_VRAM_RE       = re.compile(r"min-vram:no_consume:(\d+)([GM])")
_TRES_GPU_RE        = re.compile(r"gres/gpu=(\d+)")
_TRES_GPU_TYPED_RE  = re.compile(r"gres/gpu:([a-zA-Z0-9_]+)=(\d+)")


def _run(cmd, timeout=45):
    log.debug("slurm: %s", " ".join(cmd))
    t0 = time.monotonic()
    # Slurm commands talk to shared controller services. Serializing them keeps
    # simultaneous browser requests from multiplying controller load.
    with _SLURM_QUERY_LOCK:
        out = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, check=True, timeout=timeout,
        )
    log.debug("slurm: %s done in %.2fs", cmd[0], time.monotonic() - t0)
    return out.stdout


def _cached(key, ttl, loader, refresh=False):
    """Return a short-lived cached value, deduplicating concurrent rebuilds."""
    now = time.monotonic()
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if not refresh and cached and now - cached[0] < ttl:
            return cached[1]
        # Keep the cache lock while loading. Cached datasets are few and this
        # intentionally prevents a click burst from creating duplicate RPCs.
        value = loader()
        _CACHE[key] = (time.monotonic(), value)
        return value


_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_SCALED_AMOUNT_RE = re.compile(
    r"^([0-9]+(?:[.,][0-9]+)?)([KMGTPE]?)(?:I?B)?$", re.IGNORECASE
)
_LUMI_QUOTA_ROW_RE = re.compile(
    r"^\s*(/\S+)\s+"
    r"([0-9][0-9.,]*\s*[KMGTPE]?(?:i?B)?)\s*/\s*"
    r"([0-9][0-9.,]*\s*[KMGTPE]?(?:i?B)?)\s+"
    r"([0-9][0-9.,]*\s*[KMGTPE]?)\s*/\s*"
    r"([0-9][0-9.,]*\s*[KMGTPE]?)\s*$",
    re.IGNORECASE,
)


def _scaled_amount(value, binary):
    """Parse quota values such as ``1.7G``, ``43K``, or ``158M``."""
    compact = str(value).strip().replace(" ", "").replace("\u00a0", "")
    if compact in ("", "-", "none", "unlimited"):
        return None
    match = _SCALED_AMOUNT_RE.fullmatch(compact)
    if not match:
        return None
    number = float(match.group(1).replace(",", "."))
    exponent = "KMGTPE".find(match.group(2).upper()) + 1 if match.group(2) else 0
    return int(number * ((1024 if binary else 1000) ** exponent))


def _quota_row(scope, path, space_used_label, space_limit_label,
               files_used_label, files_limit_label):
    space_used = _scaled_amount(space_used_label, binary=True)
    space_limit = _scaled_amount(space_limit_label, binary=True)
    files_used = _scaled_amount(files_used_label, binary=False)
    files_limit = _scaled_amount(files_limit_label, binary=False)
    return {
        "scope": scope or "User",
        "path": path,
        "space_used": space_used,
        "space_limit": space_limit if space_limit else None,
        "space_used_label": space_used_label,
        "space_limit_label": (space_limit_label if space_limit else
                              "unlimited" if str(space_limit_label).lower() == "unlimited" else "—"),
        "space_percent": round(space_used / space_limit * 100, 1)
        if space_used is not None and space_limit else None,
        "files_used": files_used,
        "files_limit": files_limit if files_limit else None,
        "files_used_label": files_used_label,
        "files_limit_label": (files_limit_label if files_limit else
                              "unlimited" if str(files_limit_label).lower() == "unlimited" else "—"),
        "files_percent": round(files_used / files_limit * 100, 1)
        if files_used is not None and files_limit else None,
    }


def parse_lumi_quota(text):
    """Parse the compact path/space/files rows printed by LUMI quota tools."""
    rows = []
    scope = "User"
    clean = _ANSI_ESCAPE_RE.sub("", text)
    for raw_line in clean.splitlines():
        line = raw_line.strip()
        if line.lower().startswith("personal home"):
            scope = "Personal home"
            continue
        if line.lower().startswith("project:"):
            scope = line.split(":", 1)[1].strip() or "Project"
            continue
        match = _LUMI_QUOTA_ROW_RE.match(raw_line)
        if match:
            rows.append(_quota_row(scope, *match.groups()))
    return rows


def parse_posix_quota(text):
    """Parse Triton's user/group ``quota`` table, including inode limits."""
    rows = []
    section = "User"
    clean = _ANSI_ESCAPE_RE.sub("", text)
    for raw_line in clean.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.lower().startswith("group quotas"):
            section = "Group"
            continue
        fields = line.split()
        if not fields or not fields[0].startswith("/"):
            continue

        amount_index = None
        for index, field in enumerate(fields[1:], 1):
            if _scaled_amount(field, binary=True) is not None:
                amount_index = index
                break
        if amount_index is None:
            continue
        values = fields[amount_index:]
        if len(values) < 6:
            continue

        space_used_label, space_quota_label, space_hard_label = values[:3]
        position = 3
        if position < len(values) and _scaled_amount(values[position], binary=False) is None:
            position += 1  # optional grace-period column
        if len(values) < position + 3:
            continue
        files_used_label, files_quota_label, files_hard_label = values[position:position + 3]

        space_limit_label = (space_quota_label
                             if _scaled_amount(space_quota_label, True)
                             else space_hard_label)
        files_limit_label = (files_quota_label
                             if _scaled_amount(files_quota_label, False)
                             else files_hard_label)
        qualifier = " ".join(fields[1:amount_index])
        scope = f"{section}: {qualifier}" if qualifier else section
        rows.append(_quota_row(
            scope, fields[0], space_used_label, space_limit_label,
            files_used_label, files_limit_label,
        ))
    return rows


def parse_gpfs_quota(text):
    """Parse IBM Storage Scale/GPFS ``mmlsquota`` block and file limits."""
    rows = []
    clean = _ANSI_ESCAPE_RE.sub("", text)
    for raw_line in clean.splitlines():
        if "|" not in raw_line:
            continue
        block_side, file_side = raw_line.split("|", 1)
        blocks = block_side.split()
        files = file_side.split()
        type_index = next(
            (index for index, value in enumerate(blocks)
             if value.upper() in ("USR", "GRP", "FILESET")),
            None,
        )
        if type_index is None or len(blocks) < type_index + 4 or len(files) < 3:
            continue
        prefix = blocks[:type_index]
        if not prefix:
            continue
        filesystem = prefix[0]
        fileset = prefix[1] if len(prefix) > 1 else ""
        quota_type = blocks[type_index].upper()
        space_used_label, space_quota_label, space_hard_label = blocks[type_index + 1:type_index + 4]
        files_used_label, files_quota_label, files_hard_label = files[:3]
        if (_scaled_amount(space_used_label, True) is None or
                _scaled_amount(files_used_label, False) is None):
            continue

        # Plain numeric mmlsquota block values are KiB; --block-size auto adds units.
        def block_label(value):
            return value if re.search(r"[A-Za-z]", value) else value + "K"

        space_used_label = block_label(space_used_label)
        space_quota_label = block_label(space_quota_label)
        space_hard_label = block_label(space_hard_label)
        space_limit_label = (space_quota_label
                             if _scaled_amount(space_quota_label, True)
                             else space_hard_label)
        files_limit_label = (files_quota_label
                             if _scaled_amount(files_quota_label, False)
                             else files_hard_label)
        path = filesystem + (":" + fileset if fileset else "")
        scope = {"USR": "User", "GRP": "Group", "FILESET": "Fileset"}[quota_type]
        rows.append(_quota_row(
            scope, path, space_used_label, space_limit_label,
            files_used_label, files_limit_label,
        ))
    return rows


def parse_beegfs_quota(text):
    """Parse BeeGFS quota tables from storage pools (BeeGFS 7 and 8)."""
    rows = []
    pool = "BeeGFS"
    clean = _ANSI_ESCAPE_RE.sub("", text)
    for raw_line in clean.splitlines():
        line = raw_line.strip()
        pool_match = re.search(r"storage pool\s+(.+?)(?:\s*\(ID:|:|$)", line, re.IGNORECASE)
        if pool_match:
            pool = pool_match.group(1).strip()
            continue
        fields = [field.strip() for field in line.split("|")]
        if len(fields) < 8 or fields[2] != "" or fields[5] != "":
            continue
        if _scaled_amount(fields[3], True) is None:
            continue
        identity = fields[0] or fields[1] or "User"
        rows.append(_quota_row(
            f"User: {identity}", f"BeeGFS pool {pool}",
            fields[3], fields[4], fields[6], fields[7],
        ))
    return rows


def parse_quota_output(text):
    """Try all known structured formats; callers retain raw output as fallback."""
    for parser in (parse_lumi_quota, parse_beegfs_quota,
                   parse_gpfs_quota, parse_posix_quota):
        rows = parser(text)
        if rows:
            return rows
    return []


def _run_quota(command):
    completed = subprocess.run(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, check=True, timeout=20,
    )
    return completed.stdout


def _quota_candidates():
    """Return safe, read-only quota commands available on this cluster."""
    if _QUOTA_COMMAND:
        command = shlex.split(_QUOTA_COMMAND)
        if not command:
            raise ValueError("--quota-command must contain a command")
        return [("custom", command)]

    candidates = []
    commands = [
        ("lumi-workspaces", ["lumi-workspaces"]),
        ("lumi-quota", ["lumi-quota"]),
        ("myquota", ["myquota"]),
        ("showquota", ["showquota"]),
        ("checkquota", ["checkquota"]),
        ("quota", ["quota"]),
        ("mmlsquota", ["mmlsquota", "--block-size", "auto"]),
        ("beegfs", ["beegfs", "quota", "list-usage", "--type=user",
                    f"--ids={os.getuid()}"]),
        ("beegfs-ctl", ["beegfs-ctl", "--getquota", "--uid", str(os.getuid())]),
        ("lfs", ["lfs", "quota"]),
    ]
    for source, command in commands:
        if shutil.which(command[0]):
            candidates.append((source, command))
    return candidates


def collect_storage_quota():
    """Collect quotas from common HPC filesystems, with raw fallback."""
    try:
        candidates = _quota_candidates()
    except ValueError as exc:
        return {
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "source": "custom", "rows": [], "raw": None, "error": str(exc),
        }
    if not candidates:
        return {
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "source": None, "rows": [], "raw": None,
            "error": ("No supported quota command was found. Configure this cluster with "
                      "--quota-command if it provides a site-specific quota tool."),
        }

    first_raw = None
    failures = []
    for source, command in candidates:
        try:
            output = _run_quota(command)
        except subprocess.TimeoutExpired:
            failures.append(f"{source} timed out")
            if _QUOTA_COMMAND:
                break
            continue
        except subprocess.CalledProcessError as exc:
            details = (exc.stderr or "").strip()
            failures.append(details or f"{source} exited with status {exc.returncode}")
            if _QUOTA_COMMAND:
                break
            continue

        output = output.strip()
        if not output:
            continue
        rows = parse_quota_output(output)
        if rows:
            return {
                "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "source": source, "rows": rows, "raw": None, "error": None,
            }
        if first_raw is None:
            first_raw = (source, output[:100000])

    if first_raw:
        return {
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "source": first_raw[0], "rows": [], "raw": first_raw[1], "error": None,
        }
    return {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": candidates[0][0], "rows": [], "raw": None,
        "error": "; ".join(failures[:3]) or "Quota commands returned no data.",
    }


def build_storage_quota(refresh=False):
    return _cached("storage-quota", 300, collect_storage_quota, refresh=refresh)


def _gpu_total_from_gres(gres):
    if not gres or gres == "(null)":
        return None, 0
    m = _GRES_GPU_RE.search(gres)
    if m:
        return m.group(1), int(m.group(2))
    m = _GRES_GPU_PLAIN_RE.search(gres)
    if m:
        return None, int(m.group(1))
    return None, 0


def _gpu_alloc_from_tres(tres, gpu_type):
    if not tres:
        return 0
    if gpu_type:
        for t, c in _TRES_GPU_TYPED_RE.findall(tres):
            if t == gpu_type:
                return int(c)
    m = _TRES_GPU_RE.search(tres)
    return int(m.group(1)) if m else 0


_GPU_VRAM_GB = {
    "a100":  80,
    "a100-80":  80,
    "a100-40":  40,
    "a40":   48,
    "a30":   24,
    "a10":   24,
    "v100":  32,
    "v100-32": 32,
    "v100-16": 16,
    "mi300x": 192,
    "mi300a": 128,
    "mi250x": 128,
    "mi250":  128,
    "mi210":  64,
    "mi100":  32,
    "h100":  80,
    "h200": 141,
    "gh200": 96,
    "l40":   48,
    "l40s":  48,
}

def _vram_gb_from_gres(gres):
    if not gres:
        return None
    # prefer explicit min-vram annotation if present
    m = _GRES_VRAM_RE.search(gres)
    if m:
        val, unit = int(m.group(1)), m.group(2)
        return val if unit == "G" else val // 1024
    # fall back to GPU model lookup
    m = _GRES_GPU_RE.search(gres)
    if m:
        model = m.group(1).lower()
        if model in _GPU_VRAM_GB:
            return _GPU_VRAM_GB[model]
        # try prefix match (e.g. "mi250x" matches "mi250")
        for key, vram in _GPU_VRAM_GB.items():
            if model.startswith(key) or key.startswith(model):
                return vram
    return None


def collect_nodes(nodelist=None):
    cmd = ["scontrol", "-o", "show", "node"]
    if nodelist:
        cmd.append(nodelist)
    text = _run(cmd)
    nodes = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("NodeName="):
            continue
        vals = {}
        for key, rx in _FIELD_RE.items():
            m = rx.search(line)
            vals[key] = m.group(1) if m else None

        gres = vals["gres"]
        gpu_type, gpu_total = _gpu_total_from_gres(gres)
        gpu_alloc = _gpu_alloc_from_tres(vals["alloc_tres"], gpu_type)
        cfg_gpu_total = _gpu_alloc_from_tres(vals["cfg_tres"], gpu_type)
        if cfg_gpu_total:
            gpu_total = cfg_gpu_total

        cpu_alloc = int(vals["cpu_alloc"] or 0)
        cpu_total = int(vals["cpu_total"] or 0)
        real_mem  = int(vals["real_mem"]  or 0)
        alloc_mem = int(vals["alloc_mem"] or 0)
        partitions = vals["partitions"].split(",") if vals["partitions"] else []

        nodes.append({
            "name":        vals["name"],
            "state":       vals["state"] or "UNKNOWN",
            "partitions":  partitions,
            "cpu_alloc":   cpu_alloc,
            "cpu_idle":    max(cpu_total - cpu_alloc, 0),
            "cpu_total":   cpu_total,
            "load":        float(vals["load"]) if vals["load"] not in (None, "N/A") else None,
            "mem_alloc_mb": alloc_mem,
            "mem_total_mb": real_mem,
            "gpu_type":    gpu_type,
            "gpu_alloc":   gpu_alloc,
            "gpu_idle":    max(gpu_total - gpu_alloc, 0),
            "gpu_total":   gpu_total,
            "gpu_vram_gb": _vram_gb_from_gres(gres),
        })
    return nodes


def collect_partition_limits():
    """Return {partition: timelimit_str} from sinfo."""
    text = _run(["sinfo", "-h", "-o", "%P|%l"])
    limits = {}
    for line in text.splitlines():
        line = line.strip()
        if "|" not in line:
            continue
        part, tl = line.split("|", 1)
        limits[part.rstrip("*")] = tl
    return limits


def collect_job_counts(partition=None):
    """Return {partition: {running, pending, timelimit, jobs: [...]}} from squeue."""
    # %P partition  %i jobid  %u user  %j name  %T state
    # %M elapsed  %C cpus  %b gres  %R reason  %l timelimit  %V submit time
    cmd = ["squeue", "-h", "-o", "%P|%i|%u|%j|%T|%M|%C|%b|%R|%l|%V"]
    if partition:
        cmd[1:1] = ["-p", partition]
    text = _run(cmd)
    counts = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("|", 10)
        if len(parts) < 10:
            continue
        part, jid, user, name, state, time_used, cpus, gres, reason, timelimit = parts[:10]
        submit = parts[10] if len(parts) > 10 else None
        state_up = state.upper()
        c = counts.setdefault(part, {"running": 0, "pending": 0, "timelimit": None, "jobs": []})
        if state_up == "RUNNING":
            c["running"] += 1
        elif state_up == "PENDING":
            c["pending"] += 1
        if timelimit and timelimit not in ("", "INVALID", "NOT_SET"):
            c["timelimit"] = timelimit  # last seen; partition jobs share the same limit
        c["jobs"].append({
            "id":        jid,
            "user":      user,
            "name":      name,
            "state":     state_up,
            "time":      time_used,
            "cpus":      cpus,
            "gres":      gres if gres not in ("", "N/A") else None,
            "reason":    reason,
            "partition": part,
            "submit":    submit,
        })
    return counts


_DOWN_STATES = {"DOWN", "DRAIN", "DRAINING", "DRAINED", "FAIL", "FAILING", "ERROR", "UNKNOWN"}


_ACTIVE_STATES = {'RUNNING', 'PENDING', 'COMPLETING', 'RESIZING', 'SUSPENDED', 'REQUEUED'}

def collect_user_jobs(current_user, days=7):
    """Return list of user jobs from sacct (last N days)."""
    start = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - days * 86400))
    text = _run([
        "sacct", "-u", current_user,
        "--starttime", start,
        "--noheader", "--parsable2",
        "--format=JobID,JobName,State,Submit,Elapsed,AllocCPUS,AllocTRES,Partition",
    ])
    jobs = []
    seen = set()
    for line in text.splitlines():
        parts = line.split("|")
        if len(parts) < 8:
            continue
        jid, name, state_raw, submit, elapsed, cpus, tres, partition = parts[:8]
        if "." in jid:  # skip .batch / .extern sub-steps
            continue
        if jid in seen:
            continue
        seen.add(jid)
        state = state_raw.split(" ")[0]  # "CANCELLED by 3237978" → "CANCELLED"
        gres = ""
        for item in tres.split(","):
            if item.startswith("gres/gpu:"):
                gres = item
                break
        if not gres:
            for item in tres.split(","):
                if item.startswith("gres/gpu=") and item != "gres/gpu=0":
                    gres = item
                    break
        jobs.append({
            "id":        jid,
            "name":      name,
            "state":     state,
            "submit":    submit,
            "time":      elapsed,
            "cpus":      cpus,
            "gres":      gres or None,
            "partition": partition,
            "done":      state not in _ACTIVE_STATES,
        })
    jobs.sort(key=lambda j: j["submit"], reverse=True)
    if not jobs:
        log.warning("sacct returned no jobs for user %s", current_user)
    else:
        log.debug("sacct: %d jobs for %s", len(jobs), current_user)
    return jobs


def _int_prefix(value, default=0):
    match = re.match(r"\d+", str(value or ""))
    return int(match.group(0)) if match else default


def _cpu_state_counts(value):
    parts = str(value or "").split("/")
    if len(parts) != 4:
        return 0, 0, 0, 0
    return tuple(_int_prefix(part) for part in parts)


def _split_top_level(value):
    items, current, depth = [], [], 0
    for char in value:
        if char == "[":
            depth += 1
        elif char == "]":
            depth = max(depth - 1, 0)
        if char == "," and depth == 0:
            items.append("".join(current))
            current = []
        else:
            current.append(char)
    if current:
        items.append("".join(current))
    return items


def _expand_hostlist(value):
    """Expand common Slurm hostlist expressions for unique summary counts."""
    expanded = []

    def expand_one(expression):
        start = expression.find("[")
        if start < 0:
            return [expression] if expression else []
        end = expression.find("]", start)
        if end < 0:
            return [expression]
        prefix, body, suffix = expression[:start], expression[start + 1:end], expression[end + 1:]
        values = []
        for item in body.split(","):
            match = re.fullmatch(r"(\d+)-(\d+)(?::(\d+))?", item)
            if match:
                first, last = int(match.group(1)), int(match.group(2))
                step = int(match.group(3) or 1)
                width = max(len(match.group(1)), len(match.group(2)))
                direction = 1 if last >= first else -1
                values.extend(
                    f"{number:0{width}d}"
                    for number in range(first, last + direction, step * direction)
                )
            else:
                values.append(item)
        result = []
        for item in values:
            result.extend(expand_one(prefix + item + suffix))
        return result

    for expression in _split_top_level(value or ""):
        expanded.extend(expand_one(expression))
    return expanded


def collect_partition_summaries():
    """Collect compact partition/state groups without enumerating every node."""
    text = _run([
        "sinfo", "-h", "-o", "%P|%a|%l|%D|%t|%C|%m|%G|%N",
    ])
    partitions = {}
    unique_nodes = set()
    summary = {
        "cpu_alloc": 0, "cpu_total": 0,
        "mem_alloc_mb": None, "mem_total_mb": 0,
        "gpu_alloc": None, "gpu_total": 0,
        "node_count": 0, "node_states": {}, "gpu_by_type": {},
        "lazy": True,
    }

    for line_number, raw_line in enumerate(text.splitlines()):
        fields = raw_line.strip().split("|", 8)
        if len(fields) != 9:
            continue
        part_raw, avail, timelimit, node_count_raw, state_raw, cpus, memory, gres, nodelist = fields
        name = part_raw.rstrip("*")
        if not name:
            continue
        node_count = _int_prefix(node_count_raw)
        cpu_alloc, _cpu_idle, _cpu_other, cpu_total = _cpu_state_counts(cpus)
        mem_per_node = _int_prefix(memory)
        gpu_type, gpu_per_node = _gpu_total_from_gres(gres)
        state = state_raw.rstrip("*+~#$@").upper() or "UNKNOWN"

        part = partitions.setdefault(name, {
            "name": name, "avail": avail, "timelimit": timelimit,
            "nodes": 0, "cpu_alloc": 0, "cpu_total": 0,
            "mem_alloc_mb": None, "mem_total_mb": 0,
            "gpu_alloc": None, "gpu_idle": None, "gpu_total": 0,
            "gpu_vram_gb": None, "states": {},
            "jobs_running": None, "jobs_pending": None, "jobs": [],
            "jobs_loaded": False, "nodes_loaded": False,
            "node_lists": [],
        })
        if nodelist and nodelist != "(null)" and nodelist not in part["node_lists"]:
            part["node_lists"].append(nodelist)
        part["nodes"] += node_count
        part["cpu_alloc"] += cpu_alloc
        part["cpu_total"] += cpu_total
        part["mem_total_mb"] += mem_per_node * node_count
        part["gpu_total"] += gpu_per_node * node_count
        part["states"][state] = part["states"].get(state, 0) + node_count
        vram = _vram_gb_from_gres(gres)
        if vram:
            part["gpu_vram_gb"] = max(part["gpu_vram_gb"] or 0, vram)

        names = _expand_hostlist(nodelist)
        if not names and node_count:
            names = [f"{name}:{line_number}:{index}" for index in range(node_count)]
        new_count = sum(1 for node in names if node not in unique_nodes)
        unique_nodes.update(names)
        ratio = (new_count / node_count) if node_count else 0
        summary["cpu_alloc"] += round(cpu_alloc * ratio)
        summary["cpu_total"] += round(cpu_total * ratio)
        summary["mem_total_mb"] += round(mem_per_node * node_count * ratio)
        summary["node_states"][state] = summary["node_states"].get(state, 0) + new_count

        if gpu_per_node:
            gpu_name = gpu_type or "gpu"
            bucket = summary["gpu_by_type"].setdefault(
                gpu_name, {"alloc": None, "total": 0, "nodes": 0, "partitions": {}}
            )
            bucket["total"] += gpu_per_node * new_count
            bucket["nodes"] += new_count
            partition_bucket = bucket["partitions"].setdefault(
                name, {"alloc": None, "total": 0}
            )
            partition_bucket["total"] += gpu_per_node * node_count
            summary["gpu_total"] += gpu_per_node * new_count

    for part in partitions.values():
        has_live = any(state not in _DOWN_STATES for state in part["states"])
        if part["avail"] not in ("up", "down"):
            part["avail"] = "up" if has_live else "down"

    summary["node_count"] = len(unique_nodes)
    with _CACHE_LOCK:
        _PARTITION_NODELISTS.clear()
        _PARTITION_NODELISTS.update({
            name: tuple(part["node_lists"]) for name, part in partitions.items()
        })
    return summary, [partitions[name] for name in sorted(partitions)]


def collect_compact_node_resources():
    """Collect exact resource usage with one compact ``sinfo -N`` query."""
    text = _run([
        "sinfo", "-N", "-e", "-h", "-O",
        "NodeList:128,Partition:64,StateCompact:16,CPUsState:32,Memory:16,"
        "AllocMem:16,Gres:256,GresUsed:256",
    ])
    nodes = {}
    for raw_line in text.splitlines():
        fields = raw_line.split()
        if len(fields) < 8:
            continue
        name, partition, state, cpus, memory, alloc_memory, gres, gres_used = fields[:8]
        partition = partition.rstrip("*")
        cpu_alloc, cpu_idle, _cpu_other, cpu_total = _cpu_state_counts(cpus)
        gpu_type, gpu_total = _gpu_total_from_gres(gres)
        _used_type, gpu_alloc = _gpu_total_from_gres(gres_used)
        node = nodes.get(name)
        if node is not None:
            if partition and partition not in node["partitions"]:
                node["partitions"].append(partition)
            continue
        nodes[name] = {
            "name": name,
            "state": state.rstrip("*+~#$@").upper() or "UNKNOWN",
            "partitions": [partition] if partition else [],
            "cpu_alloc": cpu_alloc,
            "cpu_idle": cpu_idle,
            "cpu_total": cpu_total,
            "load": None,
            "mem_alloc_mb": _int_prefix(alloc_memory),
            "mem_total_mb": _int_prefix(memory),
            "gpu_type": gpu_type,
            "gpu_alloc": gpu_alloc,
            "gpu_idle": max(gpu_total - gpu_alloc, 0),
            "gpu_total": gpu_total,
            "gpu_vram_gb": _vram_gb_from_gres(gres),
        }
    return list(nodes.values())


def _apply_compact_node_resources(summary, partitions, nodes):
    """Fill aggregate blanks from exact nodes without exposing the node list."""
    if not nodes:
        return

    summary.update({
        "cpu_alloc": sum(node["cpu_alloc"] for node in nodes),
        "cpu_total": sum(node["cpu_total"] for node in nodes),
        "mem_alloc_mb": sum(node["mem_alloc_mb"] for node in nodes),
        "mem_total_mb": sum(node["mem_total_mb"] for node in nodes),
        "gpu_alloc": sum(node["gpu_alloc"] for node in nodes),
        "gpu_total": sum(node["gpu_total"] for node in nodes),
        "node_count": len(nodes),
        "node_states": {},
        "gpu_by_type": {},
        "lazy": False,
    })
    for node in nodes:
        state = node["state"]
        summary["node_states"][state] = summary["node_states"].get(state, 0) + 1
        if not node["gpu_total"]:
            continue
        gpu_type = node["gpu_type"] or "gpu"
        bucket = summary["gpu_by_type"].setdefault(
            gpu_type, {"alloc": 0, "total": 0, "nodes": 0, "partitions": {}}
        )
        bucket["alloc"] += node["gpu_alloc"]
        bucket["total"] += node["gpu_total"]
        bucket["nodes"] += 1
        for partition in node["partitions"]:
            part_bucket = bucket["partitions"].setdefault(
                partition, {"alloc": 0, "total": 0}
            )
            part_bucket["alloc"] += node["gpu_alloc"]
            part_bucket["total"] += node["gpu_total"]

    nodes_by_partition = {}
    for node in nodes:
        for partition in node["partitions"]:
            nodes_by_partition.setdefault(partition, []).append(node)

    for partition in partitions:
        part_nodes = nodes_by_partition.get(partition["name"], [])
        if not part_nodes:
            continue
        vram_values = [node["gpu_vram_gb"] for node in part_nodes if node["gpu_vram_gb"]]
        states = {}
        for node in part_nodes:
            states[node["state"]] = states.get(node["state"], 0) + 1
        partition.update({
            "nodes": len(part_nodes),
            "cpu_alloc": sum(node["cpu_alloc"] for node in part_nodes),
            "cpu_total": sum(node["cpu_total"] for node in part_nodes),
            "mem_alloc_mb": sum(node["mem_alloc_mb"] for node in part_nodes),
            "mem_total_mb": sum(node["mem_total_mb"] for node in part_nodes),
            "gpu_alloc": sum(node["gpu_alloc"] for node in part_nodes),
            "gpu_total": sum(node["gpu_total"] for node in part_nodes),
            "gpu_vram_gb": max(vram_values) if vram_values else None,
            "states": states,
        })
        partition["gpu_idle"] = max(
            partition["gpu_total"] - partition["gpu_alloc"], 0
        )


def _apply_partition_jobs(partitions, job_counts):
    """Attach one cluster-wide queue snapshot to all partition rows."""
    for partition in partitions:
        counts = job_counts.get(partition["name"], {})
        partition.update({
            "jobs_running": counts.get("running", 0),
            "jobs_pending": counts.get("pending", 0),
            "jobs": counts.get("jobs", []),
            "jobs_loaded": True,
        })


def build_cluster_summary(refresh=False):
    def load():
        started = time.monotonic()
        summary, partitions = collect_partition_summaries()
        if 0 < summary["node_count"] <= _NODE_ENRICH_LIMIT:
            try:
                nodes = _cached(
                    "compact-node-resources", _NODE_ENRICH_TTL,
                    collect_compact_node_resources,
                )
                _apply_compact_node_resources(summary, partitions, nodes)
            except (OSError, subprocess.SubprocessError) as exc:
                log.warning("exact resource enrichment unavailable: %s", exc)
            try:
                job_counts = _cached(
                    "all-partition-jobs", _PARTITION_JOBS_TTL,
                    collect_job_counts,
                )
                _apply_partition_jobs(partitions, job_counts)
            except (OSError, subprocess.SubprocessError) as exc:
                log.warning("automatic partition jobs unavailable: %s", exc)
        log.info(
            "compact cluster summary in %.2fs — %d nodes, %d partitions",
            time.monotonic() - started, summary["node_count"], len(partitions),
        )
        return {
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "summary": summary, "partitions": partitions, "nodes": [],
        }

    return _cached("cluster-summary", 30, load, refresh=refresh)


def collect_partition_nodes(partition):
    """Collect exact node details for one explicitly requested partition."""
    with _CACHE_LOCK:
        node_lists = _PARTITION_NODELISTS.get(partition, ())
    if node_lists:
        return collect_nodes(",".join(node_lists))

    # Fallback for callers that request a partition before any summary exists.
    text = _run([
        "sinfo", "-N", "-e", "-h", "-p", partition, "-O",
        "NodeList:128,Partition:64,StateCompact:16,CPUsState:32,Memory:16,"
        "AllocMem:16,Gres:256,GresUsed:256",
    ])
    nodes = []
    for raw_line in text.splitlines():
        fields = raw_line.split()
        if len(fields) < 8:
            continue
        name, _part, state, cpus, memory, alloc_memory, gres, gres_used = fields[:8]
        cpu_alloc, cpu_idle, _cpu_other, cpu_total = _cpu_state_counts(cpus)
        gpu_type, gpu_total = _gpu_total_from_gres(gres)
        _used_type, gpu_alloc = _gpu_total_from_gres(gres_used)
        nodes.append({
            "name": name, "state": state.upper(), "partitions": [partition],
            "cpu_alloc": cpu_alloc, "cpu_idle": cpu_idle, "cpu_total": cpu_total,
            "load": None,
            "mem_alloc_mb": _int_prefix(alloc_memory),
            "mem_total_mb": _int_prefix(memory),
            "gpu_type": gpu_type, "gpu_alloc": gpu_alloc,
            "gpu_idle": max(gpu_total - gpu_alloc, 0), "gpu_total": gpu_total,
            "gpu_vram_gb": _vram_gb_from_gres(gres),
        })
    return nodes


def build_partition_details(partition, refresh=False):
    def load():
        started = time.monotonic()
        nodes = collect_partition_nodes(partition)
        vram_values = [node["gpu_vram_gb"] for node in nodes if node["gpu_vram_gb"]]
        states = {}
        for node in nodes:
            states[node["state"]] = states.get(node["state"], 0) + 1
        details = {
            "name": partition, "nodes": len(nodes),
            "cpu_alloc": sum(node["cpu_alloc"] for node in nodes),
            "cpu_total": sum(node["cpu_total"] for node in nodes),
            "mem_alloc_mb": sum(node["mem_alloc_mb"] for node in nodes),
            "mem_total_mb": sum(node["mem_total_mb"] for node in nodes),
            "gpu_alloc": sum(node["gpu_alloc"] for node in nodes),
            "gpu_total": sum(node["gpu_total"] for node in nodes),
            "gpu_vram_gb": max(vram_values) if vram_values else None,
            "states": states, "nodes_loaded": True,
        }
        details["gpu_idle"] = max(details["gpu_total"] - details["gpu_alloc"], 0)
        log.info(
            "partition %s details in %.2fs — %d nodes",
            partition, time.monotonic() - started, len(nodes),
        )
        return {"partition": details, "nodes": nodes}

    return _cached(("partition", partition), 60, load, refresh=refresh)


def build_partition_jobs(partition, refresh=False):
    def load():
        counts = collect_job_counts(partition).get(
            partition, {"running": 0, "pending": 0, "jobs": []}
        )
        return {
            "jobs_running": counts["running"],
            "jobs_pending": counts["pending"],
            "jobs": counts["jobs"], "jobs_loaded": True,
        }

    return _cached(("partition-jobs", partition), 20, load, refresh=refresh)


def build_cluster_snapshot():
    """Cluster state only: scontrol show node + squeue (no sacct)."""
    t0 = time.monotonic()
    nodes = collect_nodes()
    job_counts = collect_job_counts()
    part_limits = collect_partition_limits()

    summary = {
        "cpu_alloc":    sum(n["cpu_alloc"]    for n in nodes),
        "cpu_total":    sum(n["cpu_total"]    for n in nodes),
        "mem_alloc_mb": sum(n["mem_alloc_mb"] for n in nodes),
        "mem_total_mb": sum(n["mem_total_mb"] for n in nodes),
        "gpu_alloc":    sum(n["gpu_alloc"]    for n in nodes),
        "gpu_total":    sum(n["gpu_total"]    for n in nodes),
        "node_count":   len(nodes),
        "node_states":  {},
        "gpu_by_type":  {},
    }
    for n in nodes:
        st = n["state"]
        summary["node_states"][st] = summary["node_states"].get(st, 0) + 1
        if n["gpu_total"]:
            t = n["gpu_type"] or "gpu"
            b = summary["gpu_by_type"].setdefault(t, {"alloc": 0, "total": 0, "nodes": 0, "partitions": {}})
            b["alloc"] += n["gpu_alloc"]
            b["total"] += n["gpu_total"]
            b["nodes"] += 1
            for p in n["partitions"]:
                if p:
                    pb = b["partitions"].setdefault(p, {"alloc": 0, "total": 0})
                    pb["alloc"] += n["gpu_alloc"]
                    pb["total"] += n["gpu_total"]

    part_agg = {}
    for n in nodes:
        for p in n["partitions"]:
            if not p:
                continue
            agg = part_agg.setdefault(p, {
                "name": p, "nodes": 0,
                "cpu_alloc": 0, "cpu_total": 0,
                "gpu_alloc": 0, "gpu_total": 0,
                "states": {}, "_vram_vals": [],
            })
            agg["nodes"] += 1
            agg["cpu_alloc"] += n["cpu_alloc"]
            agg["cpu_total"] += n["cpu_total"]
            agg["gpu_alloc"] += n["gpu_alloc"]
            agg["gpu_total"] += n["gpu_total"]
            agg["states"][n["state"]] = agg["states"].get(n["state"], 0) + 1
            if n["gpu_vram_gb"]:
                agg["_vram_vals"].append(n["gpu_vram_gb"])

    partitions = []
    for name, agg in sorted(part_agg.items()):
        jc = job_counts.get(name, {"running": 0, "pending": 0, "timelimit": None, "jobs": []})
        node_states = agg.get("states", {})
        has_live = any(s.rstrip("*+~").upper() not in _DOWN_STATES for s in node_states)
        agg["avail"]        = "up" if has_live else "down"
        agg["timelimit"]    = part_limits.get(name) or "—"
        agg["gpu_idle"]     = agg["gpu_total"] - agg["gpu_alloc"]
        agg["jobs_running"] = jc["running"]
        agg["jobs_pending"] = jc["pending"]
        agg["jobs"]         = jc["jobs"]
        vram_vals = agg.pop("_vram_vals")
        agg["gpu_vram_gb"]  = max(vram_vals) if vram_vals else None
        partitions.append(agg)

    log.info("cluster snapshot in %.2fs — %d nodes, %d partitions",
             time.monotonic() - t0, len(nodes), len(partitions))
    return {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "summary":      summary,
        "partitions":   partitions,
        "nodes":        nodes,
    }


def collect_active_queue(current_user):
    """Return running/pending jobs for current user from squeue (fast)."""
    text = _run(["squeue", "-u", current_user, "-h", "-o", "%i|%j|%T|%P|%M|%V|%C|%b"])
    jobs = []
    for line in text.splitlines():
        parts = line.split("|", 7)
        if len(parts) < 7:
            continue
        jid, name, state, partition, elapsed, submit, cpus = parts[:7]
        gres = parts[7] if len(parts) > 7 else ""
        jobs.append({
            "id":        jid,
            "name":      name,
            "state":     state,
            "partition": partition,
            "time":      elapsed,
            "submit":    submit,
            "cpus":      cpus,
            "gres":      gres or None,
        })
    log.debug("squeue: %d active jobs for %s", len(jobs), current_user)
    return jobs


def build_snapshot():
    """Adaptive initial page: restore full panels where cluster size permits."""
    current_user = getpass.getuser()
    snap = dict(build_cluster_summary())
    snap["current_user"] = current_user
    snap["active_queue"] = []
    snap["active_queue_loaded"] = False
    snap["user_jobs"] = []
    snap["user_jobs_loaded"] = False
    snap["auto_load_personal"] = (
        0 < snap.get("summary", {}).get("node_count", 0) <= _NODE_ENRICH_LIMIT
    )
    if snap["auto_load_personal"]:
        try:
            snap["active_queue"] = _cached(
                ("active-queue", current_user), 10,
                lambda: collect_active_queue(current_user),
            )
            snap["active_queue_loaded"] = True
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("automatic active queue unavailable: %s", exc)
    return snap


# ---------------------------------------------------------------------------
# Job detail
# ---------------------------------------------------------------------------

_JOB_RE = {
    "job_name":    re.compile(r"JobName=(\S+)"),
    "user":        re.compile(r"UserId=([^(\s]+)"),
    "account":     re.compile(r"\bAccount=(\S+)"),
    "qos":         re.compile(r"\bQOS=(\S+)"),
    "state":       re.compile(r"JobState=(\S+)"),
    "reason":      re.compile(r"\bReason=(\S+)"),
    "partition":   re.compile(r"\bPartition=(\S+)"),
    "priority":    re.compile(r"Priority=(\d+)"),
    "num_nodes":   re.compile(r"NumNodes=(\d+)"),
    "num_cpus":    re.compile(r"NumCPUs=(\d+)"),
    "num_tasks":   re.compile(r"NumTasks=(\d+)"),
    "cpus_task":   re.compile(r"CPUs/Task=(\d+)"),
    "tres":        re.compile(r"\bTRES=(\S+)"),
    "gres_raw":    re.compile(r"\bGres=(\S+)"),
    "runtime":     re.compile(r"RunTime=(\S+)"),
    "timelimit":   re.compile(r"TimeLimit=(\S+)"),
    "submit_time": re.compile(r"SubmitTime=(\S+)"),
    "start_time":  re.compile(r"StartTime=(\S+)"),
    "end_time":    re.compile(r"EndTime=(\S+)"),
    "nodelist":    re.compile(r"NodeList=(\S+)"),
    "batch_host":  re.compile(r"BatchHost=(\S+)"),
    "exit_code":   re.compile(r"ExitCode=(\S+)"),
    "mem_cpu":     re.compile(r"MinMemoryCPU=(\S+)"),
    "mem_node":    re.compile(r"MinMemoryNode=(\S+)"),
    "workdir":     re.compile(r"WorkDir=(.+)"),
    "command":     re.compile(r"Command=(.+)"),
    "stdout":      re.compile(r"StdOut=(.+)"),
    "stderr":      re.compile(r"StdErr=(.+)"),
}


def normalize_job_id(jobid):
    """Return an scontrol-compatible ID for regular and array jobs."""
    value = unquote(str(jobid)).strip()
    if re.fullmatch(r"\d+(?:_\d+)?", value):
        return value

    match = re.fullmatch(r"(\d+)_\[(\d+)\]", value)
    if match:
        return f"{match.group(1)}_{match.group(2)}"

    # squeue compresses pending array ranges (for example 123_[1-20%4]).
    # scontrol cannot show that expression directly, so query its parent job.
    match = re.fullmatch(
        r"(\d+)_\[(\d+(?:-\d+(?::\d+)?)?(?:,\d+(?:-\d+(?::\d+)?)?)*(?:%\d+)?)\]",
        value,
    )
    if match:
        return match.group(1)

    raise ValueError(f"Invalid job ID: {jobid!r}")


def _parse_scontrol_job_detail(text):
    info = {}
    for key, rx in _JOB_RE.items():
        m = rx.search(text)
        info[key] = m.group(1).strip() if m else None

    return _add_job_gpu_detail(info)


def _add_job_gpu_detail(info):
    """Add normalized GPU count/type fields from TRES or Gres."""

    # GPU count + type from TRES, then fall back to Gres field
    tres = info.get("tres") or ""
    gpu_count, gpu_type = 0, None
    m = re.search(r"gres/gpu:([a-zA-Z0-9_]+)=(\d+)", tres)
    if m:
        gpu_type, gpu_count = m.group(1), int(m.group(2))
    else:
        m = re.search(r"gres/gpu=(\d+)", tres)
        if m:
            gpu_count = int(m.group(1))
    gres_raw = info.get("gres_raw") or ""
    if not gpu_type and gres_raw not in ("", "(null)"):
        m = _GRES_GPU_RE.search(gres_raw)
        if m:
            gpu_type = m.group(1)
            if not gpu_count:
                gpu_count = int(m.group(2))
    info["gpu_count"] = gpu_count
    info["gpu_type"]  = gpu_type
    return info


def _parse_sacct_job_detail(text, query_jobid):
    """Parse the allocation row returned by sacct for a completed job."""
    fields = [
        "jobid", "job_name", "user", "account", "qos", "state", "reason",
        "partition", "priority", "num_nodes", "num_cpus", "num_tasks",
        "req_cpus", "tres", "runtime", "timelimit", "submit_time",
        "start_time", "end_time", "nodelist", "exit_code", "req_mem",
    ]
    selected = None
    fallback = None
    for line in text.splitlines():
        values = line.rstrip("\n").split("|")
        if len(values) < len(fields):
            continue
        row = dict(zip(fields, values[:len(fields)]))
        row_id = row["jobid"]
        if "." in row_id:  # exclude .batch, .extern and other job steps
            continue
        if fallback is None:
            fallback = row
        if row_id == query_jobid:
            selected = row
            break
    row = selected or fallback
    if row is None:
        raise RuntimeError(f"sacct returned no accounting record for job {query_jobid}")

    state = row["state"].split(" ", 1)[0].rstrip("+") or None
    num_tasks = int(row["num_tasks"]) if row["num_tasks"].isdigit() else 0
    num_cpus = int(row["num_cpus"]) if row["num_cpus"].isdigit() else 0
    cpus_task = str(num_cpus // num_tasks) if num_tasks and num_cpus else None
    info = {key: None for key in _JOB_RE}
    info.update({
        "job_name": row["job_name"] or None,
        "user": row["user"] or None,
        "account": row["account"] or None,
        "qos": row["qos"] or None,
        "state": state,
        "reason": row["reason"] or None,
        "partition": row["partition"] or None,
        "priority": row["priority"] or None,
        "num_nodes": row["num_nodes"] or None,
        "num_cpus": row["num_cpus"] or None,
        "num_tasks": row["num_tasks"] or None,
        "cpus_task": cpus_task,
        "tres": row["tres"] or None,
        "runtime": row["runtime"] or None,
        "timelimit": row["timelimit"] or None,
        "submit_time": row["submit_time"] or None,
        "start_time": row["start_time"] or None,
        "end_time": row["end_time"] or None,
        "nodelist": row["nodelist"] or None,
        "exit_code": row["exit_code"] or None,
        "mem_cpu": row["req_mem"] or None,
    })
    return _add_job_gpu_detail(info)


def collect_job_detail(jobid):
    query_jobid = normalize_job_id(jobid)
    try:
        text = _run(["scontrol", "show", "job", query_jobid])
        return _parse_scontrol_job_detail(text)
    except subprocess.CalledProcessError:
        log.debug("scontrol has no live record for job %s; falling back to sacct", query_jobid)

    columns = (
        "JobID,JobName,User,Account,QOS,State,Reason,Partition,Priority,"
        "AllocNodes,AllocCPUS,NTasks,ReqCPUS,ReqTRES,Elapsed,Timelimit,"
        "Submit,Start,End,NodeList,ExitCode,ReqMem"
    )
    text = _run([
        "sacct", "-j", query_jobid, "--noheader", "--parsable2",
        f"--format={columns}",
    ])
    return _parse_sacct_job_detail(text, query_jobid)


_JOB_CSS = """\
  :root {
    --bg:#0f1115;--panel:#171a21;--border:#2a2f3a;--text:#e6e9ef;
    --muted:#8b93a3;--accent:#4f8cff;--good:#3ec97c;--warn:#f0a93f;--bad:#ef5b5b;
  }
  *{box-sizing:border-box}
  body{margin:0;font-family:-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
       background:var(--bg);color:var(--text);font-size:14px}
  a{color:var(--accent);text-decoration:none}
  a:hover{text-decoration:underline}
  header{padding:16px 24px;border-bottom:1px solid var(--border);
         display:flex;align-items:center;gap:16px}
  header h1{margin:0;font-size:18px;font-weight:600}
  main{padding:24px;max-width:960px;margin:0 auto}
  .job-title{font-size:22px;font-weight:700;margin:0 0 4px}
  .job-name{color:var(--muted);font-size:15px;margin-bottom:16px}
  .reason-note{background:rgba(240,169,63,.1);border:1px solid rgba(240,169,63,.3);
               border-radius:6px;padding:8px 12px;color:var(--warn);
               font-size:13px;margin-bottom:20px}
  .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:14px}
  .card{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:16px}
  .card.full{grid-column:1/-1}
  .card-title{color:var(--muted);font-size:11px;text-transform:uppercase;
              letter-spacing:.05em;margin-bottom:10px;font-weight:600}
  table.info{width:100%;border-collapse:collapse;font-size:13px}
  table.info td{padding:5px 0;border-bottom:1px solid rgba(42,47,58,.7);vertical-align:top}
  table.info tr:last-child td{border-bottom:none}
  td.lbl{color:var(--muted);width:110px;white-space:nowrap;padding-right:12px}
  code{color:var(--accent);font-size:12px;word-break:break-all}
  .pill{display:inline-block;padding:2px 8px;border-radius:999px;font-size:12px;
        font-weight:600;text-transform:uppercase;letter-spacing:.03em}
  .running,.completing{background:rgba(240,169,63,.15);color:var(--warn)}
  .pending{background:rgba(79,140,255,.15);color:var(--accent)}
  .completed{background:rgba(62,201,124,.15);color:var(--good)}
  .failed,.cancelled,.timeout,.node_fail{background:rgba(239,91,91,.15);color:var(--bad)}
  .other{background:rgba(139,147,163,.15);color:var(--muted)}
"""


def render_job_page(jobid):
    def esc(v):
        return _html.escape(str(v)) if v not in (None, "(null)", "N/A", "") else "—"

    try:
        info = collect_job_detail(jobid)
    except Exception as exc:
        return (f'<!DOCTYPE html><html><head><meta charset="utf-8"><title>Job {esc(jobid)}</title>'
                f'<style>{_JOB_CSS}</style></head>'
                f'<body><header><a href="/">&#8592; Dashboard</a>'
                f'<h1>&#9881; Slurm Dashboard</h1></header>'
                f'<main><p style="color:var(--bad)">Error: {esc(str(exc))}</p></main>'
                f'</body></html>')

    state    = (info.get("state") or "UNKNOWN").upper()
    pill_cls = state.lower().replace(" ", "_")
    if pill_cls not in {"running", "completing", "pending", "completed",
                        "failed", "cancelled", "timeout", "node_fail"}:
        pill_cls = "other"

    gpu_str = (f"{info['gpu_count']}× {info['gpu_type'] or 'gpu'}"
               if info.get("gpu_count") else "")

    reason = info.get("reason")
    reason_html = (f'<div class="reason-note">Reason: {esc(reason)}</div>'
                   if reason and reason not in ("None", "(null)") else "")

    def row(label, val, code=False):
        v = esc(val)
        if v == "—":
            return ""
        inner = f"<code>{v}</code>" if code else v
        return f"<tr><td class='lbl'>{label}</td><td>{inner}</td></tr>"

    def card(title, *rows, full=False):
        body = "".join(rows)
        if not body:
            return ""
        cls = "card full" if full else "card"
        return (f'<div class="{cls}"><div class="card-title">{title}</div>'
                f'<table class="info">{body}</table></div>')

    mem = info.get("mem_cpu") or info.get("mem_node")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Job {esc(str(jobid))} — Slurm Dashboard</title>
<style>{_JOB_CSS}</style>
</head>
<body>
<header>
  <a href="/">&#8592; Dashboard</a>
  <h1>&#9881; Slurm Dashboard</h1>
</header>
<main>
  <div class="job-title">Job {esc(str(jobid))} &nbsp;<span class="pill {pill_cls}">{state}</span></div>
  <div class="job-name">{esc(info.get('job_name'))}</div>
  {reason_html}
  <div class="grid">
    {card("Identity",
          row("User",      info.get("user")),
          row("Account",   info.get("account")),
          row("QOS",       info.get("qos")),
          row("Partition", info.get("partition")),
          row("Priority",  info.get("priority")),
          row("Exit code", info.get("exit_code")))}
    {card("Resources",
          row("Nodes",       info.get("num_nodes")),
          row("CPUs",        info.get("num_cpus")),
          row("Tasks",       info.get("num_tasks")),
          row("CPUs / task", info.get("cpus_task")),
          row("GPUs",        gpu_str or None),
          row("Memory",      mem),
          row("TRES",        info.get("tres")))}
    {card("Timing",
          row("Submit",     info.get("submit_time")),
          row("Start",      info.get("start_time")),
          row("End",        info.get("end_time")),
          row("Run time",   info.get("runtime")),
          row("Time limit", info.get("timelimit")))}
    {card("Nodes",
          row("Node list",  info.get("nodelist")),
          row("Batch host", info.get("batch_host")))}
    {card("Paths",
          row("Work dir", info.get("workdir"), code=True),
          row("Command",  info.get("command"),  code=True),
          row("Stdout",   info.get("stdout"),   code=True),
          row("Stderr",   info.get("stderr"),   code=True),
          full=True)}
  </div>
</main>
</body>
</html>"""


# ---------------------------------------------------------------------------
# HTML page
# ---------------------------------------------------------------------------

PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Slurm Dashboard</title>
<style>
  :root {
    --bg: #0f1115; --panel: #171a21; --border: #2a2f3a; --text: #e6e9ef;
    --muted: #8b93a3; --accent: #4f8cff; --good: #3ec97c; --warn: #f0a93f; --bad: #ef5b5b;
    --sb-track: #1a1d24; --sb-thumb: #3a3f4d; --sb-thumb-hover: #4e5668;
    --bar-track: #2a2f3a; --kbd-bg: #2a2f3a; --kbd-border: transparent;
  }
  :root[data-theme="light"] {
    --bg: #f4f5f7; --panel: #ffffff; --border: #dde1ea; --text: #1a1d23;
    --muted: #6b7280; --accent: #2563eb; --good: #16a34a; --warn: #d97706; --bad: #dc2626;
    --sb-track: #e4e7ed; --sb-thumb: #b0b7c3; --sb-thumb-hover: #8f98a8;
    --bar-track: #d5d9e3; --kbd-bg: #eceef3; --kbd-border: #c8cdd8;
  }
  * { box-sizing: border-box; scrollbar-width: thin; scrollbar-color: var(--sb-thumb) var(--sb-track); }
  ::-webkit-scrollbar { width: 6px; height: 6px; }
  ::-webkit-scrollbar-track { background: var(--sb-track); border-radius: 3px; }
  ::-webkit-scrollbar-thumb { background: var(--sb-thumb); border-radius: 3px; }
  ::-webkit-scrollbar-thumb:hover { background: var(--sb-thumb-hover); }
  body { margin: 0; font-family: -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
         background: var(--bg); color: var(--text); font-size: 14px;
         display: flex; flex-direction: column; height: 100vh; overflow: hidden; }
  header { flex-shrink: 0; padding: 12px 24px; border-bottom: 1px solid var(--border);
           display: flex; align-items: baseline; gap: 16px; flex-wrap: wrap; }
  header h1 { margin: 0; font-size: 20px; font-weight: 600; }
  header .meta { color: var(--muted); font-size: 12px; }
  header .header-actions { margin-left: auto; display: flex; align-items: center; gap: 8px; }
  header .auto-refresh { display: flex; align-items: center; gap: 6px; color: var(--muted);
                         font-size: 12px; white-space: nowrap; }
  header .auto-refresh select { background: var(--panel); color: var(--text);
                                border: 1px solid var(--border); border-radius: 6px;
                                padding: 5px 7px; font: inherit; cursor: pointer; }
  header .reload { background: var(--accent); color: #fff; border: none;
                   border-radius: 6px; padding: 6px 14px; font-size: 13px; cursor: pointer; }
  header .reload:hover { filter: brightness(1.1); }
  header .theme-btn { background: transparent; border: 1px solid var(--border); color: var(--muted);
                      border-radius: 6px; padding: 5px 10px; font-size: 14px; cursor: pointer;
                      transition: color .15s, border-color .15s; }
  header .theme-btn:hover { color: var(--text); border-color: var(--text); }
  main { flex: 1; min-height: 0; display: flex; flex-direction: column;
         padding: 12px 20px 0; overflow: hidden; }
  footer { flex-shrink: 0; text-align: center; color: var(--muted); font-size: 11px;
           padding: 7px 20px; border-top: 1px solid var(--border); }
  h2 { font-size: 15px; color: var(--muted); text-transform: uppercase; letter-spacing: .04em;
       margin: 32px 0 12px; }
  .hint { font-size: 12px; color: var(--muted); margin: -8px 0 12px; }
  .hint kbd { background: var(--kbd-bg); border: 1px solid var(--kbd-border);
              border-radius: 4px; padding: 1px 5px; font-size: 11px; color: var(--text); }

  /* summary cards */
  .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 14px; }
  .card { background: var(--panel); border: 1px solid var(--border); border-radius: 10px; padding: 16px; }
  .card .label { color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .05em; }
  .card .value { font-size: 26px; font-weight: 700; margin-top: 4px; }
  .card .sub   { color: var(--muted); font-size: 12px; margin-top: 2px; }
  /* compact cards for sidebar — single column */
  .left-col .cards { grid-template-columns: 1fr; gap: 6px; }
  .left-col .card { padding: 8px 12px; }
  .left-col .card .label { font-size: 11px; }
  .left-col .card .value { font-size: 15px; line-height: 1.3; }
  .left-col .card .sub { display: none; }
  .left-col .card .bar { height: 5px; margin-top: 5px; }

  /* progress bars */
  .bar { height: 8px; border-radius: 4px; background: var(--bar-track); margin-top: 10px; overflow: hidden; }
  .bar > span { display: block; height: 100%; background: var(--accent); }
  .bar.gpu > span  { background: var(--good); }
  .bar.high > span { background: var(--warn); }
  .bar.crit > span { background: var(--bad); }
  .minibar { display: inline-block; width: 60px; height: 6px; border-radius: 3px; background: var(--bar-track);
             vertical-align: middle; margin-right: 5px; overflow: hidden; }
  .minibar > span { display: block; height: 100%; background: var(--accent); }
  .minibar.gpu > span { background: var(--good); }

  /* tables */
  table { width: 100%; border-collapse: collapse; background: var(--panel);
          border: 1px solid var(--border); border-radius: 10px; overflow: hidden; }
  th, td { text-align: left; padding: 8px 12px; border-bottom: 1px solid var(--border); white-space: nowrap; }
  /* compact partition table to avoid horizontal scroll */
  #part-table th, #part-table td { padding: 5px 7px; }
  th { color: var(--muted); font-weight: 600; font-size: 12px; text-transform: uppercase;
       letter-spacing: .04em; cursor: pointer; user-select: none; }
  th.no-sort { cursor: default; }
  th:not(.no-sort):hover { color: var(--text); }
  tr:last-child > td { border-bottom: none; }
  tr:hover > td { background: rgba(128,128,128,.05); }

  /* partition row */
  .toggle-cell { width: 52px; text-align: center; color: var(--muted); font-size: 11px; cursor: pointer; }
  .toggle-cell:hover { color: var(--text); }
  .part-name-link { cursor: pointer; border-bottom: 1px dotted var(--text); }
  .part-name-link:hover { color: var(--accent); border-bottom-color: var(--accent); }
  .th-refresh, .row-refresh {
    color: var(--muted); font-size: 13px; cursor: pointer; margin-left: 8px;
  }
  .th-refresh:hover, .row-refresh:hover { color: var(--accent); }
  .th-refresh.loading, .row-refresh.loading { color: var(--accent); opacity: 0.5; pointer-events: none; }
  .job-num { display:inline-block; min-width:3ch; text-align:right; font-variant-numeric:tabular-nums; }

  /* inner node sub-table */
  .nodes-expand-row > td { padding: 0 0 0 36px; background: var(--bg) !important; }
  .inner-wrap { border-left: 3px solid var(--border); margin: 6px 0 10px; }
  .inner-table { width: 100%; border-collapse: collapse; background: var(--bg); font-size: 13px; }
  .inner-table th { background: color-mix(in srgb, var(--accent) 6%, transparent);
                    font-size: 11px; padding: 6px 10px; }
  .inner-table td { padding: 6px 10px; border-bottom: 1px solid var(--border); }
  .inner-table tr:last-child td { border-bottom: none; }
  .inner-table tr:hover td { background: rgba(128,128,128,.06); }

  .gpu-expand-row > td { padding: 0 0 0 36px; background: var(--bg) !important; }

  /* state pills */
  .pill { display: inline-block; padding: 2px 7px; border-radius: 999px; font-size: 11px;
          font-weight: 600; text-transform: uppercase; letter-spacing: .03em; }
  .pill.idle, .pill.up       { background: rgba(62,201,124,.15);  color: var(--good); }
  .pill.mixed, .pill.alloc   { background: rgba(240,169,63,.15);  color: var(--warn); }
  .pill.down, .pill.drain,
  .pill.fail, .pill.maint    { background: rgba(239,91,91,.15);   color: var(--bad);  }
  .pill.other                { background: rgba(139,147,163,.15); color: var(--muted);}

  .filterbar { display: flex; gap: 16px; margin: 0 0 12px; flex-wrap: wrap; align-items: center; }
  .filterbar label { display: flex; align-items: center; color: var(--text); font-size: 13px; }
  .filterbar input[type="number"] {
    background: var(--panel); border: 1px solid var(--border); color: var(--text);
    border-radius: 6px; padding: 4px 8px; font-size: 13px;
  }
  .muted { color: var(--muted); }
  code { color: var(--accent); }
  /* three-column layout with draggable resizers */
  .three-col  { flex: 1; min-height: 0; display: flex; flex-direction: row; overflow: hidden; }
  .left-col   { width: 25%; min-width: 100px; flex-shrink: 0; overflow: auto; padding: 0 16px 20px 0; }
  .center-col { flex: 1; min-width: 0; overflow: auto; padding: 0 16px 20px; }
  .right-col  { width: 25%; min-width: 180px; flex-shrink: 0; padding: 0 0 0 16px;
               display: flex; flex-direction: column; overflow: hidden; }
  .jobs-section { display: flex; flex-direction: column; flex: 1; min-height: 0; padding-bottom: 12px; }
  .jobs-section h3 { flex-shrink: 0; font-size: 13px; color: var(--muted); text-transform: uppercase;
                     letter-spacing: .04em; margin: 14px 0 6px; font-weight: 600; }
  .jobs-scroll { flex: 1; min-height: 0; overflow-y: auto; }
  .col-resizer { width: 4px; flex-shrink: 0; background: var(--border); cursor: col-resize;
                 user-select: none; position: relative; transition: background .15s; }
  .col-resizer:hover, .col-resizer.dragging { background: var(--accent); }
  .col-resizer::after { content: ''; position: absolute; inset: 0 -5px; }
  .row-resizer { height: 4px; flex-shrink: 0; background: var(--border); cursor: row-resize;
                 user-select: none; position: relative; transition: background .15s; }
  .row-resizer:hover, .row-resizer.dragging { background: var(--accent); }
  .row-resizer::after { content: ''; position: absolute; inset: -5px 0; }
  .left-col h2:first-child, .center-col h2:first-child, .right-col h2:first-child { margin-top: 0; }

  /* compact tables in left/right sidebars */
  .left-col table { font-size: 12px; }
  .left-col th, .left-col td { padding: 5px 8px; }
  .quota-refresh { border: 0; background: transparent; color: var(--muted); cursor: pointer;
                   font: inherit; padding: 0 2px; }
  .quota-refresh:hover { color: var(--accent); }
  .quota-refresh.loading { color: var(--accent); opacity: .5; pointer-events: none; }
  #quota-table th { cursor: default; }
  #quota-table td { vertical-align: top; }
  .quota-scope { color: var(--text); font-weight: 600; max-width: 130px;
                 overflow: hidden; text-overflow: ellipsis; }
  .quota-path { color: var(--muted); font-size: 10px; max-width: 130px;
                overflow: hidden; text-overflow: ellipsis; }
  .quota-value { font-variant-numeric: tabular-nums; font-size: 11px; }
  .quota-meter { width: 72px; height: 4px; margin-top: 4px; border-radius: 2px;
                 background: var(--bar-track); overflow: hidden; }
  .quota-meter span { display: block; height: 100%; background: var(--good); }
  .quota-meter.high span { background: var(--warn); }
  .quota-meter.crit span { background: var(--bad); }
  .quota-meta { color: var(--muted); font-size: 10px; margin-top: 6px; }
  .quota-error { color: var(--bad); font-size: 12px; white-space: pre-wrap; }
  .quota-raw { margin: 0; padding: 8px; border: 1px solid var(--border); border-radius: 6px;
               background: var(--panel); color: var(--muted); font-size: 10px;
               white-space: pre-wrap; overflow-wrap: anywhere; }

  /* user jobs table — same padding as partition table */
  #aq-table th, #aq-table td, #hist-table th, #hist-table td { padding: 5px 7px; }
  .uj-id { color: var(--accent); text-decoration: none; border-bottom: 1px dotted var(--accent); }
  .uj-id:hover { color: var(--text); border-bottom-color: var(--text); }
  .uj-chip { display: inline-block; padding: 1px 6px; border-radius: 999px; font-size: 10px;
             font-weight: 700; text-transform: uppercase; letter-spacing: .03em; }
  .uj-chip.running, .uj-chip.completing { background: rgba(62,201,124,.15); color: var(--good); }
  .uj-chip.pending  { background: rgba(240,169,63,.15);  color: var(--warn); }
  .uj-chip.other    { background: rgba(139,147,163,.15); color: var(--muted); }
  .uj-chip.done     { background: rgba(79,140,255,.1);   color: var(--accent); }
  /* sort bar */
  .uj-sortbar { display: flex; gap: 4px; }
  .uj-sort { background: transparent; border: 1px solid var(--border); color: var(--muted);
             font-size: 10px; padding: 2px 7px; border-radius: 4px; cursor: pointer; }
  .uj-sort:hover { color: var(--text); }
  .uj-sort.active { border-color: var(--accent); color: var(--accent); }

  /* in-dashboard job detail dialog */
  .job-modal { position: fixed; inset: 0; z-index: 1000; display: none;
               align-items: center; justify-content: center; padding: 28px;
               background: rgba(0,0,0,.55); backdrop-filter: blur(2px); }
  .job-modal.open { display: flex; }
  .job-dialog { width: min(960px, 100%); max-height: min(820px, calc(100vh - 56px));
                overflow-x: hidden; overflow-y: auto; background: var(--bg); border: 1px solid var(--border);
                border-radius: 14px; box-shadow: 0 24px 80px rgba(0,0,0,.45); }
  .job-dialog-head { position: sticky; top: 0; z-index: 1; display: flex;
                     align-items: center; gap: 10px; padding: 16px 18px;
                     background: var(--bg); border-bottom: 1px solid var(--border); }
  .job-dialog-title { font-size: 18px; font-weight: 700; }
  .job-dialog-close { margin-left: auto; width: 30px; height: 30px; border-radius: 50%;
                      border: 1px solid var(--border); background: var(--panel);
                      color: var(--text); cursor: pointer; font-size: 18px; line-height: 1; }
  .job-dialog-body { padding: 18px; }
  .job-dialog-name { color: var(--muted); margin: -8px 0 14px; }
  .job-dialog-error { color: var(--bad); padding: 22px 0; text-align: center; }
  .job-dialog-title .running, .job-dialog-title .completing {
    background: rgba(62,201,124,.15); color: var(--good);
  }
  .job-dialog-title .pending { background: rgba(240,169,63,.15); color: var(--warn); }
  .job-dialog-title .completed { background: rgba(79,140,255,.15); color: var(--accent); }
  .job-dialog-title .failed, .job-dialog-title .cancelled,
  .job-dialog-title .timeout, .job-dialog-title .node_fail {
    background: rgba(239,91,91,.15); color: var(--bad);
  }
  .job-grid { display: grid; grid-template-columns: repeat(auto-fit,minmax(280px,1fr)); gap: 12px; }
  .job-card { background: var(--panel); border: 1px solid var(--border);
              border-radius: 10px; padding: 14px; }
  .job-card.full { grid-column: 1/-1; }
  .job-card-title { color: var(--muted); font-size: 11px; text-transform: uppercase;
                    letter-spacing: .05em; margin-bottom: 8px; font-weight: 600; }
  .job-card table { border: 0; border-radius: 0; font-size: 13px; }
  .job-card td { padding: 5px 0; white-space: normal; vertical-align: top; }
  .job-card td:first-child { width: 105px; color: var(--muted); padding-right: 12px; white-space: nowrap; }
  .job-card td:last-child { min-width: 0; overflow-wrap: anywhere; word-break: break-word; }
  .job-card code { white-space: normal; overflow-wrap: anywhere; word-break: break-word; }
  .job-reason { color: var(--warn); background: rgba(240,169,63,.1);
                border: 1px solid rgba(240,169,63,.3); border-radius: 6px;
                padding: 8px 12px; margin-bottom: 14px; }
</style>
</head>
<body>
<header>
  <h1>&#9881; Slurm Dashboard</h1>
  <div class="meta" id="snap-meta">snapshot taken at __GENERATED_AT__</div>
  <div class="header-actions">
    <button class="theme-btn" id="theme-toggle" title="Toggle light/dark">🌙</button>
    <label class="auto-refresh">Auto refresh
      <select id="refresh-interval" aria-label="Automatic refresh interval">
        <option value="0">Manual</option>
        <option value="15">15 seconds</option>
        <option value="30">30 seconds</option>
        <option value="60">1 minute</option>
        <option value="120">2 minutes</option>
        <option value="300">5 minutes</option>
      </select>
    </label>
    <button class="reload" id="dashboard-refresh">&#x21bb; Refresh now</button>
  </div>
</header>
<main>
  <div class="three-col">

    <aside class="left-col">
      <h2>Cluster summary</h2>
      <div class="cards" id="summary-cards"></div>

      <h2>GPUs by type</h2>
      <table id="gpu-table">
        <thead><tr>
          <th class="no-sort toggle-cell"><span id="gpu-th-toggle" title="Expand/collapse all">▶</span></th>
          <th data-k="type"     data-label="Type">Type</th>
          <th data-k="alloc"    data-label="Alloc">Alloc</th>
          <th data-k="idle"     data-label="Idle">Idle</th>
          <th data-k="total"    data-label="Total">Total</th>
          <th data-k="idle_pct" data-label="Idle%">Idle%</th>
          <th data-k="nodes"    data-label="Nodes">Nodes</th>
        </tr></thead>
        <tbody></tbody>
      </table>

      <h2>Storage quota
        <button id="quota-refresh-btn" class="quota-refresh" title="Load storage quota" aria-label="Load storage quota">&#x21bb;</button>
      </h2>
      <div id="quota-panel"><p class="muted" style="font-size:12px">Click ↻ to load disk and file quotas.</p></div>
    </aside>
    <div class="col-resizer" id="resizer-left" title="Drag to resize"></div>
    <section class="center-col">
      <h2>Partitions</h2>
      <p class="hint">
        Click a row to expand its nodes &nbsp;·&nbsp;
        Click a column header to sort &nbsp;·&nbsp;
        <kbd>Shift</kbd>+click to add a secondary sort key
      </p>
      <div class="filterbar">
        <label>Min VRAM
          <input id="vram-min" type="number" min="0" placeholder="GB" style="width:72px;margin-left:5px">
        </label>
        <label><input type="checkbox" id="idle-only">&nbsp;Idle GPUs only</label>
        <span class="muted" id="part-count"></span>
      </div>
      <table id="part-table">
        <thead><tr>
          <th class="no-sort toggle-cell" style="width:52px">
            <span id="part-th-toggle" title="Expand/collapse all">▶</span> <span id="part-th-refresh" class="th-refresh" title="Refresh all partitions">&#x21bb;</span>
          </th>
          <th data-k="name"          data-label="Partition">Partition</th>
          <th data-k="avail"         data-label="Avail">Avail</th>
          <th data-k="timelimit"     data-label="Time limit">Time limit</th>
          <th data-k="nodes"         data-label="Nodes">Nodes</th>
          <th data-k="jobs_pending"  data-label="Jobs (run/pend)">Jobs (run/pend)</th>
          <th data-k="cpu_total"     data-label="CPU (idle/total)">CPU (idle/total)</th>
          <th data-k="gpu_vram_gb"   data-label="VRAM (GB)">VRAM (GB)</th>
          <th data-k="gpu_idle"      data-label="GPU (idle/total)">GPU (idle/total)</th>
        </tr></thead>
        <tbody id="part-tbody"></tbody>
      </table>
    </section>
    <div class="col-resizer" id="resizer-right" title="Drag to resize"></div>
    <aside class="right-col">
      <h2>My Jobs <span id="my-jobs-user" class="muted" style="font-size:12px;font-weight:400;text-transform:none;letter-spacing:0"></span></h2>
      <div class="jobs-section">
        <h3>Active Queue <span id="aq-refresh-btn" class="th-refresh" title="Refresh">&#x21bb;</span></h3>
        <div class="jobs-scroll" id="aq-panel"></div>
      </div>
      <div class="row-resizer" id="resizer-jobs" title="Drag to resize"></div>
      <div class="jobs-section">
        <h3>History <span id="hist-refresh-btn" class="th-refresh" title="Refresh">&#x21bb;</span> <span class="muted" style="font-size:11px;text-transform:none;letter-spacing:0">last 7 days</span></h3>
        <div class="jobs-scroll" id="hist-panel"><p class="muted" style="font-size:13px;margin:4px 0">Loading…</p></div>
      </div>
    </aside>

  </div>
</main>
<footer>slurmboard &middot; data sourced live from <code>sinfo</code> / <code>scontrol</code> on this login node</footer>

<div class="job-modal" id="job-modal" role="dialog" aria-modal="true" aria-labelledby="job-dialog-title">
  <div class="job-dialog">
    <div class="job-dialog-head">
      <div class="job-dialog-title" id="job-dialog-title">Job details</div>
      <button class="job-dialog-close" id="job-dialog-close" aria-label="Close job details">×</button>
    </div>
    <div class="job-dialog-body" id="job-dialog-body"></div>
  </div>
</div>

<script>
let SNAPSHOT = __SNAPSHOT_JSON__;

// ── refresh (partial or full, without losing expand/sort state) ─────────────
const refreshingParts = new Set();

async function refreshData(partName) {
  const key = partName || '*';
  if (refreshingParts.has(key)) return;
  refreshingParts.add(key);
  const hdrBtn = document.getElementById('part-th-refresh');
  if (hdrBtn) hdrBtn.classList.add('loading');
  if (partName) renderPartitions(); // show spinner on that row only

  try {
    const url = partName
      ? `/data/partition/${encodeURIComponent(partName)}?refresh=1`
      : '/data?refresh=1';
    const resp = await fetch(url);
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    const newSnap = await resp.json();
    if (newSnap.error) throw new Error(newSnap.error);

    if (partName) {
      mergePartitionDetails(partName, newSnap);
    } else {
      const activeQueueLoaded = SNAPSHOT.active_queue_loaded;
      const userJobsLoaded = SNAPSHOT.user_jobs_loaded;
      SNAPSHOT = {...newSnap, active_queue: SNAPSHOT.active_queue, user_jobs: SNAPSHOT.user_jobs};
      SNAPSHOT.active_queue_loaded = activeQueueLoaded;
      SNAPSHOT.user_jobs_loaded = userJobsLoaded;
      updateSnapshotMeta(newSnap.generated_at);
      renderSummary(SNAPSHOT.summary);
      renderGpuTable(SNAPSHOT.summary.gpu_by_type);
      renderPartitions();
    }
  } catch(e) {
    console.error('slurmboard refresh failed:', e);
  }

  refreshingParts.delete(key);
  if (hdrBtn) hdrBtn.classList.remove('loading');
  if (partName) {
    patchPartRow(partName);
  }
}

function mergePartitionDetails(partName, data) {
  const part = SNAPSHOT.partitions.find(p => p.name === partName);
  if (part && data.partition) Object.assign(part, data.partition);
  SNAPSHOT.nodes = SNAPSHOT.nodes.filter(n => !n.partitions.includes(partName));
  for (const node of (data.nodes || [])) SNAPSHOT.nodes.push(node);
}

async function loadPartitionNodes(partName) {
  const part = SNAPSHOT.partitions.find(p => p.name === partName);
  if (!part || part.nodes_loaded || refreshingParts.has(partName)) return;
  part.detail_error = null;
  refreshingParts.add(partName);
  renderPartitions();
  try {
    const resp = await fetch(`/data/partition/${encodeURIComponent(partName)}`);
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    const data = await resp.json();
    if (data.error) throw new Error(data.error);
    mergePartitionDetails(partName, data);
  } catch (e) {
    part.detail_error = e.message;
    console.error('partition node load failed:', e);
  }
  refreshingParts.delete(partName);
  renderPartitions();
}

async function loadPartitionJobs(partName, requestedKind='jobs', refresh=false) {
  const part = SNAPSHOT.partitions.find(p => p.name === partName);
  if (!part || refreshingParts.has(partName + ':jobs')) return;
  if (part.jobs_loaded && !refresh) return;
  part.jobs_error = null;
  refreshingParts.add(partName + ':jobs');
  renderPartitions();
  try {
    const suffix = refresh ? '?refresh=1' : '';
    const resp = await fetch(`/data/partition/${encodeURIComponent(partName)}/jobs${suffix}`);
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    const data = await resp.json();
    if (data.error) throw new Error(data.error);
    Object.assign(part, data);
    if (requestedKind === 'jobs') {
      expandState[partName] = part.jobs_running > 0 ? 'running' : 'pending';
    }
  } catch (e) {
    part.jobs_error = e.message;
    console.error('partition job load failed:', e);
  }
  refreshingParts.delete(partName + ':jobs');
  renderPartitions();
}

function togglePartitionNodes(partName) {
  const part = SNAPSHOT.partitions.find(p => p.name === partName);
  if (!part) return;
  if (expandState[partName] === 'nodes') delete expandState[partName];
  else {
    expandState[partName] = 'nodes';
    if (!part.nodes_loaded) loadPartitionNodes(partName);
  }
  renderPartitions();
}

function togglePartitionJobs(partName, kind) {
  const part = SNAPSHOT.partitions.find(p => p.name === partName);
  if (!part) return;
  if (kind !== 'jobs' && expandState[partName] === kind) {
    delete expandState[partName];
    renderPartitions();
    return;
  }
  expandState[partName] = kind === 'jobs' ? 'running' : kind;
  if (!part.jobs_loaded) loadPartitionJobs(partName, kind);
  renderPartitions();
}

async function refreshActiveQueue(force=false) {
  const btn = document.getElementById('aq-refresh-btn');
  if (btn) btn.classList.add('loading');
  try {
    const resp = await fetch('/data/activequeue' + (force ? '?refresh=1' : ''));
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    const data = await resp.json();
    if (data.error) throw new Error(data.error);
    SNAPSHOT.active_queue = data.active_queue;
    SNAPSHOT.active_queue_loaded = true;
    renderActiveQueue();
  } catch(e) {
    console.error('activequeue refresh failed:', e);
  }
  if (btn) btn.classList.remove('loading');
}

async function refreshHistoryJobs(force=false) {
  const btn = document.getElementById('hist-refresh-btn');
  if (btn) btn.classList.add('loading');
  try {
    const resp = await fetch('/data/userjobs' + (force ? '?refresh=1' : ''));
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    const data = await resp.json();
    if (data.error) throw new Error(data.error);
    SNAPSHOT.user_jobs = data.user_jobs;
    SNAPSHOT.user_jobs_loaded = true;
    renderHistoryJobs();
  } catch(e) {
    console.error('userjobs refresh failed:', e);
  }
  if (btn) btn.classList.remove('loading');
}

// ── storage quota (lazy because cluster quota helpers can be slow) ─────────
let QUOTA_DATA = null;
let quotaLoading = false;

function appendQuotaUsage(cell, usedLabel, limitLabel, percent) {
  const value = document.createElement('div');
  value.className = 'quota-value';
  value.textContent = `${usedLabel} / ${limitLabel}`;
  cell.appendChild(value);
  if (percent == null) return;
  const meter = document.createElement('div');
  meter.className = `quota-meter ${percent >= 90 ? 'crit' : percent >= 70 ? 'high' : ''}`;
  meter.title = `${percent}% used`;
  const fill = document.createElement('span');
  fill.style.width = `${Math.min(percent, 100)}%`;
  meter.appendChild(fill);
  cell.appendChild(meter);
}

function renderStorageQuota() {
  const panel = document.getElementById('quota-panel');
  panel.replaceChildren();
  if (!QUOTA_DATA) {
    const prompt = document.createElement('p');
    prompt.className = 'muted';
    prompt.style.fontSize = '12px';
    prompt.textContent = 'Click ↻ to load disk and file quotas.';
    panel.appendChild(prompt);
    return;
  }
  if (QUOTA_DATA.error) {
    const error = document.createElement('p');
    error.className = 'quota-error';
    error.textContent = QUOTA_DATA.error;
    panel.appendChild(error);
    return;
  }
  if (QUOTA_DATA.rows && QUOTA_DATA.rows.length) {
    const table = document.createElement('table');
    table.id = 'quota-table';
    table.innerHTML = '<thead><tr><th>Storage</th><th>Space</th><th>Files</th></tr></thead>';
    const body = document.createElement('tbody');
    for (const row of QUOTA_DATA.rows) {
      const tr = document.createElement('tr');
      const location = document.createElement('td');
      const scope = document.createElement('div');
      scope.className = 'quota-scope';
      scope.textContent = row.scope;
      scope.title = row.scope;
      const path = document.createElement('div');
      path.className = 'quota-path';
      path.textContent = row.path;
      path.title = row.path;
      location.append(scope, path);
      const space = document.createElement('td');
      appendQuotaUsage(space, row.space_used_label, row.space_limit_label, row.space_percent);
      const files = document.createElement('td');
      appendQuotaUsage(files, row.files_used_label, row.files_limit_label, row.files_percent);
      tr.append(location, space, files);
      body.appendChild(tr);
    }
    table.appendChild(body);
    panel.appendChild(table);
  } else if (QUOTA_DATA.raw) {
    const raw = document.createElement('pre');
    raw.className = 'quota-raw';
    raw.textContent = QUOTA_DATA.raw;
    panel.appendChild(raw);
  } else {
    const empty = document.createElement('p');
    empty.className = 'muted';
    empty.textContent = 'No quota entries were returned.';
    panel.appendChild(empty);
  }
  const meta = document.createElement('div');
  meta.className = 'quota-meta';
  meta.textContent = `${QUOTA_DATA.source || 'quota'} · ${QUOTA_DATA.generated_at}`;
  panel.appendChild(meta);
}

async function refreshStorageQuota(force=true) {
  if (quotaLoading) return;
  quotaLoading = true;
  const button = document.getElementById('quota-refresh-btn');
  button.classList.add('loading');
  try {
    const response = await fetch('/data/quota' + (force ? '?refresh=1' : ''));
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
    QUOTA_DATA = data;
  } catch (error) {
    QUOTA_DATA = {error: error.message};
    console.error('storage quota refresh failed:', error);
  } finally {
    quotaLoading = false;
    button.classList.remove('loading');
    renderStorageQuota();
  }
}

// ── configurable dashboard auto-refresh ───────────────────────────────────
const REFRESH_COOKIE = 'slurmboard_refresh_seconds';
const REFRESH_CHOICES = [0, 15, 30, 60, 120, 300];
const DEFAULT_REFRESH_SECONDS = 60;
let refreshIntervalSeconds = DEFAULT_REFRESH_SECONDS;
let refreshTimer = null;
let dashboardRefreshRunning = false;

function readRefreshPreference() {
  const prefix = REFRESH_COOKIE + '=';
  const item = document.cookie.split(';').map(v => v.trim()).find(v => v.startsWith(prefix));
  const seconds = item ? Number(item.slice(prefix.length)) : DEFAULT_REFRESH_SECONDS;
  return REFRESH_CHOICES.includes(seconds) ? seconds : DEFAULT_REFRESH_SECONDS;
}

function writeRefreshPreference(seconds) {
  document.cookie = `${REFRESH_COOKIE}=${seconds}; Max-Age=31536000; Path=/; SameSite=Strict`;
}

function refreshLabel(seconds) {
  if (!seconds) return 'manual refresh';
  const option = document.querySelector(`#refresh-interval option[value="${seconds}"]`);
  return `auto refresh every ${option ? option.textContent.toLowerCase() : seconds + ' seconds'}`;
}

function updateSnapshotMeta(generatedAt=SNAPSHOT.generated_at) {
  const meta = document.getElementById('snap-meta');
  if (meta) meta.textContent = `snapshot taken at ${generatedAt} · ${refreshLabel(refreshIntervalSeconds)}`;
}

async function refreshDashboard() {
  if (dashboardRefreshRunning) return;
  dashboardRefreshRunning = true;
  try {
    await refreshData();
    // Once the user has loaded the active queue, keep that lightweight view fresh too.
    if (SNAPSHOT.active_queue_loaded) await refreshActiveQueue(true);
  } finally {
    dashboardRefreshRunning = false;
  }
}

function setRefreshInterval(seconds, persist=true) {
  refreshIntervalSeconds = REFRESH_CHOICES.includes(seconds) ? seconds : DEFAULT_REFRESH_SECONDS;
  const select = document.getElementById('refresh-interval');
  select.value = String(refreshIntervalSeconds);
  if (refreshTimer) clearInterval(refreshTimer);
  refreshTimer = refreshIntervalSeconds > 0
    ? setInterval(() => { if (!document.hidden) refreshDashboard(); }, refreshIntervalSeconds * 1000)
    : null;
  if (persist) writeRefreshPreference(refreshIntervalSeconds);
  updateSnapshotMeta();
}

// ── helpers ────────────────────────────────────────────────────────────────
function pct(a, t) { return t > 0 ? Math.round(a / t * 100) : 0; }
function fmtMem(mb) {
  if (mb >= 1024 * 1024) return (mb / (1024 * 1024)).toFixed(1) + ' TB';
  if (mb >= 1024)        return (mb / 1024).toFixed(1) + ' GB';
  return mb + ' MB';
}
function barClass(p) { return p >= 90 ? 'crit' : p >= 70 ? 'high' : ''; }
// For idle-ratio bars: low idle = warn/crit; normal idle = green (gpu class)
function idleBarClass(p) { return p <= 10 ? 'crit' : p <= 30 ? 'high' : 'gpu'; }
function statePill(state) {
  const s = state.toLowerCase();
  const cls = s.includes('idle') ? 'idle'
    : (s.includes('mix') || s.includes('alloc')) ? 'mixed'
    : (s.includes('down') || s.includes('drain') || s.includes('fail') || s.includes('maint')) ? 'down'
    : s.includes('up') ? 'up' : 'other';
  return `<span class="pill ${cls}">${state}</span>`;
}
function minibar(pct, cls='') {
  return `<span class="minibar ${cls}"><span style="width:${pct}%"></span></span>`;
}

// ── job detail modal ───────────────────────────────────────────────────────
const jobModal = document.getElementById('job-modal');
const jobDialogTitle = document.getElementById('job-dialog-title');
const jobDialogBody = document.getElementById('job-dialog-body');

function escapeHtml(value) {
  return String(value == null ? '' : value).replace(/[&<>"']/g, ch => ({
    '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'
  })[ch]);
}

function jobValue(value) {
  return value == null || value === '' || value === '(null)' || value === 'N/A'
    ? '—' : escapeHtml(value);
}

function jobCard(title, rows, full=false) {
  const body = rows.filter(([, value]) => value != null && value !== '' && value !== '(null)' && value !== 'N/A')
    .map(([label, value, code]) => `<tr><td>${escapeHtml(label)}</td><td>${code ? `<code>${jobValue(value)}</code>` : jobValue(value)}</td></tr>`)
    .join('');
  if (!body) return '';
  return `<section class="job-card${full ? ' full' : ''}"><div class="job-card-title">${escapeHtml(title)}</div><table><tbody>${body}</tbody></table></section>`;
}

function closeJobModal() {
  jobModal.classList.remove('open');
}

async function openJobModal(jobID) {
  jobDialogTitle.textContent = `Job ${jobID}`;
  jobDialogBody.innerHTML = '<div class="muted" style="padding:22px 0;text-align:center">Loading…</div>';
  jobModal.classList.add('open');
  try {
    const response = await fetch(`/api/job/${encodeURIComponent(jobID)}`);
    const payload = await response.json();
    if (!response.ok || payload.error) throw new Error(payload.error || `HTTP ${response.status}`);
    const info = payload.job;
    const state = (info.state || 'UNKNOWN').toUpperCase();
    const stateClass = ['running','completing','pending','completed','failed','cancelled','timeout','node_fail']
      .includes(state.toLowerCase()) ? state.toLowerCase() : 'other';
    const gpu = info.gpu_count ? `${info.gpu_count}× ${info.gpu_type || 'gpu'}` : null;
    const reason = info.reason && info.reason !== 'None' && info.reason !== '(null)'
      ? `<div class="job-reason">Reason: ${escapeHtml(info.reason)}</div>` : '';
    jobDialogTitle.innerHTML = `Job ${escapeHtml(jobID)} <span class="pill ${stateClass}">${escapeHtml(state)}</span>`;
    jobDialogBody.innerHTML = `
      <div class="job-dialog-name">${jobValue(info.job_name)}</div>${reason}
      <div class="job-grid">
        ${jobCard('Identity', [['User',info.user],['Account',info.account],['QOS',info.qos],['Partition',info.partition],['Priority',info.priority],['Exit code',info.exit_code]])}
        ${jobCard('Resources', [['Nodes',info.num_nodes],['CPUs',info.num_cpus],['Tasks',info.num_tasks],['CPUs / task',info.cpus_task],['GPUs',gpu],['Memory',info.mem_cpu || info.mem_node],['TRES',info.tres]])}
        ${jobCard('Timing', [['Submit',info.submit_time],['Start',info.start_time],['End',info.end_time],['Run time',info.runtime],['Time limit',info.timelimit]])}
        ${jobCard('Nodes', [['Node list',info.nodelist],['Batch host',info.batch_host]])}
        ${jobCard('Paths', [['Work dir',info.workdir,true],['Command',info.command,true],['Stdout',info.stdout,true],['Stderr',info.stderr,true]], true)}
      </div>`;
  } catch (error) {
    jobDialogBody.innerHTML = `<div class="job-dialog-error">${escapeHtml(error.message)}</div>`;
  }
}

document.addEventListener('click', event => {
  const link = event.target.closest('a.uj-id');
  if (!link || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  event.preventDefault();
  openJobModal(link.textContent.trim());
});
document.getElementById('job-dialog-close').addEventListener('click', closeJobModal);
jobModal.addEventListener('click', event => { if (event.target === jobModal) closeJobModal(); });
document.addEventListener('keydown', event => { if (event.key === 'Escape' && jobModal.classList.contains('open')) closeJobModal(); });

// ── multi-column sort ───────────────────────────────────────────────────────
// Array of {key, dir} objects; first entry = primary sort.
const partSortList = [{key: 'name', dir: 1}];
// expandState[partName] = "nodes"|"running"|"pending"; absent = closed.
// Only one panel open per partition at a time.
const expandState = {};

function multiSort(rows, list) {
  if (!list.length) return rows;
  return [...rows].sort((a, b) => {
    for (const {key, dir} of list) {
      let av = a[key], bv = b[key];
      if (Array.isArray(av)) { av = av.join(','); bv = (bv || []).join(','); }
      if (typeof av === 'string') { av = av.toLowerCase(); bv = (bv || '').toLowerCase(); }
      if (av == null) av = -Infinity;
      if (bv == null) bv = -Infinity;
      if (av < bv) return -dir;
      if (av > bv) return  dir;
    }
    return 0;
  });
}

function updatePartHeaders() {
  const badges = ['①','②','③','④','⑤'];
  document.querySelectorAll('#part-table th[data-k]').forEach(th => {
    const key   = th.dataset.k;
    const label = th.dataset.label;
    const idx   = partSortList.findIndex(s => s.key === key);
    if (idx < 0) { th.textContent = label; return; }
    const arrow = partSortList[idx].dir > 0 ? ' ↑' : ' ↓';
    const badge = partSortList.length > 1 ? ' ' + (badges[idx] || String(idx + 1)) : '';
    th.textContent = label + arrow + badge;
  });
}

function wirePartHeaders() {
  // header refresh button
  document.getElementById('part-th-refresh').addEventListener('click', () => {
    refreshData();
  });

  // Header toggle collapses all; expanding opens one partition at a time so a
  // large cluster never triggers a burst of node-detail queries.
  document.getElementById('part-th-toggle').addEventListener('click', () => {
    const vramMin  = parseInt(document.getElementById('vram-min').value) || 0;
    const idleOnly = document.getElementById('idle-only').checked;
    const visible  = SNAPSHOT.partitions.filter(p => {
      if (idleOnly && p.gpu_idle <= 0) return false;
      if (vramMin > 0 && (p.gpu_vram_gb == null || p.gpu_vram_gb < vramMin)) return false;
      return true;
    });
    const anyOpen = visible.some(p => expandState[p.name]);
    if (anyOpen) visible.forEach(p => delete expandState[p.name]);
    else if (visible.length) togglePartitionNodes(visible[0].name);
    if (anyOpen || !visible.length) renderPartitions();
  });

  document.querySelectorAll('#part-table th[data-k]').forEach(th => {
    th.addEventListener('click', e => {
      const key = th.dataset.k;
      const idx = partSortList.findIndex(s => s.key === key);
      if (e.shiftKey) {
        // add / toggle in multi-sort
        if (idx >= 0) partSortList[idx].dir *= -1;
        else partSortList.push({key, dir: 1});
      } else {
        // replace with single sort; toggle dir if already primary
        const prevDir = (idx === 0 && partSortList.length === 1) ? partSortList[0].dir : 1;
        partSortList.length = 0;
        partSortList.push({key, dir: idx === 0 ? prevDir * -1 : 1});
      }
      renderPartitions();
    });
  });
}

// ── render summary cards ────────────────────────────────────────────────────
function renderSummary(s) {
  const cpuIdle = pct(s.cpu_total - s.cpu_alloc, s.cpu_total);
  const memKnown = s.mem_alloc_mb != null;
  const gpuKnown = s.gpu_alloc != null;
  const memIdle = memKnown ? pct(s.mem_total_mb - s.mem_alloc_mb, s.mem_total_mb) : null;
  const gpuIdle = gpuKnown ? pct(s.gpu_total - s.gpu_alloc, s.gpu_total) : null;
  const states = Object.entries(s.node_states).sort((a,b) => b[1]-a[1])
      .map(([k,v]) => `${v} ${k.toLowerCase()}`).join(', ');
  document.getElementById('summary-cards').innerHTML = `
    <div class="card">
      <div class="label">Nodes</div>
      <div class="value">${s.node_count}</div>
      <div class="sub">${states || '—'}</div>
    </div>
    <div class="card">
      <div class="label">CPUs</div>
      <div class="value">${s.cpu_total - s.cpu_alloc} / ${s.cpu_total}</div>
      <div class="sub">${cpuIdle}% idle</div>
      <div class="bar ${idleBarClass(cpuIdle)}"><span style="width:${cpuIdle}%"></span></div>
    </div>
    <div class="card">
      <div class="label">Memory</div>
      <div class="value">${memKnown ? fmtMem(s.mem_total_mb - s.mem_alloc_mb) : '—'} / ${fmtMem(s.mem_total_mb)}</div>
      <div class="sub">${memKnown ? memIdle + '% idle' : 'allocation loads with node details'}</div>
      ${memKnown ? `<div class="bar ${idleBarClass(memIdle)}"><span style="width:${memIdle}%"></span></div>` : ''}
    </div>
    <div class="card">
      <div class="label">GPUs</div>
      <div class="value">${gpuKnown ? s.gpu_total - s.gpu_alloc : '—'} <span style="font-size:13px;font-weight:400;color:var(--muted)">idle / ${s.gpu_total}</span></div>
      <div class="sub">${gpuKnown ? gpuIdle + '% idle' : 'allocation loads per partition'}</div>
      ${gpuKnown ? `<div class="bar ${idleBarClass(gpuIdle)}"><span style="width:${gpuIdle}%"></span></div>` : ''}
    </div>`;
}

// ── light / dark theme toggle ───────────────────────────────────────────────
(function initTheme() {
  const saved = localStorage.getItem('sb_theme');
  if (saved === 'light') document.documentElement.setAttribute('data-theme', 'light');
  document.addEventListener('DOMContentLoaded', () => {
    const btn = document.getElementById('theme-toggle');
    if (!btn) return;
    const update = () => {
      const isLight = document.documentElement.getAttribute('data-theme') === 'light';
      btn.textContent = isLight ? '🌙' : '☀️';
    };
    update();
    btn.addEventListener('click', () => {
      const isLight = document.documentElement.getAttribute('data-theme') === 'light';
      if (isLight) {
        document.documentElement.removeAttribute('data-theme');
        localStorage.setItem('sb_theme', 'dark');
      } else {
        document.documentElement.setAttribute('data-theme', 'light');
        localStorage.setItem('sb_theme', 'light');
      }
      update();
    });
  });
})();

// ── render GPU-by-type table ────────────────────────────────────────────────
const gpuTypeExpanded = {};

function buildGpuPartSubTable(partitions) {
  const rows = Object.entries(partitions)
    .sort((a, b) => b[1].total - a[1].total)
    .map(([pname, pv]) => {
      const known = pv.alloc != null;
      const idlePct = known ? pct(pv.total - pv.alloc, pv.total) : null;
      return `<tr>
        <td><b>${pname}</b></td>
        <td>${known ? pv.alloc : '—'}</td>
        <td>${known ? pv.total - pv.alloc : '—'}</td>
        <td>${pv.total}</td>
        <td>${known ? minibar(idlePct, 'gpu') + idlePct + '%' : '<span class="muted">load partition</span>'}</td>
      </tr>`;
    }).join('');
  return `<div class="inner-wrap"><table class="inner-table">
    <thead><tr>
      <th>Partition</th><th>Alloc</th><th>Idle</th><th>Total</th><th>Idle%</th>
    </tr></thead>
    <tbody>${rows || '<tr><td colspan="5" class="muted" style="padding:8px 10px">No partitions.</td></tr>'}</tbody>
  </table></div>`;
}

let gpuSortKey = 'total', gpuSortDir = -1;

function updateGpuHeaders() {
  document.querySelectorAll('#gpu-table th[data-k]').forEach(th => {
    const key = th.dataset.k, label = th.dataset.label;
    th.textContent = key === gpuSortKey
      ? label + (gpuSortDir > 0 ? ' ↑' : ' ↓')
      : label;
  });
  const toggle = document.getElementById('gpu-th-toggle');
  if (toggle) {
    const anyOpen = Object.keys(gpuTypeExpanded).length > 0;
    toggle.textContent = anyOpen ? '▼' : '▶';
  }
}

function wireGpuHeaders() {
  document.querySelectorAll('#gpu-table th[data-k]').forEach(th =>
    th.addEventListener('click', () => {
      const key = th.dataset.k;
      if (gpuSortKey === key) gpuSortDir *= -1;
      else { gpuSortKey = key; gpuSortDir = -1; }
      renderGpuTable(SNAPSHOT.summary.gpu_by_type);
    }));
  document.getElementById('gpu-th-toggle').addEventListener('click', () => {
    const types = Object.keys(SNAPSHOT.summary.gpu_by_type);
    const anyOpen = types.some(t => gpuTypeExpanded[t]);
    if (anyOpen) types.forEach(t => delete gpuTypeExpanded[t]);
    else         types.forEach(t => { gpuTypeExpanded[t] = true; });
    renderGpuTable(SNAPSHOT.summary.gpu_by_type);
  });
}

function renderGpuTable(byType) {
  const tbody = document.querySelector('#gpu-table tbody');
  let entries = Object.entries(byType).map(([type, v]) => ({
    type, v,
    idle: v.alloc != null ? v.total - v.alloc : null,
    idle_pct: v.alloc != null ? pct(v.total - v.alloc, v.total) : null
  }));
  entries.sort((a, b) => {
    let av = a[gpuSortKey] ?? a.v[gpuSortKey];
    let bv = b[gpuSortKey] ?? b.v[gpuSortKey];
    if (typeof av === 'string') return gpuSortDir * av.localeCompare(bv);
    return gpuSortDir * ((av ?? 0) - (bv ?? 0));
  });
  updateGpuHeaders();
  if (!entries.length) {
    tbody.innerHTML = '<tr><td colspan="7" class="muted">No GPUs detected.</td></tr>';
    return;
  }
  tbody.innerHTML = '';
  for (const {type, v} of entries) {
    const known = v.alloc != null;
    const idlePct = known ? pct(v.total - v.alloc, v.total) : null;
    const open = !!gpuTypeExpanded[type];

    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td class="toggle-cell">${open ? '▼' : '▶'}</td>
      <td><b class="part-name-link gpu-type-link">${type}</b></td>
      <td>${known ? v.alloc : '—'}</td>
      <td>${known ? v.total - v.alloc : '—'}</td>
      <td>${v.total}</td>
      <td>${known ? minibar(idlePct, 'gpu') + idlePct + '%' : '<span class="muted">lazy</span>'}</td>
      <td>${v.nodes}</td>`;
    tr.querySelector('.toggle-cell').addEventListener('click', () => {
      if (gpuTypeExpanded[type]) delete gpuTypeExpanded[type];
      else gpuTypeExpanded[type] = true;
      renderGpuTable(SNAPSHOT.summary.gpu_by_type);
    });
    tr.querySelector('.gpu-type-link').addEventListener('click', () => {
      if (gpuTypeExpanded[type]) delete gpuTypeExpanded[type];
      else gpuTypeExpanded[type] = true;
      renderGpuTable(SNAPSHOT.summary.gpu_by_type);
    });
    tbody.appendChild(tr);

    if (open) {
      const expandTr = document.createElement('tr');
      expandTr.className = 'gpu-expand-row';
      const td = document.createElement('td');
      td.colSpan = 7;
      td.innerHTML = buildGpuPartSubTable(v.partitions || {});
      expandTr.appendChild(td);
      tbody.appendChild(expandTr);
    }
  }
}

// ── node sub-table (inside expanded partition row) ──────────────────────────
function buildNodeSubTable(partName) {
  const partition = SNAPSHOT.partitions.find(p => p.name === partName);
  if (partition && !partition.nodes_loaded) {
    if (partition.detail_error)
      return '<div style="padding:10px;color:var(--bad)">Could not load node details for this partition.</div>';
    return '<div style="padding:10px;color:var(--muted)">Loading node details for this partition…</div>';
  }
  const nodes = SNAPSHOT.nodes
    .filter(n => n.partitions.includes(partName))
    .sort((a, b) => b.gpu_idle - a.gpu_idle || a.name.localeCompare(b.name));
  if (!nodes.length)
    return '<div style="padding:10px;color:var(--muted)">No nodes in this partition.</div>';

  const rows = nodes.map(n => {
    const cpuIdleP = pct(n.cpu_total - n.cpu_alloc, n.cpu_total);
    const memIdleP = pct(n.mem_total_mb - n.mem_alloc_mb, n.mem_total_mb);
    const gpuCell = n.gpu_total
      ? minibar(pct(n.gpu_idle, n.gpu_total), 'gpu') + `${n.gpu_idle} / ${n.gpu_total}`
      : '<span class="muted">—</span>';
    const vram = n.gpu_vram_gb != null ? n.gpu_vram_gb + ' GB' : '—';
    return `<tr>
      <td><b>${n.name}</b></td>
      <td>${statePill(n.state)}</td>
      <td>${minibar(cpuIdleP, 'gpu')}${n.cpu_total - n.cpu_alloc} / ${n.cpu_total}</td>
      <td>${n.load != null ? n.load : '—'}</td>
      <td>${minibar(memIdleP, 'gpu')}${fmtMem(n.mem_total_mb - n.mem_alloc_mb)} / ${fmtMem(n.mem_total_mb)}</td>
      <td>${gpuCell}</td>
      <td class="muted">${vram}</td>
    </tr>`;
  }).join('');

  return `<div class="inner-wrap"><table class="inner-table">
    <thead><tr>
      <th>Node</th><th>State</th><th>CPU (idle/total)</th><th>Load</th>
      <th>Memory (idle/total)</th><th>GPU (idle/total)</th><th>VRAM</th>
    </tr></thead>
    <tbody>${rows}</tbody>
  </table></div>`;
}

// ── job sub-table (inside expanded running/pending section) ────────────────
function buildJobSubTable(jobs, isPending) {
  if (!jobs.length) {
    return '<div style="padding:8px 0;color:var(--muted);font-size:13px">No jobs.</div>';
  }
  const timeHeader   = isPending ? 'Queued' : 'Running';
  const reasonHeader = isPending ? 'Reason' : 'Nodes';
  const rows = jobs.map(j => {
    const gresCell = j.gres ? `<span class="muted">${j.gres}</span>` : '<span class="muted">—</span>';
    return `<tr>
      <td><a class="uj-id" href="/job/${j.id}">${j.id}</a></td>
      <td>${j.user}</td>
      <td style="max-width:160px;overflow:hidden;text-overflow:ellipsis" title="${j.name}">${j.name}</td>
      <td>${j.cpus}</td>
      <td>${gresCell}</td>
      <td>${j.time}</td>
      <td class="muted" style="max-width:200px;overflow:hidden;text-overflow:ellipsis" title="${j.reason}">${j.reason}</td>
    </tr>`;
  }).join('');
  return `<div class="inner-wrap"><table class="inner-table">
    <thead><tr>
      <th>Job ID</th><th>User</th><th>Name</th><th>CPUs</th><th>GPUs</th>
      <th>${timeHeader}</th><th>${reasonHeader}</th>
    </tr></thead>
    <tbody>${rows}</tbody>
  </table></div>`;
}

// ── render partition table ──────────────────────────────────────────────────
function buildPartCells(p) {
  const cpuIdleP = pct(p.cpu_total - p.cpu_alloc, p.cpu_total);
  const gpuKnown = p.gpu_idle != null;
  const idleP    = gpuKnown ? pct(p.gpu_idle, p.gpu_total) : null;
  const gpuCell  = p.gpu_total
    ? (gpuKnown
       ? minibar(idleP, 'gpu') + `${p.gpu_idle} / ${p.gpu_total}`
       : `<span class="muted">— / ${p.gpu_total}</span>`)
    : '<span class="muted">—</span>';
  const vramCell = p.gpu_vram_gb != null
    ? `<b>${p.gpu_vram_gb}</b> GB`
    : '<span class="muted">—</span>';
  const jobsCell = p.jobs_loaded
    ? `<span class="job-toggle" data-kind="running"
        style="color:var(--good);cursor:pointer;border-bottom:1px dotted var(--good)">
        <span class="job-num">${p.jobs_running}</span> run</span>
       <span class="muted"> · </span>
       <span class="job-toggle" data-kind="pending"
        style="color:var(--warn);cursor:pointer;border-bottom:1px dotted var(--warn)">
        <span class="job-num">${p.jobs_pending}</span> pend</span>`
    : `<span class="job-toggle muted" data-kind="jobs"
        style="cursor:pointer;border-bottom:1px dotted var(--muted)">load jobs</span>`;
  return `
    <td><b class="part-name-link">${p.name}</b></td>
    <td>${p.avail}</td>
    <td>${p.timelimit}</td>
    <td>${p.nodes}</td>
    <td style="white-space:nowrap">${jobsCell}</td>
    <td>${minibar(cpuIdleP, 'gpu')}${p.cpu_total - p.cpu_alloc} / ${p.cpu_total}</td>
    <td>${vramCell}</td>
    <td>${gpuCell}</td>`;
}

function patchPartRow(partName) {
  const p = SNAPSHOT.partitions.find(p => p.name === partName);
  if (!p) return;
  const tr = document.querySelector(`tr.part-row[data-part="${partName}"]`);
  if (!tr) return;
  const cur = expandState[partName];

  // Update toggle-cell: reset ↻ spinner
  const isRefreshing = refreshingParts.has(partName) ||
                       refreshingParts.has(partName + ':jobs') ||
                       refreshingParts.has('*');
  const refreshHtml = isRefreshing
    ? '<span class="row-refresh loading" title="Refreshing…">&#x21bb;</span>'
    : `<span class="row-refresh" data-part="${partName}" title="Refresh partition">&#x21bb;</span>`;
  const toggleCell = tr.querySelector('.toggle-cell');
  if (toggleCell) {
    toggleCell.innerHTML = `${cur ? '▼' : '▶'} ${refreshHtml}`;
    toggleCell.addEventListener('click', () => {
      if (expandState[partName]) {
        delete expandState[partName];
        renderPartitions();
      } else togglePartitionNodes(partName);
    });
    const rowBtn = toggleCell.querySelector('.row-refresh[data-part]');
    if (rowBtn) rowBtn.addEventListener('click', (e) => {
      e.stopPropagation(); refreshData(partName);
    });
  }

  // Replace data cells (all tds after the toggle-cell)
  const tds = tr.querySelectorAll('td');
  const tmp = document.createElement('tr');
  tmp.innerHTML = buildPartCells(p);
  const newTds = tmp.querySelectorAll('td');
  for (let i = 0; i < newTds.length; i++) {
    if (tds[i + 1]) tds[i + 1].replaceWith(newTds[i]);
  }

  // Re-wire clicks on newly inserted cells
  tr.querySelectorAll('.job-toggle').forEach(span => {
    span.addEventListener('click', () => {
      togglePartitionJobs(partName, span.dataset.kind);
    });
  });
  tr.querySelector('.part-name-link').addEventListener('click', () => {
    togglePartitionNodes(partName);
  });

  // If expand panel is open, refresh its content too
  const expandTr = tr.nextElementSibling;
  if (expandTr && expandTr.classList.contains('nodes-expand-row') && cur) {
    const td = expandTr.querySelector('td');
    if (td) {
      if (cur === 'nodes') {
        td.innerHTML = buildNodeSubTable(partName);
      } else {
        const jobs = (p.jobs || []).filter(j => j.state === cur.toUpperCase());
        td.innerHTML = buildJobSubTable(jobs, cur === 'pending');
      }
    }
  }
}

function renderPartitions() {
  const vramMin  = parseInt(document.getElementById('vram-min').value) || 0;
  const idleOnly = document.getElementById('idle-only').checked;

  const sorted = multiSort(SNAPSHOT.partitions, partSortList);
  const visible = sorted.filter(p => {
    if (idleOnly && p.gpu_idle <= 0) return false;
    if (vramMin > 0 && (p.gpu_vram_gb == null || p.gpu_vram_gb < vramMin)) return false;
    return true;
  });

  document.getElementById('part-count').textContent =
    visible.length === sorted.length
      ? `${sorted.length} partitions`
      : `${visible.length} / ${sorted.length} partitions`;

  const anyOpen = visible.some(p => expandState[p.name]);
  document.getElementById('part-th-toggle').textContent = anyOpen ? '▼' : '▶';

  const tbody = document.getElementById('part-tbody');
  tbody.innerHTML = '';

  for (const p of visible) {
    const cur = expandState[p.name];   // "nodes"|"running"|"pending"|undefined

    const isRefreshing = refreshingParts.has(p.name) ||
                         refreshingParts.has(p.name + ':jobs') ||
                         refreshingParts.has('*');
    const rowRefreshHtml = isRefreshing
      ? '<span class="row-refresh loading" title="Refreshing…">&#x21bb;</span>'
      : `<span class="row-refresh" data-part="${p.name}" title="Refresh partition">&#x21bb;</span>`;

    const tr = document.createElement('tr');
    tr.className = 'part-row';
    tr.dataset.part = p.name;
    tr.innerHTML = `<td class="toggle-cell">${cur ? '▼' : '▶'} ${rowRefreshHtml}</td>`
                 + buildPartCells(p);

    // row refresh button
    const rowRefreshBtn = tr.querySelector('.row-refresh[data-part]');
    if (rowRefreshBtn) {
      rowRefreshBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        refreshData(p.name);
      });
    }

    // triangle: close if anything open, open nodes if closed
    tr.querySelector('.toggle-cell').addEventListener('click', () => {
      if (cur) {
        delete expandState[p.name];
        renderPartitions();
      } else togglePartitionNodes(p.name);
    });
    // partition name: mutual-exclusion toggle for nodes
    tr.querySelector('.part-name-link').addEventListener('click', () => {
      togglePartitionNodes(p.name);
    });
    // run/pend spans: mutual-exclusion toggle
    tr.querySelectorAll('.job-toggle').forEach(span => {
      span.addEventListener('click', () => {
        togglePartitionJobs(p.name, span.dataset.kind);
      });
    });
    tbody.appendChild(tr);

    // inline expansion panel (only one at a time)
    if (cur) {
      const expandTr = document.createElement('tr');
      expandTr.className = 'nodes-expand-row';
      const td = document.createElement('td');
      td.colSpan = 9;
      if (cur === 'nodes') {
        td.innerHTML = buildNodeSubTable(p.name);
      } else {
        if (p.jobs_error) {
          td.innerHTML = '<div style="padding:10px;color:var(--bad)">Could not load partition jobs.</div>';
        } else if (!p.jobs_loaded) {
          td.innerHTML = '<div style="padding:10px;color:var(--muted)">Loading jobs for this partition…</div>';
        } else {
          const jobs = (p.jobs || []).filter(j => j.state === cur.toUpperCase());
          td.innerHTML = buildJobSubTable(jobs, cur === 'pending');
        }
      }
      expandTr.appendChild(td);
      tbody.appendChild(expandTr);
    }
  }

  updatePartHeaders();
}

// ── shared helpers for job tables ───────────────────────────────────────────
const STATE_ABBR = {
  RUNNING:'R', COMPLETING:'CG', PENDING:'PD', COMPLETED:'CD',
  FAILED:'F', CANCELLED:'CA', TIMEOUT:'TO', NODE_FAIL:'NF', PREEMPTED:'PR',
};

function jobStateCell(state, done) {
  if (done) return `<span style="color:var(--muted);font-weight:700;font-size:11px">${STATE_ABBR[state]||state.slice(0,2)}</span>`;
  const abbr  = STATE_ABBR[state] || state.slice(0,2);
  const color = (state==='RUNNING'||state==='COMPLETING') ? 'var(--good)'
              : state==='PENDING' ? 'var(--warn)' : 'var(--muted)';
  return `<span style="color:${color};font-weight:700;font-size:11px">${abbr}</span>`;
}

function fmtDate(s) {
  if (!s || s === 'N/A' || s === 'Unknown') return '—';
  const d = new Date(s.replace('T', ' '));
  if (isNaN(d)) return s.slice(0, 10);
  return `${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')} `
       + `${String(d.getHours()).padStart(2,'0')}:${String(d.getMinutes()).padStart(2,'0')}`;
}

// ── shared sort/render for job tables ───────────────────────────────────────
function sortJobs(jobs, key, dir) {
  const numId = j => parseInt(j.id) || 0;
  return [...jobs].sort((a, b) => {
    let cmp = 0;
    if      (key === 'id')        cmp = numId(a) - numId(b);
    else if (key === 'time')      cmp = (a.time||'').localeCompare(b.time||'');
    else if (key === 'partition') cmp = (a.partition||'').localeCompare(b.partition||'');
    else if (key === 'submit')    cmp = (a.submit||'').localeCompare(b.submit||'');
    else if (key === 'state')     cmp = (a.state||'').localeCompare(b.state||'');
    return dir * cmp;
  });
}

function makeTh(key, label, curKey, curDir) {
  const arrow = curKey === key ? (curDir > 0 ? ' ↑' : ' ↓') : '';
  return `<th data-k="${key}" style="cursor:pointer;user-select:none">${label}${arrow}</th>`;
}

function jobRow(j, showOpacity) {
  const opacity = showOpacity ? 'opacity:.55' : '';
  return `<tr style="${opacity}">
    <td><a class="uj-id" href="/job/${j.id}" title="${j.name}">${j.id}</a></td>
    <td>${jobStateCell(j.state, j.done||false)}</td>
    <td class="muted" style="max-width:80px;overflow:hidden;text-overflow:ellipsis" title="${j.partition||''}">${j.partition||'—'}</td>
    <td class="muted">${j.time}</td>
    <td class="muted">${fmtDate(j.submit)}</td>
  </tr>`;
}

function wireTableSort(tableId, getSortKey, setSortState, rerender) {
  document.querySelectorAll(`#${tableId} th[data-k]`).forEach(th =>
    th.addEventListener('click', () => {
      const key = th.dataset.k;
      setSortState(key);
      rerender();
    }));
}

// ── active queue panel ───────────────────────────────────────────────────────
let aqSortKey = 'state', aqSortDir = 1;

function renderActiveQueue() {
  const user = SNAPSHOT.current_user || '';
  const userEl = document.getElementById('my-jobs-user');
  if (userEl) userEl.textContent = user ? `(${user})` : '';

  const jobs = sortJobs(SNAPSHOT.active_queue || [], aqSortKey, aqSortDir);
  const panel = document.getElementById('aq-panel');
  if (!SNAPSHOT.active_queue_loaded) {
    panel.innerHTML = '<p class="muted" style="font-size:13px;margin:4px 0">Click ↻ to load your active jobs.</p>';
    return;
  }
  if (!jobs.length) {
    panel.innerHTML = '<p class="muted" style="font-size:13px;margin:4px 0">No active jobs.</p>';
    return;
  }
  const th = (k, l) => makeTh(k, l, aqSortKey, aqSortDir);
  panel.innerHTML = `<table id="aq-table">
    <thead><tr>
      ${th('id','ID')}${th('state','St')}${th('partition','Partition')}${th('time','Time')}${th('submit','Date')}
    </tr></thead>
    <tbody>${jobs.map(j => jobRow(j, false)).join('')}</tbody>
  </table>`;
  wireTableSort('aq-table', () => aqSortKey, (key) => {
    if (aqSortKey === key) aqSortDir *= -1; else { aqSortKey = key; aqSortDir = 1; }
  }, renderActiveQueue);
}

// ── history jobs panel ───────────────────────────────────────────────────────
let histSortKey = 'submit', histSortDir = -1;

function renderHistoryJobs() {
  const panel = document.getElementById('hist-panel');
  if (!SNAPSHOT.user_jobs_loaded) {
    panel.innerHTML = '<p class="muted" style="font-size:13px;margin:4px 0">Click ↻ to load recent history.</p>';
    return;
  }
  // exclude jobs still active — those are shown in Active Queue
  const allJobs = sortJobs(
    (SNAPSHOT.user_jobs || []).filter(j => j.done),
    histSortKey, histSortDir
  );
  if (!allJobs.length) {
    panel.innerHTML = '<p class="muted" style="font-size:13px;margin:4px 0">No history found.</p>';
    return;
  }
  const th = (k, l) => makeTh(k, l, histSortKey, histSortDir);
  panel.innerHTML = `<table id="hist-table">
    <thead><tr>
      ${th('id','ID')}${th('state','St')}${th('partition','Partition')}${th('time','Time')}${th('submit','Date')}
    </tr></thead>
    <tbody>${allJobs.map(j => jobRow(j, false)).join('')}</tbody>
  </table>`;
  wireTableSort('hist-table', () => histSortKey, (key) => {
    if (histSortKey === key) histSortDir *= -1; else { histSortKey = key; histSortDir = 1; }
  }, renderHistoryJobs);
}

// ── draggable column resizers ───────────────────────────────────────────────
function initColumnResizers() {
  const leftCol  = document.querySelector('.left-col');
  const rightCol = document.querySelector('.right-col');

  const aqSection   = document.getElementById('aq-panel').closest('.jobs-section');
  const histSection = document.getElementById('hist-panel').closest('.jobs-section');

  // Restore saved sizes
  try {
    const saved = JSON.parse(localStorage.getItem('sb_col_w') || 'null');
    if (saved && saved.left)  leftCol.style.width  = saved.left  + 'px';
    if (saved && saved.right) rightCol.style.width = saved.right + 'px';
    if (saved && saved.aqH)   aqSection.style.flex = `0 0 ${saved.aqH}px`;
  } catch(e) {}

  function saveWidths() {
    try {
      localStorage.setItem('sb_col_w', JSON.stringify({
        left:  leftCol.offsetWidth,
        right: rightCol.offsetWidth,
        aqH:   aqSection.offsetHeight,
      }));
    } catch(e) {}
  }

  function wire(id, col, sign) {
    const el = document.getElementById(id);
    if (!el) return;
    el.addEventListener('mousedown', e => {
      e.preventDefault();
      const startX = e.clientX, startW = col.offsetWidth;
      el.classList.add('dragging');
      const minW = parseInt(getComputedStyle(col).minWidth) || 100;
      const onMove = e => {
        col.style.width = Math.max(minW, startW + sign * (e.clientX - startX)) + 'px';
      };
      const onUp = () => {
        el.classList.remove('dragging');
        document.removeEventListener('mousemove', onMove);
        document.removeEventListener('mouseup', onUp);
        saveWidths();
      };
      document.addEventListener('mousemove', onMove);
      document.addEventListener('mouseup', onUp);
    });
  }

  function wireV(id, topSection) {
    const el = document.getElementById(id);
    if (!el) return;
    el.addEventListener('mousedown', e => {
      e.preventDefault();
      const startY = e.clientY, startH = topSection.offsetHeight;
      el.classList.add('dragging');
      const onMove = e => {
        topSection.style.flex = `0 0 ${Math.max(60, startH + (e.clientY - startY))}px`;
      };
      const onUp = () => {
        el.classList.remove('dragging');
        document.removeEventListener('mousemove', onMove);
        document.removeEventListener('mouseup', onUp);
        saveWidths();
      };
      document.addEventListener('mousemove', onMove);
      document.addEventListener('mouseup', onUp);
    });
  }

  wire('resizer-left',  leftCol,  +1);
  wire('resizer-right', rightCol, -1);
  wireV('resizer-jobs', aqSection);
}

// ── init ───────────────────────────────────────────────────────────────────
renderSummary(SNAPSHOT.summary);
renderGpuTable(SNAPSHOT.summary.gpu_by_type);
wireGpuHeaders();
wirePartHeaders();
document.getElementById('vram-min').addEventListener('input',  renderPartitions);
document.getElementById('idle-only').addEventListener('change', renderPartitions);
initColumnResizers();
document.getElementById('aq-refresh-btn').addEventListener('click', () => refreshActiveQueue(true));
document.getElementById('hist-refresh-btn').addEventListener('click', () => refreshHistoryJobs(true));
document.getElementById('quota-refresh-btn').addEventListener('click', () => refreshStorageQuota(true));
document.getElementById('dashboard-refresh').addEventListener('click', refreshDashboard);
document.getElementById('refresh-interval').addEventListener('change', e => {
  setRefreshInterval(Number(e.target.value));
});
renderPartitions();
renderActiveQueue();
renderHistoryJobs();
renderStorageQuota();
setRefreshInterval(readRefreshPreference(), false);
if (SNAPSHOT.auto_load_personal) {
  if (!SNAPSHOT.active_queue_loaded) refreshActiveQueue(false);
  refreshHistoryJobs(false);
}
</script>
</body>
</html>
"""


def render_page():
    try:
        snapshot = build_snapshot()
        snapshot_json = json.dumps(snapshot)
        generated_at  = snapshot["generated_at"]
    except Exception as exc:
        snapshot_json = json.dumps({
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "summary": {
                "cpu_alloc": 0, "cpu_total": 0,
                "mem_alloc_mb": 0, "mem_total_mb": 0,
                "gpu_alloc": 0, "gpu_total": 0,
                "node_count": 0, "node_states": {}, "gpu_by_type": {},
            },
            "partitions": [], "nodes": [],
            "active_queue": [], "active_queue_loaded": True,
            "user_jobs": [], "user_jobs_loaded": True,
            "error": str(exc),
        })
        generated_at = "ERROR"

    html = (PAGE_TEMPLATE
            .replace("__GENERATED_AT__", generated_at)
            .replace("__SNAPSHOT_JSON__", snapshot_json))
    return html.encode("utf-8")


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "slurmboard/1.0"

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        refresh = parse_qs(parsed.query).get("refresh") == ["1"]

        if path == "/health":
            body = b"ok\n"
            self.send_response(200)
            self.send_header("Content-Type",   "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control",  "no-store")
            self.end_headers()
            self.wfile.write(body)
        elif path in ("/", "/index.html"):
            body = render_page()
            self.send_response(200)
            self.send_header("Content-Type",   "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control",  "no-store")
            self.end_headers()
            self.wfile.write(body)
        elif path.startswith("/api/job/"):
            jobid = path[len("/api/job/"):].strip("/")
            try:
                body = json.dumps({"job": collect_job_detail(jobid)}).encode("utf-8")
                status = 200
            except ValueError as exc:
                body = json.dumps({"error": str(exc)}).encode("utf-8")
                status = 400
            except Exception as exc:
                log.error("collect_job_detail failed for %r: %s", jobid, exc, exc_info=True)
                body = json.dumps({"error": str(exc)}).encode("utf-8")
                status = 500
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        elif path.startswith("/job/"):
            jobid = path[5:].strip("/")
            body  = render_job_page(jobid).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type",   "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control",  "no-store")
            self.end_headers()
            self.wfile.write(body)
        elif path == "/data":
            try:
                body   = json.dumps(build_cluster_summary(refresh=refresh)).encode("utf-8")
                status = 200
            except Exception as exc:
                log.error("build_cluster_summary failed: %s", exc, exc_info=True)
                body   = json.dumps({"error": str(exc)}).encode("utf-8")
                status = 500
            self.send_response(status)
            self.send_header("Content-Type",   "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control",  "no-store")
            self.end_headers()
            self.wfile.write(body)
        elif path.startswith("/data/partition/"):
            match = re.fullmatch(r"/data/partition/([^/]+)(/jobs)?", path)
            if not match:
                self._send_json(404, {"error": "Not found"})
                return
            partition = unquote(match.group(1))
            if not re.fullmatch(r"[A-Za-z0-9_.+:-]+", partition):
                self._send_json(400, {"error": "Invalid partition name"})
                return
            try:
                if match.group(2):
                    payload = build_partition_jobs(partition, refresh=refresh)
                else:
                    payload = build_partition_details(partition, refresh=refresh)
                self._send_json(200, payload)
            except Exception as exc:
                log.error("partition %s query failed: %s", partition, exc, exc_info=True)
                self._send_json(500, {"error": str(exc)})
        elif path == "/data/activequeue":
            try:
                user = getpass.getuser()
                jobs = _cached(
                    ("active-queue", user), 10,
                    lambda: collect_active_queue(user), refresh=refresh,
                )
                body   = json.dumps({"active_queue": jobs}).encode("utf-8")
                status = 200
            except Exception as exc:
                log.error("collect_active_queue failed: %s", exc, exc_info=True)
                body   = json.dumps({"error": str(exc)}).encode("utf-8")
                status = 500
            self.send_response(status)
            self.send_header("Content-Type",   "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control",  "no-store")
            self.end_headers()
            self.wfile.write(body)
        elif path == "/data/userjobs":
            try:
                user = getpass.getuser()
                jobs = _cached(
                    ("user-jobs", user), 60,
                    lambda: collect_user_jobs(user), refresh=refresh,
                )
                body   = json.dumps({"user_jobs": jobs}).encode("utf-8")
                status = 200
            except Exception as exc:
                log.error("collect_user_jobs failed: %s", exc, exc_info=True)
                body   = json.dumps({"error": str(exc)}).encode("utf-8")
                status = 500
            self.send_response(status)
            self.send_header("Content-Type",   "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control",  "no-store")
            self.end_headers()
            self.wfile.write(body)
        elif path == "/data/quota":
            try:
                self._send_json(200, build_storage_quota(refresh=refresh))
            except Exception as exc:
                log.error("storage quota query failed: %s", exc, exc_info=True)
                self._send_json(500, {"error": str(exc)})
        else:
            body = b"not found"
            self.send_response(404)
            self.send_header("Content-Type",   "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def log_message(self, fmt, *args):
        log.debug("http: %s %s", self.address_string(), fmt % args)

    def _send_json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


# ---------------------------------------------------------------------------
# Local SSH launcher
# ---------------------------------------------------------------------------

_LAUNCHER_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Slurmboard Launcher</title>
<style>
  :root { color-scheme: dark; --bg:#0f1115; --panel:#171a21; --border:#2a2f3a;
          --text:#e6e9ef; --muted:#8b93a3; --accent:#4f8cff; --good:#3ec97c;
          --warn:#f0a93f; --bad:#ef5b5b; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--text);
         font:14px -apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif; }
  header { border-bottom:1px solid var(--border); padding:22px 28px; }
  header h1 { margin:0 0 5px; font-size:22px; }
  header p { margin:0; color:var(--muted); }
  main { max-width:900px; margin:0 auto; padding:28px; }
  .intro { display:flex; align-items:center; justify-content:space-between; gap:16px;
           margin-bottom:18px; }
  .config { color:var(--muted); font-size:12px; overflow-wrap:anywhere; }
  button, a.button { border:1px solid var(--border); background:#20242d; color:var(--text);
                     border-radius:7px; padding:7px 12px; cursor:pointer; font:inherit;
                     text-decoration:none; white-space:nowrap; }
  button:hover, a.button:hover { filter:brightness(1.15); }
  button:disabled { cursor:default; opacity:.55; }
  .primary { background:var(--accent)!important; border-color:var(--accent)!important; color:#fff!important; }
  .danger { color:var(--bad); }
  #hosts { display:grid; grid-template-columns:repeat(auto-fit,minmax(300px,1fr)); gap:12px; }
  .host { background:var(--panel); border:1px solid var(--border); border-radius:10px;
          padding:16px; min-width:0; }
  .host-top { display:flex; align-items:center; gap:9px; margin-bottom:13px; }
  .host-name { font-weight:650; font-size:16px; overflow:hidden; text-overflow:ellipsis; }
  .pin-button { border:0; background:transparent; color:var(--muted); font-size:20px;
                line-height:1; padding:2px 3px; }
  .pin-button.pinned { color:var(--warn); }
  .dot { width:9px; height:9px; border-radius:50%; flex:none; background:var(--muted); }
  .dot.connecting { background:var(--warn); animation:pulse 1s infinite alternate; }
  .dot.connected { background:var(--good); }
  .dot.error { background:var(--bad); }
  .status { color:var(--muted); font-size:12px; margin-left:auto; text-transform:capitalize; }
  .actions { display:flex; gap:8px; }
  .auth { margin:0 0 11px; }
  .auth label { display:block; color:var(--muted); font-size:12px; margin-bottom:5px; }
  .password-input { width:100%; border:1px solid var(--border); border-radius:7px;
                    background:#0f1115; color:var(--text); padding:8px 10px; font:inherit; }
  .password-input:focus { outline:2px solid color-mix(in srgb, var(--accent) 55%, transparent);
                          border-color:var(--accent); }
  .auth-note { color:var(--muted); font-size:11px; margin-top:4px; }
  .forget-password { border:0; background:transparent; color:var(--muted); padding:0;
                     font-size:11px; text-decoration:underline; }
  .error { color:var(--bad); font-size:12px; margin-top:11px; white-space:pre-wrap;
           overflow-wrap:anywhere; }
  .empty { color:var(--muted); background:var(--panel); border:1px solid var(--border);
           border-radius:10px; padding:22px; }
  @keyframes pulse { to { opacity:.35; } }
</style>
</head>
<body>
<header>
  <h1>Slurmboard Launcher</h1>
  <p>Connect to a Slurm cluster through an SSH host configured on this Mac.</p>
</header>
<main>
  <div class="intro">
    <div class="config">SSH config: <span id="config-path">__SSH_CONFIG__</span></div>
    <button id="refresh">Reload hosts</button>
  </div>
  <div id="hosts"><div class="empty">Loading SSH hosts…</div></div>
</main>
<script>
const root = document.getElementById('hosts');
const token = '__LAUNCHER_TOKEN__';
let busy = false;

async function request(path, options) {
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
  return data;
}

function button(label, className, handler, disabled=false) {
  const b = document.createElement('button');
  b.textContent = label;
  b.className = className || '';
  b.disabled = disabled;
  b.addEventListener('click', handler);
  return b;
}

function render(data) {
  root.replaceChildren();
  if (data.config_error) {
    const box = document.createElement('div');
    box.className = 'empty';
    box.textContent = data.config_error;
    root.appendChild(box);
    return;
  }
  if (!data.hosts.length) {
    const box = document.createElement('div');
    box.className = 'empty';
    box.textContent = 'No concrete Host aliases were found. Wildcard entries such as “Host *” are intentionally hidden.';
    root.appendChild(box);
    return;
  }
  for (const host of data.hosts) {
    const card = document.createElement('section');
    card.className = 'host';
    const top = document.createElement('div');
    top.className = 'host-top';
    const dot = document.createElement('span');
    dot.className = `dot ${host.status}`;
    const name = document.createElement('span');
    name.className = 'host-name';
    name.textContent = host.host;
    name.title = host.host;
    const status = document.createElement('span');
    status.className = 'status';
    status.textContent = host.status === 'idle' ? 'not connected' : host.status;
    const pin = button(host.pinned ? '★' : '☆', `pin-button${host.pinned ? ' pinned' : ''}`,
      () => act('/api/pin', host.host, {pinned: !host.pinned}));
    pin.title = host.pinned ? 'Unpin host' : 'Pin host';
    pin.setAttribute('aria-label', pin.title);
    top.append(dot, name, status, pin);
    card.appendChild(top);

    const actions = document.createElement('div');
    actions.className = 'actions';
    if (host.status === 'connected') {
      const open = document.createElement('a');
      open.className = 'button primary';
      open.textContent = 'Open dashboard';
      open.href = host.url;
      open.target = '_blank';
      actions.appendChild(open);
      actions.appendChild(button('Stop', 'danger', () => act('/api/disconnect', host.host)));
      if (host.password_cached) {
        actions.appendChild(button('Forget password', 'forget-password',
          () => act('/api/forget-password', host.host)));
      }
    } else if (host.status === 'connecting' || host.status === 'stopping') {
      actions.appendChild(button(host.status === 'connecting' ? 'Connecting…' : 'Stopping…', 'primary', () => {}, true));
    } else {
      const auth = document.createElement('div');
      auth.className = 'auth';
      const passwordLabel = document.createElement('label');
      passwordLabel.textContent = 'SSH password (optional)';
      const passwordInput = document.createElement('input');
      passwordInput.className = 'password-input';
      passwordInput.type = 'password';
      passwordInput.autocomplete = 'current-password';
      passwordInput.placeholder = host.password_cached
        ? 'Cached — leave blank to reuse'
        : 'Leave blank to use your SSH key';
      passwordLabel.htmlFor = `password-${host.host}`;
      passwordInput.id = passwordLabel.htmlFor;
      const note = document.createElement('div');
      note.className = 'auth-note';
      note.textContent = host.password_cached
        ? 'Cached in memory until the launcher quits.'
        : 'Cached after a successful connection until the launcher quits.';
      auth.append(passwordLabel, passwordInput, note);
      if (host.password_cached) {
        auth.appendChild(button('Forget cached password', 'forget-password',
          () => act('/api/forget-password', host.host)));
      }
      card.appendChild(auth);

      const connect = () => {
        const password = passwordInput.value;
        passwordInput.value = '';
        act('/api/connect', host.host, {password});
      };
      passwordInput.addEventListener('keydown', event => {
        if (event.key === 'Enter') connect();
      });
      actions.appendChild(button('Connect', 'primary', connect));
    }
    card.appendChild(actions);
    if (host.error) {
      const error = document.createElement('div');
      error.className = 'error';
      error.textContent = host.error;
      card.appendChild(error);
    }
    root.appendChild(card);
  }
}

async function load() {
  if (busy) return;
  if (document.activeElement?.classList.contains('password-input')) return;
  try { render(await request('/api/hosts')); }
  catch (error) { root.textContent = error.message; }
}

async function act(path, host, extra={}) {
  busy = true;
  try {
    render(await request(path, {
      method:'POST', headers:{'Content-Type':'application/json', 'X-Slurmboard-Token':token},
      body:JSON.stringify({host, ...extra})
    }));
  } catch (error) {
    alert(error.message);
  } finally {
    busy = false;
    load();
  }
}

document.getElementById('refresh').addEventListener('click', load);
load();
setInterval(load, 1000);
</script>
</body>
</html>
"""


def parse_ssh_hosts(config_path):
    """Return concrete Host aliases from an OpenSSH user config and its includes."""
    root = Path(config_path).expanduser()
    hosts = []
    host_set = set()
    visited = set()

    def visit(path):
        try:
            resolved = path.expanduser().resolve()
        except OSError:
            resolved = path.expanduser().absolute()
        if resolved in visited:
            return
        visited.add(resolved)

        with resolved.open(encoding="utf-8", errors="replace") as config_file:
            for raw_line in config_file:
                try:
                    words = shlex.split(raw_line, comments=True, posix=True)
                except ValueError:
                    continue
                if not words:
                    continue
                if "=" in words[0]:
                    key, first_value = words[0].split("=", 1)
                    words = [key] + ([first_value] if first_value else []) + words[1:]
                elif len(words) > 1 and words[1] == "=":
                    words = [words[0]] + words[2:]
                keyword = words[0].lower()
                if keyword == "include":
                    for pattern in words[1:]:
                        include_pattern = Path(pattern).expanduser()
                        if not include_pattern.is_absolute():
                            include_pattern = root.parent / include_pattern
                        for match in sorted(glob.glob(str(include_pattern))):
                            visit(Path(match))
                elif keyword == "host":
                    for alias in words[1:]:
                        if alias.startswith("!") or "*" in alias or "?" in alias:
                            continue
                        if alias not in host_set:
                            host_set.add(alias)
                            hosts.append(alias)

    visit(root)
    return hosts


def _available_loopback_port(start):
    """Find an unused loopback port, preferring ``start`` and scanning downward."""
    for port in range(min(start, 65535), 49151, -1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError("No free local port found between 49152 and 65535")


class LauncherState:
    def __init__(self, script_path, config_path, remote_port, state_path=None,
                 quota_command=None):
        self.script_path = Path(script_path).resolve()
        self.config_path = Path(config_path).expanduser()
        self.state_path = (Path(state_path).expanduser() if state_path else
                           Path.home() / ".config" / "slurmboard" / "launcher.json")
        self.remote_port = remote_port
        self.quota_command = quota_command
        self.hosts = []
        self.config_error = None
        self.connections = {}
        self.password_cache = {}
        self.pinned_hosts = []
        self.token = secrets.token_urlsafe(32)
        self.lock = threading.RLock()
        self._load_state()
        self.refresh_hosts()

    def _load_state(self):
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            pinned = payload.get("pinned_hosts", [])
            if not isinstance(pinned, list):
                raise ValueError("pinned_hosts must be a list")
            self.pinned_hosts = list(dict.fromkeys(
                host for host in pinned if isinstance(host, str) and host
            ))
        except FileNotFoundError:
            return
        except (OSError, ValueError, TypeError) as exc:
            log.warning("could not read launcher state %s: %s", self.state_path, exc)

    def _save_state(self, pinned_hosts):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_name(
            f".{self.state_path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        try:
            temporary.write_text(
                json.dumps({"pinned_hosts": pinned_hosts}, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(str(temporary), str(self.state_path))
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def set_pinned(self, host, pinned):
        with self.lock:
            if host not in self.hosts:
                raise ValueError("That host is not a concrete alias in the configured SSH file")
            updated = [item for item in self.pinned_hosts if item != host]
            if pinned:
                updated.append(host)
            self._save_state(updated)
            self.pinned_hosts = updated

    def refresh_hosts(self):
        try:
            hosts = parse_ssh_hosts(self.config_path)
            error = None
        except OSError as exc:
            hosts = []
            error = f"Could not read {self.config_path}: {exc}"
        with self.lock:
            self.hosts = hosts
            self.config_error = error

    def _connection_view(self, host):
        conn = self.connections.get(host)
        if not conn:
            return {"host": host, "status": "idle", "url": None, "error": None,
                    "pinned": host in self.pinned_hosts,
                    "password_cached": host in self.password_cache}
        return {
            "host": host,
            "status": conn["status"],
            "url": f"http://127.0.0.1:{conn['local_port']}" if conn["status"] == "connected" else None,
            "error": conn.get("error") or None,
            "pinned": host in self.pinned_hosts,
            "password_cached": host in self.password_cache,
        }

    def view(self, refresh=False):
        if refresh:
            self.refresh_hosts()
        with self.lock:
            pinned = [host for host in self.pinned_hosts if host in self.hosts]
            visible_hosts = pinned + [host for host in self.hosts if host not in pinned]
            for host, conn in self.connections.items():
                if conn["status"] not in ("idle",) and host not in visible_hosts:
                    visible_hosts.append(host)
            return {
                "config_path": str(self.config_path),
                "config_error": self.config_error,
                "hosts": [self._connection_view(host) for host in visible_hosts],
            }

    def connect(self, host, password=None):
        with self.lock:
            if host not in self.hosts:
                raise ValueError("That host is not a concrete alias in the configured SSH file")
            previous = self.connections.get(host)
            if previous and previous["process"].poll() is None:
                return
            entered_password = password
            if password is None:
                password = self.password_cache.get(host)
            used_cached_password = bool(password and entered_password is None)
            remote_port = self.remote_port or 49152 + secrets.randbelow(65536 - 49152)
            preferred_local_port = remote_port if remote_port >= 49152 else 65535
            local_port = _available_loopback_port(preferred_local_port)

        quota_option = (f" --quota-command {shlex.quote(self.quota_command)}"
                        if self.quota_command else "")
        remote_command = (
            'exec "$(command -v python3.13 || command -v python3.12 || '
            'command -v python3.11 || command -v python3.10 || command -v python3.9 || '
            'command -v python3.8 || command -v python3.7 || echo python3)" '
            f'- --host 127.0.0.1 --port {remote_port}{quota_option} --log-level warning'
        )
        ssh_command = [
            "ssh", "-T",
            "-o", f"BatchMode={'no' if password else 'yes'}",
            "-o", "ExitOnForwardFailure=yes",
            "-o", "ConnectTimeout=10",
            "-o", "ServerAliveInterval=30",
            "-o", "ServerAliveCountMax=3",
            "-L", f"127.0.0.1:{local_port}:127.0.0.1:{remote_port}",
            "--", host, remote_command,
        ]

        auth_dir = None
        ssh_environment = None
        if password:
            try:
                auth_dir = Path(tempfile.mkdtemp(prefix="slurmboard-askpass-"))
                password_file = auth_dir / "password"
                askpass_file = auth_dir / "askpass"
                password_file.write_text(password, encoding="utf-8")
                password_file.chmod(0o600)
                askpass_file.write_text(
                    '#!/bin/sh\nexec /bin/cat "$SLURMBOARD_ASKPASS_FILE"\n',
                    encoding="utf-8",
                )
                askpass_file.chmod(0o700)
                ssh_environment = os.environ.copy()
                ssh_environment.update({
                    "SSH_ASKPASS": str(askpass_file),
                    "SSH_ASKPASS_REQUIRE": "force",
                    "DISPLAY": ssh_environment.get("DISPLAY") or ":0",
                    "SLURMBOARD_ASKPASS_FILE": str(password_file),
                })
                ssh_command[4:4] = ["-o", "NumberOfPasswordPrompts=1"]
            except Exception:
                if auth_dir:
                    shutil.rmtree(auth_dir, ignore_errors=True)
                raise

        source = self.script_path.open("rb")
        try:
            try:
                process = subprocess.Popen(
                    ssh_command,
                    stdin=source,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    start_new_session=True,
                    **({"env": ssh_environment} if ssh_environment else {}),
                )
            except Exception:
                if auth_dir:
                    shutil.rmtree(auth_dir, ignore_errors=True)
                raise
        finally:
            source.close()

        conn = {
            "host": host,
            "local_port": local_port,
            "remote_port": remote_port,
            "process": process,
            "status": "connecting",
            "error": "",
            "stderr": [],
            "intentional_stop": False,
            "auth_dir": auth_dir,
            "pending_password": entered_password,
            "used_cached_password": used_cached_password,
            "authentication_succeeded": False,
        }
        with self.lock:
            self.connections[host] = conn
        threading.Thread(target=self._read_stderr, args=(conn,), daemon=True).start()
        threading.Thread(target=self._monitor, args=(conn,), daemon=True).start()

    def _read_stderr(self, conn):
        stream = conn["process"].stderr
        if stream is None:
            return
        for line in stream:
            line = line.strip()
            if not line:
                continue
            with self.lock:
                conn["stderr"].append(line)
                del conn["stderr"][:-8]

    def _monitor(self, conn):
        process = conn["process"]
        deadline = time.monotonic() + 30
        while process.poll() is None and time.monotonic() < deadline:
            health = HTTPConnection("127.0.0.1", conn["local_port"], timeout=0.5)
            try:
                health.request("GET", "/health")
                response = health.getresponse()
                response.read()
                if response.status == 200:
                    self._mark_connected(conn)
                    self._clear_auth(conn)
                    break
            except Exception:
                time.sleep(0.25)
            finally:
                health.close()

        if process.poll() is None and conn["status"] == "connecting":
            with self.lock:
                conn["status"] = "error"
                conn["error"] = "SSH connected, but the remote dashboard did not become ready within 30 seconds."
            process.terminate()

        return_code = process.wait()
        self._clear_auth(conn)
        time.sleep(0.05)  # let the stderr reader collect the final SSH message
        with self.lock:
            conn.pop("pending_password", None)
            if conn["intentional_stop"]:
                conn["status"] = "idle"
                conn["error"] = ""
            else:
                conn["status"] = "error"
                details = "\n".join(conn["stderr"])
                if conn.get("used_cached_password") and "Permission denied" in details:
                    self.password_cache.pop(conn["host"], None)
                if "Address already in use" in details:
                    if self.remote_port:
                        conn["error"] = (
                            f"Remote port {conn['remote_port']} is already in use. "
                            "Restart the launcher without --remote-port, or choose a different fixed port."
                        )
                    else:
                        conn["error"] = (
                            f"Remote port {conn['remote_port']} was already in use. "
                            "Click Connect to try another automatically selected port."
                        )
                else:
                    conn["error"] = details or f"SSH exited with status {return_code}"

    def _mark_connected(self, conn):
        with self.lock:
            if conn["intentional_stop"]:
                return
            conn["status"] = "connected"
            conn["authentication_succeeded"] = True
            entered_password = conn.pop("pending_password", None)
            if entered_password:
                self.password_cache[conn["host"]] = entered_password

    def forget_password(self, host):
        with self.lock:
            if host not in self.hosts:
                raise ValueError("That host is not a concrete alias in the configured SSH file")
            self.password_cache.pop(host, None)

    def _clear_auth(self, conn):
        with self.lock:
            auth_dir = conn.pop("auth_dir", None)
        if auth_dir:
            shutil.rmtree(auth_dir, ignore_errors=True)

    def disconnect(self, host):
        with self.lock:
            conn = self.connections.get(host)
            if not conn or conn["process"].poll() is not None:
                return
            conn["intentional_stop"] = True
            conn["status"] = "stopping"
            conn["process"].terminate()

    def close(self):
        with self.lock:
            live = [conn for conn in self.connections.values() if conn["process"].poll() is None]
            for conn in live:
                conn["intentional_stop"] = True
                conn["process"].terminate()
        for conn in live:
            try:
                conn["process"].wait(timeout=2)
            except subprocess.TimeoutExpired:
                conn["process"].kill()
        with self.lock:
            self.password_cache.clear()


class LauncherHandler(BaseHTTPRequestHandler):
    state = None
    server_version = "slurmboard-launcher/1.0"

    def _send(self, status, body, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            page = (_LAUNCHER_PAGE
                    .replace("__SSH_CONFIG__", _html.escape(str(self.state.config_path)))
                    .replace("__LAUNCHER_TOKEN__", self.state.token))
            self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
        elif self.path == "/api/hosts":
            self._json(200, self.state.view(refresh=True))
        else:
            self._json(404, {"error": "Not found"})

    def do_POST(self):
        if self.path not in ("/api/connect", "/api/disconnect", "/api/pin",
                             "/api/forget-password"):
            self._json(404, {"error": "Not found"})
            return
        if self.headers.get("X-Slurmboard-Token") != self.state.token:
            self._json(403, {"error": "Invalid launcher token"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 8192:
                raise ValueError("Invalid request size")
            payload = json.loads(self.rfile.read(length))
            host = payload.get("host")
            if not isinstance(host, str):
                raise ValueError("Missing SSH host")
            if self.path == "/api/connect":
                password = payload.get("password", "")
                if not isinstance(password, str) or len(password) > 4096:
                    raise ValueError("Invalid SSH password")
                self.state.connect(host, password=password or None)
            elif self.path == "/api/disconnect":
                self.state.disconnect(host)
            elif self.path == "/api/forget-password":
                self.state.forget_password(host)
            else:
                pinned = payload.get("pinned")
                if not isinstance(pinned, bool):
                    raise ValueError("Missing pin state")
                self.state.set_pinned(host, pinned)
            self._json(200, self.state.view())
        except (ValueError, OSError, RuntimeError) as exc:
            self._json(400, {"error": str(exc)})

    def log_message(self, fmt, *args):
        log.debug("launcher http: %s %s", self.address_string(), fmt % args)


def run_launcher(args):
    if not 0 <= args.remote_port <= 65535:
        raise SystemExit("--remote-port must be 0 (automatic) or between 1 and 65535")
    state = LauncherState(
        __file__, args.ssh_config, args.remote_port,
        state_path=getattr(args, "launcher_state", None),
        quota_command=getattr(args, "quota_command", None),
    )
    LauncherHandler.state = state
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", args.launcher_port), LauncherHandler)
    except OSError as exc:
        if exc.errno != errno.EADDRINUSE or args.launcher_port == 0:
            raise
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), LauncherHandler)
        log.warning(
            "local port %d is already in use; using port %d instead",
            args.launcher_port, httpd.server_address[1],
        )
    address, port = httpd.server_address[:2]
    url = f"http://{address}:{port}"
    log.info("launcher listening on %s", url)
    if not args.no_browser:
        threading.Timer(0.2, webbrowser.open, args=(url,)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log.info("shutting down launcher and SSH connections")
    finally:
        state.close()
        httpd.server_close()


def create_http_server(host, requested_port=None, max_attempts=100):
    """Bind the requested port, or probe up to 100 non-sequential ports."""
    if requested_port is not None:
        return ThreadingHTTPServer((host, requested_port), Handler)

    # Try the predictable default first, then avoid a slow sequential scan by
    # sampling unique ports from the remaining valid range. Brief jittered
    # exponential backoff prevents simultaneous launches from retrying in lockstep.
    candidates = [9001]
    remaining_attempts = max(0, min(max_attempts, 100) - 1)
    candidates.extend(random.SystemRandom().sample(range(9002, 65536), remaining_attempts))
    for attempt, port in enumerate(candidates):
        try:
            return ThreadingHTTPServer((host, port), Handler)
        except OSError as exc:
            if exc.errno == errno.EADDRINUSE:
                if attempt + 1 < len(candidates):
                    ceiling = min(0.001 * (2 ** attempt), 0.1)
                    time.sleep(random.SystemRandom().uniform(0, ceiling))
                continue
            raise
    raise RuntimeError(f"no available TCP port found above 9000 after {len(candidates)} attempts")


def print_forwarding_instructions(port):
    command = f"ssh -N -L {port}:127.0.0.1:{port} <your-ssh-host>"
    print("\nOn your local machine, run this SSH forwarding command:", file=sys.stderr)
    print(f"\n  {command}\n", file=sys.stderr)
    print("Then open:", file=sys.stderr)
    print(f"\n  http://127.0.0.1:{port}\n", file=sys.stderr)
    print("Replace <your-ssh-host> with the host or SSH config alias you normally use.\n",
          file=sys.stderr, flush=True)


def main():
    global _QUOTA_COMMAND
    ap = argparse.ArgumentParser(
        description="Tiny Slurm cluster dashboard, with an optional local SSH launcher.")
    ap.add_argument("--host",      default="0.0.0.0",  help="bind address (default: 0.0.0.0)")
    ap.add_argument("--port", type=int, default=None,
                    help="bind port (default: an available port above 9000)")
    ap.add_argument("--launcher", action="store_true",
                    help="run the local SSH host launcher on this computer")
    ap.add_argument("--launcher-port", type=int, default=65432,
                    help="preferred local launcher port (default: 65432; falls back automatically)")
    ap.add_argument("--launcher-state", default=None,
                    help="launcher preferences file (default: ~/.config/slurmboard/launcher.json)")
    ap.add_argument("--remote-port", type=int, default=0,
                    help="fixed remote dashboard port (default: choose automatically)")
    ap.add_argument("--ssh-config", default="~/.ssh/config",
                    help="OpenSSH config used by the launcher (default: ~/.ssh/config)")
    ap.add_argument("--quota-command", default=None,
                    help="custom read-only quota command for site-specific HPC storage")
    ap.add_argument("--no-browser", action="store_true",
                    help="do not automatically open the launcher page")
    ap.add_argument("--log-level", default="info",
                    choices=["debug", "info", "warning", "error"],
                    help="log verbosity (default: info)")
    args = ap.parse_args()
    _QUOTA_COMMAND = args.quota_command

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format="%(asctime)s %(levelname)-8s %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.launcher:
        run_launcher(args)
        return

    httpd = create_http_server(args.host, args.port)
    port = httpd.server_address[1]
    log.info("slurmboard listening on http://%s:%d", args.host, port)
    print_forwarding_instructions(port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log.info("shutting down")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
