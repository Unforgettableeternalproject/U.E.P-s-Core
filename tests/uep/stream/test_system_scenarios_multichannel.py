"""系統情境測試：多聲道輸入 × 機器人說話中的插話（零 GPU、零 token）。

接線照 ``StreamService._voice_loop`` 的做法：``on_voice_event`` →
``DurationInterruptionDetector`` →（機器人講話中才）``runner.interrupt``。
音訊用常數振幅 DC，第一個樣本值當作「這段話是誰說的」，fake backend 把它
原樣轉成文字，所以 ``Utterance.text`` 與 ``speaker_id`` 必須成對。

時間全部用音訊樣本數推進（VAD 的時鐘）與假 clock；``tick_interval_s`` 設極大，
背景補靜音不會介入，結果不依賴牆鐘。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from uep.stream.adapters.input_multichannel import MultiChannelSttInputStage
from uep.stream.contracts.cancellation import CancelReason
from uep.stream.contracts.turn import TurnPolicy, VoiceEvent
from uep.stream.contracts.types import TurnPhase, TurnResult, Utterance
from uep.stream.pipeline import PipelineRunner
from uep.stream.tracer import LatencyTracer
from uep.stream.turn_detector import DurationInterruptionDetector

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
from .test_input_multichannel import EchoBackend, FakeClock, Session, dc, quiet, split

S0, S1, _, _ = SENTENCES
SAFETY_S = 5.0


@dataclass
class Env:
    stage: MultiChannelSttInputStage
    session: Session
    runner: PipelineRunner
    backend: ScriptBackend
    speak: GateSpeak
    sink: ManualSink
    events: list[VoiceEvent] = field(default_factory=list)
    interrupts: list[str | None] = field(default_factory=list)
    """每次成功的 runner.interrupt() 是哪個聲道的事件觸發的。"""

    def feed(self, speaker_id: str, pcm: bytes) -> None:
        for chunk in split(pcm):
            self.stage.feed(speaker_id, chunk)

    def feed_interleaved(self, parts: dict[str, bytes]) -> None:
        """多個說話者的音訊逐 chunk 交錯餵入——模擬同時說話。"""
        streams = {sid: split(pcm) for sid, pcm in parts.items()}
        for i in range(max(len(s) for s in streams.values())):
            for sid, chunks in streams.items():
                if i < len(chunks):
                    self.stage.feed(sid, chunks[i])

    async def next_utterances(self, n: int) -> list[Utterance]:
        before = len(self.session.results)
        results = await self.session.wait_for(before + n, timeout=SAFETY_S)
        return list(results[before:])

    async def start_bot_turn(self, utterance: Utterance) -> asyncio.Task[TurnResult]:
        """讓機器人回答 ``utterance`` 並停在『第二句播放中、第三句 TTS 未好』。"""
        self.sink = self.runner.sink = ManualSink()
        self.speak = self.runner.speak_stage = GateSpeak()
        self.speak.open(0, 1)
        task = asyncio.ensure_future(self.runner.run_turn(utterance))
        await until(lambda: len(self.sink.chunks) == 2 and self.speak.seen == [0, 1, 2])
        self.sink.play(2.0)
        assert self.runner.is_speaking
        return task


async def make_env() -> Env:
    policy = TurnPolicy(
        silence_threshold_s=0.3, min_utterance_s=0.2, interruption_min_speech_s=0.4
    )
    clock = FakeClock()
    holder: list[Env] = []

    detector = DurationInterruptionDetector(policy)

    def on_event(event: VoiceEvent) -> None:
        env = holder[0]
        env.events.append(event)
        runner = env.runner
        if runner.is_speaking and detector.evaluate(event).should_interrupt:
            if runner.interrupt(CancelReason.BARGE_IN):
                env.interrupts.append(event.speaker_id)

    stage = MultiChannelSttInputStage(
        backend=EchoBackend(),
        policy=policy,
        clock=clock,
        tick_interval_s=1000.0,
        on_voice_event=on_event,
    )
    backend = ScriptBackend()
    think = build_think(backend)
    sink, speak = ManualSink(), GateSpeak()
    runner = PipelineRunner(
        think_stage=think, speak_stage=speak, sink=sink, tracer=LatencyTracer(), policy=policy
    )
    session = Session(stage)
    await session.ready()
    env = Env(stage, session, runner, backend, speak, sink)
    holder.append(env)
    return env


async def test_短促附和聲不插話_兩人同時說話時由持續說話者觸發() -> None:
    env = await make_env()
    try:
        # A 先說一句，機器人開始回答
        env.feed("A", dc(1000, 0.5) + quiet(0.6))
        (utt_a,) = await env.next_utterances(1)
        assert (utt_a.speaker_id, utt_a.text) == ("A", "1000")
        assert env.interrupts == [], "機器人還沒說話，不該有插話"
        bot = await env.start_bot_turn(utt_a)

        # A 短促附和一聲（0.2s + VAD hangover 0.15s < 0.4s 門檻）：不算插話
        env.feed("A", dc(3000, 0.2) + quiet(0.6))
        # 每 20ms 一個事件：第一句 1.1s（55 個）+ 這一聲 0.8s（40 個）
        await poll(lambda: sum(e.speaker_id == "A" for e in env.events) >= 55 + 40)
        voiced_a = [e for e in env.events if e.speaker_id == "A" and e.is_voiced]
        assert voiced_a
        assert env.interrupts == [] and env.runner.is_speaking and not env.sink.stopped

        # B、C 同時開口說話（交錯餵入，各 0.8s）：持續說話者觸發插話，且只觸發一次
        env.feed_interleaved(
            {"B": dc(2000, 0.8) + quiet(0.6), "C": dc(4000, 0.8) + quiet(0.6)}
        )
        r_bot = await asyncio.wait_for(bot, SAFETY_S)
        await poll(
            lambda: {u.speaker_id for u in env.session.results} >= {"A", "B", "C"}
            and sum(u.speaker_id in ("B", "C") for u in env.session.results) >= 2
        )
        got = list(env.session.results)

        assert len(env.interrupts) == 1 and env.interrupts[0] in ("B", "C")
        assert r_bot.phase is TurnPhase.CANCELLED
        assert r_bot.cancel_reason == CancelReason.BARGE_IN.value
        assert r_bot.utterance_text == "1000"
        assert r_bot.spoken_text == S0 + S1
        # 兩人的 Utterance 一個不少，speaker_id 與內容成對，沒有串音
        valid = {("A", "1000"), ("A", "3000"), ("B", "2000"), ("C", "4000")}
        assert {(u.speaker_id, u.text) for u in got} <= valid, "speaker_id 與內容必須成對"
        assert {("B", "2000"), ("C", "4000")} <= {(u.speaker_id, u.text) for u in got}
        assert len(got) == len({u.turn_id for u in got}), "每則 Utterance 的 turn_id 各自獨立"
        # 事件的 speaker_id 只會是真的有出聲的人
        assert {e.speaker_id for e in env.events if e.is_voiced} == {"A", "B", "C"}
    finally:
        await env.session.close()


async def test_插話的人下一輪被正確回應_不串音() -> None:
    """被插話中止後，插話者的話成為下一輪；每輪的 speaker_id 都對得上。"""
    env = await make_env()
    try:
        env.feed("A", dc(1000, 0.5) + quiet(0.6))
        (utt_a,) = await env.next_utterances(1)
        bot = await env.start_bot_turn(utt_a)

        env.feed_interleaved({"B": dc(2000, 0.8) + quiet(0.6), "A": quiet(1.4)})
        r_a = await asyncio.wait_for(bot, SAFETY_S)
        (utt_b,) = await env.next_utterances(1)
        assert env.interrupts == ["B"]
        assert (utt_b.speaker_id, utt_b.text) == ("B", "2000")

        # 下一輪回應 B：機器人整句講完
        sink2 = env.runner.sink = ManualSink()
        env.speak = env.runner.speak_stage = GateSpeak(open_all=True)
        task_b = asyncio.ensure_future(env.runner.run_turn(utt_b))
        await until(sink2.draining.is_set)
        sink2.finish()
        r_b = await asyncio.wait_for(task_b, SAFETY_S)

        assert (r_a.utterance_text, r_a.phase) == ("1000", TurnPhase.CANCELLED)
        assert (r_b.utterance_text, r_b.phase) == ("2000", TurnPhase.DONE)
        assert r_b.spoken_text == RESPONSE
        # ThinkStage 每次請求時看到的 scope（記憶隔離用）就是當輪的說話者
        assert env.backend.scopes == ["A", "B"]
        # 沒有任何殘留的未完成 task
        for _ in range(20):
            await asyncio.sleep(0)
    finally:
        await env.session.close()
    assert env.stage.engine.stats["channels_rejected"] == 0


async def test_機器人沒說話時別人的語音不會觸發插話() -> None:
    """is_speaking 為 False 時，持續說話也不能呼叫 interrupt（否則會誤殺下一輪）。"""
    env = await make_env()
    try:
        env.feed_interleaved(
            {"A": dc(1000, 0.8) + quiet(0.6), "B": dc(2000, 0.8) + quiet(0.6)}
        )
        got = await env.next_utterances(2)
        assert {(u.speaker_id, u.text) for u in got} == {("A", "1000"), ("B", "2000")}
        assert env.interrupts == []
        assert not env.runner.is_speaking
        # 之後的 turn 不受影響
        sink = env.runner.sink = ManualSink()
        env.runner.speak_stage = GateSpeak(open_all=True)
        task = asyncio.ensure_future(env.runner.run_turn(got[0]))
        await until(sink.draining.is_set)
        sink.finish()
        assert (await asyncio.wait_for(task, SAFETY_S)).phase is TurnPhase.DONE
    finally:
        await env.session.close()
