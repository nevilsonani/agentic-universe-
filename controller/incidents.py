"""Hallucination incident response (C8) and incident bookkeeping.

detect     Bernoulli CUSUM on the per-call hallucination flag, run inside the ingest
           transaction (so it is exactly-once), tuned for a 2.5× rise over the
           workflow's own baseline flag rate measured on history.
confirm    a CUSUM alarm opens an incident only after a confirmatory test: flags since
           the last prompt change (or the last 24h) must be significantly above
           baseline (Poisson p < 1e-3 on ≥100 connected calls). One call never acts.
fix        candidates in order of evidence: prompts that already ran in production
           with a normal flag rate (we know their engagement too), then LLM
           suggestions (3 credits, only if there is no evidence-backed candidate).
           Each candidate is validated (n=300, 3 credits; top-up if borderline)
           against a bar stricter than the approver's.
propose    a safety approval that says plainly whose change it reverts and why.
verify     after apply, ≥300 connected calls must show a flag rate consistent with
           baseline; otherwise the fix is marked failed and the next candidate tried.
"""
from __future__ import annotations

import json
import logging
import math

import numpy as np

from .client import ApiError, TransportError
from .features import label
from .governance import Draft
from .ingest import DAY_OFF, ChunkResult
from .planner import Facet, compose
from .stats import cusum_bernoulli, poisson_sf

log = logging.getLogger("controller.incidents")


class Incidents:
    def __init__(self, ctx):
        self.ctx = ctx

    # ------------------------------------------------------------------ CUSUM (inside ingest tx)
    def _cs(self, wf):
        return self.ctx.store.meta(f"cusum:{wf}")

    def init_baseline(self, wf: str, p0: float, prompt: str, now: int):
        s = self.ctx.s
        p0 = float(min(max(p0, 0.004), 0.2))
        self.ctx.store.set_meta(f"cusum:{wf}", {"s": 0.0, "smax": 0.0, "p0": p0, "p1": min(p0 * s.halluc_cusum_ratio, 0.5),
                                                "n": 0, "k": 0, "since": now, "prompt": prompt, "alarm": None})

    def prompt_changed(self, wf: str, now: int, prompt: str):
        cs = self._cs(wf)
        if not cs:
            return
        cs.update(s=0.0, smax=0.0, n=0, k=0, since=now, prompt=prompt, alarm=None)
        self.ctx.store.set_meta(f"cusum:{wf}", cs)

    def hook(self, res: ChunkResult):
        s = self.ctx.s
        for wf, d in res.per_wf.items():
            cs = self._cs(wf)
            if not cs or d.connected_flags is None or not len(d.connected_flags):
                continue
            s_end, s_max, i = cusum_bernoulli(d.connected_flags, cs["p0"], cs["p1"], cs["s"])
            cs["n"] += int(len(d.connected_flags))
            cs["k"] += int(d.connected_flags.sum())
            cs["smax"] = max(cs.get("smax", 0.0), s_max)
            cs["s"] = s_end
            if s_max > s.halluc_cusum_h and not cs.get("alarm"):
                cs["alarm"] = int(d.connected_minute[i])
                cs["s"] = 0.0
                self.ctx.store.decide(cs["alarm"], wf, "halluc_cusum_alarm",
                                      f"hallucination CUSUM alarm (S={s_max:.1f} > h={s.halluc_cusum_h}); "
                                      f"{cs['k']}/{cs['n']} flagged since {label(cs['since'])} vs baseline "
                                      f"{cs['p0']:.2%}", {"cusum": cs})
            self.ctx.store.set_meta(f"cusum:{wf}", cs)

    # ------------------------------------------------------------------ incident rows
    def open_incident(self, wf: str, kind: str) -> dict | None:
        r = self.ctx.store.db.execute("SELECT id, status, detail, opened_minute FROM incidents WHERE wf=? AND type=? "
                                      "AND status NOT IN ('resolved','false_alarm')", (wf, kind)).fetchone()
        if not r:
            return None
        return {"id": r[0], "status": r[1], "detail": json.loads(r[2] or "{}"), "opened": r[3]}

    def create(self, wf: str, kind: str, now: int, detail: dict, status: str = "detected") -> str:
        n = (self.ctx.store.q1("SELECT COUNT(*) FROM incidents WHERE wf=?", (wf,)) or 0) + 1
        iid = f"inc.{wf}.{kind}.{n:02d}"
        self.ctx.store.db.execute("INSERT INTO incidents(id, wf, type, status, opened_minute, detail) VALUES(?,?,?,?,?,?)",
                                  (iid, wf, kind, status, now, json.dumps(detail, default=str)))
        return iid

    def update(self, iid: str, now: int, status: str, detail: dict, msg: str, **cols):
        sets = ", ".join(["status=?", "detail=?"] + [f"{k}=?" for k in cols])
        with self.ctx.store.tx():
            self.ctx.store.db.execute(f"UPDATE incidents SET {sets} WHERE id=?",
                                      (status, json.dumps(detail, default=str), *cols.values(), iid))
            wf = self.ctx.store.q1("SELECT wf FROM incidents WHERE id=?", (iid,))
            self.ctx.store.decide(now, wf, f"incident_{status}", f"{iid}: {msg}", detail)

    # ------------------------------------------------------------------ main step
    def step(self, now: int):
        for wf in self.ctx.reg.wfs:
            cs = self._cs(wf)
            if cs and cs.get("alarm") and not self.open_incident(wf, "hallucination"):
                self._maybe_open(wf, cs, now)
            inc = self.open_incident(wf, "hallucination")
            if inc:
                try:
                    self._drive(wf, inc, now)
                except (ApiError, TransportError) as e:
                    log.warning("incident %s step failed: %s", inc["id"], e)

    def _flag_stats_since(self, wf: str, since: int, prompt: str | None = None) -> tuple[float, float]:
        hq = self.ctx.ing.t(wf)["hq"]
        if hq.n == 0:
            return 0.0, 0.0
        ah = (hq.col("day") - DAY_OFF) * 24 + hq.col("hour")
        m = ah >= since // 60 + (1 if since % 60 else 0)
        if prompt is not None:
            m &= hq.col("prompt") == self.ctx.ing.prompt_code(prompt)
        return float(hq.col("conn")[m].sum()), float(hq.col("flag")[m].sum())

    def _maybe_open(self, wf: str, cs: dict, now: int):
        n, k = cs["n"], cs["k"]
        p0 = cs["p0"]
        p = poisson_sf(int(k), n * p0) if n else 1.0
        cur = self.ctx.reg.config(wf)["prompt_version"]
        if n >= 100 and p < 1e-3:
            with self.ctx.store.tx():
                iid = self.create(wf, "hallucination", now, {
                    "prompt": cur, "baseline_rate": p0, "since": cs["since"], "since_label": label(cs["since"]),
                    "connected_since": n, "flagged_since": k, "rate_since": k / n, "p_value": p,
                    "alarm_minute": cs["alarm"], "tried": [], "attempts": 0})
                self.ctx.store.alert(now, "page", wf, "hallucination",
                                     f"{wf}: hallucination flags {k}/{n} ({k / n:.1%}) on {cur} since "
                                     f"{label(cs['since'])} vs baseline {p0:.1%} (p={p:.1e}). Controller is "
                                     f"validating a fix; check incident {iid} in the supervisor view.")
                self.ctx.store.decide(now, wf, "incident_detected", f"{iid}: hallucination spike on {cur} "
                                      f"({k / n:.1%} vs {p0:.1%}, p={p:.1e})", {"incident": iid})
        elif now - cs["alarm"] > 6 * 60 and n >= 300:
            with self.ctx.store.tx():
                self.ctx.store.decide(now, wf, "halluc_false_alarm",
                                      f"CUSUM alarm at {label(cs['alarm'])} not confirmed ({k}/{n}, p={p:.2f}); "
                                      f"no action", {"cusum": cs})
                cs["alarm"] = None
                self.ctx.store.set_meta(f"cusum:{wf}", cs)

    def _known_good(self, wf: str, exclude: set[str]) -> list[tuple[str, float, float]]:
        hq = self.ctx.ing.t(wf)["hq"]
        cs = self._cs(wf)
        out = []
        for name, code in self.ctx.ing.prompt_codes.items():
            if name in exclude or name not in self.ctx.reg.wfs[wf].options["prompt_version"]:
                continue
            m = hq.col("prompt") == code
            n, k = float(hq.col("conn")[m].sum()), float(hq.col("flag")[m].sum())
            if n >= 500 and poisson_sf(int(k), n * cs["p0"] * 1.3) > 0.05:
                eng = float(hq.col("eng")[m].sum()) / n
                out.append((name, n, k / n, eng))
        return sorted(out, key=lambda x: -x[1])

    def _credits(self) -> float:
        try:
            return float(self.ctx.client.state()["credits_remaining"])
        except (ApiError, TransportError):
            return 0.0

    def _spend_ok(self, cost: float) -> bool:
        return self._credits() >= cost

    def _drive(self, wf: str, inc: dict, now: int):
        ctx, s = self.ctx, self.ctx.s
        d = inc["detail"]
        iid, st = inc["id"], inc["status"]
        cfg = ctx.reg.config(wf)
        cur_prompt = cfg["prompt_version"]
        if st in ("detected", "validated", "proposed") and cur_prompt != d["prompt"]:
            # someone (e.g. the supervisor) already changed the prompt: go straight to verification
            d.update(applied_minute=now, fixed_prompt=cur_prompt, fixed_by="config change outside this incident")
            self.prompt_changed(wf, now, cur_prompt)
            self.update(iid, now, "verifying", d, f"prompt is now {cur_prompt}; verifying flag rate")
            return
        if st == "detected":
            self._choose_fix(wf, inc, now)
        elif st == "validated":
            self._propose(wf, inc, now)
        elif st == "proposed":
            row = ctx.gov.get(d["proposal_id"])
            if row and row["status"] == "applied":
                d.update(applied_minute=row["applied_minute"], fixed_prompt=d["candidate"], fixed_by="approval")
                self.prompt_changed(wf, now, d["candidate"])
                self.update(iid, now, "verifying", d, f"{d['candidate']} live (v{row['applied_version']}); verifying")
            elif row and row["status"] in ("rejected", "expired", "stale", "failed", "lapsed", "cancelled", "superseded"):
                d["attempts"] = d.get("attempts", 0) + 1
                d.setdefault("history", []).append({"proposal_id": d["proposal_id"], "status": row["status"],
                                                    "reasons": row.get("reasons")})
                self.update(iid, now, "validated", d, f"{d['proposal_id']} {row['status']}; re-proposing with fresh data")
        elif st == "verifying":
            n, k = self._flag_stats_since(wf, d["applied_minute"])
            p0 = self._cs(wf)["p0"]
            if n >= s.halluc_verify_calls:
                p_high = poisson_sf(int(k), n * p0 * 1.5)
                if p_high > 0.01:
                    d.update(verify={"connected": n, "flagged": k, "rate": k / n, "baseline": p0})
                    with ctx.store.tx():
                        ctx.store.resolve_alert(now, wf, "hallucination")
                    self.update(iid, now, "resolved", d, f"verified: {k:.0f}/{n:.0f} flagged ({k / n:.2%}) after the "
                                f"fix vs baseline {p0:.2%}", resolved_minute=d["applied_minute"], verified_minute=now)
                else:
                    d.setdefault("tried", []).append(d.get("fixed_prompt"))
                    d.update(verify_failed={"connected": n, "flagged": k, "rate": k / n})
                    d["prompt"] = cur_prompt
                    self.update(iid, now, "detected", d, f"fix did not hold ({k / n:.2%} flagged); trying next candidate")

    def _choose_fix(self, wf: str, inc: dict, now: int):
        ctx, s = self.ctx, self.ctx.s
        d = inc["detail"]
        w = ctx.reg.wfs[wf]
        gov_min = float(w.gov.get("min_validation_pass_rate", 0.97))
        bar = max(gov_min, 1 - s.safety_max_fail_rate)
        exclude = {d["prompt"], *[x for x in d.get("tried", []) if x]}
        cands = [(p, "production evidence: %d connected calls, %.2f%% flagged, %.1f%% engaged" % (n, 100 * r, 100 * e))
                 for p, n, r, e in self._known_good(wf, exclude)]
        if not cands and not d.get("llm"):
            if self._spend_ok(3):
                try:
                    r = ctx.client.suggest_fixes(wf, f"hallucination flags at {d['rate_since']:.1%} on prompt "
                                                     f"{d['prompt']} vs {d['baseline_rate']:.1%} baseline")
                    with ctx.store.tx():
                        ctx.store.db.execute("INSERT INTO credits(minute, wf, tenant, kind, cost, ref) VALUES(?,?,?,?,?,?)",
                                             (now, wf, w.tenant, "llm_suggest_fixes", 3, inc["id"]))
                    d["llm"] = r["suggestions"]
                except ApiError as e:
                    d["llm"] = []
                    d["llm_error"] = str(e)
        if not cands:
            cands = [(x["prompt_version"], f"LLM suggestion: {x['summary']}") for x in d.get("llm") or []
                     if x["prompt_version"] not in exclude]
        if not cands:
            cands = [(p, "catalog") for p in w.options["prompt_version"] if p not in exclude]
        for p, why in cands:
            runs = d.setdefault("validations", {}).setdefault(p, [])
            best = max(runs, key=lambda r: r["n"], default=None)
            if best is None or (gov_min <= best["pass_rate"] < bar and best["n"] < s.validation_topup_n):
                n = s.validation_n if best is None else s.validation_topup_n
                cost = math.ceil(n / 100)
                if not self._spend_ok(cost):
                    self.update(inc["id"], now, "detected", d, f"not enough credits to validate {p}")
                    with ctx.store.tx():
                        ctx.store.alert(now, "page", wf, "credits", "credits exhausted during an incident")
                    return
                try:
                    v = ctx.client.validate(wf, p, n)
                except ApiError as e:
                    d.setdefault("errors", []).append(str(e))
                    continue
                with ctx.store.tx():
                    ctx.store.db.execute("INSERT OR IGNORE INTO validations VALUES(?,?,?,?,?,?,?,?)",
                                         (v["validation_run_id"], wf, p, v["n"], v["failures"], v["pass_rate"], now,
                                          inc["id"]))
                    ctx.store.db.execute("INSERT INTO credits(minute, wf, tenant, kind, cost, ref) VALUES(?,?,?,?,?,?)",
                                         (now, wf, w.tenant, "validation", cost, v["validation_run_id"]))
                    ctx.store.decide(now, wf, "validation_run", f"{v['validation_run_id']}: {p} pass {v['pass_rate']:.3f} "
                                     f"on n={v['n']} (bar {bar:.3f})", v)
                runs.append(v)
                best = max(runs, key=lambda r: r["n"])
                if gov_min <= best["pass_rate"] < bar and best["n"] < s.validation_topup_n:
                    # borderline: one larger run to separate "fine" from "4x worse"
                    return self._choose_fix(wf, {**inc, "detail": d}, now)
            if best["pass_rate"] >= bar:
                d.update(candidate=p, candidate_why=why, validation_run_id=best["validation_run_id"],
                         validation=best)
                self.update(inc["id"], now, "validated", d, f"fix candidate {p} ({why}); validated "
                            f"{best['pass_rate']:.3f} on n={best['n']}")
                return
        self.update(inc["id"], now, "no_candidate", d, "no candidate prompt passed validation")
        with ctx.store.tx():
            ctx.store.alert(now, "page", wf, "halluc_no_fix", f"{wf}: no prompt passed validation; human needed")

    def _propose(self, wf: str, inc: dict, now: int):
        ctx = self.ctx
        d = inc["detail"]
        if d.get("attempts", 0) >= 4:
            return
        ok, why = ctx.gov.send_window(wf, now, "safety")
        if not ok:
            return
        cfg = ctx.reg.config(wf)
        ver = ctx.reg.current_version[wf]
        who = ctx.reg.version_meta.get(wf, {}).get(ver, {}).get("actor", "?")
        f = Facet("prompt", {"prompt_version"}, "safety",
                  f"switch prompt {d['prompt']} → {d['candidate']}: hallucination flags {d['rate_since']:.1%} vs "
                  f"{d['baseline_rate']:.1%} baseline since {d['since_label']}",
                  "", {"prompt_version": d["candidate"]})
        new = compose(cfg, [f])
        n_conn, n_flag = d["connected_since"], d["flagged_since"]
        rationale = (f"Since {d['since_label']} ({'set by ' + who if who else ''}) {n_flag}/{n_conn} connected calls on "
                     f"{d['prompt']} were flagged ({d['rate_since']:.2%}) against this workflow's baseline of "
                     f"{d['baseline_rate']:.2%} (Poisson p={d['p_value']:.1e}); each hallucinated call halves the "
                     f"chance of a conclusion and is a compliance event. {d['candidate']} chosen because: "
                     f"{d['candidate_why']}. Validation {d['validation_run_id']}: pass {d['validation']['pass_rate']:.3f} "
                     f"on n={d['validation']['n']}. "
                     + (f"This reverses the {who} change made at {d['since_label']} — we are asking, not overriding."
                        if who == "supervisor" else ""))
        sev = min(1.0, (d["rate_since"] / max(d["baseline_rate"], 1e-6) - 1) / 4)
        from .dryrun import affected_leads, build_diff
        aff, aff_d = affected_leads(ctx, wf, cfg, new, now)
        draft = Draft(wf=wf, kind="incident", urgency="safety", channel="approval", base_version=ver,
                      proposed_config=new, change_summary=f.summary, diff=build_diff(cfg, new),
                      affected_leads=aff,
                      dry_run={"validation": d["validation"], "affected": aff_d,
                               "expected": f"flag rate back to ≈{d['baseline_rate']:.1%}; verified on the next "
                                           f"{ctx.s.halluc_verify_calls} connected calls",
                               "candidate_evidence": d["candidate_why"]},
                      impact_score=0.8 + 0.2 * sev, rationale=rationale,
                      evidence={"validation_run_id": d["validation_run_id"], "incident": inc["id"],
                                "flag_stats": {"connected": n_conn, "flagged": n_flag, "baseline": d["baseline_rate"]}},
                      facets=[f])
        row = ctx.gov.submit(draft, now)
        d["proposal_id"] = row["proposal_id"]
        if row["status"] in ("pending", "applied"):
            self.update(inc["id"], now, "proposed", d, f"approval {row.get('approval_id')} requested "
                        f"({row['proposal_id']})")
        else:
            d["attempts"] = d.get("attempts", 0) + 1
            self.update(inc["id"], now, "validated", d, f"submit {row['status']}; will retry")
