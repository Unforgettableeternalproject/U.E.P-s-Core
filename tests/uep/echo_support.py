"""P3 S3 測試共用 rig：以 ES 的 fakes 驅動 EchoPipelineHost（需 PostgreSQL 與已安裝的 echo_stream）。

組出真的 CoreAssembly（Presence、Gate、DeliveryManager、StateCoordinator、journal），ES 端用
``FakeThinkStage``／``FakeSpeakStage``／``FakeAudioSink``，端點 Lease 走真的 PresenceManager（行程內 RPC）。
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from echo_stream.contracts.turn import VoiceEvent, VoiceState
from echo_stream.core.tracer import LatencyTracer
from echo_stream.fakes.audio import FakeAudioSink
from echo_stream.fakes.stages import FakeSpeakStage, FakeThinkStage
from sqlalchemy import select

from uep.assembly import CoreAssembly, assemble_core
from uep.control.channel import InProcessControlChannel
from uep.echo.host import EchoPipelineHost
from uep.events.bus import EventBus
from uep.events.models import EventRow
from uep.events.publisher import EventPublisher
from uep.fakes.tool import FakeToolExecutor
from uep.ids import new_id
from uep.memory.identity import IdentityDirectory
from uep.output.models import DeliveryRow, OutputIntentRow
from uep.presence.epoch import HostEpochStore
from uep.turn.models import IntakeResult, SessionContext, UtteranceFact

PRIVATE = "scope_private_owner"
SPEAKER = "discord:111"
TEXT_TWO_SENTENCES = "你好，我是測試。今天天氣不錯。"
WAIT = 8.0


def make_session(**over: Any) -> SessionContext:
    data: dict[str, Any] = {
        "conversation_id": new_id("conv"), "segment_id": new_id("seg"), "audience_scope": PRIVATE,
        "session_ref": f"sess{int(time.time() * 1000)}", "platform": "web",
    }
    data.update(over)
    return SessionContext(**data)


def make_identity() -> IdentityDirectory:
    return IdentityDirectory({SPEAKER: "actor_a"}, {"actor_a": "owner"})


def fast_think(tracer: LatencyTracer, responses=(TEXT_TWO_SENTENCES,), **over: Any) -> FakeThinkStage:
    kw: dict[str, Any] = {
        "responses": list(responses), "first_token_delay_s": 0.0, "token_interval_s": 0.0,
        "memory_delay_s": 0.0, "tracer": tracer,
    }
    kw.update(over)
    return FakeThinkStage(**kw)


def fast_speak(**over: Any) -> FakeSpeakStage:
    kw: dict[str, Any] = {"first_chunk_extra_s": 0.0, "rtf": 0.0}
    kw.update(over)
    return FakeSpeakStage(**kw)


@dataclass
class Rig:
    core: CoreAssembly
    host: EchoPipelineHost
    session_factory: Any
    think: Any
    speak: Any
    tracer: LatencyTracer
    inner_sinks: list[FakeAudioSink] = field(default_factory=list)
    seq: int = 0

    # ---- 輸入 ----

    def say(self, text: str = "哈囉", speaker: str = SPEAKER, **over: Any) -> IntakeResult:
        self.seq += 1
        fact = UtteranceFact(speaker_ref=speaker, text=text, seq=over.pop("seq", self.seq), **over)
        return self.host.submit_utterance(fact)

    def voice(self, *, speaker: str = SPEAKER, seconds: float = 3.0) -> None:
        """送一個足以觸發 barge-in 的有聲事件。"""
        ev = VoiceEvent(
            state=VoiceState.SPEECH, timestamp=time.perf_counter(), duration_s=seconds, speaker_id=speaker,
            is_voiced=True, chunk_s=seconds,
        )
        self.host.on_voice_event(ev)

    # ---- 觀測 ----

    def events(self, turn_id: str | None = None, types: tuple[str, ...] = ()) -> list[EventRow]:
        with self.session_factory() as s:
            stmt = select(EventRow).order_by(EventRow.journal_seq)
            if turn_id is not None:
                stmt = stmt.where(EventRow.turn_id == turn_id)
            if types:
                stmt = stmt.where(EventRow.event_type.in_(types))
            return list(s.scalars(stmt))

    def kinds(self, turn_id: str) -> list[str]:
        return [e.event_type for e in self.events(turn_id)]

    def deliveries(self) -> list[DeliveryRow]:
        with self.session_factory() as s:
            return list(s.scalars(select(DeliveryRow).order_by(DeliveryRow.sequence)))

    def intents(self, turn_id: str | None = None) -> list[OutputIntentRow]:
        with self.session_factory() as s:
            stmt = select(OutputIntentRow).order_by(OutputIntentRow.sequence)
            if turn_id is not None:
                stmt = stmt.where(OutputIntentRow.turn_id == turn_id)
            return list(s.scalars(stmt))

    def wait_for(self, predicate: Callable[[], bool], timeout: float = WAIT, what: str = "條件") -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.02)
        raise AssertionError(f"等待逾時：{what}")

    def wait_terminal(self, turn_id: str, timeout: float = WAIT) -> list[str]:
        """等該 turn 出現終結事件（completed／cancelled／skipped）並回傳完整事件序列。"""
        terminal = {"turn.completed", "turn.cancelled", "turn.skipped"}
        self.wait_for(lambda: bool(terminal & set(self.kinds(turn_id))), timeout, f"{turn_id} 終結事件")
        self.host._worker.drain(timeout)
        return self.kinds(turn_id)

    def run_on_es(self, coro) -> Any:
        return self.host._es.run(coro, timeout=WAIT)


def build_rig(
    session_factory, tmp_path, *, realtime: bool = False, responses=(TEXT_TWO_SENTENCES,), session=None,
    think=None, speak=None, memory=None, sink_factory=None, start: bool = True, host_kwargs=None,
    presence_audience=lambda: PRIVATE, memory_host=None, bus=None, publisher=None, assemble_over=None,
    **think_over: Any,
) -> Rig:
    bus = bus or EventBus()
    publisher = publisher or EventPublisher(session_factory, bus)
    tracer = LatencyTracer()
    think = think or fast_think(tracer, responses, **think_over)
    speak = speak or fast_speak()
    inner_sinks: list[FakeAudioSink] = []

    def default_sink_factory(view, ticket):
        sink = FakeAudioSink(realtime=realtime)
        inner_sinks.append(sink)
        return sink

    host = EchoPipelineHost(
        publisher=publisher, think=think, speak=speak, sink_factory=sink_factory or default_sink_factory,
        session=session or make_session(), identity=make_identity(), memory=memory, tracer=tracer,
        endpoint_kwargs={"tick_interval": 0.05}, **(host_kwargs or {}),
    )
    core = assemble_core(
        session_factory, bus=bus, publisher=publisher, control=InProcessControlChannel(), adapter=host.adapter,
        executors={"fake": FakeToolExecutor()}, turn_generation_source=host.generation_source,
        presence_audience=presence_audience, epoch_store=HostEpochStore(tmp_path / "host_epoch.json"),
        maintenance_interval=None, echo=host, memory=memory_host, **(assemble_over or {}),
    )
    rig = Rig(core, host, session_factory, think, speak, tracer, inner_sinks)
    if start:
        core.start()
    return rig


class ThreadSafeLog:
    """收集回呼發生的執行緒與耗時（驗證回呼沒有在 ES loop 上做阻塞工作）。"""

    def __init__(self) -> None:
        self.items: list[tuple[str, int, float]] = []
        self._lock = threading.Lock()

    def add(self, name: str, started: float) -> None:
        with self._lock:
            self.items.append((name, threading.get_ident(), time.perf_counter() - started))
