"""
EcphoryRetrieval - 聯想檢索引擎

Phase 1: 簡單的向量檢索
Phase 2+: 整合圖譜多跳展開
"""

from __future__ import annotations

import logging

from echo_memory.schemas.memory_context import MemoryContext
from echo_memory.storage.episodic import EpisodicMemory
from echo_memory.storage.semantic import SemanticMemory

logger = logging.getLogger("echo_memory.retrieval")


class EcphoryRetrieval:
    """
    EcphoryRAG 檢索引擎 - MVP 骨架。
    
    Phase 1: 僅使用 FAISS 向量檢索
    Phase 2: 添加圖譜多跳展開
    Phase 3: 神經化學參數動態調控
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
    ) -> MemoryContext:
        """
        執行 EcphoryRAG 檢索。
        
        Args:
            query: 查詢文本
            dopamine: 多巴胺（影響探索廣度）
            serotonin: 血清素（影響謹慎程度）
            top_k: 返回數量
        
        Returns:
            MemoryContext 記憶脈絡
        """
        # Phase 1: 僅向量檢索
        query_embedding = self.episodic._generate_embedding(query)
        episodes = self.episodic.search(query_embedding, top_k=top_k)
        
        context = MemoryContext(episodes=episodes)
        
        logger.debug(f"[EcphoryRetrieval] 檢索完成 - {context.summary()}")
        return context
