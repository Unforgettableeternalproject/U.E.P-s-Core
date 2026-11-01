"""
整合審查修補測試：episodic 圖存檔容錯、EvidenceStore 讀取快取、
矛盾建邊例外回滾、來源缺漏退回 legacy_unknown。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from echo_memory.dream.contradiction import ContradictionDetector, ContradictionResult
from echo_memory.schemas.concept import Concept
from echo_memory.schemas.episode import Episode
from echo_memory.storage.evidence import EvidenceStore, build_legacy_units
from echo_memory.storage.episodic import EpisodicMemory
from echo_memory.storage.graph import MemoryGraph
from echo_memory.storage.semantic import SemanticMemory
from echo_memory.utils.source_view import SUPPORT_LEGACY, support_source, units_for


def _ep(i: int, session: str = "s1") -> Episode:
    return Episode(
        id=f"ep_t{i}", user_input=f"u{i}", agent_response=f"a{i}", session_id=session,
        content_summary=f"c{i}", embedding=[0.1 * (i + 1), 0.2, 0.3],
        timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=i),
    )


# ─── episodic：圖存檔失敗 ───────────────────────────────────────────────


class TestEpisodicGraphSaveFailure:
    def test_write_survives_graph_save_failure_and_heals(self, tmp_path, monkeypatch):
        mem = EpisodicMemory(storage_path=tmp_path)
        mem.write(_ep(0))

        real_save = mem.graph.save
        monkeypatch.setattr(mem.graph, "save", lambda: (_ for _ in ()).throw(OSError("disk")))
        assert mem.write(_ep(1)) == "ep_t1"      # 不拋例外
        assert mem._graph_dirty is True
        assert mem.get("ep_t1") is not None

        # 下一次 write：重載 episodes 後缺節點的 ep_t1 被補回，圖存檔成功後 dirty 清除
        monkeypatch.setattr(mem.graph, "save", real_save)
        mem.write(_ep(2))
        assert mem._graph_dirty is False
        for n in (0, 1, 2):
            assert mem.graph.has_node(f"ep_t{n}")
        assert mem.graph.has_edge("ep_t0", "ep_t1")
        assert mem.graph.has_edge("ep_t1", "ep_t2")

    def test_reload_repairs_missing_node_and_precedes(self, tmp_path, monkeypatch):
        mem = EpisodicMemory(storage_path=tmp_path)
        mem.write(_ep(0))
        monkeypatch.setattr(mem.graph, "save", lambda: (_ for _ in ()).throw(OSError("disk")))
        mem.write(_ep(1))

        fresh = EpisodicMemory(storage_path=tmp_path)   # 磁碟上的圖缺 ep_t1
        assert fresh.graph.has_node("ep_t1")
        assert fresh.graph.has_edge("ep_t0", "ep_t1")
        # 冪等：再載入一次不重複、不報錯
        fresh._load(reset=True)
        assert fresh.graph.has_edge("ep_t0", "ep_t1")

    def test_non_write_save_still_raises(self, tmp_path, monkeypatch):
        mem = EpisodicMemory(storage_path=tmp_path)
        mem.write(_ep(0))
        monkeypatch.setattr(mem.graph, "save", lambda: (_ for _ in ()).throw(OSError("disk")))
        with pytest.raises(OSError):
            mem._save()


# ─── EvidenceStore 讀取快取 ─────────────────────────────────────────────


def _units(ep_id: str):
    return build_legacy_units(
        episode_id=ep_id, user_input="u", agent_response="a",
        occurred_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


class TestEvidenceCache:
    def test_repeated_reads_do_not_reparse(self, tmp_path, monkeypatch):
        store = EvidenceStore(tmp_path)
        us = _units("e1")
        store.append(us)
        calls = {"n": 0}
        real = store._read_raw
        monkeypatch.setattr(store, "_read_raw", lambda: (calls.__setitem__("n", calls["n"] + 1), real())[1])
        ep = SimpleNamespace(id="e1", evidence_ids=[u.id for u in us])

        store.units_for_episodes([ep])
        store.units_for_episodes([ep])
        store.get([us[0].id])
        store.get_by_episode("e1")
        assert calls["n"] == 1

    def test_append_and_redact_invalidate_cache(self, tmp_path):
        store = EvidenceStore(tmp_path)
        a = _units("e1")
        store.append(a)
        assert len(store.get_by_episode("e1")) == len(a)
        b = _units("e2")
        store.append(b)
        assert len(store.get_by_episode("e2")) == len(b)
        assert store.count() == len(a) + len(b)

        store.redact([a[0].id])
        got = store.get([a[0].id])[0]
        assert got.tombstoned_at is not None and got.content == ""

    def test_external_file_change_invalidates(self, tmp_path):
        store = EvidenceStore(tmp_path)
        store.append(_units("e1"))
        assert store.count() == 2
        other = EvidenceStore(tmp_path)         # 另一個實例（另一個行程）追加
        other.append(_units("e2"))
        assert store.count() == 4

    def test_duplicate_id_still_rejected(self, tmp_path):
        store = EvidenceStore(tmp_path)
        us = _units("e1")
        store.append(us)
        with pytest.raises(ValueError):
            store.append(us)


# ─── contradiction：建邊拋例外 ──────────────────────────────────────────


def test_edge_exception_rolls_back_mark(tmp_path, monkeypatch):
    sem = SemanticMemory(storage_path=tmp_path / "semantic")
    for name in ("甲", "乙"):
        c = Concept(name=name, description=name, embedding=[1.0, 0.0])
        sem._concepts[name] = c
        sem._concepts_by_id[c.id] = c
    sem._save()
    graph = MemoryGraph(storage_path=tmp_path / "graph")
    a, b = sem.get_by_name("甲"), sem.get_by_name("乙")
    detector = ContradictionDetector(semantic=sem, graph=graph, llm_fn=lambda p: "{}")
    monkeypatch.setattr(
        graph, "add_contradicts_edge", lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    assert detector._register_edge(ContradictionResult(a.id, b.id, True, strength=0.5)) is False
    assert a.conflict_count == 0 and b.conflict_count == 0
    assert "has_contradiction" not in a.metadata


# ─── source_view：evidence_ids 取不到 unit ──────────────────────────────


def test_missing_units_fall_back_to_legacy_unknown():
    ep = SimpleNamespace(
        id="e1", user_input="我喜歡茶", agent_response="好", evidence_ids=["gone1", "gone2"],
        timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc), metadata={},
    )
    units = units_for(ep, {"e1": []})
    assert units, "應退回合成的 legacy unit"
    assert support_source([ep], {"e1": []}) == SUPPORT_LEGACY
    # 沒有 evidence_ids 的空清單維持原樣
    ep2 = SimpleNamespace(**{**ep.__dict__, "evidence_ids": []})
    assert units_for(ep2, {"e1": []}) == []
