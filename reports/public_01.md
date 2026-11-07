# Grade report — public_01_dev

## Outcomes vs baseline (no-op controller)

| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | engagement gap captured | hallucinated calls (base) |
|---|---|---|---|---|---|---|---|---|
| ks_cancel | 0.378 | +13.4% | 0.253 | +6.2% | 0.518 | +5.5% | 16% | 61 (47) |
| nw_kyc | 0.472 | +29.8% | 0.298 | +23.0% | 0.608 | +2.9% | 17% | 21 (21) |
| nw_renewal | 0.459 | +9.4% | 0.285 | +8.4% | 0.550 | +0.8% | 7% | 43 (42) |
| ALL | 0.425 | +15.3% | 0.273 | +10.1% | 0.548 | +3.5% | 13% | 125 (110) |

## Changes applied by the controller

39 applied — neutral: 25, beneficial: 13, harmful: 1

- nw_renewal v2 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=-0.014 → **neutral**
- ks_cancel v2 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.014 → **neutral**
- ks_cancel v3 (controller_approved) dims=['cohorts', 'welcome_message', 'voice', 'schedule'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- ks_cancel v4 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.027 → **beneficial**
- nw_renewal v3 (controller_approved) dims=['cohorts', 'welcome_message', 'voice', 'schedule'] engageΔ=+0.022 didΔ=+0.000 → **beneficial**
- ks_cancel v5 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- ks_cancel v6 (controller_auto) dims=['welcome_message', 'voice'] engageΔ=+0.001 didΔ=+0.000 → **neutral**
- nw_renewal v4 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.009 didΔ=+0.000 → **neutral**
- nw_renewal v5 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.038 → **beneficial**
- nw_renewal v6 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.033 → **beneficial**
- ks_cancel v7 (controller_auto) dims=['welcome_message', 'voice'] engageΔ=-0.001 didΔ=+0.000 → **neutral**
- nw_renewal v7 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.018 didΔ=+0.000 → **beneficial**
- nw_kyc v2 (controller_approved) dims=['cohorts', 'welcome_message', 'voice', 'caller_id', 'schedule'] engageΔ=+0.022 didΔ=+0.297 → **beneficial**
- ks_cancel v8 (controller_auto) dims=['voice'] engageΔ=+0.003 didΔ=+0.000 → **neutral**
- ks_cancel v9 (controller_auto) dims=['welcome_message', 'voice'] engageΔ=+0.005 didΔ=+0.000 → **neutral**
- nw_renewal v8 (controller_auto) dims=['schedule'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- ks_cancel v10 (controller_auto) dims=['schedule'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- nw_renewal v9 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.035 didΔ=+0.000 → **beneficial**
- nw_kyc v3 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.019 didΔ=+0.000 → **beneficial**
- ks_cancel v11 (controller_auto) dims=['welcome_message', 'voice'] engageΔ=+0.013 didΔ=+0.000 → **beneficial**
- ks_cancel v12 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.005 → **neutral**
- ks_cancel v13 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.004 → **neutral**
- nw_kyc v4 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.077 → **beneficial**
- ks_cancel v14 (controller_auto) dims=['welcome_message', 'voice'] engageΔ=+0.013 didΔ=+0.000 → **beneficial**
- nw_renewal v10 (controller_auto) dims=['schedule'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- ks_cancel v15 (controller_auto) dims=['schedule'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- nw_renewal v11 (controller_approved) dims=['voice'] engageΔ=+0.002 didΔ=+0.000 → **neutral**
- nw_renewal v12 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.002 didΔ=+0.000 → **neutral**
- nw_renewal v13 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=-0.038 didΔ=+0.000 → **harmful**
- nw_renewal v14 (controller_auto) dims=['schedule'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- nw_kyc v5 (controller_approved) dims=['welcome_message', 'voice', 'schedule'] engageΔ=+0.015 didΔ=+0.000 → **beneficial**
- ks_cancel v16 (controller_auto) dims=['schedule'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- ks_cancel v17 (controller_auto) dims=['welcome_message', 'voice'] engageΔ=+0.005 didΔ=+0.000 → **neutral**
- nw_kyc v6 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.043 didΔ=+0.000 → **beneficial**
- ks_cancel v18 (controller_auto) dims=['welcome_message', 'voice'] engageΔ=+0.006 didΔ=+0.000 → **neutral**
- ks_cancel v19 (controller_auto) dims=['welcome_message', 'voice'] engageΔ=+0.006 didΔ=+0.000 → **neutral**
- ks_cancel v20 (controller_auto) dims=['welcome_message', 'voice'] engageΔ=-0.002 didΔ=+0.000 → **neutral**
- nw_kyc v7 (controller_approved) dims=['schedule'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- ks_cancel v21 (controller_auto) dims=['welcome_message', 'voice'] engageΔ=+0.001 didΔ=+0.000 → **neutral**

## Governance

```
{
  "approval_requests": 20,
  "decisions": {
    "approved": 18,
    "rejected": 2,
    "expired": 0
  },
  "rejection_reasons": {
    "approver declined (judgement call)": 2
  },
  "applied_via_approval": 14,
  "auto_applied": 25,
  "auto_apply_rejected": 0,
  "apply_conflicts": 0,
  "lapsed": 0,
  "peak_requests_per_24h": {
    "nw_renewal": 2,
    "nw_kyc": 2,
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
    "3": 0.492,
    "4": 0.496,
    "5": 0.693,
    "6": 0.847,
    "7": 0.848
  }
}
```

Credits used: 0 · validation runs: 0 · LLM fix calls: 0