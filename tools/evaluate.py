"""Run the controller on many scenarios in parallel and aggregate the grader's numbers.

    python tools/evaluate.py scenarios/public_01_dev.yaml scenarios/synthetic/*.yaml --jobs 6 --tag v1

For each scenario: baseline (cached), fresh simulator + controller, `sim grade --json`.
Summary: lift per KPI (ALL), harmful/beneficial changes, decaying-variant share after
onset, incident MTTR, flagged-DID share, governance counters. Results go to
runs/eval_<tag>/summary.json so two controller versions can be compared on the
same fixtures — one noisy run on one public truth is not evidence.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.simctl import ROOT, Sim  # noqa: E402

ENV = {**os.environ, "PYTHONUTF8": "1"}


def one(scn: str, tag: str) -> dict:
    name = Path(scn).stem
    base = ROOT / "runs" / f"base_{name}"
    if not (base / "_truth" / "aggregates.json").exists():
        subprocess.check_call([sys.executable, "-m", "sim", "baseline", "--scenario", scn, "--run-dir", str(base)],
                              cwd=ROOT, env=ENV, stdout=subprocess.DEVNULL)
    rd = ROOT / "runs" / f"eval_{tag}" / name
    rd.mkdir(parents=True, exist_ok=True)
    db = rd / "state.db"
    for p in rd.glob("state.db*"):
        p.unlink()
    t0 = time.time()
    with Sim(scn, str(rd / "sim")) as sim:
        with open(rd / "controller.log", "w", encoding="utf-8") as fh:
            rc = subprocess.call([sys.executable, "-m", "controller", "run", "--url", sim.url, "--db", str(db)],
                                 cwd=ROOT, env=ENV, stdout=fh, stderr=subprocess.STDOUT)
    wall = time.time() - t0
    if rc != 0:
        return {"scenario": name, "error": f"controller exit {rc}", "wall_s": wall}
    g = json.loads(subprocess.check_output([sys.executable, "-m", "sim", "grade", "--run", str(rd / "sim"),
                                            "--baseline", str(base), "--json"], cwd=ROOT, env=ENV))
    return summarize(name, g, wall)


def summarize(name: str, g: dict, wall: float) -> dict:
    d = g["delta_vs_baseline"]["ALL"]
    v = g["controller_changes"]["verdicts"]
    decay = []
    for key, by_day in g["decaying_variant_share_after_onset"].items():
        days = sorted(by_day, key=int)
        decay.append({"variant": key, "share_day2+": [by_day[x] for x in days[2:]] or [by_day[days[-1]]]})
    inc = []
    for i in g["incidents"]:
        if "mttr_minutes" in i:
            inc.append({"type": "prompt", "workflow": i["workflow"], "mttr": i["mttr_minutes"]})
        if "flagged_did_share_by_day" in i:
            days = sorted(i["flagged_did_share_by_day"], key=int)
            inc.append({"type": "did", "workflow": i["workflow"],
                        "share_after": [i["flagged_did_share_by_day"][x] for x in days[1:3]]})
    per_wf = {wf: {k: (round(x, 4) if x is not None else None) for k, x in m.items()}
              for wf, m in g["delta_vs_baseline"].items() if wf != "ALL"}
    gap = {wf: m.get("engagement_gap_captured") for wf, m in g["kpis"].items()}
    return {"scenario": name, "wall_s": round(wall), "conclusion": d["conclusion_rate"], "pickup": d["pickup_rate"],
            "engagement": d["engagement_rate"], "harmful": v.get("harmful", 0), "beneficial": v.get("beneficial", 0),
            "neutral": v.get("neutral", 0), "harmful_detail": [
                {k: c[k] for k in ("workflow_id", "version", "dims", "engage_delta", "did_health_delta")}
                for c in g["controller_changes"]["detail"] if c["verdict"] == "harmful"],
            "per_workflow": per_wf, "gap_captured": gap, "decay": decay, "incidents": inc,
            "governance": {k: g["governance"][k] for k in ("approval_requests", "auto_apply_rejected",
                                                             "apply_conflicts", "lapsed")},
            "rejected": g["governance"]["decisions"].get("rejected", 0),
            "expired": g["governance"]["decisions"].get("expired", 0), "credits": g["credits_used"]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("scenarios", nargs="+")
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--tag", default="dev")
    a = ap.parse_args()
    with ThreadPoolExecutor(a.jobs) as ex:
        res = list(ex.map(lambda s: one(s, a.tag), a.scenarios))
    out = ROOT / "runs" / f"eval_{a.tag}" / "summary.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(res, indent=2))
    print(f"{'scenario':34s} {'concl':>7s} {'pickup':>7s} {'engage':>7s}  harm ben neu  rej exp aa_rej conf  decay(day2+)  incidents")
    for r in res:
        if "error" in r:
            print(f"{r['scenario']:34s} ERROR {r['error']}")
            continue
        dec = ";".join(",".join(f"{x:.2f}" for x in z["share_day2+"][:3]) for z in r["decay"]) or "-"
        inc = ";".join(f"{i['type']}:{i.get('mttr', i.get('share_after'))}" for i in r["incidents"]) or "-"
        gv = r["governance"]
        print(f"{r['scenario']:34s} {r['conclusion']:+7.1%} {r['pickup']:+7.1%} {r['engagement']:+7.1%}  "
              f"{r['harmful']:4d} {r['beneficial']:3d} {r['neutral']:3d}  {r['rejected']:3d} {r['expired']:3d} "
              f"{gv['auto_apply_rejected']:6d} {gv['apply_conflicts']:4d}  {dec:12s}  {inc}")
    ok = [r for r in res if "error" not in r]
    if ok:
        n = len(ok)
        print(f"{'MEAN':34s} {sum(r['conclusion'] for r in ok) / n:+7.1%} {sum(r['pickup'] for r in ok) / n:+7.1%} "
              f"{sum(r['engagement'] for r in ok) / n:+7.1%}  {sum(r['harmful'] for r in ok):4d} "
              f"{sum(r['beneficial'] for r in ok):3d} {sum(r['neutral'] for r in ok):3d}")
        for r in ok:
            for h in r["harmful_detail"]:
                print(f"  harmful: {r['scenario']} {h}")


if __name__ == "__main__":
    main()
