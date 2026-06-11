"""
EcphoryRetrieval - 聯想檢索引擎

Phase 1.5: 向量檢索 + 圖譜多跳展開（混合架構）
Phase 2+: 添加神經化學參數動態調控
"""

from __future__ import annotations

import logging

from echo_memory.schemas.memory_context import MemoryContext
from echo_memory.storage.episodic import EpisodicMemory
from echo_memory.storage.semantic import SemanticMemory

logger = logging.getLogger("echo_memory.retrieval")


class EcphoryRetrieval:
    """
    EcphoryRAG 檢索引擎 - 混合架構。
    
    檢索流程：
      1. NumPy 餘弦相似度檢索 → 定位種子節點（高相似度）
      2. NetworkX 圖譜展開 → 沿 PRECEDES 邊多跳擴展
      3. 動態權重過濾 → 時間衰減 + 頻率 + 顯著性
      4. 整合返回 MemoryContext
    """
    
    def __init__(
        self,
        episodic: EpisodicMemory,
        semantic: SemanticMemory,
    ):
        self.episodic = episodic
        self.semantic = semantic
    
    def retrieve(
        self,
        query: str,
        dopamine: float = 0.5,
        serotonin: float = 0.5,
        top_k: int = 10,
        min_similarity: float = 0.3,
        enable_graph_expansion: bool = True,
    ) -> MemoryContext:
        """
        執行 EcphoryRAG 檢索。
        
        Args:
            query: 查詢文本
            dopamine: 多巴胺（影響探索廣度）
            serotonin: 血清素（影響謹慎程度）
            top_k: 返回數量
            min_similarity: 最小相似度閾值
            enable_graph_expansion: 是否啟用圖譜展開（Phase 1.5）
        
        Returns:
            MemoryContext 記憶脈絡
        """
        # Step 1: NumPy 餘弦相似度檢索（種子節點）
        # 查詢時加上 retrieval prefix，以符合 bge 系列模型的最佳實踐
        query_embedding = self.episodic.generate_query_embedding(query)
        seed_results = self.episodic.search(
            query_embedding,
            top_k=max(3, top_k // 2),  # 種子數量為目標的一半
            min_similarity=min_similarity,
        )
        
        if not seed_results:
            # 沒有找到相似度達標的種子
            context = MemoryContext(episodes=[], similarities={})
            logger.debug("[EcphoryRetrieval] 未找到相似度達標的種子節點")
            return context
        
        # 提取種子 Episodes 和相似度
        seed_episodes = [ep for ep, _ in seed_results]
        seed_ids = [ep.id for ep in seed_episodes]
        similarities = {ep.id: score for ep, score in seed_results}
        
        logger.debug(
            f"[EcphoryRetrieval] 種子檢索完成 - "
            f"找到 {len(seed_episodes)} 個種子 "
            f"(相似度範圍: {min(similarities.values()):.2f}-{max(similarities.values()):.2f})"
        )
        
        # Step 2: 圖譜多跳展開（Phase 1.5）
        all_episodes = seed_episodes.copy()
        
        if enable_graph_expansion and self.episodic.graph.get_node_count() > 0:
            # 計算展開參數（基於神經化學狀態）
            max_hops = 2 if serotonin > 0.6 else 1  # 血清素高 → 展開深度更大
            min_weight = max(0.15, 0.35 - dopamine * 0.3)  # 多巴胺高 → 閾值更低（更探索）
            
            # 多跳展開
            expanded_nodes = self.episodic.graph.expand_neighbors(
                seed_ids=seed_ids,
                max_hops=max_hops,
                min_weight=min_weight,
                max_results=top_k * 2,
            )
            
            logger.debug(
                f"[EcphoryRetrieval] 圖譜展開完成 - "
                f"發現 {len(expanded_nodes)} 個關聯節點 "
                f"(max_hops={max_hops}, min_weight={min_weight:.2f})"
            )
            
            # 合併展開結果
            for node_id, weight, hops in expanded_nodes:
                if node_id not in similarities:  # 避免重複
                    ep = self.episodic.get(node_id)
                    if ep:
                        all_episodes.append(ep)
                        # 圖譜展開的節點相似度基於邊權重
                        similarities[node_id] = weight * 0.8  # 稍微降權（非直接匹配）
        
        # Step 3: 按相似度/權重排序並限制數量
        all_episodes.sort(key=lambda ep: similarities.get(ep.id, 0.0), reverse=True)
        final_episodes = all_episodes[:top_k]
        
        # 構建 MemoryContext
        context = MemoryContext(episodes=final_episodes, similarities=similarities)
        
        logger.debug(
            f"[EcphoryRetrieval] 檢索完成 - "
            f"返回 {len(final_episodes)} 個情節 "
            f"(種子:{len(seed_episodes)}, 展開:{len(final_episodes)-len(seed_episodes)})"
        )
        
        return context
