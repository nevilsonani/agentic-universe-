"""Honest numbers for proposals (C10): diff, affected leads, dry run, impact score.

Dry run = off-policy replay of the proposed allocation over the last 7 days of
*logged* calls (self-normalised inverse-propensity weighting). Because welcome and
voice are randomised per call with known probabilities, this is an unbiased
estimate of what the proposed config would have done — but only where it has
support. The share of proposed traffic on (person-type, arm) combinations we have
never observed is reported as `unsupported_share`; we never paper over it with a
model ("only what actually happened counts as evidence" — Product).

Impact score (Product ranks the queue by it, so it has one meaning):
  * safety fixes: 0.8 + 0.2 × severity
  * everything else: 10 × expected relative lift in the workflow's PRIMARY KPI,
    clipped to [0, 0.79]. 0.3 ≈ +3% expected. Guardrail effects are reported in
    the dry run, never folded into the score (pickup and engagement stay separate).
"""
from __future__ import annotations

import math

import numpy as np

from .features import assign
from .ingest import DAY_OFF
from .registry import changed_dims, dist, norm
from .settings import VARIANT_DIMS


def build_diff(old: dict, new: dict) -> dict:
    d: dict = {"changed_dims": changed_dims(old, new)}
    oc = {c["id"]: c for c in old.get("cohorts") or []}
    nc = {c["id"]: c for c in new.get("cohorts") or []}
    if oc != nc:
        d["cohorts"] = {"added": [nc[k] for k in nc if k not in oc], "removed": [k for k in oc if k not in nc]}
    keys = sorted(set(old["allocations"]) | set(new["allocations"]) | {"default"} | set(nc))
    al = {}
    for k in keys:
        for dim in ("welcome_message", "voice", "caller_id"):
            a = norm(dist(old, k if k in old["allocations"] or k == "default" else "default", dim))
            b = norm(dist(new, k if k in new["allocations"] or k == "default" else "default", dim))
            if {o: round(v, 3) for o, v in a.items()} != {o: round(v, 3) for o, v in b.items()}:
                al.setdefault(k, {})[dim] = {"from": {o: round(v, 3) for o, v in a.items()},
                                              "to": {o: round(v, 3) for o, v in b.items()}}
    if al:
        d["allocations"] = al
    if old.get("prompt_version") != new.get("prompt_version"):
        d["prompt_version"] = {"from": old.get("prompt_version"), "to": new.get("prompt_version")}
    ohw = (old.get("schedule") or {}).get("hour_weights") or {}
    nhw = (new.get("schedule") or {}).get("hour_weights") or {}
    if ohw != nhw:
        d["schedule"] = {"hour_weights_changed_for": sorted(k for k in set(ohw) | set(nhw) if ohw.get(k) != nhw.get(k)),
                         "to": nhw}
    return d


def affected_leads(ctx, wf, old: dict, new: dict, now: int) -> tuple[int, dict]:
    """Active leads whose experience changes; for campaign-locked workflows, the next batch instead."""
    w = ctx.reg.wfs[wf]
    dims = changed_dims(old, new)
    act = ctx.planner.active_leads(w, now)
    L = ctx.ing.li(wf)
    tups = L.tup[act] if len(act) else np.zeros(0, np.int64)
    immediate = {"prompt_version", "schedule"} & set(dims)
    locked = w.mid_flight == "next_campaign_only"
    alloc_dims = [d for d in dims if d in ("welcome_message", "voice", "caller_id", "cohorts")]
    if immediate:
        n_now = len(act)
    elif alloc_dims and not locked:
        on, nn = assign(old, ctx.fs, tups), assign(new, ctx.fs, tups)
        ch = np.zeros(len(tups), bool)
        for d in ("welcome_message", "voice", "caller_id"):
            for a in np.unique(on):
                for b in np.unique(nn[on == a]):
                    if norm(dist(old, a, d)) != norm(dist(new, b, d)):
                        ch |= (on == a) & (nn == b)
        n_now = int(ch.sum())
    else:
        n_now = 0
    n_next = w.batch_size if alloc_dims and locked else 0
    return int(n_now + n_next), {"active_leads_now": int(n_now), "next_campaign_leads": int(n_next),
                                 "campaign_locked": locked, "active_total": int(len(act))}


def ips_engagement(ctx, wf, new: dict, cur: dict, now: int, days: int = 7) -> dict:
    """SNIPS replay of engagement for `new` and `cur` over logged connected calls."""
    w = ctx.reg.wfs[wf]
    e = ctx.ing.t(wf)["eng"]
    if e.n == 0:
        return {"note": "no logged calls yet"}
    day = e.col("day") - DAY_OFF
    m = day >= now // 1440 - days
    ver, coh, tup = e.col("ver")[m], e.col("cohort")[m], e.col("tup")[m]
    wm, vo, n, y = e.col("wm")[m], e.col("voice")[m], e.col("conn")[m].astype(float), e.col("eng")[m].astype(float)
    W, V = w.options["welcome_message"], w.options["voice"]
    pl = np.zeros(len(n))
    for v in np.unique(ver):
        cfg = ctx.reg.config(wf, int(v))
        sel = ver == v
        if cfg is None:
            continue
        for c in np.unique(coh[sel]):
            name = ctx.ing.cohort_name(int(c))
            s2 = sel & (coh == c)
            dw, dv = norm(dist(cfg, name, "welcome_message")), norm(dist(cfg, name, "voice"))
            pw = np.array([dw.get(W[i], 0) if i < len(W) else 0 for i in range(16)])
            pv = np.array([dv.get(V[i], 0) if i < len(V) else 0 for i in range(16)])
            pl[s2] = pw[wm[s2]] * pv[vo[s2]]
    ok = pl > 0
    out = {"window_days": days, "logged_connected": float(n.sum())}
    for tag, cfg in (("proposed", new), ("current", cur)):
        names = assign(cfg, ctx.fs, tup)
        pt = np.zeros(len(n))
        for c in np.unique(names):
            s2 = names == c
            dw, dv = norm(dist(cfg, c, "welcome_message")), norm(dist(cfg, c, "voice"))
            pw = np.array([dw.get(W[i], 0) if i < len(W) else 0 for i in range(16)])
            pv = np.array([dv.get(V[i], 0) if i < len(V) else 0 for i in range(16)])
            pt[s2] = pw[wm[s2]] * pv[vo[s2]]
        r = np.where(ok, pt / np.where(ok, pl, 1), 0.0)
        den = (r * n).sum()
        est = float((r * y).sum() / den) if den > 0 else None
        ess = float((r * n).sum() ** 2 / max((r ** 2 * n).sum(), 1e-9))
        # support: proposed mass, per tuple, that sits on (wm, voice) pairs never logged for that tuple
        tu = np.unique(tup)
        unsup, tot = 0.0, 0.0
        for t in tu:
            st = tup == t
            nt = n[st].sum()
            name = assign(cfg, ctx.fs, np.array([t]))[0]
            dw, dv = norm(dist(cfg, name, "welcome_message")), norm(dist(cfg, name, "voice"))
            seen = {(int(a), int(b)) for a, b in zip(wm[st & ok], vo[st & ok])}
            mass = sum(pw_ * pv_ for a, pw_ in dw.items() for b, pv_ in dv.items()
                       if a in W and b in V and (W.index(a), V.index(b)) not in seen)
            unsup += nt * mass
            tot += nt
        out[tag] = {"engagement": est, "effective_n": round(ess, 1),
                    "unsupported_share": round(unsup / tot, 4) if tot else None}
    a, b = out["proposed"]["engagement"], out["current"]["engagement"]
    out["relative_change"] = (a / b - 1) if a and b else None
    out["method"] = "self-normalised IPS over logged randomised calls; unsupported mass reported, not modelled"
    return out


def did_index(st: dict, cfg: dict) -> dict:
    """Expected caller-ID pickup index (1.0 = peers) under a caller-ID mix, from measured DID ratios."""
    d = norm(dist(cfg, "default", "caller_id"))
    idx, known = 0.0, 0.0
    for k, p in d.items():
        r = (st["did"].get(k, {}).get("last") or {}).get("ratio")
        if r is not None and st["did"].get(k, {}).get("state") != "untested":
            idx += p * r
            known += p
    return {"mix": d, "pickup_index_known_part": round(idx / known, 4) if known else None,
            "share_on_untested": round(1 - known, 4)}


def impact_score(primary: str, urgency: str, predicted: dict, severity: float = 0.5) -> float:
    if urgency == "safety":
        return round(0.8 + 0.2 * max(0.0, min(1.0, severity)), 3)
    lift = predicted.get("engagement_rel" if primary == "engagement_rate" else "pickup_rel", 0.0) or 0.0
    return round(float(np.clip(10 * lift, 0.0, 0.79)), 3)
