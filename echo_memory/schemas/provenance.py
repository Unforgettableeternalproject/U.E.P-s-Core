"""
Provenance Schema - Concept 多階衍生來源紀錄與解析結果

Concept 的來源以「derivations」記錄（append-only）：每筆指向一個 parent
（evidence / episode / concept），並記下產生者與「當時 parent 的版本」。
解析時沿 parent 一路回溯到原始 Evidence，歷史不隨 parent 後續修改而改寫。

引擎只依結構欄位判斷，不從文字內容推測來源（不使用 keyword / regex）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

DERIVATION_KINDS = ("evidence", "episode", "concept")
# 產生者；unknown 保留給無法辨識呼叫端的舊式寫入
PRODUCERS = ("dream", "reflection", "extractor", "conversation", "reviewer", "unknown")


@dataclass(frozen=True)
class Derivation:
    """一筆衍生紀錄：Concept 由 parent 衍生而來。

    Attributes:
        kind: parent 種類 (evidence/episode/concept)
        id: parent ID
        producer: 產生者 (dream/reflection/extractor/conversation/reviewer/unknown)
        parent_version: 衍生當時 parent concept 的 version（僅 kind=concept 有意義；
            None 代表未綁定，解析時取 parent 目前狀態並標示 unbound_versions）
        at_version: 此紀錄寫入時「子 Concept」的 version。
            子 Concept 被別人以版本 v 引用時，只看得到 at_version <= v 的紀錄。
        recorded_at: 寫入時間
        ref: 輔助資訊（patch_id／transaction_id 等），只記錄不解讀
    """

    kind: str
    id: str
    producer: str = "unknown"
    parent_version: int | None = None
    at_version: int = 1
    recorded_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    ref: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.kind not in DERIVATION_KINDS:
            raise ValueError(f"derivation kind 必須是 {DERIVATION_KINDS} 之一，收到 {self.kind!r}")
        if self.producer not in PRODUCERS:
            raise ValueError(f"producer 必須是 {PRODUCERS} 之一，收到 {self.producer!r}")
        if not self.id:
            raise ValueError("derivation id 不可為空")

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "id": self.id,
            "producer": self.producer,
            "parent_version": self.parent_version,
            "at_version": self.at_version,
            "recorded_at": self.recorded_at.isoformat(),
            "ref": dict(self.ref) if self.ref else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Derivation:
        recorded = data.get("recorded_at")
        if isinstance(recorded, str):
            recorded = datetime.fromisoformat(recorded)
        if recorded is None:
            recorded = datetime.now(timezone.utc)
        return cls(
            kind=data["kind"],
            id=data["id"],
            producer=data.get("producer", "unknown"),
            parent_version=data.get("parent_version"),
            at_version=int(data.get("at_version", 1)),
            recorded_at=recorded,
            ref=dict(data["ref"]) if data.get("ref") else None,
        )


# ─── 解析結果 ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PathStep:
    """回溯路徑上的一個節點。version 僅 concept 節點有值（解析時綁定的版本）。"""

    kind: str  # concept | episode | evidence
    id: str
    version: int | None = None
    producer: str | None = None  # 從上一層走到這一層的衍生者

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "id": self.id, "version": self.version, "producer": self.producer}


@dataclass(frozen=True)
class ProvenancePath:
    """從根 Concept 到一個原始 Evidence 的完整路徑。"""

    steps: tuple[PathStep, ...]

    @property
    def evidence_id(self) -> str:
        return self.steps[-1].id

    def to_dict(self) -> dict[str, Any]:
        return {"steps": [s.to_dict() for s in self.steps]}


@dataclass(frozen=True)
class MissingRef:
    """無法解析的來源（已刪除、被合併移除、或從未存在）。"""

    kind: str
    id: str
    via: tuple[str, ...]  # 從根到發現缺失處的 concept id 路徑

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "id": self.id, "via": list(self.via)}


@dataclass(frozen=True)
class CycleReport:
    """循環依賴：cycle 為首尾相同的 concept id 序列，例如 (A, B, A)。"""

    cycle: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"cycle": list(self.cycle)}


@dataclass
class ProvenanceResult:
    """單一 Concept（指定版本）的解析結果。"""

    concept_id: str
    bound_version: int | None  # 實際解析所用版本
    current_version: int | None  # 解析當下 Concept 的最新版本；Concept 不存在為 None
    found: bool = True
    evidence: list[Any] = field(default_factory=list)  # list[EvidenceUnit]，依 id 去重
    paths: list[ProvenancePath] = field(default_factory=list)  # 每個 evidence 一條代表路徑
    missing: list[MissingRef] = field(default_factory=list)
    cycles: list[CycleReport] = field(default_factory=list)
    unbound_versions: list[str] = field(default_factory=list)  # 未綁定版本的 parent concept id
    truncated: bool = False  # 超過深度上限而中止

    @property
    def stale(self) -> bool:
        """目前版本已晚於解析所用版本（歷史快照與現況不同）。"""
        return (
            self.bound_version is not None
            and self.current_version is not None
            and self.current_version > self.bound_version
        )

    @property
    def evidence_ids(self) -> list[str]:
        return [u.id for u in self.evidence]

    @property
    def complete(self) -> bool:
        """沒有缺失、循環或截斷。"""
        return self.found and not self.missing and not self.cycles and not self.truncated

    def to_dict(self) -> dict[str, Any]:
        return {
            "concept_id": self.concept_id,
            "bound_version": self.bound_version,
            "current_version": self.current_version,
            "found": self.found,
            "stale": self.stale,
            "complete": self.complete,
            "evidence_ids": self.evidence_ids,
            "paths": [p.to_dict() for p in self.paths],
            "missing": [m.to_dict() for m in self.missing],
            "cycles": [c.to_dict() for c in self.cycles],
            "unbound_versions": list(self.unbound_versions),
            "truncated": self.truncated,
        }


@dataclass
class IndependentSupport:
    """多個 Concept 的獨立來源統計（以原始 Evidence 去重）。

    independent_count 只計 provenance_status="known" 且未 tombstone 的原始 Evidence；
    legacy_unknown 單獨計數，不併入 independent_count、也不算使用者確認。
    """

    concept_ids: tuple[str, ...]
    independent_ids: list[str] = field(default_factory=list)  # known、去重
    legacy_unknown_ids: list[str] = field(default_factory=list)
    tombstoned_ids: list[str] = field(default_factory=list)
    user_attested_ids: list[str] = field(default_factory=list)  # can_attest_user_claim 者
    independent_episode_count: int = 0  # known evidence 依所屬 episode 再合併後的數量
    shared_ids: list[str] = field(default_factory=list)  # 被 >1 個 concept 共用的 evidence
    per_concept: dict[str, list[str]] = field(default_factory=dict)
    missing: list[MissingRef] = field(default_factory=list)
    cycles: list[CycleReport] = field(default_factory=list)

    @property
    def independent_count(self) -> int:
        return len(self.independent_ids)

    @property
    def legacy_unknown_count(self) -> int:
        return len(self.legacy_unknown_ids)

    @property
    def user_attested_count(self) -> int:
        return len(self.user_attested_ids)

    def to_dict(self) -> dict[str, Any]:
        return {
            "concept_ids": list(self.concept_ids),
            "independent_count": self.independent_count,
            "independent_episode_count": self.independent_episode_count,
            "legacy_unknown_count": self.legacy_unknown_count,
            "user_attested_count": self.user_attested_count,
            "independent_ids": list(self.independent_ids),
            "legacy_unknown_ids": list(self.legacy_unknown_ids),
            "tombstoned_ids": list(self.tombstoned_ids),
            "user_attested_ids": list(self.user_attested_ids),
            "shared_ids": list(self.shared_ids),
            "per_concept": {k: list(v) for k, v in self.per_concept.items()},
            "missing": [m.to_dict() for m in self.missing],
            "cycles": [c.to_dict() for c in self.cycles],
        }
