"""
Hypothesis Schema - 待驗證假說（M5-D）

由「跨週期被確認的 LATENT_BRIDGE」結構性地提案，與 LATENT_BRIDGE 語意分離：
  - LATENT_BRIDGE 是探索式關聯（embedding 相似），不是事實、不影響 confidence。
  - Hypothesis 是「這兩個概念可能有關」的待驗證主張；只有經審核者（LLM）依證據原文判為
    supported，且有獨立佐證，才由 Gate 昇格為 RELATES_TO。

狀態機：
  proposed -> under_review（帶租約，過期回 proposed）-> supported / refuted
  insufficient 回 proposed 並 attempts+1，達上限 -> expired(insufficient_exhausted)
  LLM 失敗維持 proposed 並退避（不寫假審核）
  supported 後 pair_digest 漂移 -> 回 proposed 重審；重審非 supported 則撤回昇格邊
  端點 deprecated／archived／merged -> expired(endpoint_gone)
  proposed 逾 TTL -> expired(ttl)

proposed 不存任何斷言文字：statement 預設 None，僅審核者在 supported 時填。
引擎只依結構欄位判斷，不從文字內容推測（不使用 keyword / regex）。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from echo_memory.schemas.evidence_review import CLAIM_KINDS, EvidenceReview

HYPOTHESIS_STATUSES = ("proposed", "under_review", "supported", "refuted", "expired")
OPEN_STATUSES = ("proposed", "under_review")
# 已結案且會被保留一段時間後清除的狀態
RETAINED_STATUSES = ("refuted", "expired")
EXPIRE_REASONS = ("ttl", "endpoint_gone", "insufficient_exhausted")
ORIGIN_TYPES = ("latent_bridge",)


def make_pair_key(concept_a: str, concept_b: str) -> str:
    """排序後的 concept id 對（a|b）；與 LATENT_BRIDGE 的 bridge_id 同形。"""
    a, b = sorted((str(concept_a), str(concept_b)))
    return f"{a}|{b}"


def split_pair_key(pair_key: str) -> tuple[str, str] | None:
    parts = str(pair_key).split("|")
    if len(parts) != 2 or not parts[0] or not parts[1] or parts[0] == parts[1]:
        return None
    return parts[0], parts[1]


def _parse_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str) and value:
        try:
            dt = datetime.fromisoformat(value)
        except ValueError:
            return None
    else:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _int(value: Any, default: int = 0) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def _float(value: Any, default: float = 0.0) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    return default if v != v else v


@dataclass
class HypothesisOrigin:
    """假說來源：哪條 bridge、哪個 Dream 週期、當時確認次數與相似度。"""

    type: str = "latent_bridge"
    bridge_id: str = ""
    cycle_id: str = ""
    confirm_count: int = 0
    similarity: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "bridge_id": self.bridge_id,
            "cycle_id": self.cycle_id,
            "confirm_count": int(self.confirm_count),
            "similarity": round(float(self.similarity), 4),
        }

    @classmethod
    def from_dict(cls, data: Any) -> HypothesisOrigin:
        if not isinstance(data, dict):
            return cls()
        otype = data.get("type")
        return cls(
            type=otype if otype in ORIGIN_TYPES else "latent_bridge",
            bridge_id=str(data.get("bridge_id", "") or ""),
            cycle_id=str(data.get("cycle_id", "") or ""),
            confirm_count=_int(data.get("confirm_count")),
            similarity=_float(data.get("similarity")),
        )


@dataclass
class HypothesisOutcome:
    """結案資訊：昇格的邊、昇格 patch、過期原因。"""

    promoted_edge: dict[str, Any] | None = None  # {"a","b","hypothesis_id"}；有值代表圖譜上有昇格邊
    promote_patch_id: str = ""
    expire_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "promoted_edge": dict(self.promoted_edge) if self.promoted_edge else None,
            "promote_patch_id": self.promote_patch_id,
            "expire_reason": self.expire_reason,
        }

    @classmethod
    def from_dict(cls, data: Any) -> HypothesisOutcome:
        if not isinstance(data, dict):
            return cls()
        edge = data.get("promoted_edge")
        reason = str(data.get("expire_reason", "") or "")
        return cls(
            promoted_edge=dict(edge) if isinstance(edge, dict) and edge else None,
            promote_patch_id=str(data.get("promote_patch_id", "") or ""),
            expire_reason=reason,
        )


@dataclass
class Hypothesis:
    """一筆待驗證假說。

    Attributes:
        id: hyp_xxx
        version: 每次寫入 +1（樂觀並行檢查用）
        pair_key: 排序後的 concept id 對（唯一）
        origin: 來源 bridge 資訊
        pair_digest: 提案／審核當下 compute_pair_digest（兩端證據基礎）；漂移即失效
        statement: 審核者在 supported 時填的關聯描述；其餘狀態一律 None
        claim_kind: 審核者判斷的主張類型
        review: 重用 EvidenceReview 結構（status／cited_evidence_ids／support_strength…）
        status: proposed／under_review／supported／refuted／expired
        attempts: insufficient 次數
        fail_count: LLM 失敗次數（只影響退避，不算 attempts）
        next_review_at: 退避到期前不重審
        claimed_until / claim_token: 審核租約
        outcome: 昇格邊／過期原因
        expires_at: proposed 的 TTL；refuted／expired 的保留期限（逾期清除）；supported 無
    """

    pair_key: str
    id: str = ""
    version: int = 1
    origin: HypothesisOrigin = field(default_factory=HypothesisOrigin)
    pair_digest: str = ""
    statement: str | None = None
    claim_kind: str = "fact"
    review: EvidenceReview = field(default_factory=EvidenceReview)
    status: str = "proposed"
    attempts: int = 0
    fail_count: int = 0
    next_review_at: datetime | None = None
    claimed_until: datetime | None = None
    claim_token: str = ""
    outcome: HypothesisOutcome = field(default_factory=HypothesisOutcome)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.id:
            self.id = f"hyp_{uuid.uuid4().hex[:12]}"

    @property
    def endpoints(self) -> tuple[str, str] | None:
        return split_pair_key(self.pair_key)

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_STATUSES

    def lease_expired(self, now: datetime) -> bool:
        return self.claimed_until is None or self.claimed_until <= now

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "version": int(self.version),
            "pair_key": self.pair_key,
            "origin": self.origin.to_dict(),
            "pair_digest": self.pair_digest,
            "statement": self.statement,
            "claim_kind": self.claim_kind,
            "review": self.review.to_dict(),
            "status": self.status,
            "attempts": int(self.attempts),
            "fail_count": int(self.fail_count),
            "next_review_at": _iso(self.next_review_at),
            "claimed_until": _iso(self.claimed_until),
            "claim_token": self.claim_token,
            "outcome": self.outcome.to_dict(),
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
            "expires_at": _iso(self.expires_at),
        }

    @classmethod
    def from_dict(cls, data: Any) -> Hypothesis | None:
        """容錯解析：未知欄位忽略、壞值退回安全預設；沒有可用 pair_key 回傳 None。

        狀態壞值一律視為 expired（不是開放、也不是 supported），避免壞資料被當成有效主張。
        """
        if not isinstance(data, dict):
            return None
        pair_key = str(data.get("pair_key", "") or "")
        if split_pair_key(pair_key) is None:
            return None
        status = data.get("status", "proposed")
        outcome = HypothesisOutcome.from_dict(data.get("outcome"))
        if status not in HYPOTHESIS_STATUSES:
            status = "expired"
            outcome.expire_reason = outcome.expire_reason or "corrupt"
        claim_kind = data.get("claim_kind", "fact")
        if claim_kind not in CLAIM_KINDS:
            claim_kind = "fact"
        statement = data.get("statement")
        now = datetime.now(timezone.utc)
        return cls(
            pair_key=pair_key,
            id=str(data.get("id", "") or ""),
            version=max(1, _int(data.get("version"), 1)),
            origin=HypothesisOrigin.from_dict(data.get("origin")),
            pair_digest=str(data.get("pair_digest", "") or ""),
            statement=str(statement) if isinstance(statement, str) and statement else None,
            claim_kind=claim_kind,
            review=EvidenceReview.from_dict(data.get("review")),
            status=status,
            attempts=_int(data.get("attempts")),
            fail_count=_int(data.get("fail_count")),
            next_review_at=_parse_dt(data.get("next_review_at")),
            claimed_until=_parse_dt(data.get("claimed_until")),
            claim_token=str(data.get("claim_token", "") or ""),
            outcome=outcome,
            created_at=_parse_dt(data.get("created_at")) or now,
            updated_at=_parse_dt(data.get("updated_at")) or now,
            expires_at=_parse_dt(data.get("expires_at")),
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 證據資格與獨立佐證（審核器與 Gate 共用，只看結構欄位）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

KIND_USER = "user"
KIND_OBSERVATION = "observation"


def hypothesis_evidence_kind(unit: Any) -> str | None:
    """unit 在假說審核中的證據資格：

    - "user"：使用者確實說過的話（can_attest_user_claim）；只是「主觀事實」。
    - "observation"：平台觀察（M-8，可觀測操作紀錄），known 且未塗銷。
    - None：agent／dream／reflection／tool／system、未確認來源、已塗銷、共享衍生一律不算證據。
    embedding 相似度不是證據，不在此處流動。
    """
    from echo_memory.schemas.evidence import can_attest_user_claim

    if getattr(unit, "tombstoned_at", None) is not None:
        return None
    if can_attest_user_claim(unit):
        return KIND_USER
    if unit.source_type == "observation" and unit.provenance_status == "known":
        return KIND_OBSERVATION
    return None


@dataclass
class HypothesisSupport:
    """被引用證據的支持評估。"""

    strength: float = 0.0
    independent: bool = False  # 有使用者本人以外的獨立佐證
    independent_ids: list[str] = field(default_factory=list)
    status: str = "insufficient"  # supported｜insufficient
    notes: list[str] = field(default_factory=list)


def evaluate_hypothesis_support(
    units: list[Any], claim_kind: str = "fact", cfg: Any = None,
) -> HypothesisSupport:
    """被引用 unit 的支持度與獨立佐證判斷（沿用 M4 規則，不另造一套）。

    - support_strength：僅 user unit，以 learning_weight 做 noisy-OR（同 episode 取最大 w）。
    - 獨立佐證（客觀事實的升格條件）：使用者本人的陳述只是主觀事實。需要
      (a) 至少一筆 known 的 observation（可觀測操作紀錄），或
      (b) 至少兩位不同且皆可辨識（speaker_id 非 None）的使用者說話者。
      只有同一使用者自述（含無法辨識是否同一人）-> 最高 insufficient。
    - 最終狀態：decide_review_status（門檻 SUPPORT_THRESHOLD、intention 需佐證）且 independent。
    """
    from echo_memory.schemas.evidence import learning_weight
    from echo_memory.schemas.evidence_review import decide_review_status, noisy_or

    if cfg is None:
        from echo_memory.config import get_config
        cfg = get_config()
    out = HypothesisSupport()
    by_group: dict[str, float] = {}
    obs_ids: list[str] = []
    user_units: list[Any] = []
    for u in units:
        kind = hypothesis_evidence_kind(u)
        if kind == KIND_USER:
            user_units.append(u)
            group = u.episode_id or u.id
            by_group[group] = max(by_group.get(group, 0.0), learning_weight(u, cfg))
        elif kind == KIND_OBSERVATION:
            obs_ids.append(u.id)
    out.strength = noisy_or(by_group.values())

    speakers = {u.speaker_id for u in user_units}
    multi_speaker = bool(user_units) and None not in speakers and len(speakers) >= 2
    out.independent = bool(obs_ids) or multi_speaker
    out.independent_ids = list(obs_ids) + ([u.id for u in user_units] if multi_speaker else [])
    if not user_units and not obs_ids:
        out.notes.append("no_eligible_citation")
        return out
    status = decide_review_status("supported", out.strength, claim_kind, out.independent_ids, cfg)
    if status != "supported":
        out.notes.append(
            "intention_needs_corroboration" if claim_kind == "intention"
            else "below_support_threshold"
        )
    if not out.independent:
        status = "insufficient"
        out.notes.append("subjective_only_no_independent_corroboration")
    out.status = status
    return out
