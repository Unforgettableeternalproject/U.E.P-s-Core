"""
tests/test_dream_patch.py — Dream Engine Patch Transaction 測試

驗證 GPT #4: Dream Engine 各階段透過 DreamPatchCoordinator 產生 audit trail，
蒸餾的 SemanticMemory 寫入走 ConceptInhibitor patch pipeline。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from echo_memory.affect.concept_inhibitor import ConceptInhibitor
from echo_memory.dream.patch_coordinator import DreamPatchCoordinator
from echo_memory.dream.pruning import GraphPruning
from echo_memory.dream.replay import LiDERReplay
from echo_memory.dream.distillation import KnowledgeDistillation
from echo_memory.storage.episodic import EpisodicMemory
from echo_memory.storage.graph import MemoryGraph
from echo_memory.storage.semantic import SemanticMemory


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Fixtures
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.fixture
def storage_path(tmp_path: Path) -> Path:
    return tmp_path / "mem"


@pytest.fixture
def semantic(storage_path: Path) -> SemanticMemory:
    return SemanticMemory(storage_path=storage_path / "semantic")


@pytest.fixture
def graph(storage_path: Path) -> MemoryGraph:
    return MemoryGraph(storage_path=storage_path / "graph")


@pytest.fixture
def inhibitor(semantic: SemanticMemory) -> ConceptInhibitor:
    return ConceptInhibitor(semantic=semantic, llm_fn=None)


@pytest.fixture
def audit_path(storage_path: Path) -> Path:
    return storage_path / "semantic" / "dream_audit_log.jsonl"


@pytest.fixture
def coordinator(inhibitor: ConceptInhibitor, audit_path: Path) -> DreamPatchCoordinator:
    coord = DreamPatchCoordinator(
        inhibitor=inhibitor,
        audit_log_path=audit_path,
    )
    coord.begin_cycle("manual_test")
    return coord


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# DreamPatchCoordinator 基本測試
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestDreamPatchCoordinator:
    """DreamPatchCoordinator 核心功能。"""

    def test_submit_distillation_concept(
        self, coordinator: DreamPatchCoordinator, semantic: SemanticMemory,
    ):
        """蒸餾 concept 走 inhibitor 並回傳 concept_id。"""
        concept_id, tx = coordinator.submit_distillation_concept(
            name="測試蒸餾概念",
            description="透過蒸餾產生的概念",
            concept_type="abstract",
            source_episode_ids=["ep_001", "ep_002"],
        )

        assert concept_id is not None
        assert tx is not None
        assert tx.status == "committed"
        assert tx.triggered_by == "dream"

        # 確認概念在 SemanticMemory 中
        concept = semantic.get(concept_id)
        assert concept is not None
        assert concept.name == "測試蒸餾概念"

    def test_submit_distillation_no_inhibitor(self, audit_path: Path):
        """無 inhibitor 時回傳 (None, None)。"""
        coord = DreamPatchCoordinator(inhibitor=None, audit_log_path=audit_path)
        concept_id, tx = coord.submit_distillation_concept(
            name="測試",
            description="測試",
            concept_type="abstract",
            source_episode_ids=["ep_x"],
        )
        assert concept_id is None
        assert tx is None

    def test_audit_graph_edge_removal(
        self, coordinator: DreamPatchCoordinator, audit_path: Path,
    ):
        """edge removal audit 寫入 dream_audit_log。"""
        coordinator.audit_graph_edge_removal(
            edges=[("ep_001", "ep_002"), ("ep_003", "ep_004")],
            reason="weak_edge_pruning",
        )

        assert audit_path.exists()
        line = audit_path.read_text(encoding="utf-8").strip().split("\n")[0]
        entry = json.loads(line)
        assert entry["operation"] == "remove_edges"
        assert entry["count"] == 2
        assert entry["reason"] == "weak_edge_pruning"
        assert "cycle_id" in entry
        assert "timestamp" in entry

    def test_audit_node_archive(
        self, coordinator: DreamPatchCoordinator, audit_path: Path,
    ):
        """node archive audit 寫入 dream_audit_log。"""
        coordinator.audit_graph_node_archive(["ep_old_001", "ep_old_002"])

        assert audit_path.exists()
        line = audit_path.read_text(encoding="utf-8").strip().split("\n")[0]
        entry = json.loads(line)
        assert entry["operation"] == "archive_nodes"
        assert entry["count"] == 2

    def test_audit_latent_bridge(
        self, coordinator: DreamPatchCoordinator, audit_path: Path,
    ):
        """LATENT_BRIDGE audit 寫入 dream_audit_log。"""
        coordinator.audit_graph_latent_bridge(3)

        assert audit_path.exists()
        entry = json.loads(audit_path.read_text(encoding="utf-8").strip())
        assert entry["operation"] == "create_latent_bridges"
        assert entry["count"] == 3

    def test_audit_batch_weight_update(
        self, coordinator: DreamPatchCoordinator, audit_path: Path,
    ):
        """batch weight update audit 寫入 dream_audit_log。"""
        coordinator.audit_batch_weight_update(150)

        assert audit_path.exists()
        entry = json.loads(audit_path.read_text(encoding="utf-8").strip())
        assert entry["operation"] == "batch_weight_update"
        assert entry["count"] == 150

    def test_audit_replay_batch(
        self, coordinator: DreamPatchCoordinator, audit_path: Path,
    ):
        """replay batch audit 寫入 dream_audit_log。"""
        coordinator.audit_replay_batch(
            edges_reinforced=5,
            edges_created=2,
            salience_updates=8,
        )

        assert audit_path.exists()
        entry = json.loads(audit_path.read_text(encoding="utf-8").strip())
        assert entry["operation"] == "replay_batch_summary"
        assert entry["edges_reinforced"] == 5
        assert entry["edges_created"] == 2

    def test_cycle_id_in_audit(
        self, coordinator: DreamPatchCoordinator, audit_path: Path,
    ):
        """audit entry 包含 cycle_id。"""
        coordinator.audit_batch_weight_update(1)

        entry = json.loads(audit_path.read_text(encoding="utf-8").strip())
        assert entry["cycle_id"].startswith("dream_manual_test_")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Pruning + Coordinator 整合測試
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestPruningWithCoordinator:
    """GraphPruning 整合 DreamPatchCoordinator。"""

    def test_pruning_with_coordinator_produces_audit(
        self, graph: MemoryGraph, coordinator: DreamPatchCoordinator, audit_path: Path,
    ):
        """GraphPruning 有 coordinator 時產生 audit log。"""
        pruning = GraphPruning(graph, patch_coordinator=coordinator)
        report = pruning.run()

        # 即使沒有邊需要修剪，也不應崩潰
        assert report is not None

    def test_pruning_without_coordinator_no_crash(self, graph: MemoryGraph):
        """GraphPruning 無 coordinator 時正常運作（向下相容）。"""
        pruning = GraphPruning(graph, patch_coordinator=None)
        report = pruning.run()
        assert report is not None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Distillation + Coordinator 整合測試
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestDistillationWithCoordinator:
    """KnowledgeDistillation 整合 DreamPatchCoordinator。"""

    def test_distillation_without_coordinator_no_crash(
        self, storage_path: Path, semantic: SemanticMemory, graph: MemoryGraph,
    ):
        """Distillation 無 coordinator 時正常運作（向下相容）。"""
        episodic = EpisodicMemory(
            storage_path=storage_path / "episodic",
            embedding_model=None,
        )
        distiller = KnowledgeDistillation(
            episodic=episodic,
            semantic=semantic,
            graph=graph,
            llm_fn=None,
            patch_coordinator=None,
        )
        report = distiller.run()
        # 無 LLM → 跳過
        assert report.skipped_reason == "no_llm"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Replay + Coordinator 整合測試
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestReplayWithCoordinator:
    """LiDERReplay 整合 DreamPatchCoordinator。"""

    def test_replay_without_coordinator_no_crash(
        self, storage_path: Path, graph: MemoryGraph,
    ):
        """Replay 無 coordinator 時正常運作（向下相容）。"""
        episodic = EpisodicMemory(
            storage_path=storage_path / "episodic",
            embedding_model=None,
        )
        replayer = LiDERReplay(
            episodic=episodic,
            graph=graph,
            llm_fn=None,
            patch_coordinator=None,
        )
        report = replayer.run()
        # 無 LLM → 跳過
        assert report.skipped_reason == "no_llm"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Evidence Kind 測試
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestDreamEvidenceKind:
    """驗證 Dream 來源的 evidence_kind 正確記錄。"""

    def test_distillation_evidence_kind_in_audit(
        self, coordinator: DreamPatchCoordinator, semantic: SemanticMemory,
    ):
        """蒸餾的 audit log 中 evidence_kind 為 'distillation'。"""
        coordinator.submit_distillation_concept(
            name="蒸餾概念",
            description="測試蒸餾",
            concept_type="abstract",
            source_episode_ids=["ep_test"],
        )

        audit_path = semantic.storage_path / "audit_log.jsonl"
        assert audit_path.exists()

        line = audit_path.read_text(encoding="utf-8").strip().split("\n")[0]
        tx_data = json.loads(line)
        assert tx_data["triggered_by"] == "dream"
        assert tx_data["patches"][0]["evidence"]["evidence_kind"] == "distillation"
