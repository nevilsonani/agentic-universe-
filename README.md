# Outcome-first agent loop — controller

An always-on controller that, for every workflow, **detects** (caller-ID collapse, hallucination spikes,
winners that stop winning), **experiments** (welcome × voice, per cohort, with a live holdout),
**proposes** through governance (bundled, timed to approver hours, with diff / affected leads / dry run /
impact score / rationale), **applies and learns** (promotes, decays, rolls back), and **explains** every
change from its own store.

- The assignment: [`docs/ASSIGNMENT.md`](docs/ASSIGNMENT.md) · the simulator contract: [`docs/SIMULATOR.md`](docs/SIMULATOR.md)
- **Design, decisions, stakeholder replies, results: [`SUBMISSION.md`](SUBMISSION.md)**
- On-call: [`docs/RUNBOOK.md`](docs/RUNBOOK.md)
- Reports: [`reports/`](reports/) (grades for public_01/02, load numbers for public_03, crash recovery, calibration, multi-scenario evaluation, supervisor views)

`sim/`, `scenarios/public_*`, `stakeholders/`, `examples/` are the assignment's, unchanged. The controller
talks to the simulator **only over HTTP** and never imports `sim` or reads scenario files or `runs/`.

## Quick start

```bash
pip install -r requirements.txt            # numpy, pyyaml, httpx (+ fastapi/uvicorn/pytest for the simulator & tests)

# the contract: start a simulator, then
python -m sim serve --scenario scenarios/public_01_dev.yaml --run-dir runs/mine01 &
./run.sh http://127.0.0.1:8700             # blocks until the scenario finishes; state in runs/controller-8700/

# or everything in one go: fresh simulator + controller + grade vs no-op baseline
python tools/run_scenario.py scenarios/public_01_dev.yaml --name public_01      # → reports/public_01.md
```

`run.sh` needs only Python ≥ 3.10 and the packages above (no Docker, no new services). Re-running it with
the same store (`CTL_DB=...`) resumes after a crash; a new path starts fresh.

## What to run

| command | what it does |
|---|---|
| `python -m controller run --url URL --db PATH [--view out.html]` | the controller (what `run.sh` starts) |
| `python -m controller why --db PATH --workflow ks_cancel --at "D3 14:00"` | why it changed, who approved, what happened next |
| `python -m controller heard --db PATH --person ks_cancel:p0001234 --day 3` | what a customer heard and why that variant was live |
| `python -m controller view --db PATH --serve 8800` | supervisor view (also written to `--view` every 6 sim-hours) |
| `python -m controller digest --db PATH --tenant northwind_life --since D0 --until D7` | weekly client summary (Compliance asked for it) |
| `python -m controller health --db PATH` | loop health, open alerts, in-flight proposals |
| `python -m controller pause --db PATH --workflow X --hours 12 --reason "..."` / `resume` | stop routine changes on one workflow |
| `python tools/run_scenario.py SCENARIO --name N` | simulator + controller + grade |
| `python tools/evaluate.py scenarios/public_0*.yaml scenarios/synthetic/*.yaml --jobs 8 --tag X` | many scenarios in parallel, aggregated |
| `python tools/make_scenarios.py --n 6` | synthetic scenarios with *our own* random truth (anti-overfitting fixtures) |
| `python tools/crash_test.py --kills 8` | SIGKILL the controller at random moments, verify invariants |
| `python tools/load_test.py` | public_03 throughput, wall-clock per sim day, memory |
| `python tools/calibrate.py` | Monte-Carlo false-winner rate, power, decay false alarms, CUSUM run lengths |
| `python -m pytest -q tests` | unit + end-to-end tests (~1 min) |

## Layout

```
controller/
  loop.py        the control loop: ingest → control events → bootstrap → approvals → incidents → caller-ID guard → plan → route → advance
  ingest.py      chunks → integer-keyed cell tables + per-call audit log, one transaction per chunk (exactly once)
  analysis.py    EngagementModel (arm effects per segment), PickupTimeModel (call-time), did_health (caller IDs)
  planner.py     facets: cohorts, schedule, caller ID, variants (batched Thompson + harm check + decay), prompt
  governance.py  proposal lifecycle, outbox, budgets, approver-hour windows, apply/stale/lapse, exact float encoding
  incidents.py   hallucination CUSUM → confirm → evidence-first fix → validate → safety approval → verify
  dryrun.py      diff, affected leads, IPS replay over logged calls (with unsupported share), impact score
  features.py    the shared Cohort object, feature packing, cohort design, time labels
  stats.py       stratified multi-arm GLM (MH generalisation), Cochran Q / DL, CUSUM, tails — numpy only
  store.py       SQLite schema (one file, WAL); registry.py: workflows & config versions
  audit.py view.py   why / heard / health / digest; supervisor HTML + read-only HTTP API
  settings.py    every threshold with the reason for its value
config/tenant_prefs.yaml   voluntary rules per client, stricter than policy (Kestrel: 2/day, not before 11:00, 60-min notice)
tools/           run_scenario, evaluate, make_scenarios, crash_test, load_test, calibrate, simctl
tests/           test_sim.py (given), test_controller_unit.py, test_controller_e2e.py
```

## Debugging in five commands

```bash
python -m controller health --db runs/ctl_public_02/state.db            # what's wrong right now
python -m controller why --db ... --workflow nw_renewal --at "D5 15:00"   # one change, end to end
sqlite3 runs/ctl_public_02/state.db "select minute, wf, kind, summary from decisions order by id desc limit 30"
sqlite3 ... "select proposal_id, status, summary from proposals where wf='ks_cancel' order by created_minute"
python -m controller view --db ... --serve 8800                          # the supervisor's screen
```

Every plan writes an `analysis` decision (what the model believed: per-arm rate ratios ± sd, decay tests,
why a change was skipped), so "why didn't it act earlier?" is answerable too.

## Extending

- **A new lever** (e.g. per-person call time): write a planner method returning a `Facet` (name, dims, urgency,
  summary, rationale, data, evidence, predicted) and teach `planner.compose` how to apply it. Governance,
  dry run, audit and the view pick it up.
- **A new detector**: if it must see every event exactly once, register an ingest hook
  (`Ingestor.hooks`, runs inside the chunk transaction, like the hallucination CUSUM); otherwise read the
  cell tables in `Controller._guard`/`_plan`.
- **A threshold**: `controller/settings.py` — each value says what it trades off. Re-run
  `tools/evaluate.py` and `tools/calibrate.py` before and after; don't tune on one public scenario.
- **A client's preference**: `config/tenant_prefs.yaml` (never looser than the platform policy).
