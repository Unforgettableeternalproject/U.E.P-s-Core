"""
EvidenceReview Schema - 概念證據審核結果

審核結果綁定 basis_digest（內容摘要），而非 Concept.version：
record_mutation() 會讓 mark_conflict、review 自己、rollback 都 bump version，
綁 version 會使審核自我失效。digest 只涵蓋「證據基礎」（description／facts／
衍生來源／source_episode_ids），不含 conflict_count、confidence、metadata 其他欄位。

引擎只依結構欄位判斷，不從文字內容推測（不使用 keyword / regex）。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

REVIEW_STATUSES = ("not_checked", "supported", "contradicted", "insufficient")
# 審核 patch 可寫入的狀態（not_checked 代表「沒審核」，不能作為審核結果寫入）
REVIEWED_STATUSES = ("supported", "contradicted", "insufficient")
REVIEWERS = ("llm", "none")
# 主張類型（審核 LLM 判斷）；intention＝意圖／計畫／承諾，使用者自述最高 insufficient
CLAIM_KINDS = ("fact", "preference", "state", "intention")


def _clamp01(value: Any) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0.0
    if v != v:  # NaN
        return 0.0
    return max(0.0, min(1.0, v))


def _parse_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


@dataclass
class EvidenceReview:
    """一次證據審核的結果（存於 Concept.evidence_review）。

    Attributes:
        status: not_checked / supported / contradicted / insufficient
        support_strength: 審核者在已引用證據下的支持強度 [0, 1]
        cited_evidence_ids: 審核者引用的 evidence unit id（必須是送審當下提供者）
        scope: {"subject_ref": str|None, "context": str|None}，審核者填的自由標籤，
            只能餵回 LLM，不可當 key 做字串比對
        independent_count / user_attested_count / legacy_unknown_count:
            審核當下的獨立性快取（來自 provenance.independent_support）
        basis_digest: 審核時 compute_basis_digest(concept) 的結果
        reviewed_at_version: 審核時 Concept.version（僅供 audit／追溯，不作失效依據）
        reviewed_at: 審核時間
        reviewer: "llm" | "none"
        reason: 審核理由
        patch_id: 寫入這筆審核的 review patch id（rollback／追溯用）
        claim_kind: fact / preference / state / intention（審核 LLM 判斷）
        corroborated_by: 交叉佐證的 unit id（intention 要 supported 必須非空）
    """

    status: str = "not_checked"
    support_strength: float = 0.0
    cited_evidence_ids: list[str] = field(default_factory=list)
    scope: dict[str, Any] = field(default_factory=dict)
    independent_count: int = 0
    user_attested_count: int = 0
    legacy_unknown_count: int = 0
    basis_digest: str = ""
    reviewed_at_version: int = 0
    reviewed_at: datetime | None = None
    reviewer: str = ""
    reason: str = ""
    patch_id: str = ""
    # 主張類型與交叉佐證（§8.3）
    claim_kind: str = "fact"
    corroborated_by: list[str] = field(default_factory=list)  # 佐證 unit id

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "support_strength": round(float(self.support_strength), 4),
            "cited_evidence_ids": list(self.cited_evidence_ids),
            "scope": dict(self.scope),
            "independent_count": int(self.independent_count),
            "user_attested_count": int(self.user_attested_count),
            "legacy_unknown_count": int(self.legacy_unknown_count),
            "basis_digest": self.basis_digest,
            "reviewed_at_version": int(self.reviewed_at_version),
            "reviewed_at": self.reviewed_at.isoformat() if self.reviewed_at else None,
            "reviewer": self.reviewer,
            "reason": self.reason,
            "patch_id": self.patch_id,
            "claim_kind": self.claim_kind,
            "corroborated_by": list(self.corroborated_by),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> EvidenceReview:
        """容錯解析：空 dict／None／未知欄位／壞值一律退回安全預設（not_checked）。"""
        if not isinstance(data, dict) or not data:
            return cls()
        status = data.get("status", "not_checked")
        if status not in REVIEW_STATUSES:
            status = "not_checked"
        scope = data.get("scope")
        cited = data.get("cited_evidence_ids")
        claim_kind = data.get("claim_kind", "fact")
        if claim_kind not in CLAIM_KINDS:
            claim_kind = "fact"
        corroborated = data.get("corroborated_by")

        def _int(key: str) -> int:
            try:
                return max(0, int(data.get(key, 0) or 0))
            except (TypeError, ValueError):
                return 0

        return cls(
            status=status,
            support_strength=_clamp01(data.get("support_strength", 0.0)),
            cited_evidence_ids=[str(x) for x in cited] if isinstance(cited, list) else [],
            scope=dict(scope) if isinstance(scope, dict) else {},
            independent_count=_int("independent_count"),
            user_attested_count=_int("user_attested_count"),
            legacy_unknown_count=_int("legacy_unknown_count"),
            basis_digest=str(data.get("basis_digest", "") or ""),
            reviewed_at_version=_int("reviewed_at_version"),
            reviewed_at=_parse_dt(data.get("reviewed_at")),
            reviewer=str(data.get("reviewer", "") or ""),
            reason=str(data.get("reason", "") or ""),
            patch_id=str(data.get("patch_id", "") or ""),
            claim_kind=claim_kind,
            corroborated_by=(
                [str(x) for x in corroborated] if isinstance(corroborated, list) else []
            ),
        )


def _stable(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str)


def compute_basis_digest(concept: Any) -> str:
    """概念「證據基礎」的 sha256 摘要。

    = sha256(description｜排序後 facts｜排序後 derivations 的 (kind,id,parent_version)｜
    排序後 source_episode_ids)。
    不含 conflict_count、confidence、status、version、metadata 其他欄位：
    mark_conflict、review、support_source 升級都不使審核失效；
    description／facts／來源變動才失效。
    """
    facts = sorted(_stable(f) for f in (concept.metadata.get("facts") or []))
    derivs = sorted(
        _stable([d.get("kind"), d.get("id"), d.get("parent_version")])
        for d in concept.derivations
        if isinstance(d, dict)
    )
    sources = sorted(str(x) for x in concept.source_episode_ids)
    payload = _stable([concept.description, facts, derivs, sources])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def review_confidence_cap(status: str, cfg: Any = None) -> float | None:
    """已審核狀態對應的 confidence 上限；非已審核狀態回傳 None。"""
    if status not in REVIEWED_STATUSES:
        return None
    if cfg is None:
        from echo_memory.config import get_config
        cfg = get_config()
    return float({
        "supported": cfg.review_confidence_cap_supported,
        "insufficient": cfg.review_confidence_cap_insufficient,
        "contradicted": cfg.review_confidence_cap_contradicted,
    }[status])


def compute_review_confidence(
    status: str,
    support_strength: float,
    independent_count: int,
    support_source: str | None,
    cfg: Any = None,
) -> float | None:
    """§3.4 以獨立來源重算 Concept.confidence。status 不是已審核狀態時回傳 None。

    base(n) = 1 - 1/(1+n)，n = max(independent_count, 1)（legacy 不進 n）
    confidence = min(cap[status], base * source_f[support_source])
                 * (0.5 + 0.5 * support_strength)
    support_source 未知／缺值視為 user（沿用既有概念的預設，不額外降權）。
    """
    cap = review_confidence_cap(status, cfg)
    if cap is None:
        return None
    if cfg is None:
        from echo_memory.config import get_config
        cfg = get_config()
    source_f = {
        "user": cfg.review_source_factor_user,
        "legacy_unknown": cfg.review_source_factor_legacy,
        "agent_only": cfg.review_source_factor_agent,
    }.get(support_source or "user", cfg.review_source_factor_user)
    n = max(int(independent_count), 1)
    base = 1.0 - 1.0 / (1.0 + n)
    value = min(cap, base * source_f) * (0.5 + 0.5 * _clamp01(support_strength))
    return max(0.0, min(1.0, value))


def noisy_or(weights: Any) -> float:
    """S = 1 - prod(1 - w_i)，每個 w 先截到 [0, 1]。空輸入回傳 0.0。"""
    prod = 1.0
    for w in weights:
        prod *= 1.0 - _clamp01(w)
    return max(0.0, min(1.0, 1.0 - prod))


def compute_support_strength(units: Any, cfg: Any = None) -> float:
    """獨立使用者陳述以 noisy-OR 合成 support_strength（§8.2）。

    輸入應是 independent_support 去重後的原始 Evidence unit；這裡再以 unit.id 去重一次，
    並只取 source_type="user" 的 unit（agent／tool／observation 不計入使用者陳述）。
    """
    from echo_memory.schemas.evidence import learning_weight

    seen: set[str] = set()
    weights: list[float] = []
    for unit in units:
        if unit.id in seen:
            continue
        seen.add(unit.id)
        if unit.source_type == "user":
            weights.append(learning_weight(unit, cfg))
    return noisy_or(weights)


def decide_review_status(
    llm_status: str,
    support_strength: float,
    claim_kind: str = "fact",
    corroborated_by: Any = (),
    cfg: Any = None,
) -> str:
    """由「LLM 判定」＋門檻決定最終 review status（§8.2、§8.3）。

    - LLM 判 supported 但 support_strength < SUPPORT_THRESHOLD -> insufficient
    - claim_kind=intention 且無 corroborated_by -> 最高 insufficient；
      有交叉佐證時佐證本身即證據，不再套 support_strength 門檻
    - contradicted／insufficient 照 LLM 判定；非法值視為 insufficient
    """
    if llm_status not in REVIEWED_STATUSES:
        return "insufficient"
    if llm_status != "supported":
        return llm_status
    if claim_kind == "intention":
        return "supported" if list(corroborated_by) else "insufficient"
    if cfg is None:
        from echo_memory.config import get_config
        cfg = get_config()
    return "supported" if _clamp01(support_strength) >= float(cfg.support_threshold) else "insufficient"
