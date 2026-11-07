"""Models fitted on the aggregates. Everything here is a pure function of the store.

EngagementModel  — effect of each welcome/voice arm on engagement, overall and per
                   feature segment, combined into per-cohort posteriors (C6/C7).
PickupTimeModel  — pickup by hour for any cohort, attempt-adjusted, from history (C3/C4).
did_health       — each caller ID's pickup relative to its peers in the same strata (C5).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from .features import Cohort, FeatureSpace
from .ingest import DAY_OFF, CellTable
from .settings import Settings
from .stats import chi2_sf, fit_arm_effects, heterogeneity, normal_posterior, rr_two_arm


# ====================================================================== engagement
@dataclass
class DimFit:
    dim: str
    arms: list[str]
    ref: int
    beta: np.ndarray            # likelihood estimate, log RR vs ref (all segments pooled)
    var: np.ndarray
    post_mean: np.ndarray       # after the skeptical prior
    post_var: np.ndarray
    n: np.ndarray               # connected calls per arm (time-weighted if discounted)
    y: np.ndarray
    dev: dict = field(default_factory=dict)       # feature -> {value: (dev[A], devvar[A])}
    het: dict = field(default_factory=dict)       # feature -> {arm: {"p":..,"tau2":..,"sig":bool}}
    seg: dict = field(default_factory=dict)       # (feature, value) -> (beta[A], var[A], n[A])


class EngagementModel:
    def __init__(self, s: Settings, fs: FeatureSpace, eng: CellTable, options: dict[str, list[str]],
                 ref: dict[str, str]):
        self.s, self.fs, self.eng, self.options, self.ref = s, fs, eng, options, ref

    def _rows(self, now_day: int, half_life: float | None, since_day: int | None = None,
              until_day: int | None = None):
        e = self.eng
        if e.n == 0:
            return None
        day = e.col("day") - DAY_OFF
        m = day >= (since_day if since_day is not None else -10 ** 6)
        if until_day is not None:
            m &= day < until_day
        w = np.ones(e.n)
        if half_life:
            w = 0.5 ** (np.maximum(now_day - day, 0) / half_life)
        cols = {k: e.col(k)[m] for k in ("day", "ver", "cohort", "prompt", "tup", "wm", "voice", "conn", "eng")}
        cols["w"] = w[m]
        # stratum: same allocation regime (day, version, cohort, prompt)
        cols["stratum"] = (((cols["day"] * 4096 + cols["ver"]) * 1024 + cols["cohort"]) * 64 + cols["prompt"])
        return cols

    def fit(self, now_day: int, half_life: float | None = None, since_day: int | None = None,
            segments: bool = True, until_day: int | None = None) -> dict[str, DimFit]:
        rows = self._rows(now_day, half_life, since_day, until_day)
        out = {}
        for dim, col in (("welcome_message", "wm"), ("voice", "voice")):
            arms = self.options[dim]
            A = len(arms)
            ref = arms.index(self.ref[dim]) if self.ref.get(dim) in arms else 0
            if rows is None or len(rows["conn"]) == 0:
                z = np.zeros(A)
                inf = np.full(A, np.inf)
                inf[ref] = 0
                pv = np.full(A, self.s.prior_sd ** 2)
                pv[ref] = 0
                out[dim] = DimFit(dim, arms, ref, z, inf, z.copy(), pv, z.copy(), z.copy())
                continue
            arm = rows[col]
            ok = arm < A
            r = fit_arm_effects(rows["stratum"][ok], arm[ok], rows["conn"][ok], rows["eng"][ok], A, ref,
                                w=rows["w"][ok])
            beta, var = r["beta"], np.diag(r["cov"]).copy()
            pm, pv = np.zeros(A), np.zeros(A)
            for a in range(A):
                if a == ref:
                    continue
                pm[a], pv[a] = normal_posterior(beta[a], var[a], 0.0, self.s.prior_sd ** 2)
            df = DimFit(dim, arms, ref, beta, var, pm, pv, r["n_by_arm"], r["y_by_arm"])
            if segments:
                self._segments(df, rows, col, ok)
            out[dim] = df
        return out

    def _segments(self, df: DimFit, rows, col, ok):
        A, ref = len(df.arms), df.ref
        n_tests = max(1, len(self.fs.names) * (A - 1))
        alpha = self.s.het_alpha / n_tests          # Bonferroni across (feature, arm) heterogeneity claims
        tups = rows["tup"]
        for f in self.fs.names:
            vals = self.fs.values(f)
            if len(vals) < 2:
                continue
            fc = self.fs.col(tups, f)
            ests = {}
            for v in vals:
                code = self.fs.codes[f][v]
                m = ok & (fc == code)
                if m.sum() == 0:
                    continue
                r = fit_arm_effects(rows["stratum"][m], rows[col][m], rows["conn"][m], rows["eng"][m], A, ref,
                                    w=rows["w"][m])
                ests[v] = (r["beta"], np.diag(r["cov"]).copy(), r["n_by_arm"])
                df.seg[(f, v)] = ests[v]
            df.het[f] = {}
            devs = {v: (np.zeros(A), np.zeros(A)) for v in ests}
            for a in range(A):
                if a == ref:
                    continue
                th = np.array([ests[v][0][a] for v in ests])
                se = np.sqrt(np.array([ests[v][1][a] for v in ests]))
                h = heterogeneity(th, se)
                # `sig` (Bonferroni) gates what we *claim* (segment winners in the audit); allocation uses
                # partial pooling (DerSimonian-Laird) without a hard gate — shrinkage is the multiplicity control.
                sig = h["p"] < alpha and h["tau2"] > 0
                df.het[f][df.arms[a]] = {"p": h["p"], "q": h["q"], "df": h["df"], "tau2": h["tau2"], "sig": bool(sig)}
                if h["tau2"] <= 0:
                    continue
                for v, (b, vv, _) in ests.items():
                    if not np.isfinite(vv[a]):
                        continue
                    shrink = h["tau2"] / (h["tau2"] + vv[a])
                    devs[v][0][a] = (b[a] - h["mean"]) * shrink
                    devs[v][1][a] = shrink * vv[a]
            df.dev[f] = devs

    # ------------------------------------------------------------------ cohort posteriors
    @staticmethod
    def cohort_draws(fit: DimFit, fs: FeatureSpace, tups: np.ndarray, weights: np.ndarray, draws: int,
                     rng: np.random.Generator) -> np.ndarray:
        """Posterior draws of the cohort-average rate ratio of every arm vs ref. Shape (draws, A)."""
        A = len(fit.arms)
        sd = np.sqrt(np.maximum(fit.post_var, 0))
        g = fit.post_mean[None, :] + rng.standard_normal((draws, A)) * sd[None, :]
        g[:, fit.ref] = 0.0
        if len(tups) == 0:
            return np.exp(g)
        w = np.asarray(weights, float)
        w = w / w.sum()
        theta = np.repeat(g[:, None, :], len(tups), axis=1)          # (S, T, A)
        for f, devs in fit.dev.items():
            if not any(d[0].any() for d in devs.values()):
                continue
            fc = fs.col(tups, f)
            for v, (dm, dv) in devs.items():
                if not dm.any():
                    continue
                code = fs.codes[f][v]
                sel = fc == code
                if not sel.any():
                    continue
                dd = dm[None, :] + rng.standard_normal((draws, A)) * np.sqrt(dv)[None, :]
                dd[:, fit.ref] = 0.0
                theta[:, sel, :] += dd[:, None, :]
        return np.einsum("sta,t->sa", np.exp(theta), w)


def summarize_draws(rr: np.ndarray, arms: list[str], ref: int) -> dict:
    best = np.bincount(rr.argmax(axis=1), minlength=rr.shape[1]) / rr.shape[0]
    out = {}
    for a, name in enumerate(arms):
        x = rr[:, a]
        out[name] = {"rr": float(x.mean()), "lo": float(np.quantile(x, 0.05)), "hi": float(np.quantile(x, 0.95)),
                     "p_best": float(best[a]), "p_gt_ref": float((x > 1.0).mean()) if a != ref else 0.0,
                     "p_lt_ref": float((x < 1.0).mean()) if a != ref else 0.0,
                     "p_safe": float((x > 0.98).mean()) if a != ref else 1.0,
                     "sd_log": float(np.log(np.maximum(x, 1e-9)).std())}
    return out


# ====================================================================== call time
class PickupTimeModel:
    """Attempt-adjusted pickup profile by hour for any set of feature tuples.

    pickup(tup, hour) ∝ r_all(hour) · Π_f dev_f(value_f(tup), hour), where each
    feature's hour-deviation is kept only if a χ² test says that feature changes
    the hour profile at all (most don't; age usually does), and is shrunk toward 1
    with a pseudo-count prior so a thin cell can't produce a wild weight.
    """

    def __init__(self, s: Settings, fs: FeatureSpace, ptime: CellTable, window: tuple[int, int],
                 since_day: int | None = None, prior_dials: float = 300.0):
        self.s, self.fs, self.window = s, fs, window
        self.hours = list(range(window[0], window[1]))
        H = len(self.hours)
        day = ptime.col("day") - DAY_OFF
        hour = ptime.col("hour")
        m = (hour >= window[0]) & (hour < window[1])
        if since_day is not None:
            m &= day >= since_day
        self.tup = ptime.col("tup")[m]
        self.hr = hour[m] - window[0]
        att = ptime.col("att")[m]
        self.dials = ptime.col("dials")[m].astype(float)
        self.conn = ptime.col("conn")[m].astype(float)
        self.ok = len(self.dials) > 0 and self.dials.sum() > 0
        if not self.ok:
            return
        # attempt-only expectation (indirect standardisation)
        pa = np.bincount(att, weights=self.conn, minlength=6) / np.maximum(np.bincount(att, weights=self.dials,
                                                                                         minlength=6), 1)
        self.E = self.dials * pa[att]
        Oh = np.bincount(self.hr, weights=self.conn, minlength=H)
        Eh = np.bincount(self.hr, weights=self.E, minlength=H)
        self.r_all = (Oh + 1) / (Eh + 1)
        self.p_base = float(self.conn.sum() / self.dials.sum())
        self.dev: dict[str, dict[str, np.ndarray]] = {}
        self.tests: dict[str, dict] = {}
        n_feat = len(fs.names)
        for f in fs.names:
            fc = fs.col(self.tup, f)
            vals = fs.values(f)
            devs, q, dfree = {}, 0.0, 0
            for v in vals:
                code = fs.codes[f][v]
                sel = fc == code
                if not sel.any():
                    continue
                O = np.bincount(self.hr[sel], weights=self.conn[sel], minlength=H)
                Eexp = np.bincount(self.hr[sel], weights=self.E[sel], minlength=H) * self.r_all
                lvl = O.sum() / max(Eexp.sum(), 1e-9)            # value's own level (not an hour effect)
                Eexp = Eexp * lvl
                k = prior_dials * self.p_base
                d = (O + k) / (Eexp + k)                           # shrunk toward 1
                devs[v] = d
                ok = Eexp > 5
                q += float((((O - Eexp) ** 2) / np.maximum(Eexp, 1e-9))[ok].sum())
                dfree += max(int(ok.sum()) - 1, 0)
            p = chi2_sf(q, dfree) if dfree > 0 else 1.0
            sig = p < 0.01 / n_feat
            self.tests[f] = {"chi2": q, "df": dfree, "p": p, "used": bool(sig)}
            if sig:
                self.dev[f] = devs

    def profile(self, tups: np.ndarray, weights: np.ndarray) -> np.ndarray:
        """Relative pickup by hour (mean 1 over the window) for a population of tups."""
        H = len(self.hours)
        if not self.ok or len(tups) == 0:
            return np.ones(H)
        w = np.asarray(weights, float)
        w = w / w.sum()
        logp = np.zeros((len(tups), H)) + np.log(self.r_all)[None, :]
        for f, devs in self.dev.items():
            fc = self.fs.col(tups, f)
            for v, d in devs.items():
                sel = fc == self.fs.codes[f][v]
                logp[sel] += np.log(d)[None, :]
        prof = (np.exp(logp) * w[:, None]).sum(axis=0)
        return prof / prof.mean()

    def features_used(self) -> list[str]:
        return list(self.dev)


# ====================================================================== caller ID health
def did_health(pdid: CellTable, n_did: int, now_minute: int, window_h: int, exclude: set[int] = frozenset(),
               z: float = 1.645) -> dict[int, dict]:
    """Pickup of each DID relative to the other (non-excluded) DIDs, MH-pooled over
    (day, hour, version, cohort) strata, over the last `window_h` hours."""
    if pdid.n == 0:
        return {}
    day = pdid.col("day") - DAY_OFF
    abs_hour = day * 24 + pdid.col("hour")
    now_h = now_minute // 60
    m = abs_hour >= now_h - window_h
    if not m.any():
        return {}
    did = pdid.col("did")[m]
    dials = pdid.col("dials")[m].astype(float)
    conn = pdid.col("conn")[m].astype(float)
    strat = ((abs_hour[m] * 4096 + pdid.col("ver")[m]) * 1024 + pdid.col("cohort")[m])
    su, sinv = np.unique(strat, return_inverse=True)
    K = len(su)
    D = np.zeros((K, n_did + 1))
    C = np.zeros((K, n_did + 1))
    dd = np.minimum(did, n_did)
    np.add.at(D, (sinv, dd), dials)
    np.add.at(C, (sinv, dd), conn)
    out = {}
    peers_mask = np.array([i not in exclude for i in range(n_did)] + [False])
    for i in range(n_did):
        n1, a = D[:, i], C[:, i]
        pm = peers_mask.copy()
        pm[i] = False
        n0, b = D[:, pm].sum(axis=1), C[:, pm].sum(axis=1)
        tot_d = float(n1.sum())
        if tot_d == 0:
            continue
        lr, var = rr_two_arm(a, n1, b, n0)
        se = math.sqrt(var) if np.isfinite(var) else float("inf")
        peer_rate = b.sum() / max(n0.sum(), 1)
        out[i] = {"ratio": math.exp(lr), "lo": math.exp(lr - z * se) if np.isfinite(se) else 0.0,
                  "hi": math.exp(lr + z * se) if np.isfinite(se) else float("inf"),
                  "dials": tot_d, "conn": float(a.sum()), "rate": float(a.sum() / tot_d),
                  "expected_conn": float(tot_d * peer_rate), "peer_rate": float(peer_rate)}
    return out


# ====================================================================== simple KPIs
def kpis(ptime: CellTable, eng: CellTable, day_from: int, day_to: int) -> dict:
    """Pickup and engagement (never blended) over [day_from, day_to)."""
    d = ptime.col("day") - DAY_OFF
    m = (d >= day_from) & (d < day_to)
    dials, conn = float(ptime.col("dials")[m].sum()), float(ptime.col("conn")[m].sum())
    de = eng.col("day") - DAY_OFF
    me = (de >= day_from) & (de < day_to)
    c2, e2 = float(eng.col("conn")[me].sum()), float(eng.col("eng")[me].sum())
    return {"dials": dials, "connected": conn, "engaged": e2,
            "pickup_rate": conn / dials if dials else None,
            "engagement_rate": e2 / c2 if c2 else None}
