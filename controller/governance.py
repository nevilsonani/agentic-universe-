"""Governance client: every config change goes through here (C9).

Proposal lifecycle (persisted in `proposals`, one row per proposal_id):

    approval:  sending → pending → approved → applying → applied
                                 ↘ rejected / expired           (→ planner re-plans with fresh data)
                         approved → stale   (base moved before we applied; we never call apply → no conflict)
                         approved → lapsed  (apply window missed; recorded, re-planned)
                         approved → cancelled (fresh data says it is now harmful; we let it lapse on purpose)
    auto:      [notice →] sending → applied | failed(403) | stale(409)

Outbox rule: the row (with the exact request body and a deterministic proposal_id)
is committed *before* the HTTP call. After a crash, rows still in `sending` /
`applying` are reconciled against the platform (GET /approvals, config history)
before anything is re-sent, so nothing is ever double-applied or lost.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from .client import ApiError, SimClient, TransportError
from .features import label
from .registry import Registry, changed_dims
from .settings import MIN_PER_DAY, Settings, TenantPrefs

log = logging.getLogger("controller.governance")

OPEN = ("notice", "sending", "pending", "approved", "applying")


@dataclass
class Draft:
    wf: str
    kind: str
    urgency: str                 # routine | protective | safety
    channel: str                 # approval | auto
    base_version: int
    proposed_config: dict
    change_summary: str
    diff: dict
    affected_leads: int
    dry_run: dict
    impact_score: float
    rationale: str
    evidence: dict = field(default_factory=dict)
    facets: list = field(default_factory=list)
    notice_minutes: int = 0
    expires_in_hours: float | None = None


class Governance:
    def __init__(self, store, client: SimClient, reg: Registry, s: Settings, prefs: dict[str, TenantPrefs]):
        self.store, self.c, self.reg, self.s, self.prefs = store, client, reg, s, prefs
        self.on_applied = []      # callbacks(wf, version, proposal_row) — controller refreshes config/state
        self.still_ok = None      # callback(row) -> (bool, reason) checked right before applying an approval

    # ------------------------------------------------------------------ policy helpers
    def pref(self, wf: str) -> TenantPrefs:
        return self.prefs.get(self.reg.wfs[wf].tenant, TenantPrefs())

    def requests_24h(self, wf: str, now: int) -> int:
        return self.store.q1("SELECT COUNT(*) FROM proposals WHERE wf=? AND channel='approval' AND sent_minute>? "
                             "AND status NOT IN ('failed','draft','sending')", (wf, now - MIN_PER_DAY)) or 0

    def _routine_count(self, wf: str, now: int) -> int:
        rows = self.store.q("SELECT note FROM proposals WHERE wf=? AND channel='approval' AND sent_minute>? "
                            "AND status NOT IN ('failed','draft','sending')", (wf, now - MIN_PER_DAY))
        return sum(1 for (n,) in rows if (n or "").startswith("routine"))

    def routine_cap(self, wf: str) -> int:
        pol = int(self.reg.wfs[wf].gov.get("max_requests_per_24h", 4))
        cap = max(1, pol - self.s.emergency_reserve)
        p = self.pref(wf)
        if p.max_routine_requests_per_day:
            cap = min(cap, p.max_routine_requests_per_day)
        return cap

    def open_rows(self, wf: str, channel: str | None = None) -> list[dict]:
        sql = f"SELECT * FROM proposals WHERE wf=? AND status IN ({','.join('?' * len(OPEN))})"
        args = [wf, *OPEN]
        if channel:
            sql += " AND channel=?"
            args.append(channel)
        return [self._row(r) for r in self.store.db.execute(sql, args).fetchall()]

    def _row(self, r) -> dict:
        cols = [d[0] for d in self.store.db.execute("SELECT * FROM proposals LIMIT 0").description]
        d = dict(zip(cols, r))
        d["body"] = json.loads(d["body"]) if d["body"] else {}
        d["intents"] = json.loads(d["intents"]) if d["intents"] else []
        return d

    def has_open_approval(self, wf: str, routine_only: bool = False) -> bool:
        rows = self.open_rows(wf, "approval")
        if routine_only:
            rows = [r for r in rows if (r["note"] or "").startswith("routine")]
        return bool(rows)

    @staticmethod
    def next_working(t: int, hours) -> int:
        lo, hi = hours
        day, mod = divmod(t, MIN_PER_DAY)
        if mod < lo * 60:
            return day * MIN_PER_DAY + lo * 60
        if mod >= hi * 60:
            return (day + 1) * MIN_PER_DAY + lo * 60
        return t

    def send_window(self, wf: str, now: int, urgency: str) -> tuple[bool, str]:
        """May an approval request for this workflow go out now?"""
        w = self.reg.wfs[wf]
        g = w.gov
        lo, hi = g.get("approver_hours", [10, 19])
        lat_lo, lat_hi = g.get("approver_latency_minutes", [20, 240])
        ttl = float(g.get("proposal_ttl_hours", 24)) * 60
        mod = now % MIN_PER_DAY
        if self.requests_24h(wf, now) >= int(g.get("max_requests_per_24h", 4)):
            return False, "policy request limit reached for the last 24h"
        if urgency == "safety":
            worst = self.next_working(now + lat_hi, (lo, hi))
            if worst > now + ttl - self.s.approval_send_margin_min and not (lo * 60 <= mod < hi * 60):
                return False, "would expire before an approver could see it"
            return True, "safety: send now"
        p = self.pref(wf)
        eff_lo = max(lo, p.not_before_hour or 0)
        if not (eff_lo * 60 <= mod < hi * 60 - min(lat_hi, 180)):
            return False, f"outside routine send window {eff_lo:02d}:00–{(hi * 60 - min(lat_hi, 180)) // 60:02d}:00"
        worst = self.next_working(now + lat_hi, (lo, hi))
        if worst > now + ttl - self.s.approval_send_margin_min:
            return False, "TTL too short for the approver's worst-case latency"
        if urgency == "routine":
            if self._routine_count(wf, now) >= self.routine_cap(wf):
                return False, f"routine budget {self.routine_cap(wf)}/24h used (1 slot held for incidents)"
            last = self.store.q1("SELECT MAX(sent_minute) FROM proposals WHERE wf=? AND channel='approval' "
                                 "AND note LIKE 'routine%'", (wf,))
            gap = max(180, int((hi - eff_lo) * 60 / max(self.routine_cap(wf), 1)))
            if last is not None and now - last < gap:
                return False, f"spacing routine requests ≥{gap // 60}h apart"
            back = self.store.meta(f"reject_backoff:{wf}")
            if back and now < back:
                return False, f"backing off after a rejection until {label(back)}"
        return True, "ok"

    def auto_ok(self, wf: str, now: int, urgency: str) -> tuple[bool, str]:
        if urgency == "routine" and self.has_open_approval(wf):
            return False, "an approval is in flight; a routine auto-apply would make it stale"
        last = self.store.meta(f"last_auto:{wf}")
        cool = self.s.safety_step_cooldown_min if urgency != "routine" else self.s.auto_apply_cooldown_min
        if last is not None and now - last < cool:
            return False, f"auto-apply cooldown ({cool} min)"
        if any(r["status"] in ("notice", "sending") for r in self.open_rows(wf, "auto")):
            return False, "another auto-apply is already queued"
        return True, "ok"

    # ------------------------------------------------------------------ submission
    def new_id(self, wf: str, kind: str) -> str:
        n = int(self.store.meta(f"seq:{wf}", 0)) + 1
        self.store.set_meta(f"seq:{wf}", n)
        return f"{wf}.{kind}.{n:04d}"

    def submit(self, d: Draft, now: int) -> dict:
        """Persist then send. Returns the proposal row."""
        with self.store.tx():
            pid = self.new_id(d.wf, d.kind)
            body = self._body(pid, d)
            status = "notice" if d.channel == "auto" and d.notice_minutes > 0 else "sending"
            self.store.db.execute(
                "INSERT INTO proposals(proposal_id, wf, kind, channel, status, base_version, created_minute, "
                "not_before_minute, summary, body, intents, note) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (pid, d.wf, d.kind, d.channel, status, d.base_version, now, now + d.notice_minutes,
                 d.change_summary, json.dumps(body), json.dumps([f.to_json() if hasattr(f, "to_json") else f
                                                                 for f in d.facets]),
                 f"{d.urgency}"))
            self.store.decide(now, d.wf, f"proposal_{status}", d.change_summary,
                              {"proposal_id": pid, "channel": d.channel, "urgency": d.urgency,
                               "impact_score": d.impact_score, "affected_leads": d.affected_leads,
                               "dims": changed_dims(self.reg.config(d.wf, d.base_version) or d.proposed_config,
                                                    d.proposed_config) if self.reg.config(d.wf, d.base_version) else None,
                               "rationale": d.rationale, "dry_run": d.dry_run, "evidence": d.evidence,
                               "not_before": label(now + d.notice_minutes) if status == "notice" else None},
                              proposal_id=pid)
        if status == "notice":
            return self.get(pid)
        return self._send(pid, now)

    def _body(self, pid: str, d: Draft) -> dict:
        if d.channel == "auto":
            return {"proposal_id": pid, "base_version": d.base_version, "proposed_config": d.proposed_config,
                    "rationale": f"{d.change_summary}. {d.rationale}", "evidence": d.evidence}
        b = {"proposal_id": pid, "workflow_id": d.wf, "base_version": d.base_version,
             "proposed_config": d.proposed_config, "change_summary": d.change_summary, "diff": d.diff or {"note": "-"},
             "affected_leads": int(max(0, d.affected_leads)), "dry_run": d.dry_run or {"note": "-"},
             "impact_score": float(min(1.0, max(0.0, d.impact_score))), "rationale": d.rationale,
             "evidence": d.evidence}
        ttl = self.reg.wfs[d.wf].gov.get("proposal_ttl_hours", 24)
        b["expires_in_hours"] = float(min(d.expires_in_hours or ttl, ttl))
        return b

    def get(self, pid: str) -> dict | None:
        r = self.store.db.execute("SELECT * FROM proposals WHERE proposal_id=?", (pid,)).fetchone()
        return self._row(r) if r else None

    def _set(self, pid: str, now: int, status: str, decision: str | None = None, detail=None, **cols):
        sets = ", ".join(["status=?"] + [f"{k}=?" for k in cols])
        with self.store.tx():
            self.store.db.execute(f"UPDATE proposals SET {sets} WHERE proposal_id=?",
                                  (status, *cols.values(), pid))
            if decision:
                row = self.get(pid)
                self.store.decide(now, row["wf"], decision, f"{pid}: {status}"
                                  + (f" — {detail.get('why')}" if isinstance(detail, dict) and detail.get("why") else ""),
                                  detail, proposal_id=pid)

    def _send(self, pid: str, now: int) -> dict:
        row = self.get(pid)
        if row["status"] == "notice":
            self._set(pid, now, "sending")
        body = row["body"]
        wf = row["wf"]
        try:
            if row["channel"] == "auto":
                r = self.c.auto_apply(wf, body)
                self._applied(pid, now, r["version"], r.get("changed_dims"))
            else:
                r = self.c.request_approval(body)
                g = self.reg.wfs[wf].gov
                self._set(pid, now, "pending", "approval_requested",
                          {"approval_id": r["approval_id"], "expires_at": r.get("expires_at")},
                          approval_id=r["approval_id"], sent_minute=now,
                          expires_minute=now + int(body.get("expires_in_hours", g.get("proposal_ttl_hours", 24)) * 60))
        except ApiError as e:
            if e.code == "duplicate_proposal_id":
                return self.reconcile(pid, now)
            if e.code == "stale_base_version":
                self._set(pid, now, "stale", "proposal_stale", {"why": "config moved before we sent", "detail": e.detail})
            else:
                self._set(pid, now, "failed", "proposal_failed", {"why": f"{e.status} {e.code}", "detail": e.detail},
                          reasons=json.dumps(e.detail, default=str))
                with self.store.tx():
                    self.store.alert(now, "ticket", wf, f"proposal_failed:{e.code}",
                                     f"{pid} was refused ({e.status} {e.code}): {str(e.detail)[:300]}")
        except TransportError as e:
            log.warning("send %s failed in transport (%s); will reconcile next tick", pid, e)
        return self.get(pid)

    def _applied(self, pid: str, now: int, version: int, dims=None):
        self._set(pid, now, "applied", "applied", {"version": version, "dims": dims, "why": f"live as v{version}"},
                  applied_version=version, applied_minute=now)
        if self.get(pid)["channel"] == "auto":
            with self.store.tx():
                self.store.set_meta(f"last_auto:{self.get(pid)['wf']}", now)
        row = self.get(pid)
        for cb in self.on_applied:
            cb(row["wf"], version, row)

    # ------------------------------------------------------------------ crash reconciliation
    def reconcile(self, pid: str, now: int) -> dict:
        """Find out what really happened to a proposal whose HTTP outcome we don't know."""
        row = self.get(pid)
        wf = row["wf"]
        if row["channel"] == "auto":
            hist = self.c.config_history(wf)
            v = next((h for h in hist if h.get("proposal_id") == pid), None)
            if v:
                self._applied(pid, now, v["version"], v.get("changed_dims"))
            else:
                return self._send(pid, now)
        else:
            a = next((x for x in self.c.approvals(workflow_id=wf) if x["proposal_id"] == pid), None)
            if a is None:
                return self._send(pid, now)
            self._set(pid, now, "pending", "approval_reconciled", {"approval_id": a["approval_id"]},
                      approval_id=a["approval_id"], sent_minute=row["sent_minute"] or now)
            self.poll_one(self.get(pid), now)
        return self.get(pid)

    def recover(self, now: int):
        for (pid,) in self.store.q("SELECT proposal_id FROM proposals WHERE status IN ('sending','applying')"):
            row = self.get(pid)
            log.info("recovering %s (%s)", pid, row["status"])
            if row["status"] == "sending":
                self.reconcile(pid, now)
            else:
                self.poll_one(row, now)

    # ------------------------------------------------------------------ polling / applying
    def poll(self, now: int):
        for (pid,) in self.store.q("SELECT proposal_id FROM proposals WHERE status IN ('pending','approved','applying')"):
            self.poll_one(self.get(pid), now)

    def poll_one(self, row: dict, now: int):
        pid, wf, aid = row["proposal_id"], row["wf"], row["approval_id"]
        if not aid:
            return
        try:
            a = self.c.approval(aid)
        except (ApiError, TransportError) as e:
            log.warning("poll %s: %s", aid, e)
            return
        st = a["status"]
        if st == "pending":
            return
        if st in ("rejected", "expired"):
            if row["status"] != st:
                self._set(pid, now, st, f"approval_{st}", {"reasons": a.get("reasons"), "why": "; ".join(
                    a.get("reasons") or []) or st}, reasons=json.dumps(a.get("reasons") or []),
                          decided_minute=now)
                self._after_rejection(wf, now, a)
            return
        if st == "lapsed":
            self._set(pid, now, "lapsed", "approval_lapsed", {"why": "apply window missed"})
            return
        if st == "applied":
            if row["status"] != "applied":
                hist = self.c.config_history(wf)
                v = next((h["version"] for h in hist if h.get("approval_id") == aid), None)
                self._applied(pid, now, v or self.reg.current_version.get(wf))
            return
        # approved: apply now if still valid
        apply_by = a.get("apply_by")
        if row["status"] == "pending":
            from .features import ts_to_minute
            self._set(pid, now, "approved", "approval_approved",
                      {"why": f"approved, apply by {apply_by}", "reasons": a.get("reasons")},
                      decided_minute=ts_to_minute(a["decided_at"]) if a.get("decided_at") else now,
                      apply_by_minute=ts_to_minute(apply_by) if apply_by else None)
            row = self.get(pid)
        if row["apply_by_minute"] is not None and now > row["apply_by_minute"]:
            self._set(pid, now, "lapsed", "approval_lapsed", {"why": "apply window passed before we could apply"})
            return
        cur = self.reg.current_version.get(wf)
        if cur != row["base_version"]:
            self._set(pid, now, "stale", "approval_stale",
                      {"why": f"config moved v{row['base_version']}→v{cur} (e.g. supervisor edit); not applying, "
                              f"will re-propose on the new base", "current_version": cur})
            return
        if self.still_ok:
            ok, why = self.still_ok(row, now)
            if not ok:
                self._set(pid, now, "cancelled", "approval_cancelled", {"why": why})
                return
        self._set(pid, now, "applying")
        try:
            r = self.c.apply(wf, aid)
            self._applied(pid, now, r["version"], r.get("changed_dims"))
        except ApiError as e:
            if e.code == "stale_base_version":
                self._set(pid, now, "stale", "approval_stale", {"why": "stale at apply", "detail": e.detail})
            elif e.status == 410:
                self._set(pid, now, "lapsed", "approval_lapsed", {"why": "platform says lapsed"})
            elif e.code == "not_approved":
                self.poll_one(self.get(pid), now)   # e.g. we applied it just before a crash
            else:
                self._set(pid, now, "failed", "apply_failed", {"why": f"{e.status} {e.code}", "detail": e.detail})
        except TransportError:
            log.warning("apply %s transport error; will reconcile", pid)

    def _after_rejection(self, wf: str, now: int, a: dict):
        reasons = a.get("reasons") or []
        with self.store.tx():
            if any("fatigue" in r for r in reasons):
                self.store.set_meta(f"reject_backoff:{wf}", now + MIN_PER_DAY // 2)
            elif any("judgement" in r for r in reasons):
                self.store.set_meta(f"reject_backoff:{wf}", now + int(self.s.reject_backoff_hours * 60))
            elif reasons:
                self.store.alert(now, "ticket", wf, "proposal_rejected_invalid",
                                 f"{a['approval_id']} rejected for a fixable reason: {reasons}")

    # ------------------------------------------------------------------ notice-period auto-applies
    def flush_notices(self, now: int, still_wanted=None):
        for (pid,) in self.store.q("SELECT proposal_id FROM proposals WHERE status='notice' AND not_before_minute<=?",
                                   (now,)):
            row = self.get(pid)
            if self.reg.current_version.get(row["wf"]) != row["base_version"]:
                self._set(pid, now, "stale", "notice_cancelled", {"why": "config changed during the notice period"})
                continue
            if still_wanted and not still_wanted(row, now):
                self._set(pid, now, "cancelled", "notice_cancelled", {"why": "no longer wanted after fresh data"})
                continue
            self._send(pid, now)

    def supersede_open(self, wf: str, now: int, why: str, channel: str = "auto"):
        for r in self.open_rows(wf, channel):
            if r["status"] == "notice":
                self._set(r["proposal_id"], now, "superseded", "superseded", {"why": why})
