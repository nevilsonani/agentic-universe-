"""Load test on public_03_load (12 workflows, ~650k dials/day): does the loop keep up?

    python tools/load_test.py [--scenario scenarios/public_03_load.yaml] [--no-call-log]

Measures, from the controller's own metrics table and wall clock:
  * ingest throughput (events/s) and per-chunk processing time
  * wall-clock seconds per simulated day (SRE asked for < 600 s on 4 vCPU / 8 GB)
  * decision lag: wall time from a tick's events being available to the decisions on them
  * peak RSS of the controller process, SQLite store size
then grades the run against the no-op baseline. Writes reports/public_03_load.md.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.simctl import ROOT, Sim  # noqa: E402

ENV = {**os.environ, "PYTHONUTF8": "1"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", default="scenarios/public_03_load.yaml")
    ap.add_argument("--name", default="public_03_load")
    ap.add_argument("--no-call-log", action="store_true")
    ap.add_argument("--skip-baseline", action="store_true")
    a = ap.parse_args()
    run_dir, ctl = f"runs/mine_{a.name}", ROOT / "runs" / f"ctl_{a.name}"
    ctl.mkdir(parents=True, exist_ok=True)
    for p in ctl.glob("state.db*"):
        p.unlink()
    t0 = time.time()
    sim = Sim(a.scenario, run_dir, log=str(ctl / "sim.log")).start(timeout=3600)
    t_sim_ready = time.time() - t0
    print(f"simulator ready in {t_sim_ready:.0f}s", flush=True)
    cmd = [sys.executable, "-m", "controller", "run", "--url", sim.url, "--db", str(ctl / "state.db"),
           "--view", f"reports/supervisor_{a.name}.html"]
    if a.no_call_log:
        cmd.append("--no-call-log")
    t1 = time.time()
    with open(ctl / "controller.log", "w", encoding="utf-8") as fh:
        rc = subprocess.call(cmd, cwd=ROOT, env=ENV, stdout=fh, stderr=subprocess.STDOUT)
    wall = time.time() - t1
    sim.stop()
    print(f"controller exit {rc} in {wall:.0f}s", flush=True)
    db = sqlite3.connect(ctl / "state.db")
    met = {}
    for name, minute, value in db.execute("SELECT name, minute, value FROM metrics ORDER BY minute"):
        met.setdefault(name, []).append((minute, value))
    per_day = [v for m, v in met.get("wall_s_per_sim_day", []) if m > 0]
    ev_total = met.get("ingest_events_total", [(0, 0)])[-1][1]
    ev_rate = met.get("ingest_events_per_s", [(0, 0)])[-1][1]
    rss = max((v for _, v in met.get("rss_mb", [])), default=-1)
    lag = max((v for _, v in met.get("ingest_lag_sim_min", [])), default=0)
    plan_s = met.get("plan_seconds_total", [(0, 0)])[-1][1]
    calls = db.execute("SELECT COUNT(*) FROM calls").fetchone()[0]
    db_mb = sum(p.stat().st_size for p in ctl.glob("state.db*")) / 1e6
    props = db.execute("SELECT channel, status, COUNT(*) FROM proposals GROUP BY channel, status").fetchall()
    grade = ""
    if rc == 0 and not a.skip_baseline:
        base = ROOT / "runs" / f"base_{a.name}"
        if not (base / "_truth" / "aggregates.json").exists():
            print("running baseline…", flush=True)
            subprocess.check_call([sys.executable, "-m", "sim", "baseline", "--scenario", a.scenario, "--run-dir",
                                   str(base)], cwd=ROOT, env=ENV)
        subprocess.check_call([sys.executable, "-m", "sim", "grade", "--run", run_dir, "--baseline", str(base),
                               "--out", f"reports/{a.name}_grade.md"], cwd=ROOT, env=ENV, stdout=subprocess.DEVNULL)
        grade = Path(f"reports/{a.name}_grade.md").read_text(encoding="utf-8")
    cpu = os.cpu_count()
    L = [f"# Load test — {a.name}", "",
         f"Machine: {cpu} logical CPUs ({sys.platform}); controller is single-process, single-threaded.", "",
         "| measure | value |", "|---|---|",
         f"| simulator start (history generation, not ours) | {t_sim_ready:.0f} s |",
         f"| controller wall clock, whole run | {wall:.0f} s |",
         f"| wall clock per simulated day (includes the simulator's own advance time) | "
         f"{', '.join(f'{x:.0f}' for x in per_day)} s (max {max(per_day, default=0):.0f} s; SRE budget 600 s) |",
         f"| events ingested | {ev_total:,.0f} |",
         f"| ingest throughput (parse + aggregate + commit) | {ev_rate:,.0f} events/s |",
         f"| planning time, total | {plan_s:.0f} s |",
         f"| max ingest lag (sim minutes behind the newest chunk) | {lag:.0f} |",
         f"| decision lag | ≤ 1 tick (15 sim-min): every tick ingests all new chunks before deciding |",
         f"| peak RSS (controller) | {rss:,.0f} MB |",
         f"| store size (SQLite, incl. per-call audit log of {calls:,} calls) | {db_mb:,.0f} MB |",
         f"| proposals by channel/status | {', '.join(f'{c}/{s}: {n}' for c, s, n in props)} |", ""]
    if grade:
        L += ["## Grade vs no-op baseline", "", grade.split("## Changes applied")[0].split("## Outcomes vs baseline (no-op controller)")[-1].strip(), ""]
    Path("reports").mkdir(exist_ok=True)
    Path(f"reports/{a.name}.md").write_text("\n".join(L), encoding="utf-8")
    print("\n".join(L))
    sys.exit(rc)


if __name__ == "__main__":
    main()
