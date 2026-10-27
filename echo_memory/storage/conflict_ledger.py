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


def compute_pair_digest(concept_a: Any, concept_b: Any) -> str:
    """兩個概念「證據基礎」的組合摘要（與順序無關）；ledger 的去重鍵。"""
    import hashlib

    from echo_memory.schemas.evidence_review import compute_basis_digest

    parts = sorted([
        f"{concept_a.id}:{compute_basis_digest(concept_a)}",
        f"{concept_b.id}:{compute_basis_digest(concept_b)}",
    ])
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


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

    def record_resolved(
        self,
        concept_a_id: str,
        concept_b_id: str,
        status: ConflictStatus,
        reason: str,
        pair_digest: str,
        relation: str = "",
        concept_a_name: str = "",
        concept_b_name: str = "",
        patch_id: str | None = None,
    ) -> ConflictRecord:
        """登記一筆「已解決」的配對判決（different_scope／supersedes），供 Dream 去重。

        pair_digest 是兩個概念當下證據基礎的組合摘要；配對內容不變時
        is_pair_resolved 為 True，Dream 不再重複送審同一對。
        若該配對已有未解矛盾（先前判為 contradicts）則直接就地解決，不另開紀錄。
        """
        if status == "unresolved":
            raise ValueError("record_resolved() 不能設為 unresolved")
        meta = {"relation": relation, "pair_digest": pair_digest}
        for record in self._records.values():
            if record.involves == {concept_a_id, concept_b_id}:
                # 保留「最初」解決前的狀態，供 unresolve 還原（rollback 用）；
                # 重複判決不覆蓋，否則 rollback 會還原到上一次判決而不是原本的未解矛盾
                record.metadata.setdefault("pre_resolution", {
                    "status": record.status,
                    "resolved_at": record.resolved_at.isoformat() if record.resolved_at else None,
                    "resolution_reason": record.resolution_reason,
                    "resolution_patch_id": record.resolution_patch_id,
                    "pair_digest": record.metadata.get("pair_digest"),
                    "relation": record.metadata.get("relation"),
                })
                record.resolve(status, reason, patch_id)
                record.metadata.update(meta)
                self.save()
                return record
        record = ConflictRecord(
            concept_a_id=concept_a_id,
            concept_b_id=concept_b_id,
            concept_a_name=concept_a_name,
            concept_b_name=concept_b_name,
            contradiction_strength=0.0,
            metadata={**meta, "created_resolved": True},
        )
        record.resolve(status, reason, patch_id)
        self._records[record.conflict_id] = record
        self.save()
        logger.info(
            f"[ConflictLedger] 配對已判決: {record.conflict_id} → {status} "
            f"(relation={relation})"
        )
        return record

    def unresolve(self, concept_a_id: str, concept_b_id: str) -> bool:
        """還原 record_resolved 的判決（supersede／different_scope 被 rollback 時用）。

        解決前已有未解矛盾記錄 -> 還原成解決前的狀態並清掉 pair_digest；
        該記錄是 record_resolved 新建的 -> 直接移除。沒有記錄回傳 False。
        """
        record = self.find_between(concept_a_id, concept_b_id)
        if record is None:
            return False
        if record.metadata.get("created_resolved"):
            self._records.pop(record.conflict_id, None)
            self.save()
            return True
        pre = record.metadata.pop("pre_resolution", None)
        if pre is None:
            return False
        record.status = pre.get("status", "unresolved")
        record.resolved_at = (
            datetime.fromisoformat(pre["resolved_at"]) if pre.get("resolved_at") else None
        )
        record.resolution_reason = pre.get("resolution_reason", "")
        record.resolution_patch_id = pre.get("resolution_patch_id")
        for key in ("pair_digest", "relation"):
            if pre.get(key) is None:
                record.metadata.pop(key, None)
            else:
                record.metadata[key] = pre[key]
        self.save()
        return True

    def mark_reevaluated(
        self, concept_a_id: str, concept_b_id: str, pair_digest: str,
    ) -> bool:
        """在既有（未解）矛盾記錄上記下「已重判過」的配對 digest；沒有記錄回傳 False。

        內容不變時 Dream 不再重判同一對（is_pair_reevaluated）。
        """
        record = self.find_between(concept_a_id, concept_b_id)
        if record is None:
            return False
        record.metadata["reevaluated_digest"] = pair_digest
        self.save()
        return True

    def is_pair_reevaluated(
        self, concept_a_id: str, concept_b_id: str, pair_digest: str,
    ) -> bool:
        record = self.find_between(concept_a_id, concept_b_id)
        return record is not None and record.metadata.get("reevaluated_digest") == pair_digest

    def is_pair_resolved(
        self,
        concept_a_id: str,
        concept_b_id: str,
        pair_digest: str,
    ) -> bool:
        """配對是否已有「同內容」的已解決判決（pair_digest 相符）。"""
        pair = {concept_a_id, concept_b_id}
        return any(
            r.involves == pair
            and r.is_resolved
            and r.metadata.get("pair_digest") == pair_digest
            for r in self._records.values()
        )

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
