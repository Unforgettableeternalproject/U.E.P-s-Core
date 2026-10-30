"""EchoPipelineHost：Core 內託管 Echo Stream 的轉接層（P3接入方案 §1、§5 S3；D70 (1)、D72 (a)）。

Core 眼中它同時是三個角色：輸入提交者（utterance -> Gateway）、回合執行者（接受 ``CoreTicket`` 跑
think -> speak -> sink）、播放端點（CC-4 Lease 持有者，``EchoEndpoint``）。依賴方向固定 ``uep -> echo_stream``，
只有 ``uep/echo/`` 可以 import echo_stream（``tests/uep/test_import_boundary.py`` 鎖住）。

執行緒模型（D70 (1)）：ES 跑在 ``EsLoop`` 專屬執行緒；``on_turn_ended``、``sink_factory``、``on_think_start``、
生成完成回呼都在 ES loop 上同步執行，**這些回呼只做記憶體計算與入佇列**，所有 DB／Gateway 寫入由
``CoreWorker`` 單一工作緒依序執行（證據回報因此先於 ``turn.completed``）。Core 呼叫 ES 一律經
``call_soon_threadsafe``。

audience 切換固定流程（D72 (a)）：coordinator ``live_turns()`` 確認空閒 -> ``think.reset_context()`` ->
宿主重設 profile（reset 會清空 profile 與 last_tool_calls；stream 進行中呼叫會拋 RuntimeError，視為忙碌）。

生命週期：``start`` 在 memory 之後、endpoint socket 之前；``stop`` 在 memory.stop 與 endpoint 關閉之前
（``CoreAssembly._close_inputs``）。停止順序：Core 自己擋新 submit -> 停輸入 -> ``coordinator.aclose``
（cancel_all shutdown 並等 commit）-> 排乾工作緒 -> 停 stage -> 關端點執行緒 -> 停 loop。關閉不 release Lease
（Core 關機不終止 Task，T-11；Lease 自然過期）。

ES 基準為 echo_stream develop 445d169（批 B 已合入）：用 ``bind_loop``、關閉後 submit 回 VOIDED（shutdown）、
FAILED 一律帶 ``reason_code``、``TurnRequest.on_generation_done(view, ok)``，Core 端不再有批 B 的繞道。
仍殘留的 ES 缺口只有 D5（已開播也能取消單輪的入口，見 ``_on_endpoint_stop``）與 A8 的 tool_context 鍵名。
"""
from __future__ import annotations

import asyncio
import functools
import logging
import threading
from collections import Counter, OrderedDict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from echo_stream.contracts.cancellation import CancellationToken
from echo_stream.contracts.output import TurnRequest, TurnTicket
from echo_stream.contracts.stages import AudioSink
from echo_stream.contracts.types import Utterance
from echo_stream.core.coordinator import TurnCoordinator
from echo_stream.core.tracer import LatencyTracer

from uep.contracts.presence import AcquireResult, GateDecision
from uep.echo.endpoint import DeferredCoreApi, EchoEndpoint, StopNotice
from uep.echo.loop import EsLoop
from uep.echo.memory import GuildRoster, make_memory_resolver
from uep.echo.outcomes import facts_from_outcome
from uep.echo.policy import HostPolicySource
from uep.echo.services import EchoServices
from uep.echo.sink import AdmittingSink, SentenceEvidence
from uep.echo.worker import CoreWorker
from uep.events.envelope import EventDraft, TrustedContext
from uep.events.publisher import EventPublisher
from uep.ids import new_id
from uep.memory.identity import IdentityDirectory
from uep.memory.protocol import MemoryPort
from uep.output.delivery import DeliveryEvidence
from uep.output.delivery import DeliveryStatus as DS
from uep.output.intent import OutputIntent, OutputKind, OutputOrigin
from uep.turn.context import ContextRegistrar
from uep.turn.generations import TurnGenerations
from uep.turn.models import (
    CoreTicket,
    IntakeResult,
    SessionContext,
    TurnEndFacts,
    UtteranceFact,
)
from uep.turn.orchestrator import (
    ACTOR_ECHO,
    SOURCE_ECHO,
    TurnOrchestrator,
    attempt_label,
)
from uep.turn.proposer import TurnProposer

logger = logging.getLogger("uep.echo.host")

InnerSinkFactory = Callable[[Any, CoreTicket], "AudioSink | str"]
_STOP_TIMEOUT_S = 10.0
_DISPATCH_REFUSED = frozenset({DS.REJECTED, DS.CANCELLED, DS.FAILED})


class EchoHostError(RuntimeError):
    pass


class SessionBusyError(EchoHostError):
    """audience 切換時仍有進行中的輪（或 stream 進行中）：呼叫端須等空閒再切。"""


@dataclass
class _TurnRuntime:
    ticket: CoreTicket
    wrappers: list[AdmittingSink] = field(default_factory=list)


class EchoPipelineHost:
    def __init__(
        self,
        *,
        publisher: EventPublisher,
        think: Any,
        speak: Any,
        sink_factory: InnerSinkFactory,
        session: SessionContext,
        persona_id: str = "uep",
        endpoint_id: str = "ep_echo",
        location: str = "desktop",
        identity: IdentityDirectory | None = None,
        memory: MemoryPort | None = None,
        roster: GuildRoster | None = None,
        proposer: TurnProposer | None = None,
        input_stage: Any = None,
        tracer: LatencyTracer | None = None,
        generations: TurnGenerations | None = None,
        policy_source: HostPolicySource | None = None,
        endpoint: EchoEndpoint | None = None,
        profile_provider: Callable[[SessionContext], str] | None = None,
        prewarm_prompt: str | None = None,
        stop_timeout_s: float = _STOP_TIMEOUT_S,
        endpoint_kwargs: dict[str, Any] | None = None,
    ) -> None:
        self.publisher = publisher
        self.think = think
        self.speak = speak
        self._inner_sink_factory = sink_factory
        self._session = session
        self.persona_id = persona_id
        self.endpoint_id = endpoint_id
        self._location = location
        self._memory = memory
        self.roster = roster or GuildRoster()
        self._input_stage = input_stage
        self.tracer = tracer or LatencyTracer()
        self.generations = generations or TurnGenerations()
        self.policy = policy_source or HostPolicySource(session.platform)
        self._profile_provider = profile_provider
        self._prewarm_prompt = prewarm_prompt
        self._stop_timeout_s = stop_timeout_s
        self._deferred_core = DeferredCoreApi()
        self.endpoint = endpoint or EchoEndpoint(
            endpoint_id, self._deferred_core, location=location, **(endpoint_kwargs or {})
        )
        if endpoint is None:
            self.endpoint.set_on_stop(self._on_endpoint_stop)
        self.orchestrator = TurnOrchestrator(
            publisher, self.generations, auth_provider=self.endpoint.lease_auth, persona_id=persona_id,
            proposer=proposer, identity=identity, snapshot_version=lambda: self.policy.version,
        )
        self._es = EsLoop()
        self._worker = CoreWorker()
        self.coordinator: TurnCoordinator | None = None
        self._services: EchoServices | None = None
        self._live: dict[str, _TurnRuntime] = {}  # 只在 ES loop 執行緒上存取
        self._wrappers: OrderedDict[str, list[AdmittingSink]] = OrderedDict()  # 診斷用，保留最近 64 輪
        self._intake_lock = threading.Lock()
        self._intake_pool: ThreadPoolExecutor | None = None
        self._input_task: asyncio.Future[Any] | None = None
        self._input_token: CancellationToken | None = None
        self._seq = 0
        self._control_sub: Any = None
        self._started = False
        self._closed = False  # 宿主生命週期：停止開始後不再簽新 ticket（aclose 後的 submit 由 ES 回 VOIDED）
        self.lease_result: AcquireResult | None = None
        self.metrics: Counter[str] = Counter()
        self.discarded_by_turn: Counter[str] = Counter()
        if memory is not None:
            resolver = make_memory_resolver(memory)
            if getattr(think, "memory_resolver", None) is None and hasattr(think, "memory_resolver"):
                think.memory_resolver = resolver

    # ---- 組裝 ----

    @property
    def adapter(self) -> EchoEndpoint:
        """``assemble_core(adapter=...)`` 用：DeliveryManager 的 OutputAdapter 就是這個端點。"""
        return self.endpoint

    @property
    def generation_source(self) -> Callable[[str], int | None]:
        """``assemble_core(turn_generation_source=...)`` 用。"""
        return self.generations.get

    @property
    def session(self) -> SessionContext:
        return self._session

    @property
    def started(self) -> bool:
        return self._started

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def presence_active(self) -> bool:
        return self.endpoint.lease_auth() is not None

    def wrappers_of(self, turn_id: str) -> list[AdmittingSink]:
        """某輪建立過的 sink 包裝（每個 attempt 一個；診斷與測試用，保留最近 64 輪）。"""
        return list(self._wrappers.get(turn_id, ()))

    def bind(self, services: EchoServices) -> None:
        if self._services is not None:
            raise EchoHostError("EchoPipelineHost 已 bind")
        self._services = services
        self._deferred_core.attach(services.core_api)
        self.orchestrator.bind_state(services.state)
        if self.orchestrator.registrar is None:
            self.orchestrator.registrar = ContextRegistrar(services.session_factory, persona_id=self.persona_id)

    # ---- 生命週期 ----

    def start(self) -> None:
        if self._started:
            return
        services = self._services
        if services is None:
            raise EchoHostError("EchoPipelineHost 尚未 bind（請經 assemble_core(echo=...) 組裝）")
        self._closed = False
        self._worker.start()
        self._es.start()
        try:
            if self._memory is not None:
                self._memory.set_session(self._session.segment_id)  # 記憶 episode 標記與 exclude 自我回聲用
            self._es.run(self._startup(), timeout=60.0)
            self._intake_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="uep-echo-intake")
            self.endpoint.set_on_stop(self._on_endpoint_stop)
            self._control_sub = self.endpoint.attach_control(services.control)
            self.endpoint.start()
            self.acquire_lease()
            if self._input_stage is not None:
                self._input_task = asyncio.run_coroutine_threadsafe(self._consume_input(), self._es.loop)
        except BaseException:
            self._teardown(quiet=True)
            raise
        self._started = True

    def acquire_lease(self) -> AcquireResult:
        """取得（或重試取得）端點 Lease；被拒不拋例外（之後的 utterance 一律 presence_inactive）。"""
        try:
            self.lease_result = self.endpoint.start_lease()
        except Exception:
            logger.exception("端點 acquire 失敗")
            self.lease_result = AcquireResult.DENIED
        if self.lease_result is not AcquireResult.GRANTED:
            logger.warning("EchoPipelineHost 沒有取得 Lease：%s", self.lease_result.value)
        return self.lease_result

    async def _startup(self) -> None:
        # 暖機順序（ES 介面清單 B3）：speak -> think -> prewarm -> 輸入 stage
        await self.speak.prepare()
        await self.think.prepare()
        prewarm = getattr(self.think, "prewarm", None)
        if prewarm is not None and self._prewarm_prompt is not None:
            await prewarm(self._prewarm_prompt)
        coordinator = TurnCoordinator(
            self.think, self.speak, tracer=self.tracer, policy_source=self.policy,
            on_turn_ended=self._on_turn_ended,
        )
        coordinator.bind_loop(asyncio.get_running_loop())  # 首輪之前的跨執行緒取消與語音事件才不會遺失
        self.coordinator = coordinator
        if self._input_stage is not None:
            await self._input_stage.prepare()
            if hasattr(self._input_stage, "on_voice_event"):
                self._input_stage.on_voice_event = self.on_voice_event

    def stop(self) -> None:
        """冪等。ES 先停（停 feed、cancel_all、等 commit 與事件寫完），再由組裝層停 memory 與其他入口。"""
        if not self._started and not self._es.running:
            return
        self._teardown(quiet=False)

    def _teardown(self, *, quiet: bool) -> None:
        self._closed = True  # 先停止簽新 ticket，之後才 aclose
        try:
            if self._es.running:
                self._es.run(self._shutdown_es(), timeout=self._stop_timeout_s)
        except Exception:
            if not quiet:
                logger.exception("ES 管線停止失敗")
        if not self._worker.drain(self._stop_timeout_s):
            logger.warning("Core 工作緒在逾時內沒有排乾")
        self._worker.stop()
        if self._control_sub is not None:
            try:
                self._control_sub.close()
            except Exception:
                logger.exception("端點控制訂閱關閉失敗")
            self._control_sub = None
        try:
            self.endpoint.close()
        except Exception:
            logger.exception("端點關閉失敗")
        if self._intake_pool is not None:
            self._intake_pool.shutdown(wait=False, cancel_futures=True)
            self._intake_pool = None
        self._es.stop()
        self._started = False

    async def _shutdown_es(self) -> None:
        if self._input_token is not None:
            self._input_token.cancel()
        if self._input_task is not None:
            self._input_task.cancel()
        if self._input_stage is not None:
            try:
                await self._input_stage.aclose()
            except Exception:
                logger.exception("輸入 stage 關閉失敗")
        if self.coordinator is not None:
            await self.coordinator.aclose()  # cancel_all(shutdown) + join；commit 都在這裡完成
        for stage in (self.think, self.speak):
            try:
                await stage.aclose()
            except Exception:
                logger.exception("stage 關閉失敗")

    # ---- 輸入 ----

    def submit_utterance(self, fact: UtteranceFact) -> IntakeResult:
        """任何非 ES-loop 執行緒：記錄 utterance、決策、簽 ticket、交給 ES。會寫 DB，不可在 ES loop 上呼叫。"""
        if self._es.in_loop_thread():
            raise EchoHostError("submit_utterance 會寫 DB，不可在 ES loop 執行緒上呼叫")
        if self._closed or not self._started:
            return IntakeResult(None, "closed")
        with self._intake_lock:
            result = self.orchestrator.on_utterance(self._session, fact)
        ticket = result.ticket
        if ticket is not None:
            try:
                self._es.call_soon(self._start_turn, ticket, fact)
            except RuntimeError:
                self._end_unsubmitted(ticket, "shutdown")
        return result

    def _start_turn(self, ticket: CoreTicket, fact: UtteranceFact) -> None:
        """ES loop 上：建 TurnRequest 並 submit（立即返回）。"""
        coordinator = self.coordinator
        if coordinator is None:
            self._end_unsubmitted(ticket, "shutdown")
            return
        runtime = self._live[ticket.turn_id] = _TurnRuntime(ticket)
        try:
            coordinator.submit(self._build_request(ticket, fact))
        except Exception:
            logger.exception("coordinator.submit 失敗：%s", ticket.turn_id)
            if self._live.pop(ticket.turn_id, None) is runtime:
                self._end_unsubmitted(ticket, "upstream_error")
            return

    def _build_request(self, ticket: CoreTicket, fact: UtteranceFact) -> TurnRequest:
        guild = self._session.guild_id
        retrieve_kwargs: dict[str, Any] = {
            "actor_id": ticket.actor_id, "audience": ticket.audience_scope, "session_id": ticket.segment_id,
            "shared_turn": guild is not None,
        }
        if guild is not None:
            retrieve_kwargs["present_uids"] = self.roster.present_uids(guild)
        # Core 令 ticket.turn_id、source_turn_id 與 Utterance.turn_id 相等（ES 不一致時只記 warning），
        # 輸入端原 id 放 meta["legacy_ref"]，避免 tracer 與記憶打點落到孤兒 trace
        utterance = Utterance(
            text=ticket.user_text, turn_id=ticket.turn_id, speaker_id=ticket.speaker_ref,
            language=fact.language, confidence=fact.confidence if fact.confidence is not None else 1.0,
            # 每輪各建一個新的 tool_context dict（不可跨輪共用，隱私）；ES #18 的格式：
            # {"member_recall": {"scope_id", "viewer_uid", "present_uids"?, "display_name"?, "shared_adapter"?}}，
            # 回覆後從同一個 dict 讀 member_recall_ids 併入 shared_context_ids。
            # TODO(S4／D79)：實際填值與讀回 ids；tool_executor 若再包一層須接受 context= 並往下傳。
            metadata={
                "memory_retrieve_kwargs": retrieve_kwargs, "legacy_ref": fact.legacy_ref, "tool_context": {},
            },
        )
        es_ticket = TurnTicket(
            turn_id=ticket.turn_id, source_turn_id=ticket.turn_id, owner_id=ticket.speaker_ref,
            reply_mode=ticket.reply_mode, snapshot_version=ticket.snapshot_version,
            meta={
                "legacy_ref": fact.legacy_ref, "conversation_id": ticket.conversation_id,
                "segment_id": ticket.segment_id, "turn_generation": ticket.turn_generation,
                "audience_scope": ticket.audience_scope,
            },
        )
        return TurnRequest(
            ticket=es_ticket, utterance=utterance,
            sink_factory=functools.partial(self._make_sink, ticket),
            barge_users={ticket.speaker_ref}, on_think_start=functools.partial(self._on_think_start, ticket),
            on_generation_done=functools.partial(self._on_generation_done, ticket),
            meta={"core_turn_id": ticket.turn_id},
        )

    def _end_unsubmitted(self, ticket: CoreTicket, reason: str) -> None:
        """已簽 ticket 卻沒有交給 ES（關閉中、submit 失敗）：補一個終結事件，不留孤兒 turn。"""
        facts = TurnEndFacts(status="cancelled", reason_code=reason)
        self._enqueue(functools.partial(self.orchestrator.on_turn_ended, ticket.turn_id, facts), "end_unsubmitted")

    # ---- 輸入 stage（可選）----

    def feed(self, speaker_ref: str, pcm: bytes, ts: float | None = None) -> bool:
        """平台 Adapter 餵音訊。Presence 非 ACTIVE 時在 feed 之前丟棄（P-9、V18）；回傳是否交給輸入 stage。"""
        if self._input_stage is None:
            return False
        if self._closed or not self.presence_active:
            self.metrics["echo.feed_dropped_absent"] += 1
            return False
        self._input_stage.feed(speaker_ref, pcm, ts)
        return True

    def on_voice_event(self, ev: Any) -> None:
        """輸入 stage 的有聲事件（STT 執行緒）：Presence 非 ACTIVE 或尚未啟動時丟棄，其餘交 coordinator。"""
        coordinator = self.coordinator
        if coordinator is None or self._closed or not self.presence_active:
            return
        coordinator.on_voice_event(ev)

    async def _consume_input(self) -> None:
        loop = asyncio.get_running_loop()
        token = self._input_token = CancellationToken()
        stage = self._input_stage
        try:
            async for utt in stage.open(token):
                pool = self._intake_pool
                if pool is None:
                    return
                await loop.run_in_executor(pool, self._intake_stage_utterance, utt)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("輸入 stage 的 utterance 流中斷")

    def _intake_stage_utterance(self, utt: Utterance) -> None:
        """執行緒池（單一 worker，保序）：ES 的 Utterance -> UtteranceFact -> submit_utterance。"""
        self._seq += 1
        fact = UtteranceFact(
            speaker_ref=utt.speaker_id or "unknown", text=utt.text, seq=self._seq, language=utt.language,
            confidence=utt.confidence, legacy_ref=utt.turn_id,
        )
        try:
            self.submit_utterance(fact)
        except Exception:
            logger.exception("submit_utterance 失敗")

    # ---- session ----

    def set_session(self, ctx: SessionContext) -> None:
        """切換 session（任何非 loop 執行緒，阻塞到套用完成）；audience／conversation 改變時走固定重置流程。"""
        if self._es.running:
            self._es.run(self._switch_session(ctx), timeout=30.0)
        else:
            self._assign_session(ctx)

    def _assign_session(self, ctx: SessionContext) -> None:
        self._session = ctx
        if self._memory is not None:
            self._memory.set_session(ctx.segment_id)

    async def _switch_session(self, ctx: SessionContext) -> None:
        old = self._session
        if self.coordinator is not None and self.coordinator.live_turns():
            raise SessionBusyError("仍有進行中的輪，不可切換 audience")
        changed = old.audience_scope != ctx.audience_scope or old.conversation_id != ctx.conversation_id
        reset = getattr(self.think, "reset_context", None)
        if changed and reset is not None:
            try:
                reset()
            except RuntimeError as exc:
                raise SessionBusyError(str(exc)) from exc
            profile = await asyncio.get_running_loop().run_in_executor(None, self._profile_for, ctx)
            if hasattr(self.think, "profile"):
                self.think.profile = profile  # reset 會清成 ""：宿主依新 audience 重設
        self._assign_session(ctx)

    def _profile_for(self, ctx: SessionContext) -> str:
        if self._profile_provider is not None:
            return self._profile_provider(ctx)
        if self._memory is not None and ctx.profile_actor_id:
            return self._memory.profile(actor_id=ctx.profile_actor_id, audience=ctx.audience_scope)
        return ""

    def participant_joined(self, speaker_ref: str, *, role: str = "speaker") -> None:
        self._participant("conversation.participant_joined", speaker_ref, role, joined=True)

    def participant_left(self, speaker_ref: str, *, role: str = "speaker") -> None:
        self._participant("conversation.participant_left", speaker_ref, role, joined=False)
        if self._es.running:
            self._es.call_soon(self._discard_pending, speaker_ref)  # 成員離場：待注入記憶要丟棄（隱私）

    def _participant(self, event_type: str, speaker_ref: str, role: str, *, joined: bool) -> None:
        ctx = self._session
        uid = speaker_ref.split(":", 1)[-1]
        if ctx.guild_id is not None:
            (self.roster.join if joined else self.roster.leave)(ctx.guild_id, uid)
        self.policy.publish()  # 參與者變動時推送新版快照
        draft = EventDraft(
            event_type=event_type, conversation_id=ctx.conversation_id, segment_id=ctx.segment_id,
            payload={"speaker_ref": speaker_ref, "role": role},
        )
        trusted = TrustedContext(
            source=SOURCE_ECHO, actor_id=ACTOR_ECHO, scope_ref=ctx.event_scope, persona_id=self.persona_id
        )
        self._enqueue(functools.partial(self.publisher.publish, draft, trusted), "participant")

    def _discard_pending(self, speaker_ref: str) -> None:
        discard = getattr(self.think, "discard_pending", None)
        if discard is not None:
            discard(speaker_ref)

    # ---- ES 回呼（在 ES loop 上，只入佇列）----

    def _make_sink(self, ticket: CoreTicket, view: Any) -> AudioSink | str:
        inner = self._inner_sink_factory(view, ticket)
        if isinstance(inner, str):
            return inner
        wrapper = AdmittingSink(
            inner, turn_id=ticket.turn_id, attempt_no=getattr(view, "attempt_id", 1), output_id=new_id("out"),
            admit=functools.partial(self._admit, ticket), on_sentence=self._on_sentence,
            on_reject=self._on_reject, on_discard=self._on_discard, context=ticket,
        )
        runtime = self._live.get(ticket.turn_id)
        if runtime is not None:
            runtime.wrappers.append(wrapper)
        self._wrappers.setdefault(ticket.turn_id, []).append(wrapper)
        while len(self._wrappers) > 64:
            self._wrappers.popitem(last=False)
        return wrapper  # type: ignore[return-value]

    def _admit(self, ticket: CoreTicket) -> GateDecision:
        """第二次接納（本地、只讀記憶體）：Lease 與 TTL 由端點判斷，再比對 turn_generation。"""
        decision = self.endpoint.admit_output(ticket.auth, ticket.turn_generation, ticket.valid_until)
        if decision is not GateDecision.ADMIT:
            return decision
        current = self.generations.get(ticket.turn_id)
        if current is None or current != ticket.turn_generation:
            return GateDecision.REJECT_STALE  # 取消／取代後的舊 generation（V12）
        return GateDecision.ADMIT

    async def _on_think_start(self, ticket: CoreTicket, view: Any, attempt_id: int) -> None:
        """C5：attempt 開始由 ``TurnRequest.on_think_start`` 觀測；hook 只能同步入佇列（ES 預設逾時 10 秒）。"""
        if attempt_id >= 2:
            self._enqueue(
                functools.partial(self.orchestrator.on_attempt_started, ticket.turn_id, attempt_id, "regen"),
                "attempt_started",
            )

    def _on_generation_done(self, ticket: CoreTicket, view: Any, ok: bool) -> None:
        """``TurnRequest.on_generation_done``：每輪 think 結束時一次（同步回呼早於 outcome）。
        ``ok=True`` 才發 ``turn.generation_completed``；沒進入生成的輪不會被呼叫。"""
        if ok:
            self._enqueue(
                functools.partial(self.orchestrator.on_generation_completed, ticket.turn_id), "generation_completed"
            )

    def _on_sentence(self, wrapper: AdmittingSink, index: int, sample_rate: int, channels: int) -> None:
        ticket: CoreTicket = wrapper.context
        a = ticket.auth
        intent = OutputIntent(
            output_id=wrapper.output_id, sequence=index, kind=OutputKind.AUDIO_STREAM, origin=OutputOrigin.PERSONA,
            persona_id=a.persona_id, endpoint_id=a.endpoint_id, lease_id=a.lease_id,
            fencing_epoch=a.fencing_epoch, core_epoch=a.core_epoch, presence_generation=a.presence_generation,
            turn_id=ticket.turn_id, turn_generation=ticket.turn_generation,
            attempt_id=attempt_label(wrapper.attempt_no), audience_scope=ticket.audience_scope,
            content_ref=f"es:{ticket.turn_id}:{wrapper.attempt_no}:{index}",
            content_format={"sample_rate": sample_rate, "channels": channels, "codec": "pcm_s16le"},
            valid_until=ticket.valid_until,
        )
        self._enqueue(functools.partial(self._dispatch, wrapper, intent, index), "dispatch_intent")

    def _on_reject(self, wrapper: AdmittingSink, decision: GateDecision) -> None:
        ticket: CoreTicket = wrapper.context
        self._enqueue(functools.partial(self._publish_rejection, ticket, wrapper, decision), "output_rejected")
        coordinator = self.coordinator
        if coordinator is not None:
            coordinator.cancel_voice(ticket.turn_id, "admission_rejected")  # 只關這一輪的語音，think 照常

    def _on_discard(self, wrapper: AdmittingSink) -> None:
        self.metrics["echo.discarded_chunks"] += 1
        self.discarded_by_turn[wrapper.turn_id] += 1

    def _on_turn_ended(self, outcome: Any) -> None:
        """每個 submit 恰好一次（含 DROPPED_BUSY／VOIDED）。只做記憶體計算並入佇列。"""
        runtime = self._live.pop(outcome.turn_id, None)
        if runtime is None:
            return
        facts = facts_from_outcome(outcome)
        evidence: list[tuple[AdmittingSink, list[SentenceEvidence]]] = []
        for wrapper in runtime.wrappers:
            try:
                evidence.append((wrapper, wrapper.collect_evidence()))
            except Exception:
                logger.exception("計算播放範圍失敗")
        self._enqueue(functools.partial(self._finish_turn, runtime.ticket, facts, evidence), "finish_turn")

    # ---- 工作緒（可寫 DB）----

    def _enqueue(self, fn: Callable[[], Any], label: str = "job") -> None:
        if self._worker.running:
            self._worker.submit(fn, label)
            return
        try:
            fn()  # 工作緒已停（關閉的收尾）：就地執行，不遺失終結事件
        except Exception:
            logger.exception("收尾任務失敗：%s", label)

    def _dispatch(self, wrapper: AdmittingSink, intent: OutputIntent, index: int) -> None:
        services = self._services
        assert services is not None
        delivery = services.delivery
        record = delivery.prepare(intent)
        if record.status is DS.GENERATED:
            record = delivery.mark_synthesized(record.delivery_id)
        record = delivery.dispatch(intent)
        wrapper.delivery_ids[index] = record.delivery_id
        if record.status in _DISPATCH_REFUSED:
            # Gate 或端點二次接納拒絕：靜音這一輪的語音；delivery 已由 Manager 記為 rejected／cancelled
            self.metrics["echo.dispatch_refused"] += 1
            self._es.call_soon(self._silence_voice, wrapper.turn_id)

    def _silence_voice(self, turn_id: str) -> None:
        if self.coordinator is not None:
            self.coordinator.cancel_voice(turn_id, "dispatch_refused")

    def _publish_rejection(self, ticket: CoreTicket, wrapper: AdmittingSink, decision: GateDecision) -> None:
        a = ticket.auth
        draft = EventDraft(
            event_type="output.rejected", conversation_id=ticket.conversation_id, segment_id=ticket.segment_id,
            turn_id=ticket.turn_id,
            fence={
                "persona_id": a.persona_id, "endpoint_id": a.endpoint_id, "lease_id": a.lease_id,
                "fencing_epoch": a.fencing_epoch, "core_epoch": a.core_epoch,
                "presence_generation": a.presence_generation, "turn_generation": ticket.turn_generation,
            },
            payload={"reason_code": decision.value, "ref_id": wrapper.output_id, "kind": OutputKind.AUDIO_STREAM.value},
        )
        trusted = TrustedContext(
            source=SOURCE_ECHO, actor_id=ACTOR_ECHO, scope_ref=ticket.event_scope, persona_id=self.persona_id
        )
        self.publisher.publish(draft, trusted)

    def _finish_turn(
        self, ticket: CoreTicket, facts: TurnEndFacts, evidence: list[tuple[AdmittingSink, list[SentenceEvidence]]]
    ) -> None:
        for wrapper, items in evidence:
            self._report_evidence(wrapper, items)
        self.orchestrator.on_turn_ended(ticket.turn_id, facts)
        self._store_memory(ticket, facts)

    def _report_evidence(self, wrapper: AdmittingSink, items: list[SentenceEvidence]) -> None:
        """D74：played_range 由 sink 包裝自算；只有宣告播放確認的 sink 才升級證據（否則停在 sent）。"""
        services = self._services
        assert services is not None
        for item in items:
            delivery_id = wrapper.delivery_ids.get(item.sentence_index)
            if delivery_id is None or not item.confirmed:
                continue
            try:
                services.delivery.report_evidence(delivery_id, DeliveryEvidence.LOCAL_PLAYBACK, item.played_range())
            except Exception:
                logger.debug("證據回報被拒（%s）", delivery_id, exc_info=True)

    def _store_memory(self, ticket: CoreTicket, facts: TurnEndFacts) -> None:
        """Q8：記憶寫入由 Core 依 outcome 自行 store（ES 的 think.memory 不寫入）；冪等鍵由 turn_id 衍生。"""
        if self._memory is None or not facts.generation_ok or not facts.spoken_text:
            return
        try:
            self._memory.store(
                actor_id=ticket.actor_id, user_text=ticket.user_text, spoken_text=facts.spoken_text,
                session_id=ticket.segment_id, idempotency_key=f"turn:{ticket.turn_id}",
            )
        except Exception:
            logger.exception("記憶 store 失敗")

    # ---- 端點停止通知（端點本地執行緒）----

    def _on_endpoint_stop(self, notice: StopNotice) -> None:
        """Lease 撤銷／到期／stop 控制訊號：登記 Core 的原因（generation 前進，舊輸出一律被拒）並停 ES 本地輸出。

        TODO(D5)：ES 沒有「已開播也能取消單輪」的入口；scope=turn 先以 ``cancel_voice`` 靜音，
        其餘範圍用 ``cancel_all``（任何執行緒、同步停 sink）。"""
        coordinator = self.coordinator
        tickets = self.orchestrator.live_tickets()
        gen = notice.turn_generation
        targets = [t for t in tickets if gen is None or t.turn_generation <= gen]
        for ticket in targets:
            self.orchestrator.cancel_turn(ticket.turn_id, notice.reason)
        if coordinator is None:
            return
        if gen is None:
            coordinator.cancel_all(notice.reason)
        else:
            for ticket in targets:
                coordinator.cancel_voice(ticket.turn_id, notice.reason)
