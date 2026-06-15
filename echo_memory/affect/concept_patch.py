"""
ConceptPatch — 語義概念原子變更操作資料結構

對應設計文件：docs/design/thought.md

設計原則：
  - 每個 ConceptPatch 代表對單一 Concept 節點的一次原子操作
  - PatchTransaction 封裝一輪對話產生的所有 patch（跨概念）
  - concept.metadata["patch_history"] 只存輕量索引（PatchHistoryEntry）
  - 獨立 audit_log.jsonl 存完整 transaction
  - expand 不直接修改 description，用 ExpandMode 區分三種語意
  - deprecated 概念不刪除，檢索降權，LLM 上下文預設過濾
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Enums
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class ExpandMode(Enum):
    """
    expand action 的三種語意模式。

    - ADD_FACT: 新增 fact 到 metadata["facts"]，不改 canonical description
    - REFINE_SUMMARY: 輕微改寫 description（需較高 evidence_strength）
    - ADD_RELATION: 新增圖譜邊（RELATES_TO / DERIVED_FROM）
    """
    ADD_FACT = "add_fact"
    REFINE_SUMMARY = "refine_summary"
    ADD_RELATION = "add_relation"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ConceptState
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@dataclass
class ConceptState:
    """
    概念生命週期狀態。

    檢索權重：active × 1.0 / deprecated × 0.15 / archived × 0.03
    LLM 上下文：deprecated/archived 預設不放入，除非矛盾檢測、rollback、歷史查詢。
    deprecate 時至少需要 deprecated_reason 或 replaced_by 之一（由 Inhibitor 強制）。
    """
    status: Literal["active", "deprecated", "archived"] = "active"
    deprecated_at: datetime | None = None
    deprecated_reason: str | None = None
    replaced_by: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "deprecated_at": (
                self.deprecated_at.isoformat() if self.deprecated_at else None
            ),
            "deprecated_reason": self.deprecated_reason,
            "replaced_by": self.replaced_by,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ConceptState:
        data = data.copy()
        if data.get("deprecated_at") and isinstance(data["deprecated_at"], str):
            data["deprecated_at"] = datetime.fromisoformat(data["deprecated_at"])
        return cls(**data)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# PatchEvidence
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@dataclass
class PatchEvidence:
    """
    支持 ConceptPatch 的證據。

    證據權重：user_input > verifier evidence >> llm_response（輔助）。

    evidence_kind:
      - explicit_user_claim: 用戶明確陳述（最高可信度）
      - user_correction: 用戶主動糾正
      - semantic_conflict: 與當前語境語意衝突
      - llm_inference: LLM 推論（需謹慎）
      - dream_consolidation: Dream Engine 離線整合
    """
    evidence_kind: Literal[
        "explicit_user_claim",
        "user_correction",
        "semantic_conflict",
        "llm_inference",
        "dream_consolidation",
    ]
    evidence_strength: float  # [0, 1]

    verifier_status: Literal["valid", "expand", "modify", "deprecate"]
    verifier_reason: str

    retrieved_concept_ids: list[str] = field(default_factory=list)
    source_episode_ids: list[str] = field(default_factory=list)

    user_input_excerpt: str | None = None
    llm_corroboration: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_kind": self.evidence_kind,
            "evidence_strength": round(self.evidence_strength, 4),
            "verifier_status": self.verifier_status,
            "verifier_reason": self.verifier_reason,
            "retrieved_concept_ids": self.retrieved_concept_ids,
            "source_episode_ids": self.source_episode_ids,
            "user_input_excerpt": self.user_input_excerpt,
            "llm_corroboration": self.llm_corroboration,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PatchEvidence:
        return cls(**data)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ConceptPatch
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@dataclass
class ConceptPatch:
    """
    單一概念的原子變更操作。

    action:
      - create: 新建概念節點
      - expand: 新增 fact / refine summary / 新增關係邊
      - modify: 修改現有欄位值
      - deprecate: 標記為 deprecated
      - link: 新增圖譜邊
      - unlink: 移除圖譜邊
      - merge: 合併兩個概念

    雙重 confidence:
      - old/new_confidence: 概念本身的可信度
      - patch_confidence: 此次變更操作的可信度

    風險等級:
      - low: create、expand/ADD_FACT（強證據）
      - medium: expand/REFINE_SUMMARY、link/unlink
      - high: modify、deprecate、merge
    """
    # 身份識別
    transaction_id: str
    concept_name: str
    action: Literal[
        "create", "expand", "modify", "deprecate",
        "link", "unlink", "merge",
    ]
    evidence: PatchEvidence

    # 自動生成
    patch_id: str = field(default="")

    # 目標概念
    concept_id: str | None = None

    # JSON Patch 語意
    target_path: str = ""      # 例如 "description", "metadata.facts", "edges.RELATES_TO"
    old_value: Any = None       # 變更前（rollback 用）
    new_value: Any = None       # 變更後

    # 雙重 confidence
    old_confidence: float | None = None
    new_confidence: float | None = None
    patch_confidence: float = 0.5

    # 語意細分（僅 action == "expand"）
    expand_mode: ExpandMode | None = None

    # 風險與狀態
    risk_level: Literal["low", "medium", "high"] = "medium"
    status: Literal[
        "pending", "approved", "rejected",
        "applied", "rolled_back", "held",
    ] = "pending"

    # 可選
    rejection_reason: str | None = None
    rollback_payload: dict[str, Any] | None = None
    merge_target_id: str | None = None

    def __post_init__(self) -> None:
        if not self.patch_id:
            self.patch_id = f"patch_{uuid.uuid4().hex[:12]}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "patch_id": self.patch_id,
            "transaction_id": self.transaction_id,
            "concept_id": self.concept_id,
            "concept_name": self.concept_name,
            "action": self.action,
            "expand_mode": self.expand_mode.value if self.expand_mode else None,
            "target_path": self.target_path,
            "old_value": self.old_value,
            "new_value": self.new_value,
            "old_confidence": self.old_confidence,
            "new_confidence": (
                round(self.new_confidence, 4) if self.new_confidence is not None
                else None
            ),
            "patch_confidence": round(self.patch_confidence, 4),
            "evidence": self.evidence.to_dict(),
            "risk_level": self.risk_level,
            "status": self.status,
            "rejection_reason": self.rejection_reason,
            "rollback_payload": self.rollback_payload,
            "merge_target_id": self.merge_target_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ConceptPatch:
        data = data.copy()
        data["evidence"] = PatchEvidence.from_dict(data["evidence"])
        if data.get("expand_mode"):
            data["expand_mode"] = ExpandMode(data["expand_mode"])
        return cls(**data)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# PatchTransaction
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@dataclass
class PatchTransaction:
    """
    一輪對話產生的所有概念 patch — 跨概念原子事務。

    一輪對話可能同時觸發多個 ConceptPatch。
    PatchTransaction 聚合它們，提供整輪 commit/rollback 能力。
    """
    patches: list[ConceptPatch]
    user_input: str
    llm_response: str
    session_id: str
    turn_id: str

    # 自動生成
    transaction_id: str = field(default="")
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    # 事務狀態
    status: Literal[
        "pending", "partially_applied", "committed",
        "rolled_back", "failed",
    ] = "pending"

    audit_path: str | None = None

    def __post_init__(self) -> None:
        if not self.transaction_id:
            self.transaction_id = f"tx_{uuid.uuid4().hex[:16]}"

    @property
    def patch_count(self) -> int:
        return len(self.patches)

    @property
    def applied_patches(self) -> list[ConceptPatch]:
        return [p for p in self.patches if p.status == "applied"]

    @property
    def pending_patches(self) -> list[ConceptPatch]:
        return [p for p in self.patches if p.status == "pending"]

    @property
    def held_patches(self) -> list[ConceptPatch]:
        return [p for p in self.patches if p.status == "held"]

    @property
    def has_changes(self) -> bool:
        """是否有任何非 rejected 的 patch。"""
        return any(p.status != "rejected" for p in self.patches)

    def summary(self) -> str:
        """簡短摘要。"""
        by_status: dict[str, int] = {}
        for p in self.patches:
            by_status[p.status] = by_status.get(p.status, 0) + 1
        parts = [f"{v} {k}" for k, v in by_status.items()]
        return f"TX {self.transaction_id[:12]}… [{', '.join(parts)}]"

    def to_dict(self) -> dict[str, Any]:
        return {
            "transaction_id": self.transaction_id,
            "patches": [p.to_dict() for p in self.patches],
            "user_input": self.user_input,
            "llm_response": self.llm_response,
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "timestamp": self.timestamp.isoformat(),
            "status": self.status,
            "audit_path": self.audit_path,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PatchTransaction:
        data = data.copy()
        data["patches"] = [ConceptPatch.from_dict(p) for p in data["patches"]]
        if "timestamp" in data and isinstance(data["timestamp"], str):
            data["timestamp"] = datetime.fromisoformat(data["timestamp"])
        return cls(**data)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# PatchHistoryEntry（輕量索引，存 concept.metadata["patch_history"]）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@dataclass
class PatchHistoryEntry:
    """
    concept.metadata["patch_history"] 的輕量索引條目。
    完整 patch 內容在獨立 audit log。
    """
    patch_id: str
    transaction_id: str
    timestamp: datetime
    action: str
    old_confidence: float | None
    new_confidence: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "patch_id": self.patch_id,
            "transaction_id": self.transaction_id,
            "timestamp": self.timestamp.isoformat(),
            "action": self.action,
            "old_confidence": self.old_confidence,
            "new_confidence": self.new_confidence,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PatchHistoryEntry:
        data = data.copy()
        if isinstance(data.get("timestamp"), str):
            data["timestamp"] = datetime.fromisoformat(data["timestamp"])
        return cls(**data)
