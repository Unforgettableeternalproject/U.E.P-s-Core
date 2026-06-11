"""
Concept Schema - 語義記憶節點

對應大腦結構：新皮質（Neocortex）語義知識網路
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
        concept_type: 類型 (entity/rule/abstract)
        embedding: 向量嵌入
        created_at: 創建時間
        updated_at: 最後更新時間
        metadata: 額外元數據
    """

    name: str
    description: str
    concept_type: str = "entity"  # entity | rule | abstract

    id: str = field(default="")
    embedding: list[float] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.id:
            import uuid
            self.id = f"con_{uuid.uuid4().hex[:12]}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "concept_type": self.concept_type,
            "embedding": self.embedding,
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
