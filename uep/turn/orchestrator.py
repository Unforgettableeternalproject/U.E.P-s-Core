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

S5 決策插槽：utterance 先進 ``TurnAggregator``（預設逐句通過），完成的 turn 才進 proposer（在 Core 端的專屬
執行緒、預算內執行；逾時／例外／驗證失敗／被 CC-7 拒絕都回 D80 預設）；``Proposal.interaction_to`` 以 ``core.decision``
向 CC-7 提請求；shadow proposer 只記錄差異；``tick`` 處理 aggregator 逾時與被動模式插話。
S4 才做：Interaction 軸的 SPEAKING phase。
"""
from __future__ import annotations

import dataclasses
import logging
import re
import threading
import time
from collections import Counter, OrderedDict, deque
from collections.abc import Callable, Sequence
from concurrent.futures import Future
from concurrent.futures import wait as futures_wait
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from secrets import token_hex

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
from uep.turn.aggregator import (
    AggregatedTurn,
    PassThroughAggregator,
    Segment,
    TurnAggregator,
)
from uep.turn.context import ContextRegistrar
from uep.turn.decision_config import DecisionConfig
from uep.turn.generations import TurnGenerations
from uep.turn.models import (
    CoreTicket,
    IntakeResult,
    Proposal,
    ReplyMode,
    SessionContext,
    TurnCandidate,
    TurnEndFacts,
    TurnOutcomeFacts,
    UtteranceFact,
)
from uep.turn.proposer import (
    DECISION_ACTOR,
    Interjection,
    ListenerAware,
    ProposerGuard,
    RespondProposer,
    TickingProposer,
    TurnProposer,
    fallback_proposal,
    validate_proposal,
)
from uep.turn.wake import WakeDetector

logger = logging.getLogger("uep.turn.orchestrator")

SOURCE_TURN = "core.turn"
ACTOR_TURN = "uep:turn"
SOURCE_ECHO = "core.echo_host"
ACTOR_ECHO = "uep:echo_host"
UNVERIFIED_ACTOR = "uep:unverified_speaker"  # 映射表沒有的說話者：不得填成 owner（D77）
PHASE_PROPOSER = "core.turn"
DECISION_PROPOSER = DECISION_ACTOR  # 決策插槽向 CC-7 提請求的身分（只在 Interaction 軸）

REASON_SHUTDOWN = "shutdown"
# ES 只能標 shutdown（cancel_all）或 voice_cancelled（cancel_voice）：只有這兩種是「Core 要求停止」的回音，
# 才以 Core 登記的原因覆寫；barge_in、timeout、yielded 等 ES 自己判定的原因保留 ES 的值
_ES_ECHO_CANCEL_REASONS = frozenset({"shutdown"})
_ES_ECHO_SKIP_REASONS = frozenset({"voice_cancelled"})
# Core 因輸出被拒而取消的回合：ES 的 history 已有全文，不登記快照、不寫記憶
REJECTION_REASONS = frozenset({"admission_rejected", "dispatch_refused", "dispatch_failed"})
_SAFE_REF = re.compile(r"[A-Za-z0-9_:.\-]{1,128}")
_SAFE_LANGUAGE = re.compile(r"[A-Za-z]{2,3}(?:[-_][A-Za-z0-9]{1,8}){0,3}")
UNKNOWN_SPEAKER = "unknown:invalid"
_TERMINAL_RETRIES = 1


def attempt_label(attempt_no: int) -> str:
    """ES 的整數 attempt 對映 Core 的字串 attempt_id（OutputIntent.attempt_id 是 str）。"""
    return f"att_{attempt_no}"


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class _Live:
    ticket: CoreTicket
    trace_id: str | None
    utterance_event_id: str | None
    candidate_id: str = ""
    reason_code: str = ""  # Proposal 的原因碼（回饋給 proposer）
    speakers: tuple[str, ...] = ()  # 回應對象（插話為 buffer 內所有說話者）
    cancel_reason: str | None = None
    bumped: bool = False
    generation_emitted: bool = False
    ended: bool = False


@dataclass(frozen=True)
class _Intake:
    """Segment.payload：這句 utterance 的事實與已記錄的事件。"""

    fact: UtteranceFact
    event_id: str
    trace_id: str | None


@dataclass(frozen=True)
class ShadowRecord:
    """shadow proposer 與主 proposer 的比對（只在記憶體與 metrics，不寫 journal、不提 CC-7）。"""

    candidate_id: str
    shadow_id: str
    primary_mode: str
    primary_reason: str
    shadow_mode: str | None  # None = 例外或驗證失敗
    shadow_reason: str | None
    agree: bool
    status: str  # ok／error／late
    elapsed_ms: int


class TurnOrchestrator:
    def __init__(
        self,
        publisher: EventPublisher,
        generations: TurnGenerations,
        *,
        auth_provider: Callable[[], AuthorizationTuple | None],
        persona_id: str = "uep",
        proposer: TurnProposer | None = None,
        aggregator: TurnAggregator | None = None,
        shadow_proposers: Sequence[TurnProposer] = (),
        on_shadow: Callable[[ShadowRecord], None] | None = None,
        wake: WakeDetector | None = None,
        participants: Callable[[SessionContext], int | None] | None = None,
        decision: DecisionConfig | None = None,
        identity: IdentityDirectory | None = None,
        snapshot_version: Callable[[], int] = lambda: 0,
        ticket_ttl: timedelta = timedelta(minutes=10),
        clock: Callable[[], datetime] = _utcnow,
        registrar: ContextRegistrar | None = None,
        run_id: str | None = None,
    ) -> None:
        self._publisher = publisher
        # 此次宿主執行的識別：進冪等鍵，避免重啟後 session 內 seq 從頭來而撞鍵（外部傳入的 seq 也在同一命名空間）
        self.run_id = run_id or token_hex(4)
        self.metrics: Counter[str] = Counter()
        self.dead_letters: list[tuple[str, str, dict[str, object]]] = []
        self.generations = generations
        self._auth_provider = auth_provider
        self._persona = persona_id
        self._proposer = proposer or RespondProposer()
        self._aggregator: TurnAggregator = aggregator or PassThroughAggregator()
        self._wake = wake or WakeDetector()
        self._participants = participants
        self._decision = decision or DecisionConfig()
        budget = self._decision.proposer_budget_ms
        self._budget_s: float | None = budget / 1000.0 if budget > 0 else None
        self._guard = ProposerGuard("primary")
        self._shadows = [(sp, ProposerGuard(f"shadow{i}")) for i, sp in enumerate(shadow_proposers)]
        self._on_shadow = on_shadow
        self.shadow_log: deque[ShadowRecord] = deque(maxlen=256)
        self._shadow_pending: set[Future[object]] = set()
        self._identity = identity
        self._snapshot_version = snapshot_version
        self._ttl = ticket_ttl
        self._clock = clock
        self.registrar = registrar
        self.state: StateCoordinator | None = None
        self._lock = threading.Lock()
        self._live: dict[str, _Live] = {}
        self._finished: OrderedDict[str, None] = OrderedDict()
        self._ctx: SessionContext | None = None  # tick 完成的 turn 用最近一次看到的 session
        self._tick_seq = 0

    def bind_state(self, state: StateCoordinator) -> None:
        self.state = state

    def close(self) -> None:
        """關閉決策執行緒（卡住的 proposer 執行緒無法終止，只放棄其結果）。"""
        self._guard.close()
        for _, guard in self._shadows:
            guard.close()

    @property
    def proposer(self) -> TurnProposer:
        return self._proposer

    @property
    def aggregator(self) -> TurnAggregator:
        return self._aggregator

    # ---- 觀測 ----

    def live_tickets(self) -> list[CoreTicket]:
        with self._lock:
            return [live.ticket for live in self._live.values() if not live.ended]

    def cancel_reason_of(self, turn_id: str) -> str | None:
        with self._lock:
            live = self._live.get(turn_id)
            return live.cancel_reason if live is not None else None

    def ticket_of(self, turn_id: str) -> CoreTicket | None:
        with self._lock:
            live = self._live.get(turn_id)
            return live.ticket if live is not None else None

    def wait_shadow(self, timeout: float | None = None) -> bool:
        """等所有進行中的 shadow 比對完成（測試與診斷用）；回傳是否全部完成。"""
        with self._lock:
            pending = set(self._shadow_pending)
        if not pending:
            return True
        _done, not_done = futures_wait(pending, timeout=timeout)
        return not not_done

    def clear_aggregation(self) -> None:
        """audience 切換：丟棄 aggregator 內尚未成 turn 的緩衝（utterance 已在 journal，不再決策）。"""
        self._aggregator.flush_all()

    # ---- 輸入 ----

    def on_utterance(self, ctx: SessionContext, fact: UtteranceFact) -> IntakeResult:
        """記錄 utterance、交給 aggregator；完成的 turn 跑決策插槽、簽 ticket 並落 ``turn.started``。

        utterance 還在 aggregator 等待合併時回 ``reason_code="buffered"``（ticket=None）。
        """
        fact = self._sanitize(fact)
        ident = self._identity.resolve_platform(fact.speaker_ref) if self._identity is not None else GUEST
        actor_id = ident.actor_id or UNVERIFIED_ACTOR
        payload: dict[str, object] = {"speaker_ref": fact.speaker_ref, "len": len(fact.text)}
        for key, value in (("language", fact.language), ("confidence", fact.confidence),
                           ("duration_ms", fact.duration_ms), ("utterance_seq", fact.seq)):
            if value is not None:
                payload[key] = value
        draft = EventDraft(
            event_type="input.utterance_final", conversation_id=ctx.conversation_id, segment_id=ctx.segment_id,
            idempotency_key=f"es:{self.run_id}:{ctx.session_ref}:{fact.seq}", payload=payload,
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
        self._ctx = ctx
        yield_for = self._listener_yield(fact)
        now = time.monotonic()
        segment = Segment(
            speaker_ref=fact.speaker_ref, text=fact.text, ref=event.event_id,
            start_ts=now - (fact.duration_ms or 0) / 1000.0, end_ts=now,
            payload=_Intake(fact, event.event_id, event.trace_id),
        )
        try:
            completed = self._aggregator.feed(segment)
        except Exception:
            # aggregator 壞掉不能吞掉 utterance：退回「一句一個 turn」
            logger.exception("TurnAggregator 失敗，退回逐句處理")
            self.metrics["aggregator.error"] += 1
            completed = [AggregatedTurn(fact.speaker_ref, fact.text, (segment,))]
        results = [self._decide_turn(ctx, turn) for turn in completed]
        if not results:
            return IntakeResult(None, "buffered", utterance_event_id=event.event_id, yield_for=yield_for)
        return dataclasses.replace(results[0], extra=tuple(results[1:]), yield_for=yield_for)

    def tick(self) -> list[IntakeResult]:
        """宿主定期呼叫（不可在 ES loop 上，會寫 DB）：aggregator 的等待逾時與被動模式插話。"""
        ctx = self._ctx
        if ctx is None:
            return []
        results: list[IntakeResult] = []
        try:
            turns = self._aggregator.tick()
        except Exception:
            logger.exception("TurnAggregator.tick 失敗")
            self.metrics["aggregator.error"] += 1
            turns = []
        results.extend(self._decide_turn(ctx, turn) for turn in turns)
        proposer = self._proposer
        if isinstance(proposer, TickingProposer):
            interjections, status = self._guard.call(lambda _: proposer.tick(), None, self._budget_s)
            if status != "ok":
                self.metrics[f"proposer.tick_{status}"] += 1
            for ij in interjections or ():
                results.append(self._decide_interjection(ctx, ij))
        return results

    def _listener_yield(self, fact: UtteranceFact) -> str | None:
        """主動模式下同一人又講了有意義的新內容：該說話者尚未開播的輪讓位（bot 的 ``listener_new_segment``）。"""
        proposer = self._proposer
        if not isinstance(proposer, ListenerAware):
            return None
        try:
            return fact.speaker_ref if proposer.is_listener_continuation(fact.text) else None
        except Exception:
            logger.exception("ListenerAware 判斷失敗")
            return None

    def _decide_turn(self, ctx: SessionContext, turn: AggregatedTurn) -> IntakeResult:
        intake = turn.last.payload
        if not isinstance(intake, _Intake):  # aggregator 實作沒有保留 payload：視為內部錯誤，丟棄這個 turn
            self.metrics["aggregator.lost_payload"] += 1
            return IntakeResult(None, "aggregator_error")
        fact = intake.fact if len(turn.segments) == 1 else dataclasses.replace(intake.fact, text=turn.text)
        ident = self._identity.resolve_platform(fact.speaker_ref) if self._identity is not None else GUEST
        actor_id = ident.actor_id or UNVERIFIED_ACTOR
        auth = self._auth_provider()
        candidate = self._candidate(ctx, fact, turn.refs or (intake.event_id,), len(turn.segments), auth is not None)
        proposal = self._propose(candidate)
        # Core 否決權：Presence 非 ACTIVE 時 reply_mode 一律強制為 pass（§4.3）
        if auth is None:
            self._skip(ctx, intake.trace_id, intake.event_id, fact, "presence_inactive", ReplyMode.PASS)
            return IntakeResult(None, "presence_inactive", proposal, utterance_event_id=intake.event_id, fact=fact)
        proposal = self._apply_interaction(candidate, proposal)
        if proposal.reply_mode in (ReplyMode.PASS, ReplyMode.DEFER):
            self._skip(ctx, intake.trace_id, intake.event_id, fact, proposal.reason_code, proposal.reply_mode)
            return IntakeResult(None, proposal.reason_code, proposal, utterance_event_id=intake.event_id, fact=fact)
        return self._start(ctx, fact, intake.event_id, intake.trace_id, candidate.candidate_id, proposal, auth, actor_id)

    def _decide_interjection(self, ctx: SessionContext, ij: Interjection) -> IntakeResult:
        """沒有新 utterance 觸發的決策（被動模式插話）：以 buffer 最後一句為代表，沒有 trace 可延續。"""
        self._tick_seq += 1
        fact = UtteranceFact(speaker_ref=ij.speaker_ref, text=ij.text, seq=self._tick_seq)
        ident = self._identity.resolve_platform(ij.speaker_ref) if self._identity is not None else GUEST
        actor_id = ident.actor_id or UNVERIFIED_ACTOR
        auth = self._auth_provider()
        proposal = ij.proposal
        bad = validate_proposal(proposal)
        if bad is not None or auth is None or proposal.reply_mode in (ReplyMode.PASS, ReplyMode.DEFER):
            self.metrics["decision.interjection_dropped"] += 1
            reason = "presence_inactive" if auth is None else (bad or proposal.reason_code)
            return IntakeResult(None, reason, proposal, fact=fact)
        cause = ij.utterance_refs[-1] if ij.utterance_refs else None
        candidate_id = f"tick:{self.run_id}:{ctx.session_ref}:{self._tick_seq}"
        return self._start(
            ctx, fact, cause, None, candidate_id, proposal, auth, actor_id, ij.speakers or (ij.speaker_ref,)
        )

    def _candidate(
        self, ctx: SessionContext, fact: UtteranceFact, refs: tuple[str, ...], segments: int, active: bool
    ) -> TurnCandidate:
        view = None
        if self.state is not None:
            try:
                view = self.state.current(Axis.INTERACTION)
            except Exception:
                logger.exception("讀取 Interaction 軸失敗，proposer 看不到 interaction_state")
        count: int | None = None
        if self._participants is not None:
            try:
                count = self._participants(ctx)
            except Exception:
                logger.exception("讀取在場人數失敗")
        addressed = self._wake.is_called(fact.text)
        deadline = None if self._budget_s is None else time.monotonic() + self._budget_s
        return TurnCandidate(
            candidate_id=f"{self.run_id}:{ctx.session_ref}:{fact.seq}", utterance_refs=refs,
            speakers=(fact.speaker_ref,), ctx=ctx, interaction_state=view.state if view else None,
            interaction_version=view.version if view else None, presence_active=active, text=fact.text,
            segments=segments, participant_count=count, addressed_hint=addressed, deadline=deadline,
        )

    def _start(
        self, ctx: SessionContext, fact: UtteranceFact, event_id: str | None, trace_id: str | None,
        candidate_id: str, proposal: Proposal, auth: AuthorizationTuple, actor_id: str,
        speakers: tuple[str, ...] = (),
    ) -> IntakeResult:
        turn_id = new_id("turn")
        generation = self.generations.begin(turn_id)
        try:
            result = self._issue(
                ctx, fact, event_id, trace_id, candidate_id, proposal, auth, actor_id, turn_id, generation,
                speakers or (fact.speaker_ref,),
            )
        except BaseException:
            # turn.started 寫入失敗：回收 live 與 generation，不留下沒有 started 事件的孤兒 turn
            with self._lock:
                self._live.pop(turn_id, None)
            self.generations.discard(turn_id)
            self.metrics["turn.issue_failed"] += 1
            raise
        self._notify_started(result.ticket, candidate_id, speakers or (fact.speaker_ref,))
        return result

    def _issue(
        self, ctx, fact, event_id, trace_id, candidate_id, proposal, auth, actor_id, turn_id, generation, speakers
    ) -> IntakeResult:
        ticket = CoreTicket(
            turn_id=turn_id, turn_generation=generation, source_turn_ref=fact.legacy_ref,
            conversation_id=ctx.conversation_id, segment_id=ctx.segment_id, audience_scope=ctx.audience_scope,
            event_scope=ctx.event_scope, reply_mode=proposal.reply_mode.value, auth=auth,
            valid_until=self._clock() + self._ttl, snapshot_version=self._snapshot_version(),
            speaker_ref=fact.speaker_ref, actor_id=actor_id, user_text=fact.text,
        )
        with self._lock:
            self._live[turn_id] = _Live(ticket, trace_id, event_id, candidate_id, proposal.reason_code, speakers)
        self._emit(ticket, "turn.started", {"status": "started"})
        self._phase(InteractionState.THINKING, turn_id)
        return IntakeResult(ticket, "issued", proposal, utterance_event_id=event_id, fact=fact)

    def _notify_started(self, ticket: CoreTicket | None, candidate_id: str, speakers: tuple[str, ...]) -> None:
        hook = getattr(self._proposer, "on_turn_started", None)
        if ticket is None or not callable(hook):
            return
        try:
            hook(ticket.turn_id, candidate_id, speakers)
        except Exception:
            logger.exception("OutcomeObserver.on_turn_started 失敗")

    def _sanitize(self, fact: UtteranceFact) -> UtteranceFact:
        """speaker_ref、language 是不受信資料：長度與字元集不符就丟棄（改成安全預設）並計數。"""
        speaker, language = fact.speaker_ref, fact.language
        if not isinstance(speaker, str) or not _SAFE_REF.fullmatch(speaker):
            self.metrics["intake.invalid_speaker_ref"] += 1
            speaker = UNKNOWN_SPEAKER
        if language is not None and (not isinstance(language, str) or not _SAFE_LANGUAGE.fullmatch(language)):
            self.metrics["intake.invalid_language"] += 1
            language = None
        if speaker is fact.speaker_ref and language is fact.language:
            return fact
        return dataclasses.replace(fact, speaker_ref=speaker, language=language)

    def _propose(self, candidate: TurnCandidate) -> Proposal:
        """在 Core 端、預算內執行 proposer；逾時、例外、驗證失敗一律回 D80 預設並計 telemetry。

        模型輸出視為不受信資料：只接受 schema 內的列舉與安全字元集（``validate_proposal``）。
        """
        proposal, status = self._guard.call(self._proposer.propose, candidate, self._budget_s)
        if status == "timeout":
            self.metrics["proposer.timeout"] += 1
            result = fallback_proposal(candidate, "proposer_timeout")
        elif status != "ok":
            self.metrics["proposer.error"] += 1
            result = fallback_proposal(candidate, "proposer_error")
        else:
            bad = validate_proposal(proposal)
            if bad is None:
                result = proposal
            else:
                logger.warning("Proposal 驗證失敗（%s），回預設", bad)
                self.metrics["proposer.invalid"] += 1
                result = fallback_proposal(candidate, "proposal_invalid")
        self._run_shadows(candidate, result)
        return result

    def _apply_interaction(self, candidate: TurnCandidate, proposal: Proposal) -> Proposal:
        """Proposal.interaction_to 以 ``core.decision`` 向 CC-7 提請求（只在 Interaction 軸）。

        被拒（版本衝突、不合法轉換、未授權…）一律回 D80 預設，不重試。沒有綁定 State Coordinator 時沒有地方可請求，忽略。
        """
        target = proposal.interaction_to
        if target is None or self.state is None:
            return proposal
        if candidate.interaction_state is None or candidate.interaction_version is None:
            return fallback_proposal(candidate, "proposal_rejected")  # 讀不到軸：沒有 expected_version 可帶
        try:
            decision = self.state.request_transition(StateTransitionRequest(
                request_id=f"decision_{target.lower()}:{candidate.candidate_id}", axis=Axis.INTERACTION.value,
                proposer=DECISION_PROPOSER, expected_version=candidate.interaction_version,
                from_state=candidate.interaction_state, to_state=target,
                reason={"source": "decision_slot", "reply_mode": proposal.reply_mode.value,
                        "reason_code": proposal.reason_code},
            ))
        except Exception:
            logger.exception("決策插槽的 Interaction 請求失敗")
            self.metrics["decision.cc7_error"] += 1
            return fallback_proposal(candidate, "proposal_rejected")
        if decision.decision.value != "ACCEPTED":
            self.metrics["decision.cc7_rejected"] += 1
            logger.info("Interaction 請求被拒（%s），回預設", decision.reason_code)
            return fallback_proposal(candidate, "proposal_rejected")
        self.metrics["decision.cc7_accepted"] += 1
        return proposal

    # ---- shadow mode ----

    def _run_shadows(self, candidate: TurnCandidate, primary: Proposal) -> None:
        """shadow proposer 與主決策同候選並行執行，只記錄差異：不寫 journal、不提 CC-7、不影響 ticket。"""
        for shadow, guard in self._shadows:
            started = time.monotonic()
            try:
                future = guard.submit(shadow.propose, candidate)
            except RuntimeError:
                continue
            with self._lock:
                self._shadow_pending.add(future)
            future.add_done_callback(
                lambda f, g=shadow, c=candidate, p=primary, t=started: self._record_shadow(f, g, c, p, t)
            )

    def _record_shadow(
        self, future: Future[object], shadow: TurnProposer, candidate: TurnCandidate, primary: Proposal, started: float
    ) -> None:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        shadow_id = getattr(shadow, "proposer_id", type(shadow).__name__)
        mode: str | None = None
        reason: str | None = None
        status = "ok"
        try:
            result = future.result()
            if validate_proposal(result) is not None:
                status = "error"
            else:
                assert isinstance(result, Proposal)
                mode, reason = result.reply_mode.value, result.reason_code
                if result.proposer_id != "unknown":
                    shadow_id = result.proposer_id
                if self._budget_s is not None and elapsed_ms > self._budget_s * 1000:
                    status = "late"
        except BaseException:  # noqa: BLE001 - shadow 的任何失敗都只記錄
            status = "error"
        record = ShadowRecord(
            candidate_id=candidate.candidate_id, shadow_id=str(shadow_id)[:64],
            primary_mode=primary.reply_mode.value, primary_reason=primary.reason_code,
            shadow_mode=mode, shadow_reason=reason, agree=mode == primary.reply_mode.value, status=status,
            elapsed_ms=elapsed_ms,
        )
        self.shadow_log.append(record)
        self.metrics[f"shadow.{'agree' if record.agree else 'disagree'}"] += 1
        if status != "ok":
            self.metrics[f"shadow.{status}"] += 1
        if self._on_shadow is not None:
            try:
                self._on_shadow(record)
            except Exception:
                logger.exception("on_shadow 回呼失敗")
        with self._lock:
            self._shadow_pending.discard(future)

    def _skip(
        self, ctx: SessionContext, trace_id: str | None, cause_event_id: str | None, fact: UtteranceFact,
        reason: str, reply_mode: ReplyMode,
    ) -> None:
        draft = EventDraft(
            event_type="turn.skipped", conversation_id=ctx.conversation_id, segment_id=ctx.segment_id,
            trace_id=trace_id, causation_id=cause_event_id,
            idempotency_key=f"turn_skipped:{self.run_id}:{ctx.session_ref}:{fact.seq}",
            payload={"reason_code": reason, "reply_mode": reply_mode.value,
                     "proposal_ref": f"{self.run_id}:{ctx.session_ref}:{fact.seq}"},
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
        kind: str | None = None
        try:
            status, reason = facts.status, facts.reason_code
            if live.cancel_reason is not None and (
                (status in ("cancelled", "voided") and reason in _ES_ECHO_CANCEL_REASONS)
                or (status == "skipped" and reason in _ES_ECHO_SKIP_REASONS)
            ):
                status, reason = "cancelled", live.cancel_reason
            if status == "dropped_busy":
                kind, payload = "turn.skipped", {"reason_code": "dropped_busy", "reply_mode": ticket.reply_mode}
            elif status in ("cancelled", "voided", "failed"):
                code = reason or ("upstream_error" if status == "failed" else status)
                self._bump_once(live, code)
                kind, payload = "turn.cancelled", {"reason_code": code}
            elif status == "skipped":
                kind, payload = "turn.completed", {"status": "skipped"}
                if reason:
                    payload["reason_code"] = reason
            else:
                kind, payload = "turn.completed", {"status": "completed"}
            self._emit_terminal(ticket, kind, payload)
            final_status, final_reason = status, reason
        finally:
            # 寫入失敗也要回收狀態：live、generation 的 pin 與 Interaction 軸 IDLE 都不能留著
            with self._lock:
                self._live.pop(turn_id, None)
                self._finished[turn_id] = None
                while len(self._finished) > 1024:
                    self._finished.popitem(last=False)
                idle = not self._live
            self.generations.finish(turn_id)
            if idle:
                self._phase(InteractionState.IDLE, turn_id)
        if (
            facts.generation_ok and self.registrar is not None and live.cancel_reason not in REJECTION_REASONS
        ):
            self.registrar.register(ticket)
        self._notify_outcome(live, final_status, final_reason, facts)
        return kind

    def _notify_outcome(self, live: _Live, status: str, reason: str | None, facts: TurnEndFacts) -> None:
        """回合結果回饋給 proposer（選用 ``OutcomeObserver``）：Q-1 的插話冷卻讀 ``barge_in``。"""
        hook = getattr(self._proposer, "on_turn_outcome", None)
        if not callable(hook):
            return
        try:
            hook(TurnOutcomeFacts(
                turn_id=live.ticket.turn_id, candidate_id=live.candidate_id,
                speakers=live.speakers or (live.ticket.speaker_ref,),
                reply_mode=live.ticket.reply_mode, reason_code=live.reason_code, status=status,
                end_reason=reason, generation_ok=facts.generation_ok, barge_in=facts.barge_in,
            ))
        except Exception:
            logger.exception("OutcomeObserver.on_turn_outcome 失敗")

    def _emit_terminal(self, ticket: CoreTicket, kind: str, payload: dict[str, object]) -> None:
        """終結事件寫入：失敗重試，仍失敗就 dead-letter（保留在記憶體並計數，不只記 log）。"""
        last: Exception | None = None
        for _attempt in range(_TERMINAL_RETRIES + 1):
            try:
                self._emit(ticket, kind, payload)
                return
            except Exception as exc:  # noqa: BLE001 - 任何寫入失敗都要走重試與 dead-letter
                last = exc
                self.metrics["turn.terminal_retry"] += 1
        self.metrics["turn.terminal_dead_letter"] += 1
        self.dead_letters.append((ticket.turn_id, kind, dict(payload)))
        logger.error("終結事件寫入失敗，已 dead-letter：%s %s", kind, ticket.turn_id, exc_info=last)

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
