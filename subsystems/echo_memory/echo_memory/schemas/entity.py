"""
Entity Schema - 實體記憶節點

從情節對話中提取的具體實體（人名、地名、產品名、事件名等）。
對應知識圖譜中的實體節點。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class Entity:
    """
    實體節點。

    從 Episode 中自動提取，代表現實世界的具體物件。

    Attributes:
        id: 唯一標識符（ent_xxxx）
        name: 實體名稱
        entity_type: 實體類型（person/place/concept/product/event/other）
        description: 一句話描述
        mention_count: 被提及的次數
        source_episode_ids: 出現過此實體的 Episode ID 列表
        first_seen: 首次出現時間
        last_seen: 最近出現時間
        metadata: 額外元數據
    """

    # 核心欄位（可選，由 __post_init__ 處理預設值）
    id: str = field(default="")
    name: str = ""
    entity_type: str = "other"  # person | place | concept | product | event | other
    description: str = ""
    mention_count: int = 0
    source_episode_ids: list[str] = field(default_factory=list)
    first_seen: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_seen: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        """自動生成 ID"""
        if not self.id:
            self.id = f"ent_{uuid.uuid4().hex[:12]}"

    def to_dict(self) -> dict[str, Any]:
        """序列化為字典（用於 JSON 存儲）"""
        return {
            "id": self.id,
            "name": self.name,
            "entity_type": self.entity_type,
            "description": self.description,
            "mention_count": self.mention_count,
            "source_episode_ids": self.source_episode_ids,
            "first_seen": self.first_seen.isoformat(),
            "last_seen": self.last_seen.isoformat(),
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Entity:
        """從字典反序列化"""
        data = data.copy()
        for key in ("first_seen", "last_seen"):
            if key in data and isinstance(data[key], str):
                data[key] = datetime.fromisoformat(data[key])
        return cls(**data)

    def __repr__(self) -> str:
        return f"Entity(id={self.id!r}, name={self.name!r}, type={self.entity_type!r})"
