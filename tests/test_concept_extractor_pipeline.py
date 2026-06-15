"""
tests/test_concept_extractor_pipeline.py — ConceptExtractor Patch Pipeline 測試

驗證 GPT #3: ConceptExtractor 透過 ConceptInhibitor 走 patch pipeline，
不再直接寫入 SemanticMemory。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from echo_memory.affect.concept_extractor import ConceptExtractor
from echo_memory.affect.concept_inhibitor import ConceptInhibitor
from echo_memory.affect.concept_patch import PatchTransaction
from echo_memory.schemas.concept import Concept
from echo_memory.schemas.episode import Episode
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


def _make_episode(ep_id: str = "ep_test001") -> Episode:
    return Episode(
        id=ep_id,
        user_input="我喜歡彈鋼琴",
        agent_response="鋼琴是很美的樂器",
        content_summary="用戶喜歡彈鋼琴",
        session_id="test-session",
        embedding=[0.1] * 10,
    )


def _mock_llm_fn(prompt: str) -> str:
    """模擬 LLM 回傳概念提取結果。"""
    return json.dumps([
        {"noun": "鋼琴", "desc": "一種鍵盤樂器"},
        {"noun": "音樂", "desc": "與樂器演奏相關的藝術形式"},
    ], ensure_ascii=False)


def _mock_embed_fn(text: str) -> list[float]:
    """模擬嵌入函式。根據文本產生不同的假向量。"""
    np.random.seed(hash(text) % 2**31)
    vec = np.random.randn(10).tolist()
    return vec


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# T-06 測試：Pipeline 路徑
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestExtractorPipeline:
    """ConceptExtractor 有 inhibitor 時走 patch pipeline。"""

    def test_new_concept_via_pipeline(
        self, semantic: SemanticMemory, graph: MemoryGraph, inhibitor: ConceptInhibitor,
    ):
        """全新概念走 pipeline create patch。"""
        extractor = ConceptExtractor(
            llm_fn=_mock_llm_fn,
            semantic=semantic,
            graph=graph,
            embed_fn=_mock_embed_fn,
            inhibitor=inhibitor,
        )
        ep = _make_episode()
        # 同步呼叫背景方法（不起 thread，方便測試）
        extractor._extract_concepts_bg(ep, salience=0.5)

        # 應該有概念被建立
        concepts = semantic.list_concepts()
        assert len(concepts) >= 1, "應至少建立一個概念"

    def test_pipeline_creates_audit_log(
        self, semantic: SemanticMemory, graph: MemoryGraph, inhibitor: ConceptInhibitor,
    ):
        """Pipeline 路徑應產生 audit log。"""
        extractor = ConceptExtractor(
            llm_fn=_mock_llm_fn,
            semantic=semantic,
            graph=graph,
            embed_fn=_mock_embed_fn,
            inhibitor=inhibitor,
        )
        ep = _make_episode()
        extractor._extract_concepts_bg(ep, salience=0.5)

        audit_path = semantic.storage_path / "audit_log.jsonl"
        assert audit_path.exists(), "應該產生 audit log"

        lines = audit_path.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) >= 1, "應至少有一筆 audit 記錄"

        # 檢查 triggered_by 標記
        tx_data = json.loads(lines[0])
        assert tx_data["triggered_by"] == "extractor"

    def test_synonym_merge_via_pipeline(
        self, semantic: SemanticMemory, graph: MemoryGraph, inhibitor: ConceptInhibitor,
    ):
        """同義詞合併走 expand patch，不直接改 Concept 物件。"""
        # 先手動建一個概念（帶 embedding）
        existing_emb = np.random.randn(10).tolist()
        semantic.upsert_concept(
            name="鋼琴",
            description="鍵盤樂器",
            source_episode_id="ep_000",
            embedding=existing_emb,
        )

        # 建立 embed_fn：回傳與 existing 幾乎一樣的向量（sim > 0.92）
        def high_sim_embed(text: str) -> list[float]:
            # 加微小噪音，確保 sim > 0.92
            return [v + 0.001 for v in existing_emb]

        extractor = ConceptExtractor(
            llm_fn=_mock_llm_fn,
            semantic=semantic,
            graph=graph,
            embed_fn=high_sim_embed,
            inhibitor=inhibitor,
        )

        ep = _make_episode("ep_merge001")
        extractor._extract_concepts_bg(ep, salience=0.5)

        # 鋼琴概念應只有一個（合併而非新建）
        piano_concepts = [c for c in semantic.list_concepts() if c.name == "鋼琴"]
        assert len(piano_concepts) == 1
        assert "ep_merge001" in piano_concepts[0].source_episode_ids

    def test_polysemy_via_pipeline(
        self, semantic: SemanticMemory, graph: MemoryGraph, inhibitor: ConceptInhibitor,
    ):
        """多義詞走 create patch + graph RELATES_TO 邊。"""
        # 精確控制向量使 cosine sim = 0.88（落在 0.85~0.92 多義詞區間）
        base = [1.0, 0.8, 0.6, 0.4, 0.2, 0.0, -0.2, -0.4, -0.6, -0.8]
        polysemy_vec = [
            0.5293, 0.2251, 0.4569, 0.0724, 0.2506,
            -0.2144, 0.0442, -0.2599, -0.0818, -0.5199,
        ]

        # 用不同名稱的 existing concept（避免 upsert 合併同名概念）
        semantic.upsert_concept(
            name="鍵盤樂器",
            description="包含鋼琴在內的樂器類別",
            source_episode_id="ep_000",
            embedding=base,
        )

        def mid_sim_embed(text: str) -> list[float]:
            return list(polysemy_vec)

        extractor = ConceptExtractor(
            llm_fn=_mock_llm_fn,  # 回傳 "鋼琴" 和 "音樂"
            semantic=semantic,
            graph=graph,
            embed_fn=mid_sim_embed,
            inhibitor=inhibitor,
        )
        ep = _make_episode("ep_poly001")
        extractor._extract_concepts_bg(ep, salience=0.5)

        # 應建立新概念（多義詞 create + 原有的鍵盤樂器）
        concepts = semantic.list_concepts()
        assert len(concepts) >= 2, (
            f"多義詞應建新概念，目前只有 {len(concepts)} 個: "
            f"{[c.name for c in concepts]}"
        )

    def test_no_inhibitor_uses_legacy_path(
        self, semantic: SemanticMemory, graph: MemoryGraph,
    ):
        """無 inhibitor 時走舊路徑（向下相容）。"""
        extractor = ConceptExtractor(
            llm_fn=_mock_llm_fn,
            semantic=semantic,
            graph=graph,
            embed_fn=_mock_embed_fn,
            inhibitor=None,  # 明確不給
        )
        ep = _make_episode()
        extractor._extract_concepts_bg(ep, salience=0.5)

        # 舊路徑也應該建立概念
        concepts = semantic.list_concepts()
        assert len(concepts) >= 1

        # 但不應產生 audit log（舊路徑不走 inhibitor）
        audit_path = semantic.storage_path / "audit_log.jsonl"
        assert not audit_path.exists()

    def test_no_direct_semantic_write_with_inhibitor(
        self, semantic: SemanticMemory, graph: MemoryGraph, inhibitor: ConceptInhibitor,
    ):
        """有 inhibitor 時，ConceptExtractor 不直接呼叫 semantic.upsert_concept 或 _save。"""
        extractor = ConceptExtractor(
            llm_fn=_mock_llm_fn,
            semantic=semantic,
            graph=graph,
            embed_fn=_mock_embed_fn,
            inhibitor=inhibitor,
        )

        # 追蹤：_upsert_legacy 不應被呼叫
        legacy_calls = []
        original_legacy = extractor._upsert_legacy

        def tracked_legacy(*args, **kwargs):
            legacy_calls.append(True)
            return original_legacy(*args, **kwargs)

        extractor._upsert_legacy = tracked_legacy

        ep = _make_episode()
        extractor._extract_concepts_bg(ep, salience=0.5)

        # 有 inhibitor 時，不應走 legacy 路徑
        assert len(legacy_calls) == 0, (
            f"有 inhibitor 時不應走 _upsert_legacy，"
            f"但被呼叫了 {len(legacy_calls)} 次"
        )


class TestExtractorPipelineConcurrency:
    """基本併發安全測試。"""

    def test_concurrent_extractor_calls(
        self, semantic: SemanticMemory, graph: MemoryGraph, inhibitor: ConceptInhibitor,
    ):
        """兩個 thread 同時呼叫 inhibitor，不崩潰。"""
        results: list[tuple[str | None, PatchTransaction]] = []
        errors: list[Exception] = []

        def call_inhibitor(episode_id: str):
            try:
                cid, tx = inhibitor.process_extractor_patch(
                    noun=f"概念_{episode_id}",
                    desc="測試概念",
                    episode_id=episode_id,
                    evidence_kind="extractor_new_concept",
                )
                results.append((cid, tx))
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=call_inhibitor, args=("ep_001",))
        t2 = threading.Thread(target=call_inhibitor, args=("ep_002",))

        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        assert len(errors) == 0, f"併發呼叫產生錯誤: {errors}"
        assert len(results) == 2
        # 兩個概念都應該被建立
        concepts = semantic.list_concepts()
        assert len(concepts) >= 2


class TestProcessExtractorPatch:
    """直接測試 ConceptInhibitor.process_extractor_patch。"""

    def test_create_returns_concept_id(
        self, semantic: SemanticMemory, inhibitor: ConceptInhibitor,
    ):
        """create patch 應回傳有效的 concept_id。"""
        cid, tx = inhibitor.process_extractor_patch(
            noun="測試概念",
            desc="這是測試用的概念",
            episode_id="ep_test",
            evidence_kind="extractor_new_concept",
        )
        assert cid is not None
        assert tx.status == "committed"
        assert tx.triggered_by == "extractor"
        assert len(tx.patches) == 1
        assert tx.patches[0].status == "applied"
        assert tx.patches[0].action == "create"

        # 確認概念在 SemanticMemory 中
        concept = semantic.get(cid)
        assert concept is not None
        assert concept.name == "測試概念"

    def test_merge_returns_existing_id(
        self, semantic: SemanticMemory, inhibitor: ConceptInhibitor,
    ):
        """merge patch 應回傳既有概念的 ID。"""
        # 先建立一個概念
        existing_id = semantic.upsert_concept(
            name="Python",
            description="程式語言",
            source_episode_id="ep_old",
        )

        cid, tx = inhibitor.process_extractor_patch(
            noun="Python",
            desc="程式語言",
            episode_id="ep_new",
            evidence_kind="extractor_synonym_merge",
            similarity_score=0.95,
            existing_concept_id=existing_id,
        )

        assert cid == existing_id
        assert tx.status == "committed"

        # 確認 source_episode_ids 更新
        concept = semantic.get(existing_id)
        assert concept is not None
        assert "ep_new" in concept.source_episode_ids

    def test_merge_missing_concept_fails(
        self, semantic: SemanticMemory, inhibitor: ConceptInhibitor,
    ):
        """merge 時找不到概念應 fail。"""
        cid, tx = inhibitor.process_extractor_patch(
            noun="不存在",
            desc="不存在的概念",
            episode_id="ep_x",
            evidence_kind="extractor_synonym_merge",
            existing_concept_id="non_existent_id",
        )
        assert cid is None
        assert tx.status == "failed"

    def test_evidence_kind_preserved_in_audit(
        self, semantic: SemanticMemory,
    ):
        """audit log 中的 evidence_kind 應正確記錄來源。"""
        storage_path = semantic.storage_path
        inhibitor = ConceptInhibitor(semantic=semantic, llm_fn=None)

        inhibitor.process_extractor_patch(
            noun="概念A",
            desc="描述A",
            episode_id="ep_a",
            evidence_kind="extractor_fallback",
        )

        audit_path = storage_path / "audit_log.jsonl"
        assert audit_path.exists()

        line = audit_path.read_text(encoding="utf-8").strip().split("\n")[0]
        tx_data = json.loads(line)
        assert tx_data["patches"][0]["evidence"]["evidence_kind"] == "extractor_fallback"
