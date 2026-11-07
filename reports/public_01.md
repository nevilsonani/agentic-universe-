# Grade report — public_01_dev

## Outcomes vs baseline (no-op controller)

| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | engagement gap captured | hallucinated calls (base) |
|---|---|---|---|---|---|---|---|---|
| ks_cancel | 0.408 | +22.4% | 0.266 | +11.6% | 0.531 | +8.1% | 49% | 80 (47) |
| nw_kyc | 0.485 | +33.5% | 0.297 | +22.8% | 0.626 | +5.9% | 29% | 30 (21) |
| nw_renewal | 0.460 | +9.5% | 0.284 | +7.8% | 0.552 | +1.3% | 15% | 46 (42) |
| ALL | 0.441 | +19.8% | 0.278 | +12.3% | 0.558 | +5.3% | 31% | 156 (110) |

## Changes applied by the controller

35 applied — harmful: 1, beneficial: 15, neutral: 19

- nw_renewal v2 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=-0.022 → **harmful**
- ks_cancel v2 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.022 → **beneficial**
- ks_cancel v3 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.026 → **beneficial**
- nw_renewal v3 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.049 → **beneficial**
- ks_cancel v4 (controller_approved) dims=['cohorts', 'welcome_message', 'voice', 'schedule'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- nw_renewal v4 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.030 → **beneficial**
- ks_cancel v5 (controller_auto) dims=['welcome_message', 'voice'] engageΔ=+0.001 didΔ=+0.000 → **neutral**
- nw_renewal v5 (controller_approved) dims=['cohorts', 'welcome_message', 'voice', 'schedule'] engageΔ=+0.022 didΔ=+0.000 → **beneficial**
- ks_cancel v6 (controller_auto) dims=['welcome_message', 'voice'] engageΔ=+0.022 didΔ=+0.000 → **beneficial**
- nw_renewal v6 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.062 didΔ=+0.000 → **beneficial**
- ks_cancel v7 (controller_auto) dims=['welcome_message', 'voice'] engageΔ=+0.002 didΔ=+0.000 → **neutral**
- nw_kyc v2 (controller_approved) dims=['cohorts', 'welcome_message', 'voice', 'caller_id', 'schedule'] engageΔ=+0.023 didΔ=+0.300 → **beneficial**
- ks_cancel v8 (controller_auto) dims=['voice'] engageΔ=+0.015 didΔ=+0.000 → **beneficial**
- ks_cancel v9 (controller_auto) dims=['welcome_message', 'voice'] engageΔ=+0.015 didΔ=+0.000 → **beneficial**
- nw_renewal v7 (controller_approved) dims=['welcome_message'] engageΔ=+0.012 didΔ=+0.000 → **beneficial**
- ks_cancel v10 (controller_auto) dims=['welcome_message', 'voice'] engageΔ=+0.002 didΔ=+0.000 → **neutral**
- ks_cancel v11 (controller_auto) dims=['welcome_message', 'voice'] engageΔ=+0.017 didΔ=+0.000 → **beneficial**
- ks_cancel v12 (controller_auto) dims=['schedule'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- nw_renewal v8 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.008 didΔ=+0.000 → **neutral**
- nw_kyc v3 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.019 didΔ=+0.000 → **beneficial**
- nw_renewal v9 (controller_auto) dims=['schedule'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- ks_cancel v13 (controller_auto) dims=['welcome_message', 'voice'] engageΔ=+0.008 didΔ=+0.000 → **neutral**
- ks_cancel v14 (controller_auto) dims=['welcome_message', 'voice'] engageΔ=+0.004 didΔ=+0.000 → **neutral**
- nw_kyc v4 (controller_approved) dims=['welcome_message', 'voice', 'schedule'] engageΔ=+0.028 didΔ=+0.000 → **beneficial**
- nw_renewal v10 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.009 didΔ=+0.000 → **neutral**
- ks_cancel v15 (controller_auto) dims=['schedule'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- nw_kyc v5 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.046 didΔ=+0.000 → **beneficial**
- nw_renewal v11 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.001 didΔ=+0.000 → **neutral**
- nw_renewal v12 (controller_auto) dims=['schedule'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- ks_cancel v16 (controller_auto) dims=['welcome_message', 'voice'] engageΔ=+0.006 didΔ=+0.000 → **neutral**
- ks_cancel v17 (controller_auto) dims=['welcome_message'] engageΔ=+0.005 didΔ=+0.000 → **neutral**
- ks_cancel v18 (controller_auto) dims=['schedule'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- nw_renewal v13 (controller_auto) dims=['schedule'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- ks_cancel v19 (controller_auto) dims=['welcome_message', 'voice'] engageΔ=+0.006 didΔ=+0.000 → **neutral**
- nw_kyc v6 (controller_approved) dims=['schedule'] engageΔ=+0.000 didΔ=+0.000 → **neutral**

## Governance

```
{
  "approval_requests": 15,
  "decisions": {
    "approved": 13,
    "rejected": 2,
    "expired": 0
  },
  "rejection_reasons": {
    "approver declined (judgement call)": 2
  },
  "applied_via_approval": 12,
  "auto_applied": 23,
  "auto_apply_rejected": 0,
  "apply_conflicts": 0,
  "lapsed": 0,
  "peak_requests_per_24h": {
    "nw_renewal": 2,
    "nw_kyc": 1,
    "ks_cancel": 1
  }
}
```

## Incidents

```
[]
```

## Decaying variants — share of in-population calls still on the variant, by day after onset

```
{
  "nw_renewal:welcome_message=W3": {
    "3": 0.712,
    "4": 0.758,
    "5": 0.8,
    "6": 0.807,
    "7": 0.792
  }
}
```

Credits used: 0 · validation runs: 0 · LLM fix calls: 0