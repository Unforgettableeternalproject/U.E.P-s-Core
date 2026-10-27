"""
ConflictLedger — 矛盾帳本（GPT 建議 #5）

追蹤概念之間的矛盾狀態，提供結構化的矛盾生命週期管理。

核心觀點：很多記憶不是單純「A 錯 B 對」，而是：
  - 時間變了（舊版本 → 新版本）
  - 語境變了（工作 vs 休閒偏好）
  - 使用者修正了設定
  - 兩者可以共存（表面矛盾但實為不同面向）

ConflictRecord 記錄完整生命週期：
  unresolved → resolved / accepted_dual_truth / deprecated_one_side / temporal_evolution
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from echo_memory.storage.atomic import atomic_write_json

logger = logging.getLogger("echo_memory.storage.conflict_ledger")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ConflictRecord
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


# 矛盾的解決狀態
ConflictStatus = Literal[
    "unresolved",              # 未解決
    "resolved",                # 已明確解決（一方被修正）
    "accepted_dual_truth",     # 接受為雙重真相（不同語境各自成立）
    "deprecated_one_side",     # 一方被廢除
    "temporal_evolution",      # 時間演化（舊 → 新）
]


@dataclass
class ConflictRecord:
    """
    矛盾帳本記錄。

    Attributes:
        conflict_id: 唯一識別
        concept_a_id: 矛盾的概念 A
        concept_b_id: 矛盾的概念 B
        concept_a_name: 概念 A 名稱（方便閱讀）
        concept_b_name: 概念 B 名稱
        source_episode_ids: 觸發矛盾偵測的 episode
        detected_at: 偵測時間
        status: 矛盾狀態
        resolved_at: 解決時間
        resolution_reason: 解決原因
        resolution_patch_id: 解決此矛盾的 patch transaction ID
        contradiction_strength: 矛盾強度 [0, 1]
        metadata: 額外資料
    """

    concept_a_id: str
    concept_b_id: str

    conflict_id: str = field(default="")
    concept_a_name: str = ""
    concept_b_name: str = ""
    source_episode_ids: list[str] = field(default_factory=list)
    detected_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    status: ConflictStatus = "unresolved"
    resolved_at: datetime | None = None
    resolution_reason: str = ""
    resolution_patch_id: str | None = None
    contradiction_strength: float = 0.5
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.conflict_id:
            self.conflict_id = f"cf_{uuid.uuid4().hex[:12]}"

    @property
    def is_resolved(self) -> bool:
        return self.status != "unresolved"

    @property
    def involves(self) -> set[str]:
        """涉及的概念 ID 集合。"""
        return {self.concept_a_id, self.concept_b_id}

    def resolve(
        self,
        status: ConflictStatus,
        reason: str,
        patch_id: str | None = None,
    ) -> None:
        """解決矛盾。"""
        if status == "unresolved":
            raise ValueError("resolve() 不能設為 unresolved")
        self.status = status
        self.resolution_reason = reason
        self.resolution_patch_id = patch_id
        self.resolved_at = datetime.now(timezone.utc)

    def to_dict(self) -> dict[str, Any]:
        return {
            "conflict_id": self.conflict_id,
            "concept_a_id": self.concept_a_id,
            "concept_b_id": self.concept_b_id,
            "concept_a_name": self.concept_a_name,
            "concept_b_name": self.concept_b_name,
            "source_episode_ids": self.source_episode_ids,
            "detected_at": self.detected_at.isoformat(),
            "status": self.status,
            "resolved_at": (
                self.resolved_at.isoformat() if self.resolved_at else None
            ),
            "resolution_reason": self.resolution_reason,
            "resolution_patch_id": self.resolution_patch_id,
            "contradiction_strength": round(
                self.contradiction_strength, 4
            ),
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ConflictRecord:
        data = data.copy()
        for key in ["detected_at", "resolved_at"]:
            if key in data and isinstance(data[key], str):
                data[key] = datetime.fromisoformat(data[key])
        valid_fields = {f.name for f in cls.__dataclass_fields__.values()}
        data = {k: v for k, v in data.items() if k in valid_fields}
        return cls(**data)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ConflictLedger
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class ConflictLedger:
    """
    矛盾帳本管理器。

    負責矛盾的記錄、查詢、解決。
    持久化為 JSON 檔案。
    """

    def __init__(self, storage_path: Path | str):
        self._path = Path(storage_path) / "conflict_ledger.json"
        self._records: dict[str, ConflictRecord] = {}
        self._load()

    def _load(self) -> None:
        """從磁碟載入。"""
        if self._path.exists():
            try:
                data = json.loads(self._path.read_text(encoding="utf-8"))
                for item in data:
                    rec = ConflictRecord.from_dict(item)
                    self._records[rec.conflict_id] = rec
                logger.debug(
                    f"[ConflictLedger] 載入 {len(self._records)} 條矛盾記錄"
                )
            except Exception as e:
                logger.warning(f"[ConflictLedger] 載入失敗: {e}")

    def save(self) -> None:
        """持久化到磁碟。"""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = [r.to_dict() for r in self._records.values()]
        atomic_write_json(self._path, data)

    def register(
        self,
        concept_a_id: str,
        concept_b_id: str,
        strength: float,
        concept_a_name: str = "",
        concept_b_name: str = "",
        source_episode_ids: list[str] | None = None,
        reason: str = "",
    ) -> ConflictRecord:
        """
        登記一條新矛盾。若已存在同組概念的未解矛盾，更新強度。

        Returns:
            ConflictRecord
        """
        # 檢查是否已有同組概念的未解矛盾
        existing = self.find_between(concept_a_id, concept_b_id)
        if existing and not existing.is_resolved:
            # 更新強度取最大值
            existing.contradiction_strength = max(
                existing.contradiction_strength, strength
            )
            if source_episode_ids:
                for eid in source_episode_ids:
                    if eid not in existing.source_episode_ids:
                        existing.source_episode_ids.append(eid)
            self.save()
            return existing

        record = ConflictRecord(
            concept_a_id=concept_a_id,
            concept_b_id=concept_b_id,
            concept_a_name=concept_a_name,
            concept_b_name=concept_b_name,
            source_episode_ids=source_episode_ids or [],
            contradiction_strength=strength,
            metadata={"initial_reason": reason} if reason else {},
        )
        self._records[record.conflict_id] = record
        self.save()

        logger.info(
            f"[ConflictLedger] 新增矛盾: {record.conflict_id} "
            f"({concept_a_name or concept_a_id[:8]} ↔ "
            f"{concept_b_name or concept_b_id[:8]}, "
            f"strength={strength:.2f})"
        )
        return record

    def resolve(
        self,
        conflict_id: str,
        status: ConflictStatus,
        reason: str,
        patch_id: str | None = None,
    ) -> ConflictRecord | None:
        """解決一條矛盾。"""
        record = self._records.get(conflict_id)
        if not record:
            logger.warning(
                f"[ConflictLedger] 找不到矛盾 {conflict_id}"
            )
            return None

        record.resolve(status, reason, patch_id)
        self.save()

        logger.info(
            f"[ConflictLedger] 矛盾已解決: {conflict_id} → {status} "
            f"({reason})"
        )
        return record

    def find_between(
        self,
        concept_a_id: str,
        concept_b_id: str,
    ) -> ConflictRecord | None:
        """查找兩個概念之間的矛盾記錄（不分方向）。"""
        pair = {concept_a_id, concept_b_id}
        for record in self._records.values():
            if record.involves == pair:
                return record
        return None

    def get_unresolved(self) -> list[ConflictRecord]:
        """取得所有未解矛盾。"""
        return [r for r in self._records.values() if not r.is_resolved]

    def get_by_concept(self, concept_id: str) -> list[ConflictRecord]:
        """取得涉及某概念的所有矛盾。"""
        return [
            r for r in self._records.values()
            if concept_id in r.involves
        ]

    def get_unresolved_count(self, concept_id: str) -> int:
        """取得某概念的未解矛盾數。"""
        return sum(
            1 for r in self._records.values()
            if concept_id in r.involves and not r.is_resolved
        )

    @property
    def total_count(self) -> int:
        return len(self._records)

    @property
    def unresolved_count(self) -> int:
        return len(self.get_unresolved())

    def get_stats(self) -> dict[str, Any]:
        """統計摘要。"""
        by_status: dict[str, int] = {}
        for r in self._records.values():
            by_status[r.status] = by_status.get(r.status, 0) + 1
        return {
            "total": self.total_count,
            "by_status": by_status,
        }
