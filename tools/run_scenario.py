"""One command: fresh simulator + controller + grade.

    python tools/run_scenario.py scenarios/public_01_dev.yaml --name public_01 [--max-ticks N]

Writes runs/mine_<name>/ (simulator run dir), runs/ctl_<name>/state.db (controller store),
reports/<name>.md (grade vs baseline) and reports/supervisor_<name>.html.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.simctl import ROOT, Sim  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("scenario")
    ap.add_argument("--name", required=True)
    ap.add_argument("--max-ticks", type=int, default=None)
    ap.add_argument("--no-call-log", action="store_true")
    ap.add_argument("--no-grade", action="store_true")
    a = ap.parse_args()
    env = {**os.environ, "PYTHONUTF8": "1"}
    run_dir, ctl_dir, base_dir = f"runs/mine_{a.name}", Path(f"runs/ctl_{a.name}"), f"runs/base_{a.name}"
    if ctl_dir.exists():
        for p in ctl_dir.iterdir():
            p.unlink()
    ctl_dir.mkdir(parents=True, exist_ok=True)
    Path("reports").mkdir(exist_ok=True)
    with Sim(a.scenario, run_dir) as sim:
        t0 = time.time()
        cmd = [sys.executable, "-m", "controller", "run", "--url", sim.url, "--db", str(ctl_dir / "state.db"),
               "--view", f"reports/supervisor_{a.name}.html"]
        if a.max_ticks:
            cmd += ["--max-ticks", str(a.max_ticks)]
        if a.no_call_log:
            cmd += ["--no-call-log"]
        with open(ctl_dir / "controller.log", "w", encoding="utf-8") as fh:
            rc = subprocess.call(cmd, cwd=ROOT, stdout=fh, stderr=subprocess.STDOUT, env=env)
        print(f"controller exit {rc} after {time.time() - t0:.0f}s (log: {ctl_dir / 'controller.log'})")
    if rc == 0 and not a.no_grade and not a.max_ticks:
        if not Path(base_dir, "_truth", "aggregates.json").exists():
            subprocess.check_call([sys.executable, "-m", "sim", "baseline", "--scenario", a.scenario, "--run-dir",
                                   base_dir], cwd=ROOT, env=env)
        subprocess.check_call([sys.executable, "-m", "sim", "grade", "--run", run_dir, "--baseline", base_dir,
                               "--out", f"reports/{a.name}.md"], cwd=ROOT, env=env, stdout=subprocess.DEVNULL)
        print(Path(f"reports/{a.name}.md").read_text(encoding="utf-8")[:3000])
    sys.exit(rc)


if __name__ == "__main__":
    main()
