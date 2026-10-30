"""
Episode Schema - 情節記憶節點

對應大腦結構：海馬迴（Hippocampus）情節記憶編碼
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
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
        dream_scanned_at: 最近一次被 dream 週期評估（重播或判定未達門檻）的時間；None＝從未評估。
            與 is_dreamed 獨立：不影響蒸餾候選，僅供重播輪替與遺忘判斷使用。
        metadata: 額外元數據
        evidence_ids: 此 Episode 的 EvidenceUnit id（證據存放在獨立的 append-only store）。
            空清單代表舊資料尚未遷移；讀取端應以 legacy_unknown 看待。
        episode_kind: "turn"（對話輪）或 "tool_note"（工具操作產生的記憶，不是對話輪）
    """

    # 必填欄位
    user_input: str
    agent_response: str
    session_id: str
    content_summary: str

    # 自動生成欄位
    id: str = field(default="")
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    # 向量與權重
    embedding: list[float] = field(default_factory=list)
    salience_score: float = 0.5
    da_weight: float = 0.5
    ht_weight: float = 0.5

    # 狀態標記
    is_dreamed: bool = False
    dream_scanned_at: datetime | None = None

    # 擴展資訊
    metadata: dict[str, Any] = field(default_factory=dict)

    # 證據參照與種類（舊 episodes.json 沒有這兩欄，皆需預設值）
    evidence_ids: list[str] = field(default_factory=list)
    episode_kind: str = "turn"

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
            "dream_scanned_at": (
                self.dream_scanned_at.isoformat() if self.dream_scanned_at else None
            ),
            "metadata": self.metadata,
            "evidence_ids": list(self.evidence_ids),
            "episode_kind": self.episode_kind,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Episode:
        """從字典反序列化"""
        data = data.copy()
        if "timestamp" in data and isinstance(data["timestamp"], str):
            data["timestamp"] = datetime.fromisoformat(data["timestamp"])
        # 舊資料沒有此欄位視為 None
        scanned = data.get("dream_scanned_at")
        data["dream_scanned_at"] = (
            datetime.fromisoformat(scanned) if isinstance(scanned, str) and scanned else None
        )
        return cls(**data)

    @property
    def age_hours(self) -> float:
        """距離創建的小時數"""
        now = datetime.now(timezone.utc)
        ts = self.timestamp if self.timestamp.tzinfo else self.timestamp.replace(tzinfo=timezone.utc)
        delta = now - ts
        return delta.total_seconds() / 3600
