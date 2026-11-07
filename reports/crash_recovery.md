# Crash-recovery test — public_02_incidents

`python tools/crash_test.py --scenario scenarios/public_02_incidents.yaml --kills 8 --seed 7`

The controller was hard-killed 8 times (SIGKILL / TerminateProcess — no cleanup handlers run) after a random 2–14 s of wall time each, and restarted on the same SQLite store each time. Total wall time 84 s; final exit code 0.

## Kills

| # | wall s before kill | sim time at kill | ingest cursor at kill |
|---|---|---|---|
| 1 | 5.9 | 2027-01-04T11:00 | 10 |
| 2 | 3.8 | 2027-01-04T17:00 | 34 |
| 3 | 9.8 | 2027-01-06T10:00 | 105 |
| 4 | 2.9 | 2027-01-06T13:00 | 117 |
| 5 | 8.4 | 2027-01-07T15:00 | 174 |
| 6 | 6.4 | 2027-01-08T12:00 | 211 |
| 7 | 2.7 | 2027-01-08T13:00 | 215 |
| 8 | 8.1 | 2027-01-09T13:00 | 264 |

## Invariants (checked against the platform's own records)

| check | result | detail |
|---|---|---|
| scenario finished | PASS | 2027-01-14T00:00:00+05:30 |
| one 'controller_restarted' decision per kill | PASS | 8 restarts / 8 kills |
| no proposal applied twice | PASS | 44 controller config versions |
| every controller config version = one applied proposal of ours | PASS | [] |
| nothing stuck in sending/applying | PASS | [] |
| every platform approval is known to us (none lost) | PASS | 16 approvals |
| every call event ingested exactly once | PASS | 245,032 ours / 245,032 platform |
| learned state survived (DID health, cohort design, decay/winner flags) | PASS | 3 workflows |

Proposal outcomes: {"applied": 44, "stale": 1}

## Outcome of the crashed run vs no-op baseline

conclusion +30.9% · pickup +23.9% · engagement +3.3% · changes: {'beneficial': 13, 'neutral': 31} · governance: {"approval_requests": 16, "auto_apply_rejected": 0, "apply_conflicts": 0, "lapsed": 0}

## What state is lost on a kill

Nothing that has been committed. Each ingested chunk commits atomically with the cursor; each proposal row is committed before its HTTP call and reconciled against GET /approvals or config history on restart; learned state (DID health, cohort design, winners, decay flags, CUSUM) lives in the same store. What a kill costs is time: the in-memory aggregates are rebuilt from SQLite on start (well under a second here, ~20–40 s on the load scenario) and the chunk being processed is re-read.

**Overall: PASS**