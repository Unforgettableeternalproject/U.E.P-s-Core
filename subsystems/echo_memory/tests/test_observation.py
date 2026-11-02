"""
平台觀察（observation）與活動彙整測試（M-8）

驗收重點：記錄與冪等；不建 episode、不推進 neurochem；can_attest_* 為 False；
retrieve_evidence 專用取回；prompt 不含 observation；彙整累加不重複；
排序；forget_activity 遮蔽；重啟後資料仍在；namespace 隔離。
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from echo_memory import MemoryEngine
from echo_memory.schemas.evidence import (
    SOURCE_TYPES,
    EvidenceInput,
    can_attest_agent_said,
    can_attest_user_claim,
)

T0 = datetime(2026, 5, 1, 10, 0, tzinfo=timezone.utc)


class _NoopExtractor:
    calls = 0

    def extract(self, episode):
        type(self).calls += 1
        return []


@pytest.fixture
def engine(tmp_path: Path) -> MemoryEngine:
    eng = MemoryEngine(storage_path=tmp_path, namespace_id="u1")
    eng._entity_extractor = _NoopExtractor()
    _NoopExtractor.calls = 0
    return eng


def _obs(eng, game="Minecraft", start=T0, minutes=60, **kw):
    return eng.record_observation(
        kind="game_activity", subject=game, started_at=start,
        ended_at=start + timedelta(minutes=minutes), **kw,
    )


def test_observation_is_valid_source_type():
    assert "observation" in SOURCE_TYPES


def test_record_basic_and_no_episode(engine: MemoryEngine):
    da = engine._neurochem.dopamine
    eid = _obs(engine, attrs={"app_id": "123", "subject": "忽略"})
    unit = engine.evidence.get([eid])[0]
    assert unit.source_type == "observation"
    assert unit.episode_id is None
    assert unit.source_ref.kind == "discord_presence"
    assert unit.attrs["subject"] == "Minecraft"  # 核心欄位優先
    assert unit.attrs["app_id"] == "123"
    assert unit.attrs["duration_minutes"] == 60
    assert engine.episodic.count() == 0
    assert _NoopExtractor.calls == 0
    assert engine._neurochem.dopamine == da


def test_cannot_attest(engine: MemoryEngine):
    unit = engine.evidence.get([_obs(engine)])[0]
    assert not can_attest_user_claim(unit)
    assert not can_attest_agent_said(unit)


def test_validation(engine: MemoryEngine):
    with pytest.raises(ValueError):
        engine.record_observation(
            kind="game_activity", subject="X", started_at=T0, ended_at=T0 - timedelta(seconds=1))
    with pytest.raises(ValueError):
        engine.record_observation(kind="bogus", subject="X", started_at=T0, ended_at=T0)
    with pytest.raises(ValueError):
        engine.record_observation(kind="game_activity", subject="  ", started_at=T0, ended_at=T0)
    with pytest.raises(ValueError):
        _obs(engine, idempotency_key=" ")
    assert engine.evidence.count() == 0
    # 零時長可接受（只驗 ended_at >= started_at）
    assert engine.record_observation(
        kind="game_activity", subject="X", started_at=T0, ended_at=T0)
    assert engine.evidence.count() == 1


def test_idempotent_same_key(engine: MemoryEngine):
    first = _obs(engine, idempotency_key="u1|Minecraft|t0")
    second = _obs(engine, idempotency_key="u1|Minecraft|t0")
    assert first == second
    assert engine.evidence.count() == 1
    prof = engine.activity_profile()
    assert prof[0]["play_count"] == 1
    assert prof[0]["total_minutes"] == 60


def test_idempotent_after_index_lost(engine: MemoryEngine):
    first = _obs(engine, idempotency_key="k")
    (engine.storage_path / "idempotency.json").unlink()
    assert _obs(engine, idempotency_key="k") == first
    assert engine.evidence.count() == 1
    assert engine.activity_profile()[0]["play_count"] == 1


def test_retry_after_crash_before_aggregate(engine: MemoryEngine):
    """證據已寫、彙整未完成：同 key 重試補一次，不重複。"""
    eid = _obs(engine, idempotency_key="k")
    engine.activity.clear("game_activity")
    (engine.storage_path / "idempotency.json").unlink()
    assert _obs(engine, idempotency_key="k") == eid
    assert engine.activity_profile()[0]["play_count"] == 1
    assert _obs(engine, idempotency_key="k") == eid
    assert engine.activity_profile()[0]["play_count"] == 1


def test_concurrent_same_key(engine: MemoryEngine):
    ids: list[str] = []
    ts = [threading.Thread(target=lambda: ids.append(_obs(engine, idempotency_key="k")))
          for _ in range(6)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert len(set(ids)) == 1
    assert engine.evidence.count() == 1
    assert engine.activity_profile()[0]["play_count"] == 1


def test_retrieve_evidence_observation_only(engine: MemoryEngine):
    a = _obs(engine, "Minecraft", T0)
    b = _obs(engine, "Celeste", T0 + timedelta(days=1))
    got = engine.retrieve_evidence(source_types=["observation"])
    assert [u.id for u in got] == [b, a]  # 最新在前
    assert len(engine.retrieve_evidence(source_types=["observation"], top_k=1)) == 1
    with pytest.raises(ValueError):
        engine.retrieve_evidence()


def test_observation_not_in_conversation_paths(engine: MemoryEngine):
    _obs(engine, "Minecraft")
    ep_id = engine.store_episode("你好", "嗨", "s1", salience=0.9)
    # episode_ids 路徑只展開 Episode 證據
    units = engine.retrieve_evidence(episode_ids=[ep_id])
    assert units and all(u.source_type in ("user", "agent") for u in units)
    ctx = engine.retrieve(engine.episodic.get(ep_id).content_summary, top_k=5, min_similarity=0.3)
    assert ctx.episodes
    assert "Minecraft" not in ctx.to_text()
    for evs in ctx.evidence_by_episode.values():
        assert all(u.source_type != "observation" for u in evs)
    assert engine.episodic.count() == 1


def test_aggregate_accumulates_case_insensitive(engine: MemoryEngine):
    _obs(engine, "Minecraft", T0, 30, idempotency_key="a")
    _obs(engine, "  minecraft ", T0 + timedelta(days=1), 90, idempotency_key="b")
    _obs(engine, "Minecraft", T0 + timedelta(days=1), 90, idempotency_key="b")  # 重送
    prof = engine.activity_profile()
    assert len(prof) == 1
    assert prof[0]["name"] == "Minecraft"
    assert prof[0]["play_count"] == 2
    assert prof[0]["total_minutes"] == 120
    assert prof[0]["last_played"].startswith("2026-05-02T11:30")
    entry = engine.activity.list("game_activity")[0]
    assert entry["first_seen"].startswith("2026-05-01T10:00")
    assert len(entry["evidence_ids"]) == 2


def test_evidence_ids_capped(tmp_path: Path):
    from echo_memory.storage.activity import ActivityStore
    store = ActivityStore(tmp_path, max_evidence_ids=3)
    for i in range(5):
        store.apply("game_activity", "G", minutes=1, started_at=T0, ended_at=T0, evidence_id=f"e{i}")
    entry = store.list("game_activity")[0]
    assert entry["evidence_ids"] == ["e2", "e3", "e4"]
    assert entry["play_count"] == 5


def test_profile_ordering(engine: MemoryEngine):
    _obs(engine, "A", T0, 30)
    _obs(engine, "B", T0, 120)
    _obs(engine, "C", T0 + timedelta(days=2), 30)  # 與 A 同分，較近期
    names = [p["name"] for p in engine.activity_profile(top_n=5)]
    assert names == ["B", "C", "A"]
    assert len(engine.activity_profile(top_n=1)) == 1
    assert engine.activity_profile(top_n=0) == []


def test_forget_activity(engine: MemoryEngine):
    _obs(engine, "A", idempotency_key="a")
    _obs(engine, "B", T0 + timedelta(days=1), idempotency_key="b")
    engine.store_episode("你好", "嗨", "s1", salience=0.9)
    assert engine.forget_activity() == 2
    assert engine.activity_profile() == []
    assert engine.retrieve_evidence(source_types=["observation"]) == []
    # 遮蔽後讀取：內容清空
    shell = engine.evidence.list(source_type="observation")[0]
    assert shell.content == "" and shell.tombstoned_at is not None
    # 同 key 重送不復活；對話證據不受影響
    _obs(engine, "A", idempotency_key="a")
    assert engine.activity_profile() == []
    assert engine.retrieve_evidence(source_types=["observation"]) == []
    assert engine.episodic.count() == 1
    # 退出後可重新累積（新 key）
    _obs(engine, "A", T0 + timedelta(days=5), idempotency_key="a2")
    assert engine.activity_profile()[0]["play_count"] == 1
    assert engine.forget_activity() == 1
    assert engine.forget_activity() == 0
    # 原始檔仍 append-only：observation 2 + 1 筆 + 對話 2 筆都還在
    lines = (engine.storage_path / "evidence" / "evidence.jsonl").read_text("utf-8").splitlines()
    assert len(lines) == 5


def test_forget_survives_restart(tmp_path: Path):
    e1 = MemoryEngine(storage_path=tmp_path, namespace_id="u1")
    _obs(e1, "A", idempotency_key="a")
    e1.forget_activity()
    e2 = MemoryEngine(storage_path=tmp_path, namespace_id="u1")
    assert e2.activity_profile() == []
    assert e2.retrieve_evidence(source_types=["observation"]) == []


def test_persist_across_restart(tmp_path: Path):
    e1 = MemoryEngine(storage_path=tmp_path, namespace_id="u1")
    eid = _obs(e1, "Minecraft", idempotency_key="a")
    e2 = MemoryEngine(storage_path=tmp_path, namespace_id="u1")
    assert e2.activity_profile()[0]["total_minutes"] == 60
    assert [u.id for u in e2.retrieve_evidence(source_types=["observation"])] == [eid]
    assert _obs(e2, "Minecraft", idempotency_key="a") == eid  # 重啟後仍去重
    assert e2.activity_profile()[0]["play_count"] == 1


def test_namespace_isolation(tmp_path: Path):
    a = MemoryEngine(storage_path=tmp_path, namespace_id="alice")
    b = MemoryEngine(storage_path=tmp_path, namespace_id="bob")
    _obs(a, "Minecraft", idempotency_key="k")
    assert b.activity_profile() == []
    assert b.retrieve_evidence(source_types=["observation"]) == []
    _obs(b, "Celeste", idempotency_key="k")  # 同 key 不同 namespace 互不影響
    assert [p["name"] for p in a.activity_profile()] == ["Minecraft"]
    assert b.forget_activity() == 1
    assert [p["name"] for p in a.activity_profile()] == ["Minecraft"]


def test_corrupt_activity_file_recovers(engine: MemoryEngine):
    _obs(engine, "A")
    (engine.storage_path / "activity.json").write_text("{bad", encoding="utf-8")
    assert engine.activity_profile() == []
    _obs(engine, "B", T0 + timedelta(days=1))
    assert [p["name"] for p in engine.activity_profile()] == ["B"]


def test_evidence_input_observation_roundtrip():
    inp = EvidenceInput(source_type="observation", content="x")
    unit = inp.to_unit(unit_id="u", episode_id=None, recorded_at=T0)
    assert unit.source_type == "observation"
    assert json.loads(json.dumps(unit.to_dict()))["episode_id"] is None
