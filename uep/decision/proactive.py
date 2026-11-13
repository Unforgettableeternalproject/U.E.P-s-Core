"""``proactive.initiate`` 決策點（DL-6a）：ProactiveCandidate 契約、9 關規則仲裁與 shadow 日誌。

設計依據：``hidden_docs/架構重構/Proactive決策契約草案.md``。

- 候選（``ProactiveCandidate``）與仲裁當下的即時事實（``ProactiveContext``）分開：候選只有 ID／列舉／數值，
  不帶原文、不帶當下 Audience／Presence；授權只帶 ``authorization_ref``，查證結果由 context 注入。
- 仲裁是無狀態純函式（``arbitrate``）：冷卻／預算 ledger、時間、授權查證結果全由呼叫端注入，不呼叫 clock，重播可重現。
  硬性條件（停止、授權、Audience、Presence）先於任何評分；高分不得抵銷。
- ``proactive.initiate`` 為 async、shadow-only、不開 native；結果只用 ``loop``／``placement``／``followups`` 三個區段。
  Phase 1 沒有任何消費者會依 ``ready`` 啟動 Run 或投遞提醒（由 ``test_decision_proactive`` 的 AST 鎖鎖住）。
- ``to_loop_trigger`` 是純函式，Phase 1 只用於記錄 would_start，不呼叫 AgentLoop；本模組對 ``uep.agent`` 只 import ``uep.agent.trigger``。
  goal_code -> capability_tags 的標籤表歸工具層（S-H），這裡只透過 ``ProactiveInjections.capability_lookup`` 查詢。
- 已知限制：``Phase 2`` 開放投遞前，冷卻 ledger 必須持久化，否則重啟會重複提醒。
"""
from __future__ import annotations

import logging
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from uep.agent.trigger import TRIGGER_KINDS, LoopTrigger
from uep.context.snapshot import scope_admits
from uep.contracts.presence import PresenceLocation, PresenceStatus
from uep.decision.contracts import (
    DecisionEngineInfo,
    DecisionFollowup,
    DecisionLoop,
    DecisionPlacement,
    DecisionPlan,
    DecisionRequest,
    DecisionResult,
)
from uep.decision.hub import DecisionHub
from uep.decision.presence_absent_intent import TARGET_LOCATIONS
from uep.decision.registry import DecisionPointSpec
from uep.events.envelope import scope_rank
from uep.ids import new_id

logger = logging.getLogger("uep.decision.proactive")

KIND_PROACTIVE_INITIATE = "proactive.initiate"
OUTCOME_READY = "ready"
OUTCOME_DEFERRED = "deferred"
OUTCOME_REJECTED = "rejected"
OUTCOMES = (OUTCOME_READY, OUTCOME_DEFERRED, OUTCOME_REJECTED)

_ENGINE_ID = "proactive_rules"
_ENGINE_VERSION = "dl6a"
_DEFAULT_BUDGET_MS = 1000
DEFAULT_POLICY_REF = "proactive_default"  # 尚無 Policy 服務時的固定預設（Agent 提案 D1 註記 4）

# ---- goal_code 閉集（Q-B：goal_code 歸決策層；goal_code -> capability_tags 標籤表歸工具層，S-H 才做）----
GOAL_REMIND_DUE = "remind_due"
GOAL_REPORT_TASK_DONE = "report_task_done"
GOAL_REQUEST_PRESENCE_MOVE = "request_presence_move"
GOAL_ASSIGNMENT_PICKUP = "assignment_pickup"
GOAL_MEMORY_FOLLOWUP = "memory_followup"
GOAL_JOIN_VOICE = "join_voice"  # 預留（D84），Phase 1 不實作
GOAL_CODES: frozenset[str] = frozenset({
    GOAL_REMIND_DUE, GOAL_REPORT_TASK_DONE, GOAL_REQUEST_PRESENCE_MOVE,
    GOAL_ASSIGNMENT_PICKUP, GOAL_MEMORY_FOLLOWUP, GOAL_JOIN_VOICE,
})
RESERVED_GOAL_CODES: frozenset[str] = frozenset({GOAL_JOIN_VOICE})
RUN_GOAL_CODES: frozenset[str] = frozenset({GOAL_ASSIGNMENT_PICKUP, GOAL_MEMORY_FOLLOWUP})  # 會轉成 LoopTrigger 的任務類

# ---- 關卡名（寫進日誌 sub_kind）----
GATE_VALIDITY = "validity"
GATE_STOP = "stop"
GATE_AUTHORIZATION = "authorization"
GATE_AUDIENCE = "audience"
GATE_PRESENCE = "presence"
GATE_COST = "cost"
GATE_COOLDOWN = "cooldown"
GATE_BUDGET = "budget"
GATE_CONFIDENCE = "confidence"
GATE_READY = "ready"
GATES: tuple[str, ...] = (
    GATE_VALIDITY, GATE_STOP, GATE_AUTHORIZATION, GATE_AUDIENCE, GATE_PRESENCE, GATE_COST,
    GATE_COOLDOWN, GATE_BUDGET, GATE_CONFIDENCE, GATE_READY,
)

# ---- reason_code（閉集，28 個；隨 spec 註冊）----
R_CANDIDATE_EXPIRED = "candidate_expired"
R_CANDIDATE_MALFORMED = "candidate_malformed"
R_GLOBAL_STOP = "global_stop"
R_AUTHORIZATION_REVOKED = "authorization_revoked"
R_NO_AUTHORIZATION = "no_authorization"
R_AUTHORIZATION_SCOPE_INSUFFICIENT = "authorization_scope_insufficient"
R_AUDIENCE_TOO_WIDE_MOVE = "audience_too_wide_move"
R_AUDIENCE_TOO_WIDE_WAIT = "audience_too_wide_wait"
R_PRESENCE_TRANSFERRING = "presence_transferring"
R_TARGET_NOT_MOVABLE = "target_not_movable"
R_PRESENCE_MISMATCH_MOVE = "presence_mismatch_move"
R_PRESENCE_NONE_QUEUED = "presence_none_queued"
R_PRESENCE_NONE_PREAUTHORIZED = "presence_none_preauthorized"
R_ACTIVITY_NOT_INTERRUPTIBLE = "activity_not_interruptible"
R_MUTED = "muted"
R_IN_COMMITTED_INTERACTION = "in_committed_interaction"
R_COST_EXCEEDS_VALUE = "cost_exceeds_value"
R_COOLDOWN_ACTIVE = "cooldown_active"
R_DELIVERY_UNKNOWN = "delivery_unknown"
R_DUPLICATE_DELIVERED = "duplicate_delivered"
R_BUDGET_EXHAUSTED = "budget_exhausted"
R_LOW_CONFIDENCE = "low_confidence"
R_READY_PRESENT = "ready_present"
R_READY_MOVE = "ready_move"
R_READY_ASSIGNMENT = "ready_assignment"
ON_TIMEOUT_REASON = "proactive_timeout"
ON_BUSY_REASON = "proactive_busy"
ON_ERROR_REASON = "proactive_error"
ON_INVALID_REASON = "proactive_invalid"

FALLBACK_REASONS: tuple[str, ...] = (ON_TIMEOUT_REASON, ON_BUSY_REASON, ON_ERROR_REASON, ON_INVALID_REASON)
REASON_CODES: tuple[str, ...] = (
    R_CANDIDATE_EXPIRED, R_CANDIDATE_MALFORMED, R_GLOBAL_STOP, R_AUTHORIZATION_REVOKED, R_NO_AUTHORIZATION,
    R_AUTHORIZATION_SCOPE_INSUFFICIENT, R_AUDIENCE_TOO_WIDE_MOVE, R_AUDIENCE_TOO_WIDE_WAIT,
    R_PRESENCE_TRANSFERRING, R_TARGET_NOT_MOVABLE, R_PRESENCE_MISMATCH_MOVE, R_PRESENCE_NONE_QUEUED, R_PRESENCE_NONE_PREAUTHORIZED,
    R_ACTIVITY_NOT_INTERRUPTIBLE, R_MUTED, R_IN_COMMITTED_INTERACTION, R_COST_EXCEEDS_VALUE,
    R_COOLDOWN_ACTIVE, R_DELIVERY_UNKNOWN, R_DUPLICATE_DELIVERED, R_BUDGET_EXHAUSTED, R_LOW_CONFIDENCE,
    R_READY_PRESENT, R_READY_MOVE, R_READY_ASSIGNMENT, *FALLBACK_REASONS,
)

# to_loop_trigger 不啟動的原因（只用於日誌記錄 would_start，不屬於 DecisionResult.reason_code）
LT_WOULD_START = "would_start"
LT_NOT_READY = "not_ready"
LT_NO_LOOP = "no_loop"
LT_BAD_TRIGGER = "bad_trigger"
LT_CAPABILITY_EMPTY = "capability_empty"

_SAFE_REF = re.compile(r"[A-Za-z0-9_:.\-]{1,128}")  # 比照 uep.events.envelope._SAFE_REF_PATTERN：防止夾帶私人文字
_REASON_TOKEN = re.compile(r"[a-z][a-z0-9_]{0,63}")


class Urgency(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    CRITICAL = "critical"


class SourceKind(StrEnum):
    SCHEDULE = "schedule"
    TASK = "task"
    MEMORY = "memory"
    PLATFORM = "platform"
    ASSIGNMENT = "assignment"


_URGENCY_WEIGHT = {Urgency.LOW: 0.1, Urgency.NORMAL: 0.3, Urgency.HIGH: 0.6, Urgency.CRITICAL: 1.0}
URGENCY_SOURCE_EVENT = "event"
URGENCY_SOURCE_POLICY = "policy"


# ---- 驗證小工具 ----

def _ref(name: str, value: Any) -> str:
    if not isinstance(value, str) or not _SAFE_REF.fullmatch(value):
        raise ValueError(f"{name} 不符合安全字元集")
    return value


def _unit(name: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0.0 <= float(value) <= 1.0:
        raise ValueError(f"{name} 必須是 0..1 的數值")
    return float(value)


def _aware(name: str, value: Any) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{name} 必須是 aware datetime")
    return value


def _refs(name: str, value: Any) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, (tuple, list)):
        raise ValueError(f"{name} 必須是 ID 序列")  # noqa: TRY004 - 呼叫端統一以 ValueError 處理
    return tuple(_ref(name, v) for v in value)


def _enum(cls: type[StrEnum], name: str, value: Any) -> Any:
    try:
        return cls(value)
    except ValueError:
        raise ValueError(f"{name} 不是合法值：{value!r}") from None


def _nonneg_int(name: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} 必須是非負整數")
    return value


def _set(obj: Any, **values: Any) -> None:
    for key, value in values.items():
        object.__setattr__(obj, key, value)


# ---- 候選 ----

@dataclass(frozen=True)
class ProactiveCandidate:
    """一個主動行為候選（提案10-25 §9.2）。建構時驗證，失敗拋 ``ValueError``（由評估階段處理，不進仲裁）。

    ``privacy_scope``（內容的 scope）與當下 Audience 是兩回事，不得混用。``urgency=critical`` 只能來自可信 Policy
    （Q-H）：必須同時標 ``urgency_source="policy"``。
    """

    candidate_id: str
    trigger_event_ids: tuple[str, ...]
    rule_version: str
    goal_ref: str  # goal_code 閉集
    evidence_refs: tuple[str, ...]
    memory_salience: float
    urgency: Urgency
    confidence: float
    privacy_scope: str
    expires_at: datetime
    dedupe_key: str
    source_kind: SourceKind
    owner_actor_id: str
    created_at: datetime
    interruptibility_cost: float | None = None  # 僅供參考；仲裁以 context 的即時活動事實重算
    desired_location: PresenceLocation | None = None
    decision_reason: str | None = None  # REEVALUATED 時攜帶上一次決策的 reason_code
    authorization_ref: str | None = None  # 只帶 grant id；授權內容由 context 側查證
    urgency_source: str = URGENCY_SOURCE_EVENT

    def __post_init__(self) -> None:
        _set(
            self,
            candidate_id=_ref("candidate_id", self.candidate_id),
            trigger_event_ids=_refs("trigger_event_ids", self.trigger_event_ids),
            rule_version=_ref("rule_version", self.rule_version),
            evidence_refs=_refs("evidence_refs", self.evidence_refs),
            memory_salience=_unit("memory_salience", self.memory_salience),
            urgency=_enum(Urgency, "urgency", self.urgency),
            confidence=_unit("confidence", self.confidence),
            expires_at=_aware("expires_at", self.expires_at),
            created_at=_aware("created_at", self.created_at),
            dedupe_key=_ref("dedupe_key", self.dedupe_key),
            source_kind=_enum(SourceKind, "source_kind", self.source_kind),
            owner_actor_id=_ref("owner_actor_id", self.owner_actor_id),
        )
        if not self.trigger_event_ids:
            raise ValueError("trigger_event_ids 至少需要一個")
        if self.goal_ref not in GOAL_CODES:
            raise ValueError(f"goal_ref 不在 goal_code 閉集：{self.goal_ref!r}")
        if self.goal_ref in RESERVED_GOAL_CODES:
            raise ValueError(f"goal_ref 為預留值，Phase 1 不處理：{self.goal_ref!r}")
        scope_rank(self.privacy_scope)  # 格式不合法拋 ValueError
        if self.interruptibility_cost is not None:
            _set(self, interruptibility_cost=_unit("interruptibility_cost", self.interruptibility_cost))
        if self.desired_location is not None:
            _set(self, desired_location=_enum(PresenceLocation, "desired_location", self.desired_location))
        if self.decision_reason is not None and not (
            isinstance(self.decision_reason, str) and _REASON_TOKEN.fullmatch(self.decision_reason)
        ):
            raise ValueError("decision_reason 不符合 reason_code 格式")
        if self.authorization_ref is not None:
            _set(self, authorization_ref=_ref("authorization_ref", self.authorization_ref))
        if self.urgency_source not in (URGENCY_SOURCE_EVENT, URGENCY_SOURCE_POLICY):
            raise ValueError("urgency_source 只能是 event／policy")
        if self.urgency is Urgency.CRITICAL and self.urgency_source != URGENCY_SOURCE_POLICY:
            raise ValueError("urgency=critical 必須來自可信 Policy（urgency_source=policy）")


def candidate_to_facts(candidate: ProactiveCandidate) -> dict[str, Any]:
    """候選 -> ``facts["candidate"]``：全是 ID／列舉／數值，datetime 轉 ISO。"""
    return {
        "candidate_id": candidate.candidate_id, "trigger_event_ids": list(candidate.trigger_event_ids),
        "rule_version": candidate.rule_version, "goal_ref": candidate.goal_ref,
        "evidence_refs": list(candidate.evidence_refs), "memory_salience": candidate.memory_salience,
        "urgency": candidate.urgency.value, "confidence": candidate.confidence,
        "privacy_scope": candidate.privacy_scope, "expires_at": candidate.expires_at.isoformat(),
        "dedupe_key": candidate.dedupe_key, "source_kind": candidate.source_kind.value,
        "owner_actor_id": candidate.owner_actor_id, "created_at": candidate.created_at.isoformat(),
        "interruptibility_cost": candidate.interruptibility_cost,
        "desired_location": candidate.desired_location.value if candidate.desired_location else None,
        "decision_reason": candidate.decision_reason, "authorization_ref": candidate.authorization_ref,
        "urgency_source": candidate.urgency_source,
    }


def candidate_from_facts(data: Mapping[str, Any]) -> ProactiveCandidate:
    """``facts["candidate"]`` -> 候選；缺鍵或驗證失敗拋 ``ValueError``。"""
    try:
        return ProactiveCandidate(
            candidate_id=data["candidate_id"], trigger_event_ids=_tuple(data["trigger_event_ids"]),
            rule_version=data["rule_version"], goal_ref=data["goal_ref"],
            evidence_refs=_tuple(data["evidence_refs"]), memory_salience=data["memory_salience"],
            urgency=data["urgency"], confidence=data["confidence"], privacy_scope=data["privacy_scope"],
            expires_at=_parse_dt(data["expires_at"]), dedupe_key=data["dedupe_key"],
            source_kind=data["source_kind"], owner_actor_id=data["owner_actor_id"],
            created_at=_parse_dt(data["created_at"]),
            interruptibility_cost=data.get("interruptibility_cost"), desired_location=data.get("desired_location"),
            decision_reason=data.get("decision_reason"), authorization_ref=data.get("authorization_ref"),
            urgency_source=data.get("urgency_source", URGENCY_SOURCE_EVENT),
        )
    except (KeyError, TypeError) as exc:
        raise ValueError(f"候選欄位缺漏或型別錯誤：{type(exc).__name__}") from None


def _tuple(value: Any) -> tuple[Any, ...]:
    if isinstance(value, str) or not isinstance(value, (tuple, list)):
        raise ValueError("必須是序列")  # noqa: TRY004 - 呼叫端統一以 ValueError 處理
    return tuple(value)


def _parse_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value)
    raise ValueError("必須是 ISO 時間字串")


# ---- 仲裁當下的即時事實 ----

@dataclass(frozen=True)
class GrantFact:
    """授權查證結果（由 context 側依 ``authorization_ref`` 查得；Phase 1 以 fixture 提供）。

    ``locations`` 列出授權涵蓋的通道，目標通道不在其中視為未涵蓋（空 tuple = 沒有授權任何通道）。
    """

    valid: bool = False
    revoked: bool = False
    scope_refs: tuple[str, ...] = ()  # 授權涵蓋的內容 scope（逐一精確比對）
    locations: tuple[PresenceLocation, ...] = ()
    may_choose_location: bool = False  # Presence 為 NONE 時可否自選位置
    may_move: bool = False  # 位置不符時可否直接移轉（否則只提議 request_move 並 deferred）


@dataclass(frozen=True)
class LedgerEntry:
    """近期投遞紀錄（呼叫端注入）；Phase 1 無真實投遞，內容來自測試資料或 shadow 推算。"""

    dedupe_key: str
    target_ref: str  # 對象（owner_actor_id）
    delivered_at: datetime
    ack: str = "acked"  # acked | unknown


@dataclass(frozen=True)
class ProactivePolicy:
    """規則門檻（暫定值，標示於草案 §8；數值未定前以預設注入）。"""

    version: int = 0
    policy_ref: str = DEFAULT_POLICY_REF
    confidence_floor: float = 0.5
    cooldown_s: int = 600  # 同對象最小投遞間隔
    budget_max: int = 5  # 單位時間主動次數上限（單位時間由呼叫端統計）
    short_retry_ms: int = 5_000  # 移轉中／恢復中
    checkpoint_retry_ms: int = 30_000  # 等 checkpoint（活動不可中斷、靜音、承諾中的互動）
    budget_retry_ms: int = 300_000
    queued_retry_ms: int = 60_000  # Presence 為 NONE 時列待交付


@dataclass(frozen=True)
class ProactiveContext:
    """``facts["context"]``：assembly 組裝的即時事實；不得由候選或模型自帶。"""

    now: datetime
    owner_actor_id: str
    audience_scope: str
    presence_status: PresenceStatus
    current_platform: PresenceLocation | None = None
    presence_generation: int = 0
    transfer_pending: bool = False
    private_locations: tuple[PresenceLocation, ...] = ()
    grant: GrantFact | None = None
    global_stop: bool = False
    muted: bool = False
    in_committed_interaction: bool = False
    activity_interruptible: bool = True
    activity_cost: float = 0.0  # 即時打擾成本 0..1
    ledger: tuple[LedgerEntry, ...] = ()
    budget_used: int = 0
    policy: ProactivePolicy = field(default_factory=ProactivePolicy)

    def __post_init__(self) -> None:
        _aware("now", self.now)
        _ref("owner_actor_id", self.owner_actor_id)
        scope_rank(self.audience_scope)
        _set(
            self,
            presence_status=_enum(PresenceStatus, "presence_status", self.presence_status),
            activity_cost=_unit("activity_cost", self.activity_cost),
        )
        if self.current_platform is not None:
            _set(self, current_platform=_enum(PresenceLocation, "current_platform", self.current_platform))
        _set(self, private_locations=tuple(_enum(PresenceLocation, "private_locations", v) for v in self.private_locations))
        _nonneg_int("budget_used", self.budget_used)
        _nonneg_int("presence_generation", self.presence_generation)


def context_to_facts(ctx: ProactiveContext) -> dict[str, Any]:
    """context -> ``facts["context"]``（展平成英文欄位的純資料）。"""
    grant = ctx.grant
    p = ctx.policy
    return {
        "now": ctx.now.isoformat(), "owner_actor_id": ctx.owner_actor_id, "audience_scope": ctx.audience_scope,
        "presence_status": ctx.presence_status.value,
        "current_platform": ctx.current_platform.value if ctx.current_platform else None,
        "presence_generation": ctx.presence_generation, "transfer_pending": ctx.transfer_pending,
        "private_locations": [v.value for v in ctx.private_locations],
        "grant": None if grant is None else {
            "valid": grant.valid, "revoked": grant.revoked, "scope_refs": list(grant.scope_refs),
            "locations": [v.value for v in grant.locations], "may_choose_location": grant.may_choose_location,
            "may_move": grant.may_move,
        },
        "global_stop": ctx.global_stop, "muted": ctx.muted, "in_committed_interaction": ctx.in_committed_interaction,
        "activity_interruptible": ctx.activity_interruptible, "activity_cost": ctx.activity_cost,
        "ledger": [
            {"dedupe_key": e.dedupe_key, "target_ref": e.target_ref, "delivered_at": e.delivered_at.isoformat(),
             "ack": e.ack}
            for e in ctx.ledger
        ],
        "budget_used": ctx.budget_used,
        "policy": {
            "version": p.version, "policy_ref": p.policy_ref, "confidence_floor": p.confidence_floor,
            "cooldown_s": p.cooldown_s, "budget_max": p.budget_max, "short_retry_ms": p.short_retry_ms,
            "checkpoint_retry_ms": p.checkpoint_retry_ms, "budget_retry_ms": p.budget_retry_ms,
            "queued_retry_ms": p.queued_retry_ms,
        },
    }


def context_from_facts(data: Mapping[str, Any]) -> ProactiveContext:
    """``facts["context"]`` -> context；缺鍵或驗證失敗拋 ``ValueError``。"""
    try:
        g = data.get("grant")
        grant = None if g is None else GrantFact(
            valid=bool(g["valid"]), revoked=bool(g["revoked"]), scope_refs=_tuple(g["scope_refs"]),
            locations=tuple(_enum(PresenceLocation, "grant.locations", v) for v in _tuple(g["locations"])),
            may_choose_location=bool(g["may_choose_location"]), may_move=bool(g["may_move"]),
        )
        ledger = tuple(
            LedgerEntry(
                dedupe_key=_ref("ledger.dedupe_key", e["dedupe_key"]), target_ref=_ref("ledger.target_ref", e["target_ref"]),
                delivered_at=_aware("ledger.delivered_at", _parse_dt(e["delivered_at"])),
                ack=e["ack"] if e["ack"] in ("acked", "unknown") else _bad("ledger.ack"),
            )
            for e in _tuple(data["ledger"])
        )
        policy = ProactivePolicy(**{k: v for k, v in dict(data["policy"]).items()})
        return ProactiveContext(
            now=_parse_dt(data["now"]), owner_actor_id=data["owner_actor_id"], audience_scope=data["audience_scope"],
            presence_status=data["presence_status"], current_platform=data.get("current_platform"),
            presence_generation=data["presence_generation"], transfer_pending=bool(data["transfer_pending"]),
            private_locations=_tuple(data["private_locations"]), grant=grant, global_stop=bool(data["global_stop"]),
            muted=bool(data["muted"]), in_committed_interaction=bool(data["in_committed_interaction"]),
            activity_interruptible=bool(data["activity_interruptible"]), activity_cost=data["activity_cost"],
            ledger=ledger, budget_used=data["budget_used"], policy=policy,
        )
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError(f"context 欄位缺漏或型別錯誤：{type(exc).__name__}") from None


def _bad(name: str) -> Any:
    raise ValueError(f"{name} 不合法")


# ---- 規格 ----

def proactive_initiate_spec(budget_ms: int = _DEFAULT_BUDGET_MS) -> DecisionPointSpec:
    """``proactive.initiate`` 規格：async、shadow-only、不開 native、不用 LLM；fallback 一律 ``deferred``（不誤啟動）。

    ``plan_sections`` 宣告 ``loop``／``placement``／``followups``；hub 目前只強制 ``policy``／``capability`` 白名單（草案 Q-I、DL-4 PR1），
    其餘區段限制由 ``test_decision_proactive`` 以引擎單元測試鎖定。
    """
    return DecisionPointSpec(
        kind=KIND_PROACTIVE_INITIATE, mode="async", owner="proactive", outcomes=OUTCOMES, budget_ms=budget_ms,
        on_timeout_reason=ON_TIMEOUT_REASON, on_busy_reason=ON_BUSY_REASON, on_error_reason=ON_ERROR_REASON,
        on_invalid_reason=ON_INVALID_REASON, shadow=True, allow_native=False,
        plan_sections=("loop", "placement", "followups"),
    )


# ---- 規則仲裁 ----

@dataclass(frozen=True)
class ProactiveVerdict:
    outcome: str
    reason_code: str
    gate: str
    plan: DecisionPlan


def _verdict(outcome: str, reason: str, gate: str, plan: DecisionPlan | None = None) -> ProactiveVerdict:
    return ProactiveVerdict(outcome, reason, gate, plan or DecisionPlan())


def _defer(reason: str, gate: str, after_ms: int, placement: DecisionPlacement | None = None) -> ProactiveVerdict:
    return _verdict(
        OUTCOME_DEFERRED, reason, gate,
        DecisionPlan(placement=placement, followups=(DecisionFollowup(kind=KIND_PROACTIVE_INITIATE, after_ms=after_ms),)),
    )


def _movable(loc: PresenceLocation | None) -> bool:
    """可請求她前往的場域：與 presence.absent_intent 的 TARGET_LOCATIONS 同一份閉集（console 只能被排程指定，不可成為移轉目標）。"""
    return loc is not None and loc.value in TARGET_LOCATIONS


def _value(candidate: ProactiveCandidate) -> float:
    """打擾價值：salience（排序用）、urgency、confidence 的加權和；暫定權重。"""
    return 0.35 * candidate.memory_salience + 0.45 * _URGENCY_WEIGHT[candidate.urgency] + 0.20 * candidate.confidence


def arbitrate(candidate: ProactiveCandidate, ctx: ProactiveContext) -> ProactiveVerdict:
    """9 關規則仲裁（純函式）：硬性條件先於評分，高分不得抵銷。依序 G0 validity、G1 stop、G2 authorization、
    G3 audience、G4 presence、G5 cost、G6 cooldown、G7 budget、G8 confidence，全過才是 ready。"""
    p = ctx.policy
    c = candidate
    # G0 validity（malformed 由建構階段攔下，見 ProactiveEngine）
    if c.expires_at <= ctx.now:
        return _verdict(OUTCOME_REJECTED, R_CANDIDATE_EXPIRED, GATE_VALIDITY)
    # G1 stop／撤銷：優先序最高，assignment 也一樣
    if ctx.global_stop:
        return _verdict(OUTCOME_REJECTED, R_GLOBAL_STOP, GATE_STOP)
    grant = ctx.grant
    if grant is not None and grant.revoked:
        return _verdict(OUTCOME_REJECTED, R_AUTHORIZATION_REVOKED, GATE_STOP)
    # G2 authorization：assignment 且 owner 為已知 owner 視為已授權（G3 起照走）
    pre_authorized = c.source_kind is SourceKind.ASSIGNMENT and c.owner_actor_id == ctx.owner_actor_id
    if not pre_authorized:
        if c.authorization_ref is None or grant is None or not grant.valid:
            return _verdict(OUTCOME_REJECTED, R_NO_AUTHORIZATION, GATE_AUTHORIZATION)
        channel = c.desired_location or ctx.current_platform
        if c.privacy_scope not in grant.scope_refs or (channel is not None and channel not in grant.locations):
            return _verdict(OUTCOME_REJECTED, R_AUTHORIZATION_SCOPE_INSUFFICIENT, GATE_AUTHORIZATION)
    # 移轉目標守門：desired_location 不在可請求場域時 fail-closed（比照其他不允許路徑：rejected，不產生 placement、不在原地開口）
    if c.desired_location is not None and not _movable(c.desired_location):
        return _verdict(OUTCOME_REJECTED, R_TARGET_NOT_MOVABLE, GATE_PRESENCE)
    # G3 audience：聽眾比內容寬 -> deferred；有私人位置可移則提議 request_move
    if not scope_admits(ctx.audience_scope, c.privacy_scope):
        movable_private = tuple(loc for loc in ctx.private_locations if _movable(loc))  # 非可移轉場域不當移轉落點
        move_to = c.desired_location if c.desired_location in movable_private else (
            movable_private[0] if movable_private else None
        )
        if move_to is not None:
            placement = DecisionPlacement(
                target_platform=move_to.value, presence_intent="request_move", audience_scope_max=c.privacy_scope,
            )
            return _defer(R_AUDIENCE_TOO_WIDE_MOVE, GATE_AUDIENCE, p.checkpoint_retry_ms, placement)
        return _defer(R_AUDIENCE_TOO_WIDE_WAIT, GATE_AUDIENCE, p.checkpoint_retry_ms)
    # G4 presence
    target: PresenceLocation | None
    intent = "stay"
    ready_reason = R_READY_PRESENT
    if ctx.transfer_pending or ctx.presence_status is PresenceStatus.RECOVERING:
        return _defer(R_PRESENCE_TRANSFERRING, GATE_PRESENCE, p.short_retry_ms)
    if ctx.presence_status is PresenceStatus.NONE:
        grant_movable = tuple(loc for loc in grant.locations if _movable(loc)) if grant else ()
        chosen = (c.desired_location or (grant_movable[0] if grant_movable else None))
        if grant is not None and grant.may_choose_location and chosen is not None:
            target, intent, ready_reason = chosen, "none", R_PRESENCE_NONE_PREAUTHORIZED
        else:
            return _defer(R_PRESENCE_NONE_QUEUED, GATE_PRESENCE, p.queued_retry_ms)
    elif c.desired_location is not None and c.desired_location != ctx.current_platform:
        if grant is not None and grant.may_move:
            target, intent, ready_reason = c.desired_location, "request_move", R_READY_MOVE
        else:
            placement = DecisionPlacement(
                target_platform=c.desired_location.value, presence_intent="request_move",
                audience_scope_max=c.privacy_scope,
            )
            return _defer(R_PRESENCE_MISMATCH_MOVE, GATE_PRESENCE, p.checkpoint_retry_ms, placement)
    else:
        target = ctx.current_platform
    # G5 interruption cost：不可中斷先擋（critical 也不略過），再比較價值與即時成本
    if ctx.muted:
        return _defer(R_MUTED, GATE_COST, p.checkpoint_retry_ms)
    if ctx.in_committed_interaction:
        return _defer(R_IN_COMMITTED_INTERACTION, GATE_COST, p.checkpoint_retry_ms)
    if not ctx.activity_interruptible:
        return _defer(R_ACTIVITY_NOT_INTERRUPTIBLE, GATE_COST, p.checkpoint_retry_ms)
    if c.urgency is not Urgency.CRITICAL and _value(c) < ctx.activity_cost:
        return _verdict(OUTCOME_REJECTED, R_COST_EXCEEDS_VALUE, GATE_COST)
    # G6 cooldown／dedupe
    same = [e for e in ctx.ledger if e.dedupe_key == c.dedupe_key]
    if any(e.ack == "unknown" for e in same):
        return _defer(R_DELIVERY_UNKNOWN, GATE_COOLDOWN, p.checkpoint_retry_ms)  # 先查核交付、不重試
    if same:
        return _verdict(OUTCOME_REJECTED, R_DUPLICATE_DELIVERED, GATE_COOLDOWN)
    recent = [e for e in ctx.ledger if e.target_ref == c.owner_actor_id]
    if recent:
        remaining_s = p.cooldown_s - (ctx.now - max(e.delivered_at for e in recent)).total_seconds()
        if remaining_s > 0:
            return _defer(R_COOLDOWN_ACTIVE, GATE_COOLDOWN, max(0, round(remaining_s * 1000)))
    # G7 budget
    if ctx.budget_used >= p.budget_max:
        return _defer(R_BUDGET_EXHAUSTED, GATE_BUDGET, p.budget_retry_ms)
    # G8 confidence floor：評分，不是硬性條件，放最後
    if c.confidence < p.confidence_floor:
        return _verdict(OUTCOME_REJECTED, R_LOW_CONFIDENCE, GATE_CONFIDENCE)
    # ready
    if c.goal_ref == GOAL_REQUEST_PRESENCE_MOVE:
        intent = "request_move"
    if pre_authorized and c.source_kind is SourceKind.ASSIGNMENT and ready_reason == R_READY_PRESENT:
        ready_reason = R_READY_ASSIGNMENT
    placement = DecisionPlacement(
        target_platform=target.value if target is not None else None, presence_intent=intent,
        audience_scope_max=c.privacy_scope,
    )
    loop = None
    if c.goal_ref in RUN_GOAL_CODES:
        loop = DecisionLoop(
            start=True, trigger_kind="external_event" if c.source_kind is SourceKind.ASSIGNMENT else "proactive",
            goal_code=c.goal_ref, policy_ref=p.policy_ref,
        )
    return _verdict(OUTCOME_READY, ready_reason, GATE_READY, DecisionPlan(placement=placement, loop=loop))


# ---- engine ----

_QUIET_CAP = 1024


class ProactiveEngine:
    """``DecisionEngine``：``facts["candidate"]``＋``facts["context"]`` -> ``DecisionResult``。

    無狀態仲裁；engine 只持有日誌靜默抑制用的小表（同 candidate_id 相同 reason_code 連續出現只計數）。
    同時只處理一個請求（上一個還在跑時回 busy fallback）；超過 ``spec.budget_ms`` 的結果丟棄並回 timeout fallback。
    副作用：把命中的關卡名寫入 ``request.facts["sub_kind"]``，供決策日誌 ``sub_kind`` 欄使用。
    """

    def __init__(self, spec: DecisionPointSpec | None = None, *, clock: Callable[[], float] = time.perf_counter) -> None:
        self._spec = spec or proactive_initiate_spec()
        self._clock = clock
        self._guard = threading.Lock()
        self._lock = threading.Lock()
        self._candidate_of: OrderedDict[str, str] = OrderedDict()  # decision_id -> candidate_id
        self._last_reason: OrderedDict[str, str] = OrderedDict()  # candidate_id -> 上一筆已記錄的 reason_code

    def decide(self, request: DecisionRequest) -> DecisionResult:
        spec = self._spec
        if not self._guard.acquire(blocking=False):
            return self._fallback(request, spec.on_busy_reason)
        try:
            started = self._clock()
            try:
                ctx_data, cand_data = request.facts.get("context"), request.facts.get("candidate")
                if not isinstance(ctx_data, Mapping) or not isinstance(cand_data, Mapping):
                    return self._fallback(request, spec.on_invalid_reason)
                try:
                    ctx = context_from_facts(ctx_data)
                except ValueError:
                    return self._fallback(request, spec.on_invalid_reason)
                try:
                    candidate = candidate_from_facts(cand_data)
                except ValueError:
                    verdict, cand_id = _verdict(OUTCOME_REJECTED, R_CANDIDATE_MALFORMED, GATE_VALIDITY), None
                else:
                    verdict, cand_id = arbitrate(candidate, ctx), candidate.candidate_id
            except Exception as exc:  # noqa: BLE001 - 任何例外只轉成 fallback，不外洩訊息
                logger.warning("proactive.initiate 仲裁例外：%s", type(exc).__name__)
                return self._fallback(request, spec.on_error_reason)
            if spec.budget_ms and spec.budget_ms > 0 and (self._clock() - started) * 1000 > spec.budget_ms:
                return self._fallback(request, spec.on_timeout_reason)
        finally:
            self._guard.release()
        try:
            request.facts["sub_kind"] = verdict.gate
        except TypeError:  # facts 為唯讀映射時略過，日誌 sub_kind 為 None
            logger.debug("proactive.initiate facts 唯讀，無法標註 sub_kind")
        result = DecisionResult(
            decision_id=new_id("dec"), request_id=request.request_id, kind=request.kind, outcome=verdict.outcome,
            plan=verdict.plan, confidence=1.0, reason_code=verdict.reason_code,
            engine=DecisionEngineInfo(tier="rule", id=_ENGINE_ID, version=_ENGINE_VERSION),
        )
        key = cand_id or request.subject.get("candidate_id")
        if key:
            with self._lock:
                self._candidate_of[result.decision_id] = key
                while len(self._candidate_of) > _QUIET_CAP:
                    self._candidate_of.popitem(last=False)
        return result

    def log_quiet(self, result: DecisionResult) -> bool:
        """決策日誌靜默抑制：fallback 一定記；同 candidate_id 相同 reason_code 連續出現回 True（只累加計數）。
        每個結果只應被問一次（由日誌觀察者呼叫）。"""
        with self._lock:
            key = self._candidate_of.pop(result.decision_id, None)
            if result.fallback or key is None:
                return False
            if self._last_reason.get(key) == result.reason_code:
                self._last_reason.move_to_end(key)
                return True
            self._last_reason[key] = result.reason_code
            self._last_reason.move_to_end(key)
            while len(self._last_reason) > _QUIET_CAP:
                self._last_reason.popitem(last=False)
            return False

    @staticmethod
    def _fallback(request: DecisionRequest, reason: str) -> DecisionResult:
        return DecisionResult(
            decision_id=new_id("dec"), request_id=request.request_id, kind=request.kind, outcome=OUTCOME_DEFERRED,
            plan=DecisionPlan(), confidence=0.0, reason_code=reason,
            engine=DecisionEngineInfo(tier="fallback", id="core.fallback", version="d80"), fallback=True,
        )


def register_proactive(
    hub: DecisionHub, *, spec: DecisionPointSpec | None = None, clock: Callable[[], float] = time.perf_counter,
) -> ProactiveEngine:
    """把 ``proactive.initiate`` 登記到中樞，回傳 engine。Phase 1 不掛任何會啟動 Run 的消費者，只掛決策日誌觀察者。"""
    spec = spec or proactive_initiate_spec()
    engine = ProactiveEngine(spec, clock=clock)
    hub.register(spec, engine)
    return engine


def build_request(
    candidate: ProactiveCandidate, ctx: ProactiveContext, *, namespace: str, seq: int = 1,
    trigger: str = "event", parent_decision_id: str | None = None,
) -> DecisionRequest:
    """組 ``proactive.initiate`` 請求（冪等鍵 ``<candidate_id>:eval:<seq>``）。``namespace`` 為日誌主體欄位。"""
    spec = proactive_initiate_spec()
    return DecisionRequest(
        request_id=f"{candidate.candidate_id}:eval:{seq}", kind=KIND_PROACTIVE_INITIATE, trigger=trigger,  # type: ignore[arg-type]
        parent_decision_id=parent_decision_id,
        subject={"candidate_id": candidate.candidate_id, "namespace": namespace},
        facts={"candidate": candidate_to_facts(candidate), "context": context_to_facts(ctx)},
        allowed_outcomes=spec.outcomes,
        snapshot_versions={"policy": ctx.policy.version, "presence": ctx.presence_generation},
    )


# ---- S-H 轉換（純函式；Phase 1 的輸出只記錄不啟動）----

@dataclass(frozen=True)
class ProactiveInjections:
    """轉換所需的注入值，不得由候選或模型自帶。

    ``capability_lookup(goal_code)`` 由工具層（S-H）提供 goal_code -> capability_tags；回空或查不到就視為
    ``capability_empty``（Agent 層永遠不會收到空 capability_tags）。決策側不 import 工具層的標籤表。
    """

    capability_lookup: Callable[[str], Collection[str] | None]
    snapshot_ref: str


def loop_trigger_decision(
    candidate: ProactiveCandidate, result: DecisionResult, inj: ProactiveInjections,
) -> tuple[LoopTrigger | None, str]:
    """回傳 ``(LoopTrigger | None, 原因)``；原因為 ``LT_*`` 常數，供日誌記錄 would_start 或不啟動的理由。"""
    plan = result.plan
    if result.outcome != OUTCOME_READY:
        return None, LT_NOT_READY
    loop = plan.loop if plan is not None else None
    if loop is None or not loop.start or not loop.goal_code:
        return None, LT_NO_LOOP
    if loop.trigger_kind not in TRIGGER_KINDS:
        return None, LT_BAD_TRIGGER
    try:
        tags = frozenset(inj.capability_lookup(loop.goal_code) or ())
    except Exception as exc:  # noqa: BLE001 - 查表失敗視同查不到
        logger.warning("capability_lookup 例外：%s", type(exc).__name__)
        tags = frozenset()
    if not tags:
        return None, LT_CAPABILITY_EMPTY
    trigger = LoopTrigger(
        kind=loop.trigger_kind,  # type: ignore[arg-type]
        origin_ref=candidate.candidate_id, goal_code=loop.goal_code, capability_tags=tags,
        scope_ref=candidate.privacy_scope,  # 只可收窄（E-6）；audience_scope_max 的最終把關在工具層投影
        owner_actor_id=candidate.owner_actor_id, policy_ref=loop.policy_ref or DEFAULT_POLICY_REF,
        snapshot_ref=inj.snapshot_ref, plan_ref=result.decision_id,
    )
    return trigger, LT_WOULD_START


def to_loop_trigger(
    candidate: ProactiveCandidate, result: DecisionResult, inj: ProactiveInjections,
) -> LoopTrigger | None:
    """``(candidate, result, injections) -> LoopTrigger | None``。None 條件：outcome 非 ready、``plan.loop`` 為 None 或
    ``start=False``、trigger_kind 不合法、capability_lookup 查不到或回空。"""
    return loop_trigger_decision(candidate, result, inj)[0]
