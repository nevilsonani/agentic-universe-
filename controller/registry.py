"""Workflows, their options, and every config version we have seen.

Config bodies come from GET /workflows/{id}/config (only the current one is served)
and from the configs we ourselves proposed. If two changes land inside one tick
we may miss an intermediate body; analysis does not need it (call events carry
their own variant and cohort), only IPS dry runs do, and they skip such versions.
"""
from __future__ import annotations

import json

from .settings import AGENT_DIMS


class WorkflowInfo:
    def __init__(self, idx: int, info: dict):
        self.idx = idx
        self.info = info
        self.id: str = info["workflow_id"]
        self.tenant: str = info["tenant_id"]
        self.options: dict[str, list[str]] = {d: list(v) for d, v in info["options"].items()}
        self.opt_index = {d: {o: i for i, o in enumerate(v)} for d, v in self.options.items()}
        self.gov: dict = info["governance"]
        self.objective: dict = info["objective"]
        self.window: tuple[int, int] = tuple(info["dialling_window"])
        self.mid_flight: str = info.get("mid_flight", "allowed")
        self.dph: int = info["dials_per_hour"]
        self.cadence: list[int] = info["cadence"]
        self.batch_hour: int = info["daily_batch"]["at_hour"]
        self.batch_size: int = info["daily_batch"]["size"]
        self.prompt_catalog: dict[str, str] = info.get("prompt_catalog", {})

    @property
    def primary(self) -> str:
        return self.objective["primary"]

    @property
    def guardrail(self) -> tuple[str, float]:
        g = self.objective.get("guardrail") or {}
        for k, v in g.items():
            return k.replace("_max_drop", ""), float(v)
        return ("pickup_rate" if self.primary == "engagement_rate" else "engagement_rate"), 0.02

    def auto_dims(self) -> set[str]:
        aa = self.gov.get("auto_apply") or {}
        return set(aa.get("allowed_dims") or []) if aa.get("enabled") else set()

    @property
    def max_shift(self) -> float:
        aa = self.gov.get("auto_apply") or {}
        return float(aa.get("max_shift") or 0.0) if aa.get("enabled") else 0.0


class Registry:
    def __init__(self, store):
        self.store = store
        self.wfs: dict[str, WorkflowInfo] = {}
        self.by_idx: dict[int, WorkflowInfo] = {}
        self.configs: dict[str, dict[int, dict]] = {}
        self.current_version: dict[str, int] = {}
        self.version_meta: dict[str, dict[int, dict]] = {}
        for wf, idx, tenant, info in store.q("SELECT wf, idx, tenant, info FROM workflows ORDER BY idx"):
            self._add(idx, json.loads(info))
        for wf, ver, actor, minute, dims, pid, aid, cfg in store.q(
                "SELECT wf, version, actor, minute, changed_dims, proposal_id, approval_id, config "
                "FROM config_versions ORDER BY wf, version"):
            self.version_meta.setdefault(wf, {})[ver] = {"actor": actor, "minute": minute,
                                                         "changed_dims": json.loads(dims or "[]"),
                                                         "proposal_id": pid, "approval_id": aid}
            if cfg:
                self.configs.setdefault(wf, {})[ver] = json.loads(cfg)
            self.current_version[wf] = max(self.current_version.get(wf, 0), ver)

    def _add(self, idx: int, info: dict) -> WorkflowInfo:
        w = WorkflowInfo(idx, info)
        self.wfs[w.id] = w
        self.by_idx[idx] = w
        return w

    def register(self, info: dict) -> WorkflowInfo:
        if info["workflow_id"] in self.wfs:
            w = self.wfs[info["workflow_id"]]
            w.info = info
            return w
        idx = len(self.wfs)
        self.store.db.execute("INSERT OR REPLACE INTO workflows(wf, idx, tenant, info) VALUES(?,?,?,?)",
                              (info["workflow_id"], idx, info["tenant_id"], json.dumps(info)))
        return self._add(idx, info)

    def record_version(self, wf: str, version: int, actor: str, minute: int | None, changed_dims=None,
                       proposal_id=None, approval_id=None, config: dict | None = None):
        m = self.version_meta.setdefault(wf, {})
        prev = m.get(version, {})
        rec = {"actor": actor or prev.get("actor"), "minute": minute if minute is not None else prev.get("minute"),
               "changed_dims": changed_dims if changed_dims is not None else prev.get("changed_dims", []),
               "proposal_id": proposal_id or prev.get("proposal_id"),
               "approval_id": approval_id or prev.get("approval_id")}
        m[version] = rec
        if config is not None:
            self.configs.setdefault(wf, {})[version] = config
        cfg = config if config is not None else self.configs.get(wf, {}).get(version)
        self.store.db.execute(
            "INSERT INTO config_versions(wf, version, actor, minute, changed_dims, proposal_id, approval_id, config) "
            "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(wf, version) DO UPDATE SET actor=excluded.actor, "
            "minute=excluded.minute, changed_dims=excluded.changed_dims, proposal_id=excluded.proposal_id, "
            "approval_id=excluded.approval_id, config=COALESCE(excluded.config, config_versions.config)",
            (wf, version, rec["actor"], rec["minute"], json.dumps(rec["changed_dims"]), rec["proposal_id"],
             rec["approval_id"], json.dumps(cfg) if cfg is not None else None))
        self.current_version[wf] = max(self.current_version.get(wf, 0), version)

    def config(self, wf: str, version: int | None = None) -> dict | None:
        v = self.current_version.get(wf) if version is None else version
        return self.configs.get(wf, {}).get(v)

    def option_code(self, wf: WorkflowInfo, dim: str, name: str) -> int:
        return wf.opt_index[dim].get(name, 15)


def dist(cfg: dict, cohort: str, dim: str) -> dict[str, float]:
    """Effective distribution for a cohort/dim (falls back to default, like the dialler)."""
    a = cfg.get("allocations") or {}
    return (a.get(cohort) or {}).get(dim) or a["default"][dim]


def norm(d: dict[str, float]) -> dict[str, float]:
    s = float(sum(v for v in d.values() if v > 0))
    return {k: v / s for k, v in d.items() if v > 0} if s > 0 else {}


def tv(a: dict, b: dict) -> float:
    a, b = norm(a), norm(b)
    return 0.5 * sum(abs(a.get(o, 0) - b.get(o, 0)) for o in set(a) | set(b))


def changed_dims(old: dict, new: dict) -> list[str]:
    """Same definition the platform uses for governance (docs §4.2)."""
    ch = []
    if (old.get("cohorts") or []) != (new.get("cohorts") or []):
        ch.append("cohorts")
    keys = set(old["allocations"]) | set(new["allocations"])
    for d in AGENT_DIMS:
        if any(not _same(dist(old, k, d), dist(new, k, d)) for k in keys):
            ch.append(d)
    if old.get("prompt_version") != new.get("prompt_version"):
        ch.append("prompt_version")
    if _sched(old) != _sched(new):
        ch.append("schedule")
    order = ["cohorts", "welcome_message", "voice", "caller_id", "prompt_version", "schedule"]
    return [d for d in order if d in ch]


def _same(a: dict, b: dict) -> bool:
    a, b = norm(a), norm(b)
    return set(a) == set(b) and all(abs(a[k] - b[k]) < 1e-9 for k in a)


def _sched(cfg):
    s = cfg.get("schedule") or {}
    hw = {k: {str(h): float(w) for h, w in v.items()} for k, v in (s.get("hour_weights") or {}).items()}
    return hw, dict(s.get("lead_preferred_hour") or {})


def max_shift(old: dict, new: dict) -> float:
    keys = {"default"} | {c["id"] for c in old.get("cohorts") or []} | {c["id"] for c in new.get("cohorts") or []}
    worst = 0.0
    for k in keys:
        for d in AGENT_DIMS:
            worst = max(worst, tv(dist(old, k, d), dist(new, k, d)))
    return worst
