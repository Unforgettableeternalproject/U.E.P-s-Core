"""Console 的 Core 組裝：資料庫連線與 fake 模式的 ``assemble_core`` + ``EchoPipelineHost``。"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import inspect, text
from sqlalchemy.orm import Session, sessionmaker

from uep.assembly import CoreAssembly, assemble_core
from uep.control.channel import InProcessControlChannel
from uep.db.engine import make_engine, make_session_factory
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
PRIVATE_SCOPE = "scope_private_owner"
CONSOLE_SPEAKER = "console:owner"
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
    """把一個 turn 的音訊寫成 ``<session_dir>/<turn_id>/<n>.wav``；播畢或被停時落地。"""

    def __init__(self, path: Path, on_flush: Any) -> None:
        super().__init__(path, realtime=False)
        self._on_flush = on_flush

    async def drain(self) -> None:
        await super().drain()
        self._land()

    def stop(self) -> None:
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

    def close(self) -> None:
        try:
            self.core.shutdown()
        finally:
            engine = self.session_factory.kw.get("bind")
            if engine is not None:
                engine.dispose()


def build_fake_runtime(
    session_factory: sessionmaker[Session], session_dir: Path, *, reply: str = DEFAULT_REPLY, with_tts: bool = True,
    start: bool = True,
) -> ConsoleRuntime:
    """fake think／speak 驅動的 Core（A0）：文字 → Coordinator → AdmittingSink → wav。"""
    session_dir = Path(session_dir)
    session_dir.mkdir(parents=True, exist_ok=True)
    bus = EventBus()
    publisher = EventPublisher(session_factory, bus)
    tracer = LatencyTracer()
    think = FakeThinkStage(
        responses=[reply], first_token_delay_s=0.0, token_interval_s=0.0, memory_delay_s=0.0, tracer=tracer,
    )
    speak = FakeSpeakStage(first_chunk_extra_s=0.0, rtf=0.0)
    runtime: ConsoleRuntime | None = None

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
        sink = ConsoleWavSink(turn_dir / f"{n}.wav", on_flush)
        if runtime is not None:
            runtime.wav_sinks.append(sink)
        return sink

    session = SessionContext(
        conversation_id=new_id("conv"), segment_id=new_id("seg"), audience_scope=PRIVATE_SCOPE,
        session_ref=f"console{int(time.time() * 1000)}", platform="web",
    )
    host = EchoPipelineHost(
        publisher=publisher, think=think, speak=speak, sink_factory=sink_factory, session=session,
        identity=IdentityDirectory({CONSOLE_SPEAKER: "actor_a"}, {"actor_a": "owner"}), tracer=tracer,
        endpoint_kwargs={"tick_interval": 0.05},
    )
    core = assemble_core(
        session_factory, bus=bus, publisher=publisher, control=InProcessControlChannel(), adapter=host.adapter,
        executors={"fake": FakeToolExecutor()}, tool_resolver=FakeToolSpecResolver(**{"files.read": {}}),
        turn_generation_source=host.generation_source, presence_audience=lambda: PRIVATE_SCOPE,
        epoch_store=HostEpochStore(session_dir / "host_epoch.json"), maintenance_interval=None, echo=host,
    )
    runtime = ConsoleRuntime("fake", core, host, session_factory, think, speak, tracer, session_dir)
    if start:
        core.start()
    return runtime


def build_runtime(mode: str, session_factory: sessionmaker[Session], session_dir: Path, **kwargs: Any) -> ConsoleRuntime:
    if mode == "fake":
        return build_fake_runtime(session_factory, session_dir, **kwargs)
    if mode == "real":
        raise ConsoleError(
            "real 模式尚未支援：真實 STT／TTS／Think 待 M3／M5／N3 接線後補"
            "（屆時真實 GPU 路徑需 UEP_E2E_ALLOW_GPU=1）。請改用 --mode fake。"
        )
    raise ConsoleError(f"未知的模式：{mode}（可用：{', '.join(MODES)}）")
