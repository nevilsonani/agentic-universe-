"""End-to-end: a real simulator process, the real controller, a shortened public_02.

Checks the invariants SRE and Compliance care about, not KPIs:
  * the run finishes; every controller-made config version maps to exactly one of our proposals;
  * nothing is left in flight, nothing was refused for policy reasons;
  * after a restart in the middle, every call event is ingested exactly once and the run completes.
Run: python -m pytest -q tests/test_controller_e2e.py   (~1–2 min)
"""
import json
import sqlite3
from pathlib import Path

import httpx
import pytest
import yaml

from controller.loop import Controller
from tools.simctl import Sim

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def short_scenario(tmp_path):
    sc = yaml.safe_load((ROOT / "scenarios/public_02_incidents.yaml").read_text())
    sc["name"] = "e2e_short"
    sc["days"], sc["history_days"] = 3, 4
    sc["incidents"] = [i for i in sc["incidents"] if i["at"].startswith(("D0", "D1", "D2"))]
    p = tmp_path / "short.yaml"
    p.write_text(yaml.safe_dump(sc))
    return str(p)


def _platform_calls(url: str) -> int:
    c = httpx.Client(base_url=url, timeout=300)
    n = 0
    for kind in ("history", "events"):
        for ch in c.get("/events/chunks", params={"kind": kind}).json():
            n += c.get(f"/events/chunks/{ch['chunk']}").text.count('"type":"call"')
    return n


def _check_invariants(db_path: str, url: str):
    db = sqlite3.connect(db_path)
    c = httpx.Client(base_url=url, timeout=60)
    assert c.get("/sim/state").json()["finished"]
    stuck = db.execute("SELECT proposal_id, status FROM proposals WHERE status IN ('sending','applying')").fetchall()
    assert not stuck, stuck
    refused = db.execute("SELECT proposal_id, reasons FROM proposals WHERE status='failed'").fetchall()
    assert not refused, refused
    ours = {r[0]: r for r in db.execute("SELECT proposal_id, status, applied_version FROM proposals")}
    for wf in [w["workflow_id"] for w in c.get("/workflows").json()]:
        hist = c.get(f"/workflows/{wf}/config/history").json()
        ctrl = [h for h in hist if h["actor"].startswith("controller")]
        pids = [h["proposal_id"] for h in ctrl]
        assert len(pids) == len(set(pids)), f"{wf}: a proposal was applied twice"
        for h in ctrl:
            row = ours.get(h["proposal_id"])
            assert row is not None and row[1] == "applied" and row[2] == h["version"], (wf, h, row)
    ingested = db.execute("SELECT COUNT(*) FROM calls").fetchone()[0]
    assert ingested == _platform_calls(url), "every call event exactly once"


def test_full_run_holds_invariants(short_scenario, tmp_path):
    with Sim(short_scenario, str(tmp_path / "sim")) as sim:
        out = Controller(sim.url, str(tmp_path / "state.db"), view_path=str(tmp_path / "view.html")).run()
        assert out["finished"]
        _check_invariants(str(tmp_path / "state.db"), sim.url)
        assert (tmp_path / "view.html").read_text(encoding="utf-8").startswith("<!doctype html>")
        db = sqlite3.connect(tmp_path / "state.db")
        kinds = {r[0] for r in db.execute("SELECT DISTINCT kind FROM decisions")}
        assert {"bootstrap", "analysis", "applied"} <= kinds


def test_restart_mid_run_resumes_exactly_once(short_scenario, tmp_path):
    db = str(tmp_path / "state.db")
    with Sim(short_scenario, str(tmp_path / "sim")) as sim:
        first = Controller(sim.url, db, max_ticks=70).run()
        assert not first["finished"]
        # a brand-new process image of the controller on the same store (what a restart is)
        out = Controller(sim.url, db).run()
        assert out["finished"]
        _check_invariants(db, sim.url)
        n = sqlite3.connect(db).execute("SELECT COUNT(*) FROM decisions WHERE kind='controller_restarted'").fetchone()[0]
        assert n == 1
