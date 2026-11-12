"""EchoPipelineHost：Core 內託管 Echo Stream 的轉接層（P3接入方案 §1、§5 S3；D70 (1)、D72 (a)）。

Core 眼中它同時是三個角色：輸入提交者（utterance -> Gateway）、回合執行者（接受 ``CoreTicket`` 跑
think -> speak -> sink）、播放端點（CC-4 Lease 持有者，``EchoEndpoint``）。依賴方向固定 ``uep.echo -> uep.stream``，
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

ES 基準為 echo_stream develop e31eac0（含 #19 的 ``cancel_turn``、#18 的 ``tool_context``）：用 ``bind_loop``、關閉後
submit 回 VOIDED（shutdown）、FAILED 一律帶 ``reason_code``、``TurnRequest.on_generation_done(view, ok)``。

S4（D5）：被拒、撤銷、停止、dispatch 失敗的輪一律 ``coordinator.cancel_turn(turn_id, reason_code=<Core 原因碼>,
commit="none")``（任何執行緒、同步停 sink、整輪撤出歷史）；回 False（輪已結束或已被其他取消認領）時宿主自行
``think.retract(turn_id)``，回 True 但 ``outcome.metadata["history_retracted"]`` 不是 True 時也補一次並計 metric。
Core 的原因碼隨 outcome 回來，不再覆寫 ES 的終結原因，也不再用 ``cancel_voice``／``cancel_all`` 繞道。
"""
from __future__ import annotations

import asyncio
import functools
import logging
import re
import threading
import time
from collections import Counter, OrderedDict
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from uep.stream.contracts.cancellation import CancellationToken
from uep.stream.contracts.output import (
    CommitMode,
    OutcomeStatus,
    TurnRequest,
    TurnTicket,
)
from uep.stream.contracts.stages import AudioSink
from uep.stream.contracts.types import Utterance
from uep.stream.coordinator import TurnCoordinator
from uep.stream.tracer import LatencyTracer

from uep.contracts.platform import platform_from_presence_location
from uep.contracts.presence import AcquireResult, GateDecision
from uep.decision.capability import ROUTE_DEFAULT_CAPABILITIES, ROUTE_DIRECT_CHAT
from uep.decision.hub import DecisionHub
from uep.echo.config import EchoOutputConfig
from uep.echo.endpoint import DeferredCoreApi, EchoEndpoint, StopNotice
from uep.echo.loop import EsLoop
from uep.echo.memory import GuildRoster, make_memory_resolver
from uep.echo.loop_text import LoopTextStore
from uep.echo.metrics import Metrics
from uep.echo.platform_context import PlatformShadow
from uep.echo.resume_return import ResumeCause, ResumedRunResult, ResumeDisposition, ResumeOutcome
from uep.echo.outcomes import facts_from_outcome
from uep.echo.policy import CAUSE_TTL_REFRESH, CoreYieldPolicy, HostPolicySource
from uep.echo.services import EchoServices
from uep.echo.sink import AdmittingSink, SentenceEvidence
from uep.echo.tools import CoreToolExecutor
from uep.echo.worker import CoreWorker
from uep.events.envelope import EventDraft, TrustedContext, scope_rank
from uep.events.publisher import EventPublisher
from uep.ids import new_id
from uep.memory.access import TOOL_CLASSES
from uep.memory.identity import IdentityDirectory
from uep.memory.protocol import MemoryPort
from uep.output.delivery import DeliveryEvidence
from uep.output.delivery import DeliveryStatus as DS
from uep.output.intent import OutputIntent, OutputKind, OutputOrigin
from uep.tools.catalog.memory import GUILD_ROSTER_CONTEXT, memory_registry
from uep.tools.projection import ProjectionContext
from uep.tools.registry import ToolRegistry
from uep.tools.render import render_tools
from uep.turn.aggregator import (
    ModeAwareAggregator,
    PassThroughAggregator,
    TurnAggregator,
)
from uep.turn.context import ContextRegistrar
from uep.turn.decision_config import DecisionConfig, decision_config_from_settings
from uep.turn.engagement import ChannelMode
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
    RETRACT_REASONS,
    SOURCE_ECHO,
    ShadowRecord,
    TurnOrchestrator,
    attempt_label,
)
from uep.turn.proposer import RespondProposer, TickingProposer, TurnProposer
from uep.turn.registry import create_proposer
from uep.turn.speech_filter import PassMarkerFilter
from uep.turn.wake import WakeDetector

logger = logging.getLogger("uep.echo.host")

InnerSinkFactory = Callable[[Any, CoreTicket], "AudioSink | str"]
_STOP_TIMEOUT_S = 10.0
_START_TIMEOUT_S = 60.0  # Host 啟動（_startup：各 stage prepare）總上限的預設；推理組裝會依 prepare 逾時加總覆寫
_SPEAKER_REF = re.compile(r"[A-Za-z0-9_:.\-]{1,128}")
_ROLE = re.compile(r"[a-z][a-z0-9_]{0,31}")
_DISPATCH_REFUSED = frozenset({DS.REJECTED, DS.CANCELLED, DS.FAILED})
_REASON_CODE_MAX = 64  # ES cancel_turn 的 reason_code 上限
HEALTH_SOURCE = "core.health"
_INPUT_RESTARTS = 1


def _tool_name(definition: Any) -> str:
    if not isinstance(definition, dict):
        return ""
    fn = definition.get("function")
    return str((fn if isinstance(fn, dict) else definition).get("name", ""))


class EchoHostError(RuntimeError):
    pass


class SessionBusyError(EchoHostError):
    """audience 切換時仍有進行中的輪（或 stream 進行中）：呼叫端須等空閒再切。"""


def _play_done(view: Any) -> bool:
    """coordinator 的 on_voice_event 會略過已 done 或已 play_done 的輪；TurnView 沒有公開這個旗標，容錯讀取。"""
    if getattr(view, "_done", False):
        return True
    event = getattr(view, "_play_done", None)
    return bool(event is not None and event.is_set())


@dataclass
class _TurnRuntime:
    ticket: CoreTicket
    wrappers: list[AdmittingSink] = field(default_factory=list)
    tool_context: dict[str, Any] = field(default_factory=dict)  # 與 Utterance.metadata["tool_context"] 同一個 dict


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
        aggregator: TurnAggregator | None = None,
        shadow_proposers: Sequence[TurnProposer] | None = None,
        on_shadow: Callable[[ShadowRecord], None] | None = None,
        decision: DecisionConfig | None = None,
        decision_hub: DecisionHub | None = None,
        input_stage: Any = None,
        tracer: LatencyTracer | None = None,
        generations: TurnGenerations | None = None,
        policy_source: HostPolicySource | None = None,
        endpoint: EchoEndpoint | None = None,
        profile_provider: Callable[[SessionContext], str] | None = None,
        output: EchoOutputConfig | None = None,
        prewarm_prompt: str | None = None,
        voice_retrieve_timeout_s: float | None = None,
        stop_timeout_s: float = _STOP_TIMEOUT_S,
        start_timeout_s: float = _START_TIMEOUT_S,
        endpoint_kwargs: dict[str, Any] | None = None,
        tool_path: str = "legacy",
    ) -> None:
        self.publisher = publisher
        self.think = think
        self.speak = speak
        if tool_path not in ("legacy", "agent"):
            raise ValueError(f"tool_path 只能是 legacy 或 agent：{tool_path!r}")
        self._tool_path = tool_path  # 組裝層決定一次後注入（單一來源）；host 不自己讀設定
        for attr in ("memory_resolver", "memory", "tool_executor"):
            if getattr(think, attr, None) is not None:
                # 自帶記憶來源或工具執行器會繞過 Access Layer（audience／ACL 過濾）：一律拒絕組裝
                raise ValueError(f"傳入的 think 已設定 {attr}：記憶必須經 Core 的 Access Layer（memory= 參數）注入")
        self._inner_sink_factory = sink_factory
        self.output = output or EchoOutputConfig()
        self._session = session
        self.persona_id = persona_id
        self.endpoint_id = endpoint_id
        self._location = location
        self._memory = memory
        self.roster = roster or GuildRoster()
        self._input_stage = input_stage
        self.tracer = tracer or LatencyTracer()
        self.generations = generations or TurnGenerations()
        # 未傳 decision：依 UEP_DECISION_* 疊上 proposer 選擇（DL-1b）；明確傳入的 DecisionConfig／proposer／shadow_proposers 優先
        self._decision = decision if decision is not None else decision_config_from_settings()
        self.policy = policy_source or HostPolicySource(
            session.platform, listener=self._decision.listener,
            barge_in_channel_modes=frozenset(ChannelMode(m) for m in self._decision.barge_in_channel_modes),
        )
        self._resume_lock = threading.Lock()
        self._resume_settled: OrderedDict[tuple[str, int], ResumeDisposition] = OrderedDict()  # (run_id, worker_epoch) -> 處置（冪等）
        self._stopped_reason: str | None = None  # 全域停止成立（直到取得新 Lease）：propose 之前即否決
        self._yield_policy = CoreYieldPolicy(self.policy)
        self._profile_provider = profile_provider
        self._prewarm_prompt = prewarm_prompt
        self._voice_retrieve_timeout_s = voice_retrieve_timeout_s  # None：沿用 Access Layer 預設（3 秒）
        self._stop_timeout_s = stop_timeout_s
        self._start_timeout_s = start_timeout_s
        self._deferred_core = DeferredCoreApi()
        self.endpoint = endpoint or EchoEndpoint(
            endpoint_id, self._deferred_core, location=location, **(endpoint_kwargs or {})
        )
        if endpoint is None:
            self.endpoint.set_on_stop(self._on_endpoint_stop)
        # agent_loop 回合的對外文字暫存；端點在 Gate 放行後的 send 依 content_ref 解析並渲染（DL-4 PR4）
        self.loop_text = LoopTextStore()
        set_resolver = getattr(self.endpoint, "set_text_resolver", None)
        if callable(set_resolver):
            set_resolver(self.loop_text.get)
        # primary：呼叫端明確傳入就以呼叫端為準；None 才依 DecisionConfig.proposer_id 經 registry 組裝
        if proposer is None:
            proposer = create_proposer(self._decision.proposer_id, config=self._decision)
        wake = getattr(proposer, "wake", None)
        # shadow：呼叫端明確傳入（含空序列）就以呼叫端為準；None 才依 DecisionConfig.shadow_proposer_ids 自動組裝
        if shadow_proposers is None:
            primary_id = getattr(proposer, "proposer_id", None) or RespondProposer.proposer_id
            shadow_proposers = tuple(
                create_proposer(pid, config=self._decision)
                for pid in dict.fromkeys(self._decision.shadow_proposer_ids)
                if pid != primary_id
            )
        self.decision_hub = decision_hub if decision_hub is not None else DecisionHub()
        self.orchestrator = TurnOrchestrator(
            publisher, self.generations, auth_provider=self.endpoint.lease_auth, persona_id=persona_id,
            proposer=proposer, aggregator=aggregator, shadow_proposers=shadow_proposers, on_shadow=on_shadow,
            wake=wake if isinstance(wake, WakeDetector) else WakeDetector(
                self._decision.gate, self._decision.bot_identity
            ),
            participants=self._participant_count, veto=self._veto, decision=self._decision,
            identity=identity, snapshot_version=lambda: self.policy.version, decision_hub=self.decision_hub,
            host_location=location,
        )
        self._wire_decision_slot()
        self._tick_thread: threading.Thread | None = None
        self._tick_stop = threading.Event()
        self.run_id = self.orchestrator.run_id
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
        self._tools_refresh_pending = False  # Lease 變動時因忙碌延後的工具重投影（輪結束後補做）
        self._switching = False  # set_session 進行中：不簽新 ticket（audience 不得以舊值簽發）
        self._input_failed = False
        self._closed = False  # 宿主生命週期：停止開始後不再簽新 ticket（aclose 後的 submit 由 ES 回 VOIDED）
        self.lease_result: AcquireResult | None = None
        self.metrics: Metrics = Metrics()
        self._platform_shadow: PlatformShadow | None = None  # 平台脈絡 shadow（只記錄，不改任何消費點的輸入）
        self._tool_registry: ToolRegistry | None = None  # agent 路徑的工具投影註冊表（組裝層注入；未注入時退回記憶註冊表）
        self.discarded_by_turn: Counter[str] = Counter()
        self._retract_tasks: set[asyncio.Future[Any]] = set()  # 只在 ES loop 執行緒上存取
        self._pollers: set[asyncio.Future[Any]] = set()  # 播放進度輪詢任務（只在 ES loop 上存取）
        self.dispatch_ms_max = 0.0  # D73：一句從首 chunk 寫入到 dispatch 完成的最大耗時（毫秒）
        self._tool_seq: Counter[str] = Counter()  # turn_id -> 已補記的工具呼叫數（冪等鍵；只在工作緒上存取）
        self.tool_executor: CoreToolExecutor | None = None
        if memory is not None and hasattr(think, "memory_resolver"):
            think.memory_resolver = make_memory_resolver(memory)
        if memory is not None and hasattr(think, "tool_executor") and hasattr(think, "tools"):
            # D79：ES 內建記憶工具由 Host 白名單放行，一律經 Access Layer（retrieve 的 memory_resolver 同理）
            self.tool_executor = CoreToolExecutor(
                memory, self.roster, on_action=self._record_tool_action, metrics=self.metrics,
                audience_ok=self._audience_allowed,
            )
            think.tool_executor = self.tool_executor
            self._refresh_tools()

    # ---- 決策插槽接線 ----

    def _veto(self, ctx: SessionContext) -> str | None:
        """Core 否決權（只讀記憶體）：全域停止成立、audience 不允許時，propose 之前就 pass。
        Presence 非 ACTIVE 由 Orchestrator 以 Lease 判斷。"""
        if self._stopped_reason is not None:
            return "global_stop"
        services = self._services
        provider = services.presence_audience if services is not None else None
        if callable(provider):
            allowed = provider()
            if allowed is None or scope_rank(ctx.audience_scope) > scope_rank(allowed):
                return "audience_not_allowed"
        return None

    def _audience_allowed(self, audience: str) -> bool:
        """Gate 目前的 presence audience 是否仍允許 ``audience``（只讀記憶體；沒有 provider = 不限制）。"""
        services = self._services
        provider = services.presence_audience if services is not None else None
        if not callable(provider):
            return True
        allowed = provider()
        try:
            return allowed is not None and scope_rank(audience) <= scope_rank(allowed)
        except ValueError:
            return False

    def _participant_count(self, ctx: SessionContext) -> int | None:
        """在場人數（含說話者）：只有有 guild 的 session 才知道；未知回 None（決策預設視為單人）。"""
        if ctx.guild_id is None:
            return None
        return len(self.roster.present_uids(ctx.guild_id))

    def _wire_decision_slot(self) -> None:
        """把 proposer 的頻道模式變化接到政策快照與 aggregator（等待門檻），喚醒判斷接到 aggregator 的 urgent。"""
        proposer = self.orchestrator.proposer
        aggregator = self.orchestrator.aggregator
        add_listener = getattr(proposer, "add_mode_listener", None)
        if callable(add_listener):
            add_listener(self._on_channel_mode)
            initial = getattr(proposer, "channel_mode", None)
            if isinstance(initial, ChannelMode):
                self._on_channel_mode(initial)
        wake = getattr(proposer, "wake", None)
        if isinstance(aggregator, ModeAwareAggregator) and isinstance(wake, WakeDetector):
            aggregator.set_urgent_predicate(wake.is_called)

    def _on_channel_mode(self, mode: ChannelMode) -> None:
        """頻道模式翻轉必推新版快照（讓位只在主動模式生效）；aggregator 主動模式改用短等待。"""
        self.policy.publish_channel_mode(mode)
        aggregator = self.orchestrator.aggregator
        if isinstance(aggregator, ModeAwareAggregator):
            aggregator.set_fast(mode is ChannelMode.ACTIVE)

    def _sync_participants(self, ctx: SessionContext) -> None:
        setter = getattr(self.orchestrator.proposer, "set_participant_count", None)
        count = self._participant_count(ctx)
        if callable(setter) and count is not None:
            try:
                setter(count)
            except Exception:
                logger.exception("proposer.set_participant_count 失敗")

    def _refresh_policy_if_due(self) -> None:
        """tick 成功後，政策快照年齡過半 ttl 才「確認後刷新」；永不拋例外（tick 執行緒不可因此中斷）。

        確認的層級＝Host roster：只證明 Host 這端的 roster 仍可讀、tick 路徑仍在轉；roster 是本機快取
        （GuildRoster 由平台轉接器經 set_members 灌入，沒有任何時間戳或新鮮度欄位可檢查），重讀成功並不代表
        外部來源（Discord 等）仍存活。外部來源失聯的偵測不在此處。
        流程：ttl_s 為 None 或年齡 < ttl_s×0.5 即返回；否則先 ``_sync_participants`` 重讀。重讀會經 proposer 的
        模式翻轉監聽器間接 publish，所以重讀後再看一次年齡：已歸零（版本已換）就不再補 ttl_refresh，
        同一 tick 只出一版。任何一步失敗只記 log、不 publish，讓快照自然過期。
        證據等級：需要 tick 的組裝＝submit_tick 成功＋roster 重讀成功；僅 ttl 的組裝（``needs_tick`` 為假、
        只因 ttl 啟動 tick 執行緒）＝tick 執行緒在跑＋roster 重讀成功，較弱。tick 執行緒卡住或死掉仍會過期（Q-5）。
        """
        ttl = self.policy.ttl_s
        if ttl is None:
            return
        threshold = ttl * 0.5
        try:
            if self.policy.age_s() < threshold:
                return
            self._sync_participants(self._session)
            if self.policy.age_s() < threshold:
                return  # 重讀已觸發 publish（年齡重新起算）
            self.policy.publish(cause=CAUSE_TTL_REFRESH)
        except Exception:
            logger.exception("政策快照 ttl 刷新失敗")

    @property
    def _runs_tick_thread(self) -> bool:
        """tick 執行緒是否要啟動：需要 tick，或設了政策快照 ttl（僅 ttl 的組裝只做快照刷新，不呼叫 submit_tick）。"""
        return self.needs_tick or self.policy.ttl_s is not None

    @property
    def needs_tick(self) -> bool:
        """是否需要週期性 tick：aggregator 有等待，或 proposer 會插話。"""
        return isinstance(self.orchestrator.proposer, TickingProposer) or not isinstance(
            self.orchestrator.aggregator, PassThroughAggregator
        )

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
        """真實狀態：輸入 stage 重啟後仍失敗時為 False（直接 submit_utterance 仍可用）。"""
        return self._started and not self._input_failed

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def presence_active(self) -> bool:
        return self.endpoint.lease_auth() is not None

    def wrappers_of(self, turn_id: str) -> list[AdmittingSink]:
        """某輪建立過的 sink 包裝（每個 attempt 一個；診斷與測試用，保留最近 64 輪）。"""
        return list(self._wrappers.get(turn_id, ()))

    @property
    def location(self) -> str:
        """本機 Host 持有的 Presence location（V18 缺席端輸入比對的對象）。"""
        return self._location

    def projection_platform(self) -> str | None:
        """工具投影用的當下 Presence 平台：Host 持有 Lease（ACTIVE）時為本機 location，否則 None（她在主機、不在任何平台）。

        這是刻意的授權邊界：工具投影以 Host 本機的 Lease 與 location 為準（與 V18 沿用 Host location 一致），
        不用 PlatformContextProvider 的 peek——peek 是建議性快照，不作授權依據；決策層 consult 用 peek 只做建議與 placement。
        兩邊不一致時投影偏保守（Lease 不在就不投平台限定工具），屬可接受。未知 location 同樣視為 None（fail-closed）。"""
        if not self.presence_active:
            return None
        return platform_from_presence_location(self._location)

    def set_require_source_location(self, required: bool) -> None:
        """組裝層注入 ``UEP_REQUIRE_SOURCE_LOCATION``（Host 不自己讀設定）；開啟後缺 source_location 的輸入即否決。"""
        self.orchestrator.require_source_location = bool(required)

    def bind_tool_registry(self, registry: ToolRegistry) -> None:
        """組裝層注入 agent 路徑的工具註冊表（與 TaskManager／Gateway／loop 同一份）；之後的 direct_chat 投影都用它。"""
        self._tool_registry = registry
        if self._tool_path == "agent":
            self._refresh_tools_agent()

    def bind_platform_shadow(self, shadow: PlatformShadow) -> None:
        """組裝層掛上平台脈絡 shadow；每個簽出 ticket 的 turn 觀察一次（失敗已在 shadow 內吞掉）。"""
        self._platform_shadow = shadow

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
        self._input_failed = False
        try:
            self._worker.start()
            self._es.start()
            if self._memory is not None:
                self._memory.set_session(self._session.segment_id)  # 記憶 episode 標記與 exclude 自我回聲用
            self._es.run(self._startup(), timeout=self._start_timeout_s)
            self._intake_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="uep-echo-intake")
            if self._runs_tick_thread:
                self._tick_stop.clear()
                self._tick_thread = threading.Thread(target=self._tick_loop, name="uep-echo-tick", daemon=True)
                self._tick_thread.start()
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
        if self.lease_result is AcquireResult.GRANTED:
            self._stopped_reason = None  # 新 Lease：先前的全域停止已解除
            self._sync_tools()  # 平台限定工具的曝光依 Lease 而定；建構時尚無 Lease，取得後重投影一次
        else:
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
            yield_policy=self._yield_policy, on_turn_ended=self._on_turn_ended,
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
        # 在 _intake_lock 內設 closed：等進行中的 on_utterance（已拿鎖者）結束，且其 _start_turn 已排進 loop
        with self._intake_lock:
            self._closed = True
        self._tick_stop.set()
        tick_thread, self._tick_thread = self._tick_thread, None
        if tick_thread is not None and tick_thread is not threading.current_thread():
            tick_thread.join(timeout=self._stop_timeout_s)
        try:
            if self._es.running:
                self._es.run(self._shutdown_es(), timeout=self._stop_timeout_s)
        except Exception:
            if not quiet:
                logger.exception("ES 管線停止失敗")
        if not self._worker.drain(self._stop_timeout_s):
            logger.warning("Core 工作緒在逾時內沒有排乾")
        self._worker.stop()
        for ticket in self.orchestrator.live_tickets():
            # 已發 turn.started 卻沒能啟動（或結果沒回來）的輪：補終結事件，不留孤兒 turn
            self.orchestrator.on_turn_ended(ticket.turn_id, TurnEndFacts(status="cancelled", reason_code="shutdown"))
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
        self.orchestrator.close()
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
        for poller in list(self._pollers):
            poller.cancel()
        pending = list(self._retract_tasks)
        if pending:
            await asyncio.wait(pending, timeout=self._stop_timeout_s)  # 撤回歷史要在 stage 關閉之前做完
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
        with self._intake_lock:
            # 檢查、簽發、排進 loop 都在同一把鎖內：set_session 與 teardown 取同一把鎖，
            # 因此「已簽 ticket 但 _start_turn 尚未排程」的空窗不存在
            if self._closed or not self._started:
                return IntakeResult(None, "closed")
            if self._switching:
                return IntakeResult(None, "session_switching")  # 不排隊：呼叫端自行重送
            result = self.orchestrator.on_utterance(self._session, fact)
            if result.yield_for is not None:
                # 先讓舊的輪讓位、再交出這次完成的 turn：順序顛倒會把剛簽的新輪也作廢（bot 也是先讓位再餵 middleware）
                self._yield_speaker(result.yield_for)
            self._dispatch_results(result.all_results(), fact)
        self._observe_platform(result.all_results(), fact)  # intake 鎖已釋放：shadow 的 SELECT 不占鎖
        return result

    def submit_tick(self) -> list[IntakeResult]:
        """週期性決策（aggregator 逾時、被動模式插話）。任何非 ES-loop 執行緒；會寫 DB，不可在 ES loop 上呼叫。"""
        if self._es.in_loop_thread():
            raise EchoHostError("submit_tick 會寫 DB，不可在 ES loop 執行緒上呼叫")
        # tick 不可長時間占住 intake 鎖（utterance 的決策不該被它擋住）：拿不到就略過這一次，下一個 tick 再來
        if not self._intake_lock.acquire(timeout=0.05):
            self.metrics.inc("echo.tick_skipped_contended")
            return []
        try:
            if self._closed or not self._started or self._switching:
                return []
            results = self.orchestrator.tick(self._session)
            self._dispatch_results(results, None)
        finally:
            self._intake_lock.release()
        self._observe_platform(results, None)  # intake 鎖已釋放
        return results

    def _observe_platform(self, results: Sequence[IntakeResult], fallback: UtteranceFact | None) -> None:
        """平台脈絡 shadow：對每個實際派出的 ticket 恰好觀察一次；未掛 shadow（預設）時什麼都不做。
        在 intake 鎖外執行，shadow 內部吞掉所有例外。"""
        shadow = self._platform_shadow
        if shadow is None:
            return
        for item in results:
            if item.ticket is not None and (item.fact or fallback) is not None:
                shadow.observe(item.ticket.turn_id, self._session.platform)

    def _dispatch_results(self, results: Sequence[IntakeResult], fallback: UtteranceFact | None) -> None:
        for item in results:
            ticket = item.ticket
            fact = item.fact or fallback
            if ticket is None or fact is None:
                continue
            claimed = getattr(self.orchestrator, "loop_claimed", None)  # 舊式替身 orchestrator 沒有認領機制：視為未認領
            if callable(claimed) and claimed(ticket.turn_id):
                # route=agent_loop 已由 AgentLoop 認領：不送 ES 生成（避免雙重回覆），交付與 turn 終結都由 Run 的 worker 負責
                self.metrics.inc("echo.loop_claimed_skip_es")
                continue
            try:
                self._es.call_soon(self._start_turn, ticket, fact)
            except RuntimeError:
                self._end_unsubmitted(ticket, "shutdown")

    def _yield_speaker(self, speaker_ref: str) -> None:
        """對方又講了新內容（``listener_new_segment``）：他尚未開播的輪讓位；已開播的輪由 coordinator 拒絕。"""
        try:
            self._es.call_soon(self._yield_speaker_on_loop, speaker_ref)
        except RuntimeError:
            return

    def _yield_speaker_on_loop(self, speaker_ref: str) -> None:
        coordinator = self.coordinator
        if coordinator is None:
            return
        for view in coordinator.live_turns():
            if view.owner_id == speaker_ref and not view.audio_started and not view.behind_playing:
                coordinator.void(view.turn_id, "listener_new_segment")

    def _tick_loop(self) -> None:
        interval = max(0.05, self._decision.tick_interval_s)
        ticking = self.needs_tick  # 僅 ttl 的組裝（PassThrough＋非 ticking）不碰 orchestrator.tick／interject
        while not self._tick_stop.wait(interval):
            if ticking:
                try:
                    self.submit_tick()
                except Exception:
                    logger.exception("週期性決策失敗")
                    continue  # tick 失敗不刷新政策快照
            self._refresh_policy_if_due()

    def _start_turn(self, ticket: CoreTicket, fact: UtteranceFact) -> None:
        """ES loop 上：建 TurnRequest 並 submit（立即返回）。"""
        coordinator = self.coordinator
        if coordinator is None:
            self._end_unsubmitted(ticket, "shutdown")
            return
        cancelled = self.orchestrator.cancel_reason_of(ticket.turn_id)
        if cancelled is not None or self.endpoint.lease_auth() != ticket.auth:
            # 簽發之後、進 coordinator 之前已被取消或 Lease 已變：不送進 ES
            self._end_unsubmitted(ticket, cancelled or "presence_lost")
            return
        runtime = self._live[ticket.turn_id] = _TurnRuntime(ticket)
        try:
            coordinator.submit(self._build_request(ticket, fact, runtime))
        except Exception:
            logger.exception("coordinator.submit 失敗：%s", ticket.turn_id)
            if self._live.pop(ticket.turn_id, None) is runtime:
                self._end_unsubmitted(ticket, "upstream_error")
            return
        late = self.orchestrator.cancel_reason_of(ticket.turn_id)
        if late is not None:
            # submit 的空窗裡 Core 已登記取消（撤銷、全域停止）：端點的通知找不到這一輪，這裡補一次，
            # 確保 ES 的原因碼正確、也不白跑生成
            self._cancel_in_es(ticket.turn_id, late)

    def _build_request(
        self, ticket: CoreTicket, fact: UtteranceFact, runtime: _TurnRuntime | None = None
    ) -> TurnRequest:
        guild = self._session.guild_id
        retrieve_kwargs: dict[str, Any] = {
            "actor_id": ticket.actor_id, "audience": ticket.audience_scope, "session_id": ticket.segment_id,
            "shared_turn": guild is not None,
        }
        if self._voice_retrieve_timeout_s is not None:
            retrieve_kwargs["timeout_s"] = self._voice_retrieve_timeout_s  # 語音路徑檢索逾時（逾時本輪不帶記憶）
        # D79：每輪各建一個新的 tool_context dict（不可跨輪共用，隱私）。身分與 audience 由 Core 寫入，
        # CoreToolExecutor 只讀這裡（LLM 的引數不能指定範圍）；member_recall 讀回的投影 id 也寫回這個 dict。
        tool_context: dict[str, Any] = {
            "actor_id": ticket.actor_id, "audience": ticket.audience_scope, "segment_id": ticket.segment_id,
            "ticket_ref": ticket.turn_id,
        }
        if guild is not None:
            retrieve_kwargs["present_uids"] = self.roster.present_uids(guild)
            tool_context["guild_id"] = guild
            tool_context["present_uids"] = list(retrieve_kwargs["present_uids"])
        if self._agent_tool_path():
            # agent 路徑：回合 Task 的事件要與舊補記同一組關聯 id 與 scope（legacy 路徑的 tool_context 不變）
            tool_context["event_scope"] = ticket.event_scope
            tool_context["conversation_id"] = ticket.conversation_id
        if runtime is not None:
            runtime.tool_context = tool_context
        # Core 令 ticket.turn_id、source_turn_id 與 Utterance.turn_id 相等（ES 不一致時只記 warning），
        # 輸入端原 id 放 meta["legacy_ref"]，避免 tracer 與記憶打點落到孤兒 trace
        utterance = Utterance(
            text=ticket.user_text, turn_id=ticket.turn_id, speaker_id=ticket.speaker_ref,
            language=fact.language, confidence=fact.confidence if fact.confidence is not None else 1.0,
            metadata={
                "memory_retrieve_kwargs": retrieve_kwargs, "legacy_ref": fact.legacy_ref, "tool_context": tool_context,
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
            text_filter=PassMarkerFilter(),
            commit_mode=CommitMode(self.output.commit_mode),
            history_spoken_only=self.output.history_spoken_only,  # D82 (b)：只在 eager 下有作用，預設關
            retract_on_yield=self.output.history_spoken_only,
            meta={"core_turn_id": ticket.turn_id},
        )

    def _end_unsubmitted(self, ticket: CoreTicket, reason: str) -> None:
        """已簽 ticket 卻沒有交給 ES（關閉中、submit 失敗）：補一個終結事件，不留孤兒 turn。"""
        if reason != "shutdown":
            self.orchestrator.cancel_turn(ticket.turn_id, reason)  # generation 前進，舊 ticket 的輸出一律被拒
        facts = TurnEndFacts(status="cancelled", reason_code=reason)
        self._enqueue(functools.partial(self.orchestrator.on_turn_ended, ticket.turn_id, facts), "end_unsubmitted")

    # ---- 輸入 stage（可選）----

    def feed(self, speaker_ref: str, pcm: bytes, ts: float | None = None) -> bool:
        """平台 Adapter 餵音訊。Presence 非 ACTIVE 時在 feed 之前丟棄（P-9、V18）；回傳是否交給輸入 stage。"""
        if self._input_stage is None:
            return False
        if self._closed or not self.presence_active:
            self.metrics.inc("echo.feed_dropped_absent")
            return False
        self._input_stage.feed(speaker_ref, pcm, ts)
        return True

    def on_voice_event(self, ev: Any) -> None:
        """輸入 stage 的有聲事件（STT 執行緒）：Presence 非 ACTIVE 或尚未啟動時丟棄，其餘交 coordinator。"""
        coordinator = self.coordinator
        if self._closed or not self.presence_active:
            return
        self._note_voice_activity(ev)
        if coordinator is None:
            return
        # 與 coordinator 的篩選一致（未 done 且未 play_done）：它不會呼叫策略時，由宿主補餵讓累積不中斷
        live = [t for t in coordinator.live_turns() if not _play_done(t)]
        if not live:
            self._yield_policy.on_voiced(ev, [])  # 沒有進行中的輪：coordinator 不會呼叫策略，補餵讓累積不中斷
        coordinator.on_voice_event(ev)

    def _note_voice_activity(self, ev: Any) -> None:
        """有聲事件回報給 aggregator（敘述中保持聆聽以「最後出聲」判斷說話者是否講完）；單聲道事件沒有 speaker_id，
        對應輸入 stage 產生 utterance 時的 ``unknown``。"""
        if not getattr(ev, "is_voiced", False):
            return
        note = getattr(self.orchestrator.aggregator, "note_voice", None)
        if callable(note):
            try:
                note(getattr(ev, "speaker_id", None) or "unknown")
            except Exception:  # 聲音活動回報失敗不得影響 coordinator 的讓位／插話
                logger.exception("aggregator.note_voice 失敗")

    async def _consume_input(self) -> None:
        loop = asyncio.get_running_loop()
        stage = self._input_stage
        restarts = 0
        while True:
            token = self._input_token = CancellationToken()
            try:
                async for utt in stage.open(token):
                    pool = self._intake_pool
                    if pool is None:
                        return
                    await loop.run_in_executor(pool, self._intake_stage_utterance, utt)
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("輸入 stage 的 utterance 流中斷")
                self.metrics.inc("echo.input_failures")
                self._enqueue(functools.partial(self._publish_health, "degraded"), "health_degraded")
                if restarts >= _INPUT_RESTARTS or self._closed:
                    self._input_failed = True  # 不能重啟：started 反映真實狀態
                    self._enqueue(functools.partial(self._publish_health, "unavailable"), "health_unavailable")
                    return
                restarts += 1
                self.metrics.inc("echo.input_restarts")

    def _publish_health(self, status: str) -> None:
        try:
            self.publisher.publish(
                EventDraft(
                    event_type="core.component_health_changed", payload={"ref_id": "echo_input", "status": status}
                ),
                TrustedContext(source=HEALTH_SOURCE, actor_id=ACTOR_ECHO, scope_ref="scope_private_core",
                               persona_id=self.persona_id),
            )
        except Exception as exc:  # noqa: BLE001 - health 事件失敗不得影響輸入
            logger.warning("echo health 事件發布失敗（已忽略）：%s", type(exc).__name__)

    def _intake_stage_utterance(self, utt: Utterance) -> None:
        """執行緒池（單一 worker，保序）：ES 的 Utterance -> UtteranceFact -> submit_utterance。"""
        self._seq += 1
        spoken_s = (utt.ended_at or 0.0) - (utt.started_at or 0.0)  # 音訊長度：敘述中偵測累積說話秒數用
        fact = UtteranceFact(
            speaker_ref=utt.speaker_id or "unknown", text=utt.text, seq=self._seq, language=utt.language,
            confidence=utt.confidence, legacy_ref=utt.turn_id,
            duration_ms=int(spoken_s * 1000) if spoken_s > 0 else None,
            # V18：來源由輸入 stage 自己宣告；Host 不用自己的 location 代簽（否則缺席端輸入會被蓋成本機而放行）
            source_location=getattr(self._input_stage, "source_location", None),
            source_endpoint_id=getattr(self._input_stage, "source_endpoint_id", None),
            from_voice=True,  # 輸入 stage 辨識出來的句子：分流 aggregator 只緩衝這條路徑
        )
        try:
            self.submit_utterance(fact)
        except Exception:
            logger.exception("submit_utterance 失敗")

    # ---- session ----

    def _busy(self) -> bool:
        """進行中或「已簽 ticket、_start_turn 尚未執行」的輪都算忙碌。"""
        coordinator = self.coordinator
        return bool(self.orchestrator.live_tickets()) or (
            coordinator is not None and bool(coordinator.live_turns())
        )

    def set_session(self, ctx: SessionContext) -> None:
        """切換 session（任何非 loop 執行緒，阻塞到套用完成）；audience／conversation 改變時走固定重置流程。

        忙碌檢查與 ``_switching`` 旗標在 ``_intake_lock`` 內一次完成；切換期間 ``submit_utterance`` 回
        ``session_switching``（不排隊），因此不會有 ticket 以舊 audience 簽發後在新 session 下執行。"""
        with self._intake_lock:
            if self._switching:
                raise SessionBusyError("另一個 session 切換進行中")
            if self._busy():
                raise SessionBusyError("仍有進行中的輪，不可切換 audience")
            self._switching = True
        try:
            if self._es.running:
                self._es.run(self._switch_session(ctx), timeout=30.0)
            else:
                self._assign_session(ctx)
        finally:
            self._switching = False

    def _assign_session(self, ctx: SessionContext) -> None:
        self._session = ctx
        if self._memory is not None:
            self._memory.set_session(ctx.segment_id)
        self._refresh_tools()

    def _sync_tools(self) -> None:
        """Lease 取得／遺失後重投影工具曝光。檢查與重投影都在 ``_intake_lock`` 內（期間不會簽新 ticket）；
        忙碌（有進行中的輪）或 session 切換中時不跳過，而是記為待辦，由輪結束（``_finish_turn``）再補做。"""
        with self._intake_lock:
            if self._closed:
                return
            if self._switching or self._busy():
                self._tools_refresh_pending = True
                return
            self._tools_refresh_pending = False
            try:
                self._refresh_tools()
            except Exception:
                logger.exception("Lease 變動後重投影工具失敗")

    def _refresh_tools(self) -> None:
        """D79：曝光給 LLM 的工具 = Access Layer 白名單內的工具；``member_recall`` 只在有 guild 的 session 曝光
        （只有那裡才有成員名單可解析）。只在沒有進行中的輪時呼叫（建構、session 切換）。"""
        memory = self._memory
        if memory is None or self.tool_executor is None:
            return
        if getattr(self, "_tool_path", "legacy") == "agent":  # 注入值；未注入（舊式替身）視為 legacy
            self._refresh_tools_agent()
            return
        definitions = getattr(memory, "tool_definitions", None)
        try:
            tools = list(definitions()) if callable(definitions) else []
        except Exception:
            logger.exception("讀取記憶工具定義失敗")
            tools = []
        # 場域條件取自註冊表的 scope.requires_context（member_recall 需要 guild 成員名單），不再硬編碼工具名
        available = frozenset({GUILD_ROSTER_CONTEXT}) if self._session.guild_id is not None else frozenset()
        registry = memory_registry()
        self.think.tools = [
            d for d in tools
            if _tool_name(d) in TOOL_CLASSES and registry.entry(_tool_name(d)).scope.requires_context <= available
        ]

    # direct_chat 回合只曝光唯讀記憶工具（Q-E）：標籤取自決策層 route 預設表（目前為 memory.read），
    # 寫入類（memory.write：force_remember、關於我）不在其內，只在 agent_loop 的標籤集合內才可能投出（切片 E3）；
    # 表內標籤是否合法由組裝層以 CAPABILITY_CEILING 驗證
    _DIRECT_CHAT_CAPABILITY_TAGS = frozenset(ROUTE_DEFAULT_CAPABILITIES[ROUTE_DIRECT_CHAT])

    def _refresh_tools_agent(self) -> None:
        """``UEP_TOOL_PATH=agent``：曝光 = 註冊表對 direct_chat 標籤的 ``project``，經 ``render_tools`` 渲染（D60）。
        投影失敗 fail-closed：不曝光任何工具。"""
        available = frozenset({GUILD_ROSTER_CONTEXT}) if self._session.guild_id is not None else frozenset()
        try:
            registry = getattr(self, "_tool_registry", None) or memory_registry()
            capability_set = registry.project(
                self._DIRECT_CHAT_CAPABILITY_TAGS,
                ProjectionContext.for_platform(
                    self.projection_platform(), available_context=available, audience=self._session.audience_scope,
                ),
            )
            self.think.tools = render_tools(capability_set)
        except Exception:
            logger.exception("註冊表投影失敗，不曝光工具")
            self.think.tools = []

    async def _switch_session(self, ctx: SessionContext) -> None:
        old = self._session
        if self._busy():
            raise SessionBusyError("仍有進行中的輪，不可切換 audience")
        changed = old.audience_scope != ctx.audience_scope or old.conversation_id != ctx.conversation_id
        if changed:
            self.orchestrator.clear_aggregation()  # 舊 audience 的待合併文字不得併進新 session
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

    def participant_joined(
        self, speaker_ref: str, *, role: str = "speaker", session: SessionContext | None = None
    ) -> None:
        self._participant("conversation.participant_joined", speaker_ref, role, session, joined=True)

    def participant_left(
        self, speaker_ref: str, *, role: str = "speaker", session: SessionContext | None = None
    ) -> None:
        self._participant("conversation.participant_left", speaker_ref, role, session, joined=False)
        if self._es.running:
            self._es.call_soon(self._discard_pending, speaker_ref)  # 成員離場：待注入記憶要丟棄（隱私）

    def _participant(
        self, event_type: str, speaker_ref: str, role: str, session: SessionContext | None, *, joined: bool
    ) -> None:
        """``session`` 是這個事件所屬的 session（預設目前的）；呼叫端若持有事件發生當時的 session 應明確傳入，
        不要讓切換之後的 guild 套到舊事件上。"""
        ctx = session or self._session
        if not _SPEAKER_REF.fullmatch(speaker_ref or "") or not _ROLE.fullmatch(role or ""):
            self.metrics.inc("echo.invalid_participant")
            return
        platform, _, uid = speaker_ref.partition(":")
        if ctx.guild_id is not None:
            if platform == ctx.platform and uid:
                (self.roster.join if joined else self.roster.leave)(ctx.guild_id, uid)
            else:
                # roster 以平台 uid 為鍵（不含前綴）：平台前綴與 session 不符就不更新，避免跨平台同 uid 碰撞
                self.metrics.inc("echo.participant_platform_mismatch")
        if not joined:
            self.orchestrator.on_participant_left(speaker_ref)  # 離場者留在 buffer 的文字不再被決策使用
        self._sync_participants(ctx)  # 人數影響耦合度與主動／被動，先於快照推送（模式翻轉會另推一版）
        self.policy.publish()  # 參與者變動時推送新版快照
        draft = EventDraft(
            event_type=event_type, conversation_id=ctx.conversation_id, segment_id=ctx.segment_id,
            payload={"speaker_ref": speaker_ref, "role": role},  # speaker_ref 保留平台前綴
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
            on_reject=self._on_reject, on_discard=self._on_discard, on_progress=self._on_progress, context=ticket,
        )
        runtime = self._live.get(ticket.turn_id)
        if runtime is not None:
            runtime.wrappers.append(wrapper)
        self._wrappers.setdefault(ticket.turn_id, []).append(wrapper)
        while len(self._wrappers) > 64:
            self._wrappers.popitem(last=False)
        self._start_poller(wrapper)
        return wrapper  # type: ignore[return-value]

    def _start_poller(self, wrapper: AdmittingSink) -> None:
        """ES loop 上：輕量輪詢播放進度（間隔為設定值）；TTS 比播放快時沒有後續 write 可觸發。"""
        interval = self.output.progress_poll_interval_s
        if interval <= 0:
            return
        try:
            task = asyncio.ensure_future(self._poll_progress(wrapper, interval))
        except RuntimeError:
            return
        self._pollers.add(task)
        task.add_done_callback(self._pollers.discard)

    async def _poll_progress(self, wrapper: AdmittingSink, interval: float) -> None:
        try:
            while not wrapper.stopped and not wrapper.progress_done and wrapper.turn_id in self._live:
                await asyncio.sleep(interval)
                wrapper.check_progress()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("播放進度輪詢失敗")

    def _admit(self, ticket: CoreTicket) -> GateDecision:
        """第二次接納（本地、只讀記憶體）：Lease 與 TTL 由端點判斷，再比對 turn_generation。"""
        decision = self.endpoint.admit_output(ticket.auth, ticket.turn_generation, ticket.valid_until)
        if decision is not GateDecision.ADMIT:
            return decision
        current = self.generations.get(ticket.turn_id)
        if current is None or current != ticket.turn_generation:
            return GateDecision.REJECT_STALE  # 取消／取代後的舊 generation（V12）
        services = self._services
        provider = services.presence_audience if services is not None else None
        if callable(provider):
            allowed = provider()  # 與 Gate 同一個 provider；須是只讀記憶體的 callable（在 ES loop 上被呼叫）
            if allowed is None or scope_rank(ticket.audience_scope) > scope_rank(allowed):
                return GateDecision.REJECT_AUDIENCE
        return GateDecision.ADMIT

    async def _on_think_start(self, ticket: CoreTicket, view: Any, attempt_id: int) -> None:
        """C5：attempt 開始由 ``TurnRequest.on_think_start`` 觀測；hook 只能同步入佇列（ES 預設逾時 10 秒）。"""
        # 提議在 ES 真的開始 think 時才算被採用：回饋 proposer（dropped_busy、void、讓位的輪不會走到這裡）
        self._enqueue(functools.partial(self.orchestrator.on_think_started, ticket.turn_id), "think_started")
        if attempt_id >= 2:
            runtime = self._live.get(ticket.turn_id)
            if runtime is not None:
                runtime.tool_context.pop("member_recall_ids", None)  # 丟棄舊回覆：它看到的投影不再算數
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
        started = time.perf_counter()  # D73：首 chunk 已寫入 sink 的時刻，dispatch 完成時量增量
        self._enqueue(functools.partial(self._dispatch, wrapper, intent, index, started), "dispatch_intent")

    def _on_progress(self, wrapper: AdmittingSink, items: list[SentenceEvidence]) -> None:
        """播放進度（ES loop 上，只入佇列）：已完整播畢的句子即時回報證據，不必等整輪結束（O-16）。"""
        self._enqueue(functools.partial(self._report_evidence, wrapper, items), "playback_progress")

    def _on_reject(self, wrapper: AdmittingSink, decision: GateDecision) -> None:
        ticket: CoreTicket = wrapper.context
        self._enqueue(functools.partial(self._publish_rejection, ticket, wrapper, decision), "output_rejected")
        self._refuse_turn(ticket.turn_id, "admission_rejected")

    def _on_discard(self, wrapper: AdmittingSink) -> None:
        self.metrics.inc("echo.discarded_chunks")
        self.discarded_by_turn[wrapper.turn_id] += 1

    def _on_turn_ended(self, outcome: Any) -> None:
        """每個 submit 恰好一次（含 DROPPED_BUSY／VOIDED）。只做記憶體計算並入佇列。"""
        runtime = self._live.pop(outcome.turn_id, None)
        if runtime is None:
            return
        facts = facts_from_outcome(outcome)
        if self.orchestrator.cancel_reason_of(outcome.turn_id) in RETRACT_REASONS:
            # D5：被拒、撤銷、停止的輪一律整輪撤出歷史；ES 沒有確認已撤回（認領落在 commit 之後、撤回失敗、
            # 或 outcome 不是這次的宿主取消）就自行補一次，失敗要看得到
            if facts.history_retract_error:
                self.metrics.inc("echo.history_retract_error")
                logger.warning("ES 回報撤回歷史失敗：%s", facts.history_retract_error.split(":", 1)[0])
            if facts.history_retracted is not True:
                self._start_retract(outcome.turn_id)
        evidence: list[tuple[AdmittingSink, list[SentenceEvidence]]] = []
        for wrapper in runtime.wrappers:
            try:
                evidence.append((wrapper, wrapper.collect_evidence(finished=outcome.status is OutcomeStatus.COMPLETED)))
            except Exception:
                logger.exception("計算播放範圍失敗")
        self._enqueue(
            functools.partial(self._finish_turn, runtime.ticket, facts, evidence, runtime.tool_context), "finish_turn"
        )

    # ---- 工作緒（可寫 DB）----

    def _enqueue(self, fn: Callable[[], Any], label: str = "job") -> None:
        if self._worker.submit(fn, label):
            return
        try:
            fn()  # 工作緒已停（關閉的收尾）：就地執行，不遺失終結事件
        except Exception:
            logger.exception("收尾任務失敗：%s", label)

    def _dispatch(
        self, wrapper: AdmittingSink, intent: OutputIntent, index: int, started: float | None = None
    ) -> None:
        services = self._services
        assert services is not None
        delivery = services.delivery
        try:
            record = delivery.prepare(intent)
            if record.status is DS.GENERATED:
                record = delivery.mark_synthesized(record.delivery_id)
            record = delivery.dispatch(intent)
        except Exception:
            # fail-closed：dispatch 任一步失敗就比照被拒處理，不能讓這一輪在沒有 delivery 紀錄下繼續出聲
            logger.exception("intent dispatch 失敗：%s#%s", intent.output_id, index)
            self.metrics.inc("echo.dispatch_errors")
            self._refuse_turn(wrapper.turn_id, "dispatch_failed")  # 工作緒上直接取消（兩者都執行緒安全）
            return
        wrapper.delivery_ids[index] = record.delivery_id
        if started is not None:
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            self.dispatch_ms_max = max(self.dispatch_ms_max, elapsed_ms)
            if elapsed_ms > self.output.dispatch_budget_ms:
                self.metrics.inc("echo.dispatch_over_budget")  # 只記錄，不丟棄也不降級（S6 量測後校正預算）
        if record.status in _DISPATCH_REFUSED:
            # Gate 或端點二次接納拒絕：delivery 已由 Manager 記為 rejected／cancelled
            self.metrics.inc("echo.dispatch_refused")
            self._refuse_turn(wrapper.turn_id, "dispatch_refused")

    def _refuse_turn(self, turn_id: str, reason: str) -> None:
        """輸出被拒、dispatch 失敗：Core 登記取消原因（generation 前進，終結事件為 turn.cancelled），
        並以 ES ``cancel_turn(commit="none")`` 整輪取消與撤回歷史（D5）。"""
        self.orchestrator.cancel_turn(turn_id, reason)
        self._cancel_in_es(turn_id, reason)

    def _cancel_in_es(self, turn_id: str, reason: str) -> bool:
        """任何執行緒：ES 整輪取消（同步停 sink、不看 barge_in 旗標、不連帶取消排隊的輪）。

        回 True：保證這次 outcome 是宿主取消（reason_code 隨 outcome 回來）；回 False：輪已結束、尚未進
        coordinator 或已被其他取消認領，歷史要自己撤回（``think.retract``）。"""
        coordinator = self.coordinator
        if coordinator is None:
            return False
        try:
            claimed = coordinator.cancel_turn(turn_id, reason_code=reason[:_REASON_CODE_MAX], commit="none")
        except ValueError:
            logger.warning("cancel_turn 參數不合法：%s", reason[:_REASON_CODE_MAX])
            claimed = False
        if claimed:
            self.metrics.inc("echo.cancel_turn_claimed")
            return True
        self.metrics.inc("echo.cancel_turn_missed")
        logger.debug("cancel_turn 未認領（%s，%s）：改自行撤回歷史", turn_id, reason)
        self._schedule_retract(turn_id)
        return False

    def _schedule_retract(self, turn_id: str) -> None:
        try:
            self._es.call_soon(self._start_retract, turn_id)
        except RuntimeError:
            self.metrics.inc("echo.history_retract_skipped")  # ES loop 已停：沒有 think 可呼叫

    def _start_retract(self, turn_id: str) -> None:
        """ES loop 上：排一個撤回任務（``think.retract`` 是 coroutine，冪等、重複呼叫無害）。"""
        task = asyncio.ensure_future(self._retract(turn_id))
        self._retract_tasks.add(task)
        task.add_done_callback(self._retract_tasks.discard)

    async def _retract(self, turn_id: str) -> None:
        retract = getattr(self.think, "retract", None)
        if retract is None:
            self.metrics.inc("echo.history_retract_failed")
            return
        try:
            ok = await retract(turn_id)
        except Exception as exc:  # noqa: BLE001 - 撤回失敗只記 metric 與例外類別，不影響回合
            self.metrics.inc("echo.history_retract_failed")
            logger.warning("think.retract 失敗：%s", type(exc).__name__)
            return
        self.metrics.inc("echo.history_retracted" if ok is True else "echo.history_retract_failed")

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
        self, ticket: CoreTicket, facts: TurnEndFacts, evidence: list[tuple[AdmittingSink, list[SentenceEvidence]]],
        tool_context: dict[str, Any] | None = None,
    ) -> None:
        for wrapper, items in evidence:
            self._report_evidence(wrapper, items)
        # 被拒、撤銷、停止的輪不寫記憶（與整輪撤回歷史一致）
        rejected = self.orchestrator.cancel_reason_of(ticket.turn_id) in RETRACT_REASONS
        self.orchestrator.on_turn_ended(ticket.turn_id, facts)
        self._tool_seq.pop(ticket.turn_id, None)
        if self._tools_refresh_pending:
            self._sync_tools()  # 該輪已從 orchestrator 移除；仍有其他輪時會再延後
        if not rejected:
            self._store_memory(ticket, facts, tool_context or {})  # 被拒的輪不寫記憶

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

    def store_turn_memory(self, ticket: CoreTicket, facts: TurnEndFacts) -> None:
        """route=agent_loop 的 turn 收尾（由 LoopLauncher 呼叫）寫記憶：與 direct_chat 同一條 ``_store_memory``，
        agent_loop 沒有 member_recall 的工具脈絡所以不帶額外欄位。"""
        self._store_memory(ticket, facts, {})

    # ---- 續跑結果回 Host 的入口（D107）----

    RESUME_SETTLED_MAX = 1024
    RESUME_VALID_S = 60

    def accept_resumed_result(self, result: ResumedRunResult) -> ResumeDisposition:
        """park → 核准 → resume 的 Run 到達終態後，唯一回 Host 的入口（由 ``LoopLauncher`` 注入呼叫，不對外開放）。

        以 ``(run_id, worker_epoch)`` 為冪等鍵：同鍵再呼叫回原處置，不重送、不重寫。COMPLETED 且有文字才送：activity 綁定的
        ``OutputIntent``（turn_* 為 None），授權於送出當下重取 ``lease_auth()``，經 DeliveryManager／Output Gate。
        送達後才寫記憶（key 掛回原 turn 的 ``turn:<orig>``），``cause=REJECTED`` 不寫。Gate 拒絕本階段一律 DROPPED
        （STALE 先重取 Lease 重試一次）；UNKNOWN／ENQUEUED 不重送。"""
        key = (result.run_id, result.worker_epoch)
        with self._resume_lock:
            settled = self._resume_settled.get(key)
            if settled is not None:
                return settled
            try:
                disposition = self._deliver_resumed(result)
            except Exception:
                logger.exception("續跑結果送出失敗：run=%s", result.run_id)
                self.metrics.inc("echo.resume_errors")
                disposition = ResumeDisposition.UNCERTAIN  # 不知道送到哪一步：不重送、不寫記憶
            self._resume_settled[key] = disposition
            while len(self._resume_settled) > self.RESUME_SETTLED_MAX:
                self._resume_settled.popitem(last=False)
            return disposition

    def _deliver_resumed(self, result: ResumedRunResult) -> ResumeDisposition:
        if result.outcome is not ResumeOutcome.COMPLETED:
            return ResumeDisposition.NOT_APPLICABLE  # FAILED／CANCELLED：不送、不寫（失敗訊息屬後續 PR）
        services = self._services
        if services is None or result.activity_id is None or not result.text.strip():
            self.metrics.inc("echo.resume_dropped.unavailable")
            return ResumeDisposition.DROPPED
        ticket = result.ticket
        for attempt in (1, 2):  # STALE 時重取 Lease 重試一次
            auth = self.endpoint.lease_auth()  # 送出當下重取；不使用 ticket.auth（原 turn 的授權已作廢）
            if auth is None:
                self.metrics.inc("echo.resume_dropped.no_lease")
                return ResumeDisposition.DROPPED
            output_id = new_id("out")
            ref = f"resume:{result.run_id}:{result.worker_epoch}:{attempt}"
            self.loop_text.put(ref, ticket.turn_id, result.text)
            intent = OutputIntent(
                output_id=output_id, sequence=0, kind=OutputKind.TEXT, origin=OutputOrigin.PERSONA,
                persona_id=auth.persona_id, endpoint_id=auth.endpoint_id, lease_id=auth.lease_id,
                fencing_epoch=auth.fencing_epoch, core_epoch=auth.core_epoch, presence_generation=auth.presence_generation,
                activity_id=result.activity_id, audience_scope=ticket.audience_scope, content_ref=ref,
                valid_until=datetime.now(UTC) + timedelta(seconds=self.RESUME_VALID_S),
            )
            services.delivery.prepare(intent)
            record = services.delivery.dispatch(intent)
            status = record.status
            if status in (DS.SENT, DS.ACCEPTED, DS.RENDERED_PLAYED):
                if result.cause is ResumeCause.APPROVED:  # 送達後才寫；REJECTED 不寫
                    self.store_turn_memory(ticket, TurnEndFacts(
                        status="completed", generation_ok=True, generated_len=len(result.text), spoken_text=result.text,
                        snapshot_version=ticket.snapshot_version,
                    ))
                return ResumeDisposition.DELIVERED
            if status in (DS.UNKNOWN, DS.ENQUEUED):
                return ResumeDisposition.UNCERTAIN  # 可能已送出：不重送、不寫
            code = str((record.failure or {}).get("code", status.value))
            if code == GateDecision.REJECT_STALE.value and attempt == 1:
                self.metrics.inc("echo.resume_stale_retry")
                continue
            self.metrics.inc(f"echo.resume_dropped.{code.lower()}")
            return ResumeDisposition.DROPPED
        return ResumeDisposition.DROPPED

    def _store_memory(self, ticket: CoreTicket, facts: TurnEndFacts, tool_context: dict[str, Any]) -> None:
        """Q8：記憶寫入由 Core 依 outcome 自行 store（ES 的 think.memory 不寫入）；冪等鍵由 turn_id 衍生。
        ``member_recall`` 讀回的投影 id 併入 ``shared_context_ids``（ES #18 的約定）。"""
        if self._memory is None or not facts.generation_ok or not facts.spoken_text:
            return
        extra: dict[str, Any] = {}
        ids = tool_context.get("member_recall_ids")
        guild = tool_context.get("guild_id")
        if ids and isinstance(guild, str):
            extra = {"shared_context_ids": list(ids), "shared_scope_id": f"guild:{guild}"}
        try:
            self._memory.store(
                actor_id=ticket.actor_id, user_text=ticket.user_text, spoken_text=facts.spoken_text,
                session_id=ticket.segment_id, idempotency_key=f"turn:{ticket.turn_id}", **extra,
            )
        except Exception:
            logger.exception("記憶 store 失敗")

    # ---- 端點停止通知（端點本地執行緒）----

    def _on_endpoint_stop(self, notice: StopNotice) -> None:
        """Lease 撤銷／到期／stop 控制訊號：登記 Core 的原因（generation 前進，舊輸出一律被拒）並以 ES
        ``cancel_turn`` 逐輪整輪取消（同步停 sink、撤回歷史；原因碼隨 outcome 回來，不需覆寫）。

        scope=turn 的 stop 以「<= 目標 generation」判斷，與端點的 ``_stopped_turn``（較舊回合的輸出一律被擋）
        一致：被涵蓋的較舊 live turn 反正會被第二次接納拒絕，這裡直接以 ``control_stop`` 取消比事後
        ``admission_rejected`` 乾淨。全域（generation 為 None）涵蓋 coordinator 內所有 live 輪（含排隊中）。
        ``cancel_all`` 只留給 ``aclose``（shutdown）。"""
        coordinator = self.coordinator
        if notice.reason == "global_stop":
            self._stopped_reason = "global_stop"
        gen = notice.turn_generation
        targets = [t.turn_id for t in self.orchestrator.live_tickets() if gen is None or t.turn_generation <= gen]
        for turn_id in targets:
            self.orchestrator.cancel_turn(turn_id, notice.reason)
        if notice.reason == "presence_lost":
            self._sync_tools()  # Lease 遺失：平台限定工具不得殘留在 think.tools（輪還在取消中時延後到輪結束）
        if coordinator is None:
            return
        if gen is None:
            known = set(targets)
            targets += [v.turn_id for v in coordinator.live_turns() if v.turn_id not in known]
        # 由新到舊：排隊中的輪先被認領，隊首的輪取消後釋放 think 鎖時，後面的輪已是宿主取消，不會趁隙開始生成
        for turn_id in reversed(targets):
            self._cancel_in_es(turn_id, notice.reason)

    # ---- D79：工具呼叫事後補記 ----

    def _agent_tool_path(self) -> bool:
        """agent 路徑：工具呼叫走 ActionIntent，``action.*`` 由 TaskManager 發出（與舊補記互斥）。值由建構參數注入。"""
        return self._tool_path == "agent"

    def _record_tool_action(self, turn_id: str, name: str, tool_class: str, status: str) -> None:
        """``CoreToolExecutor`` 的回呼（ES loop 上，只入佇列）：事後補記 ``action.executed``／``action.unknown``。

        agent 路徑下不補記（``action.*`` 已由 TaskManager 發出），避免重複發送。"""
        if self._agent_tool_path():
            return
        self._enqueue(functools.partial(self._publish_tool_action, turn_id, name, tool_class, status), "tool_action")

    def _publish_tool_action(self, turn_id: str, name: str, tool_class: str, status: str) -> None:
        ticket = self.orchestrator.ticket_of(turn_id)
        if ticket is None:
            self.metrics.inc("echo.tool_action_orphan")
            return
        self._tool_seq[turn_id] += 1
        draft = EventDraft(
            event_type="action.unknown" if status == "unknown" else "action.executed",
            conversation_id=ticket.conversation_id, segment_id=ticket.segment_id, turn_id=turn_id,
            idempotency_key=f"toolcall:{turn_id}:{self._tool_seq[turn_id]}",
            # 只有分類、結果碼與工具名；引數與結果原文一律不進 journal
            payload={"kind": tool_class, "status": status, "ref_id": f"tool:{name}"},
        )
        trusted = TrustedContext(
            source=SOURCE_ECHO, actor_id=ACTOR_ECHO, scope_ref=ticket.event_scope, persona_id=self.persona_id
        )
        self.publisher.publish(draft, trusted)
