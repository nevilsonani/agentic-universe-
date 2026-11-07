"""Unit tests for the controller's statistics, governance math and policy rules.

Run: python -m pytest -q tests
"""
import math

import numpy as np
import pytest

from controller.features import Cohort, FeatureSpace, design_cohorts, parse_label, label, ts_to_minute
from controller.governance import Governance, canon_config, exact_repr, platform_norm
from controller.planner import _waterfill, limit_shift, round_dist
from controller.registry import Registry, changed_dims, max_shift, tv
from controller.settings import Settings, TenantPrefs
from controller.stats import (chi2_sf, cusum_bernoulli, fit_arm_effects, heterogeneity, poisson_sf, rr_two_arm,
                              wilson)
from controller.store import Store


# ---------------------------------------------------------------------------------------------- statistics
def _sim_strata(rng, rr, K=30, n_lo=40, n_hi=200):
    rows = []
    for k in range(K):
        base = rng.uniform(0.35, 0.7)
        shares = rng.dirichlet([2, 2, 2])
        n = rng.integers(n_lo, n_hi)
        arms = rng.choice(3, n, p=shares)
        p = np.minimum(base * np.array(rr)[arms], 0.95)
        eng = rng.random(n) < p
        for a in range(3):
            m = arms == a
            rows.append((k, a, m.sum(), eng[m].sum()))
    return np.array(rows)


def test_arm_effects_null_is_calibrated():
    """Under no effect, z-scores are ~N(0,1): the variance (with binomial correction) is honest."""
    rng = np.random.default_rng(1)
    zs = []
    for _ in range(150):
        r = _sim_strata(rng, [1, 1, 1])
        f = fit_arm_effects(r[:, 0], r[:, 1], r[:, 2], r[:, 3], 3, 0)
        zs.append(f["beta"][1] / math.sqrt(f["cov"][1, 1]))
    zs = np.array(zs)
    assert abs(zs.mean()) < 0.25
    assert 0.75 < zs.std() < 1.15
    assert (abs(zs) > 1.96).mean() < 0.10


def test_arm_effects_recover_true_ratio():
    rng = np.random.default_rng(2)
    r = _sim_strata(rng, [1.0, 1.2, 0.9], K=60, n_lo=200, n_hi=400)
    f = fit_arm_effects(r[:, 0], r[:, 1], r[:, 2], r[:, 3], 3, 0)
    for a, true in ((1, 1.2), (2, 0.9)):
        se = math.sqrt(f["cov"][a, a])
        assert abs(f["beta"][a] - math.log(true)) < 3 * se


def test_stratification_prevents_simpsons_paradox():
    """Arm B is given more traffic in the low-base stratum. Pooled rates say B is worse; within strata
    they are identical. The stratified estimate must say RR ≈ 1."""
    rng = np.random.default_rng(3)
    rows = []
    for k, base, share_b in ((0, 0.7, 0.1), (1, 0.3, 0.9)):
        n = 20000
        arm = (rng.random(n) < share_b).astype(int)
        eng = rng.random(n) < base
        for a in (0, 1):
            m = arm == a
            rows.append((k, a, m.sum(), eng[m].sum()))
    r = np.array(rows)
    pooled = (r[r[:, 1] == 1, 3].sum() / r[r[:, 1] == 1, 2].sum()) / (r[r[:, 1] == 0, 3].sum() / r[r[:, 1] == 0, 2].sum())
    assert pooled < 0.7                                   # the naive answer is badly wrong
    f = fit_arm_effects(r[:, 0], r[:, 1], r[:, 2], r[:, 3], 2, 0)
    assert abs(math.exp(f["beta"][1]) - 1) < 0.05        # ours is not


def test_mh_two_arm_single_stratum_matches_closed_form():
    lr, var = rr_two_arm([30], [100], [20], [100])
    assert abs(lr - math.log(1.5)) < 1e-9
    assert var > 0


def test_cusum_vectorised_matches_page_recursion():
    rng = np.random.default_rng(4)
    x = rng.random(5000) < 0.02
    p0, p1 = 0.015, 0.04
    up, dn = math.log(p1 / p0), math.log((1 - p1) / (1 - p0))
    s, smax = 1.3, 0.0
    for v in x:
        s = max(0.0, s + (up if v else dn))
        smax = max(smax, s)
    s_end, s_max, _ = cusum_bernoulli(x, p0, p1, s0=1.3)
    assert abs(s_end - s) < 1e-9 and abs(s_max - smax) < 1e-9


def test_tail_functions():
    assert abs(chi2_sf(3.841, 1) - 0.05) < 1e-3
    assert abs(chi2_sf(5.991, 2) - 0.05) < 1e-3
    assert abs(poisson_sf(5, 2.0) - (1 - sum(math.exp(-2) * 2 ** k / math.factorial(k) for k in range(5)))) < 1e-9
    lo, hi = wilson(50, 100)
    assert lo < 0.5 < hi


def test_heterogeneity_detects_real_spread_only():
    assert heterogeneity(np.array([0.2, -0.15, 0.0, 0.01]), np.full(4, 0.05))["p"] < 1e-3
    assert heterogeneity(np.array([0.01, -0.02, 0.0, 0.015]), np.full(4, 0.05))["p"] > 0.5


# ---------------------------------------------------------------------------------------------- governance math
@pytest.mark.parametrize("target", [
    {"V1": 0.10000000000000002, "V2": 0.35000000000000003, "V3": 0.45000000000000007, "V4": 0.10000000000000002},
    {"W1": 0.15000000000000002, "W2": 0.75, "W3": 0.1},
    {"D1": 0.5, "D2": 0.5}, {"V2": 1.0}])
def test_exact_repr_reproduces_platform_floats(target):
    """The platform re-normalises and compares floats exactly; untouched maps must round-trip bit-for-bit."""
    r = exact_repr(target)
    assert r is not None and platform_norm(r) == target


def test_canon_config_keeps_untouched_dims_exact_and_changed_dims_integral():
    stored = {"allocations": {"default": {"voice": {"V1": 0.10000000000000002, "V2": 0.9000000000000001},
                                          "welcome_message": {"W1": 1.0}}}}
    proposed = {"allocations": {"default": {"voice": dict(stored["allocations"]["default"]["voice"]),
                                            "welcome_message": {"W1": 0.75, "W2": 0.25}}}}
    out = canon_config(proposed, stored)
    assert platform_norm(out["allocations"]["default"]["voice"]) == stored["allocations"]["default"]["voice"]
    assert out["allocations"]["default"]["welcome_message"] == {"W1": 75, "W2": 25}


def test_limit_shift_never_exceeds_max_shift():
    rng = np.random.default_rng(5)
    for _ in range(50):
        def rand_dist(keys):
            w = rng.dirichlet(np.ones(len(keys)))
            return {k: float(x) for k, x in zip(keys, w)}
        base = {"cohorts": [], "allocations": {"default": {"welcome_message": rand_dist(["W1", "W2", "W3"]),
                                                           "voice": rand_dist(["V1", "V2"]),
                                                           "caller_id": rand_dist(["D1", "D2", "D3"])}}}
        new = {"cohorts": [], "allocations": {"default": {"welcome_message": rand_dist(["W1", "W2", "W3"]),
                                                          "voice": rand_dist(["V1", "V2"]),
                                                          "caller_id": rand_dist(["D1", "D2", "D3"])}}}
        out = limit_shift(base, new, 0.2)
        assert max_shift(base, out) <= 0.2 + 1e-9


def test_round_dist_sums_to_one_and_keeps_floor():
    d = round_dist({"W1": 0.07, "W2": 0.611, "W3": 0.319}, 0.05, protect={"W1": 0.10})
    assert abs(sum(d.values()) - 1) < 1e-9 and d["W1"] >= 0.10 - 1e-9


def test_waterfill_respects_floors_and_caps():
    out = _waterfill({"A": 0.9, "B": 0.05, "C": 0.05}, floors={"C": 0.10}, caps={"A": 0.5})
    assert abs(sum(out.values()) - 1) < 1e-9
    assert out["A"] <= 0.5 + 1e-9 and out["C"] >= 0.10 - 1e-9


def test_changed_dims_ignores_float_noise():
    a = {"cohorts": [], "allocations": {"default": {"voice": {"V1": 0.1, "V2": 0.9}, "welcome_message": {"W1": 1.0},
                                                    "caller_id": {"D1": 1.0}}}, "prompt_version": "P1", "schedule": {}}
    b = {"cohorts": [], "allocations": {"default": {"voice": {"V1": 0.10000000000000002, "V2": 0.9000000000000001},
                                                    "welcome_message": {"W1": 1.0}, "caller_id": {"D1": 1.0}}},
         "prompt_version": "P1", "schedule": {}}
    assert changed_dims(a, b) == []
    b["prompt_version"] = "P3"
    assert changed_dims(a, b) == ["prompt_version"]


def test_design_cohorts_is_a_small_disjoint_partition():
    fs = FeatureSpace()
    rng = np.random.default_rng(6)
    counts = {}
    for _ in range(5000):
        t = fs.encode({"lang": rng.choice(["hi", "en", "ta"], p=[.5, .3, .2]), "city_tier": str(rng.integers(1, 4)),
                       "age_band": rng.choice(["18-30", "31-45", "46-60", "60+"], p=[.25, .35, .25, .15]),
                       "value_band": rng.choice(["low", "mid", "high"])})
        counts[t] = counts.get(t, 0) + 1
    cs = design_cohorts(fs, counts, "age_band", "lang")
    assert 1 < len(cs) <= 20
    tups = np.array(list(counts))
    hits = np.zeros(len(tups), int)
    for c in cs:
        hits += c.mask(fs, tups).astype(int)
    assert (hits == 1).all()                                   # every lead in exactly one cohort


def test_time_labels_round_trip():
    assert ts_to_minute("2027-01-06T14:30:00+05:30") == 2 * 1440 + 14 * 60 + 30
    assert parse_label("D2 14:30") == 2 * 1440 + 870 and label(2 * 1440 + 870) == "D2 14:30"
    assert ts_to_minute("2026-12-21T08:00:00+05:30") < 0           # history is before D0


# ---------------------------------------------------------------------------------------------- send-window rules
def _gov(tmp_path, gov_overrides=None, tenant="kestrel_finance", prefs=None):
    store = Store(tmp_path / "s.db")
    reg = Registry(store)
    info = {"workflow_id": "wf", "tenant_id": tenant, "options": {"welcome_message": ["W1"], "voice": ["V1"],
                                                                  "caller_id": ["D1"], "prompt_version": ["P1"]},
            "governance": {"approver_hours": [10, 19], "approver_latency_minutes": [20, 120],
                           "proposal_ttl_hours": 12, "apply_window_hours": 2, "max_requests_per_24h": 4,
                           "auto_apply": {"enabled": False}, **(gov_overrides or {})},
            "objective": {"primary": "engagement_rate", "guardrail": {"pickup_rate_max_drop": 0.02}},
            "dialling_window": [9, 21], "dials_per_hour": 100, "cadence": [2], "mid_flight": "allowed",
            "daily_batch": {"at_hour": 8, "size": 10}}
    with store.tx():
        reg.register(info)
    return Governance(store, None, reg, Settings(), prefs if prefs is not None else
                      {"kestrel_finance": TenantPrefs(max_routine_requests_per_day=2, not_before_hour=11)}), store


def _sent(store, minute, note="routine"):
    with store.tx():
        n = store.q1("SELECT COUNT(*) FROM proposals") + 1
        store.db.execute("INSERT INTO proposals(proposal_id, wf, kind, channel, status, sent_minute, created_minute, "
                         "note) VALUES(?,?,?,?,?,?,?,?)", (f"p{n}", "wf", "change", "approval", "applied", minute,
                                                          minute, note))


def test_routine_requests_respect_approver_hours_and_tenant_preference(tmp_path):
    g, _ = _gov(tmp_path)
    assert not g.send_window("wf", 9 * 60, "routine")[0]          # before approvers start
    assert not g.send_window("wf", 10 * 60 + 30, "routine")[0]    # Anita: never before 11:00
    assert g.send_window("wf", 11 * 60, "routine")[0]
    assert not g.send_window("wf", 18 * 60 + 30, "routine")[0]    # would be decided tomorrow / expire


def test_routine_budget_keeps_an_emergency_slot(tmp_path):
    g, store = _gov(tmp_path)
    _sent(store, 11 * 60)
    _sent(store, 15 * 60)
    assert not g.send_window("wf", 16 * 60 + 30, "routine")[0]     # tenant cap 2/day reached
    assert g.send_window("wf", 16 * 60 + 30, "safety")[0]          # incidents still get through
    _sent(store, 16 * 60, note="safety")
    _sent(store, 16 * 60 + 15, note="safety")
    assert not g.send_window("wf", 16 * 60 + 30, "safety")[0]      # but never past the platform's hard limit


def test_safety_request_not_sent_if_it_would_expire_unseen(tmp_path):
    g, _ = _gov(tmp_path, {"proposal_ttl_hours": 2})
    assert not g.send_window("wf", 22 * 60, "safety")[0]           # 22:00 + TTL 2h expires before 10:00
