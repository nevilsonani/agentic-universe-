"""Statistical primitives. numpy + stdlib only, fully deterministic.

Main tools:
  * `fit_arm_effects` — conditional Poisson GLM with stratum fixed effects: the
    multi-arm generalisation of the Mantel-Haenszel rate ratio. Strata are
    (day, config version, cohort, prompt) so arms are only ever compared with
    calls randomised under the same allocation (no Simpson's paradox when
    allocations change over time). Variance is corrected for binomial outcomes.
  * `heterogeneity` — Cochran's Q + DerSimonian-Laird tau² for segment effects.
  * `cusum_bernoulli` — vectorised Page CUSUM for alarm-on-rate-rise.
  * `rr_two_arm` — Mantel-Haenszel log rate ratio, used for caller-ID health.
"""
from __future__ import annotations

import math
from statistics import NormalDist

import numpy as np

_N = NormalDist()


def norm_cdf(x: float) -> float:
    return _N.cdf(x)


def norm_ppf(p: float) -> float:
    return _N.inv_cdf(min(max(p, 1e-12), 1 - 1e-12))


# ------------------------------------------------------------------ incomplete gamma (for chi² / Poisson tails)
def _gser(a: float, x: float) -> float:
    s = term = 1.0 / a
    ap = a
    for _ in range(500):
        ap += 1
        term *= x / ap
        s += term
        if abs(term) < abs(s) * 1e-14:
            break
    return s * math.exp(-x + a * math.log(x) - math.lgamma(a))


def _gcf(a: float, x: float) -> float:
    b = x + 1 - a
    c = 1e300
    d = 1 / b
    h = d
    for i in range(1, 500):
        an = -i * (i - a)
        b += 2
        d = an * d + b
        d = 1e-300 if abs(d) < 1e-300 else d
        c = b + an / c
        c = 1e-300 if abs(c) < 1e-300 else c
        d = 1 / d
        de = d * c
        h *= de
        if abs(de - 1) < 1e-14:
            break
    return math.exp(-x + a * math.log(x) - math.lgamma(a)) * h


def gammainc_upper(a: float, x: float) -> float:
    """Regularised upper incomplete gamma Q(a, x)."""
    if x <= 0:
        return 1.0
    if x < a + 1:
        return 1.0 - _gser(a, x)
    return _gcf(a, x)


def chi2_sf(q: float, df: int) -> float:
    if df <= 0:
        return 1.0
    return gammainc_upper(df / 2.0, max(q, 0.0) / 2.0)


def poisson_sf(k: int, lam: float) -> float:
    """P(X >= k) for X ~ Poisson(lam)."""
    if k <= 0:
        return 1.0
    return 1.0 - gammainc_upper(float(k), lam)


def wilson(k: float, n: float, z: float = 1.96) -> tuple[float, float]:
    if n <= 0:
        return (0.0, 1.0)
    p = k / n
    den = 1 + z * z / n
    c = (p + z * z / (2 * n)) / den
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return (max(0.0, c - h), min(1.0, c + h))


# ------------------------------------------------------------------ multi-arm effects
def fit_arm_effects(stratum: np.ndarray, arm: np.ndarray, n: np.ndarray, y: np.ndarray, n_arms: int,
                    ref: int, w: np.ndarray | None = None, ridge_sd: float = 1.0, iters: int = 25) -> dict:
    """log rate ratio of each arm vs `ref`, conditional on strata.

    Returns {beta, cov, n_by_arm, y_by_arm, strata_used}. beta[ref] = 0. Arms with
    no comparable data get beta 0 and variance inf.
    """
    stratum = np.asarray(stratum)
    arm = np.asarray(arm, dtype=np.int64)
    n = np.asarray(n, dtype=float)
    y = np.asarray(y, dtype=float)
    if w is None:
        w = np.ones(len(n))
    out_beta = np.zeros(n_arms)
    out_cov = np.full((n_arms, n_arms), 0.0)
    for a in range(n_arms):
        out_cov[a, a] = np.inf
    out_cov[ref, ref] = 0.0
    nb = np.bincount(arm, weights=n, minlength=n_arms)[:n_arms]
    yb = np.bincount(arm, weights=y, minlength=n_arms)[:n_arms]
    res = {"beta": out_beta, "cov": out_cov, "n_by_arm": nb, "y_by_arm": yb, "strata_used": 0}
    if len(n) == 0:
        return res
    su, sinv = np.unique(stratum, return_inverse=True)
    K = len(su)
    idx = sinv * n_arms + arm
    N = np.bincount(idx, weights=n * w, minlength=K * n_arms).reshape(K, n_arms)
    Y = np.bincount(idx, weights=y * w, minlength=K * n_arms).reshape(K, n_arms)
    Y2 = np.bincount(idx, weights=y * w * w, minlength=K * n_arms).reshape(K, n_arms)
    present = N > 0
    keep = (present.sum(axis=1) >= 2) & (Y.sum(axis=1) > 0)
    if not keep.any():
        return res
    N, Y, Y2, present = N[keep], Y[keep], Y2[keep], present[keep]
    active = np.flatnonzero(present.any(axis=0) & (np.arange(n_arms) != ref))
    if ref not in np.flatnonzero(present.any(axis=0)):
        # reference never co-occurs: estimate relative to the best-connected arm instead
        return res
    beta = np.zeros(n_arms)
    Yk = Y.sum(axis=1)
    lam = 1.0 / ridge_sd ** 2
    I = None
    for _ in range(iters):
        e = np.where(present, N * np.exp(beta), 0.0)
        pi = e / e.sum(axis=1, keepdims=True)
        U = (Y - Yk[:, None] * pi).sum(axis=0) - lam * beta
        I = (np.einsum("k,ka,ab->ab", Yk, pi, np.eye(n_arms)) - np.einsum("k,ka,kb->ab", Yk, pi, pi))
        I = I + lam * np.eye(n_arms)
        Ia = I[np.ix_(active, active)]
        try:
            step = np.linalg.solve(Ia, U[active])
        except np.linalg.LinAlgError:
            break
        step = np.clip(step, -1.0, 1.0)
        beta[active] += step
        if np.max(np.abs(step)) < 1e-8:
            break
    Ia = I[np.ix_(active, active)]
    cov_a = np.linalg.inv(Ia)
    pbar = float(Y.sum() / max(N.sum(), 1e-9))           # binomial dispersion: var = mu (1 - p)
    disc = float(Y2.sum() / max(Y.sum(), 1e-9))           # time-discount weights → effective sample size
    cov_a = cov_a * max(1.0 - pbar, 0.05) * disc
    out_beta[active] = beta[active]
    for i, a in enumerate(active):
        for j, b in enumerate(active):
            out_cov[a, b] = cov_a[i, j]
    res.update(beta=out_beta, cov=out_cov, strata_used=int(keep.sum()))
    return res


def heterogeneity(theta: np.ndarray, se: np.ndarray) -> dict:
    """Cochran's Q and DerSimonian-Laird tau² across segment estimates."""
    theta, se = np.asarray(theta, float), np.asarray(se, float)
    ok = np.isfinite(se) & (se > 0)
    if ok.sum() < 2:
        return {"q": 0.0, "df": 0, "p": 1.0, "tau2": 0.0, "mean": 0.0}
    t, v = theta[ok], se[ok] ** 2
    wts = 1 / v
    mean = float((wts * t).sum() / wts.sum())
    q = float((wts * (t - mean) ** 2).sum())
    df = int(ok.sum() - 1)
    c = wts.sum() - (wts ** 2).sum() / wts.sum()
    tau2 = max(0.0, (q - df) / c) if c > 0 else 0.0
    return {"q": q, "df": df, "p": chi2_sf(q, df), "tau2": tau2, "mean": mean}


def normal_posterior(est: float, var: float, prior_mean: float, prior_var: float) -> tuple[float, float]:
    if not np.isfinite(var):
        return prior_mean, prior_var
    pv = 1.0 / (1.0 / prior_var + 1.0 / var)
    return pv * (prior_mean / prior_var + est / var), pv


# ------------------------------------------------------------------ two-arm MH (caller-ID health)
def rr_two_arm(a: np.ndarray, n1: np.ndarray, b: np.ndarray, n0: np.ndarray) -> tuple[float, float]:
    """Mantel-Haenszel log rate ratio (arm 1 vs arm 0) over strata, with Greenland-Robins variance.
    a, b = successes; n1, n0 = trials. Returns (log_rr, var)."""
    a, n1, b, n0 = (np.asarray(x, float) for x in (a, n1, b, n0))
    m = (n1 > 0) & (n0 > 0)
    if not m.any():
        return 0.0, np.inf
    a, n1, b, n0 = a[m], n1[m], b[m], n0[m]
    N = n1 + n0
    R = (a * n0 / N).sum()
    S = (b * n1 / N).sum()
    if R <= 0 or S <= 0:
        # zero successes on one side: continuity-corrected crude estimate
        R, S = R + 0.5, S + 0.5
    var = ((n1 * n0 * (a + b) - a * b * N) / N ** 2).sum() / (R * S)
    return math.log(R / S), max(var, 1e-9)


# ------------------------------------------------------------------ CUSUM
def cusum_bernoulli(x: np.ndarray, p0: float, p1: float, s0: float = 0.0) -> tuple[float, float, int]:
    """Page CUSUM on a 0/1 sequence for a rise p0 → p1. Vectorised:
    S_t = D_t − min(0, min_{s≤t} D_s) with D_t = s0 + Σ llr. Returns (S_end, S_max, argmax)."""
    x = np.asarray(x, dtype=float)
    if len(x) == 0:
        return s0, s0, -1
    up, dn = math.log(p1 / p0), math.log((1 - p1) / (1 - p0))
    D = s0 + np.cumsum(np.where(x > 0, up, dn))
    S = D - np.minimum(0.0, np.minimum.accumulate(D))
    i = int(np.argmax(S))
    return float(S[-1]), float(S[i]), i


def cusum_arl0(p0: float, p1: float, h: float, n_runs: int = 200, max_len: int = 400_000, seed: int = 7) -> float:
    """Monte Carlo in-control average run length (calls until a false alarm)."""
    rng = np.random.default_rng(seed)
    lens = []
    for _ in range(n_runs):
        s, t = 0.0, 0
        while t < max_len:
            x = rng.random(20_000) < p0
            up, dn = math.log(p1 / p0), math.log((1 - p1) / (1 - p0))
            D = s + np.cumsum(np.where(x, up, dn))
            S = D - np.minimum(0.0, np.minimum.accumulate(D))
            hit = np.flatnonzero(S > h)
            if len(hit):
                t += int(hit[0]) + 1
                break
            s, t = float(S[-1]), t + len(x)
        lens.append(t)
    return float(np.mean(lens))
