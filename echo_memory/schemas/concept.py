"""
Concept Schema - 語義記憶節點

對應大腦結構：新皮質（Neocortex）語義知識網路

Phase 2: 擴展 schema，加入 source_episode_ids、confidence、向量索引支援
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class Concept:
    """
    語義概念節點。

    Attributes:
        id: 唯一標識符
        name: 概念名稱（唯一鍵）
        description: 概念描述
        concept_type: 類型 (entity/rule/abstract/pattern)
        embedding: 向量嵌入（與 EpisodicMemory 共用相同模型維度）
        source_episode_ids: 蒸餾來源的 Episode ID 列表（溯源用）
        confidence: 概念可信度 (0-1)，越多來源越高
        access_count: 被檢索/引用的次數
        created_at: 創建時間
        updated_at: 最後更新時間
        metadata: 額外元數據
    """

    name: str
    description: str
    concept_type: str = "entity"  # entity | rule | abstract | pattern

    id: str = field(default="")
    embedding: list[float] = field(default_factory=list)
    source_episode_ids: list[str] = field(default_factory=list)
    confidence: float = 0.5
    access_count: int = 0
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.id:
            import uuid
            self.id = f"con_{uuid.uuid4().hex[:12]}"

    def add_source_episode(self, episode_id: str) -> None:
        """記錄蒸餾來源 Episode，並自動更新 confidence。"""
        if episode_id not in self.source_episode_ids:
            self.source_episode_ids.append(episode_id)
            # 每多一個來源，confidence 漸進趨近 1.0
            # confidence = 1 - 1/(1+n)  → n=1: 0.5, n=3: 0.75, n=10: 0.91
            n = len(self.source_episode_ids)
            self.confidence = 1.0 - 1.0 / (1.0 + n)
            self.updated_at = datetime.utcnow()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "concept_type": self.concept_type,
            "embedding": self.embedding,
            "source_episode_ids": self.source_episode_ids,
            "confidence": self.confidence,
            "access_count": self.access_count,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Concept:
        data = data.copy()
        for key in ["created_at", "updated_at"]:
            if key in data and isinstance(data[key], str):
                data[key] = datetime.fromisoformat(data[key])
        return cls(**data)
