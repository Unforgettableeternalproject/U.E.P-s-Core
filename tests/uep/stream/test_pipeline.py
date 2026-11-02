"""PipelineRunner 的端到端測試——Phase 0 的驗收核心。

重點在 barge-in 善後：寫回對話歷史的必須是**實際播出**的文字，
不是 LLM 生成的全部。寫錯會讓 LLM 以為自己講完了整段，下一輪指代全錯。
"""

from __future__ import annotations

import asyncio

from uep.stream.contracts.cancellation import CancelReason
from uep.stream.contracts.turn import TurnPolicy
from uep.stream.contracts.types import TurnPhase, Utterance
from uep.stream.pipeline import PipelineRunner
from uep.stream.splitter import SplitPolicy
from uep.stream.tracer import (
    MARK_SPEAK_FIRST_CHUNK,
    MARK_THINK_FIRST_TOKEN,
    LatencyTracer,
)
from uep.fakes.stream import (
    FakeAudioSink,
    FakeAudioSource,
    FakeInputStage,
    FakeSpeakStage,
    FakeThinkStage,
)

RESPONSE = "第一句很短。第二句稍微長一點點。第三句就結束了。"


def build_runner(
    *,
    realtime: bool = False,
    stt_delay: float = 0.0,
    llm_delay: float = 0.0,
    memory_delay: float = 0.0,
    rtf: float = 0.0,
    first_chunk_extra: float = 0.0,
    prompts: list[str] | None = None,
) -> tuple[PipelineRunner, FakeThinkStage, FakeAudioSink, LatencyTracer]:
    tracer = LatencyTracer()
    think = FakeThinkStage(
        responses=[RESPONSE],
        first_token_delay_s=llm_delay,
        token_interval_s=0.0,
        memory_delay_s=memory_delay,
        split_policy=SplitPolicy(),
        tracer=tracer,
    )
    sink = FakeAudioSink(realtime=realtime)
    runner = PipelineRunner(
        input_stage=FakeInputStage(texts=prompts or ["你好"], stt_delay_s=stt_delay),
        think_stage=think,
        speak_stage=FakeSpeakStage(first_chunk_extra_s=first_chunk_extra, rtf=rtf),
        sink=sink,
        tracer=tracer,
    )
    return runner, think, sink, tracer


# --- 正常流程 ---


async def test_一輪對話完整跑通():
    runner, think, sink, _ = build_runner()
    result = await runner.run_turn(Utterance(text="你好"))

    assert result.phase is TurnPhase.DONE
    assert result.error is None
    assert result.generated_text == RESPONSE
    assert sink.chunks, "應該要有音訊產出"


async def test_正常結束時_spoken_等於_generated():
    runner, think, _, _ = build_runner()
    result = await runner.run_turn(Utterance(text="你好"))
    assert result.spoken_text == result.generated_text


async def test_commit_收到實際播出的文字():
    runner, think, _, _ = build_runner()
    result = await runner.run_turn(Utterance(text="你好"))
    assert len(think.committed) == 1
    turn_id, spoken, generated = think.committed[0]
    assert turn_id == result.turn_id
    assert spoken == result.spoken_text
    assert generated == RESPONSE


async def test_音訊段的_sentence_index_對得上():
    runner, _, sink, _ = build_runner()
    await runner.run_turn(Utterance(text="你好"))
    indices = [c.sentence_index for c in sink.chunks]
    assert indices == sorted(indices), "播放順序必須依句序"


async def test_run_session_跑多輪():
    runner, _, _, _ = build_runner(prompts=["一", "二", "三"])
    results = [r async for r in runner.run_session(FakeAudioSource())]
    assert len(results) == 3
    assert all(r.phase is TurnPhase.DONE for r in results)


# --- barge-in ---


async def test_播放確認逾時不得回報完成():
    runner, _, sink, _ = build_runner()

    async def fail_drain():
        raise TimeoutError("播放端未確認完成")

    sink.drain = fail_drain
    result = await runner.run_turn(Utterance(text="你好"))
    assert result.phase is TurnPhase.FAILED
    assert "TimeoutError" in result.error
    assert not runner.is_speaking


def test_墊片尚未播完時回答播放量不為負數():
    runner, _, _, _ = build_runner()
    spoken, duration = runner._resolve_spoken({}, [(0, 1.0)], 1.0, gap_duration=0.5)
    assert spoken == ""
    assert duration == 0.0


async def test_播放完成同時插話仍記為取消():
    runner, _, sink, _ = build_runner()

    async def interrupt_on_drain():
        runner.interrupt(CancelReason.BARGE_IN)

    sink.drain = interrupt_on_drain
    result = await runner.run_turn(Utterance(text="你好"))
    assert result.phase is TurnPhase.CANCELLED
    assert result.cancel_reason == CancelReason.BARGE_IN.value


async def test_插話等待播放清理完成才結束():
    runner, _, sink, _ = build_runner()
    entered = asyncio.Event()
    cleaned = asyncio.Event()

    async def wait_for_playback():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            cleaned.set()

    sink.drain = wait_for_playback
    task = asyncio.create_task(runner.run_turn(Utterance(text="你好")))
    await asyncio.wait_for(entered.wait(), timeout=2)
    runner.interrupt(CancelReason.BARGE_IN)
    result = await asyncio.wait_for(task, timeout=2)
    assert result.phase is TurnPhase.CANCELLED
    assert cleaned.is_set()


async def test_插話中止_phase_變成_cancelled():
    runner, _, _, _ = build_runner(realtime=True, rtf=0.05)

    async def interrupt_soon():
        await asyncio.sleep(0.15)
        runner.interrupt(CancelReason.BARGE_IN)

    asyncio.ensure_future(interrupt_soon())
    result = await runner.run_turn(Utterance(text="你好"))

    assert result.phase is TurnPhase.CANCELLED
    assert result.cancel_reason == CancelReason.BARGE_IN.value


async def test_插話後_spoken_短於_generated():
    """這是 barge-in 善後的核心：使用者沒聽到的不能寫進對話歷史。"""
    runner, think, _, _ = build_runner(realtime=True, rtf=0.05)

    async def interrupt_soon():
        await asyncio.sleep(0.15)
        runner.interrupt(CancelReason.BARGE_IN)

    asyncio.ensure_future(interrupt_soon())
    result = await runner.run_turn(Utterance(text="你好"))

    assert len(result.spoken_text) < len(RESPONSE)
    _, spoken, generated = think.committed[0]
    assert spoken == result.spoken_text
    assert len(spoken) <= len(generated)


async def test_插話讓_sink_立刻停():
    runner, _, sink, _ = build_runner(realtime=True, rtf=0.05)

    async def interrupt_soon():
        await asyncio.sleep(0.15)
        runner.interrupt(CancelReason.BARGE_IN)

    asyncio.ensure_future(interrupt_soon())
    await runner.run_turn(Utterance(text="你好"))

    played = sink.played_duration_s
    await asyncio.sleep(0.1)
    assert sink.played_duration_s == played, "stop() 之後播放進度必須凍結"


async def test_插話中止上游生成():
    """取消要往上游傳播：TTS 停播 → LLM 停生成 → 丟棄剩餘 token。"""
    runner, _, _, _ = build_runner(realtime=True, rtf=0.05)

    async def interrupt_soon():
        await asyncio.sleep(0.15)
        runner.interrupt(CancelReason.BARGE_IN)

    asyncio.ensure_future(interrupt_soon())
    result = await runner.run_turn(Utterance(text="你好"))

    assert len(result.generated_text) < len(RESPONSE), (
        "LLM 應該在中止時就停止生成，而不是把整段跑完"
    )


async def test_沒有進行中的_turn_時插話回傳_False():
    runner, _, _, _ = build_runner()
    assert runner.interrupt() is False


async def test_插話後仍會_commit():
    """善後不能因為被取消就跳過——不 commit 等於這一輪從沒發生過。"""
    runner, think, _, _ = build_runner(realtime=True, rtf=0.05)

    async def interrupt_soon():
        await asyncio.sleep(0.15)
        runner.interrupt(CancelReason.BARGE_IN)

    asyncio.ensure_future(interrupt_soon())
    await runner.run_turn(Utterance(text="你好"))
    assert len(think.committed) == 1


# --- 打點 ---


async def test_打點涵蓋關鍵階段():
    runner, _, _, tracer = build_runner(llm_delay=0.01, first_chunk_extra=0.01)
    result = await runner.run_turn(Utterance(text="你好"))
    trace = tracer.get(result.turn_id)

    assert trace is not None
    assert MARK_THINK_FIRST_TOKEN in trace.marks
    assert MARK_SPEAK_FIRST_CHUNK in trace.marks
    assert trace.ttfa_ms is not None and trace.ttfa_ms > 0


async def test_ttfa_包含_stt_延遲():
    """STT 的轉錄耗時是使用者的等待，必須算進 TTFA。"""
    fast, _, _, tracer_fast = build_runner(stt_delay=0.0)
    results = [r async for r in fast.run_session(FakeAudioSource())]
    ttfa_fast = tracer_fast.get(results[0].turn_id).ttfa_ms

    slow, _, _, tracer_slow = build_runner(stt_delay=0.3)
    results = [r async for r in slow.run_session(FakeAudioSource())]
    ttfa_slow = tracer_slow.get(results[0].turn_id).ttfa_ms

    assert ttfa_slow > ttfa_fast + 200


async def test_memory_與_llm_平行_不擋首句():
    """§7.4 定案：retrieve 與 LLM prefill 平行，首句不等它。"""
    runner, _, _, tracer = build_runner(memory_delay=0.5, llm_delay=0.0)
    result = await runner.run_turn(Utterance(text="你好"))
    trace = tracer.get(result.turn_id)

    first_token = trace.rel_ms(MARK_THINK_FIRST_TOKEN)
    assert first_token is not None
    assert first_token < 400, "首 token 不該等 memory retrieve 的 500ms"


async def test_句數與音訊段數有記帳():
    runner, _, _, tracer = build_runner()
    result = await runner.run_turn(Utterance(text="你好"))
    trace = tracer.get(result.turn_id)
    assert trace.sentence_count > 0
    assert trace.chunk_count > 0
    assert trace.split_reasons


# --- 錯誤處理 ---


async def test_stage_拋錯不會拖垮_session():
    class BoomSpeak:
        name = "boom"

        async def prepare(self):
            return

        async def aclose(self):
            return

        async def stream(self, sentences, token):
            async for _ in sentences:
                raise RuntimeError("TTS 爆了")
            return
            yield  # pragma: no cover

    tracer = LatencyTracer()
    think = FakeThinkStage(responses=[RESPONSE], token_interval_s=0.0, memory_delay_s=0.0)
    runner = PipelineRunner(
        think_stage=think,
        speak_stage=BoomSpeak(),
        tracer=tracer,
    )
    result = await runner.run_turn(Utterance(text="你好"))

    assert result.phase is TurnPhase.FAILED
    assert "TTS 爆了" in (result.error or "")


# --- interruption_backoff_s ---


class FakeClock:
    """可手動撥動的時鐘，backoff 的時間差不靠真實 sleep 推進。"""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def build_backoff_runner(backoff_s: float, clock: FakeClock) -> PipelineRunner:
    runner, _, _, _ = build_runner(realtime=True, rtf=0.05)
    runner.policy = TurnPolicy(interruption_backoff_s=backoff_s)
    runner._clock = clock
    return runner


async def _interrupt_first_turn(runner: PipelineRunner, reason: CancelReason) -> None:
    async def interrupt_soon():
        await asyncio.sleep(0.1)
        runner.interrupt(reason)

    asyncio.ensure_future(interrupt_soon())
    await runner.run_turn(Utterance(text="你好"))


async def test_backoff_冷卻只延後首個音訊_think_照常開跑():
    clock = FakeClock()
    runner = build_backoff_runner(2.0, clock)
    await _interrupt_first_turn(runner, CancelReason.BARGE_IN)
    clock.now += 1.8  # 還剩 0.2s
    sink = runner.sink
    sink.chunks.clear()

    loop = asyncio.get_event_loop()
    t0 = loop.time()
    first_write_at: list[float] = []
    orig_write = sink.write

    async def spy_write(chunk):
        first_write_at.append(loop.time() - t0)
        await orig_write(chunk)

    sink.write = spy_write
    result = await runner.run_turn(Utterance(text="再來"))
    trace = runner.tracer.get(result.turn_id)

    assert result.phase is TurnPhase.DONE
    assert first_write_at and first_write_at[0] >= 0.2 - 0.02
    # think 在冷卻期間就已產出首字，沒有被延後
    think_at = trace.marks[MARK_THINK_FIRST_TOKEN]
    speak_at = trace.marks[MARK_SPEAK_FIRST_CHUNK]
    assert speak_at - think_at >= 0.15


async def test_backoff_已過期則不延後():
    clock = FakeClock()
    runner = build_backoff_runner(2.0, clock)
    await _interrupt_first_turn(runner, CancelReason.BARGE_IN)
    clock.now += 2.0
    assert runner._backoff_remaining_s() == 0.0


async def test_backoff_為_0_時行為不變():
    clock = FakeClock()
    runner = build_backoff_runner(0.0, clock)
    await _interrupt_first_turn(runner, CancelReason.BARGE_IN)
    assert runner._backoff_remaining_s() == 0.0


async def test_非_barge_in_原因不起算_backoff():
    clock = FakeClock()
    for reason in (CancelReason.SHUTDOWN, CancelReason.SUPERSEDED):
        runner = build_backoff_runner(2.0, clock)
        await _interrupt_first_turn(runner, reason)
        assert runner._backoff_remaining_s() == 0.0


async def test_沒有進行中的_turn_時插話不起算_backoff():
    clock = FakeClock()
    runner = build_backoff_runner(2.0, clock)
    assert runner.interrupt(CancelReason.BARGE_IN) is False
    assert runner._backoff_remaining_s() == 0.0


async def test_backoff_等待中仍可被取消():
    clock = FakeClock()
    runner = build_backoff_runner(60.0, clock)
    await _interrupt_first_turn(runner, CancelReason.BARGE_IN)

    async def shutdown_soon():
        await asyncio.sleep(0.05)
        runner.interrupt(CancelReason.SHUTDOWN)

    asyncio.ensure_future(shutdown_soon())
    result = await asyncio.wait_for(runner.run_turn(Utterance(text="再來")), timeout=2)
    assert result.phase is TurnPhase.CANCELLED
    assert result.cancel_reason == CancelReason.SHUTDOWN.value
