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
Core 自己發起的取消（``cancel_turn``）：S4 起轉接層以 ES 的 ``cancel_turn(turn_id, reason_code=...)`` 停輪，
Core 的原因碼直接隨 ``outcome.metadata["reason_code"]`` 回來，這裡不再覆寫 ES 回報的終結原因（S3 的覆寫繞道已拿掉）；
已結束的輪不被事後改寫。

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
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from secrets import token_hex

from uep.contracts.presence import AuthorizationTuple, PresenceLocation
from uep.decision.contracts import DecisionRequest
from uep.decision.hub import DecisionHub
from uep.decision.presence_absent_intent import (
    AbsentIntentEngine,
    AbsentIntentInput,
    judge_absent_intent,
    presence_absent_intent_spec,
    presence_request_from_result,
)
from uep.decision.turn_interject import TurnInterjectEngine, turn_interject_spec
from uep.decision.turn_reply import (
    KIND_TURN_REPLY,
    ShadowRecord,
    TurnReplyEngine,
    result_to_proposal,
    turn_reply_spec,
)
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
    Degraded,
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
    AudienceAware,
    Interjection,
    ListenerAware,
    ParticipantAware,
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
# Core 因輸出被拒而取消的回合：不登記快照、不寫記憶
REJECTION_REASONS = frozenset({"admission_rejected", "dispatch_refused", "dispatch_failed"})
# 整輪撤回歷史的原因（D5、D82）：被拒、撤銷、停止一律以 ES cancel_turn(commit="none") 整輪撤出歷史，
# 不受 D82 的 spoken-only 開關影響（開關只管讓位與插話）
RETRACT_REASONS = REJECTION_REASONS | frozenset({"presence_lost", "global_stop", "control_stop"})
_SAFE_REF = re.compile(r"[A-Za-z0-9_:.\-]{1,128}")
_SAFE_ACTOR_REF = re.compile(r"[a-z0-9_.:\-]{1,128}")
_SAFE_LANGUAGE = re.compile(r"[A-Za-z]{2,3}(?:[-_][A-Za-z0-9]{1,8}){0,3}")
UNKNOWN_SPEAKER = "unknown:invalid"
INVALID_SOURCE_LOCATION = "invalid"  # _sanitize 對無效 source_location 的標記：視為不一致（不是缺失）


def no_presence_request(fact: UtteranceFact) -> bool | None:
    """明確關閉意圖判斷的 hook：固定回 None（未判斷）。

    預設（``presence_request_hook=None``）改由核心決策點 ``presence.absent_intent`` 判斷意圖（可用決策模型，
    不另外呼叫 LLM）；意圖只寫進 shadow 紀錄，授權只來自已認證 actor 與規則。需要完全不判斷時可傳入本函式。
    """
    return None


REASON_ABSENT_PLATFORM_INPUT = "absent_platform_input"
REASON_SOURCE_LOCATION_MISSING = "source_location_missing"
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
    think_started: bool = False  # ES 真的開始 think 過（OutcomeObserver.on_turn_started 已送出）
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
        veto: Callable[[SessionContext], str | None] | None = None,
        decision: DecisionConfig | None = None,
        decision_hub: DecisionHub | None = None,
        identity: IdentityDirectory | None = None,
        snapshot_version: Callable[[], int] = lambda: 0,
        ticket_ttl: timedelta = timedelta(minutes=10),
        clock: Callable[[], datetime] = _utcnow,
        registrar: ContextRegistrar | None = None,
        run_id: str | None = None,
        host_location: str | None = None,
        require_source_location: bool = False,
        presence_request_hook: Callable[[UtteranceFact], bool | None] | None = None,
    ) -> None:
        self._publisher = publisher
        # V18：本機 Host 持有的 location（比對對象）；None = 未提供，不做來源比對（獨立測試用）
        self._host_location = host_location
        self.require_source_location = require_source_location
        # None = 預設走核心決策點 presence.absent_intent（見 _presence_request）；傳入 callable 則完全取代
        self._presence_request_hook = presence_request_hook
        # 此次宿主執行的識別：進冪等鍵，避免重啟後 session 內 seq 從頭來而撞鍵（外部傳入的 seq 也在同一命名空間）
        self.run_id = run_id or token_hex(4)
        self.metrics: Counter[str] = Counter()
        self.dead_letters: list[tuple[str, str, dict[str, object]]] = []
        self.generations = generations
        self._auth_provider = auth_provider
        self._persona = persona_id
        self._aggregator: TurnAggregator = aggregator or PassThroughAggregator()
        self._wake = wake or WakeDetector()
        self._participants = participants
        self._veto = veto
        self._decision = decision or DecisionConfig()
        # DL-1 P0：primary／shadow proposer、guard、預算只存在於中樞的 turn.reply engine（單一份狀態）；
        # Orchestrator 的 _proposer／_budget_s／_guard／_shadows／shadow_log／_on_shadow 都是委派到 engine 的 property
        # 預算與各 fallback reason 只在 DecisionPointSpec（registry）一份；engine 與 _budget_s 都讀寫它。
        # decision_hub 可注入（共用中樞）；已註冊 turn.reply 時 register 報 ValueError（不冪等，避免兩個 engine 搶同一決策點）
        self.hub = decision_hub if decision_hub is not None else DecisionHub()
        self._reply_engine = TurnReplyEngine(
            proposer or RespondProposer(), registry=self.hub.registry, metrics=self.metrics,
            shadow_proposers=shadow_proposers, on_shadow=on_shadow,
        )
        try:
            self.hub.register(turn_reply_spec(self._decision.proposer_budget_ms), self._reply_engine)
        except BaseException:
            self._reply_engine.close()
            raise
        # DL-P1：turn.interject（被動模式插話）。primary 的 tick 仍由下方 tick() 直接呼叫（零變化），
        # engine 只負責把結果轉成 DecisionResult 記錄，並對 shadow proposer 並行 tick 比對
        self._interject_engine = TurnInterjectEngine(
            self._reply_engine, registry=self.hub.registry, metrics=self.metrics, submit=self.hub.submit,
            run_id=self.run_id,
        )
        try:
            self.hub.register(turn_interject_spec(self._decision.proposer_budget_ms), self._interject_engine)
        except BaseException:
            self._reply_engine.close()
            raise
        # V18 後續：presence.absent_intent（缺席端輸入的意圖判斷，sync、一律 shadow；proposer 可經 registry 替換）
        self._absent_intent_engine = AbsentIntentEngine(
            spec=presence_absent_intent_spec(self._decision.proposer_budget_ms)
        )
        try:
            self.hub.register(self._absent_intent_engine.spec, self._absent_intent_engine)
        except BaseException:
            self._reply_engine.close()
            raise
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
        # route=agent_loop 的掛點（切片 D1）：只有 UEP_TOOL_PATH=agent 的組裝層會綁定；None（預設）時 route 不被消費，
        # agent_loop 與 direct_chat 行為完全相同（legacy）
        self._loop_entry: Callable[[CoreTicket, Proposal], object] | None = None
        # 已被 AgentLoop 認領的 turn（入口回傳非 None）：宿主不得再為它啟動 direct_chat 回覆（避免雙重回覆）。
        # 認領在呼叫入口「之前」登記（Run 的 worker 可能在入口返回前就結束 turn），失敗時撤銷
        self._loop_claims: OrderedDict[str, None] = OrderedDict()

    # ---- turn.reply engine 的委派屬性（重設 _proposer 等仍立即生效，如包裝前）----

    @property
    def _proposer(self) -> TurnProposer:
        return self._reply_engine.proposer

    @_proposer.setter
    def _proposer(self, value: TurnProposer) -> None:
        self._reply_engine.proposer = value

    @property
    def _budget_s(self) -> float | None:
        return self._reply_engine.budget_s

    @_budget_s.setter
    def _budget_s(self, value: float | None) -> None:
        self._reply_engine.budget_s = value

    @property
    def _guard(self) -> ProposerGuard:
        return self._reply_engine.guard

    @_guard.setter
    def _guard(self, value: ProposerGuard) -> None:
        self._reply_engine.guard = value

    @property
    def _shadows(self) -> list[tuple[TurnProposer, ProposerGuard]]:
        return self._reply_engine.shadows

    @_shadows.setter
    def _shadows(self, value: list[tuple[TurnProposer, ProposerGuard]]) -> None:
        self._reply_engine.shadows = value

    @property
    def shadow_log(self) -> deque[ShadowRecord]:
        return self._reply_engine.shadow_log

    @shadow_log.setter
    def shadow_log(self, value: deque[ShadowRecord]) -> None:
        self._reply_engine.shadow_log = value

    @property
    def _on_shadow(self) -> Callable[[ShadowRecord], None] | None:
        return self._reply_engine.on_shadow

    @_on_shadow.setter
    def _on_shadow(self, value: Callable[[ShadowRecord], None] | None) -> None:
        self._reply_engine.on_shadow = value

    def bind_loop_entry(self, entry: Callable[[CoreTicket, Proposal], object] | None) -> None:
        """綁定 ``route=agent_loop`` 的 LoopTrigger 入口（只由 agent 工具路徑的組裝層呼叫）。入口須自行非同步化，不得阻塞 turn。"""
        self._loop_entry = entry

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
        return self._reply_engine.wait_shadow(timeout) and self._interject_engine.wait_shadow(timeout)

    def clear_aggregation(self) -> None:
        """audience 切換：丟棄 aggregator 內尚未成 turn 的緩衝，並讓 proposer 清掉它的狀態
        （buffer、context window、生成中標記）；之後 tick 在下一句 utterance 之前不產生任何決策。"""
        self._aggregator.flush_all()
        self._ctx = None
        for proposer in (self._proposer, *(sp for sp, _ in self._shadows)):
            if isinstance(proposer, AudienceAware):
                try:
                    proposer.on_audience_changed()
                except Exception:
                    logger.exception("on_audience_changed 失敗")

    def on_participant_left(self, speaker_ref: str) -> None:
        """成員離場：清掉他留在 aggregator 與 proposer 的文字。"""
        discard = getattr(self._aggregator, "discard_speaker", None)
        if callable(discard):
            discard(speaker_ref)
        for proposer in (self._proposer, *(sp for sp, _ in self._shadows)):
            if isinstance(proposer, ParticipantAware):
                try:
                    proposer.on_participant_left(speaker_ref)
                except Exception:
                    logger.exception("on_participant_left 失敗")

    # ---- 輸入 ----

    def on_utterance(self, ctx: SessionContext, fact: UtteranceFact) -> IntakeResult:
        """記錄 utterance、交給 aggregator；完成的 turn 跑決策插槽、簽 ticket 並落 ``turn.started``。

        utterance 還在 aggregator 等待合併時回 ``reason_code="buffered"``（ticket=None）。
        """
        fact = self._sanitize(fact)
        absent = self._absent_input_reason(fact)
        if absent is not None:
            # V18：缺席端輸入在 _listener_yield／aggregator 之前丟棄，proposer 與 buffer 都看不到文字；
            # 連 input.utterance_final（含原文 blob）都不寫，只留不含原文的 shadow 紀錄
            self._record_absent_input(ctx, fact, absent)
            return IntakeResult(None, absent, None, fact=dataclasses.replace(fact, text=""))
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
            payload=_Intake(fact, event.event_id, event.trace_id), voice=fact.from_voice,
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

    def tick(self, session: SessionContext | None = None) -> list[IntakeResult]:
        """宿主定期呼叫（不可在 ES loop 上，會寫 DB）：aggregator 的等待逾時與被動模式插話。

        ``session`` 為宿主目前的 session：與最後一次看到 utterance 的 session 不符就直接丟棄（audience 已切換）。"""
        ctx = self._ctx
        if ctx is None:
            return []
        if session is not None and session.session_ref != ctx.session_ref:
            self._aggregator.flush_all()
            self._ctx = None
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
        interjections: list[Interjection] | None = None
        status = "ok"
        started = time.perf_counter()
        if isinstance(proposer, TickingProposer):
            interjections, status = self._guard.call(lambda _: proposer.tick(), None, self._budget_s)
            if status != "ok":
                self.metrics[f"proposer.tick_{status}"] += 1
        self._observe_interject(proposer, interjections, status, started)
        for ij in interjections or ():
            results.append(self._decide_interjection(ctx, ij))
        return results

    def _observe_interject(
        self, proposer: TurnProposer, interjections: list[Interjection] | None, status: str, started: float
    ) -> None:
        """DL-P1：把這一輪 primary tick 交給 turn.interject engine 記錄並啟動 shadow tick；任何失敗都不影響插話。"""
        try:
            self._interject_engine.on_tick(
                interjections, status, int((time.perf_counter() - started) * 1000),
                primary_id=str(getattr(proposer, "proposer_id", type(proposer).__name__))[:64],
                primary_version=str(getattr(proposer, "version", "unknown"))[:64],
            )
        except Exception:
            self.metrics["interject.engine_error"] += 1
            logger.exception("turn.interject 記錄失敗，不影響插話")

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

    def _absent_input_reason(self, fact: UtteranceFact) -> str | None:
        """缺席端輸入判定（只看位置）：Host 不持有 Lease、或 source_location 與 Host location 不一致。

        缺失（None）維持現狀放行並計數；``require_source_location`` 開啟後缺失即否決。
        優先序：presence_inactive 先於來源比對（她在主機時任何輸入都不開 turn）。
        """
        try:
            auth = self._auth_provider()
        except Exception:
            logger.exception("auth_provider 失敗，fail-closed")
            return "presence_inactive"
        if auth is None:
            return "presence_inactive"
        source = fact.source_location
        if source is None:
            self.metrics["intake.source_location_missing"] += 1
            return REASON_SOURCE_LOCATION_MISSING if self.require_source_location else None
        if self._host_location is not None and str(source) != self._host_location:
            return REASON_ABSENT_PLATFORM_INPUT
        return None

    def _presence_request(
        self, ref: str, fact: UtteranceFact, actor_id: str, actor_authenticated: bool, is_owner: str,
    ) -> bool | None:
        """缺席端輸入是否為到場／移轉請求（只寫 shadow 紀錄，不觸發移轉、仲裁或回條）。

        自訂 hook 完全取代預設；預設走核心決策點 ``presence.absent_intent``（文字只在記憶體，經 native 傳入）。
        決策點逾時／busy／例外一律記 None（未判斷），不影響否決。意圖不是授權：授權只看已認證 actor 與規則。
        """
        hook = self._presence_request_hook
        try:
            if hook is not None:
                return hook(fact)
            authenticated = bool(actor_authenticated)
            inp = AbsentIntentInput(
                text=fact.text, source_platform=str(fact.source_location) if fact.source_location is not None else None,
                actor_ref=actor_id if _SAFE_ACTOR_REF.fullmatch(actor_id) else None,
                actor_authenticated=authenticated, is_owner=is_owner == "true" and authenticated,
            )
            result = judge_absent_intent(self.hub, inp, request_id=f"{self.run_id}:absent:{fact.seq}", ref=ref)
            return presence_request_from_result(result)
        except Exception:
            logger.exception("presence_request 判斷失敗，記為未判斷")
            return None

    def _record_absent_input(self, ctx: SessionContext, fact: UtteranceFact, reason: str) -> None:
        """寫缺席端輸入的 shadow 紀錄（turn.skipped）：來源、說話者／actor、is_owner、長度；不保存原文。

        is_owner：身分映射命中 owner 為 "true"、查不到為 "false"；沒有身分映射無法判定時為 "unknown"（不猜）。
        """
        self.metrics["intake.absent_input"] += 1
        self.metrics[f"intake.absent_input.{reason}"] += 1
        ident = self._identity.resolve_platform(fact.speaker_ref) if self._identity is not None else None
        is_owner = "unknown" if ident is None else ("false" if ident.guest else "true")
        actor_id = (ident.actor_id if ident is not None else None) or ctx.profile_actor_id or UNVERIFIED_ACTOR
        ref = f"es:{self.run_id}:{ctx.session_ref}:{fact.seq}"
        # actor_authenticated：只看平台認證 ID。身分映射命中且輸入帶有 ingress 以已驗證 endpoint 簽發的 source_endpoint_id 才算；
        # speaker_ref 在 ingress 簽發落地前是不受信字串，資訊不足一律 False，不比對顯示名稱
        actor_authenticated = bool(ident is not None and ident.actor_id is not None and fact.source_endpoint_id is not None)
        presence_request = self._presence_request(ref, fact, actor_id, actor_authenticated, is_owner)
        payload: dict[str, object] = {
            "reason_code": reason, "reply_mode": ReplyMode.PASS.value, "proposal_ref": ref, "utterance_ref": ref,
            "speaker_ref": fact.speaker_ref, "actor_id": actor_id, "is_owner": is_owner,
            "source_location": str(fact.source_location) if fact.source_location is not None else "missing",
            "len": len(fact.text), "presence_request": presence_request, "actor_authenticated": actor_authenticated,
        }
        if fact.source_endpoint_id is not None:
            payload["source_endpoint_id"] = fact.source_endpoint_id
        draft = EventDraft(
            event_type="turn.skipped", conversation_id=ctx.conversation_id, segment_id=ctx.segment_id,
            idempotency_key=f"turn_skipped:{self.run_id}:{ctx.session_ref}:{fact.seq}", payload=payload,
        )
        trusted = TrustedContext(
            source=SOURCE_TURN, actor_id=ACTOR_TURN, scope_ref=ctx.event_scope, persona_id=self._persona
        )
        try:
            self._publisher.publish(draft, trusted)
        except Exception:
            logger.exception("缺席端輸入 shadow 紀錄寫入失敗")

    def _decide_turn(self, ctx: SessionContext, turn: AggregatedTurn) -> IntakeResult:
        intake = turn.last.payload
        if not isinstance(intake, _Intake):  # aggregator 實作沒有保留 payload：視為內部錯誤，丟棄這個 turn
            self.metrics["aggregator.lost_payload"] += 1
            return IntakeResult(None, "aggregator_error")
        fact = intake.fact if len(turn.segments) == 1 else dataclasses.replace(intake.fact, text=turn.text)
        ident = self._identity.resolve_platform(fact.speaker_ref) if self._identity is not None else GUEST
        actor_id = ident.actor_id or UNVERIFIED_ACTOR
        auth = self._auth_provider()
        # Core 否決權（§4.3）在 propose 之前：Presence 非 ACTIVE、全域停止、audience 不允許時
        # reply_mode 一律強制為 pass，不呼叫 proposer（它不該看到不該看的內容，也不該有任何副作用）
        vetoed = self._vetoed(ctx, auth)
        if vetoed is not None:
            self._skip(ctx, intake.trace_id, intake.event_id, fact, vetoed, ReplyMode.PASS)
            return IntakeResult(None, vetoed, None, utterance_event_id=intake.event_id, fact=fact)
        candidate = self._candidate(ctx, fact, turn.refs or (intake.event_id,), len(turn.segments), True)
        proposal = self._propose(candidate)
        degraded: Degraded | None = None
        if proposal.reply_mode == ReplyMode.DEFER:
            # 能力查核在 interaction 請求之前：CC-7 與稽核記錄看到的是 respond，不是 defer
            proposal, degraded = self._capability_before_defer(proposal)
        proposal = self._apply_interaction(candidate, proposal)
        if degraded is not None and proposal.reason_code != "action_unavailable":
            degraded = None  # CC-7 被拒而回 D80 預設：已不是降級提議
        if proposal.reply_mode in (ReplyMode.PASS, ReplyMode.DEFER):
            self._skip(ctx, intake.trace_id, intake.event_id, fact, proposal.reason_code, proposal.reply_mode)
            return IntakeResult(None, proposal.reason_code, proposal, utterance_event_id=intake.event_id, fact=fact)
        assert auth is not None
        return self._start(
            ctx, fact, intake.event_id, intake.trace_id, candidate.candidate_id, proposal, auth, actor_id,
            degraded=degraded,
        )

    def _capability_before_defer(self, proposal: Proposal) -> tuple[Proposal, Degraded | None]:
        """能力查核先於 defer 確認（Chain 契約 Q-S／艾斯維爾 11/12）：``defer`` 且 route 是 task_request／agent_loop，
        工具層查不到對應能力時，不向使用者確認，直接開 turn 走降級路徑（``degraded="action_unavailable"``）。
        能力存在、入口不提供查核時維持原 defer；沒有綁定入口時只有 task_request 轉換（標籤全是寫入類，direct_chat 做不到）。
        沒被叫到（``addressed`` 為 False）的 defer 不開 turn，維持原略過。"""
        if proposal.route not in ("agent_loop", "task_request") or proposal.addressed is False:
            return proposal, None
        if self._loop_entry is None:
            if proposal.route != "task_request":
                return proposal, None  # agent_loop 沒綁入口：direct_chat 的唯讀工具仍可能處理，維持 defer
            return self._defer_to_unavailable(proposal)  # task_request 沒綁入口：標籤全是寫入類，direct_chat 做不到
        probe = getattr(self._loop_entry, "has_capability", None)
        if not callable(probe):
            return proposal, None
        try:
            capable = bool(probe(proposal))
        except Exception:
            logger.exception("capability 查核失敗，維持 defer")
            return proposal, None
        if capable:
            return proposal, None
        return self._defer_to_unavailable(proposal)

    def _defer_to_unavailable(self, proposal: Proposal) -> tuple[Proposal, Degraded | None]:
        degraded_proposal = dataclasses.replace(
            proposal, reply_mode=ReplyMode.RESPOND, reason_code="action_unavailable",
            original_reason_code=proposal.reason_code,
        )
        bad = validate_proposal(degraded_proposal)
        if bad is not None:
            logger.error("defer 轉換後的 Proposal 驗證失敗（%s），維持 defer", bad)
            self.metrics["turn.defer_conversion_invalid"] += 1
            return proposal, None
        self.metrics["turn.defer_action_unavailable"] += 1
        return degraded_proposal, "action_unavailable"

    def _vetoed(self, ctx: SessionContext, auth: AuthorizationTuple | None) -> str | None:
        if auth is None:
            return "presence_inactive"
        if self._veto is None:
            return None
        try:
            return self._veto(ctx)
        except Exception:
            logger.exception("veto 判斷失敗，fail-closed")
            return "veto_error"

    def _decide_interjection(self, ctx: SessionContext, ij: Interjection) -> IntakeResult:
        """沒有新 utterance 觸發的決策（被動模式插話）：以 buffer 最後一句為代表，沒有 trace 可延續。"""
        self._tick_seq += 1
        fact = UtteranceFact(speaker_ref=ij.speaker_ref, text=ij.text, seq=self._tick_seq)
        ident = self._identity.resolve_platform(ij.speaker_ref) if self._identity is not None else GUEST
        actor_id = ident.actor_id or UNVERIFIED_ACTOR
        auth = self._auth_provider()
        proposal = ij.proposal
        bad = validate_proposal(proposal)
        vetoed = self._vetoed(ctx, auth)
        if vetoed is not None or bad is not None or proposal.reply_mode in (ReplyMode.PASS, ReplyMode.DEFER):
            self.metrics["decision.interjection_dropped"] += 1
            return IntakeResult(None, vetoed or bad or proposal.reason_code, proposal, fact=fact)
        assert auth is not None
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
        if count is None:
            count = 1  # 未知視為單人（與 D80 預設一致）
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
        speakers: tuple[str, ...] = (), degraded: Degraded | None = None,
    ) -> IntakeResult:
        turn_id = new_id("turn")
        generation = self.generations.begin(turn_id)
        try:
            result = self._issue(
                ctx, fact, event_id, trace_id, candidate_id, proposal, auth, actor_id, turn_id, generation,
                speakers or (fact.speaker_ref,), degraded,
            )
        except BaseException:
            # turn.started 寫入失敗：回收 live 與 generation，不留下沒有 started 事件的孤兒 turn
            with self._lock:
                self._live.pop(turn_id, None)
            self.generations.discard(turn_id)
            self.metrics["turn.issue_failed"] += 1
            raise
        return result

    def _issue(
        self, ctx, fact, event_id, trace_id, candidate_id, proposal, auth, actor_id, turn_id, generation, speakers,
        degraded: Degraded | None = None,
    ) -> IntakeResult:
        if degraded is None and proposal.route == "task_request" and self._loop_entry is None:
            # 沒綁 loop 入口時 task_request 無從執行（標籤全是寫入類，direct_chat 的唯讀工具做不到）：
            # 帶降級事實，避免她假裝做了。agent_loop 沒入口則不標（direct_chat 的唯讀工具仍可能處理）
            degraded = "action_unavailable"
            self.metrics["turn.action_unavailable"] += 1
        ticket = CoreTicket(
            turn_id=turn_id, turn_generation=generation, source_turn_ref=fact.legacy_ref,
            conversation_id=ctx.conversation_id, segment_id=ctx.segment_id, audience_scope=ctx.audience_scope,
            event_scope=ctx.event_scope, reply_mode=proposal.reply_mode.value, auth=auth,
            valid_until=self._clock() + self._ttl, snapshot_version=self._snapshot_version(),
            speaker_ref=fact.speaker_ref, actor_id=actor_id, user_text=fact.text, degraded=degraded,
        )
        with self._lock:
            self._live[turn_id] = _Live(ticket, trace_id, event_id, candidate_id, proposal.reason_code, speakers)
        self._emit(ticket, "turn.started", {"status": "started"})
        self._phase(InteractionState.THINKING, turn_id)
        if degraded is None:  # defer 先查核已確定降級就不進 loop 入口
            self._enter_loop(ticket, proposal)
            with self._lock:
                live = self._live.get(turn_id)
                if live is not None:
                    ticket = live.ticket  # 入口降級時 ticket 已帶 degraded
        return IntakeResult(ticket, "issued", proposal, utterance_event_id=event_id, fact=fact)

    def _enter_loop(self, ticket: CoreTicket, proposal: Proposal) -> None:
        """route=agent_loop／task_request 且已綁定入口時交給 Agent 層（Run 之後脫離 turn 獨立存活，§4.6）；入口失敗不影響 turn。
        task_request 由入口依 route 組緊預算的 Run（Chain 契約 PR-R）；入口不啟動（標籤空、投影空、額滿）時照 direct_chat 處理。"""
        if self._loop_entry is None or proposal.route not in ("agent_loop", "task_request"):
            return
        with self._lock:
            self._loop_claims[ticket.turn_id] = None
            while len(self._loop_claims) > 1024:
                self._loop_claims.popitem(last=False)
        claimed = False
        unavailable = False  # 沒有可用工具（capability_empty／capability_projection_empty）：降級要帶事實，不能讓她假裝做了
        try:
            claimed = self._loop_entry(ticket, proposal) is not None
            unavailable = not claimed  # 入口回 None = 標籤收窄後為空（capability_empty）
        except Exception as exc:  # Agent 層的失敗不得讓 turn 簽發失敗；未認領 -> 回合照 direct_chat 處理
            if getattr(exc, "loop_refused", False):
                # 入口明確拒絕（並發上限、投影為空等）：不是故障，回合退回 direct_chat
                self.metrics["turn.loop_entry_refused"] += 1
                unavailable = bool(getattr(exc, "capability_unavailable", False))
                logger.warning("agent_loop 入口拒絕，改走 direct_chat：turn=%s reason=%s", ticket.turn_id, exc)
            else:
                self.metrics["turn.loop_entry_failed"] += 1
                logger.exception("agent_loop 入口失敗：turn=%s", ticket.turn_id)
        if not claimed and proposal.route == "task_request":
            # task_request 沒變成 Run（額滿、關閉中、入口失敗、標籤空…）：direct_chat 沒有寫入工具，動作不會發生，
            # 一律帶降級事實。agent_loop 只在 capability_empty／projection_empty 才標（direct_chat 的唯讀工具仍可能處理）
            unavailable = True
        if claimed:
            self.metrics["turn.loop_claimed"] += 1
        else:
            with self._lock:
                self._loop_claims.pop(ticket.turn_id, None)
                live = self._live.get(ticket.turn_id)
                if unavailable and live is not None:
                    live.ticket = dataclasses.replace(live.ticket, degraded="action_unavailable")
            if unavailable:
                self.metrics["turn.action_unavailable"] += 1

    def is_ended(self, turn_id: str) -> bool:
        """turn 已結束（或根本不是 live）。AgentLoop worker 用它判斷要不要繼續（公開方法，不讀私有屬性）。"""
        with self._lock:
            live = self._live.get(turn_id)
            return live is None or live.ended

    def loop_claimed(self, turn_id: str) -> bool:
        """這個 turn 是否已由 AgentLoop 認領（入口啟動成功）。宿主據此不再送 ES 生成回覆；交付只經 Run 的 ``deliver_result``。"""
        with self._lock:
            return turn_id in self._loop_claims

    def on_think_started(self, turn_id: str) -> bool:
        """ES 真的開始 think（``_on_think_start``，入佇列後呼叫）：提議在這裡才算被採用，回饋 proposer。

        從未開始生成就結束的輪（dropped_busy、void、讓位）不會走到這裡，因此不會留下 started 的副作用。每輪至多一次。"""
        with self._lock:
            live = self._live.get(turn_id)
            if live is None or live.ended or live.think_started:
                return False
            live.think_started = True
        self._feedback("on_turn_started", turn_id, live.candidate_id, live.speakers or (live.ticket.speaker_ref,))
        return True

    def _feedback(self, hook_name: str, *args: object) -> None:
        """回饋 proposer（失敗隔離）；shadow proposer 也要收到，讓分歧比較不失真（在各自的執行緒上、忙碌就略過）。"""
        hook = getattr(self._proposer, hook_name, None)
        if callable(hook):
            try:
                hook(*args)
            except Exception:
                logger.exception("OutcomeObserver.%s 失敗", hook_name)
        for shadow, guard in self._shadows:
            shadow_hook = getattr(shadow, hook_name, None)
            if not callable(shadow_hook):
                continue
            if guard.submit(lambda a, h=shadow_hook: h(*a), args) is None:
                self.metrics["shadow.feedback_skipped_busy"] += 1

    def _sanitize(self, fact: UtteranceFact) -> UtteranceFact:
        """speaker_ref、language 是不受信資料：長度與字元集不符就丟棄（改成安全預設）並計數。"""
        speaker, language = fact.speaker_ref, fact.language
        if not isinstance(speaker, str) or not _SAFE_REF.fullmatch(speaker):
            self.metrics["intake.invalid_speaker_ref"] += 1
            speaker = UNKNOWN_SPEAKER
        if language is not None and (not isinstance(language, str) or not _SAFE_LANGUAGE.fullmatch(language)):
            self.metrics["intake.invalid_language"] += 1
            language = None
        source, endpoint = fact.source_location, fact.source_endpoint_id
        if source is not None and not isinstance(source, PresenceLocation):
            try:
                source = PresenceLocation(source)
            except ValueError:
                self.metrics["intake.invalid_source_location"] += 1
                source = INVALID_SOURCE_LOCATION  # type: ignore[assignment]  # 視為不一致，不當成缺失
        if endpoint is not None and (not isinstance(endpoint, str) or not _SAFE_REF.fullmatch(endpoint)):
            endpoint = None
        if (speaker is fact.speaker_ref and language is fact.language and source is fact.source_location
                and endpoint is fact.source_endpoint_id):
            return fact
        return dataclasses.replace(
            fact, speaker_ref=speaker, language=language, source_location=source, source_endpoint_id=endpoint
        )

    def _propose(self, candidate: TurnCandidate) -> Proposal:
        """向決策中樞請求 ``turn.reply``；預算、busy／逾時／例外／驗證失敗的 D80 預設與 shadow 都在中樞內處理。"""
        request = DecisionRequest(
            request_id=f"{self.run_id}:{candidate.candidate_id}", kind=KIND_TURN_REPLY,
            subject={"conversation_id": candidate.ctx.conversation_id, "candidate_id": candidate.candidate_id},
            allowed_outcomes=tuple(m.value for m in ReplyMode), deadline=candidate.deadline, native=candidate,
        )
        return result_to_proposal(self.hub.decide(KIND_TURN_REPLY, request))

    def _apply_interaction(self, candidate: TurnCandidate, proposal: Proposal) -> Proposal:
        """Proposal.interaction_to 以 ``core.decision`` 向 CC-7 提請求（只在 Interaction 軸）。

        被拒（版本衝突、不合法轉換、未授權…）一律回 D80 預設，不重試。沒有綁定 State Coordinator 時沒有地方可請求，忽略。
        """
        target = proposal.interaction_to
        if target is None or self.state is None:
            return proposal
        observing = proposal.reply_mode in (ReplyMode.PASS, ReplyMode.DEFER)

        def rejected(counter: str) -> Proposal:
            """PASS／DEFER 本來就不開 turn：只丟棄 interaction_to，維持原提議；其餘回 D80 預設。"""
            self.metrics[counter] += 1
            if observing:
                return dataclasses.replace(proposal, interaction_to=None)
            return fallback_proposal(candidate, "proposal_rejected")

        # 重讀軸（propose 期間軸可能已被別人推進）；只允許 IDLE -> LISTENING（旁聽觀察），其他狀態不動
        try:
            view = self.state.current(Axis.INTERACTION)
        except Exception:
            logger.exception("讀取 Interaction 軸失敗")
            return rejected("decision.cc7_error")
        if target != InteractionState.LISTENING.value or view.state != InteractionState.IDLE.value:
            self.metrics["decision.interaction_skipped"] += 1
            return dataclasses.replace(proposal, interaction_to=None)
        try:
            decision = self.state.request_transition(StateTransitionRequest(
                request_id=f"decision_{target.lower()}:{candidate.candidate_id}", axis=Axis.INTERACTION.value,
                proposer=DECISION_PROPOSER, expected_version=view.version,
                from_state=view.state, to_state=target,
                reason={"source": "decision_slot", "reply_mode": proposal.reply_mode.value,
                        "reason_code": proposal.reason_code},
            ))
        except Exception:
            logger.exception("決策插槽的 Interaction 請求失敗")
            return rejected("decision.cc7_error")
        if decision.decision.value != "ACCEPTED":
            logger.info("Interaction 請求被拒（%s）", decision.reason_code)
            return rejected("decision.cc7_rejected")
        self.metrics["decision.cc7_accepted"] += 1
        return proposal

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
        self._notify_reply_to_aggregator(live, final_status)
        self._notify_outcome(live, final_status, final_reason, facts)
        return kind

    def _notify_reply_to_aggregator(self, live: _Live, status: str) -> None:
        """回覆結果回饋 aggregator（選用 ``note_replied``／``forget_replied``）：回覆完成之後的發言重新起算發言串；
        作廢／失敗（對方沒聽到）不算回覆過。"""
        if status == "completed":
            hook = getattr(self._aggregator, "note_replied", None)
        elif status in ("cancelled", "voided", "failed"):
            hook = getattr(self._aggregator, "forget_replied", None)
        else:
            return
        if not callable(hook):
            return
        for speaker in live.speakers or (live.ticket.speaker_ref,):
            try:
                hook(speaker)
            except Exception:
                logger.exception("aggregator 回覆回饋失敗")

    def _notify_outcome(self, live: _Live, status: str, reason: str | None, facts: TurnEndFacts) -> None:
        """回合結果回饋給 proposer（選用 ``OutcomeObserver``）：Q-1 的插話冷卻讀 ``barge_in``。"""
        self._feedback("on_turn_outcome", TurnOutcomeFacts(
            turn_id=live.ticket.turn_id, candidate_id=live.candidate_id,
            speakers=live.speakers or (live.ticket.speaker_ref,),
            reply_mode=live.ticket.reply_mode, reason_code=live.reason_code, status=status,
            end_reason=reason, generation_ok=facts.generation_ok, barge_in=facts.barge_in,
            started=live.think_started, reply_text=facts.spoken_text,
        ))

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
