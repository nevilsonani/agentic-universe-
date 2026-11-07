# Grade report — public_01_dev

## Outcomes vs baseline (no-op controller)

| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | engagement gap captured | hallucinated calls (base) |
|---|---|---|---|---|---|---|---|---|
| ks_cancel | 0.409 | +22.9% | 0.263 | +10.5% | 0.534 | +8.8% | 39% | 53 (47) |
| nw_kyc | 0.464 | +27.7% | 0.291 | +20.1% | 0.611 | +3.5% | 15% | 29 (21) |
| nw_renewal | 0.457 | +9.0% | 0.284 | +7.8% | 0.552 | +1.1% | 19% | 51 (42) |
| ALL | 0.437 | +18.6% | 0.276 | +11.3% | 0.556 | +5.0% | 26% | 133 (110) |

## Changes applied by the controller

28 applied — neutral: 13, beneficial: 15

- nw_renewal v2 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=-0.011 → **neutral**
- ks_cancel v2 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.011 → **neutral**
- ks_cancel v3 (controller_approved) dims=['cohorts', 'welcome_message', 'voice', 'schedule'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- nw_renewal v3 (controller_approved) dims=['cohorts', 'welcome_message', 'voice', 'schedule'] engageΔ=+0.022 didΔ=+0.000 → **beneficial**
- ks_cancel v4 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.030 → **beneficial**
- nw_renewal v4 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.041 → **beneficial**
- ks_cancel v5 (controller_auto) dims=['welcome_message', 'voice', 'caller_id'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- ks_cancel v6 (controller_auto) dims=['welcome_message', 'voice'] engageΔ=-0.002 didΔ=+0.000 → **neutral**
- nw_renewal v5 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.078 didΔ=+0.000 → **beneficial**
- nw_kyc v2 (controller_approved) dims=['cohorts', 'welcome_message', 'voice', 'caller_id', 'schedule'] engageΔ=+0.012 didΔ=+0.297 → **beneficial**
- ks_cancel v7 (controller_auto) dims=['welcome_message', 'voice'] engageΔ=+0.005 didΔ=+0.000 → **neutral**
- ks_cancel v8 (controller_auto) dims=['welcome_message', 'voice'] engageΔ=+0.013 didΔ=+0.000 → **beneficial**
- ks_cancel v9 (controller_auto) dims=['welcome_message', 'voice'] engageΔ=+0.008 didΔ=+0.000 → **neutral**
- nw_renewal v6 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.001 didΔ=+0.000 → **neutral**
- ks_cancel v10 (controller_auto) dims=['welcome_message'] engageΔ=+0.006 didΔ=+0.000 → **neutral**
- nw_renewal v7 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.025 → **beneficial**
- ks_cancel v11 (controller_auto) dims=['welcome_message', 'voice'] engageΔ=+0.025 didΔ=+0.000 → **beneficial**
- nw_renewal v8 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.031 didΔ=+0.000 → **beneficial**
- ks_cancel v12 (controller_auto) dims=['welcome_message', 'voice'] engageΔ=+0.024 didΔ=+0.000 → **beneficial**
- nw_kyc v3 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.024 didΔ=+0.000 → **beneficial**
- ks_cancel v13 (controller_auto) dims=['welcome_message', 'voice'] engageΔ=+0.017 didΔ=+0.000 → **beneficial**
- nw_renewal v9 (controller_approved) dims=['voice'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- nw_kyc v4 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.053 didΔ=+0.000 → **beneficial**
- nw_renewal v10 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.063 didΔ=+0.000 → **beneficial**
- nw_kyc v5 (controller_approved) dims=['welcome_message'] engageΔ=+0.018 didΔ=+0.000 → **beneficial**
- ks_cancel v14 (controller_auto) dims=['welcome_message', 'voice'] engageΔ=-0.004 didΔ=+0.000 → **neutral**
- ks_cancel v15 (controller_auto) dims=['welcome_message'] engageΔ=-0.007 didΔ=+0.000 → **neutral**
- ks_cancel v16 (controller_auto) dims=['welcome_message'] engageΔ=-0.007 didΔ=+0.000 → **neutral**

## Governance

```
{
  "approval_requests": 12,
  "decisions": {
    "approved": 11,
    "rejected": 1,
    "expired": 0
  },
  "rejection_reasons": {
    "approver declined (judgement call)": 1
  },
  "applied_via_approval": 11,
  "auto_applied": 17,
  "auto_apply_rejected": 0,
  "apply_conflicts": 0,
  "lapsed": 0,
  "peak_requests_per_24h": {
    "nw_renewal": 1,
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
    "3": 0.834,
    "4": 0.851,
    "5": 0.85,
    "6": 0.85,
    "7": 0.248
  }
}
```

Credits used: 0 · validation runs: 0 · LLM fix calls: 0