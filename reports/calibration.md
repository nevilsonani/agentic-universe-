# Calibration of the decision rules (Monte Carlo)

`python tools/calibrate.py --runs 200` — uses the production thresholds from `controller/settings.py`
and the production estimator (`stats.fit_arm_effects` + skeptical normal prior).

```
1. A/A false-winner rate over 8 days, winner rule checked every 4h: 0.0% of runs (non-control traffic on average 68%)
2. Power for a +10% arm: declared in 52% of runs; median day 4.8
3. False decay alarm for a stable +8% arm at 75% share: 8.5% of 8-day runs
   Detection of a decayed arm (true 0.92, held 75%): 95.5% within 8 days
4. Hallucination CUSUM (p0=1.47%, h=7.0): in-control ARL ≈ 161,848 connected calls (≈ 147 days at 1,100 connected/day)
   detection delay at flag rate 3.67%: median 478 connected calls
   detection delay at flag rate 5.35%: median 232 connected calls
```

Reading it:

* The *winner declaration* (an audited claim: P(best) ≥ 0.95, P(> control) ≥ 0.99, lift ≥ 2%, posterior
  sd ≤ 0.06) essentially never fires on noise, even with 48 looks. That strictness costs power: a real +10%
  arm is *declared* in half of 8-day runs. Traffic does not wait for the declaration — batched Thompson
  allocation moves share toward it as soon as P(best) leans its way — so the declaration is the claim we
  are willing to defend to a compliance head, not the trigger for exploiting.
* "Non-control traffic 68%" in A/A is the price of looking: harmless when arms are equal, and bounded by
  the per-change harm check when they are not.
* Decay: ~1 in 12 eight-day runs flags a stable +8% arm (the 10% holdout is small, and we look hourly).
  The consequence is bounded — the arm drops to a floor share and returns when recent evidence clears
  +2% at the 90% level — and the alternative (a larger holdout) costs every workflow every day.
* Hallucination: one false alarm per ~5 months per workflow at ks_cancel volume, and a CUSUM alarm is
  only acted on after a confirmatory Poisson test (p < 1e-3 on ≥ 100 calls).
