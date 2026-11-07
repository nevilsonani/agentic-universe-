"""The control loop. One iteration per 15 simulated minutes:

    ingest new chunks (exactly once) → apply control events (config changes, approval
    decisions) → bootstrap once from history → poll approvals and apply what is
    approved → drive open incidents → caller-ID guard (every tick) → plan (hourly)
    and route each facet through auto-apply or an approval → advance the clock.

Restart = construct a new Controller on the same store and call run(): it restores
aggregates, reconciles in-flight proposals against the platform, and carries on.
"""
from __future__ import annotations

import copy
import json
import logging
import math
import os
import time

import numpy as np

from .analysis import PickupTimeModel, kpis
from .client import ApiError, SimClient, TransportError
from .dryrun import affected_leads, build_diff, did_index, impact_score, ips_engagement
from .features import FeatureSpace, config_cohorts, label, ts_to_minute, Cohort
from .governance import Draft, Governance
from .incidents import Incidents
from .ingest import DAY_OFF, Ingestor
from .planner import Facet, Planner, compose, limit_shift
from .registry import Registry, changed_dims, dist, norm, tv
from .settings import CUSTOMER_FACING, MIN_PER_DAY, VARIANT_DIMS, Settings, load_tenant_prefs
from .store import Store

log = logging.getLogger("controller")
URG = {"routine": 0, "protective": 1, "safety": 2}


class Controller:
    def __init__(self, url: str, db_path: str, settings: Settings | None = None, prefs=None,
                 store_calls: bool = True, view_path: str | None = None, max_ticks: int | None = None):
        self.s = settings or Settings()
        self.prefs = prefs if prefs is not None else load_tenant_prefs()
        self.store = Store(db_path)
        self.client = SimClient(url)
        self.reg = Registry(self.store)
        self.fs = FeatureSpace(self.store)
        self.ing = Ingestor(self.store, self.reg, self.fs, store_calls=store_calls)
        self.planner = Planner(self.s, self.fs, self.ing, self.reg)
        self.gov = Governance(self.store, self.client, self.reg, self.s, self.prefs)
        self.inc = Incidents(self)
        self.ing.hooks.append(self.inc.hook)
        self.gov.on_applied.append(self._on_applied)
        self.gov.still_ok = self._still_ok
        self.view_path = view_path
        self.max_ticks = max_ticks
        self._last_plan = None
        self._t_day = time.time()
        self._wall = {}

    # ================================================================== run
    def run(self) -> dict:
        self._wait_for_sim()
        self._startup()
        ticks = 0
        recovered = False
        while True:
            st = self.client.state()
            now = st["sim_minute"]
            self._ingest()
            self._handle_ctl(now)
            if self.store.meta("bootstrapped") is None:
                self._bootstrap(now)
            if not recovered:
                self.gov.recover(now)
                recovered = True
            self.gov.poll(now)
            self._handle_ctl(now)
            self.inc.step(now)
            self._guard(now)
            if self._last_plan is None or now - self._last_plan >= self.s.plan_every_min:
                self._plan(now)
                self._last_plan = now
            self.gov.flush_notices(now, self._notice_still_wanted)
            self._observe(now, st)
            if st["finished"]:
                break
            if self.max_ticks is not None and ticks >= self.max_ticks:
                log.info("max_ticks reached; stopping without finishing (test mode)")
                return {"finished": False, "sim_minute": now}
            self.client.advance(self.s.step_minutes, now)
            ticks += 1
        self._finish()
        return {"finished": True, "sim_minute": now}

    def _wait_for_sim(self, timeout_s: float = 600):
        t0 = time.time()
        while not self.client.health():
            if time.time() - t0 > timeout_s:
                raise SystemExit(f"simulator at {self.client.base} not reachable")
            time.sleep(1)

    # ================================================================== startup / recovery
    def _startup(self):
        first = self.store.meta("cursor") is None
        with self.store.tx():
            for w in self.client.workflows():
                self.reg.register(w)
            if first:
                self.store.set_meta("feature_names", self.fs.names)
                self.store.set_meta("started_wall", time.time())
        for wf in self.reg.wfs:
            cur = self.client.config(wf)
            with self.store.tx():
                for h in self.client.config_history(wf):
                    self.reg.record_version(wf, h["version"], h["actor"],
                                            ts_to_minute(h["applied_at"]) if h.get("applied_at") else None,
                                            h.get("changed_dims"), h.get("proposal_id"), h.get("approval_id"))
                self.reg.record_version(wf, cur["version"], cur["actor"],
                                        ts_to_minute(cur["applied_at"]) if cur.get("applied_at") else None,
                                        config=cur["config"])
        if not first:
            log.info("restart detected: restoring state from %s", self.store.path)
            self.ing.restore()
            with self.store.tx():
                self.store.decide(self.client.state()["sim_minute"], None, "controller_restarted",
                                  f"restarted; resumed after chunk {self.store.meta('cursor')}",
                                  {"cursor": self.store.meta("cursor"),
                                   "open_proposals": self.store.q1("SELECT COUNT(*) FROM proposals WHERE status IN "
                                                                   "('notice','sending','pending','approved','applying')")})

    def _ingest(self):
        cur = self.store.meta("cursor", -1)
        chunks = self.client.chunks("history", after=cur) + self.client.chunks("events", after=cur)
        for ch in sorted(chunks, key=lambda c: c["chunk"]):
            if ch["chunk"] <= self.store.meta("cursor", -1):
                continue
            data = self.client.chunk_bytes(ch["chunk"])
            res = self.ing.ingest(ch["chunk"], ch["kind"], data)
            log.debug("chunk %d (%s): %d events in %.2fs", res.chunk, res.kind, res.events, res.seconds)

    # ================================================================== control events
    def _handle_ctl(self, now: int):
        rows = self.store.q("SELECT eid, minute, wf, type, body FROM ctl_events WHERE handled=0 ORDER BY eid")
        for eid, minute, wf, typ, body in rows:
            b = json.loads(body)
            with self.store.tx():
                if typ == "config_changed" and wf in self.reg.wfs:
                    self._config_changed(wf, minute, b, now)
                self.store.db.execute("UPDATE ctl_events SET handled=1 WHERE eid=?", (eid,))

    def _config_changed(self, wf: str, minute: int, b: dict, now: int):
        v, actor = b["version"], b["actor"]
        prev_v = max([x for x in self.reg.configs.get(wf, {}) if x < v], default=None)
        prev = self.reg.config(wf, prev_v) if prev_v else None
        cfg = None
        if self.reg.config(wf, v) is None:
            cur = self.client.config(wf)
            if cur["version"] == v:
                cfg = cur["config"]
        self.reg.record_version(wf, v, actor, minute, b.get("changed_dims"), b.get("proposal_id"),
                                b.get("approval_id"), config=cfg)
        new = self.reg.config(wf, v)
        dims = b.get("changed_dims") or []
        if "prompt_version" in dims and new:
            self.inc.prompt_changed(wf, minute, new["prompt_version"])
        if actor != "supervisor":
            return
        st = self.store.wf_state(wf)
        fr = st.setdefault("freeze", {})
        for d in dims:
            fr[d] = minute + MIN_PER_DAY
        shadow = None
        if new and prev:
            shadow = self._shadow_check(wf, prev, new, dims, now)
        st.setdefault("supervisor_edits", []).append({"minute": minute, "version": v, "dims": dims,
                                                      "diff": build_diff(prev, new) if (prev and new) else None,
                                                      "shadow": shadow})
        self.store.save_wf_state(wf, st)
        msg = (f"supervisor edited {', '.join(dims)} (v{v}); we will not change {', '.join(dims)} on this workflow "
               f"for 24h except to fix a safety incident, and only through an approval that says so")
        if shadow:
            msg += f". Note: {shadow['message']}"
        self.store.decide(minute, wf, "supervisor_edit", msg, {"version": v, "dims": dims, "shadow": shadow,
                                                               "diff": build_diff(prev, new) if (prev and new) else None})
        self.gov.supersede_open(wf, now, f"supervisor edit v{v}")

    def _shadow_check(self, wf, prev, new, dims, now):
        """Did the supervisor edit `default` for a dim that every active lead overrides via its cohort?"""
        act = self.planner.active_leads(self.reg.wfs[wf], now)
        if not len(act):
            return None
        from .features import assign
        tups = self.ing.li(wf).tup[act]
        names = assign(new, self.fs, tups)
        for d in dims:
            if d not in ("welcome_message", "voice", "caller_id"):
                continue
            if norm(dist(prev, "default", d)) == norm(dist(new, "default", d)):
                continue
            own = np.array([d in (new["allocations"].get(n) or {}) for n in names])
            share = float(own.mean())
            if share > 0.5:
                return {"dim": d, "overridden_share": round(share, 3),
                        "message": f"{share:.0%} of active leads are in cohorts with their own {d} mix, so the new "
                                   f"default reaches only {1 - share:.0%}. Shown in the supervisor view; we did not "
                                   f"change anything to 'fix' it."}
        return None

    # ================================================================== bootstrap (C3)
    def _bootstrap(self, now: int):
        for wf, w in self.reg.wfs.items():
            cfg = self.reg.config(wf)
            tabs = self.ing.t(wf)
            pickup = PickupTimeModel(self.s, self.fs, tabs["ptime"], w.window)
            k = kpis(tabs["ptime"], tabs["eng"], -10 ** 6, now // MIN_PER_DAY + 1)
            hq = tabs["hq"]
            conn, flags = float(hq.col("conn").sum()), float(hq.col("flag").sum())
            p0 = flags / conn if conn else 0.015
            st = self.store.wf_state(wf)
            st["ref"] = {d: max(norm(dist(cfg, "default", d)).items(), key=lambda kv: kv[1])[0] for d in VARIANT_DIMS}
            st["baseline"] = {"pickup_rate": k["pickup_rate"], "engagement_rate": k["engagement_rate"],
                              "flag_rate": p0, "dials": k["dials"], "prompt": cfg["prompt_version"],
                              "history_days": len(set((tabs["ptime"].col("day") - DAY_OFF).tolist()))}
            pd = tabs["pdid"]
            used = {int(x) for x in np.unique(pd.col("did"))} if pd.n else set()
            st["did"] = {d: {"state": "healthy" if i in used else "untested", "since": now,
                             "reason": "carried traffic in history" if i in used else "no history: canary first"}
                         for i, d in enumerate(w.options["caller_id"])}
            trans = self.planner.did_update(w, st, now, short=False)
            st["design"] = self.planner.design(w, now, pickup)
            st["pickup_model"] = {"tests": pickup.tests, "features_used": pickup.features_used()}
            st["bootstrapped"] = now
            with self.store.tx():
                self.inc.init_baseline(wf, p0, cfg["prompt_version"], now)
                self.store.save_wf_state(wf, st)
                self.store.decide(now, wf, "bootstrap",
                                  f"learned from {st['baseline']['history_days']} days of history: pickup "
                                  f"{k['pickup_rate']:.3f}, engagement {k['engagement_rate']:.3f}, flag rate {p0:.2%}; "
                                  f"call-time driven by {', '.join(pickup.features_used()) or 'nothing detectable'}; "
                                  f"{len(st['design']['cohorts'])} cohorts designed "
                                  f"({st['design']['primary']} × {st['design']['secondary']})",
                                  {"baseline": st["baseline"], "design": st["design"], "pickup_tests": pickup.tests,
                                   "did": st["did"], "did_transitions": trans})
        with self.store.tx():
            self.store.set_meta("bootstrapped", now)

    # ================================================================== caller-ID guard (every tick)
    def _guard(self, now: int):
        for wf, w in self.reg.wfs.items():
            st = self.store.wf_state(wf)
            if "did" not in st:
                continue
            cfg = self.reg.config(wf)
            trans = self.planner.did_update(w, st, now, short=True)
            cur = norm(dist(cfg, "default", "caller_id"))
            hits = [t for t in trans if t["to"] == "quarantined" and cur.get(t["did"], 0) > 0]
            # severity = share of the workflow's pickup being lost right now. Only a real dent (≥3%) is an
            # incident that may jump the queue (and make an in-flight approval stale); a failing canary is not.
            severity = sum(cur.get(t["did"], 0) * max(0.0, 1 - t["ratio"]) for t in hits)
            with self.store.tx():
                for t in trans:
                    self.store.decide(now, wf, "did_state", f"{t['did']}: {t['from']} → {t['to']} "
                                      f"({st['did'][t['did']]['reason']})", {**t, "severity": severity})
                if hits and severity >= 0.03 and not self.inc.open_incident(wf, "did_flagged"):
                    iid = self.inc.create(wf, "did_flagged", now, {"dids": [t["did"] for t in hits], "evidence": hits,
                                                                   "severity": severity})
                    st["did_incident_open"] = iid
                    self.store.alert(now, "page" if not w.auto_dims() & {"caller_id"} else "ticket", wf, "did_flagged",
                                     f"{wf}: caller ID {', '.join(t['did'] for t in hits)} pickup collapsed "
                                     f"(≤{hits[0]['hi']:.2f}× peers). Rotating it out"
                                     + (" via auto-apply." if "caller_id" in w.auto_dims() else "; approval requested."))
                self.store.save_wf_state(wf, st)
            if st.get("did_incident_open"):
                bad = [d for d, x in st["did"].items() if x["state"] == "quarantined"]
                if all(norm(dist(self.reg.config(wf), "default", "caller_id")).get(b, 0) == 0 for b in bad):
                    with self.store.tx():
                        iid = st.pop("did_incident_open")
                        self.store.db.execute("UPDATE incidents SET status='resolved', resolved_minute=? WHERE id=?",
                                              (now, iid))
                        self.store.decide(now, wf, "incident_resolved", f"{iid}: no traffic left on {', '.join(bad)}")
                        self.store.resolve_alert(now, wf, "did_flagged")
                        self.store.save_wf_state(wf, st)
                else:
                    f = self.planner.did_facet(w, st, now, self.reg.config(wf))
                    if f:
                        f.urgency = "safety"
                        self._route(w, now, [f], st)

    # ================================================================== planning
    def _plan(self, now: int):
        for wf, w in self.reg.wfs.items():
            st = self.store.wf_state(wf)
            if "did" not in st:
                continue
            t0 = time.time()
            cfg = self.reg.config(wf)
            frozen = {d for d, until in (st.get("freeze") or {}).items() if until > now}
            facets = []
            trans = self.planner.did_update(w, st, now, short=False)
            with self.store.tx():
                for t in trans:
                    self.store.decide(now, wf, "did_state", f"{t['did']}: {t['from']} → {t['to']} "
                                      f"({st['did'][t['did']]['reason']})", t)
            if "caller_id" not in frozen:
                f = self.planner.did_facet(w, st, now, cfg)
                if f:
                    facets.append(f)
            busy = self.gov.has_open_approval(wf, routine_only=True) or any(
                r["status"] == "notice" for r in self.gov.open_rows(wf, "auto"))
            snap = None
            if not busy and self._could_act(w, now):
                deployed = config_cohorts(cfg)
                tabs = self.ing.t(wf)
                if not deployed and st.get("design") and "cohorts" not in frozen:
                    cohorts = [Cohort(c["id"], c["where"]) for c in st["design"]["cohorts"]]
                    pickup = PickupTimeModel(self.s, self.fs, tabs["ptime"], w.window)
                    facets.append(Facet("cohorts", {"cohorts"}, "routine",
                                        f"define {len(cohorts)} cohorts ({st['design']['primary']} × "
                                        f"{st['design']['secondary']})",
                                        f"One shared cohort set for timing, experiments and reporting: "
                                        f"{st['design']['why']['primary']}; {st['design']['why']['secondary']}. "
                                        f"Cells under 3% of leads are merged, so every cohort is big enough to act on.",
                                        {"cohorts": [c.to_config() for c in cohorts]},
                                        evidence={"design": st["design"]}))
                    sf = self.planner.schedule_facet(w, now, cohorts, cfg, pickup) if "schedule" not in frozen else None
                    if sf:
                        facets.append(sf)
                        st["schedule"] = {"minute": now, "gain": sf.predicted["pickup_rel_gain"]}
                    vf, snap = self.planner.variant_facet(w, st, now, cfg, cohorts, frozen, bootstrap=True)
                    if vf:
                        facets.append(vf)
                else:
                    cohorts = deployed
                    sched = st.get("schedule") or {}
                    if "schedule" not in frozen and cohorts and now - sched.get("minute", -10 ** 9) >= \
                            self.s.schedule_refresh_days * MIN_PER_DAY:
                        pickup = PickupTimeModel(self.s, self.fs, tabs["ptime"], w.window)
                        sf = self.planner.schedule_facet(w, now, cohorts, cfg, pickup)
                        if sf:
                            facets.append(sf)
                        st["schedule"] = {"minute": now, "gain": sf.predicted["pickup_rel_gain"] if sf else 0.0}
                    vf, snap = self.planner.variant_facet(w, st, now, cfg, cohorts, frozen)
                    if vf:
                        facets.append(vf)
            if snap:
                st["snapshot"] = snap
                st["snapshot_minute"] = now
            with self.store.tx():
                self.store.save_wf_state(wf, st)
            if facets:
                self._route(w, now, facets, st)
            self._wall.setdefault("plan_s", 0.0)
            self._wall["plan_s"] += time.time() - t0

    def _could_act(self, w, now) -> bool:
        if self.gov.send_window(w.id, now, "routine")[0]:
            return True
        ad = w.auto_dims()
        return bool(ad & {"welcome_message", "voice"}) and self.gov.pref(w.id).customer_facing_auto_apply and \
            self.gov.auto_ok(w.id, now, "routine", customer_facing=True)[0] and (now % MIN_PER_DAY) // 60 in range(*w.gov["approver_hours"])

    # ================================================================== routing
    def _route(self, w, now: int, facets: list[Facet], st: dict):
        wf = w.id
        cfg = self.reg.config(wf)
        auto_dims = w.auto_dims()
        pref = self.gov.pref(wf)
        deployed = {c["id"] for c in cfg.get("cohorts") or []} | {"default"}
        auto_f, appr_f = [], []
        for f in facets:
            if f.name in ("prompt", "cohorts"):
                appr_f.append(f)
            elif f.name == "schedule":
                (auto_f if "schedule" in auto_dims and set(f.data["hour_weights"]) <= deployed else appr_f).append(f)
            elif f.name == "did":
                (auto_f if "caller_id" in auto_dims else appr_f).append(f)
            elif f.name == "variants":
                ok = f.dims <= auto_dims and pref.customer_facing_auto_apply and set(f.data["alloc"]) <= deployed
                (auto_f if ok else appr_f).append(f)
        if any(f.name == "cohorts" for f in appr_f):
            appr_f += [f for f in auto_f if f.name in ("schedule", "variants")]
            auto_f = [f for f in auto_f if f.name not in ("schedule", "variants")]
        # ---------------- auto-apply: urgent first, then routine
        for group in ([f for f in auto_f if f.urgency != "routine"], [f for f in auto_f if f.urgency == "routine"]):
            if not group:
                continue
            urg = max((f.urgency for f in group), key=URG.get)
            cf = any(f.name == "variants" for f in group)
            ok, why = self.gov.auto_ok(wf, now, urg, customer_facing=cf)
            if not ok:
                continue
            cfg = self.reg.config(wf)
            new = limit_shift(cfg, compose(cfg, group), w.max_shift)
            dims = changed_dims(cfg, new)
            if not dims:
                continue
            if not set(dims) <= auto_dims:
                appr_f += group
                continue
            notice = pref.auto_apply_notice_minutes if (set(dims) & set(CUSTOMER_FACING) and urg == "routine") else 0
            d = self._draft(w, now, cfg, new, group, "auto", urg, "auto", notice=notice)
            self.gov.submit(d, now)
        # ---------------- approval bundle
        if not appr_f:
            return
        urg = max((f.urgency for f in appr_f), key=URG.get)
        if urg != "safety" and self.gov.has_open_approval(wf):
            return
        if urg == "safety" and any((r["note"] or "").startswith("safety") and
                                   {x["name"] for x in r["intents"]} == {f.name for f in appr_f}
                                   for r in self.gov.open_rows(wf, "approval")):
            return
        if any(r["status"] == "notice" for r in self.gov.open_rows(wf, "auto")):
            return
        ok, why = self.gov.send_window(wf, now, urg)
        if not ok:
            return
        cfg = self.reg.config(wf)
        new = compose(cfg, appr_f)
        if not changed_dims(cfg, new):
            return
        kind = "bootstrap" if any(f.name == "cohorts" for f in appr_f) else ("safety" if urg == "safety" else "change")
        d = self._draft(w, now, cfg, new, appr_f, "approval", urg, kind)
        self.gov.submit(d, now)

    def _draft(self, w, now, cfg, new, facets, channel, urgency, kind, notice=0) -> Draft:
        wf = w.id
        st = self.store.wf_state(wf)
        names = {f.name for f in facets}
        aff, aff_d = affected_leads(self, wf, cfg, new, now)
        pred = {}
        dry = {"affected": aff_d}
        if "variants" in names:
            vf = next(f for f in facets if f.name == "variants")
            scale = self._partial(cfg, new, vf)
            pred["engagement_rel"] = vf.predicted["eng_rel_mean"] * scale
            dry["engagement_model"] = {k: v * scale if isinstance(v, float) else v for k, v in vf.predicted.items()}
            dry["engagement_replay"] = ips_engagement(self, wf, new, cfg, now)
        if "schedule" in names:
            sf = next(f for f in facets if f.name == "schedule")
            pred["pickup_rel"] = pred.get("pickup_rel", 0) + sf.predicted["pickup_rel_gain"]
            dry["schedule"] = sf.predicted["dry_run"]
        if "did" in names:
            a, b = did_index(st, cfg), did_index(st, new)
            dry["caller_id"] = {"from": a, "to": b}
            if a["pickup_index_known_part"] and b["pickup_index_known_part"]:
                pred["pickup_rel"] = pred.get("pickup_rel", 0) + b["pickup_index_known_part"] / a["pickup_index_known_part"] - 1
        if "prompt" in names:
            dry["prompt"] = "see incident evidence"
        g_kpi, g_max = w.guardrail
        dry["guardrail"] = {"kpi": g_kpi, "max_drop": g_max,
                            "note": ("welcome/voice are heard after pickup, so they cannot move pickup; caller-ID and "
                                     "timing changes are expected to raise it" if g_kpi == "pickup_rate" else
                                     "caller-ID/timing changes do not change what a connected caller hears; "
                                     "engagement monitored per arm against the holdout")}
        dry["predicted"] = pred
        summary = "; ".join(f.summary for f in facets)
        if len(summary) > 480:
            summary = summary[:477] + "…"
        rationale = " ".join(f"[{f.name}] {f.rationale}" for f in facets)
        evidence = {f.name: _compact(f.evidence) for f in facets}
        return Draft(wf=wf, kind=kind, urgency=urgency, channel=channel, base_version=self.reg.current_version[wf],
                     proposed_config=new, change_summary=summary, diff=build_diff(cfg, new), affected_leads=aff,
                     dry_run=dry, impact_score=impact_score(w.primary, urgency, pred), rationale=rationale,
                     evidence=evidence, facets=facets, notice_minutes=notice)

    @staticmethod
    def _partial(cfg, new, vf) -> float:
        """Fraction of the variant facet's intended move that `new` actually makes (auto-apply steps)."""
        num = den = 0.0
        ids = {c["id"] for c in cfg.get("cohorts") or []}
        for cid, dd in vf.data["alloc"].items():
            src = cid if cid in ids else "default"
            for d, t in dd.items():
                den += tv(dist(cfg, src, d), t)
                num += tv(dist(cfg, src, d), dist(new, cid if cid in {c["id"] for c in new.get("cohorts") or []}
                                                  else "default", d))
        return num / den if den else 1.0

    # ================================================================== callbacks
    def _on_applied(self, wf: str, version: int, row: dict):
        try:
            cur = self.client.config(wf)
            cfg = cur["config"] if cur["version"] == version else row["body"]["proposed_config"]
        except (ApiError, TransportError):
            cfg = row["body"]["proposed_config"]
        actor = "controller_auto" if row["channel"] == "auto" else "controller_approved"
        with self.store.tx():
            self.reg.record_version(wf, version, actor, row.get("applied_minute"), None, row["proposal_id"],
                                    row.get("approval_id"), config=cfg)
            st = self.store.wf_state(wf)
            st.setdefault("applied", []).append({"version": version, "minute": row.get("applied_minute"),
                                                 "proposal_id": row["proposal_id"],
                                                 "facets": [x["name"] for x in row["intents"]]})
            self.store.save_wf_state(wf, st)

    def _still_ok(self, row: dict, now: int):
        names = {x["name"] for x in row["intents"]}
        if "variants" not in names or row["kind"] == "bootstrap":
            return True, "ok"
        w = self.reg.wfs[row["wf"]]
        st = self.store.wf_state(row["wf"])
        cfg = self.reg.config(row["wf"])
        new = row["body"]["proposed_config"]
        tups, cnt = self.planner.population(w, now)
        from .analysis import EngagementModel
        model = EngagementModel(self.s, self.fs, self.ing.t(row["wf"])["eng"], w.options, st["ref"])
        fit = model.fit(now // MIN_PER_DAY)
        rng = np.random.default_rng(7)
        draws = {d: self.planner._tup_draws(fit[d], tups, 2000, rng) for d in VARIANT_DIMS}
        _, p_harm, mean = self.planner.engagement_delta(w, cfg, new, config_cohorts(new), tups, cnt, draws)
        if p_harm > 0.5:
            return False, f"fresh data: P(engagement −1%) = {p_harm:.0%} (mean {mean:+.1%}); letting it lapse"
        return True, "ok"

    def _notice_still_wanted(self, row: dict, now: int) -> bool:
        ok, _ = self._still_ok(row, now)
        return ok

    # ================================================================== observability (C14)
    def _observe(self, now: int, st: dict):
        if now % 60 != 0:
            return
        last_ev = self.store.meta("last_event_minute", now)
        lag = max(0, now - (last_ev or now))
        ist = self.ing.stats
        with self.store.tx():
            self.store.metric(now, "ingest_lag_sim_min", lag)
            self.store.metric(now, "ingest_events_total", ist["events"])
            self.store.metric(now, "ingest_events_per_s", ist["events"] / max(ist["seconds"], 1e-9))
            self.store.metric(now, "credits_remaining", st["credits_remaining"])
            self.store.metric(now, "open_approvals", self.store.q1(
                "SELECT COUNT(*) FROM proposals WHERE status IN ('pending','approved')") or 0)
            self.store.metric(now, "decisions_last_hour", self.store.q1(
                "SELECT COUNT(*) FROM decisions WHERE minute>?", (now - 60,)) or 0)
            self.store.metric(now, "rss_mb", _rss_mb())
            self.store.metric(now, "plan_seconds_total", self._wall.get("plan_s", 0.0))
            if now % MIN_PER_DAY == 0:
                self.store.metric(now, "wall_s_per_sim_day", time.time() - self._t_day)
                self._t_day = time.time()
            total = self.store.meta("credits_total") or st["credits_remaining"]
            self.store.set_meta("credits_total", total)
            if st["credits_remaining"] < 0.2 * total:
                self.store.alert(now, "ticket", None, "credits_low",
                                 f"credits at {st['credits_remaining']:.0f}/{total:.0f}; incident reserve at risk")
            if lag > 120:
                self.store.alert(now, "page", None, "ingest_lag",
                                 f"ingest is {lag} sim-minutes behind; decisions are stale. Check chunk fetch errors.")
            else:
                self.store.resolve_alert(now, None, "ingest_lag")
        if self.view_path and now % self.s.view_every_min == 0:
            self._write_view()

    def _write_view(self):
        try:
            from .view import render_html
            html = render_html(self.store.path)
            tmp = self.view_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(html)
            os.replace(tmp, self.view_path)
        except Exception as e:  # the view must never take the loop down
            log.warning("view render failed: %s", e)

    def _finish(self):
        self._ingest()
        now = self.client.state()["sim_minute"]
        with self.store.tx():
            self.store.decide(now, None, "run_finished", "scenario finished", {"ingest": self.ing.stats,
                                                                              "plan_seconds": self._wall.get("plan_s")})
            self.store.set_meta("finished", now)
        if self.view_path:
            self._write_view()


def _compact(ev: dict, limit: int = 6000) -> dict:
    s = json.dumps(ev, default=str)
    if len(s) <= limit:
        return json.loads(s)
    return {"truncated": True, "keys": list(ev)[:20]}


def _rss_mb() -> float:
    try:
        import resource
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
    except ImportError:
        try:
            import ctypes
            from ctypes import wintypes

            class PMC(ctypes.Structure):
                _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                            ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                            ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]
            c = PMC()
            c.cb = ctypes.sizeof(PMC)
            ctypes.windll.psapi.GetProcessMemoryInfo(ctypes.windll.kernel32.GetCurrentProcess(), ctypes.byref(c), c.cb)
            return c.PeakWorkingSetSize / 1e6
        except Exception:
            return -1.0
