"""系統情境測試的共用零件——全部由測試手動推進，不靠真實時間。

設計原則：測試裡**沒有任何用來等時序的 sleep**。狀態推進一律走三種手段：

* ``ManualSink.play(秒數)``——手動決定「使用者聽到了多少」
* ``GateSpeak.open(句序號)``——手動決定「TTS 哪一句合成完」
* ``until(條件)``——只讓出事件圈（``sleep(0)``）直到條件成立，不依賴牆鐘
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator, Callable, Sequence
from typing import Any

from uep.stream.adapters.think_llm import LLMThinkStage
from uep.stream.contracts.cancellation import CancellationToken
from uep.stream.contracts.types import TTS_SAMPLE_RATE, AudioChunk, Sentence
from uep.stream.splitter import SplitPolicy

# 每句都以句號結尾、重量夠，所以切出來剛好一句一段，內容可預測
SENTENCES = ["甲甲甲。", "乙乙乙。", "丙丙丙。", "丁丁丁。"]
RESPONSE = "".join(SENTENCES)
CHUNK_S = 1.0
"""GateSpeak 每句產出的音訊時長。"""

SPLIT = SplitPolicy(first_min_weight=1.0, min_weight=1.0)


async def until(predicate: Callable[[], bool], *, ticks: int = 5000, timeout_s: float = 5.0):
    """讓出事件圈直到條件成立。

    只做 ``sleep(0)``（不耗牆鐘時間）；``wait_for`` 僅是死鎖時的安全網，
    正常路徑不會碰到。
    """

    async def _spin() -> None:
        for _ in range(ticks):
            if predicate():
                return
            await asyncio.sleep(0)
        raise AssertionError("條件在事件圈讓出上限內沒有成立")

    await asyncio.wait_for(_spin(), timeout_s)


async def poll(predicate: Callable[[], bool], timeout_s: float = 5.0) -> None:
    """等條件成立（給需要跨執行緒的情境用，例如 STT backend 在 executor 裡跑）。

    只是條件輪詢，不是時序賭注：條件成立就立刻返回，逾時才失敗。
    """

    async def _spin() -> None:
        while not predicate():
            await asyncio.sleep(0.002)

    try:
        await asyncio.wait_for(_spin(), timeout_s)
    except asyncio.TimeoutError:
        raise AssertionError("等待條件逾時") from None


class ManualSink:
    """播放進度完全由測試手動控制的 sink。

    * ``play(s)``：使用者又聽到了 s 秒（不超過已寫入量，停止後不再前進）
    * ``finish()``：全部播完，``drain()`` 返回
    * ``log``：與其他元件共用的事件序列，用來驗順序（stop 先於 commit 等）
    """

    def __init__(
        self, log: list[str] | None = None, *, write_gate: asyncio.Semaphore | None = None
    ):
        self.log = log if log is not None else []
        self.chunks: list[AudioChunk] = []
        self.written_duration_s = 0.0
        self.stopped = False
        self.stop_calls = 0
        self.writes_after_stop = 0
        self.played_at_stop: float | None = None
        self.draining = asyncio.Event()
        self._stop_evt = asyncio.Event()
        self._played = 0.0
        self._finished = asyncio.Event()
        self._write_gate = write_gate
        """不為 None 時，每次 write 要先取得一個名額——模擬消化很慢的 sink。"""

        self.reported_after_stop: float | None = None
        """設定後，``confirm_stopped()`` 會把最終播放量改成這個值
        （模擬瀏覽器端回報的實播位置，與 stop 當下的推算不同）。"""

    async def write(self, chunk: AudioChunk) -> None:
        if self._write_gate is not None:
            # 守規矩的 sink：stop() 必須能解除卡住的 write（否則 barge-in 會被慢 sink 拖住）
            acquire = asyncio.ensure_future(self._write_gate.acquire())
            stopped = asyncio.ensure_future(self._stop_evt.wait())
            try:
                await asyncio.wait({acquire, stopped}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in (acquire, stopped):
                    task.cancel()
        if self.stopped:
            self.writes_after_stop += 1
            return
        self.chunks.append(chunk)
        self.written_duration_s += chunk.duration_s
        self.log.append(f"write:{chunk.sentence_index}")

    def stop(self) -> None:
        self.stop_calls += 1
        if self.stopped:
            return
        self.stopped = True
        self._stop_evt.set()
        self.played_at_stop = self._played
        self.log.append("stop")

    async def confirm_stopped(self) -> None:
        self.log.append("confirm")
        if self.reported_after_stop is not None:
            self._played = self.reported_after_stop

    async def drain(self) -> None:
        self.draining.set()
        await self._finished.wait()

    @property
    def played_duration_s(self) -> float:
        return self._played

    def play(self, seconds: float) -> None:
        if self.stopped:
            return
        self._played = min(self.written_duration_s, self._played + seconds)

    def finish(self) -> None:
        self._played = self.written_duration_s
        self._finished.set()


class GateSpeak:
    """每句等閘門打開才「合成完」的 SpeakStage。

    閘門沒開就卡住，但取消時會立刻中止（與真 TTS 的協作點一致）。
    ``seen`` 記錄收到哪些句子——驗證上游被拉了多少、取消後有沒有繼續被拉。
    """

    name = "gate_speak"

    def __init__(self, *, open_all: bool = False, seconds: float = CHUNK_S) -> None:
        self.seconds = seconds
        self.open_all = open_all
        self.gates: dict[int, asyncio.Event] = defaultdict(asyncio.Event)
        self.seen: list[int] = []
        self.produced: list[int] = []

    def open(self, *indexes: int) -> None:
        for i in indexes:
            self.gates[i].set()

    async def prepare(self) -> None:
        return

    async def aclose(self) -> None:
        return

    async def stream(
        self, sentences: AsyncIterator[Sentence], token: CancellationToken
    ) -> AsyncIterator[AudioChunk]:
        async for sentence in sentences:
            token.raise_if_cancelled()
            if not sentence.text:
                continue
            self.seen.append(sentence.index)
            if not self.open_all:
                gate_task = asyncio.ensure_future(self.gates[sentence.index].wait())
                cancel_task = asyncio.ensure_future(token.wait())
                try:
                    await asyncio.wait(
                        {gate_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED
                    )
                finally:
                    for task in (gate_task, cancel_task):
                        task.cancel()
            token.raise_if_cancelled()
            self.produced.append(sentence.index)
            yield AudioChunk(
                pcm=bytes(int(TTS_SAMPLE_RATE * self.seconds) * 2),
                sample_rate=TTS_SAMPLE_RATE,
                turn_id=sentence.turn_id,
                sentence_index=sentence.index,
                is_last=sentence.is_last,
            )


class ScriptBackend:
    """假 LLM backend：吐固定文字，可選擇在首 token 前卡在閘門上。

    記錄每次請求收到的 messages，以及當下 ThinkStage 的 ``active_scope``
    （多聲道不串音的證據）。``max_delivered`` 讓背壓測試看得到上游吐了多少。
    """

    def __init__(
        self, responses: Sequence[str] = (RESPONSE,), *, gate: asyncio.Event | None = None
    ):
        self.responses = list(responses)
        self.gate = gate
        self.calls: list[list[Any]] = []
        self.scopes: list[str | None] = []
        self.delivered = 0
        self.stage: LLMThinkStage | None = None

    async def stream_query(
        self, messages: Sequence[Any], system_instruction: str | None = None, **kwargs: Any
    ) -> AsyncIterator[str]:
        self.calls.append(list(messages))
        self.scopes.append(self.stage.active_scope if self.stage is not None else None)
        text = self.responses[(len(self.calls) - 1) % len(self.responses)]
        if self.gate is not None:
            await self.gate.wait()
        for ch in text:
            self.delivered += 1
            yield ch


def build_think(backend: ScriptBackend, log: list[str] | None = None) -> LLMThinkStage:
    """真的 LLMThinkStage（歷史／commit 語意用真的），commit 前後記進 log。"""
    think = LLMThinkStage(backend, split_policy=SPLIT)
    backend.stage = think
    if log is not None:
        original = think.commit

        async def spy(turn_id: str, spoken_text: str, generated_text: str) -> None:
            log.append("commit")
            await original(turn_id, spoken_text, generated_text)

        think.commit = spy  # type: ignore[method-assign]
    return think
