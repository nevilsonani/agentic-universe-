"""SQLite store: the controller's only memory.

One file, WAL mode. Every unit of work (ingesting one event chunk, sending one
proposal, applying one approval) commits atomically together with the bookkeeping
that says it happened, so a kill at any instruction leaves the store either before
or after that unit — never half-way. See SUBMISSION.md §9.

Production mapping: the same schema runs on the Postgres we already operate
(`calls` becomes a partitioned table); nothing here needs SQLite-only features
except `INSERT .. ON CONFLICT`, which Postgres has too.
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS workflows(wf TEXT PRIMARY KEY, idx INTEGER UNIQUE, tenant TEXT, info TEXT);
CREATE TABLE IF NOT EXISTS vocab(kind TEXT, name TEXT, code INTEGER, PRIMARY KEY(kind, name));
CREATE TABLE IF NOT EXISTS config_versions(
  wf TEXT, version INTEGER, actor TEXT, minute INTEGER, changed_dims TEXT,
  proposal_id TEXT, approval_id TEXT, config TEXT, PRIMARY KEY(wf, version));
CREATE TABLE IF NOT EXISTS campaigns(
  wfi INTEGER, num INTEGER, campaign_id TEXT, minute INTEGER, day INTEGER, leads INTEGER, cfg_version INTEGER,
  PRIMARY KEY(wfi, num));
CREATE TABLE IF NOT EXISTS leads(wfi INTEGER, lead INTEGER, person INTEGER, campaign INTEGER, tup INTEGER, minute INTEGER);
CREATE TABLE IF NOT EXISTS calls(
  wfi INTEGER, eid INTEGER, minute INTEGER, lead INTEGER, person INTEGER, attempt INTEGER, cday INTEGER,
  ver INTEGER, cohort INTEGER, wm INTEGER, voice INTEGER, did INTEGER, prompt INTEGER, outcome INTEGER, tup INTEGER);
CREATE TABLE IF NOT EXISTS eng(
  wfi INTEGER, day INTEGER, ver INTEGER, cohort INTEGER, prompt INTEGER, tup INTEGER, wm INTEGER, voice INTEGER,
  conn INTEGER, eng INTEGER, flag INTEGER,
  PRIMARY KEY(wfi, day, ver, cohort, prompt, tup, wm, voice)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS ptime(
  wfi INTEGER, day INTEGER, hour INTEGER, tup INTEGER, att INTEGER, dials INTEGER, conn INTEGER,
  PRIMARY KEY(wfi, day, hour, tup, att)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS pdid(
  wfi INTEGER, day INTEGER, hour INTEGER, ver INTEGER, cohort INTEGER, did INTEGER, dials INTEGER, conn INTEGER,
  PRIMARY KEY(wfi, day, hour, ver, cohort, did)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS hq(
  wfi INTEGER, day INTEGER, hour INTEGER, prompt INTEGER, conn INTEGER, flag INTEGER, eng INTEGER,
  PRIMARY KEY(wfi, day, hour, prompt)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS ctl_events(eid INTEGER PRIMARY KEY, minute INTEGER, wf TEXT, type TEXT, body TEXT,
  handled INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS proposals(
  proposal_id TEXT PRIMARY KEY, wf TEXT, kind TEXT, channel TEXT, status TEXT, base_version INTEGER,
  created_minute INTEGER, sent_minute INTEGER, not_before_minute INTEGER, approval_id TEXT,
  decided_minute INTEGER, apply_by_minute INTEGER, expires_minute INTEGER, applied_version INTEGER,
  applied_minute INTEGER, summary TEXT, body TEXT, reasons TEXT, parent TEXT, intents TEXT, note TEXT);
CREATE INDEX IF NOT EXISTS proposals_wf ON proposals(wf, status);
CREATE TABLE IF NOT EXISTS decisions(
  id INTEGER PRIMARY KEY AUTOINCREMENT, minute INTEGER, wall REAL, wf TEXT, kind TEXT, summary TEXT,
  detail TEXT, proposal_id TEXT);
CREATE INDEX IF NOT EXISTS decisions_wf ON decisions(wf, minute);
CREATE TABLE IF NOT EXISTS incidents(
  id TEXT PRIMARY KEY, wf TEXT, type TEXT, status TEXT, opened_minute INTEGER, resolved_minute INTEGER,
  verified_minute INTEGER, detail TEXT);
CREATE TABLE IF NOT EXISTS validations(
  run_id TEXT PRIMARY KEY, wf TEXT, prompt TEXT, n INTEGER, failures INTEGER, pass_rate REAL, minute INTEGER,
  purpose TEXT);
CREATE TABLE IF NOT EXISTS credits(id INTEGER PRIMARY KEY AUTOINCREMENT, minute INTEGER, wf TEXT, tenant TEXT,
  kind TEXT, cost REAL, ref TEXT);
CREATE TABLE IF NOT EXISTS wf_state(wf TEXT PRIMARY KEY, body TEXT);
CREATE TABLE IF NOT EXISTS metrics(minute INTEGER, wall REAL, name TEXT, wf TEXT, value REAL);
CREATE INDEX IF NOT EXISTS metrics_name ON metrics(name, minute);
CREATE TABLE IF NOT EXISTS alerts(
  id INTEGER PRIMARY KEY AUTOINCREMENT, minute INTEGER, severity TEXT, wf TEXT, code TEXT, message TEXT,
  resolved_minute INTEGER);
"""


class Store:
    def __init__(self, path: str | Path, readonly: bool = False):
        self.path = Path(path)
        if not readonly:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        uri = f"file:{self.path.as_posix()}{'?mode=ro' if readonly else ''}"
        self.db = sqlite3.connect(uri, uri=True, isolation_level=None, timeout=60, check_same_thread=False)
        self.db.execute("PRAGMA busy_timeout=60000")
        if not readonly:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=NORMAL")  # durable against process kill; see §9
            self.db.executescript(SCHEMA)
            if self.meta("schema_version") is None:
                self.set_meta("schema_version", SCHEMA_VERSION)
        self._in_tx = False

    # ------------------------------------------------------------------ transactions
    @contextmanager
    def tx(self):
        if self._in_tx:  # nested: join the outer transaction
            yield self.db
            return
        self.db.execute("BEGIN IMMEDIATE")
        self._in_tx = True
        try:
            yield self.db
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        else:
            self.db.execute("COMMIT")
        finally:
            self._in_tx = False

    def q(self, sql: str, args=()) -> list[tuple]:
        return self.db.execute(sql, args).fetchall()

    def q1(self, sql: str, args=()):
        r = self.db.execute(sql, args).fetchone()
        return r[0] if r else None

    # ------------------------------------------------------------------ meta / json blobs
    def meta(self, k: str, default=None):
        v = self.q1("SELECT v FROM meta WHERE k=?", (k,))
        return default if v is None else json.loads(v)

    def set_meta(self, k: str, v):
        self.db.execute("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                        (k, json.dumps(v)))

    def wf_state(self, wf: str) -> dict:
        v = self.q1("SELECT body FROM wf_state WHERE wf=?", (wf,))
        return json.loads(v) if v else {}

    def save_wf_state(self, wf: str, body: dict):
        self.db.execute("INSERT INTO wf_state(wf,body) VALUES(?,?) ON CONFLICT(wf) DO UPDATE SET body=excluded.body",
                        (wf, json.dumps(body, default=_jd)))

    # ------------------------------------------------------------------ vocab
    def vocab(self, kind: str) -> dict[str, int]:
        return {n: c for n, c in self.q("SELECT name, code FROM vocab WHERE kind=?", (kind,))}

    def add_vocab(self, kind: str, name: str, code: int):
        self.db.execute("INSERT OR IGNORE INTO vocab(kind,name,code) VALUES(?,?,?)", (kind, name, code))

    # ------------------------------------------------------------------ audit
    def decide(self, minute: int, wf: str | None, kind: str, summary: str, detail=None,
               proposal_id: str | None = None) -> int:
        cur = self.db.execute(
            "INSERT INTO decisions(minute,wall,wf,kind,summary,detail,proposal_id) VALUES(?,?,?,?,?,?,?)",
            (minute, time.time(), wf, kind, summary, json.dumps(detail, default=_jd) if detail is not None else None,
             proposal_id))
        return cur.lastrowid

    def metric(self, minute: int, name: str, value: float, wf: str | None = None):
        self.db.execute("INSERT INTO metrics(minute,wall,name,wf,value) VALUES(?,?,?,?,?)",
                        (minute, time.time(), name, wf, float(value)))

    def alert(self, minute: int, severity: str, wf: str | None, code: str, message: str):
        open_ = self.q1("SELECT id FROM alerts WHERE wf IS ? AND code=? AND resolved_minute IS NULL", (wf, code))
        if open_:
            return open_
        cur = self.db.execute("INSERT INTO alerts(minute,severity,wf,code,message) VALUES(?,?,?,?,?)",
                              (minute, severity, wf, code, message))
        return cur.lastrowid

    def resolve_alert(self, minute: int, wf: str | None, code: str):
        self.db.execute("UPDATE alerts SET resolved_minute=? WHERE wf IS ? AND code=? AND resolved_minute IS NULL",
                        (minute, wf, code))

    def close(self):
        self.db.close()


def _jd(o):
    import numpy as np
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, np.floating):
        return float(o)
    if isinstance(o, np.ndarray):
        return o.tolist()
    if isinstance(o, set):
        return sorted(o)
    raise TypeError(type(o))
