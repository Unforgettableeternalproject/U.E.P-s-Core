"""Console 的 Core 組裝：資料庫連線與 fake 模式的 ``assemble_core`` + ``EchoPipelineHost``。"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import inspect, text
from sqlalchemy.orm import Session, sessionmaker

from uep.assembly import CoreAssembly, assemble_core
from uep.control.channel import InProcessControlChannel
from uep.db.engine import make_engine, make_session_factory
from uep.decision.log import DecisionLogWriter
from uep.echo.host import EchoPipelineHost
from uep.events.bus import EventBus
from uep.events.publisher import EventPublisher
from uep.fakes.stream.stages import FakeSpeakStage, FakeThinkStage
from uep.fakes.tool import FakeToolExecutor
from uep.fakes.tool_spec import FakeToolSpecResolver
from uep.ids import new_id
from uep.memory.identity import IdentityDirectory
from uep.presence.epoch import HostEpochStore
from uep.stream.io.sinks import NullSink, WavFileSink
from uep.stream.tracer import LatencyTracer
from uep.turn.models import SessionContext

logger = logging.getLogger(__name__)

MODES = ("fake", "real")
THINKS = ("fake", "real")
TTS_CHOICES = ("fake", "real")
STT_CHOICES = ("fake", "real")
MEMORY_CHOICES = ("fake", "real")
PRIVATE_SCOPE = "scope_private_owner"
CONSOLE_SPEAKER = "console:owner"
CONSOLE_ACTOR = "actor_a"
DEFAULT_REPLY = "你好，我是回聲。今天天氣不錯。"


class ConsoleError(RuntimeError):
    """使用者可讀的啟動／指令錯誤（訊息已是繁體中文，不含連線字串）。"""


def connect_database(url: str | None = None) -> sessionmaker[Session]:
    """連到開發機 PostgreSQL（D105 Q6）；連線字串沿用 ``UEP_DATABASE_URL``。連不上或未遷移時拋 ``ConsoleError``。"""
    try:
        engine = make_engine(url)
    except RuntimeError as exc:
        raise ConsoleError(f"{exc}。Console 需要開發機 PostgreSQL，請設定 UEP_DATABASE_URL。") from exc
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            tables = set(inspect(conn).get_table_names())
    except Exception as exc:  # noqa: BLE001 - 錯誤訊息可能帶連線資訊，只回報類型
        engine.dispose()
        raise ConsoleError(f"無法連線 PostgreSQL（{type(exc).__name__}）。請確認資料庫已啟動且 UEP_DATABASE_URL 正確。") from exc
    if "presence_current" not in tables:
        engine.dispose()
        raise ConsoleError("資料庫尚未遷移，請先執行 alembic upgrade head。")
    return make_session_factory(engine)


class ConsoleWavSink(WavFileSink):
    """把一個 turn 的音訊寫成 ``<session_dir>/<turn_id>/<n>.wav``；播畢或被停時落地。

    ``player`` 給定時（``--play``）同時串流到本機播放裝置，並改以即時進度計播放量（barge-in 善後依實際播出量）；
    被停時立即中止播放。
    """

    def __init__(self, path: Path, on_flush: Any, player: Any | None = None) -> None:
        super().__init__(path, realtime=player is not None)
        self._on_flush = on_flush
        self._player = player

    async def write(self, chunk: Any) -> None:
        was_stopped = self._stopped
        await super().write(chunk)
        if self._player is not None and not was_stopped:
            await asyncio.to_thread(self._player.play, chunk.pcm, chunk.sample_rate)

    async def drain(self) -> None:
        await super().drain()
        self._land()

    def stop(self) -> None:
        if self._player is not None:
            self._player.stop()
        super().stop()
        self._land()

    def _land(self) -> None:
        written = self.flush()
        if written is not None:
            self._on_flush(self, written)


@dataclass
class ConsoleRuntime:
    """組裝完成的 Core 與觀測點；``close`` 關閉 Core 與資料庫。"""

    mode: str
    core: CoreAssembly
    host: EchoPipelineHost
    session_factory: sessionmaker[Session]
    think: Any
    speak: Any
    tracer: LatencyTracer
    session_dir: Path
    wavs: dict[str, list[Path]] = field(default_factory=dict)
    wav_sinks: list[ConsoleWavSink] = field(default_factory=list)
    seq: int = 0
    # real 模式：真實 STT 輸入 stage（None＝fake STT）、記憶 Host 與資料根、各元件 warmup 耗時（秒）、STT 路徑的提交紀錄
    input_stage: Any = None
    memory_host: Any = None
    memory_root: Path | None = None
    warmups: dict[str, float] = field(default_factory=dict)
    submissions: list[tuple[Any, Any]] = field(default_factory=list)
    # 語音路徑的接收事件（依發生順序）：("utterance", fact, result)＝STT 辨識出一句並交給 Host；
    # ("turn", fact, result)＝聚合器逾時後由 tick 交出的 turn（fact.text 為合併後全文，插話時 fact 可能為 None）。有 aggregator 時 utterance 常回 buffered
    intake_events: list[tuple[str, Any, Any]] = field(default_factory=list)
    player: Any = None
    decision_log: DecisionLogWriter | None = None  # DL-3：決策日誌寫入器（停用時為 None）
    tool_path: str = "legacy"  # 組裝的工具路徑（agent 時才有 loop_switch 與 model_port）
    loop_switch: Any = None  # agent 路徑：``/loop`` 的一次性 route 開關（None＝非 agent 路徑）
    model_port: Any = None  # agent 路徑：驅動 AgentThinkStage 與 AgentLoop 的 ModelPort
    behavior_driver: Any = None  # PP-3b：Behavior driver（只在 real 記憶路徑；off 或 fake 時為 None）
    schedule_planner: Any = None  # PP-4b：PresencePlanner（UEP_SCHEDULE_PLANNER=shadow 且 real 記憶路徑才有）
    presence_arbiter: Any = None  # AR-2：在場請求仲裁 shadow（UEP_PRESENCE_ARBITER=shadow 且 real 記憶路徑才有）

    def close(self) -> None:
        try:
            if self.behavior_driver is not None:
                try:
                    self.behavior_driver.stop()
                except Exception:
                    logger.exception("BehaviorDriver 停止失敗")
            if self.schedule_planner is not None:
                try:
                    self.schedule_planner.stop()
                except Exception:
                    logger.exception("PresencePlanner 停止失敗")
            if self.presence_arbiter is not None:
                try:
                    self.presence_arbiter.stop()
                except Exception:
                    logger.exception("仲裁 shadow 停止失敗")
            self.core.shutdown()  # 含決策日誌寫入器的關閉（排空佇列後才釋放連線）
        finally:
            engine = self.session_factory.kw.get("bind")
            if engine is not None:
                engine.dispose()


def build_fake_runtime(
    session_factory: sessionmaker[Session], session_dir: Path, *, reply: str = DEFAULT_REPLY, with_tts: bool = True,
    start: bool = True, think_factory: Callable[[LatencyTracer], Any] | None = None,
    speak_stage: Any | None = None, start_timeout_s: float | None = None,
    input_stage: Any | None = None, memory_factory: Callable[..., Any] | None = None,
    memory_root: Path | None = None, warmups: dict[str, float] | None = None, player: Any | None = None,
    host_kwargs: dict[str, Any] | None = None, assemble_kwargs: dict[str, Any] | None = None,
    loop_switch: Any | None = None, model_port: Any | None = None,
) -> ConsoleRuntime:
    """fake speak 驅動的 Core：文字 → Coordinator → AdmittingSink → wav。

    預設 think 也是 fake（A0）；``think_factory(tracer)`` 給定時改用該 Think stage（N3：真實 LLM，A1）。
    real 模式另可給 ``input_stage``（真實 STT，音訊經 ``host.feed``）與 ``memory_factory(publisher, identity)``
    （真實記憶 Host，經 Access Layer 注入 Host）；``player`` 給定時 sink 同步本機播放（``--play``）。
    ``host_kwargs``／``assemble_kwargs`` 覆寫 Host／``assemble_core`` 的建構參數（例如 agent 工具路徑的 ``tool_path``、
    ``model_port``、``agent_loop_entry``；預設不傳，行為不變）。``loop_switch`` 給定時（agent 路徑）安裝 ``/loop`` 的
    route 開關；``model_port`` 只記錄在 runtime 上供指令層顯示。
    """
    session_dir = Path(session_dir)
    session_dir.mkdir(parents=True, exist_ok=True)
    bus = EventBus()
    publisher = EventPublisher(session_factory, bus)
    tracer = LatencyTracer()
    if think_factory is not None:
        think = think_factory(tracer)
    else:
        think = FakeThinkStage(
            responses=[reply], first_token_delay_s=0.0, token_interval_s=0.0, memory_delay_s=0.0, tracer=tracer,
        )
    # M5：speak_stage 給定時用真實 Speak（TTS adapter）；Host 啟動逾時需涵蓋模型載入
    speak = speak_stage if speak_stage is not None else FakeSpeakStage(first_chunk_extra_s=0.0, rtf=0.0)
    host_extra: dict[str, Any] = {} if start_timeout_s is None else {"start_timeout_s": start_timeout_s}
    runtime: ConsoleRuntime | None = None
    warmups = warmups if warmups is not None else {}
    identity = IdentityDirectory({CONSOLE_SPEAKER: CONSOLE_ACTOR}, {CONSOLE_ACTOR: "owner"})
    memory_host = memory_factory(publisher, identity) if memory_factory is not None else None

    def on_flush(sink: ConsoleWavSink, path: Path) -> None:
        if runtime is not None:
            turn_wavs = runtime.wavs.setdefault(path.parent.name, [])
            if path not in turn_wavs:
                turn_wavs.append(path)

    def sink_factory(view: Any, ticket: Any):
        if not with_tts:
            return NullSink()  # --no-tts：音訊丟棄，文字回應與 turn 流程照常
        turn_dir = session_dir / ticket.turn_id
        n = len(list(turn_dir.glob("*.wav"))) + 1 if turn_dir.exists() else 1
        sink = ConsoleWavSink(turn_dir / f"{n}.wav", on_flush, player)
        if runtime is not None:
            runtime.wav_sinks.append(sink)
        return sink

    # Console 是本機麥克風／喇叭的桌面端，平台取 desktop（與 endpoint 預設 location 一致）
    session = SessionContext(
        conversation_id=new_id("conv"), segment_id=new_id("seg"), audience_scope=PRIVATE_SCOPE,
        session_ref=f"console{int(time.time() * 1000)}", platform="desktop",
    )
    host = EchoPipelineHost(
        publisher=publisher, think=think, speak=speak, sink_factory=sink_factory, session=session,
        identity=identity, tracer=tracer, endpoint_kwargs={"tick_interval": 0.05}, **host_extra,
        **{"input_stage": input_stage, "memory": memory_host.access if memory_host is not None else None,
           **(host_kwargs or {})},
    )
    if loop_switch is not None:
        from uep.console.agent_rig import install_loop_route

        install_loop_route(host, loop_switch)
    core = assemble_core(
        session_factory, **{
            "bus": bus, "publisher": publisher, "control": InProcessControlChannel(), "adapter": host.adapter,
            "executors": {"fake": FakeToolExecutor()}, "tool_resolver": FakeToolSpecResolver(**{"files.read": {}}),
            "turn_generation_source": host.generation_source, "presence_audience": lambda: PRIVATE_SCOPE,
            "epoch_store": HostEpochStore(session_dir / "host_epoch.json"), "maintenance_interval": None, "echo": host,
            "memory": memory_host,  # 決策日誌由組裝層依 UEP_DECISION_LOG 接上（DL-3）
            **(assemble_kwargs or {}),
        },
    )
    runtime = ConsoleRuntime(
        "fake" if think_factory is None else "real", core, host, session_factory, think, speak, tracer, session_dir,
        input_stage=input_stage, memory_host=memory_host, memory_root=memory_root, warmups=warmups, player=player,
        decision_log=core.decision_log, tool_path=(host_kwargs or {}).get("tool_path", "legacy"),
        loop_switch=loop_switch, model_port=model_port,
    )
    if input_stage is not None:
        runtime.seq = TEXT_SEQ_BASE  # 與 Host 內部 STT utterance 的 seq 錯開，文字與語音混用時不碰撞
        _record_submissions(host, runtime)
    if start:
        try:
            core.start()
            # PP-3b：只有 real_rig 組出的記憶 Host 才有 rest_window_policy 標記；fake 記憶不啟動 driver
            if memory_host is not None and hasattr(memory_host, "rest_window_policy"):
                from uep.config import get_settings
                from uep.echo.schedule_wiring import (
                    attach_behavior_driver,
                    attach_presence_arbiter,
                    attach_schedule_planner,
                )

                runtime.behavior_driver = attach_behavior_driver(
                    get_settings(), core, session_factory=session_factory, rest_policy=memory_host.rest_window_policy)
                runtime.schedule_planner = attach_schedule_planner(
                    get_settings(), core, session_factory=session_factory)
                runtime.presence_arbiter = attach_presence_arbiter(
                    get_settings(), core, session_factory=session_factory, planner=runtime.schedule_planner)
        except BaseException:
            for component in (runtime.behavior_driver, runtime.schedule_planner, runtime.presence_arbiter):
                if component is not None:
                    try:
                        component.stop()
                    except Exception:
                        logger.exception("行程元件停止失敗")
            core.shutdown()
            raise
    return runtime


TEXT_SEQ_BASE = 1_000_000


def _record_submissions(host: EchoPipelineHost, runtime: ConsoleRuntime) -> None:
    """記錄 Host 的每次 ``submit_utterance``（STT 執行緒呼叫）：語音路徑沒有 ticket 回傳值，console 靠它找到新開的 turn。"""
    original = host.submit_utterance

    def recorded(fact: Any) -> Any:
        result = original(fact)
        runtime.submissions.append((fact, result))
        runtime.intake_events.append(("utterance", fact, result))
        return result

    host.submit_utterance = recorded  # type: ignore[method-assign]
    original_tick = host.submit_tick

    def recorded_tick() -> Any:
        results = original_tick()
        for item in results:
            # 聚合器逾時交出的 turn（或被動模式插話）沒有 submit_utterance 回傳值：console 靠這裡找到新開的 turn；
            # 沒有 ticket（PASS／DEFER）的也記錄，讓 console 印出「未開 turn：原因」
            runtime.intake_events.append(("turn", item.fact, item))
        return results

    host.submit_tick = recorded_tick  # type: ignore[method-assign]


def real_think_factory() -> Callable[[LatencyTracer], Any]:
    """真實 Think（API LLM）的 stage 工廠；設定自 ``UepSettings``（``UEP_LLM_*``），缺金鑰拋 ``ConsoleError``。"""
    from uep.config import get_settings
    from uep.stream.adapters.think_config import ThinkConfigError
    from uep.stream.adapters.think_llm import LLMThinkStage, build_backend

    settings = get_settings()
    config = settings.think_config()
    try:
        backend = build_backend(config=config)  # 缺金鑰在此失敗，不等到第一句話
    except ThinkConfigError as exc:
        raise ConsoleError(str(exc)) from exc

    from uep.stream.adapters.think_compress import compression_kwargs

    # 與 Host 路徑共用同一份壓縮接線（開關、缺金鑰退路、視窗預設都一致）
    compress = compression_kwargs(settings.compress_config(), config, backend)

    def factory(tracer: LatencyTracer) -> Any:
        return LLMThinkStage(
            backend, system_prompt=settings.infer_system_prompt, tracer=tracer, config=config, **compress
        )

    return factory


def real_speak_kwargs(env: dict[str, str] | None = None, warmups: dict[str, float] | None = None) -> dict[str, Any]:
    """真實 Speak stage 與 Host 啟動逾時（M5）；TTS 需要 GPU，未設 ``UEP_E2E_ALLOW_GPU=1`` 拒絕（不載任何模型）。

    引擎預設 VOX（``UEP_INFER_TTS_ENGINE`` 明確設了才改）；Host 啟動逾時沿用 ``UEP_INFER_HOST_START_TIMEOUT_S``，
    未設時由 speak／think／input 三個 prepare 逾時加總加餘裕推得（涵蓋 VOX 載入）。
    """
    from uep.config import get_settings
    from uep.console.real_rig import gpu_allowed, host_start_timeout_s, timed_prepare
    from uep.console.tts_cmd import build_speak_stage, default_engine

    if not gpu_allowed(env):
        raise ConsoleError(GPU_REQUIRED_MESSAGE)
    engine = default_engine()
    try:
        stage = build_speak_stage(engine)
    except Exception as exc:  # noqa: BLE001 - 轉成啟動失敗訊息
        raise ConsoleError(f"TTS 後端建立失敗：{type(exc).__name__}：{exc}") from exc
    if warmups is not None:
        timed_prepare(stage, "tts", warmups)
    return {"speak_stage": stage, "start_timeout_s": host_start_timeout_s(get_settings(), engine)}


GPU_REQUIRED_MESSAGE = "真實 STT／TTS／記憶 embedding 需要 GPU：請確認 GPU 空閒（bot 未執行）後設 UEP_E2E_ALLOW_GPU=1，或改用 --tts fake／--stt fake／--memory fake"


def build_runtime(
    mode: str, session_factory: sessionmaker[Session], session_dir: Path, *, think: str | None = None,
    tts: str | None = None, stt: str | None = None, memory: str | None = None, play: bool = False,
    env: dict[str, str] | None = None, tool_path: str | None = None, **kwargs: Any,
) -> ConsoleRuntime:
    """``fake``：全 fake（A0）。``real``：Think、Speak（TTS）、STT、記憶全真實（A1–A5）。

    ``think``、``tts``、``stt``、``memory`` 可各自獨立覆寫（``--think``、``--tts``、``--stt``、``--memory``）：
    fake 模式配 ``--think real`` 即「Think 真實、其餘 fake」，``--mode real --tts fake`` 即「TTS fake、其餘真實」。
    真實 STT／TTS（記憶 embedding 預設 CPU，設成非 cpu 才算）需 GPU 守門（``UEP_E2E_ALLOW_GPU=1``），未設即拒絕、不載任何模型。
    ``play`` 把 TTS 音訊同步送本機播放裝置（需 sounddevice）。

    ``tool_path``（預設取設定 ``UEP_TOOL_PATH``）為 ``agent`` 時：Think 必須是 real，Think 改以真實 ``ModelPort``
    （``kwargs["model_port"]`` 可注入，測試用 FakePort；否則由 ``ThinkConfig`` 建 PydanticAI 埠）組 ``AgentThinkStage``，
    並依 ``UEP_AGENT_LOOP_ENTRY`` 啟用 ``route=agent_loop`` 入口；``/loop`` 可把下一句導向該 route。
    """
    if mode not in MODES:
        raise ConsoleError(f"未知的模式：{mode}（可用：{', '.join(MODES)}）")
    default = "real" if mode == "real" else "fake"
    chosen = think or default
    if chosen not in THINKS:
        raise ConsoleError(f"未知的 think：{chosen}（可用：{', '.join(THINKS)}）")
    chosen_tts = tts or default
    if chosen_tts not in TTS_CHOICES:
        raise ConsoleError(f"未知的 tts：{chosen_tts}（可用：{', '.join(TTS_CHOICES)}）")
    chosen_stt = stt or default
    if chosen_stt not in STT_CHOICES:
        raise ConsoleError(f"未知的 stt：{chosen_stt}（可用：{', '.join(STT_CHOICES)}）")
    chosen_memory = memory or default
    if chosen_memory not in MEMORY_CHOICES:
        raise ConsoleError(f"未知的 memory：{chosen_memory}（可用：{', '.join(MEMORY_CHOICES)}）")
    from uep.config import get_settings
    from uep.console.real_rig import gpu_allowed

    path = tool_path or get_settings().tool_path
    if path not in ("legacy", "agent"):
        raise ConsoleError(f"未知的 tool_path：{path}（可用：legacy、agent）")
    if path == "agent":
        if chosen != "real":
            raise ConsoleError("UEP_TOOL_PATH=agent 需要真實 Think：請加 --think real（或 --mode real）")
        factory = _agent_setup(kwargs, chosen_memory)
    else:
        factory = real_think_factory() if chosen == "real" else None  # 缺金鑰先於 GPU 守門報錯（不需 GPU 的問題先講）
    if ("real" in (chosen_tts, chosen_stt) or (chosen_memory == "real" and _memory_on_gpu())) and not gpu_allowed(env):
        raise ConsoleError(GPU_REQUIRED_MESSAGE)
    warmups: dict[str, float] = kwargs.pop("warmups", None) or {}
    if factory is not None:
        factory = _timed_think(factory, warmups)
    if chosen_tts == "real":
        kwargs.update(real_speak_kwargs(env, warmups))
    if play:
        kwargs["player"] = _build_player()
    if chosen_stt == "real" or chosen_memory == "real":
        from uep.config import get_settings
        from uep.console import real_rig

        settings = get_settings()
        if chosen_stt == "real":
            kwargs["input_stage"] = real_rig.build_stt_stage(settings, warmups)
            if settings.voice_aggregator:
                # 語音路徑接上 BufferedAggregator（文字逐句通過）；與正式組裝同一個建構函式，聚合器每個 runtime 一份
                from uep.echo.inference_wiring import build_voice_aggregator

                aggregator = build_voice_aggregator()
                if aggregator is not None:  # proposer 不支援模式切換時為 None：語音逐句
                    host_kwargs = dict(kwargs.get("host_kwargs") or {})
                    host_kwargs.setdefault("aggregator", aggregator)
                    kwargs["host_kwargs"] = host_kwargs
            if chosen_tts != "real":  # 未接真實 TTS 時 Host 仍需涵蓋 STT 載入的啟動逾時
                kwargs.setdefault("start_timeout_s", real_rig.host_start_timeout_s(settings, real_rig.tts_engine(settings)))
        if chosen_memory == "real":
            root, _default = real_rig.resolve_memory_root(settings)
            root.mkdir(parents=True, exist_ok=True)
            kwargs["memory_root"] = root
            kwargs["memory_factory"] = lambda publisher, identity: real_rig.build_memory_host(
                settings, root, identity, publisher=publisher, session_factory=session_factory, warmups=warmups,
            )
    return build_fake_runtime(session_factory, session_dir, think_factory=factory, warmups=warmups, **kwargs)


def _agent_setup(kwargs: dict[str, Any], memory: str) -> Callable[[LatencyTracer], Any]:
    """agent 工具路徑的組裝參數（就地寫入 ``kwargs``）並回傳 Think 工廠；缺金鑰拋 ``ConsoleError``。"""
    from uep.config import get_settings
    from uep.console.agent_rig import (
        ConsoleFakeMemory,
        LoopRouteSwitch,
        agent_think_factory,
        build_model_port,
    )
    from uep.stream.adapters.think_config import ThinkConfigError
    from uep.tools.catalog.memory import memory_registry

    settings = get_settings()
    config = settings.think_config()
    port = kwargs.pop("model_port", None)
    if port is None:
        try:
            port = build_model_port(config)
        except ThinkConfigError as exc:
            raise ConsoleError(str(exc)) from exc
    host_kwargs = dict(kwargs.pop("host_kwargs", None) or {})
    host_kwargs["tool_path"] = "agent"
    if memory != "real":  # real 記憶由 memory_factory 的 access 注入 Host；fake 記憶給一個行程內 MemoryPort
        host_kwargs["memory"] = ConsoleFakeMemory()
    assemble_kwargs = dict(kwargs.pop("assemble_kwargs", None) or {})
    # agent 路徑的 executor 只有記憶工具（由組裝層併入）；resolver 必須是記憶註冊表（fake 工具集不在 agent 路徑）
    assemble_kwargs.update(executors={}, tool_resolver=memory_registry(), tool_path="agent", model_port=port)
    loop_entry = kwargs.pop("agent_loop_entry", None)  # None＝依 UEP_AGENT_LOOP_ENTRY（由 assemble_core 讀設定）
    if loop_entry is not None:
        assemble_kwargs["agent_loop_entry"] = loop_entry
    kwargs.update(host_kwargs=host_kwargs, assemble_kwargs=assemble_kwargs, loop_switch=LoopRouteSwitch(), model_port=port)
    return agent_think_factory(port, settings, config)


def _memory_on_gpu() -> bool:
    from uep.config import get_settings
    from uep.console.real_rig import memory_device

    return memory_device(get_settings()) != "cpu"


def _timed_think(factory: Callable[[LatencyTracer], Any], warmups: dict[str, float]) -> Callable[[LatencyTracer], Any]:
    from uep.console.real_rig import timed_prepare

    def wrapped(tracer: LatencyTracer) -> Any:
        stage = factory(tracer)
        timed_prepare(stage, "think", warmups)
        return stage

    return wrapped


def _build_player() -> Any:
    from uep.console.audio_io import AudioIoError, SoundDevicePlayer

    try:
        return SoundDevicePlayer()
    except AudioIoError as exc:
        raise ConsoleError(str(exc)) from exc
