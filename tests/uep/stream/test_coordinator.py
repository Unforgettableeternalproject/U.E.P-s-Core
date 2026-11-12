"""TurnCoordinator（T-07b PR ②）——排隊、取消語意、regen、PASS、commit、跨執行緒。

沿用 ``scenario_support`` 的手動推進零件：沒有任何用來賭時序的 sleep，
狀態推進一律靠 ``ManualSink.play/finish``、``GateSpeak.open``、``until``。
"""

from __future__ import annotations

import asyncio
import dataclasses
import threading
from dataclasses import dataclass, field

import pytest

from uep.stream.adapters.think_llm import LLMThinkStage
from uep.stream.contracts.cancellation import CancelReason
from uep.stream.contracts.output import (
    CommitMode,
    ModeFlags,
    OutcomeStatus,
    PolicySnapshot,
    SkipReason,
    TurnOutcome,
    TurnRequest,
    YieldAction,
)
from uep.stream.contracts.turn import VoiceEvent, VoiceState
from uep.stream.contracts.types import TTS_SAMPLE_RATE, AudioChunk, Sentence, Utterance
from uep.stream import coordinator as coordinator_module
from uep.stream import tracer as marks
from uep.stream.coordinator import TurnCoordinator
from uep.stream.policies import StandaloneTicketIssuer, StaticPolicySource
from uep.stream.tracer import LatencyTracer

from .scenario_support import (
    RESPONSE,
    SENTENCES,
    GateSpeak,
    ManualSink,
    ScriptBackend,
    build_think,
    poll,
    until,
)

S0 = SENTENCES[0]
SAFETY_S = 5.0


def voiced(t: float, speaker: str = "u1", chunk: float = 0.5) -> VoiceEvent:
    return VoiceEvent(
        state=VoiceState.SPEECH, timestamp=t, duration_s=0.0, speaker_id=speaker,
        is_voiced=True, chunk_s=chunk,
    )


@dataclass
class Env:
    co: TurnCoordinator
    think: LLMThinkStage
    backend: ScriptBackend
    speak: GateSpeak
    tracer: LatencyTracer
    log: list[str] = field(default_factory=list)
    commits: list[tuple[str, str, str]] = field(default_factory=list)
    outcomes: list[TurnOutcome] = field(default_factory=list)
    sinks: list[ManualSink] = field(default_factory=list)

    def req(
        self, text: str = "你好", *, owner: str = "u1", sink: ManualSink | None = None,
        barge: tuple[str, ...] = ("u1",), commit: CommitMode = CommitMode.EAGER, **kw,
    ) -> tuple[TurnRequest, ManualSink]:
        utt = Utterance(text=text, speaker_id=owner)
        ticket = StandaloneTicketIssuer().issue(utt)
        s = sink or ManualSink(self.log)
        self.sinks.append(s)
        return TurnRequest(
            ticket=ticket, utterance=utt, sink_factory=lambda _v, s=s: s,
            barge_users=barge, commit_mode=commit, **kw,
        ), s


def build(
    responses=(RESPONSE,), *, gate: asyncio.Event | None = None, open_all: bool = True,
    platform: str = "discord", **co_kw,
) -> Env:
    log: list[str] = []
    backend = ScriptBackend(responses, gate=gate)
    think = build_think(backend, log)
    original = think.commit
    commits: list[tuple[str, str, str]] = []

    async def spy(turn_id: str, spoken: str, generated: str) -> None:
        commits.append((turn_id, spoken, generated))
        await original(turn_id, spoken, generated)

    think.commit = spy  # type: ignore[method-assign]
    speak = GateSpeak(open_all=open_all)
    tracer = LatencyTracer()
    outcomes: list[TurnOutcome] = []
    co = TurnCoordinator(
        think, speak, tracer=tracer,
        policy_source=StaticPolicySource(PolicySnapshot.for_platform(platform)),
        on_turn_ended=outcomes.append, **co_kw,
    )
    _COORDINATORS.append(co)
    return Env(co, think, backend, speak, tracer, log, commits, outcomes)


_COORDINATORS: list[TurnCoordinator] = []


@pytest.fixture(autouse=True)
async def _drain_coordinators():
    """每個測試結束都收掉還沒結束的輪，避免 task 外洩到下一個測試。"""
    yield
    while _COORDINATORS:
        co = _COORDINATORS.pop()
        co.cancel_all("test_end")
        await asyncio.wait_for(co.join(), SAFETY_S)


async def settled(handle) -> TurnOutcome:
    return await asyncio.wait_for(handle.settled, SAFETY_S)


async def playing(sink: ManualSink, n: int = 4) -> None:
    await until(lambda: len(sink.chunks) == n and sink.draining.is_set())


# ───────────────────────── 排隊 ─────────────────────────


async def test_queued_turn_writes_only_after_predecessor_ends() -> None:
    env = build()
    ra, sink_a = env.req("A", owner="u1")
    ha = env.co.submit(ra)
    await playing(sink_a)

    rb, sink_b = env.req("B", owner="u2", barge=("u2",))
    hb = env.co.submit(rb)
    await asyncio.wait_for(hb.generation_done, SAFETY_S)  # B 的文字已生成完（不等 A 播完）
    await until(lambda: hb._att is not None and len(hb._att.buffer) == 4)
    assert sink_b.chunks == []  # 合成完了，但 A 還在播，不寫 sink
    assert not hb.audio_started

    sink_a.finish()
    out_a = await settled(ha)
    await until(lambda: len(sink_b.chunks) == 4)  # A 結束後才寫
    sink_b.finish()
    out_b = await settled(hb)
    assert out_a.status is OutcomeStatus.COMPLETED and out_b.status is OutcomeStatus.COMPLETED
    assert out_b.queue_wait_s >= 0.0
    assert env.backend.calls and len(env.backend.calls) == 2


async def test_barge_in_cascades_to_queued_unstarted_turns() -> None:
    env = build()
    ra, sink_a = env.req("A", owner="u1")
    ha = env.co.submit(ra)
    await playing(sink_a)
    sink_a.play(1.0)  # 使用者聽到第一句
    rb, sink_b = env.req("B", owner="u2", barge=("u2",))
    hb = env.co.submit(rb)
    await asyncio.wait_for(hb.generation_done, SAFETY_S)

    # barge-in 只累積 A 的指定使用者；1.5s 不停、2.0s 停
    for k in range(3):
        env.co.on_voice_event(voiced(1.0 + 0.5 * k))
    assert not ha.voice_token.is_cancelled
    env.co.on_voice_event(voiced(2.5))
    assert ha.voice_token.is_cancelled and not ha.token.is_cancelled  # 只停語音，think 不取消

    out_a, out_b = await settled(ha), await settled(hb)
    assert out_a.status is OutcomeStatus.CANCELLED and out_a.barge_in and out_a.cut_short
    assert out_a.cancel_reason == CancelReason.BARGE_IN.value
    assert out_a.spoken_text == S0 and out_a.spoken_known  # 被打斷時後面有別句音訊，第一句算確知
    assert out_b.barge_in and out_b.metadata.get("cascade") is True
    assert out_b.status is OutcomeStatus.CANCELLED
    assert sink_b.chunks == [] and sink_b.stop_calls >= 1


async def test_barge_in_cascade_skips_non_interruptible_turns() -> None:
    class Rules:
        def flags_for(self, reply_mode):
            return ModeFlags(barge_in=reply_mode != "locked")

    env = build()
    env.co.policy_source = StaticPolicySource(PolicySnapshot(mode_rules=Rules()))
    ra, sink_a = env.req("A", owner="u1")
    ha = env.co.submit(ra)
    await playing(sink_a)
    sink_a.play(1.0)
    rb, sink_b = env.req("B", owner="u2", barge=("u2",))
    rb.ticket = dataclasses.replace(rb.ticket, reply_mode="locked")
    hb = env.co.submit(rb)
    rc, sink_c = env.req("C", owner="u3", barge=("u3",))
    hc = env.co.submit(rc)
    await asyncio.wait_for(hc.generation_done, SAFETY_S)

    for k in range(4):
        env.co.on_voice_event(voiced(1.0 + 0.5 * k))
    assert ha.voice_token.is_cancelled
    out_a, out_c = await settled(ha), await settled(hc)
    assert out_c.barge_in and out_c.metadata.get("cascade") is True  # 可打斷的輪照常連帶
    assert not hb.voice_token.is_cancelled and sink_b.stop_calls == 0  # 不可打斷的輪不被連帶
    await until(lambda: len(sink_b.chunks) == 4)
    sink_b.finish()
    out_b = await settled(hb)
    assert out_a.barge_in and out_b.status is OutcomeStatus.COMPLETED
    assert not out_b.barge_in and "cascade" not in out_b.metadata


async def test_void_custom_reason_keeps_yielded_reason_code() -> None:
    gate = asyncio.Event()
    env = build(gate=gate)
    ra, _ = env.req("A", owner="u1")
    env.co.submit(ra)
    await until(lambda: len(env.backend.calls) == 1)
    rb, _ = env.req("B", owner="u2")
    hb = env.co.submit(rb)
    assert env.co.void(hb.turn_id, reason="custom") is True
    out = await settled(hb)
    assert out.status is OutcomeStatus.VOIDED
    assert out.metadata["reason_code"] == "yielded"  # reason_code 看種類，不看 detail 字串
    assert out.metadata["cancel_detail"] == "custom"
    assert out.superseded is False
    gate.set()


async def test_owner_supersede_reason_code_is_superseded() -> None:
    gate = asyncio.Event()
    env = build(gate=gate)
    ra, _ = env.req("A", owner="u1")
    ha = env.co.submit(ra)
    await until(lambda: len(env.backend.calls) == 1)
    rb, _ = env.req("B", owner="u1")
    env.co.submit(rb)
    out = await settled(ha)
    assert out.metadata["reason_code"] == "superseded" and out.superseded
    gate.set()


# ───────────────────────── 取消語意表（§7）─────────────────────────


async def test_void_before_generation_is_voided_and_never_generates() -> None:
    gate = asyncio.Event()
    env = build(gate=gate)
    ra, _ = env.req("A", owner="u1")
    ha = env.co.submit(ra)
    await until(lambda: len(env.backend.calls) == 1)
    rb, sink_b = env.req("B", owner="u2")
    hb = env.co.submit(rb)
    assert env.co.void(hb.turn_id, "yielded") is True
    out_b = await settled(hb)
    assert out_b.status is OutcomeStatus.VOIDED and out_b.skipped_reason == SkipReason.VOIDED
    assert out_b.cancel_reason == CancelReason.SUPERSEDED.value
    assert out_b.metadata["reason_code"] == "yielded"
    assert len(env.backend.calls) == 1 and sink_b.chunks == []
    assert env.co.void(hb.turn_id) is False  # 已結束
    gate.set()
    await playing(env.sinks[0])
    env.sinks[0].finish()
    await settled(ha)
    assert [o.turn_id for o in env.outcomes].count(hb.turn_id) == 1


async def test_void_mid_generation_cancels_without_commit() -> None:
    gate = asyncio.Event()
    env = build(gate=gate)
    ra, sink_a = env.req("A")
    ha = env.co.submit(ra)
    await until(lambda: len(env.backend.calls) == 1)
    assert env.co.void(ha.turn_id, "yielded") is True
    out = await settled(ha)
    assert out.status is OutcomeStatus.CANCELLED and out.aborted and not out.barge_in
    assert out.cancel_reason == CancelReason.SUPERSEDED.value
    assert env.commits == [] and sink_a.chunks == []


async def test_void_after_audio_started_is_refused() -> None:
    env = build()
    ra, sink_a = env.req("A")
    ha = env.co.submit(ra)
    await playing(sink_a)
    assert ha.audio_started and ha.sink_active  # ManualSink 沒有選用成員，走推定
    assert env.co.void(ha.turn_id) is False
    sink_a.finish()
    await settled(ha)


async def test_same_owner_new_turn_supersedes_old_one() -> None:
    env = build()
    r1, sink_1 = env.req("一", owner="u1")
    h1 = env.co.submit(r1)
    await playing(sink_1)
    r2, sink_2 = env.req("二", owner="u1")
    h2 = env.co.submit(r2)
    out_1 = await settled(h1)
    assert out_1.superseded and out_1.status is OutcomeStatus.CANCELLED
    assert out_1.cancel_reason == CancelReason.SUPERSEDED.value and sink_1.stopped
    await playing(sink_2)  # 舊輪被取代不佔位，新輪不必等它
    sink_2.finish()
    assert (await settled(h2)).status is OutcomeStatus.COMPLETED


async def test_cancel_all_is_shutdown_without_commit_in_settled_mode() -> None:
    env = build()
    ra, sink_a = env.req("A", commit=CommitMode.SETTLED)
    ha = env.co.submit(ra)
    await playing(sink_a)
    env.co.cancel_all("left_channel")
    out = await settled(ha)
    assert out.cancel_reason == CancelReason.SHUTDOWN.value and out.aborted
    assert out.metadata["cancel_detail"] == "left_channel"
    assert env.commits == []  # SHUTDOWN 不 commit、不 settle


async def test_retract_on_yield_settles_eager_history() -> None:
    env = build((RESPONSE, "戊戊戊。"))
    ra, sink_a = env.req("A", owner="u1", retract_on_yield=True)
    ha = env.co.submit(ra)
    await playing(sink_a)
    assert any(h["content"] == RESPONSE for h in env.think.history)  # EAGER 已 commit
    r2, _ = env.req("二", owner="u1")  # 同 owner 取代走 SUPERSEDED
    env.co.submit(r2)
    await settled(ha)
    assert all(h["content"] != RESPONSE for h in env.think.history)  # 撤回舊輪 assistant 項


# ───────────────────────── 跨執行緒 ─────────────────────────


class ThreadSpySink(ManualSink):
    def __init__(self, log=None):
        super().__init__(log)
        self.stop_threads: list[int] = []

    def stop(self) -> None:
        self.stop_threads.append(threading.get_ident())
        super().stop()


async def test_on_voice_event_from_other_threads_stops_sync_and_cancels_once() -> None:
    env = build()
    sink = ThreadSpySink(env.log)
    ra, _ = env.req("A", sink=sink)
    ha = env.co.submit(ra)
    await playing(sink)

    cancels: list[int] = []
    ha.voice_token.on_cancel(lambda _r: cancels.append(threading.get_ident()))
    loop_thread = threading.get_ident()
    caller_threads: list[int] = []

    def send(offset: float) -> None:
        caller_threads.append(threading.get_ident())
        for k in range(8):
            env.co.on_voice_event(voiced(offset + 0.5 * k))

    loop = asyncio.get_running_loop()
    await asyncio.gather(
        loop.run_in_executor(None, send, 1.0), loop.run_in_executor(None, send, 1.0)
    )
    # stop 在「第一個命中的呼叫執行緒」同步發生（早於 token 取消回到 loop）
    assert sink.stop_threads[0] in caller_threads and sink.stop_threads[0] != loop_thread
    await until(lambda: ha.voice_token.is_cancelled)
    assert cancels == [loop_thread]  # token 只取消一次，且在 loop 內
    out = await settled(ha)
    assert out.barge_in


# ───────────────────────── regen ─────────────────────────


class OnceRegen:
    def __init__(self, text: str, times: int = 99) -> None:
        self.text, self.times, self.calls = text, times, 0

    def refresh(self, turn):
        self.calls += 1
        if self.calls > self.times:
            return None
        return Utterance(text=self.text, turn_id="ignored")


class MemorySpy:
    def __init__(self) -> None:
        self.retrieves = 0
        self.stores = 0

    async def retrieve(self, query: str, **_kw) -> str:
        self.retrieves += 1
        return ""

    async def store(self, *_a, **_kw) -> None:
        self.stores += 1


async def test_regen_reuses_turn_id_stops_old_sink_and_commits_once() -> None:
    env = build(("甲甲甲。", "乙乙乙。"), open_all=False)
    memory = MemorySpy()
    env.think.memory = memory
    regen = OnceRegen("你好", times=1)
    r, _ = env.req("你好", regen=regen, max_regenerate=1)
    made: list[ManualSink] = []

    def factory(_v) -> ManualSink:
        s = ManualSink(env.log)
        made.append(s)
        return s

    r.sink_factory = factory
    h = env.co.submit(r)
    await until(lambda: len(made) == 2)  # 舊趟 abort，新趟重新建 sink（stop 是永久的）
    assert made[0].stopped and made[0].chunks == []
    env.speak.open(0)
    await until(lambda: len(made[1].chunks) == 1 and made[1].draining.is_set())
    made[1].finish()
    out = await settled(h)
    assert out.regenerated == 1 and out.attempt_id == 2
    assert out.turn_id == r.ticket.turn_id
    assert out.generated_text == "乙乙乙。"
    assert len(env.commits) == 1
    assert memory.retrieves == 1  # 同 turn_id 同 query 不重 retrieve
    assert regen.calls == 1
    trace = env.tracer.get(r.ticket.turn_id)
    assert trace is not None and marks.MARK_THINK_DONE in trace.marks


async def test_regen_respects_limit() -> None:
    env = build(("甲甲甲。", "乙乙乙。", "丙丙丙。"), open_all=False)
    regen = OnceRegen("你好")  # 每次都想重算，但上限 1
    r, _ = env.req("你好", regen=regen, max_regenerate=1)
    made: list[ManualSink] = []

    def factory(_v) -> ManualSink:
        made.append(ManualSink(env.log))
        return made[-1]

    r.sink_factory = factory
    h = env.co.submit(r)
    await until(lambda: len(made) == 2 and h.generation_done.done())
    assert regen.calls == 1 and len(env.backend.calls) == 2
    env.co.cancel_all()
    out = await settled(h)
    assert out.regenerated == 1


async def test_no_regen_once_audio_started() -> None:
    class StartedSink(ManualSink):
        first_frame_at = 1.0

    env = build()
    regen = OnceRegen("你好")
    r, _ = env.req("你好", sink=StartedSink(env.log), regen=regen, max_regenerate=1)
    h = env.co.submit(r)
    await until(lambda: h.generation_done.done())
    assert regen.calls == 0
    env.co.cancel_all()
    await settled(h)


# ───────────────────────── PASS ─────────────────────────


class PassOnFirst:
    def __init__(self) -> None:
        self.passed = False
        self.addressee_ids: set[str] = set()

    def feed(self, text: str):
        if text.startswith("甲"):
            self.passed = True
            return None
        return text


class PassOn:
    """句子以指定前綴開頭時 PASS，並記錄每次 feed 的句子。"""

    def __init__(self, prefix: str) -> None:
        self.prefix = prefix
        self.passed = False
        self.addressee_ids: set[str] = set()
        self.fed: list[str] = []

    def feed(self, text: str):
        self.fed.append(text)
        if text.startswith(self.prefix):
            self.passed = True
            return None
        return text


def track_think_completion(env: Env) -> dict[str, bool]:
    """包住 think.stream，記錄是否真的迭代到底、或被提早 aclose。"""
    state = {"finished": False, "closed_early": False}
    original = env.think.stream

    async def stream(utterance, token):
        agen = original(utterance, token)
        try:
            async for sentence in agen:
                yield sentence
            state["finished"] = True
        except GeneratorExit:
            state["closed_early"] = not state["finished"]
            raise
        finally:
            await agen.aclose()

    env.think.stream = stream  # type: ignore[method-assign]
    return state


@pytest.mark.parametrize("mode", [CommitMode.EAGER, CommitMode.SETTLED])
async def test_pass_writes_nothing_and_commits_per_mode(mode: CommitMode) -> None:
    env = build()
    r, sink = env.req("A", commit=mode, text_filter=PassOnFirst())
    h = env.co.submit(r)
    out = await settled(h)
    assert sink.chunks == [] and sink.stop_calls == 0
    assert out.status is OutcomeStatus.SKIPPED and out.skipped_reason == SkipReason.PASS
    assert out.generated_text == RESPONSE  # PASS 只關語音，think 生成到完
    assert len(env.commits) == 1
    _tid, spoken, generated = env.commits[0]
    if mode is CommitMode.EAGER:
        assert spoken == generated == RESPONSE  # 全文（含 PASS 標記）照常 commit
    else:
        assert spoken == "" and generated == RESPONSE


async def test_pass_keeps_think_running_and_delivers_all_sentences() -> None:
    env = build()
    state = track_think_completion(env)
    sent: list[str] = []

    async def on_sentence(_view, sentence: Sentence) -> None:
        sent.append(sentence.text)

    r, sink = env.req("A", text_filter=PassOn("乙"), on_sentence=on_sentence)
    h = env.co.submit(r)
    await playing(sink, 1)
    sink.finish()
    out = await settled(h)
    assert state == {"finished": True, "closed_early": False}
    assert [x for x in sent if x] == SENTENCES  # PASS 句與之後的句子文字照常送
    assert env.speak.seen == [0]  # PASS 句與之後都不進語音
    assert len(sink.chunks) == 1
    assert out.generated_text == RESPONSE and out.skipped_reason == SkipReason.PASS
    assert env.commits[0][2] == RESPONSE


async def test_pass_is_detected_after_barge_in() -> None:
    env, mid = build_split()
    filt = PassOn("乙")
    r, sink = env.req("A", text_filter=filt)
    h = env.co.submit(r)
    await until(lambda: len(sink.chunks) == 1)
    sink.play(1.0)
    for k in range(4):
        env.co.on_voice_event(voiced(1.0 + 0.5 * k))
    assert h.voice_token.is_cancelled
    mid.set()
    out = await settled(h)
    assert filt.passed and SENTENCES[1] in filt.fed  # barge-in 後仍餵 filter
    assert out.generated_text == FULL
    # voice 已取消與 pass 同時成立：status／barge_in 維持 barge-in 語意，pass 只當資訊旗標
    assert out.barge_in and out.status is OutcomeStatus.CANCELLED
    assert out.skipped_reason == SkipReason.PASS


async def test_pass_is_detected_when_speak_is_off() -> None:
    class Rules:
        def flags_for(self, reply_mode):
            return ModeFlags(speak=False)

    env = build()
    env.co.policy_source = StaticPolicySource(PolicySnapshot(mode_rules=Rules()))
    r, sink = env.req("A", text_filter=PassOn("乙"))
    out = await settled(env.co.submit(r))
    assert sink.chunks == []
    assert out.skipped_reason == SkipReason.PASS  # PASS 比靜態的 mode_silent 更具體


# ───────────────────────── commit／settle ─────────────────────────


async def test_eager_commits_before_drain_and_settled_after() -> None:
    env = build()
    ra, sink_a = env.req("A", commit=CommitMode.EAGER)
    ha = env.co.submit(ra)
    await playing(sink_a)
    await until(lambda: "commit" in env.log)  # 文字完成就 commit，播放還在 drain
    assert not ha.settled.done()
    sink_a.finish()
    await settled(ha)

    env2 = build()
    rb, sink_b = env2.req("B", commit=CommitMode.SETTLED)
    hb = env2.co.submit(rb)
    await playing(sink_b)
    assert "commit" not in env2.log  # SETTLED：播完才 commit
    # 且鎖一路持有到結算：下一輪的 think 不會搶先開始
    rc, _ = env2.req("C", owner="u2")
    hc = env2.co.submit(rc)
    assert len(env2.backend.calls) == 1
    sink_b.finish()
    await settled(hb)
    assert env2.commits[0][1] == RESPONSE
    env2.co.cancel_all()
    await settled(hc)


class NoSettleThink:
    """只有 stream/commit、沒有 settle 的 ThinkStage。"""

    name = "no_settle"

    def __init__(self, inner: LLMThinkStage) -> None:
        self.inner = inner

    def stream(self, utterance, token):
        return self.inner.stream(utterance, token)

    async def commit(self, turn_id, spoken, generated):
        await self.inner.commit(turn_id, spoken, generated)


async def test_missing_settle_does_not_error() -> None:
    env = build()
    env.co.think = NoSettleThink(env.think)  # type: ignore[assignment]
    r, sink = env.req("A", history_spoken_only=True)
    h = env.co.submit(r)
    await playing(sink)
    sink.finish()
    assert (await settled(h)).status is OutcomeStatus.COMPLETED


async def test_history_spoken_only_settles_to_spoken_after_barge_in() -> None:
    env = build()
    r, sink = env.req("A", history_spoken_only=True)
    h = env.co.submit(r)
    await playing(sink)
    sink.play(1.0)
    for k in range(4):
        env.co.on_voice_event(voiced(1.0 + 0.5 * k))
    out = await settled(h)
    assert out.spoken_text == S0
    assert env.think.history[-1] == {"role": "assistant", "content": S0}


# ───────────────────────── sink 能力與降級 ─────────────────────────


async def test_sink_factory_string_skips_voice_but_keeps_text() -> None:
    env = build()
    sent: list[str] = []

    async def on_sentence(_view, sentence: Sentence) -> None:
        sent.append(sentence.text)

    r, _ = env.req("A", on_sentence=on_sentence)
    r.sink_factory = lambda _v: "tts_not_ready"
    h = env.co.submit(r)
    out = await settled(h)
    assert out.status is OutcomeStatus.SKIPPED and out.skipped_reason == "tts_not_ready"
    assert out.generated_text == RESPONSE and "".join(sent) == RESPONSE
    assert not out.spoken_known  # 沒有語音資料 = 未知，不是「什麼都沒播」


async def test_mode_silent_flag_means_text_only() -> None:
    class Rules:
        def flags_for(self, reply_mode):
            return ModeFlags(speak=False)

    env = build()
    env.co.policy_source = StaticPolicySource(PolicySnapshot(mode_rules=Rules()))
    r, sink = env.req("A")
    out = await settled(env.co.submit(r))
    assert out.skipped_reason == SkipReason.MODE_SILENT and sink.chunks == []


async def test_wait_idle_timeout_releases_and_logs(caplog) -> None:
    class StuckIdleSink(ManualSink):
        async def wait_idle(self) -> None:
            await asyncio.Event().wait()

    env = build(idle_timeout_s=0.01)
    sink = StuckIdleSink(env.log)
    r, _ = env.req("A", sink=sink)
    with caplog.at_level("WARNING"):
        h = env.co.submit(r)
        await playing(sink)
        # 逾時警告要等 wait_idle 卡滿 idle_timeout_s 才記錄；負載下 playing 返回時未必已記錄，改等條件成立
        await poll(lambda: any("wait_idle" in rec.message for rec in caplog.records))
    assert any("wait_idle" in rec.message for rec in caplog.records)
    sink.finish()
    await settled(h)


# ───────────────────────── admission／每個 submit 恰一個 outcome ─────────────────────────


class MaxPending:
    def __init__(self, n: int) -> None:
        self.n = n

    def admit(self, request, busy: bool, waiting: int) -> bool:
        return not (busy and waiting >= self.n)


async def test_every_submit_gets_exactly_one_outcome() -> None:
    gate = asyncio.Event()
    env = build(gate=gate, admission=MaxPending(1))
    handles = []
    for name in ("A", "B", "C"):
        r, _ = env.req(name, owner=name)
        handles.append(env.co.submit(r))
    dropped = await settled(handles[2])  # C：busy 且已有人在等 → 丟棄，不觸發任何生成
    assert dropped.status is OutcomeStatus.DROPPED_BUSY
    assert dropped.skipped_reason == SkipReason.DROPPED_BUSY
    env.co.void(handles[1].turn_id)  # B：等鎖期間作廢
    gate.set()
    await until(lambda: env.sinks[0].draining.is_set())
    env.sinks[0].finish()
    outs = [await settled(h) for h in handles]
    assert [o.status for o in outs] == [
        OutcomeStatus.COMPLETED, OutcomeStatus.VOIDED, OutcomeStatus.DROPPED_BUSY
    ]
    ids = [o.turn_id for o in env.outcomes]
    assert sorted(ids) == sorted(h.turn_id for h in handles)  # 每輪恰好一次
    assert len(env.backend.calls) == 1


async def test_busy_and_waiting_are_public_and_skip_claimed_cancels() -> None:
    gate = asyncio.Event()
    env = build(gate=gate)
    assert env.co.busy is False and env.co.waiting == 0
    ha = env.co.submit(env.req("A", owner="u1")[0])
    assert env.co.busy is True and env.co.waiting == 0  # 只有持鎖者，沒人在等
    hb = env.co.submit(env.req("B", owner="u2")[0])
    hc = env.co.submit(env.req("C", owner="u3")[0])
    assert env.co.waiting == 2
    assert env.co.void(hb.turn_id) is True  # 已認領取消、尚未離開鎖：不再算排隊
    assert env.co.waiting == 1
    env.co.cancel_all("left")
    assert env.co.busy is False and env.co.waiting == 0
    gate.set()
    for h in (ha, hb, hc):
        await settled(h)
    assert env.co.busy is False and env.co.waiting == 0


async def test_admission_sees_busy_and_waiting_from_public_properties() -> None:
    seen: list[tuple[bool, int]] = []

    class Spy:
        def admit(self, request, busy: bool, waiting: int) -> bool:
            seen.append((busy, waiting))
            return True

    gate = asyncio.Event()
    env = build(gate=gate, admission=Spy())
    for name in ("A", "B", "C"):
        env.co.submit(env.req(name, owner=name)[0])
    assert seen == [(False, 0), (True, 0), (True, 1)]
    gate.set()
    env.co.cancel_all("left")


async def test_handle_exposes_skip_reason_and_predecessor() -> None:
    gate = asyncio.Event()
    env = build(gate=gate)
    ra, _ = env.req("A", owner="u1")
    ra.sink_factory = lambda _v: "tts_not_ready"
    ha = env.co.submit(ra)
    hb = env.co.submit(env.req("B", owner="u2")[0])
    assert ha.skip_reason == "tts_not_ready" and hb.skip_reason is None
    assert ha.predecessor is None and hb.predecessor is ha
    # CANCELLED 的 outcome 也保留語音支線的靜態原因
    await until(lambda: len(env.backend.calls) == 1)
    env.co.cancel_all("left")
    out_a = await settled(ha)
    assert out_a.status is OutcomeStatus.CANCELLED
    assert out_a.metadata["sink_skip_reason"] == "tts_not_ready"
    out_b = await settled(hb)  # 等鎖期間作廢（VOIDED）沒有靜態原因
    assert "sink_skip_reason" not in out_b.metadata
    gate.set()


async def test_done_outcome_does_not_carry_sink_skip_reason_metadata() -> None:
    env = build()
    r, _ = env.req("A")
    r.sink_factory = lambda _v: "tts_not_ready"
    out = await settled(env.co.submit(r))
    assert out.skipped_reason == "tts_not_ready"  # DONE 沿用既有 skipped_reason
    assert "sink_skip_reason" not in out.metadata


def test_submit_rejects_ticket_mismatch() -> None:
    async def go() -> None:
        env = build()
        r, _ = env.req("A")
        r.ticket = StandaloneTicketIssuer().issue(Utterance(text="別人"))
        with pytest.raises(ValueError):
            env.co.submit(r)

    asyncio.run(go())


# ───────────────────────── 打點／記憶／TTFA ─────────────────────────


async def test_marks_queue_first_frame_and_separate_stt_done() -> None:
    env = build()
    r, sink = env.req("A", speech_end_at=10.0, stt_done_at=10.8, marks={"decision_done": 11.0})
    h = env.co.submit(r)
    await playing(sink)
    sink.finish()
    await settled(h)
    m = env.tracer.get(r.ticket.turn_id).marks  # type: ignore[union-attr]
    assert m[marks.MARK_SPEECH_END] == 10.0 and m[marks.MARK_INPUT_FINAL] == 10.8
    assert m["decision_done"] == 11.0
    for name in (marks.MARK_QUEUE_ACQUIRED, marks.MARK_FIRST_FRAME, marks.MARK_SPEAK_FIRST_CHUNK):
        assert name in m


async def test_coordinator_never_writes_memory() -> None:
    env = build()
    memory = MemorySpy()
    env.think.memory = memory
    r, sink = env.req("A")
    h = env.co.submit(r)
    await playing(sink)
    sink.finish()
    await settled(h)
    assert memory.stores == 0  # 記憶寫入屬宿主（bot 的 VoiceMemoryHub），ES 不雙寫
    import uep.stream.coordinator as mod

    src = open(mod.__file__, encoding="utf-8").read()
    assert ".store(" not in src and "memory" not in src.lower().replace("memory_", "")


async def test_first_chunk_to_sink_write_has_no_extra_loop_iteration() -> None:
    ticks = {"n": 0}

    async def ticker() -> None:
        while True:
            ticks["n"] += 1
            await asyncio.sleep(0)

    class Speak:
        name = "one"
        at_yield: list[int] = []

        async def stream(self, sentences, token):
            async for s in sentences:
                self.at_yield.append(ticks["n"])
                yield AudioChunk(
                    pcm=bytes(2000), sample_rate=TTS_SAMPLE_RATE, turn_id=s.turn_id,
                    sentence_index=s.index,
                )

    class Sink(ManualSink):
        at_write: list[int] = []

        async def write(self, chunk):
            self.at_write.append(ticks["n"])
            await super().write(chunk)

    env = build()
    speak = Speak()
    env.co.speak = speak  # type: ignore[assignment]
    sink = Sink(env.log)
    r, _ = env.req("A", sink=sink)
    task = asyncio.ensure_future(ticker())
    try:
        h = env.co.submit(r)
        await until(lambda: len(sink.at_write) >= 1)
        assert sink.at_write[0] == speak.at_yield[0]  # 首 chunk 產出到 sink.write 之間沒讓出 loop
        sink.finish()
        await settled(h)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert env.co.live_turns() == []


async def test_yield_policy_is_called_and_stale_snapshot_disables_it() -> None:
    class Y:
        def __init__(self) -> None:
            self.calls = 0

        def on_voiced(self, ev, live):
            self.calls += 1
            return [YieldAction(live[0].turn_id)] if live and not live[0].audio_started else []

    gate = asyncio.Event()
    y = Y()
    env = build(gate=gate, yield_policy=y)
    r, _ = env.req("A")
    h = env.co.submit(r)
    await until(lambda: len(env.backend.calls) == 1)
    snap = PolicySnapshot(ttl_s=0.0, issued_at=-100.0)  # 已過期
    env.co.policy_source = StaticPolicySource(snap)
    env.co.on_voice_event(voiced(1.0))
    assert y.calls == 0 and not h.token.is_cancelled  # 過期：讓位停用
    env.co.policy_source = StaticPolicySource(PolicySnapshot())
    env.co.on_voice_event(voiced(2.0))
    assert y.calls == 1
    out = await settled(h)
    assert out.metadata["reason_code"] == "yielded"
    gate.set()


# ───────────────────────── barge-in 只停語音、不停生成 ─────────────────────────


class SplitBackend(ScriptBackend):
    """第一句之後卡在閘門上，用來把「生成途中」與「播放中」重疊。"""

    def __init__(self, mid: asyncio.Event) -> None:
        super().__init__(("",))
        self.mid = mid

    async def stream_query(self, messages, system_instruction=None, **kwargs):
        self.calls.append(list(messages))
        for ch in SENTENCES[0]:
            yield ch
        await self.mid.wait()
        for ch in "".join(SENTENCES[1:3]):
            yield ch


def build_split(**kw) -> tuple[Env, asyncio.Event]:
    mid = asyncio.Event()
    env = build(**kw)
    backend = SplitBackend(mid)
    backend.stage = env.think
    env.think._backend = backend
    env.backend = backend
    return env, mid


FULL = "".join(SENTENCES[:3])


@pytest.mark.parametrize("mode", [CommitMode.EAGER, CommitMode.SETTLED])
async def test_barge_in_stops_voice_but_think_runs_to_completion(mode: CommitMode) -> None:
    env, mid = build_split()
    r, sink = env.req("A", commit=mode)
    h = env.co.submit(r)
    await until(lambda: len(sink.chunks) == 1)  # 第一句已在播，其餘文字還沒生成
    sink.play(1.0)
    for k in range(4):
        env.co.on_voice_event(voiced(1.0 + 0.5 * k))
    assert h.voice_token.is_cancelled and sink.stopped
    assert not h.token.is_cancelled  # think 不受影響
    assert not h.generation_done.done()
    mid.set()  # 之後的文字照常生成
    out = await settled(h)
    assert out.barge_in and out.status is OutcomeStatus.CANCELLED and out.cut_short
    assert out.generated_text == FULL  # 完整文字可供補傳
    # 整句制：被打斷時唯一一句的後半不確知（後面沒有別句音訊），不算已播
    assert out.spoken_text == "" and out.spoken_known
    assert len(sink.chunks) == 1 and sink.writes_after_stop == 0  # 不再 write
    assert env.speak.seen == [0]  # TTS 不再被拉取
    _tid, spoken, generated = env.commits[0]
    assert generated == FULL
    assert spoken == (FULL if mode is CommitMode.EAGER else "")  # EAGER 文字完成即 commit 全文
    assert len(env.commits) == 1


@pytest.mark.parametrize("how", ["void", "cancel_all", "owner"])
async def test_whole_turn_cancels_still_stop_think(how: str) -> None:
    env, mid = build_split()
    r, sink = env.req("A", owner="u1")
    h = env.co.submit(r)
    await until(lambda: len(sink.chunks) == 1)
    if how == "void":
        # 讓位只作用於未開播的輪；這裡改用還沒出聲的第二輪驗證 think 被停
        env2, mid2 = build_split()
        r2, sink2 = env2.req("B")
        env2.speak.open_all = False
        h2 = env2.co.submit(r2)
        await until(lambda: len(env2.backend.calls) == 1)
        assert env2.co.void(h2.turn_id) is True
        out2 = await settled(h2)
        assert h2.token.is_cancelled and out2.generated_text != FULL
        assert sink2.chunks == []
        mid2.set()
        env.co.cancel_all()
    elif how == "cancel_all":
        env.co.cancel_all("left")
    else:
        r3, _ = env.req("二", owner="u1")
        env.co.submit(r3)
    out = await settled(h)
    assert h.token.is_cancelled and out.generated_text != FULL  # think 被停在中途
    assert out.status is OutcomeStatus.CANCELLED
    mid.set()


async def test_whole_turn_cancel_after_barge_in_still_works() -> None:
    env, mid = build_split()
    r, sink = env.req("A")
    h = env.co.submit(r)
    await until(lambda: len(sink.chunks) == 1)
    for k in range(4):
        env.co.on_voice_event(voiced(1.0 + 0.5 * k))
    assert h.voice_token.is_cancelled and not h.token.is_cancelled
    env.co.cancel_all("left")  # barge-in 之後仍可整輪作廢
    out = await settled(h)
    assert h.token.is_cancelled and out.cancel_reason == CancelReason.SHUTDOWN.value
    assert out.generated_text != FULL
    mid.set()


# ───────────────────────── on_sentence 文字回呼的排空 ─────────────────────────


async def test_whole_turn_cancel_stops_pending_text_delivery() -> None:
    env = build()
    block = asyncio.Event()
    sent: list[str] = []

    async def on_sentence(_view, sentence: Sentence) -> None:
        sent.append(sentence.text)
        await block.wait()  # 第一句卡住（例如平台限流），其餘句子排在佇列裡

    r, sink = env.req("A", on_sentence=on_sentence)
    h = env.co.submit(r)
    await playing(sink)
    await until(lambda: len(sent) == 1)
    env.co.cancel_all("left")  # 整輪作廢：剩餘句子不再送出
    block.set()
    out = await settled(h)
    assert out.cancel_reason == CancelReason.SHUTDOWN.value
    assert sent == [S0]


async def test_barge_in_still_delivers_all_text() -> None:
    env = build()
    block = asyncio.Event()
    sent: list[str] = []

    async def on_sentence(_view, sentence: Sentence) -> None:
        sent.append(sentence.text)
        await block.wait()

    r, sink = env.req("A", on_sentence=on_sentence)
    h = env.co.submit(r)
    await playing(sink)
    await until(lambda: len(sent) == 1)
    for k in range(4):
        env.co.on_voice_event(voiced(1.0 + 0.5 * k))
    block.set()
    out = await settled(h)
    assert out.barge_in and "".join(sent) == RESPONSE  # 補傳需要完整文字


async def test_stuck_on_sentence_does_not_block_next_turn_or_lock() -> None:
    env = build()
    block = asyncio.Event()

    async def on_sentence(_view, _sentence: Sentence) -> None:
        await block.wait()

    ra, sink_a = env.req("A", owner="u1", on_sentence=on_sentence, commit=CommitMode.SETTLED)
    ha = env.co.submit(ra)
    rb, sink_b = env.req("B", owner="u2", barge=("u2",))
    hb = env.co.submit(rb)
    await playing(sink_a)
    sink_a.finish()  # A 播完；文字回呼還卡著
    await until(lambda: len(env.commits) == 1)  # SETTLED 已 commit、放鎖，B 開始生成
    await playing(sink_b)  # B 不被 A 卡住的文字回呼擋住
    assert not ha.settled.done()  # A 的 outcome 還在等文字送完
    block.set()
    assert (await settled(ha)).status is OutcomeStatus.COMPLETED
    sink_b.finish()
    await settled(hb)


async def test_text_timeout_gives_up_remaining_sentences() -> None:
    env = build(text_timeout_s=0.01)

    async def on_sentence(_view, _sentence: Sentence) -> None:
        await asyncio.Event().wait()

    r, sink = env.req("A", on_sentence=on_sentence)
    h = env.co.submit(r)
    await playing(sink)
    sink.finish()
    assert (await settled(h)).status is OutcomeStatus.COMPLETED


async def test_history_spoken_only_does_not_erase_when_progress_unknown() -> None:
    env = build()
    class NoProgressSink(ManualSink):
        @property
        def played_duration_s(self):  # type: ignore[override]
            return None  # 沒有實播進度

    sink = NoProgressSink(env.log)
    ra, _ = env.req("A", sink=sink, history_spoken_only=True)
    ha = env.co.submit(ra)
    await playing(sink)  # 整句制拿不到進度 -> spoken_known=False
    for k in range(4):
        env.co.on_voice_event(voiced(1.0 + 0.5 * k))
    out = await settled(ha)
    assert out.barge_in and not out.spoken_known
    assert env.think.history[-1] == {"role": "assistant", "content": RESPONSE}  # 沒有被刪掉


async def test_join_cancellation_does_not_cancel_turns() -> None:
    env = build()
    r, sink = env.req("A")
    h = env.co.submit(r)
    await playing(sink)
    joiner = asyncio.ensure_future(env.co.join())
    await asyncio.sleep(0)
    joiner.cancel()
    await asyncio.gather(joiner, return_exceptions=True)
    assert not h.token.is_cancelled and not h._task.cancelled()
    sink.finish()
    assert (await settled(h)).status is OutcomeStatus.COMPLETED


async def test_failed_generation_still_delivers_generated_sentences() -> None:
    """生成中途出錯（FAILED）不是整輪作廢：已生成的句子要照樣送完，即使回呼很慢。"""

    class FailingBackend(ScriptBackend):
        async def stream_query(self, messages, system_instruction=None, **kwargs):
            self.calls.append(list(messages))
            for ch in "".join(SENTENCES[:3]):
                yield ch
            raise RuntimeError("backend boom")

    env = build()
    backend = FailingBackend()
    backend.stage = env.think
    env.think._backend = backend
    release = asyncio.Event()
    sent: list[str] = []

    async def on_sentence(_view, sentence: Sentence) -> None:
        await release.wait()  # 每句都慢：由測試決定何時放行
        sent.append(sentence.text)

    r, _ = env.req("A", on_sentence=on_sentence)
    h = env.co.submit(r)
    # 等到出錯處理已把 root 取消（UPSTREAM_ERROR）、播放位置讓出，且 _conclude 走進 _drain_text
    # 時佇列仍有多句沒送——這正是舊缺陷（worker 被誤當整輪作廢而取消）發生的時點
    await until(lambda: h._root.is_cancelled and h._play_done.is_set())
    for _ in range(50):
        await asyncio.sleep(0)
    assert sent == [] and not h.settled.done()
    release.set()
    out = await settled(h)
    assert out.status is OutcomeStatus.FAILED and "backend boom" in (out.error or "")
    assert out.generated_text and out.generated_text in "".join(sent)
    assert "".join(sent).startswith(SENTENCES[0])


# ───────────────────────── on_think_start（取得 think 鎖後的宿主 hook）─────────────────────────


async def test_on_think_start_runs_in_lock_before_every_attempt() -> None:
    env = build(("甲甲甲。", "乙乙乙。"), open_all=False)
    regen = OnceRegen("你好", times=1)
    seen: list[tuple[int, bool, int]] = []
    views: list[object] = []

    async def hook(view, attempt_id: int) -> None:
        views.append(view)
        seen.append((attempt_id, env.co._think_lock.locked(), len(env.backend.calls)))

    r, _ = env.req("你好", regen=regen, max_regenerate=1, on_think_start=hook)
    made: list[ManualSink] = []

    def factory(_v) -> ManualSink:
        s = ManualSink(env.log)
        made.append(s)
        return s

    r.sink_factory = factory
    h = env.co.submit(r)
    await until(lambda: len(made) == 2)
    env.speak.open(0)
    await until(lambda: len(made[1].chunks) == 1 and made[1].draining.is_set())
    made[1].finish()
    out = await settled(h)
    assert out.regenerated == 1
    # 每趟（含 regen）都呼叫、都在鎖內、都在該趟 think 開始之前（已呼叫 backend 的次數 = 0、1）
    assert seen == [(1, True, 0), (2, True, 1)]
    assert all(v is h for v in views)


async def test_on_think_start_waits_for_previous_turn_to_release_lock() -> None:
    env = build()
    order: list[str] = []

    def hook_for(name: str):
        async def hook(_view, _attempt_id: int) -> None:
            env.log.append(f"hook:{name}")
            order.append(name)

        return hook

    ra, sa = env.req("A", commit=CommitMode.SETTLED, on_think_start=hook_for("A"))
    rb, sb = env.req("B", owner="u2", commit=CommitMode.SETTLED, on_think_start=hook_for("B"))
    ha = env.co.submit(ra)
    hb = env.co.submit(rb)
    await playing(sa)
    assert order == ["A"]  # B 還在等鎖，hook 不會提前跑
    sa.finish()
    await settled(ha)
    await playing(sb)
    sb.finish()
    await settled(hb)
    assert order == ["A", "B"]
    assert env.log.index("hook:B") > env.log.index("commit")  # 在 A 結算 commit 之後


async def test_on_think_start_error_is_swallowed_and_turn_still_runs(caplog) -> None:
    env = build()

    async def hook(_view, _attempt_id: int) -> None:
        raise RuntimeError("hook boom")

    r, sink = env.req("A", on_think_start=hook)
    with caplog.at_level("WARNING"):
        h = env.co.submit(r)
        await playing(sink)
        sink.finish()
        out = await settled(h)
    assert out.status is OutcomeStatus.COMPLETED and out.error is None
    assert out.generated_text == RESPONSE
    assert len(env.commits) == 1
    assert any("on_think_start" in rec.getMessage() for rec in caplog.records)
    assert "hook boom" in out.metadata["on_think_start_error"]


@pytest.mark.parametrize("how", ["void", "cancel_all"])
async def test_cancel_during_on_think_start_ends_turn_without_thinking(how: str) -> None:
    env = build()
    entered = asyncio.Event()
    hook_cancelled = asyncio.Event()

    async def hook(_view, _attempt_id: int) -> None:
        entered.set()
        try:
            await asyncio.Event().wait()  # 卡住，等外部取消
        finally:
            hook_cancelled.set()

    r, sink = env.req("A", on_think_start=hook)
    h = env.co.submit(r)
    await asyncio.wait_for(entered.wait(), SAFETY_S)
    if how == "void":
        assert env.co.void(h.turn_id) is True
    else:
        env.co.cancel_all("left")
    out = await settled(h)
    assert out.status is OutcomeStatus.CANCELLED and out.error is None
    assert hook_cancelled.is_set()  # hook 被取消，不是被放著跑
    assert env.backend.calls == [] and sink.chunks == []  # think 從未開始
    assert env.commits == []
    # 鎖已放掉：下一輪照常完成
    r2, s2 = env.req("B", owner="u2")
    h2 = env.co.submit(r2)
    await playing(s2)
    s2.finish()
    assert (await settled(h2)).status is OutcomeStatus.COMPLETED


# ───────────────────────── 語音側出錯：只停語音、think 照常跑完 ─────────────────────────


class FlakySpeak(GateSpeak):
    """第一次 stream 在產出 ``after`` 個音訊塊後拋例外，之後恢復正常。"""

    def __init__(self, after: int = 1) -> None:
        super().__init__(open_all=True)
        self.after = after
        self.streams = 0

    async def stream(self, sentences, token):
        self.streams += 1
        fail = self.streams == 1
        n = 0
        async for chunk in super().stream(sentences, token):
            yield chunk
            n += 1
            if fail and n >= self.after:
                raise RuntimeError("tts boom")


class FailingWriteSink(ManualSink):
    async def write(self, chunk) -> None:
        raise OSError("sink write boom")


class FailingDrainSink(ManualSink):
    async def drain(self) -> None:
        self.draining.set()
        raise OSError("sink drain boom")


def assert_voice_error_outcome(out: TurnOutcome, needle: str) -> None:
    assert out.status is OutcomeStatus.SKIPPED and out.skipped_reason == SkipReason.VOICE_ERROR
    assert out.phase.value == "done" and out.error is None  # think 沒事，不是 FAILED
    assert needle in out.metadata["voice_error"]
    assert not out.barge_in and not out.aborted and not out.superseded and out.cut_short
    assert out.cancel_reason is None
    assert "reason_code" not in out.metadata  # 不誤標 barge_in 等取消原因


@pytest.mark.parametrize("mode", [CommitMode.EAGER, CommitMode.SETTLED])
async def test_tts_error_stops_voice_only_and_think_finishes(mode: CommitMode) -> None:
    env, mid = build_split()
    env.co.speak = FlakySpeak(after=1)
    sent: list[str] = []

    async def on_sentence(_view, sentence: Sentence) -> None:
        sent.append(sentence.text)

    r, sink = env.req("A", commit=mode, on_sentence=on_sentence)
    h = env.co.submit(r)
    await until(lambda: sink.stopped)  # TTS 出錯 -> 語音被關
    assert h.voice_token.is_cancelled and not h.token.is_cancelled  # think 不受影響
    assert not h.generation_done.done()
    mid.set()  # 之後的文字照常生成
    out = await settled(h)
    assert_voice_error_outcome(out, "tts boom")
    assert out.generated_text == FULL
    assert "".join(sent) == FULL  # 文字照常送出
    assert len(env.commits) == 1 and env.commits[0][2] == FULL
    assert env.commits[0][1] == (FULL if mode is CommitMode.EAGER else "")
    # 下一輪不受影響（FlakySpeak 只壞第一次）
    r2, s2 = env.req("B", owner="u2")
    h2 = env.co.submit(r2)
    await until(lambda: len(s2.chunks) == 3 and s2.draining.is_set())
    s2.finish()
    out2 = await settled(h2)
    assert out2.status is OutcomeStatus.COMPLETED and out2.generated_text == FULL


@pytest.mark.parametrize("which", ["write", "drain"])
async def test_sink_error_stops_voice_only_and_text_still_commits(which: str) -> None:
    env = build()
    sink = (FailingWriteSink if which == "write" else FailingDrainSink)(env.log)
    sent: list[str] = []

    async def on_sentence(_view, sentence: Sentence) -> None:
        sent.append(sentence.text)

    r, _ = env.req("A", sink=sink, commit=CommitMode.SETTLED, on_sentence=on_sentence)
    h = env.co.submit(r)
    out = await settled(h)
    assert_voice_error_outcome(out, "sink " + which + " boom")
    assert out.generated_text == RESPONSE and "".join(sent) == RESPONSE
    assert len(env.commits) == 1 and env.commits[0][2] == RESPONSE
    assert sink.stopped
    r2, s2 = env.req("B", owner="u2")
    h2 = env.co.submit(r2)
    await playing(s2)
    s2.finish()
    assert (await settled(h2)).status is OutcomeStatus.COMPLETED


async def test_queued_turn_flush_error_does_not_touch_predecessor() -> None:
    env = build()
    ra, sa = env.req("A")
    rb, _ = env.req("B", owner="u2", sink=FailingWriteSink(env.log))
    ha = env.co.submit(ra)
    hb = env.co.submit(rb)
    await playing(sa)
    sa.finish()  # 前一輪播完，B 的緩衝才開始寫出 -> 寫入失敗
    out_a = await settled(ha)
    out_b = await settled(hb)
    assert out_a.status is OutcomeStatus.COMPLETED
    assert_voice_error_outcome(out_b, "sink write boom")
    assert out_b.generated_text == RESPONSE


async def test_think_error_is_still_failed_not_voice_error() -> None:
    class FailingBackend(ScriptBackend):
        async def stream_query(self, messages, system_instruction=None, **kwargs):
            self.calls.append(list(messages))
            for ch in SENTENCES[0]:
                yield ch
            raise RuntimeError("backend boom")

    env = build()
    backend = FailingBackend()
    backend.stage = env.think
    env.think._backend = backend
    r, _ = env.req("A")
    out = await settled(env.co.submit(r))
    assert out.status is OutcomeStatus.FAILED and "backend boom" in (out.error or "")
    assert "voice_error" not in out.metadata


# ───────────────────────── cancel_voice（單輪只取消語音）─────────────────────────


async def test_cancel_voice_stops_voice_only_and_think_finishes() -> None:
    env, mid = build_split()
    r, sink = env.req("A")
    h = env.co.submit(r)
    await until(lambda: len(sink.chunks) == 1)
    sink.play(0.5)
    assert env.co.cancel_voice(h.turn_id, "host_stop") is True
    assert sink.stopped and h.voice_token.is_cancelled
    assert not h.token.is_cancelled and not h.generation_done.done()  # think 不受影響
    assert env.co.cancel_voice(h.turn_id) is False  # 重複呼叫
    for k in range(4):  # 之後再有人插話也不會重複取消
        env.co.on_voice_event(voiced(2.0 + 0.5 * k))
    mid.set()
    out = await settled(h)
    assert out.status is OutcomeStatus.SKIPPED and out.skipped_reason == SkipReason.VOICE_CANCELLED
    assert out.metadata["voice_cancelled"] == "host_stop"
    assert not out.barge_in and not out.aborted and out.cut_short
    assert "reason_code" not in out.metadata and "voice_error" not in out.metadata
    assert out.generated_text == FULL
    assert len(env.commits) == 1 and env.commits[0][2] == FULL
    assert len(sink.chunks) == 1 and sink.writes_after_stop == 0
    assert env.co.cancel_voice(h.turn_id) is False  # 已結束


async def test_cancel_voice_returns_false_without_voice_or_unknown_turn() -> None:
    env = build()
    assert env.co.cancel_voice("nope") is False
    r, _ = env.req("A")
    r.sink_factory = lambda _v: "no_voice"  # 沒有語音支線
    h = env.co.submit(r)
    assert env.co.cancel_voice(h.turn_id) is False
    out = await settled(h)
    assert out.status is OutcomeStatus.SKIPPED and out.skipped_reason == "no_voice"


async def test_cancel_voice_does_not_cascade_to_queued_turns() -> None:
    env = build()
    ra, sa = env.req("A")
    rb, sb = env.req("B", owner="u2")
    ha = env.co.submit(ra)
    hb = env.co.submit(rb)
    await playing(sa)
    assert env.co.cancel_voice(ha.turn_id) is True
    out_a = await settled(ha)
    assert out_a.skipped_reason == SkipReason.VOICE_CANCELLED
    assert not hb.voice_token.is_cancelled  # 不像 barge-in 連帶取消後面的輪
    await playing(sb)
    sb.finish()
    assert (await settled(hb)).status is OutcomeStatus.COMPLETED


async def test_cancel_voice_on_queued_turn_keeps_its_text() -> None:
    env = build()
    ra, sa = env.req("A")
    rb, sb = env.req("B", owner="u2")
    ha = env.co.submit(ra)
    hb = env.co.submit(rb)
    await playing(sa)
    assert env.co.cancel_voice(hb.turn_id) is True
    sa.finish()
    await settled(ha)
    out_b = await settled(hb)
    assert out_b.skipped_reason == SkipReason.VOICE_CANCELLED
    assert out_b.generated_text == RESPONSE and sb.chunks == [] and sb.stopped


async def test_cancel_voice_from_other_threads_stops_sync_and_wins_once() -> None:
    env, mid = build_split()
    sink = ThreadSpySink(env.log)
    r, _ = env.req("A", sink=sink)
    h = env.co.submit(r)
    await until(lambda: len(sink.chunks) == 1)
    cancels: list[int] = []
    h.voice_token.on_cancel(lambda _r: cancels.append(threading.get_ident()))
    loop_thread = threading.get_ident()
    caller_threads: list[int] = []
    results: list[bool] = []

    def call() -> None:
        caller_threads.append(threading.get_ident())
        results.append(env.co.cancel_voice(h.turn_id, "thread"))

    loop = asyncio.get_running_loop()
    await asyncio.gather(*(loop.run_in_executor(None, call) for _ in range(4)))
    assert sorted(results) == [False, False, False, True]  # 只有一個呼叫贏得認領
    assert sink.stop_threads[0] in caller_threads and sink.stop_threads[0] != loop_thread
    await until(lambda: h.voice_token.is_cancelled)
    assert cancels == [loop_thread] and not h.token.is_cancelled
    mid.set()
    out = await settled(h)
    assert out.skipped_reason == SkipReason.VOICE_CANCELLED and out.generated_text == FULL


# ───────────────────────── 播完窗口：drain 已返回、播放位置還沒讓出 ─────────────────────────


class SlowCloseSpeak(GateSpeak):
    """stream 回傳物件的 aclose 卡住，把「drain 已返回、_play_done 還沒 set」的窗口撐開。"""

    def __init__(self) -> None:
        super().__init__(open_all=True)
        self.release = asyncio.Event()

    def stream(self, sentences, token):
        inner = super().stream(sentences, token)
        release = self.release

        class _Held:
            def __aiter__(self):
                return self

            async def __anext__(self):
                return await inner.__anext__()

            async def aclose(self) -> None:
                await release.wait()
                await inner.aclose()

        return _Held()


async def test_cancel_voice_after_drain_returned_is_refused() -> None:
    env = build()
    speak = SlowCloseSpeak()
    env.co.speak = speak
    r, sink = env.req("A", history_spoken_only=True)
    h = env.co.submit(r)
    await playing(sink)
    sink.finish()  # 全部播完，drain 返回；但收尾卡在 aclose，播放位置還沒讓出
    for _ in range(50):
        await asyncio.sleep(0)
    assert not h._play_done.is_set()
    assert env.co.cancel_voice(h.turn_id, "late") is False  # 語音早已播完，不能再標成取消
    assert not h.voice_token.is_cancelled
    speak.release.set()
    out = await settled(h)
    assert out.status is OutcomeStatus.COMPLETED and out.skipped_reason is None
    assert not out.cut_short and out.spoken_text == RESPONSE  # 最後一句沒被截掉
    assert "voice_cancelled" not in out.metadata


async def test_on_think_start_timeout_counts_as_failure_and_frees_lock() -> None:
    env = build(think_hook_timeout_s=0.05)

    async def hook(_view, _attempt_id: int) -> None:
        await asyncio.Event().wait()  # 永遠不返回

    r, sink = env.req("A", on_think_start=hook)
    h = env.co.submit(r)
    await poll(lambda: len(sink.chunks) == 4 and sink.draining.is_set())  # 逾時靠牆鐘
    sink.finish()
    out = await settled(h)
    assert out.status is OutcomeStatus.COMPLETED and out.generated_text == RESPONSE
    assert out.metadata["on_think_start_error"] == "timeout"
    r2, s2 = env.req("B", owner="u2")
    h2 = env.co.submit(r2)
    await playing(s2)
    s2.finish()
    out2 = await settled(h2)
    assert out2.status is OutcomeStatus.COMPLETED
    assert "on_think_start_error" not in out2.metadata


async def test_on_think_start_swallowing_cancel_does_not_hold_lock(monkeypatch) -> None:
    monkeypatch.setattr(coordinator_module, "THINK_HOOK_CANCEL_GRACE_S", 0.05)
    env = build(think_hook_timeout_s=0.05)
    stop = asyncio.Event()

    async def hook(_view, _attempt_id: int) -> None:
        while not stop.is_set():
            try:
                await stop.wait()
            except asyncio.CancelledError:  # 壞掉的 hook：吞掉取消
                pass

    r, sink = env.req("A", on_think_start=hook)
    h = env.co.submit(r)
    try:
        await poll(lambda: len(sink.chunks) == 4 and sink.draining.is_set())
        sink.finish()
        out = await settled(h)
        assert out.status is OutcomeStatus.COMPLETED
        assert out.metadata["on_think_start_error"] == "timeout"
    finally:
        stop.set()


async def test_coordinator_internal_error_is_failed_not_voice_error() -> None:
    """只有 speak／sink.write／flush／drain 的例外算語音側；coordinator 自身錯誤仍是 FAILED。"""
    env = build()
    original = env.tracer.mark

    def mark(tid, name, *args, **kw):
        if name == marks.MARK_SPEAK_FIRST_CHUNK:
            raise RuntimeError("internal bug")
        return original(tid, name, *args, **kw)

    env.tracer.mark = mark  # type: ignore[method-assign]
    r, _ = env.req("A")
    out = await settled(env.co.submit(r))
    assert out.status is OutcomeStatus.FAILED and "internal bug" in (out.error or "")
    assert "voice_error" not in out.metadata and out.skipped_reason != SkipReason.VOICE_ERROR


# ───────────────────────── 補強：結算、競爭認領、取消時序 ─────────────────────────


async def test_voice_cancel_waits_for_confirm_stopped_before_resolving_spoken() -> None:
    """被停掉的 sink 要先 confirm_stopped 才推算 spoken（Pipecat #4111 的順序）。"""
    env = build()
    r, sink = env.req("A", commit=CommitMode.SETTLED)
    sink.reported_after_stop = 2.0  # confirm 時才回報的實播位置
    h = env.co.submit(r)
    await playing(sink)
    sink.play(0.5)
    assert env.co.cancel_voice(h.turn_id) is True
    out = await settled(h)
    assert out.skipped_reason == SkipReason.VOICE_CANCELLED
    assert out.spoken_text == SENTENCES[0] + SENTENCES[1]  # 沒等 confirm 會是空字串
    assert env.commits == [(r.ticket.turn_id, out.spoken_text, RESPONSE)]


async def test_settled_voice_error_commits_truncated_spoken_and_full_generated() -> None:
    env = build()
    env.co.speak = FlakySpeak(after=2)
    r, sink = env.req("A", commit=CommitMode.SETTLED)
    sink.reported_after_stop = 1.0
    out = await settled(env.co.submit(r))
    assert_voice_error_outcome(out, "tts boom")
    assert out.spoken_text == SENTENCES[0] and out.generated_text == RESPONSE
    assert env.commits == [(r.ticket.turn_id, SENTENCES[0], RESPONSE)]


async def test_cancel_all_after_voice_error_is_cancelled_shutdown_with_voice_error() -> None:
    env, _mid = build_split()
    env.co.speak = FlakySpeak(after=1)
    r, sink = env.req("A")
    h = env.co.submit(r)
    await until(lambda: sink.stopped)  # 語音錯誤已認領，think 還卡在中途
    env.co.cancel_all("left")
    out = await settled(h)
    assert out.status is OutcomeStatus.CANCELLED and out.cancel_reason == "shutdown"
    assert out.metadata["reason_code"] == "shutdown"
    assert "tts boom" in out.metadata["voice_error"]
    assert out.skipped_reason is None and not out.barge_in


async def test_voice_error_and_other_voice_cancels_are_mutually_exclusive() -> None:
    # 語音錯誤先認領：之後的 cancel_voice／插話都不再生效
    env, mid = build_split()
    env.co.speak = FlakySpeak(after=1)
    r, sink = env.req("A")
    h = env.co.submit(r)
    await until(lambda: sink.stopped)
    assert env.co.cancel_voice(h.turn_id, "late") is False
    for k in range(4):
        env.co.on_voice_event(voiced(1.0 + 0.5 * k))
    mid.set()
    out = await settled(h)
    assert out.skipped_reason == SkipReason.VOICE_ERROR and not out.barge_in
    assert "voice_cancelled" not in out.metadata

    # cancel_voice 先認領：之後的語音錯誤認領失敗，只剩 voice_cancelled
    env2, mid2 = build_split()
    r2, sink2 = env2.req("A")
    h2 = env2.co.submit(r2)
    await until(lambda: len(sink2.chunks) == 1)
    assert env2.co.cancel_voice(h2.turn_id, "host") is True
    assert env2.co._claim_voice(h2, "late error", coordinator_module.KIND_VOICE_ERROR) is False
    mid2.set()
    out2 = await settled(h2)
    assert out2.skipped_reason == SkipReason.VOICE_CANCELLED
    assert "voice_error" not in out2.metadata


async def test_cancel_voice_ignores_barge_in_flag_of_non_interruptible_turn() -> None:
    class Rules:
        def flags_for(self, reply_mode):
            return ModeFlags(barge_in=False)

    env = build()
    env.co.policy_source = StaticPolicySource(PolicySnapshot(mode_rules=Rules()))
    r, sink = env.req("A")
    h = env.co.submit(r)
    await playing(sink)
    sink.play(1.0)
    for k in range(4):
        env.co.on_voice_event(voiced(1.0 + 0.5 * k))
    assert not h.voice_token.is_cancelled  # 使用者插話打斷不了
    assert env.co.cancel_voice(h.turn_id, "host") is True  # 宿主的明確指令可以
    out = await settled(h)
    assert out.skipped_reason == SkipReason.VOICE_CANCELLED and not out.barge_in


async def test_cancel_during_on_think_start_of_regen_second_attempt() -> None:
    env = build(("甲甲甲。", "乙乙乙。"), open_all=False)
    entered = asyncio.Event()
    hook_cancelled = asyncio.Event()

    async def hook(_view, attempt_id: int) -> None:
        if attempt_id == 1:
            return
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            hook_cancelled.set()

    r, _ = env.req("你好", regen=OnceRegen("你好", times=1), max_regenerate=1, on_think_start=hook)
    h = env.co.submit(r)
    await asyncio.wait_for(entered.wait(), SAFETY_S)
    assert len(env.backend.calls) == 1  # 第一趟 think 跑過，第二趟還沒開始
    env.co.cancel_all("left")
    out = await settled(h)
    assert out.status is OutcomeStatus.CANCELLED and out.regenerated == 1
    assert hook_cancelled.is_set() and len(env.backend.calls) == 1
    assert env.commits == []



# ───────────────────── S3：bind_loop／關閉後 submit／reason_code／生成完成 ─────────────────────


def test_bind_loop_rejects_submit_from_another_loop() -> None:
    env = build()
    loop_a = asyncio.new_event_loop()
    try:
        env.co.bind_loop(loop_a)

        async def go() -> None:
            r, _ = env.req("A")
            with pytest.raises(RuntimeError):
                env.co.submit(r)

        asyncio.run(go())  # 另一個 loop
    finally:
        loop_a.close()


async def test_bind_loop_same_loop_submit_ok_and_unbound_keeps_lazy_capture() -> None:
    env = build()
    env.co.bind_loop(asyncio.get_running_loop())
    r, sink = env.req("A")
    h = env.co.submit(r)
    await playing(sink)
    sink.finish()
    assert (await settled(h)).status is OutcomeStatus.COMPLETED
    # 未綁定：維持首次 submit 擷取
    env2 = build()
    assert env2.co._loop is None
    r2, sink2 = env2.req("B")
    h2 = env2.co.submit(r2)
    assert env2.co._loop is asyncio.get_running_loop()
    await playing(sink2)
    sink2.finish()
    await settled(h2)


async def test_cross_thread_entries_after_bind_loop_before_first_submit_are_safe() -> None:
    env = build()
    loop = asyncio.get_running_loop()
    results: list[object] = []

    def poke() -> None:
        env.co.bind_loop(loop)  # 從其他執行緒綁定
        env.co.on_voice_event(voiced(0.0))
        results.append(env.co.void("nope"))
        results.append(env.co.cancel_voice("nope"))
        env.co.cancel_all("early")

    await loop.run_in_executor(None, poke)
    assert results == [False, False]
    assert env.co._loop is loop
    # 之後第一輪照常（早先的 cancel_all 沒有殘留影響）
    r, sink = env.req("A")
    h = env.co.submit(r)
    await playing(sink)
    sink.finish()
    assert (await settled(h)).status is OutcomeStatus.COMPLETED


async def test_bound_loop_cross_thread_cancel_all_cancels_live_turn() -> None:
    env, mid = build_split()
    env.co.bind_loop(asyncio.get_running_loop())
    r, sink = env.req("A")
    h = env.co.submit(r)
    await until(lambda: len(sink.chunks) == 1)
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, env.co.cancel_all, "left")
    out = await settled(h)
    assert out.status is OutcomeStatus.CANCELLED
    mid.set()


async def test_submit_after_aclose_is_voided_shutdown_and_never_speaks() -> None:
    env = build()
    await env.co.aclose()
    factory_calls: list[int] = []
    r, sink = env.req("A")
    r.sink_factory = lambda _v: factory_calls.append(1) or sink
    h = env.co.submit(r)
    out = await settled(h)
    assert out.status is OutcomeStatus.VOIDED
    assert out.skipped_reason == SkipReason.SHUTDOWN == "shutdown"
    assert out.metadata["reason_code"] == "shutdown"
    assert out.turn_id == h.turn_id
    assert factory_calls == [] and sink.chunks == [] and env.backend.calls == []
    assert env.commits == []
    assert [o.turn_id for o in env.outcomes] == [h.turn_id]  # on_turn_ended 恰一次
    assert h.generation_done.done()
    assert env.co.live_turns() == [] and not env.co.busy


async def test_cross_thread_entries_after_aclose_are_noops_returning_false() -> None:
    env, mid = build_split()
    r, sink = env.req("A")
    h = env.co.submit(r)
    await until(lambda: len(sink.chunks) == 1)
    mid.set()
    await env.co.aclose()
    assert (await settled(h)).status is OutcomeStatus.CANCELLED
    loop = asyncio.get_running_loop()

    def poke() -> list[object]:
        env.co.on_voice_event(voiced(0.0))
        env.co.cancel_all("again")
        return [env.co.void(h.turn_id), env.co.cancel_voice(h.turn_id)]

    assert await loop.run_in_executor(None, poke) == [False, False]


class _FailingBackend(ScriptBackend):
    async def stream_query(self, messages, system_instruction=None, **kwargs):
        self.calls.append(list(messages))
        yield SENTENCES[0]
        raise RuntimeError("backend boom")


def _fail_think(env: Env) -> None:
    backend = _FailingBackend()
    backend.stage = env.think
    env.think._backend = backend


async def test_failed_outcome_carries_upstream_error_reason_code() -> None:
    env = build()
    _fail_think(env)
    r, _ = env.req("A")
    out = await settled(env.co.submit(r))
    assert out.status is OutcomeStatus.FAILED
    assert out.metadata["reason_code"] == "upstream_error"


class GenDoneSpy:
    def __init__(self, env: Env) -> None:
        self.calls: list[tuple[str, bool, int]] = []  # (turn_id, ok, 呼叫當下的 outcome 數)
        self.env = env

    def __call__(self, view, ok: bool) -> None:
        self.calls.append((view.turn_id, ok, len(self.env.outcomes)))


async def test_on_generation_done_ok_true_before_outcome_on_normal_completion() -> None:
    env = build()
    spy = GenDoneSpy(env)
    r, sink = env.req("A", on_generation_done=spy)
    h = env.co.submit(r)
    await playing(sink)
    sink.finish()
    out = await settled(h)
    assert out.status is OutcomeStatus.COMPLETED
    assert spy.calls == [(h.turn_id, True, 0)]  # 一次、ok、早於 outcome


async def test_on_generation_done_ok_true_on_pass_and_barge_in() -> None:
    env = build()
    spy = GenDoneSpy(env)
    r, sink = env.req("A", on_generation_done=spy, text_filter=PassOnFirst())
    h = env.co.submit(r)
    out = await settled(h)
    assert out.skipped_reason == SkipReason.PASS
    assert spy.calls == [(h.turn_id, True, 0)]

    env2, mid = build_split()
    spy2 = GenDoneSpy(env2)
    r2, sink2 = env2.req("B", on_generation_done=spy2)
    h2 = env2.co.submit(r2)
    await until(lambda: len(sink2.chunks) == 1)
    sink2.play(1.0)
    for k in range(4):
        env2.co.on_voice_event(voiced(1.0 + 0.5 * k))
    assert spy2.calls == []  # think 還沒生成完
    mid.set()
    out2 = await settled(h2)
    assert out2.barge_in
    assert spy2.calls == [(h2.turn_id, True, 0)]  # 只停語音，生成仍正常完成


async def test_on_generation_done_ok_false_on_think_error_and_whole_turn_cancel() -> None:
    env = build()
    _fail_think(env)
    spy = GenDoneSpy(env)
    r, _ = env.req("A", on_generation_done=spy)
    h = env.co.submit(r)
    out = await settled(h)
    assert out.status is OutcomeStatus.FAILED
    assert spy.calls == [(h.turn_id, False, 0)]

    env2, mid = build_split()
    spy2 = GenDoneSpy(env2)
    r2, sink2 = env2.req("B", on_generation_done=spy2)
    h2 = env2.co.submit(r2)
    await until(lambda: len(sink2.chunks) == 1)
    env2.co.cancel_all("left")
    out2 = await settled(h2)
    assert out2.status is OutcomeStatus.CANCELLED
    assert spy2.calls == [(h2.turn_id, False, 0)]
    mid.set()


async def test_on_generation_done_exception_is_swallowed_and_async_supported(caplog) -> None:
    env = build()

    def boom(_view, _ok) -> None:
        raise RuntimeError("cb boom")

    r, sink = env.req("A", on_generation_done=boom)
    with caplog.at_level("WARNING"):
        h = env.co.submit(r)
        await playing(sink)
        sink.finish()
        out = await settled(h)
    assert out.status is OutcomeStatus.COMPLETED and out.error is None
    assert any("on_generation_done" in rec.getMessage() for rec in caplog.records)

    seen: list[bool] = []

    async def acb(_view, ok: bool) -> None:
        seen.append(ok)

    r2, sink2 = env.req("B", owner="u2", on_generation_done=acb)
    h2 = env.co.submit(r2)
    await playing(sink2)
    sink2.finish()
    await settled(h2)
    await until(lambda: seen == [True])


async def test_on_generation_done_not_called_when_generation_never_started() -> None:
    gate = asyncio.Event()
    env = build(gate=gate)
    spy = GenDoneSpy(env)
    ra, _ = env.req("A")
    rb, _ = env.req("B", owner="u2", on_generation_done=spy)
    env.co.submit(ra)
    hb = env.co.submit(rb)
    env.co.void(hb.turn_id)  # 等鎖期間作廢：沒有生成
    out = await settled(hb)
    assert out.status is OutcomeStatus.VOIDED
    assert spy.calls == []
    gate.set()


async def test_submit_warns_when_ticket_and_utterance_turn_ids_differ(caplog) -> None:
    env = build()
    r, sink = env.req("A")
    utt = r.utterance
    r.ticket = dataclasses.replace(r.ticket, turn_id="turn_core_1", source_turn_id=utt.turn_id)
    with caplog.at_level("WARNING"):
        h = env.co.submit(r)
    assert any("turn_id" in rec.getMessage() for rec in caplog.records)
    assert h.turn_id == "turn_core_1"  # 行為不變
    await playing(sink)
    sink.finish()
    assert (await settled(h)).status is OutcomeStatus.COMPLETED


async def test_submit_does_not_warn_when_turn_ids_equal(caplog) -> None:
    env = build()
    r, sink = env.req("A")
    r.ticket = dataclasses.replace(r.ticket, turn_id=r.utterance.turn_id)
    with caplog.at_level("WARNING"):
        h = env.co.submit(r)
    assert not [rec for rec in caplog.records if "turn_id" in rec.getMessage()]
    await playing(sink)
    sink.finish()
    await settled(h)


# ───────────────────── S3 審查追補：fallback 出口／背景回呼／loop 綁定 ─────────────────────


async def test_fallback_failed_outcome_has_upstream_error_and_gen_done_before_outcome() -> None:
    env = build()
    spy = GenDoneSpy(env)

    async def bad_produce(t) -> None:
        t._gen_started = True  # 生成已開始但尚未 resolve，然後 _produce 逃出例外
        raise RuntimeError("internal bug")

    env.co._produce = bad_produce  # type: ignore[method-assign]
    r, _ = env.req("A", on_generation_done=spy)
    h = env.co.submit(r)
    out = await settled(h)
    assert out.status is OutcomeStatus.FAILED
    assert out.metadata["reason_code"] == "upstream_error"
    assert spy.calls == [(h.turn_id, False, 0)]  # 同步回呼早於 outcome


async def test_fallback_task_cancelled_outcome_has_shutdown_and_gen_done_before_outcome() -> None:
    env, mid = build_split()
    spy = GenDoneSpy(env)
    r, sink = env.req("A", on_generation_done=spy)
    h = env.co.submit(r)
    await until(lambda: len(sink.chunks) == 1)  # 生成中
    h._task.cancel()
    out = await settled(h)
    assert out.status is OutcomeStatus.CANCELLED
    assert out.metadata["reason_code"] == "shutdown"
    assert spy.calls == [(h.turn_id, False, 0)]
    mid.set()


async def test_aclose_cancels_hanging_async_generation_callback() -> None:
    env = build()
    started = asyncio.Event()

    async def hang(_view, _ok) -> None:
        started.set()
        await asyncio.Event().wait()

    r, sink = env.req("A", on_generation_done=hang)
    h = env.co.submit(r)
    await playing(sink)
    sink.finish()
    await settled(h)
    await started.wait()
    assert env.co._bg_tasks
    await asyncio.wait_for(env.co.aclose(), SAFETY_S)
    assert not env.co._bg_tasks


async def test_bind_different_loop_after_submit_raises() -> None:
    env = build()
    r, sink = env.req("A")
    h = env.co.submit(r)
    other = asyncio.new_event_loop()
    try:
        with pytest.raises(RuntimeError):
            env.co.bind_loop(other)
        env.co.bind_loop(asyncio.get_running_loop())  # 同一個 loop 可以
    finally:
        other.close()
    await playing(sink)
    sink.finish()
    await settled(h)


async def test_cross_thread_cancel_on_closed_loop_does_not_raise() -> None:
    env, mid = build_split()
    r, sink = env.req("A")
    h = env.co.submit(r)
    await until(lambda: len(sink.chunks) == 1)
    real = env.co._loop
    dead = asyncio.new_event_loop()
    dead.close()
    env.co._loop = dead
    loop = asyncio.get_running_loop()
    errors: list[BaseException] = []

    def poke() -> None:
        try:
            env.co.cancel_all("late")
            env.co.on_voice_event(voiced(0.0))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    await loop.run_in_executor(None, poke)
    assert errors == []
    assert sink.stopped  # 同步 stop 仍然做了
    env.co._loop = real
    env.co._cancel_in_loop(h, False)  # 補上被吞掉的 token 取消，讓輪收尾
    await settled(h)
    mid.set()


async def test_cross_thread_voice_cancel_on_closed_loop_does_not_raise() -> None:
    env, mid = build_split()
    r, sink = env.req("A")
    h = env.co.submit(r)
    await until(lambda: len(sink.chunks) == 1)
    real = env.co._loop
    dead = asyncio.new_event_loop()
    dead.close()
    env.co._loop = dead
    loop = asyncio.get_running_loop()
    assert await loop.run_in_executor(None, env.co.cancel_voice, h.turn_id) is True
    env.co._loop = real
    env.co._cancel_voice_in_loop(h, False)
    mid.set()
    out = await settled(h)
    assert out.skipped_reason == SkipReason.VOICE_CANCELLED


class SilentReturnThink:
    """被取消就直接 return 的 think（不拋 CancelledError）。"""

    name = "silent_return"

    def __init__(self) -> None:
        self.commits: list[tuple[str, str, str]] = []
        self.waiting = asyncio.Event()

    async def stream(self, utterance, token):
        yield Sentence(text=S0, turn_id=utterance.turn_id, index=0, is_first=True)
        self.waiting.set()
        await token.wait()
        return

    async def commit(self, turn_id, spoken, generated):
        self.commits.append((turn_id, spoken, generated))


async def test_gen_done_ok_false_when_think_returns_silently_after_cancel() -> None:
    env = build()
    think = SilentReturnThink()
    env.co.think = think  # type: ignore[assignment]
    spy = GenDoneSpy(env)
    r, _ = env.req("A", on_generation_done=spy)
    r.sink_factory = lambda _v: "no_voice"  # 沒有語音支線：_generate 直接在主流程跑
    h = env.co.submit(r)
    await asyncio.wait_for(think.waiting.wait(), SAFETY_S)
    env.co.cancel_all("left")
    out = await settled(h)
    assert out.status is OutcomeStatus.CANCELLED
    assert spy.calls == [(h.turn_id, False, 0)]
    assert think.commits == []  # 被取消的輪不 commit


async def test_gen_done_called_once_across_regen() -> None:
    env = build(("甲甲甲。", "乙乙乙。"), open_all=False)
    spy = GenDoneSpy(env)
    regen = OnceRegen("你好", times=1)
    r, _ = env.req("你好", regen=regen, max_regenerate=1, on_generation_done=spy)
    made: list[ManualSink] = []

    def factory(_v) -> ManualSink:
        s = ManualSink(env.log)
        made.append(s)
        return s

    r.sink_factory = factory
    h = env.co.submit(r)
    await until(lambda: len(made) == 2)
    assert spy.calls == []  # 被丟棄的第一趟不算生成結束
    env.speak.open(0)
    await until(lambda: len(made[1].chunks) == 1 and made[1].draining.is_set())
    made[1].finish()
    out = await settled(h)
    assert out.regenerated == 1
    assert spy.calls == [(h.turn_id, True, 0)]


async def test_submit_during_aclose_is_voided_shutdown() -> None:
    env, mid = build_split()
    r, sink = env.req("A")
    ha = env.co.submit(r)
    await until(lambda: len(sink.chunks) == 1)
    closing = asyncio.ensure_future(env.co.aclose())
    await asyncio.sleep(0)  # aclose 已設 _closed，正在等 A 收尾
    rb, sinkb = env.req("B", owner="u2")
    hb = env.co.submit(rb)
    outb = await settled(hb)
    assert outb.status is OutcomeStatus.VOIDED and outb.skipped_reason == SkipReason.SHUTDOWN
    assert sinkb.chunks == []
    mid.set()
    await asyncio.wait_for(closing, SAFETY_S)
    assert (await settled(ha)).status is OutcomeStatus.CANCELLED


# ───────────────────────── cancel_turn：宿主單輪整輪取消 ─────────────────────────


async def test_cancel_turn_before_generation_is_voided_with_reason_code() -> None:
    gate = asyncio.Event()
    env = build(gate=gate)
    ra, _ = env.req("A", owner="u1")
    ha = env.co.submit(ra)
    await until(lambda: len(env.backend.calls) == 1)
    rb, sink_b = env.req("B", owner="u2")
    hb = env.co.submit(rb)  # 排隊中：還沒拿到 think 鎖
    assert env.co.cancel_turn(hb.turn_id, reason_code="global_stop", detail="d1") is True
    out_b = await settled(hb)
    assert out_b.status is OutcomeStatus.VOIDED and out_b.aborted
    assert out_b.metadata["reason_code"] == "global_stop"
    assert out_b.metadata["cancel_detail"] == "d1"
    assert not out_b.barge_in and not out_b.superseded
    assert len(env.backend.calls) == 1 and sink_b.chunks == []
    assert not ha.token.is_cancelled and not ha.voice_token.is_cancelled  # 不連帶前面的輪
    gate.set()
    await playing(env.sinks[0])
    env.sinks[0].finish()
    assert (await settled(ha)).status is OutcomeStatus.COMPLETED


async def test_cancel_turn_default_reason_code_is_host_cancelled() -> None:
    gate = asyncio.Event()
    env = build(gate=gate)
    r, _ = env.req("A")
    h = env.co.submit(r)
    await until(lambda: len(env.backend.calls) == 1)
    assert env.co.cancel_turn(h.turn_id) is True
    out = await settled(h)
    assert out.metadata["reason_code"] == "host_cancelled" and out.metadata["cancel_detail"] == ""
    gate.set()


async def test_cancel_turn_after_audio_started_stops_think_and_sink() -> None:
    env, mid = build_split()
    spy = GenDoneSpy(env)
    r, sink = env.req("A", commit=CommitMode.SETTLED, on_generation_done=spy)
    h = env.co.submit(r)
    await until(lambda: len(sink.chunks) == 1)  # 第一句已在播，其餘文字還沒生成
    assert h.audio_started
    assert env.co.cancel_turn(h.turn_id, reason_code="presence_lost", detail="gone") is True
    out = await settled(h)
    assert h.token.is_cancelled and sink.stopped  # think 與 sink 都被停
    assert out.status is OutcomeStatus.CANCELLED and out.phase.name == "CANCELLED"
    assert out.audio_started and out.aborted and out.cut_short
    assert not out.barge_in and not out.superseded
    assert out.metadata["reason_code"] == "presence_lost"
    assert out.metadata["cancel_detail"] == "gone"
    assert out.generated_text != FULL  # think 被停在中途
    assert env.commits == []  # 結算不 commit
    assert spy.calls == [(h.turn_id, False, 0)]  # 生成被中止：ok=False，早於 outcome
    mid.set()


async def test_cancel_turn_default_keeps_eager_commit_and_none_retracts_it() -> None:
    # EAGER 且生成已完成：完整生成文字早已 commit；default 保留，none 由 coordinator 撤回
    env = build((RESPONSE, "戊戊戊。"))
    ra, sink_a = env.req("A")
    ha = env.co.submit(ra)
    await playing(sink_a)
    assert any(h["content"] == RESPONSE for h in env.think.history)
    assert env.co.cancel_turn(ha.turn_id, reason_code="global_stop") is True
    out_a = await settled(ha)
    assert out_a.metadata["reason_code"] == "global_stop"
    assert len(env.commits) == 1  # 只有生成完成時的 EAGER commit，結算沒有再 commit
    assert any(h["content"] == RESPONSE for h in env.think.history)  # default：留在歷史

    before = list(env.think.history)
    rb, sink_b = env.req("B", owner="u2")
    hb = env.co.submit(rb)
    await asyncio.wait_for(hb.generation_done, SAFETY_S)  # 生成完才有 EAGER commit
    assert env.co.cancel_turn(hb.turn_id, reason_code="global_stop", commit="none") is True
    await settled(hb)
    assert env.think.history == before  # none：user 與 assistant 都撤回


async def test_cancel_turn_settled_mode_never_commits_for_either_commit_arg() -> None:
    env = build()
    r, sink = env.req("A", commit=CommitMode.SETTLED)
    h = env.co.submit(r)
    await playing(sink)
    assert env.co.cancel_turn(h.turn_id, commit="none") is True
    await settled(h)
    assert env.commits == [] and env.think.history == []


async def test_cancel_turn_none_without_settle_does_not_error() -> None:
    env = build()
    env.co.think = NoSettleThink(env.think)  # type: ignore[assignment]
    r, sink = env.req("A")
    h = env.co.submit(r)
    await playing(sink)
    assert env.co.cancel_turn(h.turn_id, commit="none") is True
    assert (await settled(h)).status is OutcomeStatus.CANCELLED


async def test_cancel_turn_does_not_cascade_to_queued_turns() -> None:
    env = build()
    ra, sink_a = env.req("A", owner="u1")
    ha = env.co.submit(ra)
    rb, sink_b = env.req("B", owner="u2")
    hb = env.co.submit(rb)
    await asyncio.wait_for(hb.generation_done, SAFETY_S)  # B 已生成完，排隊等播
    await playing(sink_a)
    assert env.co.cancel_turn(ha.turn_id) is True
    await settled(ha)
    assert not hb.token.is_cancelled and not hb.voice_token.is_cancelled
    await until(lambda: len(sink_b.chunks) == 4)
    sink_b.finish()
    out_b = await settled(hb)
    assert out_b.status is OutcomeStatus.COMPLETED


async def test_cancel_turn_works_on_non_interruptible_turn() -> None:
    class Rules:
        def flags_for(self, reply_mode):
            return ModeFlags(barge_in=False)

    env = build()
    env.co.policy_source = StaticPolicySource(PolicySnapshot(mode_rules=Rules()))
    r, sink = env.req("A")
    h = env.co.submit(r)
    await playing(sink)
    assert h.flags.barge_in is False
    assert env.co.cancel_turn(h.turn_id, reason_code="global_stop") is True
    out = await settled(h)
    assert out.status is OutcomeStatus.CANCELLED and sink.stopped


async def test_cancel_turn_after_barge_in_or_cancel_voice_still_works() -> None:
    env, mid = build_split()
    r, sink = env.req("A")
    h = env.co.submit(r)
    await until(lambda: len(sink.chunks) == 1)
    assert env.co.cancel_voice(h.turn_id) is True
    assert env.co.cancel_turn(h.turn_id, reason_code="global_stop") is True
    out = await settled(h)
    assert out.status is OutcomeStatus.CANCELLED and not out.barge_in and out.aborted
    assert out.metadata["reason_code"] == "global_stop" and h.token.is_cancelled
    mid.set()


async def test_cancel_turn_returns_false_when_unknown_finished_repeated_or_claimed() -> None:
    env = build()
    assert env.co.cancel_turn("nope") is False
    r, sink = env.req("A")
    h = env.co.submit(r)
    await playing(sink)
    assert env.co.cancel_turn(h.turn_id, reason_code="first") is True
    assert env.co.cancel_turn(h.turn_id, reason_code="second") is False  # 同一輪只成功一次
    out = await settled(h)
    assert out.metadata["reason_code"] == "first"
    assert env.co.cancel_turn(h.turn_id) is False  # 已結束

    rb, sink_b = env.req("B")
    hb = env.co.submit(rb)
    await playing(sink_b)
    sink_b.finish()
    await settled(hb)
    assert env.co.cancel_turn(hb.turn_id) is False  # 正常結束的輪

    rc, sink_c = env.req("C")
    hc = env.co.submit(rc)
    await playing(sink_c)
    env.co.cancel_all("x")
    assert env.co.cancel_turn(hc.turn_id) is False  # 已被整輪認領
    assert (await settled(hc)).metadata["reason_code"] == "shutdown"


async def test_cancel_turn_loses_to_earlier_void_and_blocks_later_void() -> None:
    gate = asyncio.Event()
    env = build(gate=gate)
    ra, _ = env.req("A", owner="u1")
    env.co.submit(ra)
    await until(lambda: len(env.backend.calls) == 1)
    rb, _ = env.req("B", owner="u2")
    hb = env.co.submit(rb)
    rc, _ = env.req("C", owner="u3")
    hc = env.co.submit(rc)
    assert env.co.void(hb.turn_id) is True
    assert env.co.cancel_turn(hb.turn_id) is False
    assert env.co.cancel_turn(hc.turn_id, reason_code="global_stop") is True
    assert env.co.void(hc.turn_id) is False
    assert (await settled(hb)).metadata["reason_code"] == "yielded"
    assert (await settled(hc)).metadata["reason_code"] == "global_stop"
    gate.set()


async def test_cancel_turn_after_aclose_is_false() -> None:
    env = build()
    r, sink = env.req("A")
    h = env.co.submit(r)
    await playing(sink)
    await env.co.aclose()
    assert env.co.cancel_turn(h.turn_id) is False


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"reason_code": ""}, "reason_code"),
        ({"reason_code": "   "}, "reason_code"),
        ({"reason_code": None}, "reason_code"),
        ({"reason_code": 7}, "reason_code"),
        ({"commit": "all"}, "commit"),
    ],
)
async def test_cancel_turn_rejects_invalid_arguments(kwargs: dict, message: str) -> None:
    env = build()
    r, sink = env.req("A")
    h = env.co.submit(r)
    await playing(sink)
    with pytest.raises(ValueError, match=message):
        env.co.cancel_turn(h.turn_id, **kwargs)
    assert not h.token.is_cancelled  # 驗證失敗不會認領


async def test_cancel_turn_from_other_threads_stops_sync_and_wins_once() -> None:
    env, mid = build_split()
    sink = ThreadSpySink(env.log)
    r, _ = env.req("A", sink=sink)
    h = env.co.submit(r)
    await until(lambda: len(sink.chunks) == 1)
    cancels: list[int] = []
    h.token.on_cancel(lambda _r: cancels.append(threading.get_ident()))
    loop_thread = threading.get_ident()
    caller_threads: list[int] = []
    results: list[bool] = []

    def call(code: str) -> None:
        caller_threads.append(threading.get_ident())
        results.append(env.co.cancel_turn(h.turn_id, reason_code=code))

    def mixed() -> None:
        results.append(env.co.void(h.turn_id))  # 已開播：一律 False
        env.co.cancel_all("late")  # 與 cancel_turn 搶同一個認領

    loop = asyncio.get_running_loop()
    await asyncio.gather(
        *(loop.run_in_executor(None, call, f"code{i}") for i in range(4)),
        loop.run_in_executor(None, mixed),
    )
    # cancel_turn 與 cancel_all 搶同一個認領，只有一個贏；void 對已開播輪恆為 False
    assert results.count(True) <= 1
    assert sink.stop_threads[0] in caller_threads and sink.stop_threads[0] != loop_thread
    await until(lambda: h.token.is_cancelled)
    assert cancels == [loop_thread]  # token 只取消一次，且在 loop 內
    mid.set()
    out = await settled(h)
    winner = out.metadata["reason_code"]
    if results.count(True) == 1:
        assert winner.startswith("code")
    else:
        assert winner == "shutdown"  # cancel_all 搶先
    assert [o.turn_id for o in env.outcomes].count(h.turn_id) == 1


async def test_cancel_turn_cross_thread_on_closed_loop_does_not_raise() -> None:
    env, mid = build_split()
    r, sink = env.req("A")
    h = env.co.submit(r)
    await until(lambda: len(sink.chunks) == 1)
    real = env.co._loop
    dead = asyncio.new_event_loop()
    dead.close()
    env.co._loop = dead
    loop = asyncio.get_running_loop()
    assert await loop.run_in_executor(None, env.co.cancel_turn, h.turn_id) is True
    assert sink.stopped  # 同步 stop 仍然做了
    env.co._loop = real
    env.co._cancel_in_loop(h, False)  # 補上被吞掉的 token 取消，讓輪收尾
    out = await settled(h)
    assert out.metadata["reason_code"] == "host_cancelled"
    mid.set()


# ───────────────────────── cancel_turn：歷史撤回與認領窗口（審查補強）─────────────────────────


async def test_cancel_turn_none_leaves_no_user_or_assistant_in_history() -> None:
    env = build()
    r, sink = env.req("你好嗎")
    h = env.co.submit(r)
    await playing(sink)
    assert [m["role"] for m in env.think.history] == ["user", "assistant"]  # EAGER 已 commit
    assert env.co.cancel_turn(h.turn_id, reason_code="global_stop", commit="none") is True
    out = await settled(h)
    assert env.think.history == []  # 使用者話語與回答都撤掉
    assert out.metadata["history_retracted"] is True
    assert "history_retract_error" not in out.metadata


async def test_cancel_turn_none_only_removes_the_cancelled_turn() -> None:
    env = build((RESPONSE, "戊戊戊。"))
    ra, sink_a = env.req("A問", owner="u1")
    ha = env.co.submit(ra)
    await playing(sink_a)
    sink_a.finish()
    await settled(ha)
    before = list(env.think.history)
    assert [m["content"] for m in before][0] == "A問"
    rb, sink_b = env.req("B問", owner="u2")
    hb = env.co.submit(rb)
    await asyncio.wait_for(hb.generation_done, SAFETY_S)
    assert len(env.think.history) == 4
    assert env.co.cancel_turn(hb.turn_id, commit="none") is True
    await settled(hb)
    assert env.think.history == before


async def test_cancel_turn_default_keeps_history_and_reports_not_retracted() -> None:
    env = build()
    r, sink = env.req("你好嗎")
    h = env.co.submit(r)
    await playing(sink)
    assert env.co.cancel_turn(h.turn_id) is True
    out = await settled(h)
    assert [m["role"] for m in env.think.history] == ["user", "assistant"]
    assert out.metadata["history_retracted"] is False


async def test_cancel_turn_before_commit_reports_clean_history() -> None:
    env, mid = build_split()
    r, sink = env.req("A問")
    h = env.co.submit(r)
    await until(lambda: len(sink.chunks) == 1)  # 生成中，EAGER 還沒 commit
    assert env.co.cancel_turn(h.turn_id) is True
    out = await settled(h)
    assert env.think.history == [] and out.metadata["history_retracted"] is True
    mid.set()


async def test_cancel_turn_none_while_commit_in_flight_still_retracts() -> None:
    env = build()
    original = env.think.commit
    started, release = asyncio.Event(), asyncio.Event()

    async def slow(turn_id: str, spoken: str, generated: str) -> None:
        started.set()
        await release.wait()
        await original(turn_id, spoken, generated)

    env.think.commit = slow  # type: ignore[method-assign]
    r, _ = env.req("你好嗎")
    h = env.co.submit(r)
    await asyncio.wait_for(started.wait(), SAFETY_S)  # EAGER commit 進行中
    assert env.co.cancel_turn(h.turn_id, commit="none") is True
    release.set()
    out = await settled(h)
    assert env.think.history == [] and out.metadata["history_retracted"] is True


async def test_cancel_turn_default_while_commit_in_flight_reports_not_retracted() -> None:
    env = build()
    original = env.think.commit
    started, release = asyncio.Event(), asyncio.Event()

    async def slow(turn_id: str, spoken: str, generated: str) -> None:
        started.set()
        await release.wait()
        await original(turn_id, spoken, generated)

    env.think.commit = slow  # type: ignore[method-assign]
    r, _ = env.req("你好嗎")
    h = env.co.submit(r)
    await asyncio.wait_for(started.wait(), SAFETY_S)
    assert env.co.cancel_turn(h.turn_id) is True
    release.set()
    out = await settled(h)
    # commit 可能被取消打斷而沒寫入，default 不去確認，保守回報「不保證」
    assert out.metadata["history_retracted"] is False


async def test_cancel_turn_claim_between_done_and_conclude_is_a_host_cancel() -> None:
    # 決定性重現：認領落在 phase=DONE 決定後、_mark_play_done 前
    env = build()
    r, sink = env.req("你好嗎")
    h = env.co.submit(r)
    await playing(sink)
    original = env.co._mark_play_done
    results: list[bool] = []

    def hooked(turn) -> None:
        if not results and turn is h:
            results.append(env.co.cancel_turn(h.turn_id, reason_code="global_stop", commit="none"))
        original(turn)

    env.co._mark_play_done = hooked  # type: ignore[method-assign]
    sink.finish()
    out = await settled(h)
    assert results == [True]
    assert out.status is OutcomeStatus.CANCELLED and out.aborted and out.cut_short
    assert out.cancel_reason == CancelReason.HOST.value
    assert out.metadata["reason_code"] == "global_stop"
    assert env.think.history == [] and out.metadata["history_retracted"] is True


async def test_cancel_turn_after_conclude_started_is_refused() -> None:
    env = build()
    r, sink = env.req("你好嗎", commit=CommitMode.SETTLED)
    h = env.co.submit(r)
    await playing(sink)
    original = env.think.commit
    results: list[bool] = []

    async def spy(turn_id: str, spoken: str, generated: str) -> None:
        results.append(env.co.cancel_turn(turn_id))  # 結算中（commit 前）才來
        await original(turn_id, spoken, generated)

    env.think.commit = spy  # type: ignore[method-assign]
    sink.finish()
    out = await settled(h)
    assert results == [False]  # 回 False，outcome 照常完成
    assert out.status is OutcomeStatus.COMPLETED and "reason_code" not in out.metadata


async def test_cancel_turn_none_retract_failures_are_observable(caplog) -> None:
    async def returns_false(_tid: str) -> bool:
        return False

    async def raises(_tid: str) -> bool:
        raise RuntimeError("boom")

    for label in ("false", "raises", "unsupported"):
        env = build()
        if label == "false":
            env.think.retract = returns_false  # type: ignore[method-assign]
        elif label == "raises":
            env.think.retract = raises  # type: ignore[method-assign]
        else:
            env.co.think = NoSettleThink(env.think)  # type: ignore[assignment]
        r, sink = env.req("你好嗎")
        h = env.co.submit(r)
        await playing(sink)
        assert env.co.cancel_turn(h.turn_id, commit="none") is True
        out = await settled(h)
        assert out.status is OutcomeStatus.CANCELLED
        assert out.metadata["history_retracted"] is False, label
        assert out.metadata["history_retract_error"], label
    assert "撤回歷史失敗" in caplog.text


async def test_cancel_turn_none_without_retract_falls_back_to_settle() -> None:
    class SettleOnly(NoSettleThink):
        async def settle(self, turn_id, spoken, generated=""):
            return await self.inner.settle(turn_id, spoken, generated)

    env = build()
    env.co.think = SettleOnly(env.think)  # type: ignore[assignment]
    r, sink = env.req("你好嗎")
    h = env.co.submit(r)
    await playing(sink)
    assert env.co.cancel_turn(h.turn_id, commit="none") is True
    out = await settled(h)
    assert [m["role"] for m in env.think.history] == ["user"]  # settle 只能移除 assistant 項
    assert out.metadata["history_retracted"] is False and out.metadata["history_retract_error"]


async def test_retract_drops_late_commit_and_pending_user_text() -> None:
    from uep.fakes.stream.stages import FakeThinkStage

    for stage in (build().think, FakeThinkStage()):
        utt = Utterance(text="你好", speaker_id="u1", turn_id="t-retract")
        agen = stage.stream(utt, coordinator_module.CancellationToken())
        await agen.__anext__()
        await agen.aclose()
        assert await stage.retract("t-retract") is True  # 尚未 commit 就撤回
        await stage.commit("t-retract", "回答", "回答")
        assert stage.history == []  # 晚到的 commit 被丟棄，暫存的使用者話語也清了
        await stage.commit("t-other", "別輪", "別輪")
        assert stage.history == [{"role": "assistant", "content": "別輪"}]
        assert await stage.retract("") is False


async def test_retract_after_commit_removes_user_and_assistant_only_for_that_turn() -> None:
    env = build()
    r1, s1 = env.req("A問", owner="u1")
    h1 = env.co.submit(r1)
    await playing(s1)
    s1.finish()
    await settled(h1)
    r2, s2 = env.req("B問", owner="u2")
    h2 = env.co.submit(r2)
    await playing(s2)
    s2.finish()
    await settled(h2)
    assert len(env.think.history) == 4
    assert await env.think.retract(h2.turn_id) is True
    assert [m["content"] for m in env.think.history][0] == "A問"
    assert len(env.think.history) == 2


def test_think_stages_satisfy_retract_capable() -> None:
    from uep.stream.contracts.stages import RetractCapable
    from uep.fakes.stream.stages import FakeThinkStage

    assert isinstance(FakeThinkStage(), RetractCapable)
    assert isinstance(build().think, RetractCapable)


async def test_cancel_turn_cancel_reason_is_host_not_shutdown() -> None:
    env, mid = build_split()
    r, sink = env.req("A")
    h = env.co.submit(r)
    await until(lambda: len(sink.chunks) == 1)
    assert env.co.cancel_turn(h.turn_id) is True
    out = await settled(h)
    assert out.cancel_reason == CancelReason.HOST.value == "host_cancelled"
    assert h.token.reason is CancelReason.HOST
    mid.set()


async def test_cancel_turn_reason_code_is_stripped_and_length_limited() -> None:
    env = build()
    r, sink = env.req("A")
    h = env.co.submit(r)
    await playing(sink)
    with pytest.raises(ValueError, match="64"):
        env.co.cancel_turn(h.turn_id, reason_code="x" * 65)
    assert env.co.cancel_turn(h.turn_id, reason_code="  global_stop \n") is True
    assert (await settled(h)).metadata["reason_code"] == "global_stop"
    ok = env.co.cancel_turn("nope", reason_code="y" * 64)
    assert ok is False  # 64 字元可以


async def test_cancel_turn_validates_arguments_even_after_aclose() -> None:
    env = build()
    await env.co.aclose()
    with pytest.raises(ValueError, match="reason_code"):
        env.co.cancel_turn("t", reason_code="")
    assert env.co.cancel_turn("t") is False


async def test_cancel_turn_none_fallback_path_still_retracts_history() -> None:
    # _conclude 內部拋例外 → 走保底 outcome；EAGER 已 commit 的內容仍要撤回並標記
    env = build()
    r, sink = env.req("你好嗎")
    h = env.co.submit(r)
    await playing(sink)
    assert [m["role"] for m in env.think.history] == ["user", "assistant"]

    async def boom(_sink) -> None:
        raise RuntimeError("confirm boom")

    env.co._confirm_stopped = boom  # type: ignore[method-assign]
    assert env.co.cancel_turn(h.turn_id, reason_code="global_stop", commit="none") is True
    out = await settled(h)
    assert out.status is OutcomeStatus.FAILED and "confirm boom" in (out.error or "")
    assert out.metadata["reason_code"] == "global_stop"
    assert env.think.history == []
    assert out.metadata["history_retracted"] is True


async def test_cancel_turn_default_fallback_path_reports_conservative_value() -> None:
    env = build()
    r, sink = env.req("你好嗎")
    h = env.co.submit(r)
    await playing(sink)

    async def boom(_sink) -> None:
        raise RuntimeError("confirm boom")

    env.co._confirm_stopped = boom  # type: ignore[method-assign]
    assert env.co.cancel_turn(h.turn_id) is True
    out = await settled(h)
    assert out.metadata["history_retracted"] is False  # EAGER 已 commit，不保證乾淨
