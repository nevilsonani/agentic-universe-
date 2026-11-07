# On-call runbook — outcome-loop controller

For the engineer who gets paged when the author is on leave. Everything below works from the
controller's own store (`state.db`, one SQLite file next to the process). Nothing here needs the
simulator's logs.

## What the process is

One Python process per deployment (`./run.sh <PLATFORM_URL>` → `python -m controller run`). Each
15-minute tick it: ingests new event chunks → applies control events (config changes, approval
decisions) → polls/applies approvals → drives open incidents → checks caller IDs → plans (hourly) →
sends proposals → advances. All state is in `state.db` (WAL mode; `state.db-wal` / `-shm` next to it).

**Restarting is always safe.** Kill it any way you like and start it again with the same `--db`. It
restores aggregates (seconds; ~30 s on the load scenario), reconciles in-flight proposals against the
platform (`GET /approvals`, config history) before sending anything, and continues. Tested with 8 random
SIGKILLs per run: `python tools/crash_test.py` → `reports/crash_recovery.md`.

## First three commands

```bash
python -m controller health --db state.db        # metrics, open alerts, in-flight proposals
python -m controller view   --db state.db --serve 8800   # the supervisor's screen (read-only)
sqlite3 state.db "select minute, wf, kind, summary from decisions order by id desc limit 40"
```

`minute` is simulated minutes since D0 00:00 IST; `python -c "from controller.features import label; print(label(5835))"` → `D4 01:15`.

## Alerts

Severity is either **page** (act now) or **ticket** (next business morning). Alerts live in the `alerts`
table and on the view; each message says what to look at.

| code | severity | meaning | what to do |
|---|---|---|---|
| `ingest_lag` | page | the newest chunk we applied trails the newest published chunk by > 2 sim-hours; decisions are stale | `health`; check the log for chunk fetch / SQLite errors; disk full? restart is safe |
| `hallucination` | page | a workflow's hallucination-flag rate is confirmed above baseline (CUSUM + Poisson p<1e-3). The controller is already validating a fix and will request a safety approval | open the incident in the view; if the client's approver is unreachable, phone them — the request is in their queue with validation evidence |
| `halluc_no_fix` | page | no candidate prompt passed validation | a human must choose the prompt; incident detail lists every validation run |
| `credits` | page | an incident hit its credit cap (20) or the pool is empty | decide manually; raise `max_credits_per_incident` only with the CTO's ok |
| `did_flagged` | page if the workflow has no caller-ID auto-apply, else ticket | a caller ID's pickup collapsed vs peers at equal load; we are rotating it out (auto) or have asked for approval | if approval-only (e.g. KYC), chase the approver; quarantined numbers need a human to clear with the carrier — the controller never re-enables them |
| `proposal_failed:<code>` | ticket | the platform refused a request as malformed or not permitted (422/403) | a bug or a policy change: read `reasons` in `proposals`; the controller will not retry the same body |
| `proposal_rejected_invalid` | ticket | an approver rejected for a fixable reason (not a judgement call) | same as above |
| `credits_low` | ticket | < 20% of credits left | nothing is spent outside incidents; tell the platform team before the next incident |

Things that deliberately do **not** page: judgement-call rejections (we back off 4 h and re-plan),
expired proposals (treated as rejections, re-planned with fresh data), stale approvals after a supervisor
edit (expected; re-proposed on the new base), a failing canary caller ID (small share by design).

## Levers

```bash
# stop routine changes on one workflow (safety fixes still flow); visible in decisions as operator_pause
python -m controller pause  --db state.db --workflow ks_cancel --hours 12 --reason "client escalation"
python -m controller resume --db state.db --workflow ks_cancel
```

Client preferences (approval caps, quiet hours, notice before customer-facing auto-applies) are in
`config/tenant_prefs.yaml`; they apply on restart. They can only make the controller stricter than the
platform policy.

## Answering "why did X change?"

```bash
python -m controller why   --db state.db --workflow nw_renewal --at "D5 15:00"
python -m controller heard --db state.db --person ks_cancel:p0001234 --day 3
python -m controller digest --db state.db --tenant northwind_life --since D0 --until D7
```

`why` shows the version live at that time, who changed it (supervisor / approver with approval id and
decision time / auto-apply policy), the proposal's summary, rationale, impact score, affected leads, dry
run, its full lifecycle, and pickup / engagement / flag rate in the 24 h before vs after.

## Known sharp edges

- **Float encoding.** The platform compares allocation weights with exact float equality after
  re-normalising them. `governance.canon_config` sends weights in a form that round-trips bit-for-bit
  (`exact_repr`). If you see `auto_apply_not_permitted … dims not permitted: ['voice']` on a change that
  didn't touch voice, this is where to look.
- **Campaign-locked workflows** (`mid_flight: next_campaign_only`, e.g. KYC): allocation changes reach
  customers only from the next daily batch, so experiment data lags by up to a day. The planner waits
  ("awaiting data") rather than re-exploring.
- **The store grows** ~45 bytes per call (audit log). At production volume, partition `calls` by day and
  archive after the audit-retention period; aggregates are separate and small.
