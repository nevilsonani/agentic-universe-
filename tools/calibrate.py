"""Monte-Carlo calibration of the decision rules (numbers quoted in SUBMISSION.md §5 and §7).

    python tools/calibrate.py [--runs 400]

1. False-winner rate: A/A worlds (no arm differs). Batched allocation updated twice a day
   with the production rule (share ∝ P(best)^2, 10% holdout, floors), the *winner rule*
   checked every 4 sim-hours for 8 days with the production thresholds. We report the share
   of runs in which any non-control arm was ever declared a winner, and how much traffic
   the allocation put on non-control arms anyway (harmless here, but it is the cost of looking).
2. Power: same, with one arm truly +10% — how often, and how fast, it is declared.
3. False decay alarms: an arm that truly stays +8% holds 75% share; how often does the decay
   rule (post-promotion vs recent, z ≥ 2.6, ≥5% drop, recent below control) fire in 8 days.
4. Hallucination CUSUM: in-control average run length and detection delay at 2.5× and 3.6×.
"""
from __future__ import annotations

import argparse
import math

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from controller.settings import Settings  # noqa: E402
from controller.stats import cusum_arl0, cusum_bernoulli, fit_arm_effects, normal_posterior  # noqa: E402

S = Settings()


def world(rng, rr, days=8, conn_per_day=900, base=0.55, check_every_h=4, decay_test=False, start_share=None):
    A = len(rr)
    share = np.array(start_share if start_share is not None else [0.8] + [0.2 / (A - 1)] * (A - 1))
    cells = []                                    # (stratum, arm, n, y, day)
    declared, first_day, nonctrl_traffic, decays = False, None, [], 0
    held_since = 0 if start_share is not None else None
    for day in range(days):
        for block in range(24 // check_every_h):
            n = int(conn_per_day * check_every_h / 12) if 9 <= block * check_every_h < 21 else 0
            if n:
                arms = rng.choice(A, n, p=share / share.sum())
                y = rng.random(n) < base * np.asarray(rr)[arms]
                stratum = day * 10 + (0 if block < 3 else 1)
                for a in range(A):
                    m = arms == a
                    cells.append((stratum, a, m.sum(), y[m].sum(), day))
                nonctrl_traffic.append(1 - share[0] / share.sum())
            if not cells:
                continue
            c = np.array(cells)
            f = fit_arm_effects(c[:, 0], c[:, 1], c[:, 2], c[:, 3], A, 0)
            mu, sd = np.zeros(A), np.zeros(A)
            for a in range(1, A):
                mu[a], v = normal_posterior(f["beta"][a], f["cov"][a, a], 0.0, S.prior_sd ** 2)
                sd[a] = math.sqrt(v)
            draws = mu + rng.standard_normal((2000, A)) * sd
            draws[:, 0] = 0
            pbest = np.bincount(draws.argmax(1), minlength=A) / 2000
            for a in range(1, A):
                rrd = np.exp(draws[:, a])
                if (pbest[a] >= S.winner_p_best and (rrd > 1).mean() >= S.winner_p_ref
                        and rrd.mean() >= 1 + S.winner_min_lift and sd[a] <= S.winner_max_sd):
                    if not declared:
                        first_day = day + block * check_every_h / 24
                    declared = True
            if decay_test and held_since is not None and day - held_since >= 2:
                post = c[(c[:, 4] >= held_since) & (c[:, 4] < day - 1)]
                rec = c[c[:, 4] >= day - 1]
                fp = fit_arm_effects(post[:, 0], post[:, 1], post[:, 2], post[:, 3], A, 0)
                fr = fit_arm_effects(rec[:, 0], rec[:, 1], rec[:, 2], rec[:, 3], A, 0)
                a = 1
                z = (fp["beta"][a] - fr["beta"][a]) / math.sqrt(fp["cov"][a, a] + fr["cov"][a, a])
                hi_rec = math.exp(fr["beta"][a] + 1.2816 * math.sqrt(fr["cov"][a, a]))
                if (z >= S.decay_drop_z and fp["beta"][a] - fr["beta"][a] >= math.log(1 + S.decay_min_drop)
                        and fr["beta"][a] < 0) or (hi_rec < 1 and fr["n_by_arm"][0] >= S.decay_min_control_n):
                    decays += 1
                    break
            if block in (2, 4) and not decay_test:        # two governance windows a day (10:00, 16:00)
                w = np.maximum(pbest, 1e-6) ** S.ts_gamma
                w = w / w.sum()
                w[0] = max(w[0], S.holdout_share)
                w[1:] = np.maximum(w[1:], S.floor_share)
                share = w / w.sum()
        if decays:
            break
    return declared, first_day, float(np.mean(nonctrl_traffic)) if nonctrl_traffic else 0.0, decays


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=400)
    a = ap.parse_args()
    rng = np.random.default_rng(11)
    R = a.runs
    aa = [world(rng, [1, 1, 1]) for _ in range(R)]
    fw = np.mean([x[0] for x in aa])
    print(f"1. A/A false-winner rate over 8 days, winner rule checked every 4h: {fw:.1%} of runs "
          f"(non-control traffic on average {np.mean([x[2] for x in aa]):.0%})")
    pw = [world(rng, [1, 1.10, 1]) for _ in range(R)]
    days = [x[1] for x in pw if x[0]]
    print(f"2. Power for a +10% arm: declared in {np.mean([x[0] for x in pw]):.0%} of runs; median day "
          f"{np.median(days) if days else float('nan'):.1f}")
    fd = [world(rng, [1, 1.08, 1], decay_test=True, start_share=[0.10, 0.75, 0.15]) for _ in range(R)]
    print(f"3. False decay alarm for a stable +8% arm at 75% share: {np.mean([x[3] > 0 for x in fd]):.1%} of 8-day runs")
    td = [world(rng, [1, 0.92, 1], decay_test=True, start_share=[0.10, 0.75, 0.15]) for _ in range(R)]
    print(f"   Detection of a decayed arm (true 0.92, held 75%): {np.mean([x[3] > 0 for x in td]):.1%} within 8 days")
    p0 = 0.0147
    arl = cusum_arl0(p0, p0 * S.halluc_cusum_ratio, S.halluc_cusum_h, n_runs=60)
    print(f"4. Hallucination CUSUM (p0={p0:.2%}, h={S.halluc_cusum_h}): in-control ARL ≈ {arl:,.0f} connected calls "
          f"(≈ {arl / 1100:.0f} days at 1,100 connected/day)")
    for p1 in (p0 * 2.5, 0.0535):
        delays = []
        for _ in range(300):
            x = rng.random(20000) < p1
            up, dn = math.log(S.halluc_cusum_ratio), math.log((1 - p0 * S.halluc_cusum_ratio) / (1 - p0))
            D = np.cumsum(np.where(x, up, dn))
            Sx = D - np.minimum(0.0, np.minimum.accumulate(D))
            hit = np.flatnonzero(Sx > S.halluc_cusum_h)
            delays.append(hit[0] + 1 if len(hit) else 20000)
        print(f"   detection delay at flag rate {p1:.2%}: median {np.median(delays):.0f} connected calls")


if __name__ == "__main__":
    main()
