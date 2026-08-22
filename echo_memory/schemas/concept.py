"""
Concept Schema - 語義記憶節點

對應大腦結構：新皮質（Neocortex）語義知識網路

Phase 2: 擴展 schema，加入 source_episode_ids、confidence、向量索引支援
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal


@dataclass
class Concept:
    """
    語義概念節點。

    Attributes:
        id: 唯一標識符
        name: 概念名稱（唯一鍵）
        description: 概念描述（canonical summary，應保持精簡）
        concept_type: 類型 (entity/rule/abstract/pattern)
        subject: 這個概念是「關於誰」的 — user（使用者本人的事實/偏好，
            構成 profile）/ agent（agent 自己的風格或規則）/ world（其他知識）。
            與 concept_type 正交：「使用者討厭香菜」是 rule+user，
            「回覆先肯定再建議」是 pattern+agent。
        embedding: 向量嵌入（與 EpisodicMemory 共用相同模型維度）
        source_episode_ids: 蒸餾來源的 Episode ID 列表（溯源用）
        confidence: 概念可信度 (0-1)，越多來源越高
        status: 生命週期狀態 (active/deprecated/archived)
        access_count: 被檢索/引用的次數
        created_at: 創建時間
        updated_at: 最後更新時間
        metadata: 額外元數據，包括：
          - facts: list[dict] — expand 追加的事實（不改 description）
          - patch_history: list[dict] — 變更歷史輕量索引
          - deprecated_at: str — deprecated 時間
          - deprecated_reason: str — deprecated 原因
          - replaced_by: str — 取代此概念的新概念 ID
    """

    name: str
    description: str
    concept_type: str = "entity"  # entity | rule | abstract | pattern
    subject: str = "world"  # user | agent | world

    id: str = field(default="")
    embedding: list[float] = field(default_factory=list)
    source_episode_ids: list[str] = field(default_factory=list)
    confidence: float = 0.5
    status: Literal["active", "deprecated", "archived"] = "active"
    access_count: int = 0
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = field(default_factory=dict)

    # ── Concept Health 指標（GPT 建議 #10）──────────────────────────
    # 這些指標讓檢索時不只看 cosine，也能評估「這個概念健康嗎」。
    # 高相似度 + 高 health → 可放心餵給 LLM
    # 高相似度 + 低 health → 餵給 Verifier，不直接餵給 LLM
    # 高相似度 + deprecated → 只在歷史/矛盾情境使用
    mutation_count: int = 0           # 被修改的總次數（expand/modify/deprecate）
    conflict_count: int = 0           # 有多少未解矛盾（CONTRADICTS 邊數）
    last_verified_at: datetime | None = None  # 最後一次被 Verifier 驗證的時間
    source_diversity: int = 0         # 來源 session 數量（不同 session 的 episode 數）

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
            self.updated_at = datetime.now(timezone.utc)

    @property
    def evidence_count(self) -> int:
        """有多少 episode/fact 支撐此概念。"""
        return len(self.source_episode_ids) + len(self.facts)

    @property
    def stability_hours(self) -> float:
        """距離最後一次修改的小時數。越大越穩定。"""
        now = datetime.now(timezone.utc)
        ts = self.updated_at
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (now - ts).total_seconds() / 3600

    @property
    def health_score(self) -> float:
        """
        綜合健康度分數 [0, 1]。

        公式：
          health = confidence × stability_factor × evidence_factor
                   - conflict_penalty

        各分量：
          - confidence: 已有的可信度（隨來源數漸增）
          - stability_factor: 穩定度（最近沒被修改 → 高）
          - evidence_factor: 證據充分度（多來源 → 高）
          - conflict_penalty: 未解矛盾的懲罰
        """
        import math

        # stability: 超過 24h 未修改 → 1.0, 剛修改 → 0.5
        hours = self.stability_hours
        stability = min(1.0, 0.5 + 0.5 * (1.0 - math.exp(-hours / 24.0)))

        # evidence: 至少有 3 個來源 → 1.0
        ev = self.evidence_count
        evidence = min(1.0, ev / 3.0) if ev > 0 else 0.3

        # conflict penalty: 每個未解矛盾 -0.15，上限 -0.6
        penalty = min(0.6, self.conflict_count * 0.15)

        score = self.confidence * stability * evidence - penalty
        return max(0.0, min(1.0, score))

    @property
    def facts(self) -> list[dict[str, Any]]:
        """取得 expand 追加的事實列表。"""
        return self.metadata.get("facts", [])

    @property
    def patch_history(self) -> list[dict[str, Any]]:
        """取得變更歷史輕量索引。"""
        return self.metadata.get("patch_history", [])

    @property
    def is_active(self) -> bool:
        """概念是否為 active 狀態。"""
        return self.status == "active"

    @property
    def is_deprecated(self) -> bool:
        """概念是否為 deprecated 狀態。"""
        return self.status == "deprecated"

    @property
    def retrieval_weight(self) -> float:
        """檢索權重乘數，依據生命週期狀態。"""
        if self.status == "active":
            return 1.0
        elif self.status == "deprecated":
            return 0.15
        else:  # archived
            return 0.03

    def record_mutation(self) -> None:
        """記錄一次概念變更（expand/modify/deprecate），更新 health 相關指標。"""
        self.mutation_count += 1
        self.updated_at = datetime.now(timezone.utc)

    def record_verification(self) -> None:
        """記錄一次 Verifier 驗證。"""
        self.last_verified_at = datetime.now(timezone.utc)

    def update_source_diversity(self, session_ids: set[str] | None = None) -> None:
        """根據 source_episode_ids 計算來源多樣性（不同 session 數）。"""
        if session_ids is not None:
            self.source_diversity = len(session_ids)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "concept_type": self.concept_type,
            "subject": self.subject,
            "embedding": self.embedding,
            "source_episode_ids": self.source_episode_ids,
            "confidence": self.confidence,
            "status": self.status,
            "access_count": self.access_count,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "metadata": self.metadata,
            "mutation_count": self.mutation_count,
            "conflict_count": self.conflict_count,
            "last_verified_at": (
                self.last_verified_at.isoformat()
                if self.last_verified_at else None
            ),
            "source_diversity": self.source_diversity,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Concept:
        data = data.copy()
        for key in ["created_at", "updated_at", "last_verified_at"]:
            if key in data and isinstance(data[key], str):
                data[key] = datetime.fromisoformat(data[key])
        # 向後相容：舊資料可能沒有 health 欄位
        valid_fields = {f.name for f in cls.__dataclass_fields__.values()}
        data = {k: v for k, v in data.items() if k in valid_fields}
        return cls(**data)
