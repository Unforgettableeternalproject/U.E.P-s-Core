"""dream_scanned_at：dream 週期獨立標記「已評估」，與 is_dreamed 分離。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from echo_memory import MemoryEngine
from echo_memory.dream.pruning import GraphPruning
from echo_memory.schemas.episode import Episode

HIGH = 0.9
LOW = 0.1


@pytest.fixture
def engine(tmp_path):
    eng = MemoryEngine(storage_path=tmp_path)
    eng.ensure_initialized()
    return eng


def _seed(eng, n: int, salience: float, prefix: str = "e") -> list[str]:
    ids = []
    for i in range(n):
        ep = Episode(
            user_input=f"{prefix}u{i}", agent_response=f"{prefix}a{i}", session_id="s",
            content_summary=f"{prefix}摘要 {i}", salience_score=salience,
        )
        eng.episodic.prepare_embedding(ep)
        ids.append(eng.episodic.write(ep))
    return ids


def _replay_llm(prompt: str) -> str:
    """replay 回傳反思；蒸餾解析不出概念（保持空結果）。"""
    return json.dumps({"reflections": [
        {"index": i, "reflection": "r", "importance": 0.6, "related_indices": []}
        for i in range(8)
    ]})


def _dream(eng):
    eng.set_llm_fn(_replay_llm)
    report = eng.dream(triggered_by="manual")
    assert report["error"] is None
    return report


def test_empty_distillation_scans_replayed_but_keeps_is_dreamed_false(engine):
    ids = _seed(engine, 3, HIGH)
    _dream(engine)
    for ep_id in ids:
        ep = engine.episodic.get(ep_id)
        assert ep.dream_scanned_at is not None
        assert ep.is_dreamed is False  # 蒸餾空結果須保留重試機會


def test_low_salience_episode_is_scanned_after_cycle(engine):
    low_ids = _seed(engine, 2, LOW, prefix="low")
    _seed(engine, 1, HIGH)
    _dream(engine)
    for ep_id in low_ids:
        ep = engine.episodic.get(ep_id)
        assert ep.dream_scanned_at is not None
        assert ep.is_dreamed is False


def test_consecutive_replays_rotate_candidates(engine):
    ids = _seed(engine, 16, HIGH)
    _dream(engine)
    first = {i for i in ids if engine.episodic.get(i).dream_scanned_at is not None}
    assert len(first) == 8
    _dream(engine)
    second = {
        i for i in ids
        if engine.episodic.get(i).dream_scanned_at is not None and i not in first
    }
    assert len(second) == 8  # 第二輪換成另一批，不重播同一批


def test_scanned_node_below_threshold_is_archived(engine):
    ids = _seed(engine, 1, LOW)
    ep_id = ids[0]
    _dream(engine)
    assert engine.episodic.get(ep_id).dream_scanned_at is not None
    # 讓圖譜節點超過 30 天
    old = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat()
    engine.episodic.graph.graph.nodes[ep_id]["timestamp"] = old
    pruning = GraphPruning(engine.episodic.graph)
    assert pruning._archive_stale_nodes() == 1
    assert engine.episodic.graph.graph.nodes[ep_id]["archived"] is True


def test_legacy_dict_without_field_loads_as_none():
    ep = Episode(user_input="u", agent_response="a", session_id="s", content_summary="c")
    data = ep.to_dict()
    data.pop("dream_scanned_at")
    loaded = Episode.from_dict(data)
    assert loaded.dream_scanned_at is None


def test_scanned_at_roundtrip():
    stamp = datetime.now(timezone.utc)
    ep = Episode(
        user_input="u", agent_response="a", session_id="s", content_summary="c",
        dream_scanned_at=stamp,
    )
    loaded = Episode.from_dict(ep.to_dict())
    assert loaded.dream_scanned_at == stamp


def test_mark_as_scanned_overwrite_flag(engine):
    ids = _seed(engine, 1, HIGH)
    t1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    t2 = datetime(2026, 2, 1, tzinfo=timezone.utc)
    assert engine.episodic.mark_as_scanned(ids, at=t1) == 1
    assert engine.episodic.mark_as_scanned(ids, at=t2, overwrite=False) == 0
    assert engine.episodic.get(ids[0]).dream_scanned_at == t1
    assert engine.episodic.mark_as_scanned(ids, at=t2) == 1
    assert engine.episodic.get(ids[0]).dream_scanned_at == t2
    # 蒸餾候選與 count_pending 口徑不受影響
    assert engine.episodic.count_pending(0.5) == 1
