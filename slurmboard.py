#!/usr/bin/env python3
"""
slurmboard - a tiny, dependency-free web dashboard for a Slurm cluster.

Run directly on the Slurm login/submit node (no SSH, no extra packages).
Each time the page is loaded (i.e. the user hits refresh in the browser),
the server shells out to `sinfo` / `scontrol`, parses partition/node/GPU
(gres) usage, and renders a single self-contained HTML page. No background
polling, no caching, no third-party packages - stdlib only.

Usage:
    python3 slurmboard.py [--port 8000] [--host 0.0.0.0]
"""

import argparse
import json
import re
import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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


def _run(cmd):
    out = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                         text=True, check=True)
    return out.stdout


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


def _vram_gb_from_gres(gres):
    if not gres:
        return None
    m = _GRES_VRAM_RE.search(gres)
    if not m:
        return None
    val, unit = int(m.group(1)), m.group(2)
    return val if unit == "G" else val // 1024


def collect_nodes():
    text = _run(["scontrol", "-o", "show", "node"])
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


def collect_partitions():
    text = _run(["sinfo", "-h", "-o", "%P|%a|%l"])
    info = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("|")
        if len(parts) < 3:
            continue
        name = parts[0].rstrip("*")
        if name not in info:
            info[name] = {"avail": parts[1], "timelimit": parts[2]}
    return info


def collect_job_counts():
    """Return {partition: {running, pending, jobs: [...]}} from squeue."""
    # %P partition  %i jobid  %u user  %j name  %T state
    # %M elapsed/queue time  %C cpus  %b gres  %R reason/nodelist
    text = _run(["squeue", "-h", "-o", "%P|%i|%u|%j|%T|%M|%C|%b|%R"])
    counts = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("|", 8)  # maxsplit keeps %R intact if it contains |
        if len(parts) < 9:
            continue
        part, jid, user, name, state, time_used, cpus, gres, reason = parts
        state_up = state.upper()
        c = counts.setdefault(part, {"running": 0, "pending": 0, "jobs": []})
        if state_up == "RUNNING":
            c["running"] += 1
        elif state_up == "PENDING":
            c["pending"] += 1
        c["jobs"].append({
            "id":     jid,
            "user":   user,
            "name":   name,
            "state":  state_up,
            "time":   time_used,
            "cpus":   cpus,
            "gres":   gres if gres not in ("", "N/A") else None,
            "reason": reason,
        })
    return counts


def build_snapshot():
    nodes = collect_nodes()
    part_meta = collect_partitions()
    job_counts = collect_job_counts()

    summary = {
        "cpu_alloc":   sum(n["cpu_alloc"]    for n in nodes),
        "cpu_total":   sum(n["cpu_total"]    for n in nodes),
        "mem_alloc_mb": sum(n["mem_alloc_mb"] for n in nodes),
        "mem_total_mb": sum(n["mem_total_mb"] for n in nodes),
        "gpu_alloc":   sum(n["gpu_alloc"]    for n in nodes),
        "gpu_total":   sum(n["gpu_total"]    for n in nodes),
        "node_count":  len(nodes),
        "node_states": {},
        "gpu_by_type": {},
    }
    for n in nodes:
        st = n["state"]
        summary["node_states"][st] = summary["node_states"].get(st, 0) + 1
        if n["gpu_total"]:
            t = n["gpu_type"] or "gpu"
            b = summary["gpu_by_type"].setdefault(t, {"alloc": 0, "total": 0, "nodes": 0})
            b["alloc"] += n["gpu_alloc"]
            b["total"] += n["gpu_total"]
            b["nodes"] += 1

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
        meta = part_meta.get(name, {})
        jc   = job_counts.get(name, {"running": 0, "pending": 0, "jobs": []})
        agg["avail"]        = meta.get("avail",    "?")
        agg["timelimit"]    = meta.get("timelimit", "?")
        agg["gpu_idle"]     = agg["gpu_total"] - agg["gpu_alloc"]
        agg["jobs_running"] = jc["running"]
        agg["jobs_pending"] = jc["pending"]
        agg["jobs"]         = jc["jobs"]
        vram_vals = agg.pop("_vram_vals")
        agg["gpu_vram_gb"] = max(vram_vals) if vram_vals else None
        partitions.append(agg)

    return {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "summary":    summary,
        "partitions": partitions,
        "nodes":      nodes,
    }


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
  }
  * { box-sizing: border-box; }
  body { margin: 0; font-family: -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
         background: var(--bg); color: var(--text); font-size: 14px; }
  header { padding: 16px 24px; border-bottom: 1px solid var(--border);
           display: flex; align-items: baseline; gap: 16px; flex-wrap: wrap; }
  header h1 { margin: 0; font-size: 20px; font-weight: 600; }
  header .meta { color: var(--muted); font-size: 12px; }
  header .reload { margin-left: auto; background: var(--accent); color: #fff; border: none;
                   border-radius: 6px; padding: 6px 14px; font-size: 13px; cursor: pointer; }
  header .reload:hover { filter: brightness(1.1); }
  main { padding: 20px 24px 60px; max-width: 1400px; margin: 0 auto; }
  h2 { font-size: 15px; color: var(--muted); text-transform: uppercase; letter-spacing: .04em;
       margin: 32px 0 12px; }
  .hint { font-size: 12px; color: var(--muted); margin: -8px 0 12px; }
  .hint kbd { background: #2a2f3a; border-radius: 4px; padding: 1px 5px; font-size: 11px; }

  /* summary cards */
  .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 14px; }
  .card { background: var(--panel); border: 1px solid var(--border); border-radius: 10px; padding: 16px; }
  .card .label { color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .05em; }
  .card .value { font-size: 26px; font-weight: 700; margin-top: 4px; }
  .card .sub   { color: var(--muted); font-size: 12px; margin-top: 2px; }

  /* progress bars */
  .bar { height: 8px; border-radius: 4px; background: #2a2f3a; margin-top: 10px; overflow: hidden; }
  .bar > span { display: block; height: 100%; background: var(--accent); }
  .bar.gpu > span  { background: var(--good); }
  .bar.high > span { background: var(--warn); }
  .bar.crit > span { background: var(--bad); }
  .minibar { display: inline-block; width: 60px; height: 6px; border-radius: 3px;
             background: #2a2f3a; vertical-align: middle; margin-right: 5px; overflow: hidden; }
  .minibar > span { display: block; height: 100%; background: var(--accent); }
  .minibar.gpu > span { background: var(--good); }

  /* tables */
  table { width: 100%; border-collapse: collapse; background: var(--panel);
          border: 1px solid var(--border); border-radius: 10px; overflow: hidden; }
  th, td { text-align: left; padding: 8px 12px; border-bottom: 1px solid var(--border); white-space: nowrap; }
  th { color: var(--muted); font-weight: 600; font-size: 12px; text-transform: uppercase;
       letter-spacing: .04em; cursor: pointer; user-select: none; }
  th.no-sort { cursor: default; }
  th:not(.no-sort):hover { color: var(--text); }
  tr:last-child > td { border-bottom: none; }
  tr:hover > td { background: rgba(255,255,255,.03); }

  /* partition row expand */
  .part-row { cursor: pointer; }
  .part-row:hover > td { background: rgba(79,140,255,.06) !important; }
  .toggle-cell { width: 28px; text-align: center; color: var(--muted); font-size: 11px; }

  /* inner node sub-table */
  .nodes-expand-row > td { padding: 0 0 0 36px; background: var(--bg) !important; }
  .inner-wrap { border-left: 3px solid var(--border); margin: 6px 0 10px; }
  .inner-table { width: 100%; border-collapse: collapse; background: var(--bg); font-size: 13px; }
  .inner-table th { background: rgba(79,140,255,.05); font-size: 11px; padding: 6px 10px; }
  .inner-table td { padding: 6px 10px; border-bottom: 1px solid #1e2330; }
  .inner-table tr:last-child td { border-bottom: none; }
  .inner-table tr:hover td { background: rgba(255,255,255,.025); }

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
  footer { text-align: center; color: var(--muted); font-size: 12px; padding: 20px; }
</style>
</head>
<body>
<header>
  <h1>&#9881; Slurm Dashboard</h1>
  <div class="meta">snapshot taken at __GENERATED_AT__ &middot; reload the page to refresh</div>
  <button class="reload" onclick="location.reload()">&#x21bb; Refresh</button>
</header>
<main>
  <h2>Cluster summary</h2>
  <div class="cards" id="summary-cards"></div>

  <h2>GPUs by type</h2>
  <table id="gpu-table">
    <thead><tr>
      <th class="no-sort">Type</th><th class="no-sort">Allocated</th>
      <th class="no-sort">Idle</th><th class="no-sort">Total</th>
      <th class="no-sort">Usage</th><th class="no-sort">Nodes</th>
    </tr></thead>
    <tbody></tbody>
  </table>

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
      <th class="no-sort" style="width:28px"></th>
      <th data-k="name"          data-label="Partition">Partition</th>
      <th data-k="avail"         data-label="Avail">Avail</th>
      <th data-k="timelimit"     data-label="Time limit">Time limit</th>
      <th data-k="nodes"         data-label="Nodes">Nodes</th>
      <th data-k="jobs_pending"  data-label="Jobs (run/pend)">Jobs (run/pend)</th>
      <th data-k="cpu_total"     data-label="CPU (alloc/total)">CPU (alloc/total)</th>
      <th data-k="gpu_vram_gb"   data-label="VRAM (GB)">VRAM (GB)</th>
      <th data-k="gpu_idle"      data-label="GPU (idle/total)">GPU (idle/total)</th>
    </tr></thead>
    <tbody id="part-tbody"></tbody>
  </table>

  <footer>slurmboard &middot; data sourced live from <code>sinfo</code> / <code>scontrol</code> on this login node &middot; reload to refresh</footer>
</main>

<script>
const SNAPSHOT = __SNAPSHOT_JSON__;

// ── helpers ────────────────────────────────────────────────────────────────
function pct(a, t) { return t > 0 ? Math.round(a / t * 100) : 0; }
function fmtMem(mb) {
  if (mb >= 1024 * 1024) return (mb / (1024 * 1024)).toFixed(1) + ' TB';
  if (mb >= 1024)        return (mb / 1024).toFixed(1) + ' GB';
  return mb + ' MB';
}
function barClass(p) { return p >= 90 ? 'crit' : p >= 70 ? 'high' : ''; }
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

// ── multi-column sort ───────────────────────────────────────────────────────
// Array of {key, dir} objects; first entry = primary sort.
const partSortList = [{key: 'name', dir: 1}];
const expandedParts   = new Set();
const expandedRunning = new Set();
const expandedPending = new Set();

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
  const cpuPct = pct(s.cpu_alloc, s.cpu_total);
  const memPct = pct(s.mem_alloc_mb, s.mem_total_mb);
  const gpuPct = pct(s.gpu_alloc,   s.gpu_total);
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
      <div class="value">${s.cpu_alloc} / ${s.cpu_total}</div>
      <div class="sub">${cpuPct}% allocated</div>
      <div class="bar ${barClass(cpuPct)}"><span style="width:${cpuPct}%"></span></div>
    </div>
    <div class="card">
      <div class="label">Memory</div>
      <div class="value">${fmtMem(s.mem_alloc_mb)} / ${fmtMem(s.mem_total_mb)}</div>
      <div class="sub">${memPct}% allocated</div>
      <div class="bar ${barClass(memPct)}"><span style="width:${memPct}%"></span></div>
    </div>
    <div class="card">
      <div class="label">GPUs</div>
      <div class="value">${s.gpu_total - s.gpu_alloc} <span style="font-size:16px;font-weight:400;color:var(--muted)">idle / ${s.gpu_total}</span></div>
      <div class="sub">${gpuPct}% allocated &middot; ${s.gpu_total - s.gpu_alloc} idle</div>
      <div class="bar gpu ${barClass(gpuPct)}"><span style="width:${gpuPct}%"></span></div>
    </div>`;
}

// ── render GPU-by-type table ────────────────────────────────────────────────
function renderGpuTable(byType) {
  const tbody = document.querySelector('#gpu-table tbody');
  const entries = Object.entries(byType).sort((a,b) => b[1].total - a[1].total);
  if (!entries.length) {
    tbody.innerHTML = '<tr><td colspan="6" class="muted">No GPUs detected.</td></tr>';
    return;
  }
  tbody.innerHTML = entries.map(([type, v]) => {
    const p = pct(v.alloc, v.total);
    return `<tr>
      <td><b>${type}</b></td>
      <td>${v.alloc}</td>
      <td>${v.total - v.alloc}</td>
      <td>${v.total}</td>
      <td>${minibar(p, 'gpu')}${p}%</td>
      <td>${v.nodes}</td>
    </tr>`;
  }).join('');
}

// ── node sub-table (inside expanded partition row) ──────────────────────────
function buildNodeSubTable(partName) {
  const nodes = SNAPSHOT.nodes
    .filter(n => n.partitions.includes(partName))
    .sort((a, b) => b.gpu_idle - a.gpu_idle || a.name.localeCompare(b.name));
  if (!nodes.length)
    return '<div style="padding:10px;color:var(--muted)">No nodes in this partition.</div>';

  const rows = nodes.map(n => {
    const cpuP = pct(n.cpu_alloc, n.cpu_total);
    const memP = pct(n.mem_alloc_mb, n.mem_total_mb);
    const gpuCell = n.gpu_total
      ? minibar(pct(n.gpu_idle, n.gpu_total), 'gpu') + `${n.gpu_idle} / ${n.gpu_total}`
      : '<span class="muted">—</span>';
    const vram = n.gpu_vram_gb != null ? n.gpu_vram_gb + ' GB' : '—';
    return `<tr>
      <td><b>${n.name}</b></td>
      <td>${statePill(n.state)}</td>
      <td>${minibar(cpuP)}${n.cpu_alloc} / ${n.cpu_total}</td>
      <td>${n.load != null ? n.load : '—'}</td>
      <td>${minibar(memP)}${fmtMem(n.mem_alloc_mb)} / ${fmtMem(n.mem_total_mb)}</td>
      <td>${gpuCell}</td>
      <td class="muted">${vram}</td>
    </tr>`;
  }).join('');

  return `<div class="inner-wrap"><table class="inner-table">
    <thead><tr>
      <th>Node</th><th>State</th><th>CPU (alloc/total)</th><th>Load</th>
      <th>Memory (alloc/total)</th><th>GPU (idle/total)</th><th>VRAM</th>
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
      <td>${j.id}</td>
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

  const tbody = document.getElementById('part-tbody');
  tbody.innerHTML = '';

  for (const p of visible) {
    const isOpen  = expandedParts.has(p.name);
    const cpuP    = pct(p.cpu_alloc, p.cpu_total);
    const idleP   = pct(p.gpu_idle,  p.gpu_total);
    const gpuCell = p.gpu_total
      ? minibar(idleP, 'gpu') + `${p.gpu_idle} / ${p.gpu_total}`
      : '<span class="muted">—</span>';
    const vramCell = p.gpu_vram_gb != null
      ? `<b>${p.gpu_vram_gb}</b> GB`
      : '<span class="muted">—</span>';
    const runOpen  = expandedRunning.has(p.name);
    const pendOpen = expandedPending.has(p.name);

    const runSpan  = `<span class="job-toggle" data-part="${p.name}" data-kind="running"
      style="color:var(--good);cursor:pointer;border-bottom:1px dotted var(--good)"
      title="Click to ${runOpen ? 'hide' : 'show'} running jobs">${p.jobs_running} run</span>`;
    const pendSpan = `<span class="job-toggle" data-part="${p.name}" data-kind="pending"
      style="color:var(--warn);cursor:pointer;border-bottom:1px dotted var(--warn)"
      title="Click to ${pendOpen ? 'hide' : 'show'} pending jobs">${p.jobs_pending} pend</span>`;
    const jobsCell = `${runSpan}<span class="muted"> · </span>${pendSpan}`;

    const tr = document.createElement('tr');
    tr.className = 'part-row';
    tr.innerHTML = `
      <td class="toggle-cell">${isOpen ? '▼' : '▶'}</td>
      <td><b>${p.name}</b></td>
      <td>${p.avail}</td>
      <td>${p.timelimit}</td>
      <td>${p.nodes}</td>
      <td>${jobsCell}</td>
      <td>${minibar(cpuP)}${p.cpu_alloc} / ${p.cpu_total}</td>
      <td>${vramCell}</td>
      <td>${gpuCell}</td>`;

    // partition row click → toggle nodes
    tr.addEventListener('click', () => {
      if (expandedParts.has(p.name)) expandedParts.delete(p.name);
      else expandedParts.add(p.name);
      renderPartitions();
    });
    // run/pend spans → toggle job lists (stop propagation to avoid row toggle)
    tr.querySelectorAll('.job-toggle').forEach(span => {
      span.addEventListener('click', e => {
        e.stopPropagation();
        const kind = span.dataset.kind;
        const set  = kind === 'running' ? expandedRunning : expandedPending;
        if (set.has(p.name)) set.delete(p.name);
        else set.add(p.name);
        renderPartitions();
      });
    });
    tbody.appendChild(tr);

    // running jobs inline
    if (runOpen) {
      const runJobs = (p.jobs || []).filter(j => j.state === 'RUNNING');
      const expandTr = document.createElement('tr');
      expandTr.className = 'nodes-expand-row';
      const td = document.createElement('td');
      td.colSpan = 9;
      td.innerHTML = buildJobSubTable(runJobs, false);
      expandTr.appendChild(td);
      tbody.appendChild(expandTr);
    }
    // pending jobs inline
    if (pendOpen) {
      const pendJobs = (p.jobs || []).filter(j => j.state === 'PENDING');
      const expandTr = document.createElement('tr');
      expandTr.className = 'nodes-expand-row';
      const td = document.createElement('td');
      td.colSpan = 9;
      td.innerHTML = buildJobSubTable(pendJobs, true);
      expandTr.appendChild(td);
      tbody.appendChild(expandTr);
    }
    // nodes inline
    if (isOpen) {
      const expandTr = document.createElement('tr');
      expandTr.className = 'nodes-expand-row';
      const td = document.createElement('td');
      td.colSpan = 9;
      td.innerHTML = buildNodeSubTable(p.name);
      expandTr.appendChild(td);
      tbody.appendChild(expandTr);
    }
  }

  updatePartHeaders();
}

// ── init ───────────────────────────────────────────────────────────────────
renderSummary(SNAPSHOT.summary);
renderGpuTable(SNAPSHOT.summary.gpu_by_type);
wirePartHeaders();
document.getElementById('vram-min').addEventListener('input',  renderPartitions);
document.getElementById('idle-only').addEventListener('change', renderPartitions);
renderPartitions();
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
        if self.path in ("/", "/index.html"):
            body = render_page()
            self.send_response(200)
            self.send_header("Content-Type",   "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control",  "no-store")
            self.end_headers()
            self.wfile.write(body)
        else:
            body = b"not found"
            self.send_response(404)
            self.send_header("Content-Type",   "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def log_message(self, *_):
        pass


def main():
    ap = argparse.ArgumentParser(
        description="Tiny Slurm cluster dashboard (run on the login node).")
    ap.add_argument("--host",     default="0.0.0.0", help="bind address  (default: 0.0.0.0)")
    ap.add_argument("--port",     type=int, default=8000, help="bind port (default: 8000)")
    args = ap.parse_args()

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"slurmboard listening on http://{args.host}:{args.port}"
          f"  (runs sinfo/scontrol fresh on every page load)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
