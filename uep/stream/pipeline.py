"""PipelineRunner——把三層串起來。

## 職責邊界

這個檔案只做四件事：串接、打點、取消傳播、善後。**不做任何推理**。
一旦這裡出現「怎麼生成回應」「怎麼判斷語意」的邏輯，這個專案就從整合層
退化成第六個孤島。

## barge-in 善後的正確順序（重要）

Pipecat 有個已知 bug（issue #4111）：TTS 播放與 context aggregator 之間的
frame 排序問題，導致對話歷史在 TTS 完整輸出前就被提交。後果是 LLM 以為
自己講了使用者從沒聽到的內容，下一輪的指代全錯。

所以這裡的順序是死的：

1. 音訊送進 sink
2. **確認實際播出了多少**（``sink.played_duration_s``）
3. 據此推算 ``spoken_text``
4. 才呼叫 ``think.commit()``

commit 絕對不能提前於步驟 2。這也是為什麼
:meth:`~uep.stream.contracts.stages.ThinkStage.commit` 的參數是
``spoken_text`` 和 ``generated_text`` 兩個而不是一個——兩者在被插話時不同，
而寫進對話歷史的必須是前者。
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator, Callable

from .contracts.cancellation import CancellationToken, CancelledError, CancelReason
from .contracts.stages import AudioSink, AudioSource, SpeakStage, ThinkStage
from .contracts.turn import TurnPolicy
from .contracts.types import AudioChunk, Sentence, TurnPhase, TurnResult, Utterance
from . import tracer as marks
from .gap_filler import GAP_SENTENCE_INDEX, GapFiller
from .tracer import LatencyTracer


class PipelineRunner:
    """串接 InputStage → ThinkStage → SpeakStage 的執行器。

    用法::

        runner = PipelineRunner(input_stage, think_stage, speak_stage, sink=sink)
        await runner.prepare()
        async for result in runner.run_session(source):
            print(runner.tracer.report(result.turn_id))
    """

    def __init__(
        self,
        input_stage=None,
        think_stage: ThinkStage | None = None,
        speak_stage: SpeakStage | None = None,
        sink: AudioSink | None = None,
        tracer: LatencyTracer | None = None,
        policy: TurnPolicy | None = None,
        gap_filler: GapFiller | None = None,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self.input_stage = input_stage
        self.think_stage = think_stage
        self.speak_stage = speak_stage
        self.sink = sink
        self.tracer = tracer or LatencyTracer()
        self.policy = policy or TurnPolicy()
        self.gap_filler = gap_filler
        """墊片音（D 段）。None = 不用。它只碰 sink，不碰三層。"""

        self._current_token: CancellationToken | None = None
        self._current_turn_id: str | None = None
        self._clock = clock
        """可注入的時鐘，測試用；backoff 的時間差都由它計算。"""
        self._last_interrupt_at: float | None = None

    # --- 生命週期 ---

    async def prepare(self) -> None:
        for stage in (self.input_stage, self.think_stage, self.speak_stage):
            if stage is not None:
                await stage.prepare()

    async def aclose(self) -> None:
        for stage in (self.input_stage, self.think_stage, self.speak_stage):
            if stage is not None:
                with contextlib.suppress(Exception):
                    await stage.aclose()

    # --- 插話 ---

    @property
    def is_speaking(self) -> bool:
        return self._current_token is not None and not self._current_token.is_cancelled

    def interrupt(self, reason: CancelReason = CancelReason.BARGE_IN) -> bool:
        """中止當前 turn。回傳是否真的中止了什麼。

        由前端在 :class:`~uep.stream.contracts.turn.InterruptionDetector`
        判定成立時呼叫。sink 的 ``stop()`` 是**同步立即**執行的——
        等 event loop 排程會讓機器人多講半秒，體感上就是「叫不停」。
        """
        token = self._current_token
        if token is None or token.is_cancelled:
            return False
        if self.sink is not None:
            self.sink.stop()
        token.cancel(reason)
        # 只有「真的被使用者插話中止」才起算 backoff；SHUTDOWN／SUPERSEDED
        # 等原因不是使用者在搶話，不該讓下一輪被延後
        if reason is CancelReason.BARGE_IN:
            self._last_interrupt_at = self._clock()
        return True

    def _backoff_remaining_s(self) -> float:
        """距離 backoff 結束還剩幾秒；0 表示可以開口。"""
        backoff = self.policy.interruption_backoff_s
        if backoff <= 0 or self._last_interrupt_at is None:
            return 0.0
        return max(0.0, backoff - (self._clock() - self._last_interrupt_at))

    async def _wait_backoff(self, token: CancellationToken) -> None:
        """被插話後的冷卻：等到 backoff 結束才開始這一輪（可被取消中斷）。

        語意取自 Vapi ``backoffSeconds``：被插話後機器人隔 N 秒才再開口。
        只擋「開口」（墊片音啟動、第一個送進 sink 的音訊），LLM／TTS 照常
        開跑，首音時間 = max(原本, 冷卻剩餘)，不犧牲 TTFA。
        """
        remaining = self._backoff_remaining_s()
        if remaining <= 0:
            return
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(token.wait(), timeout=remaining)
        token.raise_if_cancelled()

    # --- 執行 ---

    async def run_session(self, source: AudioSource) -> AsyncIterator[TurnResult]:
        """跑一整個 session：持續從 InputStage 取 turn 並處理。"""
        if self.input_stage is None:
            raise RuntimeError("未設定 InputStage")
        session_token = CancellationToken()
        async for utterance in self.input_stage.stream(source, session_token):
            yield await self.run_turn(utterance)

    async def run_turn(self, utterance: Utterance) -> TurnResult:
        """處理單一 turn，回傳結果摘要。

        不對外拋例外——取消與錯誤都收斂成 :class:`TurnResult` 的欄位。
        一個 turn 掛掉不該讓整個 session 停擺。
        """
        if self.think_stage is None or self.speak_stage is None:
            raise RuntimeError("未設定 ThinkStage / SpeakStage")

        turn_id = utterance.turn_id
        token = CancellationToken()
        self._current_token = token
        self._current_turn_id = turn_id

        trace = self.tracer.start(turn_id)
        # t0 用使用者說完的時刻——STT 的轉錄耗時是使用者的等待，要算進 TTFA
        self.tracer.mark(turn_id, marks.MARK_SPEECH_END, at=utterance.ended_at)
        self.tracer.mark(turn_id, marks.MARK_INPUT_FINAL)
        trace.phase = TurnPhase.THINKING

        # sink 的停止要掛在取消 callback 上，不能等協作點——
        # 協作點最快也要等下一個 chunk 產出，那時已經多播了一段
        if self.sink is not None:
            token.on_cancel(lambda _reason: self.sink.stop())

        sentences: dict[int, Sentence] = {}
        # (sentence_index, 累積至該 chunk 結尾的音訊時長) — 用來把播放時間對回文字
        timeline: list[tuple[int, float]] = []
        emitted_duration = 0.0
        phase = TurnPhase.DONE
        cancel_reason: str | None = None
        error: str | None = None

        gap = self.gap_filler

        def start_gap() -> None:
            # 墊片音：STT 完成就開始計時，真音訊來之前排進 sink（排隊，不打斷）
            if gap is not None:
                gap.start(
                    self.sink, turn_id, token,
                    on_first_audio=lambda: self.tracer.mark(
                        turn_id, marks.MARK_GAP_FIRST_AUDIO
                    ),
                    language=utterance.language,
                )

        async def open_gate() -> None:
            await self._wait_backoff(token)
            start_gap()

        # 被插話後的冷卻只擋「開口」：墊片音與第一個音訊 chunk 的 sink 寫入。
        # 冷卻中 LLM／TTS 照常跑（產出的 chunk 由通道背壓暫存）
        gate: asyncio.Future | None = None
        if self._backoff_remaining_s() > 0:
            gate = asyncio.ensure_future(open_gate())
        else:
            start_gap()
        try:
            sentence_stream = self._trace_sentences(
                self.think_stage.stream(utterance, token), turn_id, sentences
            )
            chunk_stream = self.speak_stage.stream(sentence_stream, token)

            async for chunk in chunk_stream:
                token.raise_if_cancelled()
                if gate is not None:
                    # 冷卻未結束就不碰 sink；等待中被取消會在此拋 CancelledError
                    await gate
                    gate = None
                if gap is not None:
                    # 寫第一段真音訊之前先收掉墊片——它正在寫的那段要寫完，
                    # 之後不能再追加，否則 sink 裡會倒序
                    await gap.stop()
                self.tracer.mark(turn_id, marks.MARK_SPEAK_FIRST_CHUNK)
                trace.phase = TurnPhase.SPEAKING

                if self.sink is not None:
                    await self.sink.write(chunk)

                emitted_duration += chunk.duration_s
                timeline.append((chunk.sentence_index, emitted_duration))
                self.tracer.count_chunk(turn_id, chunk.duration_s)

            self.tracer.mark(turn_id, marks.MARK_SPEAK_LAST_CHUNK)

            # 音訊產完不等於播完。TTS 的 RTF < 1，管線邏輯跑完時緩衝裡還有
            # 好幾秒沒播——不等它就去算 spoken_text，會嚴重低估使用者聽到的量。
            # drain 期間仍可被插話（使用者常在最後一句還沒播完時就接話）。
            if self.sink is not None:
                await self._drain_or_cancel(token)

        except CancelledError as exc:
            phase = TurnPhase.CANCELLED
            cancel_reason = exc.reason.value
        except Exception as exc:  # noqa: BLE001 - 單一 turn 失敗不該拖垮 session
            phase = TurnPhase.FAILED
            error = f"{type(exc).__name__}: {exc}"
            token.cancel(CancelReason.UPSTREAM_ERROR, error)
        finally:
            if gate is not None:
                gate.cancel()
                await asyncio.gather(gate, return_exceptions=True)
            if gap is not None:
                await gap.stop()
            self._current_token = None
            self._current_turn_id = None

        # 被停掉的 sink 可能還有「最後實播位置」在回報路上（例如瀏覽器端）：
        # 先等它確認，再推算 spoken_text——commit 不能早於這一步
        if phase is not TurnPhase.DONE:
            await self._confirm_sink_stopped()

        generated_text = "".join(
            sentences[i].text for i in sorted(sentences) if sentences[i].text
        )
        spoken_text, spoken_duration = self._resolve_spoken(
            sentences, timeline, emitted_duration,
            gap_duration=gap.emitted_duration_s if gap is not None else 0.0,
        )

        # commit 嚴格排在確認實際播出量之後——見模組 docstring 的 Pipecat #4111
        with contextlib.suppress(Exception):
            await self.think_stage.commit(turn_id, spoken_text, generated_text)

        trace.spoken_duration_s = spoken_duration
        self.tracer.finish(turn_id, phase, cancel_reason, error)

        return TurnResult(
            turn_id=turn_id,
            phase=phase,
            utterance_text=utterance.text,
            generated_text=generated_text,
            spoken_text=spoken_text,
            spoken_duration_s=spoken_duration,
            cancel_reason=cancel_reason,
            error=error,
        )

    # --- 內部 ---

    async def _confirm_sink_stopped(self) -> None:
        """停止後等 sink 確認最終播放量。

        選用 hook：sink 有 ``confirm_stopped()`` 才呼叫，時限由 sink 自己負責
        （逾時就沿用最後已知的 ``played_duration_s``）。沒有此方法的 sink 視為
        停止當下就已確定。
        """
        confirm = getattr(self.sink, "confirm_stopped", None)
        if confirm is None:
            return
        with contextlib.suppress(Exception):
            await confirm()

    async def _drain_or_cancel(self, token: CancellationToken) -> None:
        """等 sink 播完，但保持可被插話中斷。"""
        drain_task = asyncio.ensure_future(self.sink.drain())
        cancel_task = asyncio.ensure_future(token.wait())
        try:
            await asyncio.wait(
                {drain_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED
            )
            # 播放完成與插話可能同時到達，取消優先。播放失敗也必須交給
            # run_turn 記為 FAILED，不能因為生成完成就誤報 DONE。
            token.raise_if_cancelled()
            drain_task.result()
        finally:
            for task in (drain_task, cancel_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(drain_task, cancel_task, return_exceptions=True)

    async def _trace_sentences(
        self,
        source: AsyncIterator[Sentence],
        turn_id: str,
        sink: dict[int, Sentence],
    ) -> AsyncIterator[Sentence]:
        """在句子流上打點並記帳，不改變內容。

        記帳是 barge-in 善後的前提：要知道每一句的文字，才能在中止時
        推算出「使用者實際聽到了什麼」。
        """
        async for sentence in source:
            self.tracer.mark(turn_id, marks.MARK_THINK_FIRST_SENTENCE)
            sink[sentence.index] = sentence
            self.tracer.count_sentence(turn_id, sentence.split_reason)
            yield sentence
        self.tracer.mark(turn_id, marks.MARK_THINK_DONE)

    def _resolve_spoken(
        self,
        sentences: dict[int, Sentence],
        timeline: list[tuple[int, float]],
        emitted_duration: float,
        gap_duration: float = 0.0,
    ) -> tuple[str, float]:
        """推算「使用者實際聽到了什麼」。

        沒有 sink（純量測、無播放）時，送出去就算聽到了。
        有 sink 時以 ``played_duration_s`` 為準——TTS 已經合成、也寫進 sink
        緩衝，但 barge-in 時緩衝會被丟棄，那部分**使用者從沒聽到**，
        不能寫進對話歷史。

        句子的「部分播出」一律算作已播出。寧可讓 LLM 以為自己多講了半句，
        也不要讓它以為少講了——後者會導致下一輪重複已經講過的內容。
        """
        if not timeline:
            return "", 0.0

        played = emitted_duration
        if self.sink is not None:
            with contextlib.suppress(Exception):
                # sink 的播放量含墊片音（它排在最前面），對回文字前要扣掉——
                # 不扣就會把「嗯…」那一秒算成真句子已播出
                played = max(
                    0.0,
                    min(emitted_duration, float(self.sink.played_duration_s) - gap_duration),
                )

        max_index = -1
        chunk_start = 0.0
        for sentence_index, cumulative in timeline:
            # 只要這個 chunk 的起點在播放範圍內，就算聽到了一部分
            # （timeline 記的是 chunk 結尾累積時長，起點 = 前一個 chunk 的結尾）
            if chunk_start + 1e-9 < played or sentence_index <= max_index:
                max_index = max(max_index, sentence_index)
            else:
                break
            chunk_start = cumulative

        if max_index < 0:
            return "", played

        spoken = "".join(
            sentences[i].text for i in sorted(sentences) if i <= max_index and sentences[i].text
        )
        return spoken, played


__all__ = ["PipelineRunner", "AudioChunk", "GAP_SENTENCE_INDEX"]
