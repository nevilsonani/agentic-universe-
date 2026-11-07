# Development log — what each iteration taught us

The commit history shows *what* changed; this is *why*, in order. Numbers are from `sim grade` against the
no-op baseline; "harmful" is the grader's verdict on a change at the moment it was applied (engagement
−1% or caller-ID health −2% under the hidden truth).

## 1. Read the contract, not the memos
Read `docs/SIMULATOR.md`, then the simulator and grader source to understand the *mechanics* the API
exposes (we never read hidden truth from the controller). Three facts shaped the design:
- **Caller-ID health falls with call velocity.** Spreading volume over more numbers in good standing is a
  large, safe pickup lever — and a single-DID workflow (KYC) is a concentration risk as well as slow.
- **Every applied change is judged at apply time.** Exploration must be risk-bounded per change, not just
  "on average": a 15% share on a −10% arm is already a harmful change.
- **Several memo claims are false against the policies** (Kestrel does not auto-apply "everything";
  Northwind KYC has auto-apply off; the LLM endpoint returns a random subset of the public catalog).

## 2. First end-to-end run (public_01): +19.8% conclusion, 1 harmful
Worked: bootstrap from 14 days of history (call-time model found `age_band` on its own, p<1e-6),
caller-ID canaries auto-applied at 00:00, one bundled approval per workflow at 10:00.
Broke: a 12.5% canary on a pre-flagged number cost 2.2% DID health → 8%, later 6%.

## 3. "Decay" that wasn't: Kestrel hourly auto-applies chased noise
W2 looked +32% on its first ~100 calls; hourly Thompson moved it to 75%; regression to the mean then read
as decay (z=4.6), and four rollback steps were harmful. Verified the simulator randomises fairly (a clean
80/20 A/A run, and an instrumented run comparing *true* per-call engagement probabilities by arm) and that
our estimator is calibrated (null z: sd 0.955, 5.0% beyond 1.96). The problem was aggressiveness:
→ evidence-gated ramp (share caps by posterior sd), 3-hour minimum between customer-facing auto-applies.

## 4. The decay test that only watched declared winners
W3 (novelty, decays from 1.15 to 0.93) sat at 80% for days: it was never formally "declared", so nobody
watched it. → watch every arm holding ≥40%. Then the rollback proposals were **approved and cancelled by
our own pre-apply check**, which re-scored them with the cumulative fit (where W3 still looked good). →
one decay-aware posterior shared by planner and pre-apply check.

## 5. Floats
The platform re-normalises submitted weights and compares them with exact equality. After a supervisor
edit re-normalised our stored weights (0.1 → 0.10000000000000002), an untouched `voice` dim looked
changed and a schedule-only auto-apply was refused (403). → send each untouched distribution in a form the
platform's own normalisation maps back to the stored floats bit-for-bit (verified by emulation; a wide
search finds a preimage for all of 3,000 generated drift cases). Supervisor-edit dims are also
reconciled with a tolerance, so a prompt-only edit no longer freezes `welcome_message` for 24 h.

## 6. Stop tuning on one noisy run
Results moved ±3 points between runs of the *same* code because the world reacts to our actions. Built
`tools/make_scenarios.py` (our own random truth: harmful arms, segment effects, novelty decay, pre-flagged
numbers, moved incidents) and `tools/evaluate.py` (8 scenarios in parallel). From here on, every rule
change was judged on the aggregate.

## 7. What the aggregate taught
- Shrinking the *recent* estimate toward 0 drags real winners into false "decay" → decay is now a decline
  **since the arm became the majority arm** (post-promotion vs recent, unshrunk, z ≥ 2.6, ≥5%, below
  control), or "worse than control" (90% upper bound < 1) on the post-promotion regime or the last day.
  Harmful changes 21 → 5.
- Thompson kept *ramping* an arm whose novelty was fading (the cumulative mean lags) → an arm may only
  gain share while recent evidence agrees with the cumulative estimate.
- No heterogeneity gate let noise invent segment effects (V4, truly 0.9×, got 35% in some cohorts);
  Bonferroni never let real ones through in 8 days → Cochran Q p < 0.01 gate + partial pooling for
  allocation; Bonferroni only for what we *claim*.
- KYC re-explored arms that were allocated but not yet observed (campaign lock delays exposure by a day)
  → "awaiting data" + a 300-call dwell before reallocating.
- An overloaded number underperforming is load, not a spam flag (KYC's D1 after carrying 100% for two
  weeks looked 0.34× next to fresh numbers and was quarantined) → quarantine only at comparable load;
  graduation needs a 0.95 lower bound (a healthy *fresh* number looks ~1.2×), keeping existing traffic
  needs only 0.85.

## 8. Observability that doesn't cry wolf
"Ingest lag = minutes since the last event" paged every night (no calls after 21:00); "since the newest
applied chunk" too (no chunks are published at night). Lag is now newest *published* chunk minus newest
*applied* chunk.

## 9. Proving the boring parts
Crash test with 8 random SIGKILLs (all invariants PASS against the platform's own records), end-to-end
tests that count every call event on the platform against our store, a load run on public_03, and Monte
Carlo calibration of every decision rule we quote.
