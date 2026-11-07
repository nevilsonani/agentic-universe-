"""Generate synthetic test scenarios (our own fixtures, never read by the controller).

The public scenarios have one fixed truth each; tuning against them is overfitting.
These variants keep the public *schema and policies* but redraw the hidden truth:
seeds, variant effects (global / segment-specific / none / harmful), novelty decay,
pre-flagged caller IDs, and when/where incidents fire.

    python tools/make_scenarios.py --n 6 --out scenarios/synthetic
"""
from __future__ import annotations

import argparse
import copy
from pathlib import Path

import numpy as np
import yaml

ROOT = Path(__file__).resolve().parent.parent
FEATURES = {"lang": None, "city_tier": ["1", "2", "3"], "age_band": ["18-30", "31-45", "46-60", "60+"],
            "value_band": ["low", "mid", "high"]}


def draw_effects(rng, wf) -> list[dict]:
    out = []
    langs = list(wf["language_mix"])
    for dim in ("welcome_message", "voice"):
        default = next(iter(wf["default_config"]["allocations"]["default"][dim]))
        for arm in wf["options"][dim]:
            if arm == default:
                continue
            u = rng.random()
            if u < 0.40:
                continue                                    # no effect
            if u < 0.70:                                     # global effect, sometimes harmful
                mult = float(np.exp(rng.normal(0.0, 0.08)))
                e = {"workflow": wf["id"], "dim": dim, "value": arm, "mult": round(mult, 3)}
                if mult > 1.08 and rng.random() < 0.4:      # novelty that fades
                    e["decay"] = {"after_exposure_days": int(rng.integers(2, 4)),
                                  "to_mult": round(float(rng.uniform(0.88, 0.97)), 3), "over_days": 2}
                out.append(e)
            else:                                            # segment-specific effect
                feat = str(rng.choice(["age_band", "lang", "city_tier", "value_band"]))
                vals = langs if feat == "lang" else FEATURES[feat]
                k = int(rng.integers(1, max(2, len(vals))))
                where = {feat: sorted(rng.choice(vals, size=k, replace=False).tolist())}
                mult = float(np.exp(rng.normal(0.0, 0.14)))
                out.append({"workflow": wf["id"], "dim": dim, "value": arm, "mult": round(mult, 3), "where": where})
    return out


def variant(template: dict, k: int, rng) -> dict:
    sc = copy.deepcopy(template)
    sc["name"] = f"{template['name']}_syn{k:02d}"
    sc["seed"] = int(rng.integers(1, 10 ** 6))
    wfs = [w for t in sc["tenants"] for w in t["workflows"]]
    sc["effects"] = [e for w in wfs for e in draw_effects(rng, w)]
    for w in wfs:
        w["did_base_health"] = {}
        default_dids = set(w["default_config"]["allocations"]["default"]["caller_id"])
        spare = [d for d in w["options"]["caller_id"] if d not in default_dids]
        if spare and rng.random() < 0.35:
            w["did_base_health"][str(rng.choice(spare))] = round(float(rng.uniform(0.45, 0.7)), 2)
    if sc.get("incidents"):
        inc = []
        ids = [w["id"] for w in wfs]
        for i0 in sc["incidents"]:
            i1 = copy.deepcopy(i0)
            i1["workflow"] = str(rng.choice(ids))
            day = int(rng.integers(2, max(3, sc["days"] - 3)))
            i1["at"] = f"D{day} {int(rng.integers(9, 18)):02d}:{int(rng.choice([0, 15, 30, 45])):02d}"
            w = next(x for x in wfs if x["id"] == i1["workflow"])
            if i1["type"] == "did_flagged":
                i1["did"] = str(rng.choice(list(w["default_config"]["allocations"]["default"]["caller_id"])))
            if i1["type"] == "supervisor_edit" and "prompt_version" in i1.get("patch", {}):
                bad = i1["patch"]["prompt_version"]
                if bad not in w["options"]["prompt_version"]:
                    w["options"]["prompt_version"].append(bad)
            if i1["type"] == "supervisor_edit" and "allocations" in i1.get("patch", {}):
                arm = str(rng.choice(w["options"]["voice"]))
                i1["patch"] = {"allocations": {"default": {"voice": {arm: 1}}}}
            inc.append(i1)
        sc["incidents"] = inc
    return sc


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=6)
    ap.add_argument("--out", default="scenarios/synthetic")
    ap.add_argument("--seed", type=int, default=2026)
    a = ap.parse_args()
    rng = np.random.default_rng(a.seed)
    out = ROOT / a.out
    out.mkdir(parents=True, exist_ok=True)
    templates = [yaml.safe_load(open(ROOT / f"scenarios/{n}.yaml")) for n in ("public_01_dev", "public_02_incidents")]
    for k in range(a.n):
        sc = variant(templates[k % 2], k, rng)
        p = out / f"{sc['name']}.yaml"
        p.write_text("# SYNTHETIC test fixture (our own random truth). Never read by the controller.\n"
                     + yaml.safe_dump(sc, sort_keys=False), encoding="utf-8")
        print(p)


if __name__ == "__main__":
    main()
