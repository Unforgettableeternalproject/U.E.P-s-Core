"""
Concept Schema - 語義記憶節點

對應大腦結構：新皮質（Neocortex）語義知識網路

Phase 2: 擴展 schema，加入 source_episode_ids、confidence、向量索引支援
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

from echo_memory.schemas.evidence_review import compute_basis_digest, split_corroboration
from echo_memory.schemas.provenance import Derivation
from echo_memory.schemas.sharing import (
    LEVEL_PRIVATE,
    LEVEL_SCOPE_SHARED,
    SharingState,
    copy_sharing,
)


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
        version: 單調遞增的內容版本（每次 apply patch / 更新 +1，rollback 不回退），
            供 derivations 綁定「衍生當時 parent 的版本」
        derivations: 多階衍生來源紀錄（append-only，Derivation.to_dict 格式）。
            舊概念沒有此欄位時，解析端以 source_episode_ids 視為 legacy 來源
        evidence_review: 證據審核結果（EvidenceReview.to_dict 格式）。預設 {} ＝ not_checked；
            舊資料沒有此欄位時零遷移載入即 not_checked。是否仍有效請用 effective_review_status()
        sharing: 跨使用者分享狀態（SharingState.to_dict 格式，by_scope 以 ``guild:<id>`` 為鍵）。
            預設 {} ＝所有場域 private；舊資料沒有此欄位時零遷移載入即 private。
            是否仍有效請用 effective_sharing_level()
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
    version: int = 1
    derivations: list[dict[str, Any]] = field(default_factory=list)
    evidence_review: dict[str, Any] = field(default_factory=dict)
    sharing: dict[str, Any] = field(default_factory=dict)

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

    def add_source_episode(self, episode_id: str, producer: str = "unknown") -> None:
        """記錄蒸餾來源 Episode，並自動更新 confidence；同時寫入 derivation。"""
        if episode_id not in self.source_episode_ids:
            self.source_episode_ids.append(episode_id)
            self.add_derivation("episode", episode_id, producer)
            # 每多一個來源，confidence 漸進趨近 1.0
            # confidence = 1 - 1/(1+n)  → n=1: 0.5, n=3: 0.75, n=10: 0.91
            n = len(self.source_episode_ids)
            self.confidence = 1.0 - 1.0 / (1.0 + n)
            self.updated_at = datetime.now(timezone.utc)

    def add_derivation(
        self,
        kind: str,
        parent_id: str,
        producer: str = "unknown",
        parent_version: int | None = None,
        ref: dict[str, Any] | None = None,
    ) -> bool:
        """追加一筆衍生紀錄（append-only；同 kind/id/parent_version 不重複）。

        at_version 取呼叫當下的 self.version，因此若本次寫入同時是一次 mutation，
        須先 record_mutation()／遞增 version 再呼叫，紀錄才會落在新版本。
        """
        for d in self.derivations:
            if (
                d.get("kind") == kind
                and d.get("id") == parent_id
                and d.get("parent_version") == parent_version
            ):
                return False
        self.derivations.append(Derivation(
            kind=kind,
            id=parent_id,
            producer=producer,
            parent_version=parent_version,
            at_version=self.version,
            ref=ref,
        ).to_dict())
        return True

    def remove_derivations_by_patch(self, patch_id: str) -> int:
        """rollback 用：移除由指定 patch 寫入的 derivation，回傳移除數量。"""
        before = len(self.derivations)
        self.derivations = [
            d for d in self.derivations
            if (d.get("ref") or {}).get("patch_id") != patch_id
        ]
        return before - len(self.derivations)

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

    def effective_review_status(self, sp_validator: Any = None) -> str:
        """目前有效的審核狀態（唯讀，不改磁碟、不需 LLM，檢索期可直接用）。

        無審核紀錄、或審核綁定的 basis_digest 與現在的證據基礎不符（description／
        facts／來源已變動）時回傳 not_checked；否則回傳審核當時的 status。

        sp_validator（可選）：``(SpRef) -> bool``，由共享層提供，確認 sp: 佐證參考
        仍存在、未撤回、digest 相符。有提供時，supported 的 intention 若剩下的有效
        佐證（unit id ＋ 通過驗證的 sp 參考）為空，回落 insufficient。
        未提供時 sp: 參考以存檔內容為準（呼叫端無共享層可驗）。
        """
        review = self.evidence_review
        if not isinstance(review, dict) or not review:
            return "not_checked"
        status = review.get("status", "not_checked")
        if status == "not_checked" or review.get("basis_digest") != compute_basis_digest(self):
            return "not_checked"
        if (
            status == "supported"
            and sp_validator is not None
            and review.get("claim_kind") == "intention"
        ):
            unit_ids, sp_refs = split_corroboration(review.get("corroborated_by"))
            valid_sp = []
            for ref in sp_refs:
                try:
                    if sp_validator(ref):
                        valid_sp.append(ref)
                except Exception:
                    continue  # 驗證失敗視為失效
            if not unit_ids and not valid_sp:
                return "insufficient"
        return str(status)

    def sharing_state(self) -> SharingState:
        """Concept.sharing 的結構化視圖（壞值退回 private）。"""
        return SharingState.from_dict(self.sharing)

    def effective_sharing_level(self, scope_id: str) -> str:
        """概念在 scope_id 場域目前有效的可見度（唯讀）。

        只有「active、該場域狀態為 scope_shared、且判定綁定的 basis_digest 與現在的證據
        基礎相符」才回傳 scope_shared；其餘（含 digest 變動、deprecated、缺項）一律 private。
        """
        if self.status != "active":
            return LEVEL_PRIVATE
        entry = self.sharing_state().get(scope_id)
        if entry is None or entry.level != LEVEL_SCOPE_SHARED:
            return LEVEL_PRIVATE
        if not entry.basis_digest or entry.basis_digest != compute_basis_digest(self):
            return LEVEL_PRIVATE
        return LEVEL_SCOPE_SHARED

    def record_mutation(self) -> None:
        """記錄一次概念變更（expand/modify/deprecate），更新 health 相關指標。"""
        self.mutation_count += 1
        self.version += 1
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
            "version": self.version,
            "derivations": [dict(d) for d in self.derivations],
            "evidence_review": copy.deepcopy(self.evidence_review),
            "sharing": copy_sharing(self.sharing),
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
        if not isinstance(data.get("sharing"), dict):
            data.pop("sharing", None)  # 壞值／缺值 -> 預設 {}（private）
        return cls(**data)
