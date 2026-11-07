"""Event ingestion: chunks → columnar aggregates + call log, exactly once.

Each chunk is processed inside ONE SQLite transaction that also advances the
cursor (`meta.cursor`). A kill before COMMIT rolls everything back and the chunk is
re-read on restart; a kill after COMMIT means it is never read again. Chunks are
immutable and numbered, so this is idempotent by construction (C1, C13).

Aggregates are kept as "cell tables": integer keys (day, hour, config version,
cohort, prompt, feature-tuple, variant…) → counts. They are small (~10⁵ rows per
workflow over a run) and are what every model reads; raw calls are only written
to the `calls` table for audit ("why did this customer hear V3 on D4?").
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field

import numpy as np

from .features import FeatureSpace, ts_to_minute
from .registry import Registry

log = logging.getLogger("controller.ingest")

DAY_OFF = 64  # days are stored offset so history (negative days) packs into unsigned bits


class CellTable:
    """Append-only aggregate table keyed by packed integer keys."""

    def __init__(self, name: str, keys: list[tuple[str, int]], vals: list[str]):
        self.name = name
        self.key_names = [k for k, _ in keys]
        self.bits = [b for _, b in keys]
        # first key is the most significant
        self.shifts = np.array([sum(self.bits[i + 1:]) for i in range(len(self.bits))], dtype=np.int64)
        assert sum(self.bits) <= 62, name
        self.val_names = vals
        self.n = 0
        self._cap = 1024
        self.K = np.zeros((self._cap, len(keys)), dtype=np.int64)
        self.V = np.zeros((self._cap, len(vals)), dtype=np.int64)
        self.index: dict[int, int] = {}

    def pack(self, cols: list[np.ndarray]) -> np.ndarray:
        out = np.zeros(len(cols[0]), dtype=np.int64)
        for c, b, s, nm in zip(cols, self.bits, self.shifts, self.key_names):
            c = np.asarray(c, dtype=np.int64)
            if len(c) and (c.min() < 0 or c.max() >= (1 << b)):
                raise ValueError(f"{self.name}.{nm} out of range [{c.min()}, {c.max()}] for {b} bits")
            out |= c << s
        return out

    def add(self, cols: list[np.ndarray], vals: list[np.ndarray]) -> tuple[np.ndarray, np.ndarray]:
        """Aggregate (cols → vals) into the table. Returns (unique key rows, summed vals) for persistence."""
        if len(cols[0]) == 0:
            return np.zeros((0, len(self.key_names)), np.int64), np.zeros((0, len(self.val_names)), np.int64)
        packed = self.pack(cols)
        uniq, inv = np.unique(packed, return_inverse=True)
        V = np.zeros((len(uniq), len(vals)), dtype=np.int64)
        for j, v in enumerate(vals):
            V[:, j] = np.bincount(inv, weights=np.asarray(v, dtype=np.float64), minlength=len(uniq)).round().astype(np.int64)
        K = np.stack([(uniq >> s) & ((1 << b) - 1) for b, s in zip(self.bits, self.shifts)], axis=1)
        self._merge(uniq, K, V)
        return K, V

    def _merge(self, uniq, K, V):
        rows = np.empty(len(uniq), dtype=np.int64)
        new = []
        idx = self.index
        for i, u in enumerate(uniq.tolist()):
            r = idx.get(u)
            if r is None:
                new.append(i)
                rows[i] = -1
            else:
                rows[i] = r
        if new:
            need = self.n + len(new)
            if need > self._cap:
                while self._cap < need:
                    self._cap *= 2
                K2 = np.zeros((self._cap, self.K.shape[1]), dtype=np.int64)
                V2 = np.zeros((self._cap, self.V.shape[1]), dtype=np.int64)
                K2[: self.n], V2[: self.n] = self.K[: self.n], self.V[: self.n]
                self.K, self.V = K2, V2
            new = np.array(new)
            rr = np.arange(self.n, self.n + len(new))
            self.K[rr] = K[new]
            rows[new] = rr
            for u, r in zip(uniq[new].tolist(), rr.tolist()):
                idx[u] = r
            self.n += len(new)
        np.add.at(self.V, rows, V)

    def load(self, K: np.ndarray, V: np.ndarray):
        if len(K) == 0:
            return
        cols = [K[:, j] for j in range(K.shape[1])]
        packed = self.pack(cols)
        self._merge(packed, K.astype(np.int64), V.astype(np.int64))

    def col(self, name: str) -> np.ndarray:
        if name in self.key_names:
            return self.K[: self.n, self.key_names.index(name)]
        return self.V[: self.n, self.val_names.index(name)]


def _tables() -> dict[str, CellTable]:
    return {
        "eng": CellTable("eng", [("day", 7), ("ver", 11), ("cohort", 10), ("prompt", 6), ("tup", 16), ("wm", 4),
                                 ("voice", 4)], ["conn", "eng", "flag"]),
        "ptime": CellTable("ptime", [("day", 7), ("hour", 5), ("tup", 16), ("att", 3)], ["dials", "conn"]),
        "pdid": CellTable("pdid", [("day", 7), ("hour", 5), ("ver", 11), ("cohort", 10), ("did", 4)],
                          ["dials", "conn"]),
        "hq": CellTable("hq", [("day", 7), ("hour", 5), ("prompt", 6)], ["conn", "flag", "eng"]),
    }


SQL_UPSERT = {
    "eng": ("INSERT INTO eng(wfi,day,ver,cohort,prompt,tup,wm,voice,conn,eng,flag) VALUES(?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT DO UPDATE SET conn=conn+excluded.conn, eng=eng+excluded.eng, flag=flag+excluded.flag"),
    "ptime": ("INSERT INTO ptime(wfi,day,hour,tup,att,dials,conn) VALUES(?,?,?,?,?,?,?) "
              "ON CONFLICT DO UPDATE SET dials=dials+excluded.dials, conn=conn+excluded.conn"),
    "pdid": ("INSERT INTO pdid(wfi,day,hour,ver,cohort,did,dials,conn) VALUES(?,?,?,?,?,?,?,?) "
             "ON CONFLICT DO UPDATE SET dials=dials+excluded.dials, conn=conn+excluded.conn"),
    "hq": ("INSERT INTO hq(wfi,day,hour,prompt,conn,flag,eng) VALUES(?,?,?,?,?,?,?) "
           "ON CONFLICT DO UPDATE SET conn=conn+excluded.conn, flag=flag+excluded.flag, eng=eng+excluded.eng"),
}


class LeadIndex:
    """lead number → (feature tuple, person number, campaign number, concluded). Dense numpy arrays."""

    def __init__(self):
        self.tup = np.full(1024, -1, dtype=np.int32)
        self.person = np.full(1024, -1, dtype=np.int64)
        self.campaign = np.full(1024, -1, dtype=np.int32)
        self.minute = np.zeros(1024, dtype=np.int32)
        self.concluded = np.zeros(1024, dtype=bool)
        self.n = 0

    def _grow(self, m: int):
        if m <= len(self.tup):
            return
        cap = len(self.tup)
        while cap < m:
            cap *= 2
        for nm, fill in (("tup", -1), ("person", -1), ("campaign", -1), ("minute", 0), ("concluded", False)):
            a = getattr(self, nm)
            b = np.full(cap, fill, dtype=a.dtype)
            b[: len(a)] = a
            setattr(self, nm, b)

    def add(self, lead, person, campaign, tup, minute):
        lead = np.asarray(lead, dtype=np.int64)
        if not len(lead):
            return
        self._grow(int(lead.max()) + 1)
        self.tup[lead], self.person[lead], self.campaign[lead], self.minute[lead] = tup, person, campaign, minute
        self.n = max(self.n, int(lead.max()) + 1)


@dataclass
class WfDelta:
    """What one chunk changed for one workflow, handed to detectors inside the same transaction."""
    calls: int = 0
    connected_flags: np.ndarray | None = None   # hallucination flags of connected calls, in event order
    connected_prompt: np.ndarray | None = None
    connected_minute: np.ndarray | None = None
    max_minute: int = -10 ** 9


@dataclass
class ChunkResult:
    chunk: int
    kind: str
    events: int
    calls: int
    seconds: float
    max_minute: int
    per_wf: dict[str, WfDelta] = field(default_factory=dict)
    ctl: list[dict] = field(default_factory=list)


def _num(s: str) -> int:
    """'nw_renewal:l00012345' → 12345. IDs are opaque; we only rely on the trailing number being unique per workflow."""
    return int(s[s.rindex(":") + 2:])


class Ingestor:
    def __init__(self, store, registry: Registry, fs: FeatureSpace, store_calls: bool = True):
        self.store = store
        self.reg = registry
        self.fs = fs
        self.store_calls = store_calls
        self.tables: dict[str, dict[str, CellTable]] = {}
        self.leads: dict[str, LeadIndex] = {}
        self.campaign_day: dict[str, dict[int, int]] = {}
        self.cohort_codes = store.vocab("cohort")
        self.prompt_codes = store.vocab("prompt")
        self.hooks = []          # callables(ChunkResult) run inside the chunk transaction
        self.stats = {"events": 0, "calls": 0, "seconds": 0.0, "chunks": 0}

    # ------------------------------------------------------------------ codes
    def cohort_code(self, name: str) -> int:
        c = self.cohort_codes.get(name)
        if c is None:
            c = len(self.cohort_codes)
            self.cohort_codes[name] = c
            self.store.add_vocab("cohort", name, c)
        return c

    def prompt_code(self, name: str) -> int:
        c = self.prompt_codes.get(name)
        if c is None:
            c = len(self.prompt_codes)
            self.prompt_codes[name] = c
            self.store.add_vocab("prompt", name, c)
        return c

    def cohort_name(self, code: int) -> str:
        return next((n for n, c in self.cohort_codes.items() if c == code), str(code))

    def prompt_name(self, code: int) -> str:
        return next((n for n, c in self.prompt_codes.items() if c == code), str(code))

    def t(self, wf: str) -> dict[str, CellTable]:
        if wf not in self.tables:
            self.tables[wf] = _tables()
        return self.tables[wf]

    def li(self, wf: str) -> LeadIndex:
        if wf not in self.leads:
            self.leads[wf] = LeadIndex()
        return self.leads[wf]

    # ------------------------------------------------------------------ restore after restart
    def restore(self):
        t0 = time.time()
        for w in self.reg.wfs.values():
            tabs = self.t(w.id)
            for name, tab in tabs.items():
                cols = ", ".join(tab.key_names + tab.val_names)
                rows = self.store.q(f"SELECT {cols} FROM {name} WHERE wfi=?", (w.idx,))
                if rows:
                    arr = np.array(rows, dtype=np.int64)
                    k = len(tab.key_names)
                    tab.load(arr[:, :k], arr[:, k:])
            rows = self.store.q("SELECT lead, person, campaign, tup, minute FROM leads WHERE wfi=?", (w.idx,))
            if rows:
                a = np.array(rows, dtype=np.int64)
                self.li(w.id).add(a[:, 0], a[:, 1], a[:, 2], a[:, 3], a[:, 4])
            conc = self.store.q("SELECT lead FROM calls WHERE wfi=? AND (outcome & 2)", (w.idx,))
            if conc:
                L = self.li(w.id)
                L.concluded[np.array([r[0] for r in conc])] = True
            self.campaign_day[w.id] = {n: d for n, d in self.store.q(
                "SELECT num, day FROM campaigns WHERE wfi=?", (w.idx,))}
        log.info("restored aggregates in %.1fs", time.time() - t0)

    # ------------------------------------------------------------------ one chunk
    def ingest(self, n: int, kind: str, data: bytes) -> ChunkResult:
        t0 = time.time()
        loads = json.loads
        calls, leads, camps, ctl = [], [], [], []
        ev = 0
        for line in data.split(b"\n"):
            if not line:
                continue
            ev += 1
            d = loads(line)
            t = d["type"]
            if t == "call":
                v = d["variant"]
                q = d.get("qa")
                calls.append((d["workflow_id"], d["event_id"], d["ts"], d["lead_id"], d["person_id"],
                              d["attempt_no"], d["campaign_day_index"], d["config_version"], d["cohort_id"],
                              v["welcome_message"], v["voice"], v["caller_id"], v["prompt_version"],
                              d["connected"], d["engaged"], bool(q and q.get("hallucination_flag"))))
            elif t == "lead":
                leads.append((d["workflow_id"], d["lead_id"], d["person_id"], d["campaign_id"], d["features"],
                              d["ts"]))
            elif t == "campaign_created":
                camps.append(d)
            else:
                ctl.append(d)
        res = ChunkResult(n, kind, ev, len(calls), 0.0, -10 ** 9)
        db = self.store.db
        with self.store.tx():
            self._leads(leads)
            self._campaigns(camps)
            self._calls(calls, res)
            for d in ctl:
                m = ts_to_minute(d["ts"])
                db.execute("INSERT OR IGNORE INTO ctl_events(eid, minute, wf, type, body) VALUES(?,?,?,?,?)",
                           (d["event_id"], m, d.get("workflow_id"), d["type"], json.dumps(d)))
                res.ctl.append(d)
                res.max_minute = max(res.max_minute, m)
            for h in self.hooks:
                h(res)
            self.store.set_meta("cursor", n)
            self.store.set_meta("last_event_minute", res.max_minute)
        res.seconds = time.time() - t0
        self.stats["events"] += ev
        self.stats["calls"] += len(calls)
        self.stats["seconds"] += res.seconds
        self.stats["chunks"] += 1
        return res

    def _leads(self, leads):
        if not leads:
            return
        by: dict[str, list] = {}
        for wf, lid, pid, cid, feats, ts in leads:
            by.setdefault(wf, []).append((_num(lid), _num(pid), _num(cid), self.fs.encode(feats), ts_to_minute(ts)))
        rows = []
        for wf, lst in by.items():
            w = self.reg.wfs[wf]
            a = np.array(lst, dtype=np.int64)
            self.li(wf).add(a[:, 0], a[:, 1], a[:, 2], a[:, 3], a[:, 4])
            rows.extend((w.idx, *r) for r in lst)
        self.store.db.executemany("INSERT INTO leads(wfi, lead, person, campaign, tup, minute) VALUES(?,?,?,?,?,?)",
                                  rows)

    def _campaigns(self, camps):
        for d in camps:
            w = self.reg.wfs[d["workflow_id"]]
            m = ts_to_minute(d["ts"])
            num = _num(d["campaign_id"])
            day = m // 1440
            self.campaign_day.setdefault(w.id, {})[num] = day
            self.store.db.execute(
                "INSERT OR IGNORE INTO campaigns(wfi, num, campaign_id, minute, day, leads, cfg_version) "
                "VALUES(?,?,?,?,?,?,?)", (w.idx, num, d["campaign_id"], m, day, d["leads"],
                                          d.get("config_version_at_creation")))

    def _calls(self, calls, res: ChunkResult):
        if not calls:
            return
        by: dict[str, list] = {}
        for c in calls:
            by.setdefault(c[0], []).append(c)
        db = self.store.db
        mcache: dict[str, int] = {}
        for wf, lst in by.items():
            w = self.reg.wfs[wf]
            oi = w.opt_index
            wm_i, vo_i, did_i = oi["welcome_message"], oi["voice"], oi["caller_id"]
            cols = list(zip(*lst))
            eid = np.array(cols[1], dtype=np.int64)
            minute = np.array([mcache.get(s) if s in mcache else mcache.setdefault(s, ts_to_minute(s))
                               for s in cols[2]], dtype=np.int64)
            lead = np.array([_num(s) for s in cols[3]], dtype=np.int64)
            person = np.array([_num(s) for s in cols[4]], dtype=np.int64)
            att = np.array(cols[5], dtype=np.int64)
            cday = np.array(cols[6], dtype=np.int64)
            ver = np.array(cols[7], dtype=np.int64)
            coh = np.array([self.cohort_code(s) for s in cols[8]], dtype=np.int64)
            wm = np.array([wm_i.get(s, 15) for s in cols[9]], dtype=np.int64)
            vo = np.array([vo_i.get(s, 15) for s in cols[10]], dtype=np.int64)
            did = np.array([did_i.get(s, 15) for s in cols[11]], dtype=np.int64)
            pr = np.array([self.prompt_code(s) for s in cols[12]], dtype=np.int64)
            conn = np.array(cols[13], dtype=bool)
            eng = np.array(cols[14], dtype=bool)
            flag = np.array(cols[15], dtype=bool)
            L = self.li(wf)
            tup = L.tup[lead] if L.n else np.full(len(lead), -1)
            tup = np.where(tup < 0, 0, tup)
            L.concluded[lead[eng]] = True
            day = minute // 1440 + DAY_OFF
            hour = (minute % 1440) // 60
            tabs = self.t(wf)
            c = conn
            out = {}
            out["eng"] = tabs["eng"].add([day[c], ver[c], coh[c], pr[c], tup[c], wm[c], vo[c]],
                                         [np.ones(c.sum()), eng[c], flag[c]])
            out["ptime"] = tabs["ptime"].add([day, hour, tup, np.minimum(att, 4)], [np.ones(len(att)), conn])
            out["pdid"] = tabs["pdid"].add([day, hour, ver, coh, did], [np.ones(len(att)), conn])
            out["hq"] = tabs["hq"].add([day[c], hour[c], pr[c]], [np.ones(c.sum()), flag[c], eng[c]])
            for name, (K, V) in out.items():
                if len(K):
                    db.executemany(SQL_UPSERT[name], [(w.idx, *k, *v) for k, v in zip(K.tolist(), V.tolist())])
            if self.store_calls:
                outcome = conn.astype(np.int64) | (eng.astype(np.int64) << 1) | (flag.astype(np.int64) << 2)
                db.executemany(
                    "INSERT INTO calls(wfi,eid,minute,lead,person,attempt,cday,ver,cohort,wm,voice,did,prompt,"
                    "outcome,tup) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    zip([w.idx] * len(eid), eid.tolist(), minute.tolist(), lead.tolist(), person.tolist(),
                        att.tolist(), cday.tolist(), ver.tolist(), coh.tolist(), wm.tolist(), vo.tolist(),
                        did.tolist(), pr.tolist(), outcome.tolist(), tup.tolist()))
            order = np.argsort(eid[c], kind="stable")
            res.per_wf[wf] = WfDelta(calls=len(eid), connected_flags=flag[c][order], connected_prompt=pr[c][order],
                                     connected_minute=minute[c][order], max_minute=int(minute.max()))
            res.max_minute = max(res.max_minute, int(minute.max()))
