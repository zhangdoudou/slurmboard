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
import html as _html
import json
import os
import re
import subprocess
import getpass
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
    # %M elapsed/queue time  %C cpus  %b gres  %R reason/nodelist  %V submit time
    text = _run(["squeue", "-h", "-o", "%P|%i|%u|%j|%T|%M|%C|%b|%R|%V"])
    counts = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("|", 9)  # maxsplit keeps %V intact
        if len(parts) < 9:
            continue
        part, jid, user, name, state, time_used, cpus, gres, reason = parts[:9]
        submit = parts[9] if len(parts) > 9 else None
        state_up = state.upper()
        c = counts.setdefault(part, {"running": 0, "pending": 0, "jobs": []})
        if state_up == "RUNNING":
            c["running"] += 1
        elif state_up == "PENDING":
            c["pending"] += 1
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


_JOBS_HIST_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jobs_history.json")
_JOBS_HIST_TTL  = 7 * 24 * 3600  # seconds

def _load_job_hist():
    try:
        with open(_JOBS_HIST_PATH) as f:
            return json.load(f)
    except Exception:
        return {}

def _save_job_hist(hist):
    try:
        with open(_JOBS_HIST_PATH, "w") as f:
            json.dump(hist, f)
    except Exception:
        pass


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

    current_user = getpass.getuser()

    # Update persistent job history
    hist     = _load_job_hist()
    now_ts   = time.time()
    active   = set()
    for jc_data in job_counts.values():
        for j in jc_data["jobs"]:
            if j["user"] == current_user:
                active.add(j["id"])
                hist[j["id"]] = {**j, "last_seen": now_ts, "done": False}
    for jid in list(hist.keys()):
        jdata = hist[jid]
        if jid not in active and not jdata.get("done"):
            hist[jid] = {**jdata, "done": True, "done_at": now_ts}
        if now_ts - jdata.get("done_at", jdata.get("last_seen", 0)) > _JOBS_HIST_TTL:
            del hist[jid]
    _save_job_hist(hist)

    return {
        "generated_at":    time.strftime("%Y-%m-%d %H:%M:%S"),
        "current_user":    current_user,
        "user_jobs":       list(hist.values()),
        "summary":         summary,
        "partitions":      partitions,
        "nodes":           nodes,
    }


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


def collect_job_detail(jobid):
    if not re.match(r"^\d+(_\d+)?$", str(jobid)):
        raise ValueError(f"Invalid job ID: {jobid!r}")
    text = _run(["scontrol", "show", "job", str(jobid)])

    info = {}
    for key, rx in _JOB_RE.items():
        m = rx.search(text)
        info[key] = m.group(1).strip() if m else None

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
  }
  * { box-sizing: border-box; }
  body { margin: 0; font-family: -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
         background: var(--bg); color: var(--text); font-size: 14px;
         display: flex; flex-direction: column; height: 100vh; overflow: hidden; }
  header { flex-shrink: 0; padding: 12px 24px; border-bottom: 1px solid var(--border);
           display: flex; align-items: baseline; gap: 16px; flex-wrap: wrap; }
  header h1 { margin: 0; font-size: 20px; font-weight: 600; }
  header .meta { color: var(--muted); font-size: 12px; }
  header .reload { margin-left: auto; background: var(--accent); color: #fff; border: none;
                   border-radius: 6px; padding: 6px 14px; font-size: 13px; cursor: pointer; }
  header .reload:hover { filter: brightness(1.1); }
  main { flex: 1; min-height: 0; display: flex; flex-direction: column;
         padding: 12px 20px 0; overflow: hidden; }
  footer { flex-shrink: 0; text-align: center; color: var(--muted); font-size: 11px;
           padding: 7px 20px; border-top: 1px solid var(--border); }
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
  /* compact cards for sidebar — single column */
  .left-col .cards { grid-template-columns: 1fr; gap: 6px; }
  .left-col .card { padding: 8px 12px; }
  .left-col .card .label { font-size: 11px; }
  .left-col .card .value { font-size: 15px; line-height: 1.3; }
  .left-col .card .sub { display: none; }
  .left-col .card .bar { height: 5px; margin-top: 5px; }

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
  /* compact partition table to avoid horizontal scroll */
  #part-table th, #part-table td { padding: 5px 7px; }
  th { color: var(--muted); font-weight: 600; font-size: 12px; text-transform: uppercase;
       letter-spacing: .04em; cursor: pointer; user-select: none; }
  th.no-sort { cursor: default; }
  th:not(.no-sort):hover { color: var(--text); }
  tr:last-child > td { border-bottom: none; }
  tr:hover > td { background: rgba(255,255,255,.03); }

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
  /* three-column layout with draggable resizers */
  .three-col  { flex: 1; min-height: 0; display: flex; flex-direction: row; overflow: hidden; }
  .left-col   { width: 25%; min-width: 100px; flex-shrink: 0; overflow: auto; padding: 0 16px 20px 0; }
  .center-col { flex: 1; min-width: 0; overflow: auto; padding: 0 16px 20px; }
  .right-col  { width: 25%; min-width: 180px; flex-shrink: 0; overflow: auto; padding: 0 0 20px 16px; }
  .col-resizer { width: 4px; flex-shrink: 0; background: var(--border); cursor: col-resize;
                 user-select: none; position: relative; transition: background .15s; }
  .col-resizer:hover, .col-resizer.dragging { background: var(--accent); }
  .col-resizer::after { content: ''; position: absolute; inset: 0 -5px; }
  .left-col h2:first-child, .center-col h2:first-child, .right-col h2:first-child { margin-top: 0; }

  /* compact tables in left/right sidebars */
  .left-col table { font-size: 12px; }
  .left-col th, .left-col td { padding: 5px 8px; }

  /* user jobs table — same padding as partition table */
  #uj-table th, #uj-table td { padding: 5px 7px; }
  .uj-id { color: var(--accent); text-decoration: none; border-bottom: 1px dotted var(--accent); }
  .uj-id:hover { color: #fff; }
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
</style>
</head>
<body>
<header>
  <h1>&#9881; Slurm Dashboard</h1>
  <div class="meta" id="snap-meta">snapshot taken at __GENERATED_AT__ &middot; reload the page to refresh</div>
  <button class="reload" onclick="location.reload()">&#x21bb; Refresh</button>
</header>
<main>
  <div class="three-col">

    <aside class="left-col">
      <h2>Cluster summary</h2>
      <div class="cards" id="summary-cards"></div>

      <h2>GPUs by type</h2>
      <table id="gpu-table">
        <thead><tr>
          <th class="no-sort">Type</th>
          <th class="no-sort">Alloc</th>
          <th class="no-sort">Idle</th>
          <th class="no-sort">Total</th>
          <th class="no-sort">Idle%</th>
          <th class="no-sort">Nodes</th>
        </tr></thead>
        <tbody></tbody>
      </table>
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
      <h2>My Jobs <span id="my-jobs-user" class="muted" style="font-size:12px;font-weight:400;text-transform:none;letter-spacing:0"></span> <span id="uj-refresh-btn" class="th-refresh" title="Refresh">&#x21bb;</span></h2>
      <div id="user-jobs-panel"></div>
    </aside>

  </div>
</main>
<footer>slurmboard &middot; data sourced live from <code>sinfo</code> / <code>scontrol</code> on this login node &middot; reload to refresh</footer>

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
  renderPartitions();

  try {
    const resp = await fetch('/data');
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    const newSnap = await resp.json();
    if (newSnap.error) throw new Error(newSnap.error);

    if (partName) {
      // Patch only this partition's entry and its nodes
      const newPart = newSnap.partitions.find(p => p.name === partName);
      if (newPart) {
        const idx = SNAPSHOT.partitions.findIndex(p => p.name === partName);
        if (idx >= 0) SNAPSHOT.partitions[idx] = newPart;
        else SNAPSHOT.partitions.push(newPart);
      }
      newSnap.nodes.forEach(n => {
        if (!n.partitions.includes(partName)) return;
        const i = SNAPSHOT.nodes.findIndex(sn => sn.name === n.name);
        if (i >= 0) SNAPSHOT.nodes[i] = n; else SNAPSHOT.nodes.push(n);
      });
    } else {
      // Full snapshot replace — keep expand/sort state (those live outside SNAPSHOT)
      SNAPSHOT = newSnap;
      const meta = document.getElementById('snap-meta');
      if (meta) meta.textContent =
        `snapshot taken at ${newSnap.generated_at} · reload the page to refresh`;
      renderSummary(SNAPSHOT.summary);
      renderGpuTable(SNAPSHOT.summary.gpu_by_type);
    }
  } catch(e) {
    console.error('slurmboard refresh failed:', e);
  }

  refreshingParts.delete(key);
  if (hdrBtn) hdrBtn.classList.remove('loading');
  renderPartitions();
  renderUserJobs();
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

  // header toggle: expand all nodes / collapse all
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
    else         visible.forEach(p => { expandState[p.name] = 'nodes'; });
    renderPartitions();
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
  const memIdle = pct(s.mem_total_mb - s.mem_alloc_mb, s.mem_total_mb);
  const gpuIdle = pct(s.gpu_total   - s.gpu_alloc,   s.gpu_total);
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
      <div class="value">${fmtMem(s.mem_total_mb - s.mem_alloc_mb)} / ${fmtMem(s.mem_total_mb)}</div>
      <div class="sub">${memIdle}% idle</div>
      <div class="bar ${idleBarClass(memIdle)}"><span style="width:${memIdle}%"></span></div>
    </div>
    <div class="card">
      <div class="label">GPUs</div>
      <div class="value">${s.gpu_total - s.gpu_alloc} <span style="font-size:13px;font-weight:400;color:var(--muted)">idle / ${s.gpu_total}</span></div>
      <div class="sub">${gpuIdle}% idle</div>
      <div class="bar ${idleBarClass(gpuIdle)}"><span style="width:${gpuIdle}%"></span></div>
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
    const idlePct = pct(v.total - v.alloc, v.total);
    return `<tr>
      <td><b>${type}</b></td>
      <td>${v.alloc}</td>
      <td>${v.total - v.alloc}</td>
      <td>${v.total}</td>
      <td>${minibar(idlePct, 'gpu')}${idlePct}%</td>
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
      <td><a href="/job/${j.id}" style="color:var(--accent);border-bottom:1px dotted var(--accent)">${j.id}</a></td>
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

  const anyOpen = visible.some(p => expandState[p.name]);
  document.getElementById('part-th-toggle').textContent = anyOpen ? '▼' : '▶';

  const tbody = document.getElementById('part-tbody');
  tbody.innerHTML = '';

  for (const p of visible) {
    const cur       = expandState[p.name];   // "nodes"|"running"|"pending"|undefined
    const cpuIdleP  = pct(p.cpu_total - p.cpu_alloc, p.cpu_total);
    const idleP     = pct(p.gpu_idle,  p.gpu_total);
    const gpuCell = p.gpu_total
      ? minibar(idleP, 'gpu') + `${p.gpu_idle} / ${p.gpu_total}`
      : '<span class="muted">—</span>';
    const vramCell = p.gpu_vram_gb != null
      ? `<b>${p.gpu_vram_gb}</b> GB`
      : '<span class="muted">—</span>';

    const runSpan  = `<span class="job-toggle" data-kind="running"
      style="color:var(--good);cursor:pointer;border-bottom:1px dotted var(--good)"
      ><span class="job-num">${p.jobs_running}</span> run</span>`;
    const pendSpan = `<span class="job-toggle" data-kind="pending"
      style="color:var(--warn);cursor:pointer;border-bottom:1px dotted var(--warn)"
      ><span class="job-num">${p.jobs_pending}</span> pend</span>`;

    const isRefreshing = refreshingParts.has(p.name) || refreshingParts.has('*');
    const rowRefreshHtml = isRefreshing
      ? '<span class="row-refresh loading" title="Refreshing…">&#x21bb;</span>'
      : `<span class="row-refresh" data-part="${p.name}" title="Refresh partition">&#x21bb;</span>`;

    const tr = document.createElement('tr');
    tr.className = 'part-row';
    tr.innerHTML = `
      <td class="toggle-cell">${cur ? '▼' : '▶'} ${rowRefreshHtml}</td>
      <td><b class="part-name-link">${p.name}</b></td>
      <td>${p.avail}</td>
      <td>${p.timelimit}</td>
      <td>${p.nodes}</td>
      <td style="white-space:nowrap">${runSpan}<span class="muted"> · </span>${pendSpan}</td>
      <td>${minibar(cpuIdleP, 'gpu')}${p.cpu_total - p.cpu_alloc} / ${p.cpu_total}</td>
      <td>${vramCell}</td>
      <td>${gpuCell}</td>`;

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
      if (cur) delete expandState[p.name];
      else expandState[p.name] = 'nodes';
      renderPartitions();
    });
    // partition name: mutual-exclusion toggle for nodes
    tr.querySelector('.part-name-link').addEventListener('click', () => {
      if (cur === 'nodes') delete expandState[p.name];
      else expandState[p.name] = 'nodes';
      renderPartitions();
    });
    // run/pend spans: mutual-exclusion toggle
    tr.querySelectorAll('.job-toggle').forEach(span => {
      span.addEventListener('click', () => {
        const kind = span.dataset.kind;
        if (cur === kind) delete expandState[p.name];
        else expandState[p.name] = kind;
        renderPartitions();
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
        const jobs = (p.jobs || []).filter(j => j.state === cur.toUpperCase());
        td.innerHTML = buildJobSubTable(jobs, cur === 'pending');
      }
      expandTr.appendChild(td);
      tbody.appendChild(expandTr);
    }
  }

  updatePartHeaders();
}

// ── user jobs panel ─────────────────────────────────────────────────────────
let ujSortKey = 'state', ujSortDir = 1;

function renderUserJobs() {
  const user = SNAPSHOT.current_user || '';
  const userEl = document.getElementById('my-jobs-user');
  if (userEl) userEl.textContent = user ? `(${user})` : '';

  // History maintained server-side in ~/.slurmboard_jobs.json
  const allJobs = SNAPSHOT.user_jobs || [];
  const panel = document.getElementById('user-jobs-panel');
  if (!allJobs.length) {
    panel.innerHTML = '<p class="muted" style="font-size:13px;margin:4px 0">No jobs found.</p>';
    return;
  }

  // Sort
  const stateRank = j => j.done ? 3
    : j.state === 'RUNNING' || j.state === 'COMPLETING' ? 0
    : j.state === 'PENDING' ? 1 : 2;
  const numId = j => parseInt(j.id) || 0;
  allJobs.sort((a, b) => {
    let cmp = 0;
    if      (ujSortKey === 'state')     cmp = stateRank(a) - stateRank(b) || numId(b) - numId(a);
    else if (ujSortKey === 'id')        cmp = numId(a) - numId(b);
    else if (ujSortKey === 'time')      cmp = a.time.localeCompare(b.time);
    else if (ujSortKey === 'partition') cmp = (a.partition||'').localeCompare(b.partition||'');
    else if (ujSortKey === 'submit')    cmp = (a.submit||'').localeCompare(b.submit||'');
    return ujSortDir * cmp;
  });

  const nRun  = allJobs.filter(j => !j.done && (j.state==='RUNNING'||j.state==='COMPLETING')).length;
  const nPend = allJobs.filter(j => !j.done && j.state==='PENDING').length;
  const nDone = allJobs.filter(j => j.done).length;

  // Slurm state → abbreviation + color (mirrors squeue output codes)
  const STATE_ABBR = {
    RUNNING:'R', COMPLETING:'CG', PENDING:'PD', COMPLETED:'CD',
    FAILED:'F', CANCELLED:'CA', TIMEOUT:'TO', NODE_FAIL:'NF', PREEMPTED:'PR',
  };
  function stateCell(j) {
    if (j.done) return `<span style="color:var(--accent);font-weight:700;font-size:11px">DONE</span>`;
    const abbr  = STATE_ABBR[j.state] || j.state.slice(0,2);
    const color = (j.state==='RUNNING'||j.state==='COMPLETING') ? 'var(--good)'
                : j.state==='PENDING' ? 'var(--warn)' : 'var(--muted)';
    return `<span style="color:${color};font-weight:700;font-size:11px">${abbr}</span>`;
  }

  function ujTh(key, label) {
    const arrow = ujSortKey === key ? (ujSortDir > 0 ? ' ↑' : ' ↓') : '';
    return `<th data-k="${key}" data-label="${label}" style="cursor:pointer;user-select:none">${label}${arrow}</th>`;
  }

  function fmtDate(s) {
    if (!s || s === 'N/A' || s === 'Unknown') return '—';
    const d = new Date(s.replace('T', ' '));
    if (isNaN(d)) return s.slice(0, 10);
    return `${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')} `
         + `${String(d.getHours()).padStart(2,'0')}:${String(d.getMinutes()).padStart(2,'0')}`;
  }

  const rows = allJobs.map(j => {
    const part = j.partition || '—';
    return `<tr style="${j.done?'opacity:.4':''}">
      <td><a class="uj-id" href="/job/${j.id}" title="${j.name}">${j.id}</a></td>
      <td>${stateCell(j)}</td>
      <td class="muted" style="max-width:100px;overflow:hidden;text-overflow:ellipsis" title="${part}">${part}</td>
      <td class="muted">${j.time}</td>
      <td class="muted">${fmtDate(j.submit)}</td>
    </tr>`;
  }).join('');

  panel.innerHTML = `
    <div style="font-size:12px;color:var(--muted);margin-bottom:6px">
      <span style="color:var(--good)">${nRun}</span> R &nbsp;·&nbsp;
      <span style="color:var(--warn)">${nPend}</span> PD
      ${nDone ? `&nbsp;·&nbsp; ${nDone} done` : ''}
    </div>
    <table id="uj-table">
      <thead><tr>
        ${ujTh('id','ID')}
        ${ujTh('state','St')}
        ${ujTh('partition','Partition')}
        ${ujTh('time','Time')}
        ${ujTh('submit','Date')}
      </tr></thead>
      <tbody>${rows}</tbody>
    </table>`;

  document.querySelectorAll('#uj-table th[data-k]').forEach(th =>
    th.addEventListener('click', () => {
      const key = th.dataset.k;
      if (ujSortKey === key) ujSortDir *= -1;
      else { ujSortKey = key; ujSortDir = 1; }
      renderUserJobs();
    }));
}

// ── draggable column resizers ───────────────────────────────────────────────
function initColumnResizers() {
  const leftCol  = document.querySelector('.left-col');
  const rightCol = document.querySelector('.right-col');

  // Restore saved widths (CSS width:25% is the default when no saved value)
  try {
    const saved = JSON.parse(localStorage.getItem('sb_col_w') || 'null');
    if (saved && saved.left)  leftCol.style.width  = saved.left  + 'px';
    if (saved && saved.right) rightCol.style.width = saved.right + 'px';
  } catch(e) {}

  function saveWidths() {
    try {
      localStorage.setItem('sb_col_w', JSON.stringify({
        left:  leftCol.offsetWidth,
        right: rightCol.offsetWidth,
      }));
    } catch(e) {}
  }

  function wire(id, col, sign) {
    // sign=+1: drag right → col grows; sign=-1: drag right → col shrinks
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

  wire('resizer-left',  leftCol,  +1);  // left resizer → left col grows/shrinks
  wire('resizer-right', rightCol, -1);  // right resizer → right col shrinks/grows
}

// ── init ───────────────────────────────────────────────────────────────────
renderSummary(SNAPSHOT.summary);
renderGpuTable(SNAPSHOT.summary.gpu_by_type);
wirePartHeaders();
document.getElementById('vram-min').addEventListener('input',  renderPartitions);
document.getElementById('idle-only').addEventListener('change', renderPartitions);
initColumnResizers();
document.getElementById('uj-refresh-btn').addEventListener('click', () => refreshData());
renderPartitions();
renderUserJobs();
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
        elif self.path.startswith("/job/"):
            jobid = self.path[5:].strip("/").split("?")[0]
            body  = render_job_page(jobid).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type",   "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control",  "no-store")
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/data":
            try:
                body = json.dumps(build_snapshot()).encode("utf-8")
            except Exception as exc:
                body = json.dumps({"error": str(exc)}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type",   "application/json; charset=utf-8")
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
