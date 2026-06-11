"""
Episode Schema - 情節記憶節點

對應大腦結構：海馬迴（Hippocampus）情節記憶編碼
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class Episode:
    """
    情節記憶節點（Engram）。
    
    Attributes:
        id: 唯一標識符（自動生成）
        user_input: 用戶原始輸入
        agent_response: Agent 回應
        session_id: 所屬會話 ID
        content_summary: 對話摘要
        embedding: 向量嵌入 (384-dim)
        timestamp: 創建時間
        salience_score: 顯著性分數 (0-1)
        da_weight: 虛擬多巴胺強度
        ht_weight: 虛擬血清素強度
        is_dreamed: 是否已在夢境中重播
        metadata: 額外元數據
    """

    # 必填欄位
    user_input: str
    agent_response: str
    session_id: str
    content_summary: str

    # 自動生成欄位
    id: str = field(default="")
    timestamp: datetime = field(default_factory=datetime.utcnow)

    # 向量與權重
    embedding: list[float] = field(default_factory=list)
    salience_score: float = 0.5
    da_weight: float = 0.5
    ht_weight: float = 0.5

    # 狀態標記
    is_dreamed: bool = False

    # 擴展資訊
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        """自動生成 ID"""
        if not self.id:
            import uuid
            self.id = f"ep_{uuid.uuid4().hex[:12]}"

    def to_dict(self) -> dict[str, Any]:
        """序列化為字典（用於 JSON 存儲）"""
        return {
            "id": self.id,
            "user_input": self.user_input,
            "agent_response": self.agent_response,
            "session_id": self.session_id,
            "content_summary": self.content_summary,
            "embedding": self.embedding,
            "timestamp": self.timestamp.isoformat(),
            "salience_score": self.salience_score,
            "da_weight": self.da_weight,
            "ht_weight": self.ht_weight,
            "is_dreamed": self.is_dreamed,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Episode:
        """從字典反序列化"""
        data = data.copy()
        if "timestamp" in data and isinstance(data["timestamp"], str):
            data["timestamp"] = datetime.fromisoformat(data["timestamp"])
        return cls(**data)

    @property
    def age_hours(self) -> float:
        """距離創建的小時數"""
        delta = datetime.utcnow() - self.timestamp
        return delta.total_seconds() / 3600
