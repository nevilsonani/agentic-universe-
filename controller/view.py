"""Supervisor view (C11): one screen per workflow, rendered from the controller's store.

What's being tested · what's winning · what we need from you · what changed today ·
caller IDs · call timing · incidents · loop health. Pickup and engagement are always
shown as two numbers. Self-contained HTML (no CDN, works offline); `serve()` adds a
tiny read-only HTTP API (stdlib only):  /  /api/workflows  /api/why?workflow=&at=  /metrics
"""
from __future__ import annotations

import html
import json
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .features import label

DAY = 1440
CSS = """
:root{--bg:#f6f7f9;--card:#fff;--ink:#1d2330;--mute:#667085;--line:#e4e7ec;--ok:#12805c;--warn:#b54708;--bad:#b42318;
--acc:#3b5bdb;--soft:#eef2ff}
*{box-sizing:border-box}body{margin:0;font:14px/1.45 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
background:var(--bg);color:var(--ink)}header{padding:20px 28px;background:#111827;color:#fff}
header h1{margin:0;font-size:20px;font-weight:600}header p{margin:4px 0 0;color:#cbd5e1;font-size:13px}
main{padding:20px 28px;max-width:1280px;margin:auto}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(380px,1fr));gap:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px 18px;margin-bottom:16px}
h2{font-size:17px;margin:0 0 2px}h3{font-size:13px;text-transform:uppercase;letter-spacing:.04em;color:var(--mute);margin:16px 0 6px}
.sub{color:var(--mute);font-size:12.5px}.kpis{display:flex;gap:12px;flex-wrap:wrap;margin:10px 0}
.kpi{flex:1;min-width:140px;border:1px solid var(--line);border-radius:8px;padding:10px 12px}
.kpi b{display:block;font-size:20px}.kpi span{font-size:12px;color:var(--mute)}
.up{color:var(--ok)}.down{color:var(--bad)}table{border-collapse:collapse;width:100%;font-size:12.5px}
th,td{text-align:left;padding:5px 6px;border-bottom:1px solid var(--line);vertical-align:top}th{color:var(--mute);font-weight:600}
.pill{display:inline-block;padding:1px 8px;border-radius:999px;font-size:11.5px;font-weight:600}
.p-ok{background:#ecfdf3;color:var(--ok)}.p-warn{background:#fffaeb;color:var(--warn)}.p-bad{background:#fef3f2;color:var(--bad)}
.p-info{background:var(--soft);color:var(--acc)}.bar{height:8px;background:var(--soft);border-radius:4px;overflow:hidden;min-width:60px}
.bar i{display:block;height:100%;background:var(--acc)}.heat td{padding:2px 3px;text-align:center;font-size:11px}
.need{border-left:4px solid var(--warn)}.nav a{color:#cbd5e1;margin-right:14px;text-decoration:none;font-size:13px}
code{background:#f2f4f7;padding:1px 4px;border-radius:4px;font-size:12px}
"""


def _db(path):
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
    db.row_factory = sqlite3.Row
    return db


def _pct(x, d=1):
    return "—" if x is None else f"{100 * x:.{d}f}%"


def _rate(db, wfi, d0, d1):
    r = db.execute("SELECT SUM(dials), SUM(conn) FROM ptime WHERE wfi=? AND day>=? AND day<?",
                   (wfi, d0 + 64, d1 + 64)).fetchone()
    e = db.execute("SELECT SUM(conn), SUM(eng), SUM(flag) FROM eng WHERE wfi=? AND day>=? AND day<?",
                   (wfi, d0 + 64, d1 + 64)).fetchone()
    return {"dials": r[0] or 0, "pickup": (r[1] / r[0]) if r[0] else None,
            "engagement": (e[1] / e[0]) if e[0] else None, "flag": (e[2] / e[0]) if e[0] else None}


def _concl(db, wfi):
    up = db.execute("SELECT COUNT(*) FROM leads WHERE wfi=? AND minute>=0", (wfi,)).fetchone()[0]
    c = db.execute("SELECT COUNT(DISTINCT c.lead) FROM calls c JOIN leads l ON l.wfi=c.wfi AND l.lead=c.lead "
                   "WHERE c.wfi=? AND (c.outcome & 2) AND l.minute>=0", (wfi,)).fetchone()[0]
    return (c / up) if up else None, up


def workflows_json(path: str) -> list[dict]:
    db = _db(path)
    now = (db.execute("SELECT v FROM meta WHERE k='last_event_minute'").fetchone() or [0])[0]
    now = int(json.loads(now)) if isinstance(now, str) else int(now or 0)
    out = []
    for w in db.execute("SELECT wf, idx, tenant, info FROM workflows ORDER BY idx"):
        info = json.loads(w["info"])
        st = json.loads((db.execute("SELECT body FROM wf_state WHERE wf=?", (w["wf"],)).fetchone() or ["{}"])[0])
        cur = db.execute("SELECT version, config, actor, minute FROM config_versions WHERE wf=? ORDER BY version DESC "
                         "LIMIT 1", (w["wf"],)).fetchone()
        today = now // DAY
        pend = [dict(r) for r in db.execute(
            "SELECT proposal_id, channel, status, summary, created_minute, not_before_minute, apply_by_minute, "
            "expires_minute, body FROM proposals WHERE wf=? AND status IN ('notice','pending','approved','applying') "
            "ORDER BY created_minute", (w["wf"],))]
        for p in pend:
            b = json.loads(p.pop("body"))
            p.update(impact=b.get("impact_score"), affected=b.get("affected_leads"))
        changes = [dict(r) for r in db.execute(
            "SELECT version, actor, minute, changed_dims, proposal_id, approval_id FROM config_versions WHERE wf=? "
            "AND minute>=? ORDER BY minute", (w["wf"], today * DAY))]
        for c in changes:
            p = db.execute("SELECT summary FROM proposals WHERE proposal_id=?", (c["proposal_id"],)).fetchone() \
                if c["proposal_id"] else None
            c["summary"] = p["summary"] if p else ("supervisor edit" if c["actor"] == "supervisor" else "")
        inc = [dict(r) for r in db.execute("SELECT id, type, status, opened_minute, resolved_minute, detail FROM "
                                           "incidents WHERE wf=? ORDER BY opened_minute", (w["wf"],))]
        concl, uploaded = _concl(db, w["idx"])
        out.append({"workflow": w["wf"], "tenant": w["tenant"], "info": info, "state": st,
                    "config": json.loads(cur["config"]) if cur and cur["config"] else None,
                    "version": cur["version"] if cur else None,
                    "history": _rate(db, w["idx"], -64, 0), "run": _rate(db, w["idx"], 0, 10 ** 4),
                    "today": _rate(db, w["idx"], today, today + 1), "conclusion": concl, "uploaded": uploaded,
                    "pending": pend, "changes_today": changes, "incidents": inc, "now": now})
    return out


def _kpi(name, run, hist, fmt=_pct):
    d = None if run is None or not hist else run / hist - 1
    cls = "" if d is None else ("up" if d >= 0 else "down")
    return (f"<div class=kpi><span>{name}</span><b>{fmt(run)}</b><span class={cls}>"
            f"{'' if d is None else f'{d:+.1%} vs history'}</span></div>")


def render_html(path: str) -> str:
    wfs = workflows_json(path)
    db = _db(path)
    now = wfs[0]["now"] if wfs else 0
    alerts = [dict(r) for r in db.execute("SELECT severity, wf, code, message, minute FROM alerts WHERE "
                                          "resolved_minute IS NULL ORDER BY severity, minute")]
    met = {r["name"]: r["value"] for r in db.execute(
        "SELECT name, value FROM metrics m WHERE minute=(SELECT MAX(minute) FROM metrics m2 WHERE m2.name=m.name)")}
    e = html.escape
    H = [f"<!doctype html><html><head><meta charset=utf-8><title>Supervisor view — {label(now)}</title>"
         f"<style>{CSS}</style></head><body><header><h1>Outcome loop — supervisor view</h1>"
         f"<p>As of {label(now)} (simulated IST) · one screen per workflow: what is being tested, what is winning, "
         f"what we need from you, what changed today</p><p class=nav>"
         + "".join(f"<a href='#{e(w['workflow'])}'>{e(w['workflow'])}</a>" for w in wfs) + "</p></header><main>"]
    # loop health strip
    H.append("<div class=card><h2>Loop health</h2><div class=kpis>"
             f"<div class=kpi><span>ingest lag</span><b>{met.get('ingest_lag_sim_min', 0):.0f} min</b><span>sim-time behind</span></div>"
             f"<div class=kpi><span>ingest throughput</span><b>{met.get('ingest_events_per_s', 0):,.0f}/s</b><span>events</span></div>"
             f"<div class=kpi><span>decisions</span><b>{met.get('decisions_last_hour', 0):.0f}</b><span>last hour</span></div>"
             f"<div class=kpi><span>credits</span><b>{met.get('credits_remaining', 0):.0f}</b><span>remaining</span></div>"
             f"<div class=kpi><span>open alerts</span><b>{len(alerts)}</b><span>{sum(a['severity'] == 'page' for a in alerts)} paging</span></div>"
             "</div>")
    if alerts:
        H.append("<table><tr><th>severity</th><th>workflow</th><th>since</th><th>what to look at</th></tr>" + "".join(
            f"<tr><td><span class='pill {'p-bad' if a['severity'] == 'page' else 'p-warn'}'>{e(a['severity'])}</span></td>"
            f"<td>{e(a['wf'] or '-')}</td><td>{label(a['minute'])}</td><td>{e(a['message'])}</td></tr>" for a in alerts)
                 + "</table>")
    H.append("</div>")
    for w in wfs:
        info, st, cfg = w["info"], w["state"], w["config"] or {}
        obj = info["objective"]
        g = info["governance"]
        aa = g.get("auto_apply") or {}
        H.append(f"<div class=card id='{e(w['workflow'])}'><h2>{e(w['workflow'])} <span class=sub>· {e(w['tenant'])} · "
                 f"{e(info.get('use_case') or '')}</span></h2><div class=sub>primary KPI <b>{e(obj['primary'])}</b> · "
                 f"guardrail {e(json.dumps(obj.get('guardrail')))} · config v{w['version']} · auto-apply "
                 f"{', '.join(aa.get('allowed_dims') or []) if aa.get('enabled') else 'off'} · "
                 f"≤{g.get('max_requests_per_24h')} requests/24h · approvers {g.get('approver_hours')}</div>")
        H.append("<div class=kpis>" + _kpi("pickup rate (run)", w["run"]["pickup"], w["history"]["pickup"])
                 + _kpi("engagement rate (run)", w["run"]["engagement"], w["history"]["engagement"])
                 + f"<div class=kpi><span>leads concluded / uploaded</span><b>{_pct(w['conclusion'])}</b>"
                   f"<span>{w['uploaded']:,} uploaded since D0</span></div>"
                 + f"<div class=kpi><span>today</span><b>{_pct(w['today']['pickup'])} · {_pct(w['today']['engagement'])}</b>"
                   f"<span>pickup · engagement · flags {_pct(w['today']['flag'], 2)}</span></div></div>")
        # ---- what we need from you
        if w["pending"]:
            H.append("<div class='card need'><h3>What we need from you</h3><table><tr><th>request</th><th>status</th>"
                     "<th>impact</th><th>affected leads</th><th>deadline</th></tr>" + "".join(
                f"<tr><td>{e(p['summary'])}<div class=sub>{e(p['proposal_id'])}</div></td><td><span class='pill p-warn'>"
                f"{e(p['status'])}</span></td><td>{(p['impact'] or 0):.2f}</td><td>{p['affected'] or 0:,}</td><td>"
                + (f"applies {label(p['not_before_minute'])} unless you object" if p['status'] == 'notice' else
                   f"apply by {label(p['apply_by_minute'])}" if p['apply_by_minute'] else
                   f"expires {label(p['expires_minute'])}" if p['expires_minute'] else "") + "</td></tr>"
                for p in w["pending"]) + "</table></div>")
        else:
            H.append("<h3>What we need from you</h3><div class=sub>Nothing right now.</div>")
        # ---- what's being tested / winning
        snap = st.get("snapshot") or {}
        dims = snap.get("dims") or {}
        if dims:
            H.append(f"<h3>What's being tested (as of {label(st.get('snapshot_minute', now))})</h3><div class=grid>")
            for d, x in dims.items():
                rows = []
                for a, rr, sd, n in zip(x["arms"], x["global_rr"], x["global_sd"], x["n"]):
                    tag = "control" if a == x["ref"] else ""
                    dec = (st.get("decay") or {}).get(d, {}).get(a)
                    pill = (f"<span class='pill p-bad'>stopped winning</span>" if dec else
                            f"<span class='pill p-info'>control</span>" if tag else
                            f"<span class='pill p-ok'>ahead</span>" if rr - 1.645 * sd > 1 else
                            f"<span class='pill p-bad'>behind</span>" if rr + 1.645 * sd < 1 else "")
                    rows.append(f"<tr><td><b>{e(a)}</b></td><td>{rr:.3f} <span class=sub>±{1.645 * sd:.3f}</span></td>"
                                f"<td>{n:,.0f}</td><td>{pill}</td></tr>")
                H.append(f"<div><b>{e(d.replace('_', ' '))}</b><table><tr><th>arm</th><th>engagement vs control "
                         f"(90% CI)</th><th>connected calls</th><th></th></tr>{''.join(rows)}</table></div>")
            H.append("</div>")
            coh = snap.get("cohorts") or {}
            if coh:
                arms_w, arms_v = dims.get("welcome_message", {}).get("arms", []), dims.get("voice", {}).get("arms", [])
                H.append("<h3>Live allocation by cohort (same cohort ids as the config and the experiments)</h3><table>"
                         "<tr><th>cohort</th><th>share of leads</th><th>welcome</th><th>voice</th></tr>")
                for cid, c in coh.items():
                    if cid == "default" and c.get("share", 0) < 0.005:
                        continue
                    def mix(dim, arms):
                        cur = (c.get(dim) or {}).get("current") or {}
                        return " ".join(f"{a}:{cur.get(a, 0):.0%}" for a in arms if cur.get(a, 0) > 0)
                    H.append(f"<tr><td>{e(c.get('label', cid))}<div class=sub>{e(cid)}</div></td><td>{c.get('share', 0):.0%}"
                             f"</td><td>{e(mix('welcome_message', arms_w))}</td><td>{e(mix('voice', arms_v))}</td></tr>")
                H.append("</table>")
        # ---- what changed today
        H.append(f"<h3>What changed today (D{now // DAY})</h3>")
        if w["changes_today"]:
            H.append("<table><tr><th>time</th><th>version</th><th>by</th><th>what & why</th></tr>" + "".join(
                f"<tr><td>{label(c['minute'])}</td><td>v{c['version']}</td><td>{e(c['actor'])}"
                + (f"<div class=sub>{e(c['approval_id'])}</div>" if c['approval_id'] else "")
                + f"</td><td>{e(', '.join(json.loads(c['changed_dims'] or '[]')))} — {e(c['summary'] or '')}</td></tr>"
                for c in w["changes_today"]) + "</table>")
        else:
            H.append("<div class=sub>No changes today.</div>")
        # ---- caller IDs
        did = st.get("did") or {}
        if did:
            mixd = ((cfg.get("allocations") or {}).get("default") or {}).get("caller_id") or {}
            tot = sum(mixd.values()) or 1
            H.append("<h3>Caller IDs</h3><table><tr><th>number</th><th>state</th><th>share</th><th>pickup vs peers</th>"
                     "<th>note</th></tr>" + "".join(
                f"<tr><td>{e(k)}</td><td><span class='pill {'p-ok' if v['state'] == 'healthy' else 'p-bad' if v['state'] == 'quarantined' else 'p-info'}'>"
                f"{e(v['state'])}</span></td><td><div class=bar><i style='width:{100 * mixd.get(k, 0) / tot:.0f}%'></i></div>"
                f"{mixd.get(k, 0) / tot:.0%}</td><td>{(v.get('last') or {}).get('ratio', float('nan')):.2f}×</td>"
                f"<td class=sub>{e(v.get('reason', ''))}</td></tr>" for k, v in did.items()) + "</table>")
        # ---- call timing
        hw = ((cfg.get("schedule") or {}).get("hour_weights") or {})
        if hw:
            hours = sorted({int(h) for t in hw.values() for h in t})
            H.append("<h3>Call timing — who gets dialled first in each hour (weight; 1 = neutral)</h3><table class=heat>"
                     "<tr><th>cohort</th>" + "".join(f"<th>{h}</th>" for h in hours) + "</tr>")
            for cid, t in hw.items():
                cells = []
                for h in hours:
                    v = float(t.get(str(h), 0))
                    a = min(1.0, max(0.0, (v - 0.2) / 2.0))
                    cells.append(f"<td style='background:rgba(59,91,219,{a:.2f});color:{'#fff' if a > .5 else '#1d2330'}'>{v:.1f}</td>")
                H.append(f"<tr><td>{e(cid)}</td>{''.join(cells)}</tr>")
            H.append("</table>")
        # ---- incidents
        if w["incidents"]:
            H.append("<h3>Incidents</h3><table><tr><th>id</th><th>status</th><th>opened</th><th>resolved</th><th>detail</th></tr>")
            for i in w["incidents"]:
                det = json.loads(i["detail"] or "{}")
                txt = (f"{det.get('prompt')} flags {det.get('rate_since', 0):.1%} vs {det.get('baseline_rate', 0):.1%}; "
                       f"fix {det.get('candidate') or det.get('fixed_prompt') or '…'}" if i["type"] == "hallucination"
                       else f"caller IDs {', '.join(det.get('dids', []))} collapsed")
                H.append(f"<tr><td>{e(i['id'])}</td><td><span class='pill {'p-ok' if i['status'] == 'resolved' else 'p-bad'}'>"
                         f"{e(i['status'])}</span></td><td>{label(i['opened_minute'])}</td><td>"
                         f"{label(i['resolved_minute']) if i['resolved_minute'] is not None else '—'}</td><td>{e(txt)}</td></tr>")
            H.append("</table>")
        edits = st.get("supervisor_edits") or []
        if edits:
            H.append("<h3>Your edits (we never overwrite them silently)</h3><ul>" + "".join(
                f"<li>{label(x['minute'])}: v{x['version']} changed {e(', '.join(x['dims']))}"
                + (f" — <i>{e(x['shadow']['message'])}</i>" if x.get('shadow') else "") + "</li>" for x in edits) + "</ul>")
        H.append("</div>")
    H.append("<p class=sub>Rendered from the controller's own store. Ask <code>python -m controller why --workflow X "
             "--at 'D3 14:00'</code> for the full audit trail of any change.</p></main></body></html>")
    return "".join(H)


def metrics_text(path: str) -> str:
    db = _db(path)
    L = []
    for r in db.execute("SELECT name, wf, value FROM metrics m WHERE minute=(SELECT MAX(minute) FROM metrics m2 "
                        "WHERE m2.name=m.name)"):
        L.append(f"controller_{r['name']}{{wf=\"{r['wf'] or ''}\"}} {r['value']}")
    L.append(f"controller_open_alerts {db.execute('SELECT COUNT(*) FROM alerts WHERE resolved_minute IS NULL').fetchone()[0]}")
    return "\n".join(L) + "\n"


def serve(path: str, port: int):
    from .audit import why

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            try:
                if u.path == "/":
                    body, ct = render_html(path), "text/html; charset=utf-8"
                elif u.path == "/api/workflows":
                    body, ct = json.dumps(workflows_json(path), default=str), "application/json"
                elif u.path == "/api/why":
                    body, ct = why(path, q["workflow"], q["at"], as_json=True), "application/json"
                elif u.path == "/metrics":
                    body, ct = metrics_text(path), "text/plain"
                else:
                    self.send_error(404)
                    return
            except Exception as ex:  # read-only view: report, don't crash
                body, ct = json.dumps({"error": str(ex)}), "application/json"
            b = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", ct)
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def log_message(self, *a):
            pass

    print(f"supervisor view on http://127.0.0.1:{port}/")
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
