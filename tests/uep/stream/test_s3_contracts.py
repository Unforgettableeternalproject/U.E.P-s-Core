"""S3 契約補洞的測試：Protocol 結構檢查、單 stream 不變條件、fake 呼叫計數、
取消後不再送文字、sink 停止後播放量凍結。

對應 Core（U.E.P）以 EchoPipelineHost 託管 ES 時需要的觀測點與契約。
"""

from __future__ import annotations

import asyncio

import pytest

from uep.stream.adapters.input_multichannel import MultiChannelSttInputStage
from uep.stream.adapters.think_llm import LLMThinkStage
from uep.stream.contracts import (
    MemoryRetriever,
    MultiChannelInputStage,
    SettleCapable,
    ThinkHostSurface,
    ThinkStage,
)
from uep.stream.contracts.cancellation import CancelReason
from uep.stream.contracts.output import OutcomeStatus
from uep.stream.contracts.types import AudioChunk, Sentence, Utterance
from uep.stream.pipeline import PipelineRunner
from uep.fakes.stream import FakeAudioSink, FakeSpeakStage, FakeThinkStage
from uep.stream.io.sinks import WavFileSink

from .scenario_support import RESPONSE, SENTENCES, until
from .test_coordinator import SAFETY_S, build, playing, settled
from .test_think_llm import FakeBackend, FakeMemory

S0 = SENTENCES[0]


# ───────────────────────── A4／A6：Protocol 結構檢查 ─────────────────────────


def test_LLMThinkStage滿足Think宿主面():
    stage = LLMThinkStage(FakeBackend())
    assert isinstance(stage, ThinkStage)
    assert isinstance(stage, SettleCapable)
    assert isinstance(stage, ThinkHostSurface)


def test_宿主面缺成員時不成立():
    class OnlyThink:
        name = "x"

        async def prepare(self) -> None: ...

        async def aclose(self) -> None: ...

        def stream(self, utterance, token): ...

        async def commit(self, turn_id, spoken_text, generated_text) -> None: ...

    assert isinstance(OnlyThink(), ThinkStage)
    assert not isinstance(OnlyThink(), ThinkHostSurface)


def test_多聲道輸入實作滿足Protocol含回呼成員():
    stage = MultiChannelSttInputStage()
    assert isinstance(stage, MultiChannelInputStage)
    assert stage.on_voice_event is None and stage.on_partial is None


def test_多聲道Protocol要求回呼成員():
    class NoCallbacks:
        name = "x"

        async def prepare(self) -> None: ...

        async def aclose(self) -> None: ...

        def open(self, token): ...

        def feed(self, speaker_id, pcm, ts=None) -> None: ...

        def join(self, speaker_id, **opts) -> None: ...

        def leave(self, speaker_id, flush=True) -> None: ...

        def discard(self, speaker_id=None) -> None: ...

    assert not isinstance(NoCallbacks(), MultiChannelInputStage)


def test_記憶實作滿足MemoryRetriever():
    from uep.stream.adapters.memory_echo import EchoMemoryAdapter

    assert isinstance(FakeMemory(), MemoryRetriever)
    assert isinstance(EchoMemoryAdapter(engine=object()), MemoryRetriever)


async def test_memory_retrieve_kwargs原樣傳給記憶():
    seen: dict = {}

    class KwMemory:
        async def retrieve(self, text: str, **kwargs):
            seen.update(kwargs)
            return None

    stage = LLMThinkStage(FakeBackend(delay_s=0.001), memory=KwMemory())
    utt = Utterance(
        text="嗨", speaker_id="A",
        metadata={"memory_retrieve_kwargs": {"audience": "public", "actor_id": "u1"}},
    )
    from uep.stream.contracts.cancellation import CancellationToken

    _ = [s async for s in stage.stream(utt, CancellationToken())]
    await asyncio.sleep(0.01)
    assert seen == {"audience": "public", "actor_id": "u1"}


# ───────────────────────── E3：單 stream 不變條件 ─────────────────────────


async def test_排隊兩輪時active_scope不交錯():
    env = build()
    entry_scopes: list[str | None] = []
    exit_scopes: list[str | None] = []
    original = env.think.stream

    async def spy(utterance, token):
        entry_scopes.append(env.think.active_scope)  # stream 開始前必須已清空
        async for sentence in original(utterance, token):
            yield sentence
        exit_scopes.append(env.think.active_scope)

    env.think.stream = spy  # type: ignore[method-assign]

    ra, sink_a = env.req("A", owner="u1")
    rb, sink_b = env.req("B", owner="u2", barge=("u2",))
    ha, hb = env.co.submit(ra), env.co.submit(rb)
    await playing(sink_a)
    sink_a.finish()
    await settled(ha)
    await until(lambda: len(sink_b.chunks) == 4)
    sink_b.finish()
    await settled(hb)

    assert entry_scopes == [None, None]
    assert exit_scopes == [None, None]
    assert env.backend.scopes == ["u1", "u2"]  # 各輪 LLM 呼叫時看到的是自己的 scope
    assert env.think.active_scope is None


# ───────────────────────── E4：fake 呼叫計數 ─────────────────────────


async def test_fake三層一次輸入各被呼叫一次():
    think = FakeThinkStage(responses=[RESPONSE], token_interval_s=0.0, memory_delay_s=0.0)
    speak = FakeSpeakStage(first_chunk_extra_s=0.0, rtf=0.0)
    assert think.stream_calls == 0 and speak.speak_calls == 0
    runner = PipelineRunner(think_stage=think, speak_stage=speak, sink=FakeAudioSink())
    await runner.run_turn(Utterance(text="你好"))
    assert think.stream_calls == 1
    assert speak.speak_calls == 1
    await runner.run_turn(Utterance(text="再一次"))
    assert (think.stream_calls, speak.speak_calls) == (2, 2)


# ───────────────────────── E5：整輪取消後不再送文字 ─────────────────────────


async def test_整輪取消後不再呼叫on_sentence():
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
    env.co.cancel_all("left")
    block.set()
    out = await asyncio.wait_for(h.settled, SAFETY_S)
    assert out.status is OutcomeStatus.CANCELLED
    assert out.cancel_reason == CancelReason.SHUTDOWN.value

    count_at_settle = len(sent)
    await asyncio.sleep(0.05)  # 給任何晚到的文字足夠的時間
    for _ in range(50):
        await asyncio.sleep(0)
    assert len(sent) == count_at_settle == 1
    assert sent == [S0]


# ───────────────────────── D4：sink 停止後播放量凍結 ─────────────────────────


def _chunk(seconds: float = 1.0, index: int = 0) -> AudioChunk:
    rate = 16000
    return AudioChunk(
        pcm=b"\x00\x00" * int(rate * seconds), sample_rate=rate, turn_id="t", sentence_index=index,
    )


def _make_fake(realtime: bool):
    return FakeAudioSink(realtime=realtime)


def _make_wav(realtime: bool, tmp_path):
    return WavFileSink(tmp_path / "out.wav", realtime=realtime)


@pytest.mark.parametrize("kind", ["fake", "wav"])
@pytest.mark.parametrize("realtime", [True, False])
async def test_sink停止後played_duration凍結(kind: str, realtime: bool, tmp_path):
    sink = _make_fake(realtime) if kind == "fake" else _make_wav(realtime, tmp_path)
    await sink.write(_chunk(2.0))
    await asyncio.sleep(0.05)
    sink.stop()
    frozen = sink.played_duration_s
    assert frozen > 0.0
    await asyncio.sleep(0.05)
    assert sink.played_duration_s == frozen  # 時間流逝不再推進
    await sink.write(_chunk(1.0, index=1))  # stop 後晚到的 chunk 被丟棄
    assert sink.played_duration_s == frozen
    sink.stop()  # 冪等
    assert sink.played_duration_s == frozen
