"""Dream audit log 觸發來源與週期起訖紀錄測試。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from echo_memory.dream.engine import DreamEngine
from echo_memory.dream.patch_coordinator import DreamPatchCoordinator
from echo_memory.storage.graph import MemoryGraph


def _read(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


@pytest.fixture
def audit_path(tmp_path: Path) -> Path:
    return tmp_path / "dream_audit_log.jsonl"


def _engine(tmp_path: Path, audit_path: Path, monkeypatch, lease=None) -> DreamEngine:
    # 固定 dream_* 設定，不依賴本機 .env
    import echo_memory.config as cfg

    for name, value in (
        ("DREAM_ENABLE_IDLE", False),
        ("DREAM_ENABLE_CRON", False),
    ):
        monkeypatch.setattr(cfg, name, value, raising=False)
    eng = DreamEngine(
        MemoryGraph(storage_path=tmp_path / "g"),
        enable_idle=False,
        enable_cron=False,
        lease=lease,
    )
    eng._patch_coordinator = DreamPatchCoordinator(audit_log_path=audit_path)
    return eng


def test_begin_cycle_default_manual_and_mutation_has_triggered_by(audit_path):
    coord = DreamPatchCoordinator(audit_log_path=audit_path)
    coord.begin_cycle()
    assert coord.triggered_by == "manual"
    coord.begin_cycle("idle")
    assert coord.record_mutation("prune", "edge_remove", target={"from": "a", "to": "b"})
    rec = _read(audit_path)[0]
    assert rec["operation"] == "mutation"
    assert rec["triggered_by"] == "idle"
    assert rec["cycle_id"].startswith("dream_idle_")


@pytest.mark.parametrize("trigger", ["idle", "daydream", "manual"])
def test_cycle_start_and_end_written(tmp_path, audit_path, monkeypatch, trigger):
    eng = _engine(tmp_path, audit_path, monkeypatch)
    report = eng.dream_now(triggered_by=trigger)
    assert report.error is None
    entries = _read(audit_path)
    start = [e for e in entries if e["operation"] == "cycle_start"]
    end = [e for e in entries if e["operation"] == "cycle_end"]
    assert len(start) == 1 and len(end) == 1
    assert start[0]["triggered_by"] == end[0]["triggered_by"] == trigger
    assert start[0]["cycle_id"] == end[0]["cycle_id"]
    assert start[0]["pending"] == 0
    assert end[0]["status"] == "completed"
    assert end[0]["episodes_marked"] == 0
    assert entries[0]["operation"] == "cycle_start"
    assert entries[-1]["operation"] == "cycle_end"
    # 週期內其他 audit entry 也帶 triggered_by
    assert all(e["triggered_by"] == trigger for e in entries)


def test_cycle_end_records_error(tmp_path, audit_path, monkeypatch):
    eng = _engine(tmp_path, audit_path, monkeypatch)

    def boom():
        raise RuntimeError("爆炸")

    monkeypatch.setattr(eng._pruning, "update_edge_weights", boom)
    report = eng.dream_now(triggered_by="idle")
    assert report.error
    end = [e for e in _read(audit_path) if e["operation"] == "cycle_end"][0]
    assert end["status"] == "error"
    assert "爆炸" in end["error"]


def test_lease_blocked_cycle_writes_nothing(tmp_path, audit_path, monkeypatch):
    class DenyLease:
        def try_acquire_ex(self, *a, **kw):
            return False, "lease_held", "租約由 pid=1 持有"

    eng = _engine(tmp_path, audit_path, monkeypatch, lease=DenyLease())
    report = eng.dream_now(triggered_by="cron")
    assert report.skipped_reason == "lease_held"
    assert report.skipped_detail
    assert _read(audit_path) == []


def test_rollback_ignores_cycle_records(tmp_path, audit_path, monkeypatch):
    eng = _engine(tmp_path, audit_path, monkeypatch)
    eng.dream_now(triggered_by="manual")
    coord = eng._patch_coordinator
    graph = eng.graph
    g = graph.graph
    g.add_node("x")
    g.add_node("y")
    g.add_edge("x", "y", weight=0.5)
    coord.begin_cycle("manual")
    assert coord.record_mutation(
        "prune", "edge_add", target={"from": "x", "to": "y", "edge_type": "t"}, prior=None,
    )
    monkeypatch.setattr(graph, "save", lambda: None)
    entries = _read(audit_path)
    assert {e["operation"] for e in entries} >= {"cycle_start", "cycle_end", "mutation"}
    mutation = next(e for e in entries if e["operation"] == "mutation")
    assert coord.rollback_mutation(mutation, graph=graph) is True
    assert not g.has_edge("x", "y")
    # rollback 本身寫入的紀錄也帶 triggered_by
    assert _read(audit_path)[-1]["triggered_by"] == "manual"
