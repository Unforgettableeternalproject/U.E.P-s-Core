"""Turn Orchestrator（P3接入方案 §1.2、§4）：唯一綁定 turn_id／turn_generation 的地方。

不依賴 echo_stream：只收「事實」（utterance、attempt、生成完成、回合結束），發 journal 事件、向 CC-7
提 Interaction 軸的 phase 請求（proposer ``core.turn``）、簽發 ``CoreTicket``。ES 轉接層（``uep/echo/``）
負責把 ES 的回呼換成這裡的呼叫。

執行緒：這裡的方法都會寫 DB，**不得在 ES 的 event loop 上呼叫**；轉接層用專屬工作緒呼叫。

事件（D81、S1 註冊表）：``input.utterance_final``（source ``core.echo_host``，外部來源、帶冪等鍵、文字走
payload_blob）；``turn.started``／``turn.attempt_started``／``turn.generation_completed``／
``turn.completed``／``turn.cancelled``／``turn.skipped``（source ``core.turn``）。``turn_generation`` 放信封
``fence``。``turn.cancelled`` 的 fence 帶**取消後**的新 generation（事件宣告「目前有效值」）。

終結事件對映（S3，ES 介面清單 C1／C6、待決 Q-3 採建議）：
- completed -> ``turn.completed(status=completed)``；skipped（PASS、mode_silent、無文字、語音出錯等）->
  ``turn.completed(status=skipped, reason_code=<跳過原因>)``；
- dropped_busy -> ``turn.skipped(reason_code=dropped_busy)``；
- cancelled／voided／failed -> ``turn.cancelled(reason_code)``，generation 前進（``shutdown`` 例外）。
Core 自己發起的取消（``cancel_turn``）以 Core 的原因碼為準（ES 的 ``reason_code`` 對 ``global_stop`` 等無法標記，
介面清單 D5／C2），僅在 ES 回報為取消／作廢／語音取消時覆寫；已完成的輪不被覆寫。

S4／S5 才做：Interaction 軸的 SPEAKING phase、Proposal.interaction_to（``core.decision``）、shadow mode。
"""
from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from uep.contracts.presence import AuthorizationTuple
from uep.events.envelope import EventDraft, TrustedContext
from uep.events.publisher import EventPublisher
from uep.ids import new_id
from uep.memory.identity import GUEST, IdentityDirectory
from uep.state.coordinator import (
    Axis,
    InteractionState,
    StateCoordinator,
    StateTransitionRequest,
)
from uep.turn.context import ContextRegistrar
from uep.turn.generations import TurnGenerations
from uep.turn.models import (
    CoreTicket,
    IntakeResult,
    Proposal,
    ReplyMode,
    SessionContext,
    TurnCandidate,
    TurnEndFacts,
    UtteranceFact,
)
from uep.turn.proposer import RespondProposer, TurnProposer

logger = logging.getLogger("uep.turn.orchestrator")

SOURCE_TURN = "core.turn"
ACTOR_TURN = "uep:turn"
SOURCE_ECHO = "core.echo_host"
ACTOR_ECHO = "uep:echo_host"
UNVERIFIED_ACTOR = "uep:unverified_speaker"  # 映射表沒有的說話者：不得填成 owner（D77）
PHASE_PROPOSER = "core.turn"

REASON_SHUTDOWN = "shutdown"
_CORE_OVERRIDABLE = frozenset({"cancelled", "voided"})
_OVERRIDABLE_SKIPS = frozenset({"voice_cancelled"})


def attempt_label(attempt_no: int) -> str:
    """ES 的整數 attempt 對映 Core 的字串 attempt_id（OutputIntent.attempt_id 是 str）。"""
    return f"att_{attempt_no}"


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class _Live:
    ticket: CoreTicket
    trace_id: str
    utterance_event_id: str | None
    cancel_reason: str | None = None
    bumped: bool = False
    generation_emitted: bool = False
    ended: bool = False


class TurnOrchestrator:
    def __init__(
        self,
        publisher: EventPublisher,
        generations: TurnGenerations,
        *,
        auth_provider: Callable[[], AuthorizationTuple | None],
        persona_id: str = "uep",
        proposer: TurnProposer | None = None,
        identity: IdentityDirectory | None = None,
        snapshot_version: Callable[[], int] = lambda: 0,
        ticket_ttl: timedelta = timedelta(minutes=10),
        clock: Callable[[], datetime] = _utcnow,
        registrar: ContextRegistrar | None = None,
    ) -> None:
        self._publisher = publisher
        self.generations = generations
        self._auth_provider = auth_provider
        self._persona = persona_id
        self._proposer = proposer or RespondProposer()
        self._identity = identity
        self._snapshot_version = snapshot_version
        self._ttl = ticket_ttl
        self._clock = clock
        self.registrar = registrar
        self.state: StateCoordinator | None = None
        self._lock = threading.Lock()
        self._live: dict[str, _Live] = {}
        self._finished: OrderedDict[str, None] = OrderedDict()

    def bind_state(self, state: StateCoordinator) -> None:
        self.state = state

    # ---- 觀測 ----

    def live_tickets(self) -> list[CoreTicket]:
        with self._lock:
            return [live.ticket for live in self._live.values() if not live.ended]

    def ticket_of(self, turn_id: str) -> CoreTicket | None:
        with self._lock:
            live = self._live.get(turn_id)
            return live.ticket if live is not None else None

    # ---- 輸入 ----

    def on_utterance(self, ctx: SessionContext, fact: UtteranceFact) -> IntakeResult:
        """記錄 utterance、跑決策插槽、簽 ticket 並落 ``turn.started``；不開 turn 時回 ticket=None。"""
        ident = self._identity.resolve_platform(fact.speaker_ref) if self._identity is not None else GUEST
        actor_id = ident.actor_id or UNVERIFIED_ACTOR
        payload: dict[str, object] = {"speaker_ref": fact.speaker_ref, "len": len(fact.text)}
        for key, value in (("language", fact.language), ("confidence", fact.confidence),
                           ("duration_ms", fact.duration_ms), ("utterance_seq", fact.seq)):
            if value is not None:
                payload[key] = value
        draft = EventDraft(
            event_type="input.utterance_final", conversation_id=ctx.conversation_id, segment_id=ctx.segment_id,
            idempotency_key=f"es:{ctx.session_ref}:{fact.seq}", payload=payload,
            payload_blob=fact.text.encode("utf-8"),
        )
        trusted = TrustedContext(
            source=SOURCE_ECHO, actor_id=actor_id, scope_ref=ctx.event_scope, persona_id=self._persona,
            external=True,
        )
        recorded = self._publisher.publish(draft, trusted)
        if recorded.duplicate:
            return IntakeResult(None, "duplicate", duplicate=True, utterance_event_id=recorded.event.event_id)
        event = recorded.event
        auth = self._auth_provider()
        proposal = self._propose(ctx, fact, event.event_id, auth is not None)
        # Core 否決權：Presence 非 ACTIVE 時 reply_mode 一律強制為 pass（§4.3）
        if auth is None:
            self._skip(ctx, event.trace_id, event.event_id, fact, "presence_inactive", ReplyMode.PASS)
            return IntakeResult(None, "presence_inactive", proposal, utterance_event_id=event.event_id)
        if proposal.reply_mode in (ReplyMode.PASS, ReplyMode.DEFER):
            self._skip(ctx, event.trace_id, event.event_id, fact, proposal.reason_code, proposal.reply_mode)
            return IntakeResult(None, proposal.reason_code, proposal, utterance_event_id=event.event_id)
        turn_id = new_id("turn")
        generation = self.generations.begin(turn_id)
        ticket = CoreTicket(
            turn_id=turn_id, turn_generation=generation, source_turn_ref=fact.legacy_ref,
            conversation_id=ctx.conversation_id, segment_id=ctx.segment_id, audience_scope=ctx.audience_scope,
            event_scope=ctx.event_scope, reply_mode=proposal.reply_mode.value, auth=auth,
            valid_until=self._clock() + self._ttl, snapshot_version=self._snapshot_version(),
            speaker_ref=fact.speaker_ref, actor_id=actor_id, user_text=fact.text,
        )
        with self._lock:
            self._live[turn_id] = _Live(ticket, event.trace_id, event.event_id)
        self._emit(ticket, "turn.started", {"status": "started"})
        self._phase(InteractionState.THINKING, turn_id)
        return IntakeResult(ticket, "issued", proposal, utterance_event_id=event.event_id)

    def _propose(self, ctx: SessionContext, fact: UtteranceFact, event_id: str, active: bool) -> Proposal:
        view = None
        if self.state is not None:
            try:
                view = self.state.current(Axis.INTERACTION)
            except Exception:
                logger.exception("讀取 Interaction 軸失敗，proposer 看不到 interaction_state")
        candidate = TurnCandidate(
            candidate_id=f"{ctx.session_ref}:{fact.seq}", utterance_refs=(event_id,),
            speakers=(fact.speaker_ref,), ctx=ctx, interaction_state=view.state if view else None,
            interaction_version=view.version if view else None, presence_active=active,
        )
        try:
            proposal = self._proposer.propose(candidate)
            if not isinstance(proposal.reply_mode, ReplyMode):
                raise TypeError("reply_mode 不在列舉內")
            return proposal
        except Exception:
            # 例外回預設 Proposal（被點名或單人 -> respond）；模型輸出一律視為不受信，只接受列舉值
            logger.exception("TurnProposer 失敗，回預設 respond")
            return Proposal(reply_mode=ReplyMode.RESPOND, reason_code="proposer_error")

    def _skip(
        self, ctx: SessionContext, trace_id: str, cause_event_id: str, fact: UtteranceFact, reason: str,
        reply_mode: ReplyMode,
    ) -> None:
        draft = EventDraft(
            event_type="turn.skipped", conversation_id=ctx.conversation_id, segment_id=ctx.segment_id,
            trace_id=trace_id, causation_id=cause_event_id,
            idempotency_key=f"turn_skipped:{ctx.session_ref}:{fact.seq}",
            payload={"reason_code": reason, "reply_mode": reply_mode.value,
                     "proposal_ref": f"{ctx.session_ref}:{fact.seq}"},
        )
        trusted = TrustedContext(
            source=SOURCE_TURN, actor_id=ACTOR_TURN, scope_ref=ctx.event_scope, persona_id=self._persona
        )
        try:
            self._publisher.publish(draft, trusted)
        except Exception:
            logger.exception("turn.skipped 寫入失敗")

    # ---- ES 事實 ----

    def on_attempt_started(self, turn_id: str, attempt_no: int, reason_code: str = "regen") -> None:
        """attempt 2 以上（regen／fallback）才發；regen 不增加 turn_generation（§1.4 修改一）。"""
        live = self._get_live(turn_id)
        if live is None or live.ended or attempt_no < 2:
            return
        self._emit(
            live.ticket, "turn.attempt_started",
            {"attempt_id": attempt_label(attempt_no), "reason_code": reason_code},
            key=f"turn_attempt:{turn_id}:{attempt_no}",
        )

    def on_generation_completed(self, turn_id: str, length: int | None = None) -> bool:
        """每個 turn 至多一筆；回傳是否真的發出。沒進入生成的輪（busy、作廢、closed）ES 不會回報，也就不會發。"""
        with self._lock:
            live = self._live.get(turn_id)
            if live is None or live.ended or live.generation_emitted:
                return False
            live.generation_emitted = True
        payload: dict[str, object] = {"status": "completed"}
        if length is not None:
            payload["len"] = length
        self._emit(live.ticket, "turn.generation_completed", payload)
        return True

    def cancel_turn(self, turn_id: str, reason_code: str) -> bool:
        """Core 發起的取消（撤銷、全域停止等）：登記原因並讓 generation 前進，之後舊輸出一律被拒。

        ES 端的停止由轉接層另行呼叫；本方法冪等（重複呼叫不再前進），回傳是否為首次。
        """
        with self._lock:
            live = self._live.get(turn_id)
            if live is None or live.ended or live.cancel_reason is not None:
                return False
            live.cancel_reason = reason_code
            if reason_code != REASON_SHUTDOWN and not live.bumped:
                live.bumped = True
                self.generations.bump(turn_id)
        return True

    def on_turn_ended(self, turn_id: str, facts: TurnEndFacts) -> str | None:
        """每個 turn 恰好一個終結事件；回傳終結事件的 event_type（未知或已結束的 turn 回 None）。"""
        with self._lock:
            live = self._live.get(turn_id)
            if live is None or live.ended:
                return None
            live.ended = True
        ticket = live.ticket
        status, reason = facts.status, facts.reason_code
        if live.cancel_reason is not None and (
            status in _CORE_OVERRIDABLE or (status == "skipped" and reason in _OVERRIDABLE_SKIPS)
        ):
            status, reason = "cancelled", live.cancel_reason
        if status == "dropped_busy":
            kind = "turn.skipped"
            self._emit(ticket, kind, {"reason_code": "dropped_busy", "reply_mode": ticket.reply_mode})
        elif status in ("cancelled", "voided", "failed"):
            code = reason or ("upstream_error" if status == "failed" else status)
            self._bump_once(live, code)
            kind = "turn.cancelled"
            self._emit(ticket, kind, {"reason_code": code})
        elif status == "skipped":
            kind = "turn.completed"
            payload: dict[str, object] = {"status": "skipped"}
            if reason:
                payload["reason_code"] = reason
            self._emit(ticket, kind, payload)
        else:
            kind = "turn.completed"
            self._emit(ticket, kind, {"status": "completed"})
        with self._lock:
            self._live.pop(turn_id, None)
            self._finished[turn_id] = None
            while len(self._finished) > 1024:
                self._finished.popitem(last=False)
            idle = not self._live
        if idle:
            self._phase(InteractionState.IDLE, turn_id)
        if facts.generation_ok and self.registrar is not None:
            self.registrar.register(ticket)
        return kind

    # ---- 內部 ----

    def _bump_once(self, live: _Live, reason_code: str) -> None:
        with self._lock:
            if reason_code == REASON_SHUTDOWN or live.bumped:
                return
            live.bumped = True
        self.generations.bump(live.ticket.turn_id)

    def _get_live(self, turn_id: str) -> _Live | None:
        with self._lock:
            return self._live.get(turn_id)

    def _emit(
        self, ticket: CoreTicket, event_type: str, payload: dict[str, object], *, key: str | None = None
    ) -> None:
        live = self._get_live(ticket.turn_id)
        generation = self.generations.get(ticket.turn_id)
        draft = EventDraft(
            event_type=event_type, conversation_id=ticket.conversation_id, segment_id=ticket.segment_id,
            turn_id=ticket.turn_id,
            fence={"turn_generation": generation if generation is not None else ticket.turn_generation},
            trace_id=live.trace_id if live is not None else None,
            causation_id=live.utterance_event_id if live is not None else None,
            idempotency_key=key or f"{event_type}:{ticket.turn_id}", payload=payload,
        )
        trusted = TrustedContext(
            source=SOURCE_TURN, actor_id=ACTOR_TURN, scope_ref=ticket.event_scope, persona_id=self._persona
        )
        self._publisher.publish(draft, trusted)

    def _phase(self, target: InteractionState, turn_id: str) -> None:
        """向 CC-7 提 Interaction 軸 phase 請求（best-effort：被拒不重試、不影響回合）。"""
        if self.state is None:
            return
        try:
            view = self.state.current(Axis.INTERACTION)
            if view.state == target.value:
                return
            decision = self.state.request_transition(StateTransitionRequest(
                request_id=f"turn_{target.value.lower()}:{turn_id}", axis=Axis.INTERACTION.value,
                proposer=PHASE_PROPOSER, expected_version=view.version, from_state=view.state,
                to_state=target.value, reason={"source": "turn_orchestrator"},
            ))
            if decision.decision.value != "ACCEPTED":
                logger.info("Interaction 軸轉 %s 未被接受：%s", target.value, decision.reason_code)
        except Exception:
            logger.exception("Interaction 軸 phase 請求失敗")
