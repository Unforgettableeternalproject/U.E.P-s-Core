"""系統情境測試——影響使用者體驗的整條管線行為（零 GPU、零 token、零時序賭博）。

和 ``test_pipeline.py`` 的分工：那邊用真實時間 + ``sleep`` 賽跑驗「大致正確」；
這裡全部由 ``ManualSink`` / ``GateSpeak`` 手動推進，驗**精確**語意：

1. 播放中插話：取消即時生效、後續句不再送 sink、spoken 只含聽到的部分
2. 播放前插話：首句已生成但沒出聲／已進緩衝但沒播／首 token 之前
3. 連續多輪：取消後下一輪乾淨開始
4. 背壓：sink 慢時上游不無限堆積；慢 sink 不拖住插話

多聲道見 ``test_system_scenarios_multichannel.py``。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from uep.stream.adapters.think_llm import LLMThinkStage
from uep.stream.contracts.cancellation import CancelReason
from uep.stream.contracts.types import TurnPhase, TurnResult, Utterance
from uep.stream.pipeline import PipelineRunner
from uep.stream.tracer import (
    MARK_SPEAK_FIRST_CHUNK,
    MARK_SPEAK_LAST_CHUNK,
    MARK_THINK_DONE,
    LatencyTracer,
)
from uep.fakes.stream import FakeAudioSink

from .scenario_support import (
    RESPONSE,
    SENTENCES,
    GateSpeak,
    ManualSink,
    ScriptBackend,
    build_think,
    until,
)

S0, S1, S2, S3 = SENTENCES
RESPONSE_2 = "戊戊戊。己己己。"
SAFETY_S = 5.0
"""只是死鎖時的安全網，正常路徑完全不會用到這段時間。"""


@dataclass
class Env:
    runner: PipelineRunner
    think: LLMThinkStage
    speak: GateSpeak
    sink: ManualSink
    backend: ScriptBackend
    tracer: LatencyTracer
    log: list[str] = field(default_factory=list)

    def start(self, text: str = "你好") -> asyncio.Task[TurnResult]:
        return asyncio.ensure_future(self.runner.run_turn(Utterance(text=text)))

    async def result(self, task: asyncio.Task[TurnResult]) -> TurnResult:
        return await asyncio.wait_for(task, SAFETY_S)

    def history_text(self) -> str:
        return "".join(m["content"] for m in self.think.history)


def make_env(
    *,
    responses: tuple[str, ...] = (RESPONSE,),
    open_all: bool = False,
    gate: asyncio.Event | None = None,
    write_gate: asyncio.Semaphore | None = None,
) -> Env:
    log: list[str] = []
    backend = ScriptBackend(responses, gate=gate)
    think = build_think(backend, log)
    speak = GateSpeak(open_all=open_all)
    sink = ManualSink(log, write_gate=write_gate)
    tracer = LatencyTracer()
    runner = PipelineRunner(think_stage=think, speak_stage=speak, sink=sink, tracer=tracer)
    return Env(runner, think, speak, sink, backend, tracer, log)


def content_of(message: object) -> str:
    """backend 收到的 message 可能是 dict 或 SessionControl 的 Message。"""
    if isinstance(message, dict):
        return message["content"]
    return getattr(message, "content", "")


# --- 情境 1：播放中插話 ---


async def test_播放中插話_後續句不再送sink_spoken只含聽到的() -> None:
    env = make_env()
    env.speak.open(0, 1)  # 第 0、1 句合成完；第 2 句的 TTS 還沒好
    task = env.start()
    await until(lambda: len(env.sink.chunks) == 2 and env.speak.seen == [0, 1, 2])
    env.sink.play(2.0)  # 使用者剛好聽完前兩句

    assert env.runner.interrupt(CancelReason.BARGE_IN) is True
    # 同步生效：interrupt() 一返回 sink 就已停止，不必等任何 await
    assert env.sink.stopped

    result = await env.result(task)
    # 插話之後 TTS 才把第 2、3 句做好——不可以再被送進 sink
    env.speak.open(2, 3)
    for _ in range(100):
        await asyncio.sleep(0)

    assert result.phase is TurnPhase.CANCELLED
    assert result.cancel_reason == CancelReason.BARGE_IN.value
    assert result.spoken_text == S0 + S1
    assert result.spoken_duration_s == pytest.approx(2.0)
    assert S0 + S1 in result.generated_text and S3 not in result.generated_text
    assert len(env.sink.chunks) == 2
    assert env.sink.writes_after_stop == 0
    assert env.speak.produced == [0, 1]


async def test_插話後未播出的內容不進歷史() -> None:
    env = make_env()
    env.speak.open(0, 1)
    task = env.start("今天天氣如何")
    await until(lambda: len(env.sink.chunks) == 2 and env.speak.seen == [0, 1, 2])
    env.sink.play(2.0)
    env.runner.interrupt()
    result = await env.result(task)

    assert env.think.history == [
        {"role": "user", "content": "今天天氣如何"},
        {"role": "assistant", "content": S0 + S1},
    ]
    assert S2 not in env.history_text() and S3 not in env.history_text()
    assert result.spoken_text == S0 + S1


async def test_commit排在sink停止與播放量確認之後() -> None:
    """Pipecat #4111 的防線：停止 → 確認實播量 → 才 commit。"""
    env = make_env()
    env.sink.reported_after_stop = 1.0  # 前端最後回報：其實只聽到 1 秒
    env.speak.open(0, 1)
    task = env.start()
    await until(lambda: len(env.sink.chunks) == 2 and env.speak.seen == [0, 1, 2])
    env.sink.play(2.0)  # stop 當下的估計是 2 秒
    env.runner.interrupt()
    result = await env.result(task)

    order = [e for e in env.log if e in ("stop", "confirm", "commit")]
    assert order == ["stop", "confirm", "commit"]
    # 以確認後的 1 秒為準，不是 stop 當下的 2 秒
    assert result.spoken_text == S0
    assert env.think.history[-1] == {"role": "assistant", "content": S0}


async def test_重複插話冪等() -> None:
    env = make_env()
    env.speak.open(0)
    task = env.start()
    await until(lambda: len(env.sink.chunks) == 1)
    assert [env.runner.interrupt(), env.runner.interrupt()] == [True, False]
    result = await env.result(task)
    assert result.phase is TurnPhase.CANCELLED
    assert env.log.count("commit") == 1, "同一輪只能 commit 一次"


async def test_正常播完_spoken等於generated_且drain前不結算() -> None:
    env = make_env(open_all=True)
    task = env.start()
    await until(env.sink.draining.is_set)
    # 音訊都寫完了但還沒播完——這時不能結束、不能 commit
    assert not task.done()
    assert "commit" not in env.log
    env.sink.finish()
    result = await env.result(task)

    assert result.phase is TurnPhase.DONE
    assert result.spoken_text == result.generated_text == RESPONSE
    assert env.think.history[-1] == {"role": "assistant", "content": RESPONSE}


@pytest.mark.parametrize(
    ("played", "expected"),
    [(0.4, S0), (1.5, S0 + S1)],
    ids=["第一句播到一半", "第二句播到一半"],
)
async def test_播到句中算已播出(played: float, expected: str) -> None:
    env = make_env()
    env.speak.open(0, 1)
    task = env.start()
    await until(lambda: len(env.sink.chunks) == 2 and env.speak.seen == [0, 1, 2])
    env.sink.play(played)
    env.runner.interrupt()
    result = await env.result(task)
    assert result.spoken_text == expected


# --- 情境 2：播放前插話 ---


async def test_首句已生成但TTS還沒出聲就插話() -> None:
    env = make_env()  # 閘門全關：TTS 一句都還沒好
    task = env.start()
    await until(lambda: env.speak.seen == [0])
    assert env.sink.chunks == []

    assert env.runner.interrupt() is True
    result = await env.result(task)

    assert result.phase is TurnPhase.CANCELLED
    assert result.spoken_text == "" and result.spoken_duration_s == 0.0
    assert result.generated_text == S0, "已生成的首句仍要記帳，但不是『說過』"
    assert env.sink.chunks == [] and env.sink.writes_after_stop == 0
    assert env.log.count("commit") == 1, "沒出聲也要 commit，否則這輪對話憑空消失"
    # 使用者的話進歷史、機器人什麼都沒說就不寫 assistant
    assert env.think.history == [{"role": "user", "content": "你好"}]
    # 取消要往上游傳播：沒有繼續向 LLM 要更多句子
    assert env.speak.seen == [0]


async def test_音訊已進緩衝但使用者還沒聽到就插話() -> None:
    env = make_env(open_all=True)
    task = env.start()
    await until(env.sink.draining.is_set)  # 三、四句都已進 sink 緩衝
    assert env.sink.played_duration_s == 0.0

    env.runner.interrupt()
    result = await env.result(task)

    assert result.phase is TurnPhase.CANCELLED
    assert result.spoken_text == "", "緩衝裡的音訊使用者從沒聽到"
    assert result.generated_text == RESPONSE
    assert env.think.history == [{"role": "user", "content": "你好"}]


async def test_首token之前插話_首token一到就結束且不產句() -> None:
    gate = asyncio.Event()
    env = make_env(gate=gate)
    task = env.start()
    await until(lambda: len(env.backend.calls) == 1)

    assert env.runner.interrupt() is True
    gate.set()  # LLM 的第一個 token 終於回來
    result = await env.result(task)

    assert result.phase is TurnPhase.CANCELLED
    assert result.generated_text == "" and result.spoken_text == ""
    assert env.speak.seen == [] and env.sink.chunks == []
    assert env.backend.delivered <= 1, "取消後不該繼續消費 token"
    assert env.think.history == [{"role": "user", "content": "你好"}]


async def test_首token之前插話_不必等LLM回應() -> None:
    gate = asyncio.Event()  # 永遠不打開：模擬 LLM 網路卡住
    env = make_env(gate=gate)
    task = env.start()
    await until(lambda: len(env.backend.calls) == 1)
    env.runner.interrupt()
    for _ in range(200):
        await asyncio.sleep(0)
    done = task.done()
    gate.set()  # 收尾，避免遺留卡住的 task
    await env.result(task)
    assert done, "取消後 turn 應該立即結束，不等 LLM 首 token"


# --- 情境 4：連續多輪 ---


async def test_取消後下一輪乾淨開始() -> None:
    env = make_env(responses=(RESPONSE, RESPONSE_2))
    env.speak.open(0, 1)
    task1 = env.start("第一個問題")
    await until(lambda: len(env.sink.chunks) == 2 and env.speak.seen == [0, 1, 2])
    env.sink.play(2.0)
    env.runner.interrupt()
    r1 = await env.result(task1)
    assert not env.runner.is_speaking
    assert env.runner.interrupt() is False, "兩輪之間的插話不該命中任何東西"

    # 第二輪：換新的 sink（與 Web 每輪新建 _EmitSink 同樣做法），TTS 全開
    sink2 = ManualSink(env.log)
    env.runner.sink = sink2
    env.speak.open(0, 1, 2, 3)
    utt2 = Utterance(text="第二個問題")
    task2 = asyncio.ensure_future(env.runner.run_turn(utt2))
    await until(sink2.draining.is_set)
    sink2.finish()
    r2 = await env.result(task2)

    # 第二輪沒有繼承第一輪的取消狀態
    assert r2.phase is TurnPhase.DONE and r2.cancel_reason is None
    assert r2.spoken_text == r2.generated_text == RESPONSE_2
    assert [c.sentence_index for c in sink2.chunks] == [0, 1]
    assert env.sink.writes_after_stop == 0, "舊 sink 不再收到任何東西"
    assert len(env.sink.chunks) == 2

    # 歷史：只有第一輪實際聽到的，加上第二輪完整的
    assert env.think.history == [
        {"role": "user", "content": "第一個問題"},
        {"role": "assistant", "content": S0 + S1},
        {"role": "user", "content": "第二個問題"},
        {"role": "assistant", "content": RESPONSE_2},
    ]
    # 第二輪送給 LLM 的上下文含「第一輪實際說出的」，不含沒聽到的
    sent = "".join(content_of(m) for m in env.backend.calls[1])
    assert S0 + S1 in sent and "第一個問題" in sent
    assert S2 not in sent and S3 not in sent

    # tracer 不混輪
    t1, t2 = env.tracer.get(r1.turn_id), env.tracer.get(r2.turn_id)
    assert t1 is not None and t2 is not None and t1 is not t2
    assert t1.phase is TurnPhase.CANCELLED and t1.cancel_reason == CancelReason.BARGE_IN.value
    assert MARK_SPEAK_LAST_CHUNK not in t1.marks and MARK_THINK_DONE not in t1.marks
    assert t1.chunk_count == 2 and t1.spoken_duration_s == pytest.approx(2.0)
    assert t2.phase is TurnPhase.DONE and t2.cancel_reason is None
    assert MARK_SPEAK_LAST_CHUNK in t2.marks and MARK_THINK_DONE in t2.marks
    assert t2.chunk_count == 2 and t2.spoken_duration_s == pytest.approx(2.0)
    assert t2.sentence_count < t1.sentence_count + 2, "第二輪的句數不含第一輪"
    assert t2.marks[MARK_SPEAK_FIRST_CHUNK] >= utt2.ended_at
    assert env.tracer.get(r1.turn_id) is t1  # 兩個 turn 各自成帳，沒有互相覆蓋


async def test_連續被插話三次_不殘留task_歷史只留聽到的() -> None:
    env = make_env(responses=(RESPONSE,))
    results: list[TurnResult] = []
    # (插話前聽到幾秒, 預期 spoken)：開口前、第一句後、兩句後
    plan = [(None, ""), (1.0, S0), (2.0, S0 + S1)]
    for round_no, (heard, expected) in enumerate(plan):
        env.runner.sink = env.sink = ManualSink(env.log)
        env.speak = GateSpeak()
        env.runner.speak_stage = env.speak
        if heard is not None:
            env.speak.open(0, 1)
        task = env.start(f"問題{round_no}")
        if heard is None:  # 這輪 TTS 連第一句都還沒好
            await until(lambda: env.speak.seen == [0])
        else:
            await until(lambda: len(env.sink.chunks) == 2 and env.speak.seen == [0, 1, 2])
            env.sink.play(heard)
        assert env.runner.interrupt() is True
        result = await env.result(task)
        assert result.phase is TurnPhase.CANCELLED
        assert result.spoken_text == expected
        results.append(result)

    # 第四輪正常說完，證明前三次取消沒有留下狀態
    env.runner.sink = sink4 = ManualSink(env.log)
    env.speak = GateSpeak(open_all=True)
    env.runner.speak_stage = env.speak
    task4 = env.start("問題3")
    await until(sink4.draining.is_set)
    sink4.finish()
    r4 = await env.result(task4)
    assert r4.phase is TurnPhase.DONE and r4.spoken_text == RESPONSE

    assert env.think.history == [
        {"role": "user", "content": "問題0"},
        {"role": "user", "content": "問題1"},
        {"role": "assistant", "content": S0},
        {"role": "user", "content": "問題2"},
        {"role": "assistant", "content": S0 + S1},
        {"role": "user", "content": "問題3"},
        {"role": "assistant", "content": RESPONSE},
    ]
    assert len({r.turn_id for r in [*results, r4]}) == 4
    for _ in range(20):
        await asyncio.sleep(0)
    leftover = [
        t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()
    ]
    assert leftover == [], f"取消後殘留未結束的 task：{leftover}"


@pytest.mark.xfail(
    strict=True,
    reason=(
        "AudioSink 契約沒有『新一輪開始』的重置點，stop() 是永久的："
        "同一個 FakeAudioSink（cli demo --barge-in 就是這樣用）被插話後，"
        "下一輪的音訊全被丟掉，且 played_duration_s 凍結在上一輪的值，"
        "spoken_text 被截短。Web 路徑每輪新建 _EmitSink 所以沒中，"
        "Phase 5 的長壽命 Discord sink 會中。"
    ),
)
async def test_同一個sink跨輪使用_插話後下一輪仍可播() -> None:
    env = make_env(responses=(RESPONSE, RESPONSE_2))
    fake = FakeAudioSink(realtime=False)
    env.runner.sink = fake
    env.speak.open(0)
    task1 = env.start("第一個問題")
    await until(lambda: len(fake.chunks) == 1 and env.speak.seen == [0, 1])
    env.runner.interrupt()
    await env.result(task1)

    env.speak.open(0, 1, 2, 3)
    r2 = await env.result(env.start("第二個問題"))
    assert len(fake.chunks) == 3, "第二輪的兩段音訊應該寫進 sink"
    assert r2.spoken_text == RESPONSE_2


# --- 情境 5：背壓 ---


async def test_sink慢時上游不無限堆積() -> None:
    sentences = 40
    text = "哈哈哈。" * sentences
    gate = asyncio.Semaphore(0)  # sink 每次 write 都要名額：一開始一個都沒有
    env = make_env(responses=(text,), open_all=True, write_gate=gate)
    task = env.start()

    await until(lambda: len(env.speak.produced) >= 1)
    for _ in range(300):  # 給「無界緩衝」充分的機會暴衝
        await asyncio.sleep(0)
    assert len(env.sink.chunks) == 0
    assert len(env.speak.seen) <= 2, f"sink 卡住時 TTS 已經收了 {len(env.speak.seen)} 句"
    assert env.backend.delivered < len(text) // 2, "LLM 在 sink 卡住時還一路吐到底"

    # 一次放一個名額：上游領先 sink 的量始終有界
    for released in range(1, 11):
        gate.release()
        await until(lambda n=released: len(env.sink.chunks) == n)
        for _ in range(50):
            await asyncio.sleep(0)
        lead = len(env.speak.produced) - len(env.sink.chunks)
        assert lead <= 1, f"寫了 {released} 段時上游領先 {lead} 段"
        assert env.backend.delivered < len(text), "還沒放行完就吐完了"

    for _ in range(sentences):
        gate.release()
    await until(env.sink.draining.is_set)
    env.sink.finish()
    result = await env.result(task)
    assert result.phase is TurnPhase.DONE
    assert [c.sentence_index for c in env.sink.chunks] == list(range(sentences))
    assert result.spoken_text == text


async def test_sink卡在write時插話仍能結束() -> None:
    gate = asyncio.Semaphore(0)
    env = make_env(open_all=True, write_gate=gate)
    task = env.start()
    await until(lambda: len(env.speak.produced) >= 1)  # 第一段正卡在 sink.write 裡

    assert env.runner.interrupt() is True
    result = await env.result(task)

    assert result.phase is TurnPhase.CANCELLED
    assert result.spoken_text == ""
    assert env.sink.writes_after_stop == 1 and env.sink.chunks == []
    assert env.speak.seen == [0], "卡住期間取消後不該再往上游拉句子"
