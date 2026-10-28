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
from echo_memory.dream.engine import DreamEngine
from echo_memory.dream.patch_coordinator import DreamPatchCoordinator
from echo_memory.dream.pruning import GraphPruning
from echo_memory.dream.replay import LiDERReplay
from echo_memory.dream.distillation import KnowledgeDistillation
from echo_memory.schemas.episode import Episode
from echo_memory.storage.episodic import EpisodicMemory
from echo_memory.storage.graph import MemoryGraph
from echo_memory.storage.procedural import ProceduralMemory
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


def _failing_audit_coordinator(
    audit_path: Path,
    inhibitor: ConceptInhibitor | None = None,
) -> DreamPatchCoordinator:
    """建立 audit 會失敗的 coordinator（audit_path 指向既有目錄）。"""
    audit_path.mkdir(parents=True, exist_ok=True)
    coord = DreamPatchCoordinator(inhibitor=inhibitor, audit_log_path=audit_path)
    coord.begin_cycle("audit_failure_test")
    return coord


def _episodic_with_fixed_embedding(storage_path: Path) -> EpisodicMemory:
    episodic = EpisodicMemory(storage_path=storage_path, embedding_model=None)
    episodic._generate_embedding = lambda _text: [0.1, 0.2, 0.3]
    return episodic


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
        assert concept.source_episode_ids == ["ep_001", "ep_002"]

        audit_path = semantic.storage_path / "audit_log.jsonl"
        tx_data = json.loads(audit_path.read_text(encoding="utf-8").strip())
        assert tx_data["patches"][0]["evidence"]["source_episode_ids"] == [
            "ep_001",
            "ep_002",
        ]

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

    def test_weak_edge_audit_failure_blocks_removal(
        self, storage_path: Path,
    ):
        """弱邊移除必須 audit 成功後才 mutate，且 report 不能誤報。"""
        graph = MemoryGraph(storage_path / "weak_graph")
        graph.graph.add_node("ep_a", node_type="Episode")
        graph.graph.add_node("ep_b", node_type="Episode")
        graph.graph.add_edge("ep_a", "ep_b", weight=0.01)

        pruning = GraphPruning(
            graph,
            patch_coordinator=_failing_audit_coordinator(storage_path / "bad_audit"),
        )
        report = pruning.run()
        reloaded = MemoryGraph(storage_path / "weak_graph")

        assert report.weak_edges_removed == 0
        assert graph.graph.has_edge("ep_a", "ep_b")
        assert reloaded.graph.has_edge("ep_a", "ep_b")

    def test_latent_bridge_audit_failure_blocks_creation(
        self, storage_path: Path,
    ):
        """LATENT_BRIDGE 必須 audit 成功後才建邊。"""
        semantic = SemanticMemory(storage_path=storage_path / "semantic_bridge")
        concept_a = semantic.upsert_concept(
            name="概念A",
            description="A",
            concept_type="abstract",
            embedding=[1.0, 0.0],
        )
        concept_b = semantic.upsert_concept(
            name="概念B",
            description="B",
            concept_type="abstract",
            embedding=[0.99, 0.01],
        )
        graph = MemoryGraph(storage_path / "bridge_graph")
        graph.add_concept_node(concept_a, "概念A", "abstract")
        graph.add_concept_node(concept_b, "概念B", "abstract")

        pruning = GraphPruning(
            graph,
            semantic=semantic,
            patch_coordinator=_failing_audit_coordinator(storage_path / "bad_audit"),
        )
        report = pruning.run()
        reloaded = MemoryGraph(storage_path / "bridge_graph")

        assert report.latent_bridges_created == 0
        assert graph.graph.number_of_edges() == 0
        assert reloaded.graph.number_of_edges() == 0


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

    def test_contradiction_audit_failure_skips_detector_and_edges(
        self, storage_path: Path,
    ):
        """矛盾偵測會改 graph/health/ledger，必須 audit 成功後才呼叫。"""
        semantic = SemanticMemory(storage_path=storage_path / "semantic_contradiction")
        graph = MemoryGraph(storage_path / "contradiction_graph")

        class Detector:
            def __init__(self):
                self.called = False

            def detect_and_register(self, concept):
                self.called = True
                raise AssertionError("audit failure should skip contradiction detector")

        detector = Detector()
        distiller = KnowledgeDistillation(
            episodic=_episodic_with_fixed_embedding(storage_path / "episodic_contradiction"),
            semantic=semantic,
            graph=graph,
            llm_fn=None,
            contradiction_detector=detector,
            patch_coordinator=_failing_audit_coordinator(
                storage_path / "bad_audit",
                ConceptInhibitor(semantic=semantic, llm_fn=None),
            ),
        )

        is_new, edges_created, contradicts_created = distiller._ingest_concept(
            {
                "name": "矛盾概念",
                "description": "測試描述",
                "concept_type": "abstract",
            },
            [
                Episode(
                    user_input="u",
                    agent_response="a",
                    session_id="s",
                    content_summary="c",
                )
            ],
        )

        assert is_new is True
        assert edges_created == 0
        assert contradicts_created == 0
        assert detector.called is False
        assert graph.graph.number_of_edges() == 0


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

    def test_replay_audit_failure_blocks_salience_and_graph_mutation(
        self, storage_path: Path,
    ):
        """Replay 會改 salience/graph，必須 audit 成功後才 mutate。"""
        episodic = _episodic_with_fixed_embedding(storage_path / "episodic_replay")
        episodic.write(Episode(
            user_input="u1",
            agent_response="a1",
            session_id="s",
            content_summary="alpha",
            salience_score=0.9,
        ))
        episodic.write(Episode(
            user_input="u2",
            agent_response="a2",
            session_id="s",
            content_summary="beta",
            salience_score=0.8,
        ))
        before_edges = episodic.graph.graph.number_of_edges()
        before_salience = {
            ep.id: ep.salience_score
            for ep in episodic.list_episodes(limit=10)
        }

        def llm_fn(_prompt: str) -> str:
            return json.dumps({
                "reflections": [{
                    "index": 0,
                    "reflection": "測試反思",
                    "importance": 0.1,
                    "related_indices": [1],
                }]
            }, ensure_ascii=False)

        replayer = LiDERReplay(
            episodic=episodic,
            graph=episodic.graph,
            llm_fn=llm_fn,
            patch_coordinator=_failing_audit_coordinator(storage_path / "bad_audit"),
        )
        report = replayer.run()
        after_salience = {
            ep.id: ep.salience_score
            for ep in episodic.list_episodes(limit=10)
        }

        assert report.skipped_reason == "audit_failure"
        assert report.edges_created == 0
        assert episodic.graph.graph.number_of_edges() == before_edges
        assert after_salience == before_salience


class TestDreamEngineAuditBeforeMutation:
    """DreamEngine 層級的 audit-before-mutate 契約。"""

    def test_procedure_stage_has_no_summary_preaudit(self, storage_path: Path):
        """程序提取不再有數量未知的預 audit；寫入由 write_procedure 逐條 audit 把關（M5-B）。"""
        episodic = _episodic_with_fixed_embedding(storage_path / "episodic_procedure")
        episodic.write(Episode(
            user_input="u",
            agent_response="a",
            session_id="s",
            content_summary="c",
            salience_score=0.9,
        ))
        semantic = SemanticMemory(storage_path=storage_path / "semantic_procedure")
        procedural = ProceduralMemory(storage_path=storage_path / "procedural")
        engine = DreamEngine(
            graph=episodic.graph,
            episodic=episodic,
            semantic=semantic,
            procedural=procedural,
            llm_fn=None,
            concept_inhibitor=None,
        )
        engine._patch_coordinator = _failing_audit_coordinator(storage_path / "bad_audit")
        engine._replay = None
        engine._distillation = None

        class ProcedureExtractor:
            def __init__(self):
                self.called = False

            def run(self, candidates):
                self.called = True
                return None

        extractor = ProcedureExtractor()
        engine._procedure_extractor = extractor

        report = engine.dream_now("audit_failure_test")

        assert report.error is None
        assert extractor.called is True


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
