"""
Procedure Schema - 程序記憶節點

對應大腦結構：基底核（Basal Ganglia）+ 小腦（Cerebellum）程序性記憶

第四層記憶：技能/SOP/操作步驟的結構化記錄。
由 ProcedureExtractor 從高顯著性 Episode 中蒸餾而來。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class Procedure:
    """
    程序記憶節點 — 可複用的操作步驟或決策流程。

    Attributes:
        id: 唯一標識符（proc_xxxx）
        name: 程序名稱（唯一鍵）
        description: 一句話描述
        steps: 步驟序列
        domain: 領域標籤（coding/analysis/planning/general）
        trigger_patterns: 觸發條件關鍵詞
        embedding: 向量嵌入（與其他記憶層共用同一模型維度）
        success_count: 執行成功次數
        failure_count: 執行失敗次數
        source_episode_ids: 蒸餾來源的 Episode ID 列表
        version: 版本號（refine 時遞增）
        parent_id: 被精化的前一版 Procedure ID（REFINES 關係）
        is_archived: 是否已封存
        created_at: 創建時間
        last_used: 最近使用時間
        metadata: 額外元數據
    """

    name: str
    description: str
    steps: list[str] = field(default_factory=list)
    domain: str = "general"  # coding | analysis | planning | general
    trigger_patterns: list[str] = field(default_factory=list)

    id: str = field(default="")
    embedding: list[float] = field(default_factory=list)
    success_count: int = 0
    failure_count: int = 0
    source_episode_ids: list[str] = field(default_factory=list)
    version: int = 1
    parent_id: str | None = None
    is_archived: bool = False
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_used: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.id:
            self.id = f"proc_{uuid.uuid4().hex[:12]}"

    @property
    def success_rate(self) -> float:
        """成功率（0-1），無執行記錄時回傳 0。"""
        total = self.success_count + self.failure_count
        return self.success_count / total if total > 0 else 0.0

    @property
    def total_uses(self) -> int:
        return self.success_count + self.failure_count

    def to_dict(self) -> dict[str, Any]:
        """序列化為字典（用於 JSON 存儲）"""
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "steps": self.steps,
            "domain": self.domain,
            "trigger_patterns": self.trigger_patterns,
            "embedding": self.embedding,
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "source_episode_ids": self.source_episode_ids,
            "version": self.version,
            "parent_id": self.parent_id,
            "is_archived": self.is_archived,
            "created_at": self.created_at.isoformat(),
            "last_used": self.last_used.isoformat(),
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Procedure:
        """從字典反序列化"""
        data = data.copy()
        for key in ("created_at", "last_used"):
            if key in data and isinstance(data[key], str):
                data[key] = datetime.fromisoformat(data[key])
        return cls(**data)

    def __repr__(self) -> str:
        return (
            f"Procedure(id={self.id!r}, name={self.name!r}, "
            f"v{self.version}, {len(self.steps)} steps, "
            f"success_rate={self.success_rate:.0%})"
        )
