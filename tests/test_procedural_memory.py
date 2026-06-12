"""
tests/test_procedural_memory.py - 程序記憶測試套件

驗證 Branch 7: feat/procedural-memory 的完整功能。

測試策略：
  - 不依賴真正的嵌入模型或 LLM
  - 使用 mock embed_fn / llm_fn
  - 覆蓋 schema、storage、extractor、retrieval 整合
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest

from echo_memory.schemas.procedure import Procedure
from echo_memory.storage.procedural import ProceduralMemory
from echo_memory.storage.graph import MemoryGraph
from echo_memory.storage.semantic import SemanticMemory
from echo_memory.dream.procedure_extractor import ProcedureExtractor, ProcedureExtractionReport
from echo_memory.schemas.episode import Episode
from echo_memory.schemas.memory_context import MemoryContext


# ─── 測試用 embed_fn ────────────────────────────────────────────────────


def _fake_embed_fn(text: str) -> list[float]:
    """產生可預測的 4 維向量（依文字 hash 生成）"""
    h = hash(text) % 10000
    rng = np.random.RandomState(h)
    vec = rng.randn(4).astype(np.float32)
    norm = np.linalg.norm(vec)
    return (vec / norm).tolist() if norm > 0 else [0.0] * 4


# ─── Procedure Schema ───────────────────────────────────────────────────


class TestProcedureSchema:
    def test_auto_id(self):
        p = Procedure(name="test", description="desc")
        assert p.id.startswith("proc_")
        assert len(p.id) > 5

    def test_roundtrip(self):
        p = Procedure(
            name="部署流程",
            description="標準部署步驟",
            steps=["build", "test", "deploy"],
            domain="coding",
            trigger_patterns=["部署", "上線"],
            success_count=5,
            failure_count=1,
            source_episode_ids=["ep_001"],
            version=2,
            parent_id="proc_old",
        )
        d = p.to_dict()
        p2 = Procedure.from_dict(d)
        assert p2.name == p.name
        assert p2.steps == p.steps
        assert p2.domain == p.domain
        assert p2.success_count == 5
        assert p2.version == 2
        assert p2.parent_id == "proc_old"

    def test_success_rate(self):
        p = Procedure(name="t", description="d", success_count=3, failure_count=1)
        assert p.success_rate == 0.75
        assert p.total_uses == 4

    def test_success_rate_no_uses(self):
        p = Procedure(name="t", description="d")
        assert p.success_rate == 0.0
        assert p.total_uses == 0

    def test_datetime_is_utc(self):
        p = Procedure(name="t", description="d")
        assert p.created_at.tzinfo is not None


# ─── ProceduralMemory Storage ────────────────────────────────────────────


class TestProceduralMemoryStorage:
    @pytest.fixture
    def storage(self, tmp_path: Path) -> ProceduralMemory:
        return ProceduralMemory(
            storage_path=tmp_path / "procedural",
            embed_fn=_fake_embed_fn,
        )

    def test_write_and_get(self, storage: ProceduralMemory):
        proc = Procedure(
            name="code review",
            description="程式碼審查流程",
            steps=["讀 diff", "找問題", "寫評論"],
            domain="coding",
        )
        pid = storage.write(proc)
        assert pid.startswith("proc_")

        got = storage.get(pid)
        assert got is not None
        assert got.name == "code review"
        assert len(got.steps) == 3

    def test_get_by_name(self, storage: ProceduralMemory):
        storage.write(Procedure(name="deploy", description="部署", steps=["a", "b"]))
        got = storage.get_by_name("deploy")
        assert got is not None
        assert got.name == "deploy"

    def test_count(self, storage: ProceduralMemory):
        assert storage.count() == 0
        storage.write(Procedure(name="p1", description="d", steps=["a", "b"]))
        storage.write(Procedure(name="p2", description="d", steps=["c", "d"]))
        assert storage.count() == 2

    def test_write_updates_existing(self, storage: ProceduralMemory):
        storage.write(Procedure(name="flow", description="v1", steps=["old"]))
        storage.write(Procedure(name="flow", description="v2", steps=["new"]))
        assert storage.count() == 1
        got = storage.get_by_name("flow")
        assert got.description == "v2"

    def test_record_success_failure(self, storage: ProceduralMemory):
        proc = Procedure(name="t", description="d", steps=["a", "b"])
        pid = storage.write(proc)
        storage.record_success(pid)
        storage.record_success(pid)
        storage.record_failure(pid)
        got = storage.get(pid)
        assert got.success_count == 2
        assert got.failure_count == 1
        assert got.success_rate == pytest.approx(2 / 3)

    def test_vector_search(self, storage: ProceduralMemory):
        storage.write(Procedure(name="deploy", description="部署到 production", steps=["a", "b"]))
        storage.write(Procedure(name="debug", description="除錯流程", steps=["c", "d"]))

        query_vec = _fake_embed_fn("deploy: 部署到 production. 步驟: a; b")
        results = storage.search_by_vector(query_vec, top_k=2, min_similarity=0.0)
        assert len(results) >= 1
        # 第一個結果應該是 deploy（最相似）
        assert results[0][0].name == "deploy"
        assert results[0][1] > 0.0

    def test_search_excludes_archived(self, storage: ProceduralMemory):
        proc = Procedure(name="old", description="舊的", steps=["a", "b"])
        pid = storage.write(proc)
        storage.get(pid).is_archived = True
        storage._save()

        query_vec = _fake_embed_fn("old: 舊的. 步驟: a; b")
        results = storage.search_by_vector(query_vec, top_k=5, min_similarity=0.0)
        assert all(r[0].name != "old" for r in results)

    def test_search_filter_domain(self, storage: ProceduralMemory):
        storage.write(Procedure(name="code", description="程式", steps=["a", "b"], domain="coding"))
        storage.write(Procedure(name="plan", description="計畫", steps=["c", "d"], domain="planning"))

        query_vec = _fake_embed_fn("anything")
        coding_results = storage.search_by_vector(query_vec, domain="coding", min_similarity=0.0)
        assert all(r[0].domain == "coding" for r in coding_results)

    def test_list_procedures(self, storage: ProceduralMemory):
        storage.write(Procedure(name="a", description="d", steps=["1", "2"], domain="coding"))
        storage.write(Procedure(name="b", description="d", steps=["3", "4"], domain="planning"))

        all_procs = storage.list_procedures()
        assert len(all_procs) == 2

        coding = storage.list_procedures(domain="coding")
        assert len(coding) == 1
        assert coding[0].domain == "coding"

    def test_refine(self, storage: ProceduralMemory):
        proc = Procedure(name="flow", description="v1", steps=["old1", "old2"])
        pid = storage.write(proc)

        new_pid = storage.refine(pid, new_steps=["new1", "new2", "new3"])
        assert new_pid is not None
        assert new_pid != pid

        refined = storage.get(new_pid)
        assert refined.version == 2
        assert refined.parent_id == pid
        assert refined.steps == ["new1", "new2", "new3"]

        original = storage.get(pid)
        assert original.is_archived is True

    def test_persistence_round_trip(self, tmp_path: Path):
        path = tmp_path / "persist_test"
        pm1 = ProceduralMemory(storage_path=path, embed_fn=_fake_embed_fn)
        pm1.write(Procedure(name="test", description="持久化測試", steps=["a", "b"]))
        assert pm1.count() == 1

        # 重新載入
        pm2 = ProceduralMemory(storage_path=path, embed_fn=_fake_embed_fn)
        assert pm2.count() == 1
        got = pm2.get_by_name("test")
        assert got is not None
        assert got.steps == ["a", "b"]

    def test_clear(self, storage: ProceduralMemory):
        storage.write(Procedure(name="t", description="d", steps=["a", "b"]))
        assert storage.count() == 1
        storage.clear()
        assert storage.count() == 0


# ─── ProcedureExtractor ─────────────────────────────────────────────────


class TestProcedureExtractor:
    @pytest.fixture
    def procedural(self, tmp_path: Path) -> ProceduralMemory:
        return ProceduralMemory(storage_path=tmp_path / "procedural", embed_fn=_fake_embed_fn)

    @pytest.fixture
    def graph(self, tmp_path: Path) -> MemoryGraph:
        return MemoryGraph(storage_path=tmp_path / "graph")

    def _make_episode(self, user: str = "user", agent: str = "agent") -> Episode:
        return Episode(
            user_input=user,
            agent_response=agent,
            session_id="session",
            content_summary=f"{user[:20]}...",
            salience_score=0.8,
        )

    def test_no_llm_skips(self, procedural: ProceduralMemory):
        extractor = ProcedureExtractor(procedural=procedural, llm_fn=None)
        episodes = [self._make_episode()]
        report = extractor.run(episodes)
        assert report.skipped_reason == "no_llm"
        assert report.procedures_created == 0

    def test_no_episodes_skips(self, procedural: ProceduralMemory):
        mock_llm = MagicMock(return_value='{"found": false}')
        extractor = ProcedureExtractor(procedural=procedural, llm_fn=mock_llm)
        report = extractor.run([])
        assert report.skipped_reason == "no_episodes"

    def test_llm_found_false(self, procedural: ProceduralMemory):
        mock_llm = MagicMock(return_value='{"found": false}')
        extractor = ProcedureExtractor(procedural=procedural, llm_fn=mock_llm)
        report = extractor.run([self._make_episode()])
        assert report.procedures_created == 0
        assert report.skipped_reason is None

    def test_llm_creates_procedure(self, procedural: ProceduralMemory, graph: MemoryGraph):
        response = json.dumps({
            "found": True,
            "name": "部署流程",
            "description": "標準部署步驟",
            "domain": "coding",
            "steps": ["build", "test", "deploy"],
            "trigger_patterns": ["部署", "上線"],
            "source_indices": [0],
        })
        mock_llm = MagicMock(return_value=response)
        extractor = ProcedureExtractor(
            procedural=procedural,
            graph=graph,
            llm_fn=mock_llm,
        )
        report = extractor.run([self._make_episode()])
        assert report.procedures_created == 1
        assert procedural.count() == 1
        proc = procedural.get_by_name("部署流程")
        assert proc is not None
        assert proc.steps == ["build", "test", "deploy"]
        assert proc.domain == "coding"

    def test_llm_error_degrades(self, procedural: ProceduralMemory):
        mock_llm = MagicMock(side_effect=Exception("LLM timeout"))
        extractor = ProcedureExtractor(procedural=procedural, llm_fn=mock_llm)
        report = extractor.run([self._make_episode()])
        assert report.skipped_reason == "llm_error"
        assert len(report.errors) == 1

    def test_steps_less_than_2_rejected(self, procedural: ProceduralMemory):
        response = json.dumps({
            "found": True,
            "name": "短流程",
            "description": "只有一步",
            "steps": ["唯一步驟"],
        })
        mock_llm = MagicMock(return_value=response)
        extractor = ProcedureExtractor(procedural=procedural, llm_fn=mock_llm)
        report = extractor.run([self._make_episode()])
        assert report.procedures_created == 0  # 被拒絕


# ─── Graph 整合 ──────────────────────────────────────────────────────────


class TestGraphProcedure:
    @pytest.fixture
    def graph(self, tmp_path: Path) -> MemoryGraph:
        return MemoryGraph(storage_path=tmp_path / "graph")

    def test_add_procedure_node(self, graph: MemoryGraph):
        graph.add_procedure_node("proc_001", "deploy", "coding")
        assert graph.has_node("proc_001")
        data = graph.graph.nodes["proc_001"]
        assert data["node_type"] == "Procedure"
        assert data["domain"] == "coding"

    def test_applies_to_edge(self, graph: MemoryGraph):
        graph.add_procedure_node("proc_001", "deploy", "coding")
        graph.add_concept_node("con_001", "CI/CD", "abstract")
        graph.add_applies_to_edge("proc_001", "con_001", weight=0.8)

        assert graph.graph.has_edge("proc_001", "con_001")
        edge_data = graph.graph["proc_001"]["con_001"]
        assert edge_data["edge_type"] == "APPLIES_TO"
        assert edge_data["weight"] == 0.8


# ─── MemoryContext 整合 ──────────────────────────────────────────────────


class TestMemoryContextProcedures:
    def test_procedures_field_exists(self):
        ctx = MemoryContext()
        assert ctx.procedures == []

    def test_to_text_includes_procedures(self):
        proc = Procedure(
            name="部署流程",
            description="標準部署步驟",
            steps=["build", "test", "deploy"],
            success_count=5,
            failure_count=1,
        )
        ctx = MemoryContext(procedures=[proc])
        text = ctx.to_text()
        assert "部署流程" in text
        assert "SOP" in text or "程序" in text
        assert "build" in text

    def test_is_empty_with_only_procedures(self):
        proc = Procedure(name="t", description="d", steps=["a", "b"])
        ctx = MemoryContext(procedures=[proc])
        assert not ctx.is_empty()


# ─── ProceduralMemory Stats ──────────────────────────────────────────────


class TestProceduralMemoryStats:
    def test_get_stats(self, tmp_path: Path):
        pm = ProceduralMemory(storage_path=tmp_path / "pm", embed_fn=_fake_embed_fn)
        pm.write(Procedure(name="a", description="d", steps=["1", "2"], domain="coding"))
        pm.write(Procedure(name="b", description="d", steps=["3", "4"], domain="planning"))

        stats = pm.get_stats()
        assert stats["total_procedures"] == 2
        assert stats["active_procedures"] == 2
        assert "coding" in stats["domain_distribution"]
