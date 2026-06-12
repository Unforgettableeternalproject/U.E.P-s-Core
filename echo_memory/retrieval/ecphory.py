"""
EcphoryRetrieval - 聯想檢索引擎

Phase 2: 整合 SemanticMemory 概念檢索 + RELATES_TO 展開 + CONTRADICTS penalty

檢索流程：
  1. NumPy 餘弦相似度 → 定位 Episode 種子節點
  2. NetworkX 圖譜展開 → 沿 PRECEDES/RELATES_TO 邊多跳擴展
  3. 語義概念搜索 → SemanticMemory 向量檢索 + RELATES_TO 1-hop 展開
  4. 矛盾檢測 → CONTRADICTS 邊 penalty 降權
  5. 整合返回 MemoryContext（episodes + concepts）
"""

from __future__ import annotations

import logging
from typing import Any

from echo_memory.neurochem.state import NeurochemState, RetrievalParams
from echo_memory.schemas.concept import Concept
from echo_memory.schemas.memory_context import MemoryContext
from echo_memory.storage.episodic import EpisodicMemory
from echo_memory.storage.semantic import SemanticMemory
from echo_memory.storage.working import WorkingMemory

logger = logging.getLogger("echo_memory.retrieval")


class EcphoryRetrieval:
    """
    EcphoryRAG 檢索引擎 - 混合架構。

    整合三層記憶的聯想檢索：
      - Episodic: 向量種子 + 圖譜多跳展開
      - Semantic: 概念向量搜索 + RELATES_TO 展開 + CONTRADICTS penalty
      - Working: (Phase 2 Branch 6) 查詢快取
    """

    # CONTRADICTS penalty 係數：命中矛盾邊時，相似度乘以此值
    CONTRADICTS_PENALTY = 0.3

    # 查詢向量快取的 key prefix
    _CACHE_PREFIX = "qemb:"

    def __init__(
        self,
        episodic: EpisodicMemory,
        semantic: SemanticMemory,
        working: WorkingMemory | None = None,
        procedural: Any | None = None,  # ProceduralMemory（延遲導入）
    ):
        self.episodic = episodic
        self.semantic = semantic
        self.working = working  # 若提供，啟用查詢向量快取
        self.procedural = procedural  # 若提供，檢索時一併搜索程序

    def retrieve(
        self,
        query: str,
        dopamine: float = 0.5,
        serotonin: float = 0.5,
        top_k: int = 10,
        min_similarity: float = 0.3,
        enable_graph_expansion: bool = True,
        enable_semantic: bool = True,
        concept_top_k: int = 5,
        concept_min_similarity: float = 0.3,
        neurochem: NeurochemState | None = None,
    ) -> MemoryContext:
        """
        執行 EcphoryRAG 檢索。

        Args:
            query: 查詢文本
            dopamine: 多巴胺（影響探索廣度，neurochem=None 時使用）
            serotonin: 血清素（影響謹慎程度，neurochem=None 時使用）
            top_k: Episode 返回數量
            min_similarity: Episode 最小相似度閾值
            enable_graph_expansion: 是否啟用圖譜展開
            enable_semantic: 是否啟用語義概念檢索
            concept_top_k: Concept 返回數量
            concept_min_similarity: Concept 最小相似度閾值
            neurochem: NeurochemState 實例（若提供，覆寫 dopamine/serotonin
                       並用衍生參數驅動檢索行為）

        Returns:
            MemoryContext 記憶脈絡（包含 episodes + concepts）
        """
        # 若有 NeurochemState，用它的衍生參數覆寫預設值（只呼叫一次，快取結果）
        _ncparams: RetrievalParams | None = None
        if neurochem is not None:
            _ncparams = neurochem.get_retrieval_params()
            dopamine = neurochem.dopamine
            serotonin = neurochem.serotonin
            concept_top_k = _ncparams.concept_top_k
            concept_min_similarity = _ncparams.concept_min_similarity

        # Step 1: Episode 向量檢索（種子節點）
        # 查詢向量快取：相同查詢不重複生成 embedding（省 ~50ms）
        cache_key = f"{self._CACHE_PREFIX}{query.strip()}"
        query_embedding = None

        if self.working is not None:
            cached = self.working.get(cache_key)
            if cached is not None:
                query_embedding = cached
                logger.debug("[EcphoryRetrieval] 查詢向量快取命中")

        if query_embedding is None:
            query_embedding = self.episodic.generate_query_embedding(query)
            if self.working is not None:
                self.working.set(cache_key, query_embedding)

        # NeurochemState 影響種子數
        seed_k = max(3, top_k // 2)
        if _ncparams is not None:
            seed_k = _ncparams.seed_k

        seed_results = self.episodic.search(
            query_embedding,
            top_k=seed_k,
            min_similarity=min_similarity,
        )

        if not seed_results:
            # 沒有 Episode 種子，但仍可能有 Concept 命中
            episodes: list = []
            similarities: dict[str, float] = {}
            seed_ids: list[str] = []
            logger.debug("[EcphoryRetrieval] 未找到相似度達標的 Episode 種子")
        else:
            episodes = [ep for ep, _ in seed_results]
            seed_ids = [ep.id for ep in episodes]
            similarities = {ep.id: score for ep, score in seed_results}

            logger.debug(
                f"[EcphoryRetrieval] Episode 種子檢索完成 - "
                f"找到 {len(episodes)} 個種子 "
                f"(相似度範圍: {min(similarities.values()):.2f}-{max(similarities.values()):.2f})"
            )

        # Step 2: 圖譜多跳展開（Episode PRECEDES 邊）
        if enable_graph_expansion and seed_ids and self.episodic.graph.get_node_count() > 0:
            if _ncparams is not None:
                max_hops = _ncparams.max_hops
                min_weight = _ncparams.min_weight
            else:
                max_hops = 2 if serotonin > 0.6 else 1
                min_weight = max(0.15, 0.35 - dopamine * 0.3)

            expanded_nodes = self.episodic.graph.expand_neighbors(
                seed_ids=seed_ids,
                max_hops=max_hops,
                min_weight=min_weight,
                max_results=top_k * 2,
            )

            logger.debug(
                f"[EcphoryRetrieval] 圖譜展開完成 - "
                f"發現 {len(expanded_nodes)} 個關聯節點"
            )

            for node_id, weight, hops in expanded_nodes:
                if node_id not in similarities:
                    ep = self.episodic.get(node_id)
                    if ep:
                        episodes.append(ep)
                        similarities[node_id] = weight * 0.8

        # Step 3: 語義概念檢索
        concepts: list[Concept] = []
        concept_similarities: dict[str, float] = {}

        if enable_semantic and self.semantic.count() > 0:
            concepts, concept_similarities = self._retrieve_concepts(
                query_embedding=query_embedding,
                top_k=concept_top_k,
                min_similarity=concept_min_similarity,
                dopamine=dopamine,
            )

            logger.debug(
                f"[EcphoryRetrieval] 語義概念檢索完成 - "
                f"找到 {len(concepts)} 個概念"
            )

        # Step 4: 程序記憶搜索（若有 ProceduralMemory）
        procedures = []
        if self.procedural is not None and self.procedural.count() > 0:
            try:
                proc_results = self.procedural.search_by_vector(
                    query_embedding=query_embedding,
                    top_k=3,
                    min_similarity=0.3,
                )
                procedures = [proc for proc, _ in proc_results]
                if procedures:
                    logger.debug(
                        f"[EcphoryRetrieval] 程序搜索完成 - "
                        f"找到 {len(procedures)} 個相關程序"
                    )
            except Exception as e:
                logger.warning(f"[EcphoryRetrieval] 程序搜索失敗（不影響檢索）: {e}")

        # Step 5: 排序 + 截斷
        episodes.sort(key=lambda ep: similarities.get(ep.id, 0.0), reverse=True)
        final_episodes = episodes[:top_k]

        # 構建 MemoryContext
        context = MemoryContext(
            episodes=final_episodes,
            concepts=concepts,
            procedures=procedures,
            similarities=similarities,
            concept_similarities=concept_similarities,
        )

        logger.debug(
            f"[EcphoryRetrieval] 檢索完成 - "
            f"Episodes: {len(final_episodes)}, Concepts: {len(concepts)}, "
            f"Procedures: {len(procedures)}"
        )

        return context

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 語義概念檢索
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _retrieve_concepts(
        self,
        query_embedding: list[float],
        top_k: int = 5,
        min_similarity: float = 0.3,
        dopamine: float = 0.5,
    ) -> tuple[list[Concept], dict[str, float]]:
        """
        從 SemanticMemory 檢索概念，並進行圖譜展開和矛盾 penalty。

        流程：
          1. 向量搜索 SemanticMemory → 初始概念集合
          2. RELATES_TO 1-hop 展開 → 擴展相關概念
          3. CONTRADICTS penalty → 降低矛盾概念的分數
          4. 按分數排序返回

        Args:
            query_embedding: 查詢向量（已加 query prefix）
            top_k: 最終返回數量
            min_similarity: 最低相似度閾值
            dopamine: 多巴胺（影響展開閾值）

        Returns:
            (concepts, concept_similarities) — 概念列表和對應分數
        """
        # Step 1: 向量搜索
        vector_results = self.semantic.search_by_vector(
            query_embedding,
            top_k=top_k,
            min_similarity=min_similarity,
        )

        if not vector_results:
            return [], {}

        concept_scores: dict[str, float] = {}
        concepts_by_id: dict[str, Concept] = {}

        for concept, sim in vector_results:
            concept_scores[concept.id] = sim
            concepts_by_id[concept.id] = concept

        # Step 2: RELATES_TO 1-hop 展開
        graph = self.episodic.graph
        seed_concept_ids = list(concept_scores.keys())

        for concept_id in seed_concept_ids:
            if not graph.has_node(concept_id):
                continue

            # 查找 RELATES_TO 鄰居
            relations = graph.get_concept_relations(
                concept_id, edge_type="RELATES_TO"
            )

            # 多巴胺影響展開閾值：DA 高 → 更願意展開弱關聯
            expand_threshold = max(0.2, 0.5 - dopamine * 0.3)

            for neighbor_id, edge_type, weight in relations:
                if neighbor_id in concept_scores:
                    continue  # 已經在結果中
                if weight < expand_threshold:
                    continue

                # 從 SemanticMemory 取得 Concept 物件
                neighbor_concept = self.semantic.get(neighbor_id)
                if neighbor_concept is None:
                    continue

                # 展開的概念分數 = 來源分數 × 邊權重 × 衰減因子
                derived_score = concept_scores[concept_id] * weight * 0.7
                if derived_score >= min_similarity:
                    concept_scores[neighbor_id] = derived_score
                    concepts_by_id[neighbor_id] = neighbor_concept

        # Step 3: CONTRADICTS penalty
        self._apply_contradicts_penalty(concept_scores, graph)

        # Step 4: 排序 + 截斷
        sorted_ids = sorted(
            concept_scores.keys(),
            key=lambda cid: concept_scores[cid],
            reverse=True,
        )[:top_k]

        # 過濾掉被 penalty 壓到 min_similarity 以下的
        final_concepts = []
        final_scores: dict[str, float] = {}
        for cid in sorted_ids:
            score = concept_scores[cid]
            if score >= min_similarity:
                final_concepts.append(concepts_by_id[cid])
                final_scores[cid] = score

        return final_concepts, final_scores

    @staticmethod
    def _apply_contradicts_penalty(
        concept_scores: dict[str, float],
        graph,
    ) -> None:
        """
        對有 CONTRADICTS 邊的概念施加 penalty。

        如果概念 A 在結果中，且 A→B 有 CONTRADICTS 邊，
        B 的分數會乘以 CONTRADICTS_PENALTY（0.3）。

        這是側向抑制（lateral inhibition）——矛盾的概念會互相壓制。

        Args:
            concept_scores: concept_id → score（會被直接修改）
            graph: MemoryGraph 實例
        """
        penalty_targets: dict[str, float] = {}

        for concept_id in list(concept_scores.keys()):
            if not graph.has_node(concept_id):
                continue

            relations = graph.get_concept_relations(
                concept_id, edge_type="CONTRADICTS"
            )

            for neighbor_id, _, weight in relations:
                if neighbor_id in concept_scores:
                    # 記錄最強的 penalty（如果被多個概念矛盾）
                    penalty = weight * EcphoryRetrieval.CONTRADICTS_PENALTY
                    if neighbor_id not in penalty_targets:
                        penalty_targets[neighbor_id] = penalty
                    else:
                        # 取更強的 penalty（更低的乘數）
                        penalty_targets[neighbor_id] = min(
                            penalty_targets[neighbor_id], penalty
                        )

        # 施加 penalty
        for target_id, penalty_factor in penalty_targets.items():
            original = concept_scores[target_id]
            concept_scores[target_id] = original * penalty_factor
            logger.debug(
                f"[EcphoryRetrieval] CONTRADICTS penalty: "
                f"{target_id[:12]} {original:.3f} → {concept_scores[target_id]:.3f}"
            )
