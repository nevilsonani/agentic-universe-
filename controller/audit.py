"""Answer audit questions from the controller's own store (C12) — never from simulator logs.

    why    "why did workflow X change at T, who approved it, and what happened next?"
    heard  "why did this customer hear what they heard on day D?" (Compliance, IRDAI, 24h SLA)
    health loop health + open alerts for whoever is on call (C14)
"""
from __future__ import annotations

import json
import sqlite3
import time

from .features import label, parse_label

MIN_PER_DAY = 1440


def _db(path: str) -> sqlite3.Connection:
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


def _wfi(db, wf: str) -> int:
    r = db.execute("SELECT idx FROM workflows WHERE wf=?", (wf,)).fetchone()
    if not r:
        raise SystemExit(f"unknown workflow {wf}")
    return r[0]


def _kpi(db, wfi: int, m0: int, m1: int) -> dict:
    """Pickup and engagement from the call log over [m0, m1) — two separate numbers, never blended."""
    r = db.execute("SELECT COUNT(*), SUM(outcome & 1), SUM((outcome & 3) = 3), SUM((outcome & 5) = 5) FROM calls "
                   "WHERE wfi=? AND minute>=? AND minute<?", (wfi, m0, m1)).fetchone()
    dials, conn, eng, flag = (r[0] or 0), (r[1] or 0), (r[2] or 0), (r[3] or 0)
    return {"dials": dials, "pickup_rate": round(conn / dials, 4) if dials else None,
            "engagement_rate": round(eng / conn, 4) if conn else None,
            "hallucination_flag_rate": round(flag / conn, 4) if conn else None}


def why(path: str, wf: str, at: str, as_json: bool = False) -> str:
    db = _db(path)
    t = parse_label(at)
    wfi = _wfi(db, wf)
    v = db.execute("SELECT * FROM config_versions WHERE wf=? AND minute IS NOT NULL AND minute<=? "
                   "ORDER BY minute DESC, version DESC LIMIT 1", (wf, t + 14)).fetchone()
    if not v:
        return f"{wf}: no configuration change at or before {label(t)} — the business default was live."
    out = {"question": f"why did {wf} change at {label(t)}?", "version": v["version"], "actor": v["actor"],
           "live_from": label(v["minute"]), "changed_dims": json.loads(v["changed_dims"] or "[]")}
    if v["minute"] < t - 14:
        out["note"] = (f"no change exactly at {label(t)}; the config live then was v{v['version']} from "
                       f"{label(v['minute'])}")
    p = db.execute("SELECT * FROM proposals WHERE proposal_id=?", (v["proposal_id"],)).fetchone() \
        if v["proposal_id"] else None
    if v["actor"] == "supervisor":
        d = db.execute("SELECT summary, detail FROM decisions WHERE wf=? AND kind='supervisor_edit' AND minute=?",
                       (wf, v["minute"])).fetchone()
        out["who"] = "client supervisor (edit made directly on the platform, outside governance)"
        out["what_we_did"] = d["summary"] if d else None
    elif p:
        body = json.loads(p["body"])
        out["who"] = ("auto-applied by the controller under the workflow's auto-apply policy"
                      if p["channel"] == "auto" else
                      f"approved by the client's approver ({p['approval_id']}), decided {label(p['decided_minute'])}"
                      if p["decided_minute"] is not None else f"approval {p['approval_id']}")
        out["proposal"] = {"id": p["proposal_id"], "channel": p["channel"], "urgency": p["note"],
                           "summary": p["summary"], "requested": label(p["sent_minute"] or p["created_minute"]),
                           "impact_score": body.get("impact_score"), "affected_leads": body.get("affected_leads"),
                           "rationale": body.get("rationale"), "evidence": body.get("evidence"),
                           "dry_run": body.get("dry_run")}
        trail = db.execute("SELECT minute, kind, summary FROM decisions WHERE proposal_id=? ORDER BY id",
                           (p["proposal_id"],)).fetchall()
        out["lifecycle"] = [f"{label(r['minute'])} {r['kind']}: {r['summary']}" for r in trail]
    before = _kpi(db, wfi, v["minute"] - MIN_PER_DAY, v["minute"])
    after = _kpi(db, wfi, v["minute"], v["minute"] + MIN_PER_DAY)
    out["what_happened_next"] = {"24h_before": before, "24h_after": after}
    nxt = db.execute("SELECT version, actor, minute, changed_dims FROM config_versions WHERE wf=? AND minute>? "
                     "ORDER BY minute LIMIT 3", (wf, v["minute"])).fetchall()
    out["what_happened_next"]["next_changes"] = [f"v{r['version']} {label(r['minute'])} by {r['actor']}: "
                                                 f"{', '.join(json.loads(r['changed_dims'] or '[]'))}" for r in nxt]
    inc = db.execute("SELECT id, type, status, opened_minute FROM incidents WHERE wf=? AND opened_minute BETWEEN ? "
                     "AND ?", (wf, v["minute"] - MIN_PER_DAY, v["minute"] + MIN_PER_DAY)).fetchall()
    out["what_happened_next"]["incidents_nearby"] = [f"{r['id']} ({r['type']}, {r['status']}) opened "
                                                     f"{label(r['opened_minute'])}" for r in inc]
    if as_json:
        return json.dumps(out, indent=2, default=str)
    L = [f"# {out['question']}", "",
         f"v{out['version']} went live {out['live_from']} — actor: {out['actor']} — dims: {', '.join(out['changed_dims'])}"]
    if out.get("note"):
        L.append(f"({out['note']})")
    L.append(f"Who: {out.get('who')}")
    if out.get("what_we_did"):
        L.append(f"What we did about it: {out['what_we_did']}")
    if "proposal" in out:
        pr = out["proposal"]
        L += ["", f"Proposal {pr['id']} ({pr['channel']}, {pr['urgency']}), requested {pr['requested']}",
              f"  Summary: {pr['summary']}", f"  Impact score: {pr['impact_score']} · affected leads: {pr['affected_leads']}",
              f"  Rationale: {pr['rationale']}"]
        dr = pr.get("dry_run") or {}
        if dr.get("predicted"):
            L.append(f"  Predicted: {json.dumps(dr['predicted'])}")
        rep = dr.get("engagement_replay") or {}
        if rep.get("proposed"):
            L.append(f"  Dry run (IPS replay, last {rep.get('window_days')}d): proposed {rep['proposed']['engagement']} vs "
                     f"current {rep['current']['engagement']}, unsupported share {rep['proposed']['unsupported_share']}")
        L += ["", "Lifecycle:"] + [f"  {x}" for x in out.get("lifecycle", [])]
    n = out["what_happened_next"]
    L += ["", "What happened next:",
          f"  24h before: pickup {n['24h_before']['pickup_rate']} · engagement {n['24h_before']['engagement_rate']} "
          f"· flag rate {n['24h_before']['hallucination_flag_rate']} ({n['24h_before']['dials']} dials)",
          f"  24h after:  pickup {n['24h_after']['pickup_rate']} · engagement {n['24h_after']['engagement_rate']} "
          f"· flag rate {n['24h_after']['hallucination_flag_rate']} ({n['24h_after']['dials']} dials)"]
    L += [f"  next: {x}" for x in n["next_changes"]] + [f"  incident: {x}" for x in n["incidents_nearby"]]
    return "\n".join(L)


def heard(path: str, person: str, day: int | None = None) -> str:
    db = _db(path)
    wf, num = person.rsplit(":", 1)
    pnum = int(num.lstrip("p"))
    wfi = _wfi(db, wf)
    info = json.loads(db.execute("SELECT info FROM workflows WHERE wf=?", (wf,)).fetchone()[0])
    opts = info["options"]
    coh = {r[1]: r[0] for r in db.execute("SELECT name, code FROM vocab WHERE kind='cohort'")}
    pro = {r[1]: r[0] for r in db.execute("SELECT name, code FROM vocab WHERE kind='prompt'")}
    q = "SELECT * FROM calls WHERE wfi=? AND person=?"
    args = [wfi, pnum]
    if day is not None:
        q += " AND minute>=? AND minute<?"
        args += [day * MIN_PER_DAY, (day + 1) * MIN_PER_DAY]
    rows = db.execute(q + " ORDER BY minute", args).fetchall()
    if not rows:
        return f"no calls to {person}" + (f" on D{day}" if day is not None else "")
    L = [f"# What {person} heard" + (f" on D{day}" if day is not None else ""), ""]
    reasons = {}
    for r in rows:
        o = r["outcome"]
        L.append(f"{label(r['minute'])} attempt {r['attempt']}: caller ID {opts['caller_id'][r['did']]}, "
                 + (f"welcome {opts['welcome_message'][r['wm']]}, voice {opts['voice'][r['voice']]}, "
                    f"prompt {pro.get(r['prompt'])} — " if o & 1 else "")
                 + ("engaged" if o & 2 else "connected, no conclusion" if o & 1 else "not connected")
                 + (" [hallucination flag]" if o & 4 else "")
                 + f" · cohort {coh.get(r['cohort'])} · config v{r['ver']}")
        reasons.setdefault(r["ver"], None)
    L += ["", "Why those variants were live (randomised per call from the cohort's allocation in that version):"]
    for ver in sorted(reasons):
        v = db.execute("SELECT actor, minute, proposal_id, approval_id FROM config_versions WHERE wf=? AND version=?",
                       (wf, ver)).fetchone()
        if not v:
            continue
        s = ""
        if v["proposal_id"]:
            p = db.execute("SELECT summary FROM proposals WHERE proposal_id=?", (v["proposal_id"],)).fetchone()
            s = p["summary"] if p else ""
        L.append(f"  v{ver}: set by {v['actor']} at {label(v['minute']) if v['minute'] is not None else 'start'}"
                 + (f" via {v['approval_id']}" if v["approval_id"] else "") + (f" — {s}" if s else ""))
    return "\n".join(L)


def health(path: str) -> str:
    db = _db(path)
    m = {r["name"]: (r["value"], r["minute"]) for r in db.execute(
        "SELECT name, value, minute FROM metrics m WHERE minute=(SELECT MAX(minute) FROM metrics m2 WHERE m2.name=m.name)")}
    L = ["# Loop health", ""]
    for k in ("ingest_lag_sim_min", "ingest_events_per_s", "ingest_events_total", "decisions_last_hour",
              "credits_remaining", "open_approvals", "rss_mb", "wall_s_per_sim_day"):
        if k in m:
            L.append(f"  {k:24s} {m[k][0]:>12,.1f}   (at {label(m[k][1])})")
    cur = db.execute("SELECT v FROM meta WHERE k='cursor'").fetchone()
    L.append(f"  {'ingest cursor':24s} {cur[0] if cur else '-':>12}")
    L += ["", "Open alerts:"]
    al = db.execute("SELECT severity, wf, code, message, minute FROM alerts WHERE resolved_minute IS NULL "
                    "ORDER BY severity, minute").fetchall()
    L += [f"  [{a['severity'].upper()}] {label(a['minute'])} {a['wf'] or '-'} {a['code']}: {a['message']}" for a in al] \
        or ["  none"]
    L += ["", "In-flight proposals:"]
    pr = db.execute("SELECT proposal_id, status, created_minute, summary FROM proposals WHERE status IN "
                    "('notice','sending','pending','approved','applying') ORDER BY created_minute").fetchall()
    L += [f"  {p['proposal_id']} {p['status']} since {label(p['created_minute'])}: {p['summary'][:100]}" for p in pr] \
        or ["  none"]
    return "\n".join(L)


def digest(path: str, tenant: str | None = None, since: str = "D0", until: str | None = None) -> str:
    """Weekly summary for a client (Compliance asked for it): every change, who made it and why, with the
    operational auto-applies we made under the signed policy called out separately; approvals; incidents."""
    db = _db(path)
    t0 = parse_label(since)
    t1 = parse_label(until) if until else 10 ** 9
    wfs = [r for r in db.execute("SELECT wf, tenant, info FROM workflows ORDER BY idx")
           if tenant is None or r["tenant"] == tenant]
    L = [f"# Weekly summary{' — ' + tenant if tenant else ''} ({label(t0)} → {label(min(t1, 10 ** 8)) if until else 'now'})", ""]
    for w in wfs:
        wf = w["wf"]
        L += [f"## {wf}", ""]
        rows = db.execute("SELECT version, actor, minute, changed_dims, proposal_id, approval_id FROM config_versions "
                          "WHERE wf=? AND minute>=? AND minute<? ORDER BY minute", (wf, t0, t1)).fetchall()
        ops = [r for r in rows if r["actor"] == "controller_auto"]
        appr = [r for r in rows if r["actor"] == "controller_approved"]
        sup = [r for r in rows if r["actor"] == "supervisor"]

        def line(r):
            p = db.execute("SELECT summary FROM proposals WHERE proposal_id=?", (r["proposal_id"],)).fetchone() \
                if r["proposal_id"] else None
            return (f"- {label(r['minute'])} v{r['version']} ({', '.join(json.loads(r['changed_dims'] or '[]'))})"
                    + (f" via {r['approval_id']}" if r["approval_id"] else "") + (f": {p['summary']}" if p else ""))
        L += [f"**Approved by your approvers ({len(appr)})**", ""] + ([line(r) for r in appr] or ["- none"]) + [""]
        L += [f"**Applied by us under the auto-apply policy you signed ({len(ops)})** — operational settings only "
              f"(caller-ID rotation, call timing{', and within max_shift, welcome/voice' if any('voice' in (r['changed_dims'] or '') or 'welcome' in (r['changed_dims'] or '') for r in ops) else ''})",
              ""] + ([line(r) for r in ops] or ["- none"]) + [""]
        L += [f"**Your own edits ({len(sup)})** — never overwritten silently", ""] + \
             ([f"- {label(r['minute'])} v{r['version']} ({', '.join(json.loads(r['changed_dims'] or '[]'))})" for r in sup]
              or ["- none"]) + [""]
        st = {r["status"]: r["n"] for r in db.execute(
            "SELECT status, COUNT(*) n FROM proposals WHERE wf=? AND channel='approval' AND created_minute>=? "
            "AND created_minute<? GROUP BY status", (wf, t0, t1))}
        L += [f"Approval requests: {sum(st.values())} ({', '.join(f'{k} {v}' for k, v in st.items()) or 'none'})"]
        inc = db.execute("SELECT id, type, status, opened_minute, resolved_minute FROM incidents WHERE wf=? AND "
                         "opened_minute>=? AND opened_minute<?", (wf, t0, t1)).fetchall()
        L += [f"Incidents: " + ("; ".join(f"{i['id']} {i['type']} opened {label(i['opened_minute'])}, {i['status']}"
                                          + (f" {label(i['resolved_minute'])}" if i['resolved_minute'] is not None else "")
                                          for i in inc) or "none")]
        val = db.execute("SELECT run_id, prompt, n, pass_rate FROM validations WHERE wf=? AND minute>=? AND minute<?",
                         (wf, t0, t1)).fetchall()
        if val:
            L += ["Validation evidence: " + "; ".join(f"{v['run_id']} {v['prompt']} pass {v['pass_rate']:.3f} (n={v['n']})"
                                                      for v in val)]
        L.append("")
    return "\n".join(L)
