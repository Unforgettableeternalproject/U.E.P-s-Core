"""GapFiller（D 段）——即時合成、排隊語意、一輪一聲、按語言挑、打點。"""

from __future__ import annotations

import asyncio

import pytest

from uep.stream.contracts.cancellation import CancellationToken
from uep.stream.contracts.types import AudioChunk, Utterance
from uep.stream.gap_filler import GAP_SENTENCE_INDEX, GapFiller, GapFillerPolicy
from uep.stream.pipeline import PipelineRunner
from uep.stream.tracer import MARK_GAP_FIRST_AUDIO, LatencyTracer
from uep.fakes.stream import FakeSpeakStage, FakeThinkStage


class ListSink:
    def __init__(self) -> None:
        self.chunks: list[AudioChunk] = []
        self.played_duration_s = 0.0

    async def write(self, chunk: AudioChunk) -> None:
        self.chunks.append(chunk)
        self.played_duration_s += chunk.duration_s

    def stop(self) -> None:
        pass

    async def drain(self) -> None:
        pass


class CountingSpeakStage(FakeSpeakStage):
    """記錄合成次數——驗證「即時合成、一輪一次」而非預建。"""

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.stream_calls = 0

    def stream(self, sentences, token):  # noqa: ANN001
        self.stream_calls += 1
        return super().stream(sentences, token)


def _policy(**kw) -> GapFillerPolicy:
    base = dict(
        enabled=True,
        start_delay_s=0.0,
        phrases={"zh": ["嗯…"], "en": ["Hmm…"]},
    )
    base.update(kw)
    return GapFillerPolicy(**base)


def _gap(policy: GapFillerPolicy | None = None, stage=None) -> GapFiller:  # noqa: ANN001
    gap = GapFiller(policy or _policy())
    gap.attach(stage or FakeSpeakStage(first_chunk_extra_s=0.0, rtf=0.0))
    return gap


async def test_attach前不合成_attach後每輪即時合成一次():
    """核心變更（2026-08-24）：不預建語音庫，start 之後才合成這一輪那一句。"""
    stage = CountingSpeakStage(first_chunk_extra_s=0.0, rtf=0.0)
    gap = _gap(stage=stage)
    assert gap.ready
    assert stage.stream_calls == 0, "attach 不合成任何東西"

    sink = ListSink()
    gap.start(sink, "t", CancellationToken(), language="zh")
    await asyncio.sleep(0.05)
    await gap.stop()
    assert stage.stream_calls == 1, "一輪只合成挑中的那一句"
    assert sink.chunks and gap.last_emitted == "嗯…"


async def test_按語言挑_沒對應池子退回預設語言():
    gap = _gap(_policy(phrases={"zh": ["嗯…"], "en": ["Hmm…"]}, default_language="zh"))
    assert gap.phrases_for("en") == ["Hmm…"]
    assert gap.phrases_for("en-US") == ["Hmm…"]
    assert gap.phrases_for("ja") == ["嗯…"], "沒日文池子 → 預設語言"
    assert gap.phrases_for(None) == ["嗯…"]


async def test_不連續重複():
    gap = _gap(_policy(phrases={"zh": ["A", "B"]}))
    picks = [gap._pick_text("zh") for _ in range(10)]
    assert all(a != b for a, b in zip(picks, picks[1:], strict=False))


async def test_墊片排在真音訊前面_且只有一聲():
    """核心語意：排隊、不打斷、一輪最多一聲。"""
    sink = ListSink()
    gap = _gap()
    token = CancellationToken()
    gap.start(sink, "t", token, language="zh")
    await asyncio.sleep(0.05)
    await gap.stop()  # 真音訊來了
    await sink.write(
        AudioChunk(pcm=b"\x00\x00" * 100, sample_rate=100, turn_id="t", sentence_index=0)
    )
    await asyncio.sleep(0.05)

    idx = [c.sentence_index for c in sink.chunks]
    assert idx[0] == GAP_SENTENCE_INDEX
    assert idx[-1] == 0
    assert idx.count(GAP_SENTENCE_INDEX) == len(sink.chunks) - 1
    assert gap.last_emitted == "嗯…"
    assert gap.emitted_duration_s > 0


async def test_真音訊比起播延遲還快_就不出聲():
    sink = ListSink()
    gap = _gap(_policy(start_delay_s=0.5))
    gap.start(sink, "t", CancellationToken())
    await gap.stop()  # 立刻就有真音訊
    assert sink.chunks == []
    assert gap.last_emitted is None
    assert gap.emitted_duration_s == 0.0


async def test_合成到一半真音訊來了_直接放棄不寫():
    """即時合成的新邊界：stop 落在合成期間 → 一個 chunk 都不寫，
    stop 立刻返回，真音訊不等墊片。"""
    stage = CountingSpeakStage(first_chunk_extra_s=0.5, rtf=0.0)  # 合成要 0.5s
    sink = ListSink()
    gap = _gap(stage=stage)
    gap.start(sink, "t", CancellationToken(), language="zh")
    await asyncio.sleep(0.1)  # 已過 delay、正在合成
    assert stage.stream_calls == 1
    t0 = asyncio.get_event_loop().time()
    await gap.stop()
    assert asyncio.get_event_loop().time() - t0 < 0.2, "stop 不等合成跑完"
    assert sink.chunks == []
    assert gap.last_emitted is None
    assert gap.emitted_duration_s == 0.0


async def test_stop會取消墊片專屬token但不取消回合token():
    class WaitingSpeakStage:
        def __init__(self):
            self.started = asyncio.Event()
            self.token = None

        def stream(self, sentences, token):  # noqa: ANN001
            async def gen():
                self.token = token
                self.started.set()
                await token.wait()
                yield  # pragma: no cover

            return gen()

    stage = WaitingSpeakStage()
    gap = _gap(stage=stage)
    parent = CancellationToken()
    gap.start(ListSink(), "t", parent, language="zh")
    await asyncio.wait_for(stage.started.wait(), timeout=1)

    await gap.stop()

    assert stage.token is not None and stage.token.is_cancelled
    assert not parent.is_cancelled, "墊片取消不得向上取消回合 token"


async def test_合成失敗_這輪安靜_不炸管線():
    class BrokenSpeakStage:
        def stream(self, sentences, token):  # noqa: ANN001
            async def gen():
                raise RuntimeError("CUDA error: device-side assert triggered")
                yield  # pragma: no cover

            return gen()

    sink = ListSink()
    gap = _gap(stage=BrokenSpeakStage())
    gap.start(sink, "t", CancellationToken(), language="zh")
    await asyncio.sleep(0.05)
    await gap.stop()
    assert sink.chunks == []
    assert gap.last_emitted is None


async def test_關閉或沒attach就什麼都不做():
    sink = ListSink()
    gap = _gap(_policy(enabled=False))
    gap.start(sink, "t", CancellationToken())
    await asyncio.sleep(0.02)
    assert sink.chunks == []
    gap2 = GapFiller(_policy())  # enabled 但沒 attach SpeakStage
    assert not gap2.ready
    gap2.start(sink, "t", CancellationToken())
    await asyncio.sleep(0.02)
    assert sink.chunks == []


async def test_runner_整合_墊片先於真音訊_打點_感知TTFA():
    tracer = LatencyTracer()
    sink = ListSink()
    gap = _gap()
    runner = PipelineRunner(
        think_stage=FakeThinkStage(first_token_delay_s=0.3, memory_delay_s=0.0, tracer=tracer),
        speak_stage=FakeSpeakStage(first_chunk_extra_s=0.0, rtf=0.0),
        sink=sink,
        tracer=tracer,
        gap_filler=gap,
    )
    await runner.prepare()
    result = await runner.run_turn(Utterance(text="你好嗎", language="zh"))

    assert result.phase.value == "done"
    idx = [c.sentence_index for c in sink.chunks]
    assert idx[0] == GAP_SENTENCE_INDEX
    assert GAP_SENTENCE_INDEX not in idx[idx.index(0):], "真音訊之後不得再有墊片"

    trace = tracer.get(result.turn_id)
    assert MARK_GAP_FIRST_AUDIO in trace.marks
    segs = trace.segments_ms()
    assert segs["gap_audio"] is not None
    assert segs["perceived_ttfa"] <= segs["ttfa"]
    assert "感知 TTFA" in tracer.report(result.turn_id)
    # spoken_text 推算要扣掉墊片時長——否則「嗯…」那段會被算成真句子已播出
    assert result.spoken_text == result.generated_text
    assert result.spoken_duration_s == pytest.approx(
        sum(c.duration_s for c in sink.chunks if c.sentence_index != GAP_SENTENCE_INDEX), abs=1e-6
    )


async def test_runner_沒給gap_filler行為不變():
    sink = ListSink()
    runner = PipelineRunner(
        think_stage=FakeThinkStage(first_token_delay_s=0.0, memory_delay_s=0.0),
        speak_stage=FakeSpeakStage(first_chunk_extra_s=0.0, rtf=0.0),
        sink=sink,
    )
    await runner.prepare()
    result = await runner.run_turn(Utterance(text="你好"))
    assert result.phase.value == "done"
    assert all(c.sentence_index >= 0 for c in sink.chunks)
