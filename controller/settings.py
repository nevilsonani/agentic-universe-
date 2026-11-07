"""Every tunable in one place, with the reason it has the value it has.

Thresholds are deliberately *not* fitted to the public scenarios. Where a value
trades speed against false positives, the trade is written next to it and in
SUBMISSION.md §5. Tenant preferences (voluntary rules stricter than the signed
policy) live in `config/tenant_prefs.yaml` so ops can change them without a deploy.
"""
from __future__ import annotations

from dataclasses import dataclass, field, fields
from pathlib import Path

import yaml

MIN_PER_DAY = 1440
TICK = 15
AGENT_DIMS = ("welcome_message", "voice", "caller_id")
VARIANT_DIMS = ("welcome_message", "voice")           # dims we A/B test for engagement
CUSTOMER_FACING = ("welcome_message", "voice", "prompt_version")  # what a customer hears
OPERATIONAL = ("caller_id", "schedule")


@dataclass
class Settings:
    # ---------------------------------------------------------------- experiment / learning
    prior_sd: float = 0.07            # skeptical prior on an arm's log-RR vs control (most variants are within ±15%)
    explore_risk_quantile: float = 0.25  # P(change is worse than -1%) we accept for a pure exploration step
    explore_risk_quantile_guardrail: float = 0.10  # ...when engagement is the workflow's *guardrail* (KYC)
    ts_gamma: float = 2.0             # share ∝ P(best)^γ — sharper than probability matching
    min_calls_between_variant_changes: int = 300  # dwell: connected calls under the latest mix first
    harm_threshold: float = -0.01     # "harmful" = expected engagement drop of more than 1% (relative)
    max_harm_prob: float = 0.10       # exploitation changes: P(delta < harm_threshold) must stay below this
    explore_share_max: float = 0.15   # per-arm share cap for the very first exploration step
    explore_share_min: float = 0.04
    unproven_cap: float = 0.25        # max share for an arm whose P(RR > 0.98) < proven_prob
    proven_prob: float = 0.80
    holdout_share: float = 0.10       # control arm kept live: measures lift honestly, detects decay
    floor_share: float = 0.05         # minimum share for a still-uncertain challenger
    share_step: float = 0.05          # allocations are rounded to this grid (no churn on noise)
    winner_p_best: float = 0.95       # declare a winner: P(best) and P(better than control)
    winner_p_ref: float = 0.99
    winner_min_lift: float = 0.02
    winner_max_sd: float = 0.06       # ...and the posterior must be tight enough to act on
    loser_p: float = 0.95             # drop an arm when P(worse than control) >= this
    loser_max_rr: float = 0.98
    het_alpha: float = 0.05           # segment-specific *claims* in the audit: Bonferroni-divided
    het_alloc_p: float = 0.01         # segment deviations used for allocation only if Cochran's Q p < this
    recent_half_life_days: float = 1.0   # time-discounted estimate used for decay detection
    decay_window_days: int = 1        # 'recent' = today + yesterday (24–48h); 'earlier' = everything before
    decay_drop_z: float = 1.0         # an arm holding ≥ decay_min_share is demoted when recent effect is this many
    decay_min_share: float = 0.4      #   SEs below its earlier effect AND P(recent RR > 1+min_lift) ≤ decay_p_still_good
    decay_p_still_good: float = 0.2
    decay_min_hold_h: int = 24        # an arm must have carried its share this long before 'decay' can be claimed
    ramp: tuple = ((0.06, 0.35), (0.045, 0.6), (0.0, 0.85))  # (posterior sd >= x -> max share y): evidence-gated ramp
    loser_max_sd: float = 0.05        # 'loser' needs a tight posterior, not one bad afternoon
    min_realloc_tv: float = 0.10      # don't spend an approval on a reallocation smaller than this
    min_expected_gain: float = 0.003  # ... or one whose expected engagement gain is below 0.3%
    mc_draws: int = 4000              # posterior draws (fixed seed → deterministic)

    # ---------------------------------------------------------------- call time
    schedule_kappa: float = 2.0       # weight = (relative pickup at this hour) ** kappa
    schedule_floor: float = 0.2       # never 0: an hour left out of a table gets weight 0 → starved leads
    schedule_cap: float = 4.0
    schedule_min_dials: int = 400     # per cohort, before its hour profile is trusted
    schedule_min_gain: float = 0.01   # predicted pickup gain needed to propose a schedule
    schedule_refresh_days: float = 2.0

    # ---------------------------------------------------------------- caller-ID health
    did_quarantine_ratio: float = 0.75  # DID pickup below 75% of its peers (upper CI) → quarantine
    did_graduate_ratio: float = 0.85    # canary graduates when its lower CI clears this
    did_canary_share: float = 0.08   # a pre-flagged canary (≈0.55 health) then costs <1.5% DID health
    did_short_window_h: int = 3
    did_long_window_h: int = 24
    did_min_expected_conn: float = 30.0   # long window: enough evidence to call a canary / quarantine
    did_short_ratio: float = 0.60         # short window only catches a collapse (spam flag), not drift
    did_short_min_expected_conn: float = 25.0
    did_max_load_ratio: float = 1.5      # never quarantine a DID that is simply carrying more load than peers
    did_ci_z: float = 1.645

    # ---------------------------------------------------------------- hallucination
    halluc_cusum_ratio: float = 2.5   # CUSUM tuned to detect a 2.5x rise in the flag rate
    halluc_cusum_h: float = 7.0       # alarm threshold (in-control ARL ≈ 10^5 calls; see tools/calibrate.py)
    halluc_verify_calls: int = 300    # connected calls after a fix before we call it verified
    validation_n: int = 300           # 3 credits; enough to separate 1% from 3% fail rates
    validation_topup_n: int = 1000
    safety_max_fail_rate: float = 0.02  # our own bar on validated failure rate (stricter than 97% pass)

    # ---------------------------------------------------------------- governance
    emergency_reserve: int = 1        # approval slots per 24h held back for incidents
    reject_backoff_hours: float = 4.0
    approval_send_margin_min: int = 0    # the platform decides requests that land exactly at expiry
    auto_apply_cooldown_min: int = 60  # between routine operational auto-applies (staged ramp, not a loophole)
    variant_auto_cooldown_min: int = 180  # customer-facing auto-applies: at most every 3h, or we chase noise
    safety_step_cooldown_min: int = 15  # between safety (risk-reducing) auto-apply steps
    plan_every_min: int = 60
    credit_reserve_frac: float = 0.5   # half the credits are only for incidents

    # ---------------------------------------------------------------- loop
    step_minutes: int = 15
    view_every_min: int = 360

    def as_dict(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self)}


@dataclass
class TenantPrefs:
    """Voluntary rules, stricter than the signed policy, from people we talked to."""
    max_routine_requests_per_day: int | None = None
    not_before_hour: int | None = None
    customer_facing_auto_apply: bool = True
    auto_apply_notice_minutes: int = 0
    note: str = ""


def load_tenant_prefs(path: str | Path | None = None) -> dict[str, TenantPrefs]:
    p = Path(path) if path else Path(__file__).resolve().parent.parent / "config" / "tenant_prefs.yaml"
    if not p.exists():
        return {}
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    out = {}
    for tid, d in (raw.get("tenants") or {}).items():
        known = {f.name for f in fields(TenantPrefs)}
        out[tid] = TenantPrefs(**{k: v for k, v in (d or {}).items() if k in known})
    return out
