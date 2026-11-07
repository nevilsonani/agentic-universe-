"""Planner: turns models into *facets* — self-contained, explainable config changes.

A facet is one reason to change the config (rotate caller IDs, re-time calls for a
cohort set, reallocate welcome/voice traffic, revert a prompt…). Governance then
decides which facets travel together, through which channel, and when.

Exploration/exploitation (SUBMISSION.md §5):
  * Untested arms start at a small share chosen so the prior-predictive chance of
    the change costing >1% engagement is ≤ `explore_risk_quantile`.
  * After that, each (cohort, dim) gets batched Thompson allocation: share ∝
    P(arm is best), with a control holdout, floors for still-uncertain challengers,
    caps for unproven arms, and losers dropped.
  * Every candidate change is scored by posterior draws of the workflow-level
    engagement change; if P(Δ < −1%) is too high the change is shrunk toward the
    current config until it passes, or dropped.
"""
from __future__ import annotations

import copy
import json
import math
import zlib
from dataclasses import dataclass, field

import numpy as np

from .analysis import EngagementModel, PickupTimeModel, did_health, summarize_draws
from .features import Cohort, FeatureSpace, assign, config_cohorts, design_cohorts, label
from .ingest import DAY_OFF
from .registry import WorkflowInfo, dist, norm, tv
from .settings import VARIANT_DIMS, Settings


@dataclass
class Facet:
    name: str
    dims: set
    urgency: str                     # routine | protective | safety
    summary: str
    rationale: str
    data: dict
    evidence: dict = field(default_factory=dict)
    predicted: dict = field(default_factory=dict)

    def to_json(self) -> dict:
        return {"name": self.name, "dims": sorted(self.dims), "urgency": self.urgency, "summary": self.summary,
                "rationale": self.rationale, "data": self.data, "evidence": self.evidence,
                "predicted": self.predicted}


# ---------------------------------------------------------------------- config composition
def compose(base: dict, facets: list[Facet]) -> dict:
    cfg = copy.deepcopy(base)
    cfg.setdefault("cohorts", [])
    cfg.setdefault("schedule", {"hour_weights": {}, "lead_preferred_hour": {}})
    cfg["schedule"].setdefault("hour_weights", {})
    cfg["schedule"].setdefault("lead_preferred_hour", {})
    for f in sorted(facets, key=lambda f: 0 if f.name == "cohorts" else 1):
        if f.name == "cohorts":
            cfg["cohorts"] = copy.deepcopy(f.data["cohorts"])
            ids = {c["id"] for c in cfg["cohorts"]}
            cfg["allocations"] = {k: v for k, v in cfg["allocations"].items() if k == "default" or k in ids}
            cfg["schedule"]["hour_weights"] = {k: v for k, v in cfg["schedule"]["hour_weights"].items()
                                               if k == "default" or k in ids}
        elif f.name == "variants":
            ids = {c["id"] for c in cfg["cohorts"]} | {"default"}
            for cid, dims in f.data["alloc"].items():
                if cid not in ids:
                    continue
                for d, dd in dims.items():
                    cfg["allocations"].setdefault(cid, {})[d] = dict(dd)
        elif f.name == "did":
            cfg["allocations"]["default"]["caller_id"] = dict(f.data["caller_id"])
            bad = set(f.data.get("remove", []))
            for cid, dims in cfg["allocations"].items():
                if cid != "default" and "caller_id" in dims and bad & set(dims["caller_id"]):
                    keep = {k: v for k, v in dims["caller_id"].items() if k not in bad}
                    if keep:
                        dims["caller_id"] = keep
                    else:
                        del dims["caller_id"]
        elif f.name == "schedule":
            ids = {c["id"] for c in cfg["cohorts"]} | {"default"}
            cfg["schedule"]["hour_weights"] = {k: dict(v) for k, v in f.data["hour_weights"].items() if k in ids}
        elif f.name == "prompt":
            cfg["prompt_version"] = f.data["prompt_version"]
    # drop empty cohort allocation blocks (purely cosmetic, keeps diffs readable)
    cfg["allocations"] = {k: v for k, v in cfg["allocations"].items() if k == "default" or v}
    return cfg


def limit_shift(base: dict, new: dict, max_shift: float) -> dict:
    """Move each (cohort, dim) distribution toward `new` by at most `max_shift` TV (staged ramp)."""
    out = copy.deepcopy(new)
    keys = {"default"} | {c["id"] for c in new.get("cohorts") or []}
    cap = max_shift * 0.995
    for k in keys:
        for d in ("welcome_message", "voice", "caller_id"):
            a, b = norm(dist(base, k, d)), norm(dist(new, k, d))
            t = tv(a, b)
            if t <= cap or t == 0:
                continue
            lam = cap / t
            mix = {o: (1 - lam) * a.get(o, 0) + lam * b.get(o, 0) for o in set(a) | set(b)}
            mix = {o: round(v, 6) for o, v in mix.items() if v > 1e-6}
            out["allocations"].setdefault(k, {})[d] = mix
    return out


def round_dist(d: dict[str, float], step: float, protect: dict[str, float] | None = None) -> dict[str, float]:
    """Largest-remainder rounding to a grid; arms with a floor keep it."""
    d = norm(d)
    if not d:
        return d
    units = int(round(1 / step))
    raw = {k: v * units for k, v in d.items()}
    base = {k: int(math.floor(v)) for k, v in raw.items()}
    for k, mn in (protect or {}).items():
        if k in base:
            base[k] = max(base[k], int(round(mn * units)))
    left = units - sum(base.values())
    order = sorted(raw, key=lambda k: raw[k] - math.floor(raw[k]), reverse=True)
    i = 0
    while left > 0 and order:
        base[order[i % len(order)]] += 1
        left -= 1
        i += 1
    while left < 0:
        k = max(base, key=lambda k: base[k] - (protect or {}).get(k, 0) * units)
        base[k] -= 1
        left += 1
    return {k: round(v / units, 4) for k, v in base.items() if v > 0}


def _waterfill(weights: dict[str, float], floors: dict[str, float], caps: dict[str, float]) -> dict[str, float]:
    """Shares proportional to `weights`, subject to per-arm floors and caps. Mass that nobody
    may take (everyone capped) goes to the arm with the highest floor (the control)."""
    arms = list(weights)
    fixed: dict[str, float] = {}
    prop: dict[str, float] = {}
    for _ in range(len(arms) + 2):
        free = [a for a in arms if a not in fixed]
        mass = 1.0 - sum(fixed.values())
        if not free:
            break
        sw = sum(max(weights[a], 1e-6) for a in free)
        prop = {a: mass * max(weights[a], 1e-6) / sw for a in free}
        moved = False
        for a in free:
            lo, hi = floors.get(a, 0.0), caps.get(a, 1.0)
            if prop[a] < lo - 1e-12:
                fixed[a], moved = lo, True
            elif prop[a] > hi + 1e-12:
                fixed[a], moved = hi, True
        if not moved:
            break
    out = {**fixed, **{a: v for a, v in prop.items() if a not in fixed}}
    left = 1.0 - sum(out.values())
    if left > 1e-9:
        sink = max(floors, key=floors.get) if floors else arms[0]
        out[sink] = out.get(sink, 0) + left
    return {a: max(v, 0.0) for a, v in out.items()}


# ---------------------------------------------------------------------- planner
class Planner:
    def __init__(self, s: Settings, fs: FeatureSpace, ingestor, registry):
        self.s, self.fs, self.ing, self.reg = s, fs, ingestor, registry

    # ------------------------------------------------------------------ population helpers
    def population(self, wf: WorkflowInfo, now: int, days: int = 3) -> tuple[np.ndarray, np.ndarray]:
        """Feature tuples of recently uploaded leads, with counts (the population a change will touch)."""
        L = self.ing.li(wf.id)
        if L.n == 0:
            return np.zeros(0, np.int64), np.zeros(0)
        m = L.minute[: L.n] >= now - days * 1440
        if m.sum() < 50:
            m = np.ones(L.n, bool)
        t, c = np.unique(L.tup[: L.n][m], return_counts=True)
        return t.astype(np.int64), c.astype(float)

    def active_leads(self, wf: WorkflowInfo, now: int) -> np.ndarray:
        """Indices of leads still in their cadence window and not concluded."""
        L = self.ing.li(wf.id)
        if L.n == 0:
            return np.zeros(0, np.int64)
        cd = self.ing.campaign_day.get(wf.id, {})
        days = np.array([cd.get(int(c), -999) for c in range(int(L.campaign[: L.n].max()) + 1)])
        camp_day = days[np.maximum(L.campaign[: L.n], 0)]
        live = (now // 1440 - camp_day < len(wf.cadence)) & ~L.concluded[: L.n]
        return np.flatnonzero(live)

    def base_engagement(self, wf: WorkflowInfo, tups: np.ndarray) -> np.ndarray:
        e = self.ing.t(wf.id)["eng"]
        if e.n == 0:
            return np.full(len(tups), 0.5)
        T = e.col("tup")
        conn = np.bincount(T, weights=e.col("conn"), minlength=1 << 16)
        eng = np.bincount(T, weights=e.col("eng"), minlength=1 << 16)
        pbar = eng.sum() / max(conn.sum(), 1)
        k = 50.0
        return (eng[tups] + k * pbar) / (conn[tups] + k)

    # ------------------------------------------------------------------ cohorts (C2)
    def design(self, wf: WorkflowInfo, now: int, pickup: PickupTimeModel) -> dict:
        tups, cnt = self.population(wf, now, days=7)
        tup_counts = dict(zip(tups.tolist(), cnt.tolist()))
        hour_feats = sorted(pickup.tests.items(), key=lambda kv: kv[1]["p"]) if pickup.ok else []
        primary = next((f for f, t in hour_feats if t["used"]), None) or "age_band"
        secondary = "lang" if primary != "lang" and len(self.fs.values("lang")) > 1 else \
            next((f for f, t in hour_feats if f != primary), None)
        cohorts = design_cohorts(self.fs, tup_counts, primary, secondary)
        return {"primary": primary, "secondary": secondary, "cohorts": [c.to_config() for c in cohorts],
                "why": {"primary": f"{primary} changes the pickup-by-hour profile "
                                   f"(χ² p={pickup.tests.get(primary, {}).get('p', 1):.1e}); it drives call timing",
                        "secondary": f"{secondary}: strongest prior for welcome/voice fit (language of the opener)",
                        "min_share": 0.03, "max_cohorts": 20}}

    # ------------------------------------------------------------------ schedule (C4)
    def schedule_facet(self, wf: WorkflowInfo, now: int, cohorts: list[Cohort], base_cfg: dict,
                       pickup: PickupTimeModel) -> Facet | None:
        if not pickup.ok or not pickup.features_used() or not cohorts:
            return None
        tups, cnt = self.population(wf, now, days=7)
        hours = pickup.hours
        weights, profiles, gain_num, gain_den = {}, {}, 0.0, 0.0
        base_prof = pickup.profile(tups, cnt)
        for c in cohorts:
            m = c.mask(self.fs, tups)
            if not m.any():
                continue
            prof = pickup.profile(tups[m], cnt[m]) / base_prof          # comparative advantage by hour
            prof = prof / prof.mean()
            w = np.clip(prof ** self.s.schedule_kappa, self.s.schedule_floor, self.s.schedule_cap)
            weights[c.id] = {str(h): round(float(x), 3) for h, x in zip(hours, w)}
            profiles[c.id] = [round(float(x), 3) for x in prof]
        if not weights:
            return None
        # dry run: re-weight each hour's historical dial mix by the new weights, same capacity per hour
        gain = self._schedule_gain(wf, pickup, cohorts, weights, tups, cnt)
        old_hw = (base_cfg.get("schedule") or {}).get("hour_weights") or {}
        if old_hw == weights or gain["rel_gain"] < self.s.schedule_min_gain:
            return None
        used = ", ".join(pickup.features_used())
        return Facet("schedule", {"schedule"}, "routine",
                     f"time calls by {used}: each cohort called more in its best hours "
                     f"(predicted pickup {gain['rel_gain']:+.1%})",
                     f"Over {gain['dials']:,.0f} historical dials, pickup by hour differs by {used} "
                     f"(χ² p={min(pickup.tests[f]['p'] for f in pickup.features_used()):.1e}); other features do not. "
                     f"Weights = (cohort's relative pickup at that hour)^{self.s.schedule_kappa}, clipped to "
                     f"[{self.s.schedule_floor}, {self.s.schedule_cap}] so no hour is starved. Capacity and windows "
                     f"are unchanged — this only changes who is dialled first in each hour.",
                     {"hour_weights": weights},
                     evidence={"feature_tests": pickup.tests, "relative_pickup_by_hour": profiles, "hours": hours},
                     predicted={"pickup_rel_gain": gain["rel_gain"], "dry_run": gain})

    def _schedule_gain(self, wf, pickup, cohorts, weights, tups, cnt) -> dict:
        pt = self.ing.t(wf.id)["ptime"]
        day = pt.col("day") - DAY_OFF
        hour = pt.col("hour")
        recent = day >= day.max() - 7 if pt.n else np.zeros(0, bool)
        H = pickup.hours
        coh_names = assign({"cohorts": [c.to_config() for c in cohorts]}, self.fs, pt.col("tup")[recent])
        hrs = hour[recent]
        dials = pt.col("dials")[recent].astype(float)
        old, new = 0.0, 0.0
        tot = 0.0
        for h in H:
            mh = hrs == h
            if not mh.any():
                continue
            x_old = {}
            for c in cohorts:
                sel = mh & (coh_names == c.id)
                x_old[c.id] = dials[sel].sum()
            cap = sum(x_old.values())
            if cap == 0:
                continue
            ws = {cid: x * weights.get(cid, {}).get(str(h), 1.0) for cid, x in x_old.items()}
            sw = sum(ws.values()) or 1.0
            for c in cohorts:
                m = c.mask(self.fs, tups)
                if not m.any():
                    continue
                p = pickup.profile(tups[m], cnt[m])[H.index(h)] * pickup.p_base
                old += x_old[c.id] * p
                new += cap * ws[c.id] / sw * p
            tot += cap
        return {"rel_gain": (new / old - 1) if old else 0.0, "dials": float(tot),
                "method": "reweight last-7-day dial mix per hour by new weights; capacity per hour fixed; pickup "
                          "from the attempt-adjusted hour model",
                "assumption": "eligible pool in each hour looks like the historical dialled mix"}

    # ------------------------------------------------------------------ caller ID (C5)
    def did_facet(self, wf: WorkflowInfo, st: dict, now: int, cur_cfg: dict) -> Facet | None:
        dids = wf.options["caller_id"]
        ds = st["did"]
        healthy = [d for d in dids if ds[d]["state"] == "healthy"]
        canary = [d for d in dids if ds[d]["state"] in ("canary", "untested")]
        bad = [d for d in dids if ds[d]["state"] == "quarantined"]
        if not healthy and not canary:
            return None
        if len(healthy) <= 1:
            # concentration risk: one number carrying the whole workflow is the bigger danger
            pool = healthy + canary
            target = {d: 1.0 / len(pool) for d in pool}
            mode = "spread"
        else:
            c_share = min(self.s.did_canary_share, 0.4 / max(len(canary), 1)) if canary else 0.0
            target = {d: c_share for d in canary}
            for d in healthy:
                target[d] = (1 - c_share * len(canary)) / len(healthy)
            mode = "canary" if canary else "even"
        target = {k: round(v, 4) for k, v in target.items() if v > 0}
        cur = norm(dist(cur_cfg, "default", "caller_id"))
        cohort_bad = any(bad and set(bad) & set((d.get("caller_id") or {})) for k, d in cur_cfg["allocations"].items()
                         if k != "default")
        if tv(cur, target) < 0.02 and not cohort_bad:
            return None
        urgency = "protective" if any(cur.get(b, 0) > 0 for b in bad) or cohort_bad else "routine"
        if st.get("did_incident_open"):
            urgency = "safety"
        parts = []
        if bad:
            parts.append(f"stop using {', '.join(bad)} (pickup well below peers)")
        if canary:
            parts.append(f"test {', '.join(canary)} at {target.get(canary[0], 0):.0%}")
        parts.append(f"spread volume over {', '.join(sorted(set(healthy) | (set(canary) if mode == 'spread' else set())))}")
        ev = {d: ds[d] for d in dids}
        return Facet("did", {"caller_id"}, urgency, "caller ID: " + "; ".join(parts),
                     "Caller-ID reputation falls with call velocity, so volume is spread evenly over numbers in good "
                     "standing; untested numbers start as small canaries and graduate when their pickup is within "
                     f"{self.s.did_graduate_ratio:.0%} of peers; a number whose pickup is below "
                     f"{self.s.did_quarantine_ratio:.0%} of peers (upper 90% bound, same hour/cohort strata) is pulled.",
                     {"caller_id": target, "remove": bad}, evidence={"did_state": ev, "mode": mode})

    def did_update(self, wf: WorkflowInfo, st: dict, now: int, short: bool) -> list[dict]:
        """Update DID states from data. Returns a list of transitions for the audit log."""
        pd = self.ing.t(wf.id)["pdid"]
        dids = wf.options["caller_id"]
        ds = st["did"]
        quarantined = {i for i, d in enumerate(dids) if ds[d]["state"] == "quarantined"}
        win = self.s.did_short_window_h if short else self.s.did_long_window_h
        h = did_health(pd, len(dids), now, win, exclude=quarantined, z=self.s.did_ci_z)
        out = []
        for i, d in enumerate(dids):
            r = h.get(i)
            if not r:
                continue
            ds[d]["last"] = {k: round(v, 4) for k, v in r.items()}
            enough = r["expected_conn"] >= self.s.did_min_expected_conn
            state = ds[d]["state"]
            if enough and r["hi"] < self.s.did_quarantine_ratio and state != "quarantined":
                ds[d].update(state="quarantined", since=now, reason=f"pickup {r['ratio']:.2f}× peers "
                             f"(90% CI {r['lo']:.2f}–{r['hi']:.2f}, {r['dials']:.0f} dials, last {win}h)")
                out.append({"did": d, "from": state, "to": "quarantined", "window_h": win, **r})
            elif not short and state in ("canary", "untested") and enough and r["lo"] >= self.s.did_graduate_ratio:
                ds[d].update(state="healthy", since=now, reason=f"canary pickup {r['ratio']:.2f}× peers "
                             f"(lower bound {r['lo']:.2f} ≥ {self.s.did_graduate_ratio})")
                out.append({"did": d, "from": state, "to": "healthy", "window_h": win, **r})
            elif state == "untested" and r["dials"] > 0:
                ds[d]["state"] = "canary"
        return out

    # ------------------------------------------------------------------ variants (C6/C7)
    def variant_facet(self, wf: WorkflowInfo, st: dict, now: int, base_cfg: dict, cohorts: list[Cohort],
                      frozen: set[str], bootstrap: bool = False) -> tuple[Facet | None, dict]:
        """Returns (facet or None, analysis snapshot for the audit log / view)."""
        s = self.s
        now_day = now // 1440
        ref = st["ref"]
        eng = self.ing.t(wf.id)["eng"]
        model = EngagementModel(s, self.fs, eng, wf.options, ref)
        fit = model.fit(now_day, half_life=None)
        recent = model.fit(now_day, half_life=s.recent_half_life_days, segments=False)
        tups, cnt = self.population(wf, now)
        if len(tups) == 0:
            return None, {}
        rng = np.random.default_rng(zlib.crc32(f"{wf.id}|{now}".encode()))  # stable across restarts
        S = s.mc_draws
        conn_w = cnt.copy()  # population weights: recently uploaded leads per feature tuple
        cohorts_all = cohorts + [Cohort("default", {})]
        names = assign({"cohorts": [c.to_config() for c in cohorts]}, self.fs, tups)
        snap = {"dims": {}, "cohorts": {}}
        tup_draws, tup_draws_recent = {}, {}
        for d in VARIANT_DIMS:
            f = fit[d]
            snap["dims"][d] = {"arms": f.arms, "ref": f.arms[f.ref],
                               "n": [round(x, 1) for x in f.n.tolist()], "y": [round(x, 1) for x in f.y.tolist()],
                               "global_rr": [round(math.exp(x), 4) for x in f.post_mean.tolist()],
                               "global_sd": [round(math.sqrt(x), 4) for x in f.post_var.tolist()],
                               "heterogeneity": {feat: {a: {"p": round(v["p"], 5), "sig": v["sig"]}
                                                        for a, v in hv.items()} for feat, hv in f.het.items()}}
            tup_draws[d] = self._tup_draws(f, tups, S, rng)
            tup_draws_recent[d] = self._tup_draws(recent[d], tups, S, rng)
        targets: dict[str, dict[str, dict]] = {}
        decisions = []
        for c in cohorts_all:
            sel = names == c.id
            if not sel.any():
                continue
            w = conn_w[sel] / conn_w[sel].sum()
            snap["cohorts"][c.id] = {"label": c.label, "share": round(float(conn_w[sel].sum() / conn_w.sum()), 4)}
            for d in VARIANT_DIMS:
                f = fit[d]
                cur = norm(dist(base_cfg, c.id if c.id in {x["id"] for x in base_cfg.get("cohorts") or []}
                                else "default", d))
                rr = np.einsum("sta,t->sa", tup_draws[d][:, sel, :], w)
                rr_recent = np.einsum("sta,t->sa", tup_draws_recent[d][:, sel, :], w)
                summ = summarize_draws(rr, f.arms, f.ref)
                summ_r = summarize_draws(rr_recent, f.arms, f.ref)
                cstate = st.setdefault("arms", {}).setdefault(c.id, {}).setdefault(d, {"winner": None})
                tgt, why = self._allocate(f, summ, summ_r, cur, cstate, c.id, d, now, decisions)
                snap["cohorts"][c.id][d] = {"current": cur, "target": tgt, "why": why,
                                            "arms": {a: {k: round(v, 4) for k, v in x.items()} for a, x in summ.items()},
                                            "recent": {a: round(x["rr"], 4) for a, x in summ_r.items()},
                                            "winner": cstate.get("winner")}
                if d in frozen:
                    continue
                targets.setdefault(c.id, {})[d] = tgt
        # ---- workflow-level risk check: P(engagement change < -1%) must be small
        new_cfg = self._apply_targets(base_cfg, cohorts, targets)
        delta, p_harm, mean = self.engagement_delta(wf, base_cfg, new_cfg, cohorts, tups, conn_w, tup_draws)
        explores_new = any(fit[d].n[fit[d].arms.index(a)] == 0
                           for cid, dd in targets.items() for d, dist_ in dd.items() for a in dist_ if a)
        limit = s.explore_risk_quantile if explores_new else s.max_harm_prob
        lam_used = 1.0
        if p_harm > limit or (not explores_new and mean < s.min_expected_gain and not bootstrap
                              and not any(x["kind"] in ("loser", "decay") for x in decisions)):
            ok = False
            for lam in (0.75, 0.5, 0.33, 0.2):
                cand = self._interp_targets(base_cfg, cohorts, targets, lam)
                new2 = self._apply_targets(base_cfg, cohorts, cand)
                dl2, ph2, m2 = self.engagement_delta(wf, base_cfg, new2, cohorts, tups, conn_w, tup_draws)
                if ph2 <= limit and (explores_new or m2 >= s.min_expected_gain * lam):
                    targets, new_cfg, delta, p_harm, mean, lam_used, ok = cand, new2, dl2, ph2, m2, lam, True
                    break
            if not ok:
                snap["skipped"] = {"reason": "risk/value check failed", "p_harm": p_harm, "mean": mean,
                                   "limit": limit}
                return None, snap
        # ---- is it worth a change?
        max_tv = 0.0
        for cid, dd in targets.items():
            src = cid if cid in {x["id"] for x in base_cfg.get("cohorts") or []} else "default"
            for d, t in dd.items():
                max_tv = max(max_tv, tv(dist(base_cfg, src, d), t))
        structural = any(x["kind"] in ("loser", "decay", "winner") for x in decisions)
        if max_tv < 1e-9 or (max_tv < s.min_realloc_tv and not structural and not bootstrap):
            snap["skipped"] = {"reason": f"largest reallocation {max_tv:.2f} < {s.min_realloc_tv}", "mean": mean}
            return None, snap
        urgency = "protective" if any(x["kind"] in ("decay", "loser") for x in decisions) and mean > 0 else "routine"
        n_explore = sum(1 for x in decisions if x["kind"] == "explore")
        bits = []
        for kind, word in (("winner", "promote"), ("decay", "roll back"), ("loser", "drop")):
            xs = [x for x in decisions if x["kind"] == kind]
            if xs:
                bits.append(f"{word} " + ", ".join(sorted({f'{x["arm"]}' for x in xs})) +
                            f" ({len(xs)} cohort·dim)")
        if explores_new:
            bits.append("start testing untested welcome/voice arms on a small share")
        if not bits:
            bits.append("shift traffic toward better-performing welcome/voice arms")
        summary = "; ".join(bits) + f" — expected engagement {mean:+.1%}"
        rationale = (f"Batched Thompson allocation per cohort from {int(sum(fit[d].n.sum() for d in VARIANT_DIMS) / 2):,} "
                     f"connected calls (arms compared only within the same day/config/cohort/prompt strata). "
                     f"Posterior P(engagement change < -1%) = {p_harm:.0%} (limit {limit:.0%}); "
                     f"control arm kept at ≥{s.holdout_share:.0%} in every cohort as a live holdout."
                     + (f" Change scaled to {lam_used:.0%} of the Thompson target to respect the risk limit."
                        if lam_used < 1 else ""))
        facet = Facet("variants", {"welcome_message", "voice"} - frozen, urgency, summary, rationale,
                      {"alloc": targets},
                      evidence={"decisions": decisions, "analysis_minute": now},
                      predicted={"eng_rel_mean": mean, "eng_p_harm": p_harm,
                                 "eng_q05": float(np.quantile(delta, 0.05)), "eng_q95": float(np.quantile(delta, 0.95))})
        return facet, snap

    def _tup_draws(self, f, tups, S, rng) -> np.ndarray:
        """Posterior draws of each tuple's RR per arm: (S, T, A)."""
        A = len(f.arms)
        g = f.post_mean[None, :] + rng.standard_normal((S, A)) * np.sqrt(np.maximum(f.post_var, 0))[None, :]
        g[:, f.ref] = 0
        theta = np.repeat(g[:, None, :], len(tups), axis=1)
        for feat, devs in f.dev.items():
            fc = self.fs.col(tups, feat)
            for v, (dm, dv) in devs.items():
                if not dm.any():
                    continue
                sel = fc == self.fs.codes[feat][v]
                if not sel.any():
                    continue
                dd = dm[None, :] + rng.standard_normal((S, A)) * np.sqrt(dv)[None, :]
                dd[:, f.ref] = 0
                theta[:, sel, :] += dd[:, None, :]
        return np.exp(theta)

    def _allocate(self, f, summ, summ_r, cur, cstate, cid, d, now, decisions) -> tuple[dict, str]:
        s = self.s
        arms = f.arms
        ref = arms[f.ref]
        never_run = [a for i, a in enumerate(arms) if f.n[i] == 0]
        # ---- decay: the current winner's recent (1-day half-life) effect has turned
        win = cstate.get("winner")
        use = summ
        if win and win.get("arm") in summ_r:
            a = win["arm"]
            r = summ_r[a]
            drop_z = (math.log(max(win["rr"], 1e-6)) - math.log(max(r["rr"], 1e-6))) / max(
                math.hypot(win.get("sd_log", 0.05), r["sd_log"]), 1e-6)
            if r["p_lt_ref"] >= s.decay_p or drop_z >= s.decay_drop_z:
                decisions.append({"kind": "decay", "cohort": cid, "dim": d, "arm": a, "promoted_rr": win["rr"],
                                  "recent_rr": r["rr"], "p_recent_below_control": r["p_lt_ref"],
                                  "drop_z": round(drop_z, 2)})
                cstate["winner"] = None
                cstate.setdefault("decayed", []).append({"arm": a, "minute": now, "recent_rr": r["rr"]})
                use = summ_r
        decayed = {x["arm"] for x in cstate.get("decayed", [])}
        if decayed:
            use = summ_r   # once novelty decay was seen here, allocate on the recent (discounted) evidence
        # ---- first exposure: small exploration shares sized by the risk budget
        if len(never_run) == len(arms) - 1:
            k = len(never_run)
            s0 = self._explore_share(k_total=k)
            tgt = {a: s0 for a in never_run}
            rest = 1 - s0 * k
            for a, w in cur.items():
                tgt[a] = tgt.get(a, 0) + rest * w
            decisions.append({"kind": "explore", "cohort": cid, "dim": d, "arms": never_run, "share": s0})
            return round_dist(tgt, 0.01), f"explore untested arms at {s0:.0%} each"
        # ---- Thompson target with guard rails
        pb = {a: use[a]["p_best"] for a in arms}
        losers = {a for a in arms if a != ref and use[a]["p_lt_ref"] >= s.loser_p and use[a]["rr"] <= s.loser_max_rr}
        for a in losers:
            if cur.get(a, 0) > 0:
                decisions.append({"kind": "loser", "cohort": cid, "dim": d, "arm": a, "rr": use[a]["rr"],
                                  "p_worse": use[a]["p_lt_ref"]})
        floors, caps = {ref: s.holdout_share}, {}
        for a in arms:
            if a in losers:
                caps[a] = 0.0
                continue
            if a != ref and use[a]["sd_log"] > s.winner_max_sd and a not in decayed:
                floors[a] = max(floors.get(a, 0), s.floor_share)
            if a != ref and use[a]["p_safe"] < s.proven_prob:
                caps[a] = s.unproven_cap
            if a in decayed:
                caps[a] = min(caps.get(a, 1.0), s.floor_share)
        tgt = _waterfill(pb, floors, caps)
        # ---- winner declaration (deterministic rule, audited)
        best = max(arms, key=lambda a: use[a]["p_best"])
        b = use[best]
        if best != ref and best not in decayed and b["p_best"] >= s.winner_p_best and b["p_gt_ref"] >= s.winner_p_ref \
                and b["rr"] >= 1 + s.winner_min_lift and b["sd_log"] <= s.winner_max_sd:
            if not cstate.get("winner") or cstate["winner"]["arm"] != best:
                cstate["winner"] = {"arm": best, "minute": now, "rr": b["rr"], "lo": b["lo"], "hi": b["hi"],
                                    "sd_log": b["sd_log"], "p_best": b["p_best"]}
                decisions.append({"kind": "winner", "cohort": cid, "dim": d, "arm": best, "rr": b["rr"],
                                  "lo": b["lo"], "hi": b["hi"], "p_best": b["p_best"]})
        tgt = round_dist(tgt, s.share_step, protect={ref: s.holdout_share})
        why = ", ".join(f"{a}:{tgt.get(a, 0):.0%} (P(best) {pb[a]:.2f}, RR {use[a]['rr']:.3f})" for a in arms)
        return tgt, why

    def _explore_share(self, k_total: int) -> float:
        """Largest per-arm share whose prior-predictive P(Δ < −1%) stays within the exploration budget,
        assuming both dims explore at once (5 arms in a typical workflow)."""
        s = self.s
        from .stats import norm_ppf
        k = max(k_total * 2, 1)       # welcome and voice explored together → roughly double the arms
        z = -norm_ppf(s.explore_risk_quantile)
        share = -s.harm_threshold / (z * s.prior_sd * math.sqrt(k)) if z > 0 else s.explore_share_max
        return float(np.clip(round(share, 2), s.explore_share_min, s.explore_share_max))

    def _apply_targets(self, base_cfg, cohorts, targets) -> dict:
        f = Facet("variants", set(VARIANT_DIMS), "routine", "", "", {"alloc": targets})
        cfg = copy.deepcopy(base_cfg)
        if cohorts and {c.id for c in cohorts} != {x["id"] for x in cfg.get("cohorts") or []}:
            cfg = compose(cfg, [Facet("cohorts", {"cohorts"}, "routine", "", "",
                                      {"cohorts": [c.to_config() for c in cohorts]})])
        return compose(cfg, [f])

    def _interp_targets(self, base_cfg, cohorts, targets, lam) -> dict:
        ids = {x["id"] for x in base_cfg.get("cohorts") or []}
        out = {}
        for cid, dd in targets.items():
            for d, t in dd.items():
                cur = norm(dist(base_cfg, cid if cid in ids else "default", d))
                mix = {o: (1 - lam) * cur.get(o, 0) + lam * t.get(o, 0) for o in set(cur) | set(t)}
                out.setdefault(cid, {})[d] = round_dist(mix, 0.01)
        return out

    def engagement_delta(self, wf, old_cfg, new_cfg, cohorts, tups, w, tup_draws) -> tuple[np.ndarray, float, float]:
        """Posterior draws of the relative change in expected engagement rate, old → new config."""
        b = self.base_engagement(wf, tups)
        ww = w * b
        res = {}
        for tag, cfg in (("old", old_cfg), ("new", new_cfg)):
            names = assign(cfg, self.fs, tups)
            M = np.ones((tup_draws[VARIANT_DIMS[0]].shape[0], len(tups)))
            for d in VARIANT_DIMS:
                arms = wf.options[d]
                P = np.zeros((len(tups), len(arms)))
                for ci in np.unique(names):
                    sel = names == ci
                    dd = norm(dist(cfg, ci, d))
                    for o, p in dd.items():
                        if o in arms:
                            P[sel, arms.index(o)] = p
                M *= np.einsum("sta,ta->st", tup_draws[d], P)
            res[tag] = M @ ww
        delta = res["new"] / res["old"] - 1
        return delta, float((delta < self.s.harm_threshold).mean()), float(delta.mean())
