"""State Coordinator（契約 CC-7）：各軸投影與 Interaction／Behavior 轉換請求的合法性裁決。

- 唯一寫入 ``state_projection``（6A.3）；不寫 presence／tasks／deliveries 等他人實體表（7.1）。
- 只有 Interaction、Behavior 兩軸接受轉換請求（``request_transition``）；Runtime／Presence／Health
  由實體 Manager 的事件或 Core lifecycle 驅動投影（``set_runtime``、``project_event``），
  Presence 軸不接受請求（S-2、VC-7A-2）。
- 請求帶 ``expected_version``（樂觀併發，S-3）；``request_id`` 為冪等鍵，重送回原裁決、不重寫。
  每個請求（含被拒者）都記入 ``state_requests`` 與 journal（``state.transition_requested``）；
  ACCEPTED 另發 ``state.changed``。裁決在鎖定投影列的單一交易內完成，提交後才投遞 Bus。
- 投影可由 journal 事件重建：``project_event`` 以來源 ``aggregate_version`` 去重，重播不產生外部輸出（S-6）。
- 沒有儲存列的軸視為（預設狀態，版本 0）；第一次轉換寫入版本 1。
- 合法轉換表以 ``STATE_POLICY_VERSION`` 版本化；列舉內容為草案補充（D40）。
- 不 import 任何 CC-2~CC-6 型別（契約 0.4）；Task 軸投影不在本卡範圍。
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from uep.events.envelope import EventDraft, TrustedContext, UEPEvent
from uep.events.publisher import EventPublisher, RecordedEvent
from uep.state.models import StateProjectionRow, StateRequestRow

logger = logging.getLogger("uep.state.coordinator")

SOURCE = "core.state_coordinator"
ACTOR = "uep:state_coordinator"
SCOPE = "scope_private_owner"
STATE_POLICY_VERSION = 1
# project_event 只信任這些來源（Gateway 重建後的 source 欄位），其他來源的事件不得驅動投影
DEFAULT_PRESENCE_SOURCES = frozenset({"core.presence"})
DEFAULT_HEALTH_SOURCES = frozenset({"core.health", "core.health_registry"})


class Axis(StrEnum):
    RUNTIME = "Runtime"
    PRESENCE = "Presence"
    INTERACTION = "Interaction"
    BEHAVIOR = "Behavior"
    HEALTH = "Health"


class Decision(StrEnum):
    ACCEPTED = "ACCEPTED"
    REJECTED_ILLEGAL = "REJECTED_ILLEGAL"
    REJECTED_VERSION = "REJECTED_VERSION"
    REJECTED_UNAUTHORIZED = "REJECTED_UNAUTHORIZED"
    REJECTED_EXPIRED = "REJECTED_EXPIRED"
    DEFERRED = "DEFERRED"


class RuntimeState(StrEnum):
    STARTING = "STARTING"
    READY = "READY"
    DEGRADED = "DEGRADED"
    STOPPING = "STOPPING"


class InteractionState(StrEnum):
    IDLE = "IDLE"
    LISTENING = "LISTENING"
    THINKING = "THINKING"
    SPEAKING = "SPEAKING"
    WAITING_USER = "WAITING_USER"


class BehaviorState(StrEnum):
    CHAT = "CHAT"
    WORK = "WORK"
    MISCHIEF = "MISCHIEF"
    SLEEP = "SLEEP"


class HealthState(StrEnum):
    READY = "ready"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


PRESENCE_STATES = frozenset({"NONE", "ACTIVE", "RECOVERING"})

AXIS_STATES: dict[Axis, frozenset[str]] = {
    Axis.RUNTIME: frozenset(s.value for s in RuntimeState),
    Axis.PRESENCE: PRESENCE_STATES,
    Axis.INTERACTION: frozenset(s.value for s in InteractionState),
    Axis.BEHAVIOR: frozenset(s.value for s in BehaviorState),
    Axis.HEALTH: frozenset(s.value for s in HealthState),
}
DEFAULT_STATES: dict[Axis, str] = {
    Axis.RUNTIME: RuntimeState.STARTING.value,
    Axis.PRESENCE: "NONE",
    Axis.INTERACTION: InteractionState.IDLE.value,
    Axis.BEHAVIOR: BehaviorState.CHAT.value,
    Axis.HEALTH: HealthState.READY.value,
}
# 只有這兩軸接受轉換請求（S-2）
REQUESTABLE_AXES = frozenset({Axis.INTERACTION, Axis.BEHAVIOR})

_I, _B, _R = InteractionState, BehaviorState, RuntimeState
INTERACTION_TRANSITIONS: dict[str, frozenset[str]] = {
    _I.IDLE: frozenset({_I.LISTENING, _I.THINKING}),
    _I.LISTENING: frozenset({_I.THINKING, _I.IDLE}),
    _I.THINKING: frozenset({_I.SPEAKING, _I.WAITING_USER, _I.LISTENING, _I.IDLE}),
    _I.SPEAKING: frozenset({_I.IDLE, _I.LISTENING, _I.WAITING_USER}),
    _I.WAITING_USER: frozenset({_I.LISTENING, _I.THINKING, _I.IDLE}),
}
BEHAVIOR_TRANSITIONS: dict[str, frozenset[str]] = {
    _B.CHAT: frozenset({_B.WORK, _B.MISCHIEF, _B.SLEEP}),
    _B.WORK: frozenset({_B.CHAT, _B.MISCHIEF, _B.SLEEP}),
    _B.MISCHIEF: frozenset({_B.CHAT, _B.WORK, _B.SLEEP}),
    _B.SLEEP: frozenset({_B.CHAT}),  # 睡眠需先喚醒，不能直接進入工作／搗蛋
}
RUNTIME_TRANSITIONS: dict[str, frozenset[str]] = {
    _R.STARTING: frozenset({_R.READY, _R.DEGRADED, _R.STOPPING}),
    _R.READY: frozenset({_R.DEGRADED, _R.STOPPING}),
    _R.DEGRADED: frozenset({_R.READY, _R.STOPPING}),
    _R.STOPPING: frozenset(),
}
_REQUEST_TABLES: dict[Axis, dict[str, frozenset[str]]] = {
    Axis.INTERACTION: INTERACTION_TRANSITIONS,
    Axis.BEHAVIOR: BEHAVIOR_TRANSITIONS,
}
# 全域停止期間不得進入的主動行為（T-10：阻止新工具與主動行動）
_ACTIVE_BEHAVIORS = frozenset({_B.WORK.value, _B.MISCHIEF.value})


class StateTransitionRequest(BaseModel):
    """轉換請求（6A.2）。``proposer`` 必須是 Gateway 認證後的提議者，不是 payload 自稱。"""

    model_config = ConfigDict(frozen=True)

    request_id: str
    axis: str  # 字串：非法軸也要能被裁決並留下被拒紀錄
    proposer: str = Field(min_length=1)
    expected_version: int = Field(ge=0)
    from_state: str
    to_state: str
    reason: dict[str, Any] = Field(default_factory=dict)  # 理由與證據引用，不含私人原文
    scope_ref: str = SCOPE
    expires_at: datetime | None = None


@dataclass(frozen=True)
class StateDecision:
    request_id: str
    decision: Decision
    reason_code: str | None
    new_version: int | None
    duplicate: bool = False  # 重送命中既有裁決


@dataclass(frozen=True)
class StateView:
    persona_id: str
    axis: Axis
    subject: str
    state: str
    version: int
    policy_version: int


# 全域停止暫停旗標檢查：用呼叫者的 session 讀，旗標成立回原因碼，None 表示放行
StopCheck = Callable[[Session], str | None]


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _check_axis_state(axis: Axis, state: str) -> None:
    if state not in AXIS_STATES[axis]:
        raise ValueError(f"{axis.value} 軸沒有狀態 {state!r}")


class StateCoordinator:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        publisher: EventPublisher,
        *,
        persona_id: str = "uep",
        clock: Callable[[], datetime] = _utcnow,
        authorized_proposers: Mapping[Axis, frozenset[str]] | None = None,
        presence_sources: frozenset[str] = DEFAULT_PRESENCE_SOURCES,
        health_sources: frozenset[str] = DEFAULT_HEALTH_SOURCES,
        global_stop_check: StopCheck | None = None,
        global_stop_locked_check: StopCheck | None = None,
        outbox_targets: tuple[str, ...] = (),
    ) -> None:
        self._sf = session_factory
        self._publisher = publisher
        self._persona = persona_id
        self._clock = clock
        # fail-closed（S-1）：沒給名單＝沒有任何提議者被授權；每個軸只收名單內的提議者
        self._proposers = dict(authorized_proposers) if authorized_proposers is not None else {}
        self._presence_sources = presence_sources
        self._health_sources = health_sources
        self._stop_check = global_stop_check
        self._stop_locked_check = global_stop_locked_check
        self._outbox_targets = outbox_targets

    @property
    def persona_id(self) -> str:
        return self._persona

    # ---- 基礎 ----

    @contextmanager
    def _tx(self) -> Iterator[tuple[Session, list[RecordedEvent]]]:
        """單一交易；提交後才投遞事件（投遞失敗只記 log，不影響已提交的裁決）。"""
        pending: list[RecordedEvent] = []
        with self._sf() as session, session.begin():
            yield session, pending
        for recorded in pending:
            try:
                self._publisher.dispatch(recorded)
            except Exception:
                logger.exception("state 事件投遞失敗：%s", recorded.event.event_type)

    def _lock(self, s: Session, axis: Axis, subject: str) -> StateProjectionRow:
        """鎖定投影列；不存在則先以預設值建立（版本 0）。鎖定讀取必須 populate_existing。"""
        s.execute(
            pg_insert(StateProjectionRow)
            .values(
                persona_id=self._persona, axis=axis.value, subject=subject, state=DEFAULT_STATES[axis],
                version=0, policy_version=STATE_POLICY_VERSION,
            )
            .on_conflict_do_nothing(index_elements=["persona_id", "axis", "subject"])
        )
        return s.execute(
            select(StateProjectionRow)
            .where(
                StateProjectionRow.persona_id == self._persona, StateProjectionRow.axis == axis.value,
                StateProjectionRow.subject == subject,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        ).scalar_one()

    def _record(
        self, s: Session, pending: list[RecordedEvent], event_type: str, payload: dict[str, Any], *,
        actor: str = ACTOR, idempotency_key: str | None = None, aggregate_version: int | None = None,
    ) -> UEPEvent:
        draft = EventDraft(
            event_type=event_type, occurred_at=self._clock(), payload=payload, idempotency_key=idempotency_key,
            aggregate_type="state" if aggregate_version is not None else None,
            aggregate_id=self._persona if aggregate_version is not None else None,
            aggregate_version=aggregate_version,
        )
        trusted = TrustedContext(source=SOURCE, actor_id=actor, scope_ref=SCOPE, persona_id=self._persona)
        recorded = self._publisher.record(s, draft, trusted, outbox_targets=self._outbox_targets)
        pending.append(recorded)
        return recorded.event

    def _apply(
        self, s: Session, pending: list[RecordedEvent], row: StateProjectionRow, axis: Axis, subject: str,
        new_state: str, *, cause_event_id: str | None, source_version: int | None = None,
    ) -> None:
        """寫入投影並發 ``state.changed``（payload：axis／from／to／version／cause_event_id）。"""
        old_state = row.state
        row.state, row.version = new_state, row.version + 1
        row.policy_version = STATE_POLICY_VERSION
        row.cause_event_id = cause_event_id
        if source_version is not None:
            row.source_version = source_version
        row.updated_at = self._clock()
        payload: dict[str, Any] = {
            "axis": axis.value, "from_state": old_state, "to_state": new_state, "version": row.version,
        }
        if cause_event_id:
            payload["cause_event_id"] = cause_event_id
        if subject:
            payload["subject"] = subject
        self._record(
            s, pending, "state.changed", payload,
            idempotency_key=f"state:{self._persona}:{axis.value}:{subject}:{row.version}",
        )

    # ---- 查詢 ----

    def current(self, axis: Axis | str, subject: str = "") -> StateView:
        """唯讀快照；沒有儲存列時回（預設狀態，版本 0）。"""
        axis = Axis(axis)
        with self._sf() as s:
            row = s.get(StateProjectionRow, (self._persona, axis.value, subject))
            if row is None:
                return StateView(self._persona, axis, subject, DEFAULT_STATES[axis], 0, STATE_POLICY_VERSION)
            return StateView(self._persona, axis, subject, row.state, row.version, row.policy_version)

    def health(self) -> dict[str, str]:
        """所有服務的 Health 投影（subject -> state）。"""
        with self._sf() as s:
            rows = s.scalars(
                select(StateProjectionRow).where(
                    StateProjectionRow.persona_id == self._persona, StateProjectionRow.axis == Axis.HEALTH.value
                )
            )
            return {r.subject: r.state for r in rows}

    # ---- 轉換請求（Interaction／Behavior）----

    def request_transition(self, req: StateTransitionRequest) -> StateDecision:
        """裁決一個轉換請求。每個請求（含被拒者）都留下 ``state_requests`` 與 journal 紀錄。"""
        try:
            return self._request_transition(req)
        except IntegrityError:
            # 同一 request_id 併發寫入（例如非可請求軸沒有投影列鎖）：另一方已裁決，回既有結果
            with self._sf() as s:
                existing = s.get(StateRequestRow, req.request_id)
                if existing is None:
                    raise
                return StateDecision(
                    req.request_id, Decision(existing.decision), existing.reason_code, existing.new_version,
                    duplicate=True,
                )

    def _request_transition(self, req: StateTransitionRequest) -> StateDecision:
        with self._tx() as (s, pending):
            existing = s.get(StateRequestRow, req.request_id, populate_existing=True)
            if existing is not None:
                return StateDecision(
                    req.request_id, Decision(existing.decision), existing.reason_code, existing.new_version,
                    duplicate=True,
                )
            axis = self._parse_axis(req.axis)
            row: StateProjectionRow | None = None
            if axis in REQUESTABLE_AXES:
                row = self._lock(s, axis, "")  # 先鎖投影列，使 request_id 重送與併發請求序列化
                again = s.get(StateRequestRow, req.request_id, populate_existing=True)
                if again is not None:  # 等鎖期間同一 request_id 已被裁決
                    return StateDecision(
                        req.request_id, Decision(again.decision), again.reason_code, again.new_version,
                        duplicate=True,
                    )
            decision, code = self._decide(s, req, axis, row)
            # state_requests 為追加式（trigger 禁止 UPDATE），new_version 須在插入時就確定
            new_version = row.version + 1 if decision is Decision.ACCEPTED and row is not None else None
            now = self._clock()
            requested = self._record(
                s, pending, "state.transition_requested",
                {
                    "axis": req.axis[:32], "from_state": req.from_state[:64], "to_state": req.to_state[:64],
                    "version": req.expected_version, "reason_code": code or decision.value,
                    "status": decision.value, "ref_id": req.request_id,
                },
                actor=req.proposer[:128],
            )
            s.add(StateRequestRow(
                request_id=req.request_id, persona_id=self._persona, axis=req.axis, proposer=req.proposer,
                expected_version=req.expected_version, from_state=req.from_state, to_state=req.to_state,
                reason=req.reason, scope_ref=req.scope_ref, expires_at=req.expires_at,
                decision=decision.value, reason_code=code, new_version=new_version, decided_at=now,
                cause_event_id=requested.event_id,
            ))
            if decision is Decision.ACCEPTED:
                assert row is not None and axis is not None
                self._apply(s, pending, row, axis, "", req.to_state, cause_event_id=requested.event_id)
            return StateDecision(req.request_id, decision, code, new_version)

    @staticmethod
    def _parse_axis(value: str) -> Axis | None:
        try:
            return Axis(value)
        except ValueError:
            return None

    def _decide(
        self, s: Session, req: StateTransitionRequest, axis: Axis | None, row: StateProjectionRow | None
    ) -> tuple[Decision, str | None]:
        if req.expires_at is not None and req.expires_at <= self._clock():
            return Decision.REJECTED_EXPIRED, "request_expired"
        if axis is None or axis not in REQUESTABLE_AXES:
            return Decision.REJECTED_ILLEGAL, "axis_not_requestable"  # S-2：Presence 等軸不接受請求
        assert row is not None
        if req.proposer not in self._proposers.get(axis, frozenset()):
            return Decision.REJECTED_UNAUTHORIZED, "proposer_not_allowed"
        if req.expected_version != row.version:
            return Decision.REJECTED_VERSION, "version_mismatch"  # S-3：不得靜默覆寫
        if req.from_state != row.state:
            return Decision.REJECTED_ILLEGAL, "from_state_mismatch"
        if req.to_state not in AXIS_STATES[axis]:
            return Decision.REJECTED_ILLEGAL, "unknown_state"
        if req.to_state not in _REQUEST_TABLES[axis].get(row.state, frozenset()):
            return Decision.REJECTED_ILLEGAL, "transition_not_allowed"
        runtime = self._runtime_state(s)
        if runtime == RuntimeState.STARTING.value:
            return Decision.DEFERRED, "runtime_starting"  # 尚未 READY：稍後重提
        if runtime == RuntimeState.STOPPING.value:
            return Decision.REJECTED_ILLEGAL, "runtime_stopping"
        if axis is Axis.BEHAVIOR and req.to_state in _ACTIVE_BEHAVIORS and self._stop_check is not None:
            code = self._stop_check(s)  # T-10：全域停止期間不得轉入主動行為
            if code:
                return Decision.REJECTED_ILLEGAL, code
        return Decision.ACCEPTED, None

    def action_entry_blocked(self, session: Session) -> str | None:
        """execute_action 階段 1 的裁決（T-10），在呼叫者的交易 session 內進行。

        Runtime STOPPING 或全域停止旗標成立即拒絕；旗標以共享鎖讀（``global_stop_locked_check``），
        鎖持有到階段 1 提交，engage 因此不可能插在「讀旗標」與「提交」之間。
        """
        if self._runtime_state(session) == RuntimeState.STOPPING.value:
            return "runtime_stopping"
        check = self._stop_locked_check or self._stop_check
        return check(session) if check is not None else None

    def task_entry_blocked(self) -> str | None:
        """Task／Run 要（重新）進入 RUNNING 前的 CC-7 裁決：回拒絕原因碼，None 表示放行。

        取代 TaskManager 的 transition_guard 預設（None=全放行）：Core 停止中、或全域停止暫停旗標成立時
        一律不得轉入 RUNNING（T-10：阻止新工具與主動行動）。轉入 PAUSED 等收斂方向的轉移不受影響。
        """
        with self._sf() as s:
            if self._runtime_state(s) == RuntimeState.STOPPING.value:
                return "runtime_stopping"
            if self._stop_check is not None:
                return self._stop_check(s)
        return None

    def _runtime_state(self, s: Session) -> str | None:
        """讀 Runtime 軸（無鎖；只用來決定 DEFERRED／拒絕）。尚無 Runtime 投影列時不攔（None）。"""
        runtime = s.get(StateProjectionRow, (self._persona, Axis.RUNTIME.value, ""))
        return None if runtime is None else runtime.state

    # ---- Core lifecycle 與事件驅動的投影 ----

    def set_runtime(self, state: RuntimeState | str) -> bool:
        """Core lifecycle 回報 Runtime 軸（Core 為實體寫入者，這裡只投影）。回傳是否有變更。

        ``STARTING`` 代表新程序啟動，可自任何狀態進入；其餘依 ``RUNTIME_TRANSITIONS``，STOPPING 為終點。
        """
        target = RuntimeState(state).value
        with self._tx() as (s, pending):
            row = self._lock(s, Axis.RUNTIME, "")
            if row.version > 0 and row.state == target:
                return False
            if (
                target != RuntimeState.STARTING.value and row.version > 0
                and target not in RUNTIME_TRANSITIONS[RuntimeState(row.state)]
            ):
                raise ValueError(f"Runtime 軸不允許 {row.state} -> {target}")
            self._apply(s, pending, row, Axis.RUNTIME, "", target, cause_event_id=None)
            return True

    def project_event(self, event: UEPEvent) -> bool:
        """依事件投影 Presence／Health 軸；回傳是否更新。重播與晚到的舊事件為 no-op（S-6）。

        - ``presence.*``：狀態取自 payload.status（Presence Manager 發事件時的 presence_current.status）。
        - ``core.component_health_changed``：subject＝payload.ref_id（服務名），狀態＝payload.status
          （ready／degraded／unavailable）。Health 錯誤只寫 Health 軸，不動 Interaction（S-4）。
        """
        if event.persona_id != self._persona:
            return False
        if event.event_type.startswith("presence."):
            if event.source not in self._presence_sources:
                logger.warning("拒絕非 Presence Manager 來源的 %s：source=%s", event.event_type, event.source)
                return False
            axis, subject = Axis.PRESENCE, ""
            ordering = event.aggregate_version
        elif event.event_type == "core.component_health_changed":
            if event.source not in self._health_sources:
                logger.warning("拒絕非預期來源的 health 事件：source=%s", event.source)
                return False
            axis, subject = Axis.HEALTH, str(event.payload.get("ref_id") or "")
            ordering = event.journal_seq  # Health 事件沒有 aggregate 版本：以 journal 序號排序，晚到舊事件 no-op
            if not subject:
                logger.warning("health 事件缺少 ref_id（服務名），略過：%s", event.event_id)
                return False
        else:
            return False
        state = event.payload.get("status")
        if not isinstance(state, str):
            return False  # 沒有 status 的 presence.* 事件（如 arrival_requested）不是狀態轉換
        if state not in AXIS_STATES[axis]:
            logger.warning("%s 事件的 status 不合法，略過：%s", event.event_type, event.event_id)
            return False
        return self.project(
            axis, state, subject=subject, source_version=ordering, cause_event_id=event.event_id
        )

    def project(
        self, axis: Axis, state: str, *, subject: str = "", source_version: int | None = None,
        cause_event_id: str | None = None,
    ) -> bool:
        """把實體 Manager 的權威狀態投影到某軸（Presence／Health）；相同狀態或舊來源版本為 no-op。

        啟動時用來補投影初始 Presence（Core 重啟若沒有舊 Lease 就不會有 presence.* 事件）。
        """
        if axis not in (Axis.PRESENCE, Axis.HEALTH):
            raise ValueError(f"{axis.value} 軸不可由外部直接投影")
        _check_axis_state(axis, state)
        with self._tx() as (s, pending):
            row = self._lock(s, axis, subject)
            if source_version is not None and row.source_version is not None and source_version <= row.source_version:
                return False  # 重播或晚到
            if row.version > 0 and row.state == state:
                if source_version is not None:
                    row.source_version = source_version
                return False
            self._apply(
                s, pending, row, axis, subject, state, cause_event_id=cause_event_id,
                source_version=source_version,
            )
            return True
