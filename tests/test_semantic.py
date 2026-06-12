"""
SemanticMemory 測試 — Phase 2 Branch 1 驗收

驗收標準：
  1. upsert_concept → count() > 0
  2. 重啟後（重新載入）資料不遺失
  3. 向量搜索返回正確排序的結果
  4. remove_concept 正確清理
  5. MemoryGraph 的新邊類型正常運作
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from echo_memory.schemas.concept import Concept
from echo_memory.storage.graph import MemoryGraph
from echo_memory.storage.semantic import SemanticMemory


# ─── Fixtures ───────────────────────────────────────────────────────

FAKE_DIM = 8  # 測試用小維度，加速


def _fake_embed_fn(text: str) -> list[float]:
    """確定性的假 embedding：用 text 的 hash 生成 [-1, 1] 範圍向量。"""
    import hashlib
    h = hashlib.sha256(text.encode()).digest()
    # 用 uint8 → 映射到 [-1, 1]，避免 float32 overflow
    raw = np.frombuffer(h[:FAKE_DIM], dtype=np.uint8).astype(np.float32)
    vec = (raw / 127.5) - 1.0  # [0, 255] → [-1, 1]
    norm = np.linalg.norm(vec)
    if norm > 0:
        vec /= norm
    return vec.tolist()


@pytest.fixture
def storage_dir(tmp_path: Path) -> Path:
    """每個測試獨立的臨時目錄。"""
    d = tmp_path / "semantic_test"
    d.mkdir()
    return d


@pytest.fixture
def sem(storage_dir: Path) -> SemanticMemory:
    """帶有假 embed_fn 的 SemanticMemory 實例。"""
    return SemanticMemory(storage_path=storage_dir, embed_fn=_fake_embed_fn)


@pytest.fixture
def graph_dir(tmp_path: Path) -> Path:
    d = tmp_path / "graph_test"
    d.mkdir()
    return d


# ─── 基本 CRUD ──────────────────────────────────────────────────────


class TestSemanticCRUD:
    """基本的新增/查詢/更新/刪除。"""

    def test_upsert_new_concept(self, sem: SemanticMemory):
        cid = sem.upsert_concept("Python", "通用程式語言", concept_type="entity")
        assert cid.startswith("con_")
        assert sem.count() == 1

    def test_upsert_updates_existing(self, sem: SemanticMemory):
        cid1 = sem.upsert_concept("Python", "程式語言")
        cid2 = sem.upsert_concept("Python", "通用高階程式語言")
        # 同名 → 同一個 concept，ID 不變
        assert cid1 == cid2
        assert sem.count() == 1
        concept = sem.get_by_name("Python")
        assert concept is not None
        assert concept.description == "通用高階程式語言"

    def test_upsert_with_source_episode(self, sem: SemanticMemory):
        sem.upsert_concept("AI", "人工智慧", source_episode_id="ep_abc123")
        concept = sem.get_by_name("AI")
        assert concept is not None
        assert "ep_abc123" in concept.source_episode_ids
        assert concept.confidence == 0.5  # 1 source → 1-1/(1+1) = 0.5

        sem.upsert_concept("AI", "人工智慧", source_episode_id="ep_def456")
        concept = sem.get_by_name("AI")
        assert len(concept.source_episode_ids) == 2
        assert concept.confidence > 0.5  # 2 sources → higher

    def test_get_by_id(self, sem: SemanticMemory):
        cid = sem.upsert_concept("ML", "機器學習")
        concept = sem.get(cid)
        assert concept is not None
        assert concept.name == "ML"

    def test_get_nonexistent(self, sem: SemanticMemory):
        assert sem.get("con_nonexistent") is None
        assert sem.get_by_name("不存在") is None

    def test_remove_concept(self, sem: SemanticMemory):
        sem.upsert_concept("temp", "臨時概念")
        assert sem.count() == 1
        removed = sem.remove_concept("temp")
        assert removed is True
        assert sem.count() == 0
        assert sem.get_by_name("temp") is None

    def test_remove_nonexistent(self, sem: SemanticMemory):
        assert sem.remove_concept("不存在") is False

    def test_clear(self, sem: SemanticMemory):
        sem.upsert_concept("A", "概念A")
        sem.upsert_concept("B", "概念B")
        assert sem.count() == 2
        sem.clear()
        assert sem.count() == 0

    def test_list_concepts(self, sem: SemanticMemory):
        sem.upsert_concept("A", "aaa", concept_type="entity")
        sem.upsert_concept("B", "bbb", concept_type="rule")
        sem.upsert_concept("C", "ccc", concept_type="entity")

        all_concepts = sem.list_concepts()
        assert len(all_concepts) == 3

        entities_only = sem.list_concepts(concept_type="entity")
        assert len(entities_only) == 2
        assert all(c.concept_type == "entity" for c in entities_only)


# ─── 持久化（核心驗收） ────────────────────────────────────────────


class TestSemanticPersistence:
    """重啟後資料不遺失——Branch 1 的核心驗收標準。"""

    def test_persistence_round_trip(self, storage_dir: Path):
        """寫入 → 關閉 → 重新打開 → 資料完整。"""
        # 第一個實例：寫入
        sem1 = SemanticMemory(storage_path=storage_dir, embed_fn=_fake_embed_fn)
        sem1.upsert_concept("Python", "通用程式語言", source_episode_id="ep_001")
        sem1.upsert_concept("Rust", "系統程式語言", source_episode_id="ep_002")
        assert sem1.count() == 2

        # 確認檔案已寫入磁盤
        assert (storage_dir / "concepts.json").exists()
        assert (storage_dir / "concept_embeddings.npy").exists()

        # 第二個實例：重新載入（模擬重啟）
        sem2 = SemanticMemory(storage_path=storage_dir, embed_fn=_fake_embed_fn)
        assert sem2.count() == 2

        python_concept = sem2.get_by_name("Python")
        assert python_concept is not None
        assert python_concept.description == "通用程式語言"
        assert "ep_001" in python_concept.source_episode_ids

        rust_concept = sem2.get_by_name("Rust")
        assert rust_concept is not None
        assert rust_concept.description == "系統程式語言"

    def test_persistence_embeddings_preserved(self, storage_dir: Path):
        """重啟後向量索引仍可正常搜索。"""
        sem1 = SemanticMemory(storage_path=storage_dir, embed_fn=_fake_embed_fn)
        sem1.upsert_concept("貓", "一種常見寵物")
        sem1.upsert_concept("狗", "忠誠的動物夥伴")
        sem1.upsert_concept("量子力學", "微觀粒子的物理理論")

        # 用「貓」的 embedding 搜索，應該找得到
        query_vec = _fake_embed_fn("貓: 一種常見寵物")
        results1 = sem1.search_by_vector(query_vec, top_k=3, min_similarity=0.0)
        assert len(results1) > 0

        # 重啟
        sem2 = SemanticMemory(storage_path=storage_dir, embed_fn=_fake_embed_fn)
        results2 = sem2.search_by_vector(query_vec, top_k=3, min_similarity=0.0)
        assert len(results2) == len(results1)
        # 排序和分數應一致
        for (c1, s1), (c2, s2) in zip(results1, results2):
            assert c1.name == c2.name
            assert abs(s1 - s2) < 1e-5

    def test_persistence_after_remove(self, storage_dir: Path):
        """刪除概念後重啟，被刪的確實不見了。"""
        sem1 = SemanticMemory(storage_path=storage_dir, embed_fn=_fake_embed_fn)
        sem1.upsert_concept("A", "概念A")
        sem1.upsert_concept("B", "概念B")
        sem1.remove_concept("A")
        assert sem1.count() == 1

        sem2 = SemanticMemory(storage_path=storage_dir, embed_fn=_fake_embed_fn)
        assert sem2.count() == 1
        assert sem2.get_by_name("A") is None
        assert sem2.get_by_name("B") is not None

    def test_persistence_files_content(self, storage_dir: Path):
        """驗證磁盤檔案格式正確。"""
        sem = SemanticMemory(storage_path=storage_dir, embed_fn=_fake_embed_fn)
        sem.upsert_concept("Test", "測試概念", concept_type="abstract")

        # concepts.json 是合法 JSON
        with open(storage_dir / "concepts.json", "r", encoding="utf-8") as f:
            data = json.load(f)
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["name"] == "Test"
        assert data[0]["concept_type"] == "abstract"

        # concept_embeddings.npy 是合法的 NumPy 陣列
        emb = np.load(storage_dir / "concept_embeddings.npy")
        assert emb.ndim == 2
        assert emb.shape[0] == 1  # 一個概念
        assert emb.shape[1] == FAKE_DIM


# ─── 向量搜索 ──────────────────────────────────────────────────────


class TestSemanticVectorSearch:
    """向量相似度搜索。"""

    def test_search_returns_sorted_results(self, sem: SemanticMemory):
        """搜索結果按相似度降序排列。"""
        sem.upsert_concept("A", "概念A")
        sem.upsert_concept("B", "概念B")
        sem.upsert_concept("C", "概念C")

        query = _fake_embed_fn("概念A")
        results = sem.search_by_vector(query, top_k=3, min_similarity=0.0)
        assert len(results) > 0
        # 確認降序
        for i in range(len(results) - 1):
            assert results[i][1] >= results[i + 1][1]

    def test_search_min_similarity_filter(self, sem: SemanticMemory):
        """min_similarity 過濾功能。"""
        sem.upsert_concept("A", "概念A")
        # 用一個極高的 min_similarity
        query = _fake_embed_fn("完全無關的查詢")
        results = sem.search_by_vector(query, top_k=5, min_similarity=0.9999)
        # 大部分隨機向量不會有 0.9999 的相似度
        # 不對長度做硬斷言，但過濾邏輯要正確執行
        for _, sim in results:
            assert sim >= 0.9999

    def test_search_empty_memory(self, sem: SemanticMemory):
        """空記憶搜索不會報錯。"""
        query = _fake_embed_fn("任何東西")
        results = sem.search_by_vector(query, top_k=5)
        assert results == []

    def test_search_zero_vector(self, sem: SemanticMemory):
        """零向量查詢不會報錯。"""
        sem.upsert_concept("A", "概念A")
        results = sem.search_by_vector([0.0] * FAKE_DIM, top_k=5)
        assert results == []

    def test_search_without_embed_fn(self, storage_dir: Path):
        """沒有 embed_fn 但手動提供 embedding 也能搜索。"""
        sem = SemanticMemory(storage_path=storage_dir, embed_fn=None)
        vec = _fake_embed_fn("手動向量")
        sem.upsert_concept("Manual", "手動概念", embedding=vec)
        assert sem.count() == 1

        results = sem.search_by_vector(vec, top_k=1, min_similarity=0.0)
        assert len(results) == 1
        assert results[0][0].name == "Manual"


# ─── Concept Schema ────────────────────────────────────────────────


class TestConceptSchema:
    """Concept dataclass 的新功能。"""

    def test_add_source_episode_updates_confidence(self):
        c = Concept(name="test", description="test")
        assert c.confidence == 0.5  # 預設值

        c.add_source_episode("ep_1")
        assert c.confidence == pytest.approx(0.5)  # 1/(1+1) → 0.5

        c.add_source_episode("ep_2")
        assert c.confidence == pytest.approx(2/3)  # 1-1/(1+2) = 2/3

        c.add_source_episode("ep_3")
        assert c.confidence == pytest.approx(0.75)  # 1-1/(1+3) = 3/4

    def test_add_source_episode_deduplicates(self):
        c = Concept(name="test", description="test")
        c.add_source_episode("ep_1")
        c.add_source_episode("ep_1")  # 重複
        assert len(c.source_episode_ids) == 1

    def test_concept_round_trip(self):
        c = Concept(
            name="AI",
            description="人工智慧",
            concept_type="abstract",
            embedding=[0.1, 0.2, 0.3],
            source_episode_ids=["ep_1", "ep_2"],
            confidence=0.67,
            access_count=5,
        )
        d = c.to_dict()
        c2 = Concept.from_dict(d)
        assert c2.name == c.name
        assert c2.description == c.description
        assert c2.source_episode_ids == c.source_episode_ids
        assert c2.confidence == c.confidence
        assert c2.access_count == c.access_count


# ─── MemoryGraph 新邊類型 ──────────────────────────────────────────


class TestGraphNewEdgeTypes:
    """MemoryGraph 的 RELATES_TO / CONTRADICTS / DERIVED_FROM。"""

    def test_add_concept_node(self, graph_dir: Path):
        graph = MemoryGraph(graph_dir)
        graph.add_concept_node("con_abc", "Python", "entity")
        assert graph.has_node("con_abc")
        assert graph.graph.nodes["con_abc"]["node_type"] == "Concept"

    def test_relates_to_edge(self, graph_dir: Path):
        graph = MemoryGraph(graph_dir)
        graph.add_concept_node("con_a", "Python")
        graph.add_concept_node("con_b", "程式設計")

        graph.add_relates_to_edge("con_a", "con_b", weight=0.7)

        # 雙向邊
        assert graph.graph.has_edge("con_a", "con_b")
        assert graph.graph.has_edge("con_b", "con_a")
        assert graph.graph["con_a"]["con_b"]["edge_type"] == "RELATES_TO"
        assert graph.graph["con_a"]["con_b"]["weight"] == pytest.approx(0.7)

    def test_relates_to_reinforcement(self, graph_dir: Path):
        """重複添加 RELATES_TO 應該強化而非覆蓋。"""
        graph = MemoryGraph(graph_dir)
        graph.add_concept_node("con_a", "A")
        graph.add_concept_node("con_b", "B")

        graph.add_relates_to_edge("con_a", "con_b", weight=0.5)
        graph.add_relates_to_edge("con_a", "con_b", weight=0.8)

        edge = graph.graph["con_a"]["con_b"]
        assert edge["frequency"] == 2
        # 權重是 (0.5 + 0.8) / 2 = 0.65
        assert edge["weight"] == pytest.approx(0.65)

    def test_contradicts_edge(self, graph_dir: Path):
        graph = MemoryGraph(graph_dir)
        graph.add_concept_node("con_a", "地球是平的")
        graph.add_concept_node("con_b", "地球是球形")

        graph.add_contradicts_edge("con_a", "con_b", weight=0.9)

        assert graph.graph.has_edge("con_a", "con_b")
        assert graph.graph["con_a"]["con_b"]["edge_type"] == "CONTRADICTS"

    def test_derived_from_edge(self, graph_dir: Path):
        from echo_memory.schemas.episode import Episode

        graph = MemoryGraph(graph_dir)
        ep = Episode(
            user_input="test",
            agent_response="test",
            session_id="s1",
            content_summary="test",
        )
        graph.add_episode_node(ep)
        graph.add_concept_node("con_abc", "TestConcept")

        graph.add_derived_from_edge("con_abc", ep.id, weight=0.8)

        assert graph.graph.has_edge("con_abc", ep.id)
        assert graph.graph["con_abc"][ep.id]["edge_type"] == "DERIVED_FROM"

    def test_get_concept_relations(self, graph_dir: Path):
        graph = MemoryGraph(graph_dir)
        graph.add_concept_node("con_a", "A")
        graph.add_concept_node("con_b", "B")
        graph.add_concept_node("con_c", "C")

        graph.add_relates_to_edge("con_a", "con_b", weight=0.6)
        graph.add_contradicts_edge("con_a", "con_c", weight=0.8)

        # 全部關聯
        all_rels = graph.get_concept_relations("con_a")
        assert len(all_rels) >= 3  # a→b, b→a, a→c, c→a 的部分

        # 只看 RELATES_TO
        relates = graph.get_concept_relations("con_a", edge_type="RELATES_TO")
        assert all(r[1] == "RELATES_TO" for r in relates)

    def test_expand_excludes_contradicts(self, graph_dir: Path):
        """expand_neighbors 預設不展開 CONTRADICTS 邊。"""
        graph = MemoryGraph(graph_dir)
        graph.add_concept_node("con_a", "A")
        graph.add_concept_node("con_b", "B")
        graph.add_concept_node("con_c", "C")

        graph.add_relates_to_edge("con_a", "con_b", weight=0.7)
        graph.add_contradicts_edge("con_a", "con_c", weight=0.9)

        expanded = graph.expand_neighbors(["con_a"], max_hops=1, min_weight=0.1)
        expanded_ids = {node_id for node_id, _, _ in expanded}

        assert "con_b" in expanded_ids      # RELATES_TO → 展開
        assert "con_c" not in expanded_ids   # CONTRADICTS → 不展開

    def test_remove_concept_node(self, graph_dir: Path):
        graph = MemoryGraph(graph_dir)
        graph.add_concept_node("con_a", "A")
        graph.add_concept_node("con_b", "B")
        graph.add_relates_to_edge("con_a", "con_b")

        graph.remove_concept_node("con_a")
        assert not graph.has_node("con_a")
        assert not graph.graph.has_edge("con_a", "con_b")


# ─── 統計 ──────────────────────────────────────────────────────────


class TestSemanticStats:
    def test_stats(self, sem: SemanticMemory):
        sem.upsert_concept("A", "概念A", concept_type="entity")
        sem.upsert_concept("B", "概念B", concept_type="rule")

        stats = sem.get_stats()
        assert stats["total_concepts"] == 2
        assert stats["type_distribution"]["entity"] == 1
        assert stats["type_distribution"]["rule"] == 1
        assert stats["concepts_with_embeddings"] == 2
