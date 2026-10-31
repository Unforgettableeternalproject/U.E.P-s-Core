"""
tests/test_contradiction_detection.py - 自動矛盾偵測測試套件

驗證 Branch 9: feat/contradiction-detection 的完整功能。

測試策略：
  - 不依賴真正的嵌入模型或 LLM
  - 使用 mock llm_fn 和手動設定的向量
  - 測試所有降級路徑和邊界條件
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from echo_memory.dream.contradiction import ContradictionDetector
from echo_memory.dream.distillation import KnowledgeDistillation
from echo_memory.schemas.concept import Concept
from echo_memory.schemas.episode import Episode
from echo_memory.storage.graph import MemoryGraph
from echo_memory.storage.semantic import SemanticMemory

# ─── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def tmp_semantic(tmp_path: Path) -> SemanticMemory:
    """建立隔離的 SemanticMemory 實例"""
    return SemanticMemory(storage_path=tmp_path / "semantic")


@pytest.fixture
def tmp_graph(tmp_path: Path) -> MemoryGraph:
    """建立隔離的 MemoryGraph 實例"""
    return MemoryGraph(storage_path=tmp_path / "graph")


def make_concept(
    name: str,
    description: str,
    embedding: list[float] | None = None,
) -> Concept:
    """
    建立測試用概念，可選擇指定向量。

    Args:
        name: 概念名稱
        description: 概念描述
        embedding: 向量嵌入；為 None 時使用零向量

    Returns:
        Concept 實例
    """
    concept = Concept(name=name, description=description)
    concept.embedding = embedding or []
    return concept


def llm_says_contradiction(strength: float = 0.85, reason: str = "明顯矛盾") -> callable:
    """
    建立回傳「是矛盾」的 mock llm_fn。

    Args:
        strength: 矛盾強度
        reason: 矛盾原因

    Returns:
        mock llm_fn callable
    """
    response = json.dumps({
        "is_contradiction": True,
        "strength": strength,
        "reason": reason,
    })
    return MagicMock(return_value=response)


def llm_says_no_contradiction() -> callable:
    """建立回傳「不是矛盾」的 mock llm_fn"""
    response = json.dumps({
        "is_contradiction": False,
        "strength": 0.0,
        "reason": "語義一致",
    })
    return MagicMock(return_value=response)


# ─── 測試：無 LLM 時跳過 ───────────────────────────────────────────────────────


def test_detector_no_llm_skips(tmp_semantic: SemanticMemory, tmp_graph: MemoryGraph):
    """
    無 LLM 時，detect_and_register 應回傳空清單且不建任何邊。

    降級策略：純向量相似度無法判斷矛盾，故無 LLM 時不建邊。
    """
    detector = ContradictionDetector(
        semantic=tmp_semantic,
        graph=tmp_graph,
        llm_fn=None,
    )

    concept_a = make_concept("概念A", "描述A", embedding=[1.0, 0.0])
    concept_b = make_concept("概念B", "描述B", embedding=[0.99, 0.14])

    # 加入 semantic
    tmp_semantic._concepts["概念B"] = concept_b

    edges = detector.detect_and_register(concept_a)

    assert edges == [], "無 LLM 時不應建立任何矛盾邊"
    assert tmp_graph.graph.number_of_edges() == 0, "圖中不應有任何邊"


# ─── 測試：LLM 確認矛盾時建立邊 ───────────────────────────────────────────────


def test_detector_with_llm_finds_contradiction(
    tmp_semantic: SemanticMemory, tmp_graph: MemoryGraph
):
    """
    mock llm_fn 回傳矛盾時，應建立 CONTRADICTS 邊（雙向）。
    """
    mock_llm = llm_says_contradiction(strength=0.9)

    detector = ContradictionDetector(
        semantic=tmp_semantic,
        graph=tmp_graph,
        similarity_threshold=0.80,
        llm_fn=mock_llm,
    )

    # 設定高相似度向量（> threshold）
    concept_a = make_concept("運動有益健康", "適度運動可以強化免疫系統", embedding=[1.0, 0.0])
    concept_b = make_concept("運動有害健康", "過度運動會損害免疫系統", embedding=[0.99, 0.14])

    tmp_semantic._concepts[concept_b.name] = concept_b

    edges = detector.detect_and_register(concept_a, candidate_pool=[concept_b])

    # 驗證回傳值
    assert len(edges) == 1, "應建立一條矛盾邊"
    a_id, b_id, weight = edges[0]
    assert {a_id, b_id} == {concept_a.id, concept_b.id}
    assert 0.0 <= weight <= 1.0

    # 驗證圖中有 CONTRADICTS 邊（雙向）
    assert tmp_graph.has_contradicts_edge(concept_a.id, concept_b.id), \
        "圖中應存在 CONTRADICTS 邊"


# ─── 測試：LLM 回傳不矛盾時不建邊 ────────────────────────────────────────────


def test_detector_with_llm_no_contradiction(
    tmp_semantic: SemanticMemory, tmp_graph: MemoryGraph
):
    """
    mock llm_fn 回傳不矛盾時，不應建立任何 CONTRADICTS 邊。
    """
    mock_llm = llm_says_no_contradiction()

    detector = ContradictionDetector(
        semantic=tmp_semantic,
        graph=tmp_graph,
        similarity_threshold=0.80,
        llm_fn=mock_llm,
    )

    concept_a = make_concept("蘋果是水果", "蘋果屬於水果類別", embedding=[1.0, 0.0])
    concept_b = make_concept("蘋果含維生素", "蘋果富含維生素C", embedding=[0.99, 0.14])

    edges = detector.detect_and_register(concept_a, candidate_pool=[concept_b])

    assert edges == [], "LLM 回傳不矛盾時不應建立邊"
    assert tmp_graph.graph.number_of_edges() == 0, "圖中不應有任何邊"


# ─── 測試：概念不和自己比較 ────────────────────────────────────────────────────


def test_detector_skips_self(tmp_semantic: SemanticMemory, tmp_graph: MemoryGraph):
    """
    detect_and_register 不應將概念與自身比較。
    """
    mock_llm = llm_says_contradiction()

    detector = ContradictionDetector(
        semantic=tmp_semantic,
        graph=tmp_graph,
        similarity_threshold=0.0,  # 設很低確保任何向量都會進入候選
        llm_fn=mock_llm,
    )

    concept = make_concept("自我概念", "概念描述", embedding=[1.0, 0.0])

    # 候選池只有自身
    edges = detector.detect_and_register(concept, candidate_pool=[concept])

    assert edges == [], "不應與自身比較建立矛盾邊"
    assert mock_llm.call_count == 0, "不應呼叫 LLM 比較自身"


# ─── 測試：不重複建邊 ─────────────────────────────────────────────────────────


def test_detector_skips_existing_contradicts(
    tmp_semantic: SemanticMemory, tmp_graph: MemoryGraph
):
    """
    已存在 CONTRADICTS 邊的配對，不應重複建立。
    """
    mock_llm = llm_says_contradiction()

    detector = ContradictionDetector(
        semantic=tmp_semantic,
        graph=tmp_graph,
        similarity_threshold=0.80,
        llm_fn=mock_llm,
    )

    concept_a = make_concept("A概念", "A描述", embedding=[1.0, 0.0])
    concept_b = make_concept("B概念", "B描述", embedding=[0.99, 0.14])

    # 手動確保節點存在後先建立矛盾邊
    tmp_graph.graph.add_node(concept_a.id, node_type="Concept")
    tmp_graph.graph.add_node(concept_b.id, node_type="Concept")
    tmp_graph.add_contradicts_edge(concept_a.id, concept_b.id, weight=0.8)

    # 初始邊數
    edges_before = tmp_graph.graph.number_of_edges()

    # 再次偵測
    edges = detector.detect_and_register(concept_a, candidate_pool=[concept_b])

    assert edges == [], "已有 CONTRADICTS 邊時不應重複建立"
    assert tmp_graph.graph.number_of_edges() == edges_before, "邊數不應增加"
    assert mock_llm.call_count == 0, "已有矛盾邊時不應呼叫 LLM"


# ─── 測試：無向量概念跳過 ─────────────────────────────────────────────────────


def test_detector_skips_concept_without_embedding(
    tmp_semantic: SemanticMemory, tmp_graph: MemoryGraph
):
    """
    無向量的概念應跳過偵測，不呼叫 LLM。
    """
    mock_llm = llm_says_contradiction()

    detector = ContradictionDetector(
        semantic=tmp_semantic,
        graph=tmp_graph,
        similarity_threshold=0.80,
        llm_fn=mock_llm,
    )

    # 目標概念無向量
    concept_no_emb = make_concept("無向量概念", "沒有嵌入向量", embedding=[])

    edges = detector.detect_and_register(concept_no_emb)

    assert edges == [], "無向量概念不應偵測矛盾"
    assert mock_llm.call_count == 0, "無向量時不應呼叫 LLM"


# ─── 測試：低相似度候選被過濾 ─────────────────────────────────────────────────


def test_detector_filters_low_similarity(
    tmp_semantic: SemanticMemory, tmp_graph: MemoryGraph
):
    """
    低於相似度門檻的概念不應進入 LLM 確認階段。
    """
    mock_llm = llm_says_contradiction()

    detector = ContradictionDetector(
        semantic=tmp_semantic,
        graph=tmp_graph,
        similarity_threshold=0.99,  # 很高的門檻
        llm_fn=mock_llm,
    )

    concept_a = make_concept("A", "描述A", embedding=[1.0, 0.0])
    # 相似度約 0.707（45度角），低於 0.99
    concept_b = make_concept("B", "描述B", embedding=[1.0, 1.0])

    edges = detector.detect_and_register(concept_a, candidate_pool=[concept_b])

    assert edges == [], "低相似度候選不應建立矛盾邊"
    assert mock_llm.call_count == 0, "低相似度時不應呼叫 LLM"


# ─── 測試：distillation 觸發矛盾偵測 ──────────────────────────────────────────


def test_distillation_triggers_contradiction_check(
    tmp_semantic: SemanticMemory, tmp_graph: MemoryGraph
):
    """
    端對端測試：KnowledgeDistillation 執行後，ContradictionDetector 應被觸發。
    """
    # LLM 回傳一個 JSON 陣列格式的概念（distillation 的 prompt 格式）
    distillation_response = json.dumps([
        {
            "name": "測試概念",
            "description": "這是一個測試概念",
            "concept_type": "entity",
            "source_indices": [0],
        }
    ])
    mock_llm = MagicMock(return_value=distillation_response)

    # mock EpisodicMemory
    mock_episodic = MagicMock()
    episode = Episode(
        user_input="測試輸入",
        agent_response="測試回應",
        session_id="test_session",
        content_summary="測試摘要",
        salience_score=0.8,
        is_dreamed=False,
    )
    mock_episodic.list_pending.return_value = [episode]
    mock_episodic.mark_as_dreamed.return_value = 1
    # 蒸餾寫回前會以 episodic.get 比對過期，mock 需回傳同一個 Episode
    mock_episodic.get.side_effect = lambda ep_id: episode if ep_id == episode.id else None

    # mock ContradictionDetector
    mock_detector = MagicMock(spec=ContradictionDetector)
    mock_detector.detect_and_register.return_value = []

    distillation = KnowledgeDistillation(
        episodic=mock_episodic,
        semantic=tmp_semantic,
        graph=tmp_graph,
        llm_fn=mock_llm,
        contradiction_detector=mock_detector,
    )

    report = distillation.run()

    # 驗證蒸餾結果
    assert report.episodes_scanned == 1, "應掃描一個情節"
    assert report.concepts_created == 1, "應建立一個概念"

    # 驗證矛盾偵測被觸發
    assert mock_detector.detect_and_register.call_count >= 1, \
        "蒸餾後應觸發矛盾偵測"


def test_distillation_no_contradiction_detector(
    tmp_semantic: SemanticMemory, tmp_graph: MemoryGraph
):
    """
    未配置 ContradictionDetector 時，蒸餾仍應正常完成（向後相容）。
    """
    distillation_response = json.dumps([
        {
            "name": "相容概念",
            "description": "向後相容測試",
            "concept_type": "rule",
            "source_indices": [0],
        }
    ])
    mock_llm = MagicMock(return_value=distillation_response)

    mock_episodic = MagicMock()
    episode = Episode(
        user_input="輸入",
        agent_response="回應",
        session_id="session",
        content_summary="摘要",
        salience_score=0.8,
        is_dreamed=False,
    )
    mock_episodic.list_pending.return_value = [episode]
    mock_episodic.mark_as_dreamed.return_value = 1
    # 蒸餾寫回前會以 episodic.get 比對過期，mock 需回傳同一個 Episode
    mock_episodic.get.side_effect = lambda ep_id: episode if ep_id == episode.id else None

    distillation = KnowledgeDistillation(
        episodic=mock_episodic,
        semantic=tmp_semantic,
        graph=tmp_graph,
        llm_fn=mock_llm,
        contradiction_detector=None,
    )

    report = distillation.run()

    assert report.concepts_created == 1, "應正常建立概念"
    assert report.contradicts_edges_created == 0, "未配置矛盾偵測器時矛盾邊數量為 0"
    assert len(report.errors) == 0, "不應有錯誤"


def test_distillation_no_llm_skips(
    tmp_semantic: SemanticMemory, tmp_graph: MemoryGraph
):
    """
    無 LLM 時，蒸餾應降級跳過並回傳帶 skipped 的報告。
    """
    mock_episodic = MagicMock()

    distillation = KnowledgeDistillation(
        episodic=mock_episodic,
        semantic=tmp_semantic,
        graph=tmp_graph,
        llm_fn=None,
    )

    report = distillation.run()

    assert report.skipped_reason == "no_llm", "無 LLM 時應跳過蒸餾"
    assert report.episodes_scanned == 0, "無 LLM 時不應掃描任何情節"


# ─── 測試：graph.add_contradicts_edge 基本功能 ────────────────────────────────


def test_graph_add_contradicts_edge(tmp_graph: MemoryGraph):
    """
    驗證 MemoryGraph.add_contradicts_edge 建立雙向邊。
    """
    tmp_graph.graph.add_node("node_a", node_type="Concept")
    tmp_graph.graph.add_node("node_b", node_type="Concept")

    tmp_graph.add_contradicts_edge("node_a", "node_b", weight=0.75)

    assert tmp_graph.has_contradicts_edge("node_a", "node_b"), "應存在 A→B 矛盾邊"
    assert tmp_graph.has_contradicts_edge("node_b", "node_a"), "應存在 B→A 矛盾邊（雙向）"

    # 驗證邊屬性
    edge_data = tmp_graph.graph["node_a"]["node_b"]
    assert edge_data["edge_type"] == "CONTRADICTS"
    assert edge_data["weight"] == 0.75


def test_graph_contradicts_self_skipped(tmp_graph: MemoryGraph):
    """
    add_contradicts_edge 不應建立自我矛盾邊。
    """
    tmp_graph.graph.add_node("self_node", node_type="Concept")

    tmp_graph.add_contradicts_edge("self_node", "self_node", weight=0.9)

    assert tmp_graph.graph.number_of_edges() == 0, "自我矛盾邊不應被建立"


def test_graph_has_contradicts_edge_false_for_other_types(tmp_graph: MemoryGraph):
    """
    has_contradicts_edge 對非 CONTRADICTS 邊應回傳 False。
    """
    tmp_graph.graph.add_node("node_a", node_type="Episode")
    tmp_graph.graph.add_node("node_b", node_type="Episode")

    # 添加 PRECEDES 邊（非矛盾邊）
    tmp_graph.graph.add_edge("node_a", "node_b", edge_type="PRECEDES", weight=0.5)

    assert not tmp_graph.has_contradicts_edge("node_a", "node_b"), \
        "PRECEDES 邊不應被判斷為 CONTRADICTS 邊"
