"""
tests/test_m9b_bypass.py — M-9b：M3 遺留繞過收尾

1. 矛盾標記經 ConceptInhibitor（mark_conflict patch）：留 audit、可 rollback、version 遞增
2. MemoryGraph.save() 失敗不再被吞掉：呼叫端看得見，semantic 合併失敗仍補償
3. Judge force_remember 標記改用 judge_force_remember，舊鍵仍可讀
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from echo_memory.affect.concept_inhibitor import ConceptInhibitor
from echo_memory.affect.concept_patch import ConceptPatch
from echo_memory.affect.judge import (
    JUDGE_FORCE_REMEMBER_KEY,
    is_judge_force_remember,
)
from echo_memory.dream.contradiction import ContradictionDetector
from echo_memory.dream.patch_coordinator import DreamPatchCoordinator
from echo_memory.schemas.concept import Concept
from echo_memory.storage.graph import MemoryGraph
from echo_memory.storage.semantic import SemanticMemory


@pytest.fixture
def semantic(tmp_path: Path) -> SemanticMemory:
    sem = SemanticMemory(storage_path=tmp_path / "semantic")
    for name, desc in [("運動有益", "適度運動有益健康"), ("運動有害", "運動會損害健康")]:
        c = Concept(name=name, description=desc, embedding=[1.0, 0.0])
        sem._concepts[name] = c
        sem._concepts_by_id[c.id] = c
    sem._save()
    return sem


@pytest.fixture
def graph(tmp_path: Path) -> MemoryGraph:
    return MemoryGraph(storage_path=tmp_path / "graph")


def _pair(semantic: SemanticMemory) -> tuple[Concept, Concept]:
    return semantic.get_by_name("運動有益"), semantic.get_by_name("運動有害")


def _audit_lines(semantic: SemanticMemory) -> list[dict]:
    path = semantic.storage_path / "audit_log.jsonl"
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


# ─── 1. mark_conflict ────────────────────────────────────────────────────────


class TestConflictMarkPatch:
    def test_mark_applies_with_audit_and_version_bump(self, semantic):
        a, b = _pair(semantic)
        v_a, v_b = a.version, b.version
        inhibitor = ConceptInhibitor(semantic=semantic, llm_fn=None)

        ok, tx = inhibitor.process_conflict_mark(a.id, b.id, strength=0.9, reason="對立")

        assert ok and tx.status == "committed"
        assert a.conflict_count == 1 and b.conflict_count == 1
        assert a.metadata["has_contradiction"] is True
        assert a.version == v_a + 1 and b.version == v_b + 1
        # patch 帶對手 id 與增量；history 留索引；不新增 derivation
        pa = next(p for p in tx.patches if p.concept_id == a.id)
        assert pa.action == "mark_conflict"
        assert pa.new_value == {"conflict_count_delta": 1, "rival_concept_id": b.id}
        assert a.metadata["patch_history"][-1]["patch_id"] == pa.patch_id
        assert not a.derivations
        # audit：先寫（mutate 前）再寫終態
        lines = [x for x in _audit_lines(semantic) if x["transaction_id"] == tx.transaction_id]
        assert [x["status"] for x in lines] == ["pending", "committed"]
        assert all(p["action"] == "mark_conflict" for p in lines[-1]["patches"])
        # 已持久化
        reloaded = SemanticMemory(storage_path=semantic.storage_path)
        assert reloaded.get_by_name("運動有益").conflict_count == 1

    def test_rollback_restores_and_version_monotonic(self, semantic):
        a, b = _pair(semantic)
        inhibitor = ConceptInhibitor(semantic=semantic, llm_fn=None)
        ok, tx = inhibitor.process_conflict_mark(a.id, b.id)
        assert ok
        v_after_apply = a.version

        inhibitor.rollback_conflict_mark(tx)

        assert a.conflict_count == 0 and b.conflict_count == 0
        assert "has_contradiction" not in a.metadata
        assert a.version == v_after_apply + 1  # rollback 本身也是一次變更，version 不回退
        assert all(p.status == "rolled_back" for p in tx.patches)
        assert _audit_lines(semantic)[-1]["status"] == "rolled_back"

    def test_audit_failure_blocks_mutation(self, semantic, monkeypatch):
        a, b = _pair(semantic)
        inhibitor = ConceptInhibitor(semantic=semantic, llm_fn=None)
        monkeypatch.setattr(inhibitor, "_try_write_audit", lambda tx: False)

        ok, tx = inhibitor.process_conflict_mark(a.id, b.id)

        assert not ok and tx.status == "failed"
        assert a.conflict_count == 0 and b.conflict_count == 0
        assert "has_contradiction" not in a.metadata

    def test_save_failure_rolls_back_in_memory(self, semantic, monkeypatch):
        a, b = _pair(semantic)
        inhibitor = ConceptInhibitor(semantic=semantic, llm_fn=None)
        real_save = semantic._save
        calls = {"n": 0}

        def flaky_save():
            calls["n"] += 1
            if calls["n"] == 2:  # 第二筆 patch 存檔失敗
                raise OSError("disk full")
            return real_save()

        monkeypatch.setattr(semantic, "_save", flaky_save)
        ok, tx = inhibitor.process_conflict_mark(a.id, b.id)

        assert not ok and tx.status == "failed"
        assert a.conflict_count == 0 and b.conflict_count == 0
        assert "has_contradiction" not in a.metadata and "has_contradiction" not in b.metadata

    def test_patch_serialization_roundtrip(self, semantic):
        a, b = _pair(semantic)
        inhibitor = ConceptInhibitor(semantic=semantic, llm_fn=None)
        _ok, tx = inhibitor.process_conflict_mark(a.id, b.id)
        restored = ConceptPatch.from_dict(tx.patches[0].to_dict())
        assert restored.action == "mark_conflict"

    def test_detector_routes_through_gate(self, semantic, graph):
        a, b = _pair(semantic)
        coordinator = DreamPatchCoordinator(semantic=semantic)
        detector = ContradictionDetector(
            semantic=semantic, graph=graph, llm_fn=lambda p: "{}",
            patch_coordinator=coordinator,
        )
        from echo_memory.dream.contradiction import ContradictionResult

        with detector._guard():
            assert detector._register_edge(ContradictionResult(
                a.id, b.id, True, strength=0.8, reason="對立",
            )) is True

        assert graph.has_contradicts_edge(a.id, b.id)
        assert a.conflict_count == 1 and b.conflict_count == 1
        assert any(
            p["action"] == "mark_conflict"
            for line in _audit_lines(semantic) for p in line["patches"]
        )

    def test_detector_without_coordinator_still_uses_gate(self, semantic, graph):
        a, b = _pair(semantic)
        detector = ContradictionDetector(semantic=semantic, graph=graph, llm_fn=lambda p: "{}")
        from echo_memory.dream.contradiction import ContradictionResult

        assert detector._register_edge(ContradictionResult(a.id, b.id, True, strength=0.5))
        assert any(
            p["action"] == "mark_conflict"
            for line in _audit_lines(semantic) for p in line["patches"]
        )

    def test_gate_rejection_creates_no_edge(self, semantic, graph, monkeypatch):
        a, b = _pair(semantic)
        coordinator = DreamPatchCoordinator(semantic=semantic)
        monkeypatch.setattr(coordinator._inhibitor, "_try_write_audit", lambda tx: False)
        detector = ContradictionDetector(
            semantic=semantic, graph=graph, llm_fn=lambda p: "{}",
            patch_coordinator=coordinator,
        )
        from echo_memory.dream.contradiction import ContradictionResult

        assert detector._register_edge(ContradictionResult(a.id, b.id, True, strength=0.5)) is False
        assert not graph.has_contradicts_edge(a.id, b.id)
        assert a.conflict_count == 0

    def test_edge_failure_rolls_back_mark(self, semantic, graph, monkeypatch):
        a, b = _pair(semantic)
        detector = ContradictionDetector(semantic=semantic, graph=graph, llm_fn=lambda p: "{}")
        monkeypatch.setattr(graph, "add_contradicts_edge", lambda **kw: None)
        from echo_memory.dream.contradiction import ContradictionResult

        assert detector._register_edge(ContradictionResult(a.id, b.id, True, strength=0.5)) is False
        assert a.conflict_count == 0 and b.conflict_count == 0
        assert "has_contradiction" not in a.metadata


# ─── 2. graph.save 失敗可見 ──────────────────────────────────────────────────


class TestGraphSaveFailureVisible:
    def test_save_raises_and_cleans_tmp(self, graph, monkeypatch):
        graph.add_concept_node("con_x", "x")

        def boom(*args, **kwargs):
            raise OSError("replace failed")

        monkeypatch.setattr("echo_memory.storage.graph.replace_with_retry", boom)
        with pytest.raises(OSError):
            graph.save()
        leftovers = [p for p in graph.storage_path.iterdir() if p.name != "memory_graph.gpickle"]
        assert leftovers == []

    def test_mark_merged_compensates_when_graph_save_fails(self, semantic, graph, monkeypatch):
        a, b = _pair(semantic)
        graph.add_concept_node(b.id, b.name)
        graph.add_concept_node(a.id, a.name)
        graph.add_relates_to_edge(a.id, b.id, weight=0.5)
        semantic.attach_graph(graph)
        status_before, meta_before = b.status, dict(b.metadata)
        edges_before = graph.graph.number_of_edges()

        def boom():
            raise OSError("graph disk full")

        monkeypatch.setattr(graph, "save", boom)
        with pytest.raises(OSError):
            semantic.mark_merged(b.name, a.id, "patch_x")

        # 記憶體補償：概念還原、圖節點與邊回來
        assert b.status == status_before
        assert "merged_into" not in b.metadata and b.metadata == meta_before
        assert graph.has_node(b.id)
        assert graph.graph.number_of_edges() == edges_before

    def test_remove_concept_surfaces_graph_save_failure(self, semantic, graph, monkeypatch):
        a, _b = _pair(semantic)
        graph.add_concept_node(a.id, a.name)
        semantic.attach_graph(graph)
        monkeypatch.setattr(graph, "save", lambda: (_ for _ in ()).throw(OSError("x")))
        with pytest.raises(OSError):
            semantic.remove_concept(a.name)

    def test_dream_cycle_reports_failure_instead_of_crashing(self, tmp_path, monkeypatch):
        from echo_memory.dream.engine import DreamEngine

        graph = MemoryGraph(storage_path=tmp_path / "g")
        engine = DreamEngine(
            graph=graph, enable_idle=False, enable_cron=False,
        )
        monkeypatch.setattr(graph, "save", lambda: (_ for _ in ()).throw(OSError("disk")))

        report = engine.dream_now(triggered_by="manual")

        assert "disk" in (report.error or "")
        assert engine.is_dreaming() is False


# ─── 3. judge_force_remember ─────────────────────────────────────────────────


class TestJudgeForceRememberKey:
    def test_new_key_name(self):
        assert JUDGE_FORCE_REMEMBER_KEY == "judge_force_remember"

    def test_new_key_readable(self):
        assert is_judge_force_remember({JUDGE_FORCE_REMEMBER_KEY: True}) is True

    def test_legacy_key_still_readable(self):
        assert is_judge_force_remember({"force_remember": True}) is True

    def test_absent_or_false(self):
        assert is_judge_force_remember(None) is False
        assert is_judge_force_remember({}) is False
        assert is_judge_force_remember({"force_remember": False}) is False

    def test_new_key_takes_precedence(self):
        assert is_judge_force_remember(
            {JUDGE_FORCE_REMEMBER_KEY: False, "force_remember": True}
        ) is False

    def test_writers_use_new_key(self):
        root = Path(__file__).resolve().parents[1]
        for rel in ("Entry.py", "scripts/probe_pipeline.py"):
            text = (root / rel).read_text(encoding="utf-8")
            assert 'ep_metadata["force_remember"]' not in text
