"""
SemanticMemory - 語義記憶存儲（佔位符）

Phase 1: 簡單的 dict 存儲
Phase 2+: 整合 NetworkX 圖譜
"""

from __future__ import annotations

import logging
from pathlib import Path

from echo_memory.schemas.concept import Concept

logger = logging.getLogger("echo_memory.semantic")


class SemanticMemory:
    """語義記憶管理器 - MVP 骨架"""

    def __init__(self, storage_path: str | Path):
        self.storage_path = Path(storage_path)
        self.storage_path.mkdir(parents=True, exist_ok=True)

        self._concepts: dict[str, Concept] = {}

        logger.info("[SemanticMemory] 初始化完成（簡化版）")

    def upsert_concept(
        self,
        name: str,
        description: str,
        concept_type: str = "entity",
    ) -> str:
        """插入或更新概念"""
        if name in self._concepts:
            concept = self._concepts[name]
            concept.description = description
            concept_id = concept.id
        else:
            concept = Concept(
                name=name,
                description=description,
                concept_type=concept_type,
            )
            self._concepts[name] = concept
            concept_id = concept.id

        return concept_id

    def get(self, concept_id: str) -> Concept | None:
        """根據 ID 獲取概念"""
        for concept in self._concepts.values():
            if concept.id == concept_id:
                return concept
        return None

    def count(self) -> int:
        return len(self._concepts)

    def clear(self):
        self._concepts.clear()
