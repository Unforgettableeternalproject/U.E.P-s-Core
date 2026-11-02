"""三個 Fake Stage——Phase 0 的骨架驗證用。

## 為什麼要先做 fake

「先確保整個對應沒有問題」是 Phase 0 的全部意義。用 fake 跑通後，
契約對不對、背壓會不會爆、取消能不能正確中斷、打點準不準——這四件事
就都驗過了。之後逐一換真模組，出問題永遠知道是哪一層。

**全程不需要 GPU，秒級迭代。** 這是把 GPU 模組留到 Phase 1+ 的理由：
在 4 秒才出一段音訊的環境裡除錯背壓，一個下午跑不了幾輪。

## 延遲參數的預設值取自實測

不是隨便填的數字，是 2026-06-22 那次 Discord 實測與 TTS 段級串流實測的值，
所以 fake 跑出來的 TTFA 應該接近真實管線接上後的量級。骨架驗證階段就能看到
「大概會有多慢」，而不是等接上真模組才發現預算不夠分。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Sequence

from uep.stream.contracts.cancellation import CancellationToken
from uep.stream.contracts.stages import AudioSource
from uep.stream.contracts.types import (
    TTS_SAMPLE_RATE,
    AudioChunk,
    Sentence,
    Utterance,
    new_turn_id,
)
from uep.stream.splitter import SentenceSplitter, SplitPolicy, text_weight
from uep.stream.tracer import (
    MARK_MEMORY_DONE,
    MARK_MEMORY_START,
    MARK_THINK_FIRST_TOKEN,
    LatencyTracer,
)

SECONDS_PER_WEIGHT = 0.25
"""一單位發音權重（≈一個中文字）約 0.25 秒。用於估算 fake 音訊長度。"""


class FakeInputStage:
    """從預設文字列表產出 turn，模擬 STT 轉錄延遲。

    不做任何音訊處理——Phase 3 換真 STT 時，這一層會變成
    ``MultiChannelSTTEngine`` 的 adapter，但契約不變。
    """

    name = "fake_input"

    def __init__(
        self,
        texts: Sequence[str] | None = None,
        stt_delay_s: float = 1.2,
        think_gap_s: float = 0.0,
    ) -> None:
        self.texts = list(texts or ["你好，今天過得怎麼樣？"])
        self.stt_delay_s = stt_delay_s
        """使用者說完 → 最終轉錄就緒的延遲。現況實測 1-2s，預算 400ms。"""

        self.think_gap_s = think_gap_s
        """兩個 turn 之間的間隔。"""

    async def prepare(self) -> None:
        return

    async def aclose(self) -> None:
        return

    async def stream(
        self, source: AudioSource, token: CancellationToken
    ) -> AsyncIterator[Utterance]:
        for text in self.texts:
            if token.is_cancelled:
                return
            if self.think_gap_s > 0:
                await asyncio.sleep(self.think_gap_s)

            # 使用者「說完」的時刻——TTFA 從這裡起算，
            # 所以 STT 的轉錄耗時必須算在使用者的等待裡
            speech_end = time.perf_counter()
            await asyncio.sleep(self.stt_delay_s)

            yield Utterance(
                text=text,
                turn_id=new_turn_id(),
                started_at=speech_end,
                ended_at=speech_end,
                is_final=True,
            )


class FakeThinkStage:
    """固定回應 + 模擬 token 串流。

    也負責跑 :class:`SentenceSplitter`——真的 ThinkStage 同樣如此，
    因為切句需要看到 token 流，是 ThinkStage 的內部細節而非獨立階段。

    ``memory_delay_s`` 模擬 §7.4 定案的**平行** retrieve：它跟 token 生成
    同時跑，**不擋首句**。所以 trace 裡 ``memory_done`` 晚於
    ``think_first_token`` 是正常的。
    """

    name = "fake_think"

    def __init__(
        self,
        responses: Sequence[str] | None = None,
        first_token_delay_s: float = 0.4,
        token_interval_s: float = 0.012,
        memory_delay_s: float = 1.5,
        split_policy: SplitPolicy | None = None,
        tracer: LatencyTracer | None = None,
    ) -> None:
        self.responses = list(
            responses
            or [
                "嗯，我想想。今天大致上還算順利，"
                "把幾個卡了很久的問題處理掉了，感覺輕鬆不少。"
                "不過還有一些收尾的工作要做，晚點再繼續吧。"
            ]
        )
        self.first_token_delay_s = first_token_delay_s
        """LLM 首 token 延遲。串流呼叫下約 200-400ms。"""

        self.token_interval_s = token_interval_s
        """token 之間的間隔。約當每秒 80 token。"""

        self.memory_delay_s = memory_delay_s
        """Memory retrieve 耗時。現況 1-3s，與 LLM 平行跑所以不擋首句。"""

        self.split_policy = split_policy or SplitPolicy()
        self.tracer = tracer
        self._turn_count = 0
        self.stream_calls = 0
        """``stream()`` 被呼叫（開始迭代）的次數，單輸入單輸出（V09）斷言用。"""
        self.committed: list[tuple[str, str, str]] = []
        """commit 記錄 ``(turn_id, spoken_text, generated_text)``，供測試檢查
        barge-in 善後是否寫對了東西。"""

        self.history: list[dict[str, str]] = []
        """對話歷史，語意與 :class:`LLMThinkStage` 一致——fake 也要維護它，
        Web 前端的會話切換才能在無 GPU 環境下驗證。"""

        self._pending_user_text = ""
        self._pending_turn_id = ""
        self._assistant_by_turn: dict[str, dict[str, str]] = {}
        self._user_by_turn: dict[str, dict[str, str]] = {}
        self._retracted_turns: set[str] = set()

    async def prepare(self) -> None:
        return

    async def aclose(self) -> None:
        return

    async def stream(
        self, utterance: Utterance, token: CancellationToken
    ) -> AsyncIterator[Sentence]:
        response = self.responses[self._turn_count % len(self.responses)]
        self._turn_count += 1
        self.stream_calls += 1
        self._pending_user_text = utterance.text
        self._pending_turn_id = utterance.turn_id or ""

        # Memory retrieve 與 LLM 平行起跑（§7.4）
        memory_task = asyncio.ensure_future(self._retrieve(utterance.turn_id))
        try:
            splitter = SentenceSplitter(self.split_policy)
            async for sentence in splitter.split(
                self._generate_tokens(response, utterance.turn_id),
                utterance.turn_id,
                token,
            ):
                yield sentence
        finally:
            if not memory_task.done():
                memory_task.cancel()
            # retrieve 的例外不該冒到管線外——它是 best-effort 的旁路
            with_suppress = (asyncio.CancelledError, Exception)
            try:
                await memory_task
            except with_suppress:
                pass

    async def _retrieve(self, turn_id: str) -> None:
        if self.tracer is not None:
            self.tracer.mark(turn_id, MARK_MEMORY_START)
        await asyncio.sleep(self.memory_delay_s)
        if self.tracer is not None:
            self.tracer.mark(turn_id, MARK_MEMORY_DONE)

    async def _generate_tokens(self, text: str, turn_id: str) -> AsyncIterator[str]:
        await asyncio.sleep(self.first_token_delay_s)
        if self.tracer is not None:
            self.tracer.mark(turn_id, MARK_THINK_FIRST_TOKEN)
        for ch in text:
            yield ch
            if self.token_interval_s > 0:
                await asyncio.sleep(self.token_interval_s)

    async def commit(self, turn_id: str, spoken_text: str, generated_text: str) -> None:
        """記錄實際播出的內容。

        真的 ThinkStage 在這裡寫回 SessionControl 的對話歷史與 EchoMemory。
        **寫的是 spoken_text**——被 barge-in 中止時它短於 generated_text，
        寫錯會讓 LLM 以為自己講完了整段。
        """
        self.committed.append((turn_id, spoken_text, generated_text))
        if turn_id and turn_id in self._retracted_turns:
            if self._pending_turn_id == turn_id:
                self._pending_user_text = ""
            return
        # 與 LLMThinkStage 相同的歷史語意：兩邊要嘛都進、要嘛都不進
        if self._pending_user_text:
            user_item = {"role": "user", "content": self._pending_user_text}
            self.history.append(user_item)
            if turn_id:
                self._user_by_turn[turn_id] = user_item
        if spoken_text:
            item = {"role": "assistant", "content": spoken_text}
            self.history.append(item)
            self._assistant_by_turn[turn_id] = item
        self._pending_user_text = ""

    async def settle(self, turn_id: str, spoken_text: str, generated_text: str = "") -> bool:
        """commit 之後任意時間，依 turn_id 改寫該輪的 assistant 歷史項。

        ``spoken_text`` 非空 = 換成這段文字（例如私訊占位、之後再回填可透露的版本）；
        空字串 = 移除該 assistant 項。找不到（沒 commit 過、已被修剪出歷史）回 False，不報錯。
        """
        item = self._assistant_by_turn.get(turn_id)
        if item is None or not any(h is item for h in self.history):
            return False
        if spoken_text:
            item["content"] = spoken_text
        else:
            self.history[:] = [h for h in self.history if h is not item]
            self._assistant_by_turn.pop(turn_id, None)
        return True

    async def retract(self, turn_id: str) -> bool:
        """整輪撤出歷史（user＋assistant）；尚未 commit 的輪登記為已撤回，之後的 commit 丟棄。"""
        if not turn_id:
            return False
        self._retracted_turns.add(turn_id)
        if self._pending_turn_id == turn_id:
            self._pending_user_text = ""
        items = [
            i for i in (self._user_by_turn.pop(turn_id, None),
                        self._assistant_by_turn.pop(turn_id, None))
            if i is not None
        ]
        if items:
            self.history[:] = [h for h in self.history if not any(h is i for i in items)]
        return True


class FakeSpeakStage:
    """產生正弦波，模擬 TTS 合成耗時。

    刻意模擬真 TTS 的兩個特性：

    * **首段比較慢**（模型暖機、prefill），這是 TTFA 的主要成分
    * **合成耗時與文字長度成正比**（RTF），所以長句會拖垮後續

    第二點是 :class:`SentenceSplitter` 要設長度上限的原因——
    一個 80 字的長句在 RTF 0.3 下要合成 6 秒，後面全部延遲。
    """

    name = "fake_speak"

    def __init__(
        self,
        first_chunk_extra_s: float = 0.6,
        rtf: float = 0.30,
        sample_rate: int = TTS_SAMPLE_RATE,
        base_freq: float = 196.0,
    ) -> None:
        self.first_chunk_extra_s = first_chunk_extra_s
        """首段的額外開銷（暖機 / prefill）。"""

        self.rtf = rtf
        """Real-Time Factor：合成 1 秒音訊需要多少秒。IndexTTS2 實測約 0.2-0.4。"""

        self.sample_rate = sample_rate
        self.base_freq = base_freq
        self.speak_calls = 0
        """``stream()`` 被呼叫（開始迭代）的次數，單輸入單輸出（V09）斷言用。"""

    async def prepare(self) -> None:
        return

    async def aclose(self) -> None:
        return

    async def stream(
        self, sentences: AsyncIterator[Sentence], token: CancellationToken
    ) -> AsyncIterator[AudioChunk]:
        from .audio import sine_pcm

        self.speak_calls += 1
        first = True
        async for sentence in sentences:
            token.raise_if_cancelled()
            if not sentence.text:
                continue

            audio_s = max(0.2, text_weight(sentence.text) * SECONDS_PER_WEIGHT)
            synth_s = audio_s * self.rtf + (self.first_chunk_extra_s if first else 0.0)

            # 合成期間也要能被取消——真 TTS 在 executor 執行緒裡跑同步推理，
            # 靠的是 callback 的協作點；這裡用 sleep 與取消的 race 模擬同樣行為
            await self._interruptible_sleep(synth_s, token)
            token.raise_if_cancelled()

            # 每句音高略微變化，聽感上能分辨句子邊界（調參時有用）
            freq = self.base_freq * (1.0 + 0.05 * (sentence.index % 4))
            yield AudioChunk(
                pcm=sine_pcm(audio_s, self.sample_rate, freq=freq),
                sample_rate=self.sample_rate,
                turn_id=sentence.turn_id,
                sentence_index=sentence.index,
                is_last=sentence.is_last,
            )
            first = False

    @staticmethod
    async def _interruptible_sleep(seconds: float, token: CancellationToken) -> None:
        if seconds <= 0:
            return
        sleep_task = asyncio.ensure_future(asyncio.sleep(seconds))
        cancel_task = asyncio.ensure_future(token.wait())
        try:
            done, _ = await asyncio.wait(
                {sleep_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            for task in (sleep_task, cancel_task):
                if not task.done():
                    task.cancel()
        if sleep_task not in done:
            token.raise_if_cancelled()
