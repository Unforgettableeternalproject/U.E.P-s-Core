"""SpeakStage adapter：VOX TTS。

對外介面與 :class:`~uep.stream.adapters.speak_indextts.IndexTTS2SpeakStage`
相同（prepare / stream / aclose），bot 只換 builder 就能切換。

## 與 IndexTTS2 adapter 的差異

* VoxBackend 的 ``stream(text, lang, speed)`` 是**同步 generator**，
  逐段 yield 約 160ms、48kHz 的單聲道 float32 numpy chunk；
  不是 push callback，所以不需要靠例外中止，直接 ``gen.close()`` 即可。
* backend 同時只能處理一個請求：stage 內部句子本來就序列合成，
  另外加一把 lock，連「上一個 turn 被取消、執行緒還在收尾」時
  新 turn 也不會並行呼叫 backend。
* 輸出取樣率預設 48kHz 直出（int16，不重採樣；11/11 Bernie 確認不降頻）；明確指定其他取樣率時用 soxr
  的串流重採樣器轉換（狀態跨 chunk 延續，chunk 邊界不會有不連續）。

## 取消

取消檢查與 ``gen.close()`` 都在**工作執行緒內**做：generator 綁在
建立它的執行緒上，跨執行緒 close 會拋 ValueError。正在跑的那一步
（幾十 ms）會跑完，之後不再繼續。通道有界，背壓同樣傳到 backend。

## 段緣平滑

句內 chunk 邊界是連續訊號，不能每個 chunk 都淡入淡出（會每 160ms 凹一次）。
所以只在句首淡入、句尾淡出 + 靜音墊。為了在不知道哪個 chunk 是最後一個的
情況下仍能淡出，會把輸出尾端 ``edge_fade_ms`` 的樣本留在 carry buffer
延後一個 chunk 送出——只多延遲幾毫秒，不影響 TTFA。
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import logging
import threading
import time
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any

from ..contracts.cancellation import CancellationToken, CancelledError, CancelReason
from ..contracts.style import EMOTION_DIMENSIONS, SpeechStyle
from ..contracts.types import AudioChunk, Sentence
from ..channel import ChannelClosed, StreamChannel
from ..splitter import SplitPolicy
from .speak_indextts import sanitize_for_tts, smooth_segment_edges, tensor_to_pcm16

logger = logging.getLogger(__name__)

VOX_SAMPLE_RATE = 48_000
"""VoxBackend 固定的輸出取樣率。"""

# 建議切句策略。來源：11/01 VOX 實測，首包約 0.1s，所以首句不必壓短；
# TTS 端要求只在標點切（hard_cap 沿用 SplitPolicy 預設 2 x max，超過 max 後遇標點即切）。
VOX_SPLIT_FIRST_MIN = 10.0
VOX_SPLIT_FIRST_MAX = 24.0
VOX_SPLIT_MIN = 30.0
VOX_SPLIT_MAX = 80.0

NEUTRAL_EMOTION = "neutral"
"""對照表的「不帶情緒」值：不傳 emotion／intensity 給 backend，行為與未接情緒時完全相同。"""

VOX_EMOTION_LABELS: tuple[str, ...] = (*EMOTION_DIMENSIONS, NEUTRAL_EMOTION)
"""Vox backend 接受的情緒標籤（僅 zh 生效，其餘語言由 backend 忽略）：八維標籤加 neutral。"""

DEFAULT_VOX_EMOTION_MAP: dict[str, tuple[str, float | None]] = {
    # SpeechStyle.style（preset 名；空字串時改用主導維度名）→ (Vox 情緒標籤, intensity)
    # intensity 為 None＝沿用 SpeechStyle.intensity。Bernie 11/11 裁決；
    # real-mode 驗證後再開（開關 UEP_INFER_VOX_EMOTION_ENABLED 預設關）。
    **{name: (name, None) for name in EMOTION_DIMENSIONS},
    "excited": ("happy", 0.8),
    "gentle": ("calm", 0.4),
    "stern": ("angry", 0.3),
    "wistful": ("melancholic", None),
    "flustered": ("surprised", None),
    "anticipated": ("happy", 0.4),
    "tsundere": (NEUTRAL_EMOTION, None),
    NEUTRAL_EMOTION: (NEUTRAL_EMOTION, None),
}
"""SpeechStyle → Vox 情緒對照表預設值（Bernie 11/11 裁決）；開關關閉時不生效。"""


def normalize_emotion_map(
    raw: Mapping[str, Any] | None,
) -> dict[str, tuple[str, float | None]]:
    """把設定的對照表疊在預設表上，並檢查值域（fail-closed）。

    每個值可寫成標籤字串（``"happy"``）或 ``{"emotion": "happy", "intensity": 0.6}``；
    標籤須在 :data:`VOX_EMOTION_LABELS`，intensity 須落在 0~1，否則丟 ValueError。
    """
    table = dict(DEFAULT_VOX_EMOTION_MAP)
    for key, value in (raw or {}).items():
        if isinstance(value, str):
            label, intensity = value, None
        elif isinstance(value, Mapping):
            label, intensity = value.get("emotion", NEUTRAL_EMOTION), value.get("intensity")
        else:
            raise TypeError(f"情緒對照表 {key!r} 的值必須是字串或物件：{value!r}")
        label = str(label).strip().lower()
        if label not in VOX_EMOTION_LABELS:
            raise ValueError(f"情緒對照表 {key!r} 的標籤 {label!r} 不在 {VOX_EMOTION_LABELS}")
        if intensity is not None:
            intensity = float(intensity)
            if not 0.0 <= intensity <= 1.0:
                raise ValueError(f"情緒對照表 {key!r} 的 intensity 必須在 0~1：{intensity}")
        table[str(key).strip().lower()] = (label, intensity)
    return table


DEFAULT_QUEUE_SIZE = 12
"""音訊通道上限（chunk 數）預設值；M5 前由 ES 的 ``ECHO_STREAM_VOX_QUEUE`` 環境變數覆寫，已移除，改由建構參數 ``queue_size`` 指定。"""


class VoxSpeakStage:
    """把 VoxBackend 包成 SpeakStage。

    ``backend`` 可注入（測試用假 backend）；None 時在 :meth:`prepare`
    才延遲 import Core 內 ``echo_tts`` 的 ``VoxBackend``，模組頂層不碰 echo_tts。
    """

    name = "vox"

    def __init__(
        self,
        *,
        backend: Any | None = None,
        backend_config: dict | None = None,
        sample_rate: int = VOX_SAMPLE_RATE,
        language: str | None = None,
        speed: float | None = None,
        sentence_buffer: int = 2,
        queue_size: int | None = None,
        edge_fade_ms: float = 4.0,
        segment_silence_ms: float = 15.0,
        terminal_pause_ms: float | None = None,
        secondary_pause_ms: float | None = None,
        emotion_enabled: bool = False,
        emotion_map: Mapping[str, Any] | None = None,
    ) -> None:
        self._backend = backend
        self.backend_config = backend_config
        self.sample_rate = sample_rate
        self.language = language
        self.speed = speed
        """stage 預設語速；句子的 ``SpeechStyle.speed`` 非 1.0 時優先。
        都沒有就傳 None，由 backend 決定。"""
        self.sentence_buffer = sentence_buffer
        self.queue_size = queue_size or DEFAULT_QUEUE_SIZE
        """音訊通道上限（chunk 數）。VOX 一個 chunk 約 160ms，這個值就是合成能領先
        播放多少：IndexTTS 那組的 2（約 320ms）在句間換新一次 backend 呼叫、又和
        Whisper 搶 GPU 時會欠流（11/01 實測接縫雜音）。12 ≈ 1.9s 的餘裕；取消時
        generator 每個 chunk 都檢查 token，已排隊的音訊直接丟棄，不會多燒 GPU。"""
        self.edge_fade_ms = edge_fade_ms
        self.segment_silence_ms = segment_silence_ms
        self.terminal_pause_ms = terminal_pause_ms
        """句末標點（。！？）切開的句子，句尾停頓毫秒。None = 沿用 segment_silence_ms。"""
        self.secondary_pause_ms = secondary_pause_ms
        """逗號等次級標點切開的句子，句尾停頓毫秒。None = 沿用 segment_silence_ms。

        兩者都是「換氣感」的旋鈕（11/01 聽感：太快、沒有呼吸感）：句間的停頓
        依切點性質給，硬切／flush 與整個 turn 的最後一句一律只用 segment_silence_ms。"""
        self.emotion_enabled = emotion_enabled
        """情緒開關：False 時完全不傳 emotion／intensity（行為同未接情緒）。"""
        self.emotion_map = normalize_emotion_map(emotion_map)
        """SpeechStyle → Vox 情緒對照表（已疊上預設）；預設全 neutral，開關打開也不會帶情緒。"""
        self._backend_lock = threading.Lock()
        """backend 同時只能處理一個請求；跨 turn 也要串行。"""
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="vox-tts"
        )
        """所有 backend 呼叫（建立、prepare、合成、unload）都走這條**同一個**執行緒。

        torch inductor 的 CUDA graph tree manager 是執行緒區域（TLS）的，只在做
        compile／暖機的那條執行緒初始化；合成落到別條執行緒會在
        ``cudagraph_trees.get_obj`` 斷言失敗。預設執行緒池不保證同一條。"""
        self._prepared = False
        self._last_synth_end: tuple[str, float] | None = None
        """(turn_id, perf_counter)：上一句合成結束時間，只用於逐句 log 的句間間隔。"""
        self._play_clock: tuple[str, float, int] | None = None
        """(turn_id, 第一個 chunk 送出的 perf_counter, 已送出樣本數)：只用於 log 的 lead_ms。"""

    @classmethod
    def recommended_split_policy(cls) -> SplitPolicy:
        """VOX 建議的切句策略；Core 與 bot 共用這一份，其餘欄位用 SplitPolicy 預設。"""
        return SplitPolicy(
            first_min_weight=VOX_SPLIT_FIRST_MIN,
            first_max_weight=VOX_SPLIT_FIRST_MAX,
            min_weight=VOX_SPLIT_MIN,
            max_weight=VOX_SPLIT_MAX,
        )

    # --- 生命週期 ---

    async def prepare(self) -> None:
        """載入模型與暖機。很慢（數十秒起），在 executor 跑且只做一次。"""
        if self._prepared:
            return
        loop = asyncio.get_running_loop()
        if self._backend is None:
            self._backend = await loop.run_in_executor(self._executor, self._build_backend)
        prepare = getattr(self._backend, "prepare", None)
        if callable(prepare):
            await loop.run_in_executor(self._executor, prepare)
        self._prepared = True

    def _build_backend(self) -> Any:
        """建立真的 VoxBackend（echo_tts 已遷入 Core）。**只有這裡 import echo_tts。**

        ``model_dir``／``lora_dir`` 未給時依 ``UEP_TTS_MODELS_DIR``（預設 ``~/.uep/models/vox``）補上。
        """
        from echo_tts.config.model_paths import with_default_vox_paths
        from echo_tts.runtime.vox_backend import VoxBackend

        return VoxBackend(with_default_vox_paths(self.backend_config))

    async def aclose(self) -> None:
        backend = self._backend
        unload = getattr(backend, "unload", None)
        if callable(unload):
            loop = asyncio.get_running_loop()
            # 在 executor 內取得 lock 再 unload，不會踩到正在推理的執行緒
            await loop.run_in_executor(self._executor, self._unload_locked, unload)
        self._prepared = False

    def _unload_locked(self, unload: Any) -> None:
        with self._backend_lock, contextlib.suppress(Exception):
            unload()

    # --- 串流 ---

    async def stream(
        self, sentences: AsyncIterator[Sentence], token: CancellationToken
    ) -> AsyncIterator[AudioChunk]:
        await self.prepare()
        loop = asyncio.get_running_loop()

        # 句子緩衝：合成期間 SentenceSplitter 要能繼續跑，維持層間重疊
        sentence_ch: StreamChannel[Sentence] = StreamChannel(
            maxsize=self.sentence_buffer, token=token
        )
        audio_ch: StreamChannel[AudioChunk] = StreamChannel(
            maxsize=self.queue_size, token=token
        )

        # per-call 停止訊號：消費端提早離開（break / aclose）但 token 沒取消時，
        # 工作執行緒要靠它從阻塞的 put 與 lock 等待中脫身，否則會永久握住 lock
        stop = threading.Event()

        feeder = asyncio.ensure_future(self._feed(sentences, sentence_ch))
        producer = asyncio.ensure_future(
            self._produce(sentence_ch, audio_ch, loop, token, stop)
        )

        try:
            async for chunk in audio_ch:
                yield chunk
        finally:
            stop.set()
            for task in (feeder, producer):
                if not task.done():
                    task.cancel()
            for task in (feeder, producer):
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    await task

    async def _feed(
        self, sentences: AsyncIterator[Sentence], channel: StreamChannel[Sentence]
    ) -> None:
        try:
            async for sentence in sentences:
                if not sentence.text:
                    continue
                await channel.put(sentence)
        except CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001 - 傳給消費端而不是消失在 task 裡
            channel.fail(exc)
            return
        channel.close()

    async def _produce(
        self,
        sentence_ch: StreamChannel[Sentence],
        audio_ch: StreamChannel[AudioChunk],
        loop: asyncio.AbstractEventLoop,
        token: CancellationToken,
        stop: threading.Event,
    ) -> None:
        try:
            async for sentence in sentence_ch:
                token.raise_if_cancelled()
                await loop.run_in_executor(
                    self._executor, self._synthesize_one, sentence, audio_ch, loop, token, stop
                )
        except CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001
            audio_ch.fail(exc)
            return
        audio_ch.close()

    def _tail_silence_ms(self, sentence: Sentence) -> float:
        """依切點性質決定句尾停頓；最後一句與硬切／flush 用 segment_silence_ms。"""
        pause: float | None = None
        if not sentence.is_last:
            if sentence.split_reason == "terminal":
                pause = self.terminal_pause_ms
            elif sentence.split_reason == "secondary":
                pause = self.secondary_pause_ms
        return self.segment_silence_ms if pause is None else pause

    def _resolve_speed(self, style: SpeechStyle | None) -> float | None:
        """只取 speed。style 非 1.0 優先，否則用 stage 預設，都沒有回 None。"""
        if style is not None and style.speed != 1.0:
            return style.speed
        return self.speed

    def _resolve_emotion(self, style: SpeechStyle | None) -> tuple[str, float] | None:
        """SpeechStyle → (Vox 情緒標籤, intensity)；不需帶情緒時回 None。

        查表順序：``style.style``（preset 名）→ 主導維度名；查不到或對到 neutral 都回 None。
        表內 intensity 為 None 時沿用 ``style.intensity``。
        """
        if not self.emotion_enabled or style is None or style.is_neutral:
            return None
        entry = self.emotion_map.get(style.style.strip().lower())
        dominant = style.dominant()
        if entry is None and dominant is not None:
            entry = self.emotion_map.get(dominant[0])
        if entry is None or entry[0] == NEUTRAL_EMOTION:
            return None
        return entry[0], entry[1] if entry[1] is not None else style.intensity

    def _synthesize_one(
        self,
        sentence: Sentence,
        audio_ch: StreamChannel[AudioChunk],
        loop: asyncio.AbstractEventLoop,
        token: CancellationToken,
        stop: threading.Event,
    ) -> None:
        """在 executor 執行緒裡消費同步 generator。**這個函式不在 event loop 上。**"""
        import numpy as np

        speak_text = sanitize_for_tts(sentence.text)
        if not speak_text:
            return  # 整句都是符號——沒東西可唸

        speed = self._resolve_speed(sentence.style)
        emotion = self._resolve_emotion(sentence.style)
        # 沒有情緒時不帶這兩個參數，呼叫形狀與未接情緒時一致
        emotion_kwargs: dict[str, Any] = (
            {"emotion": emotion[0], "intensity": emotion[1]} if emotion is not None else {}
        )
        tail_silence_ms = self._tail_silence_ms(sentence)
        # smooth_segment_edges 的淡化長度上限是段長的一半，尾端留兩倍才淡得滿 edge_fade_ms
        n_hold = 2 * int(self.sample_rate * self.edge_fade_ms / 1000)
        resampler = self._make_resampler()
        carry = np.zeros(0, dtype="float32")
        chunk_index = 0
        lead_ms: float | None = None

        def set_lead(value: float) -> None:
            nonlocal lead_ms
            lead_ms = value

        def emit(samples: Any, *, first: bool, final: bool) -> None:
            nonlocal chunk_index
            pcm = tensor_to_pcm16(samples) if len(samples) else b""
            if self.edge_fade_ms > 0 or tail_silence_ms > 0:
                pcm = smooth_segment_edges(
                    pcm,
                    self.sample_rate,
                    fade_ms=self.edge_fade_ms,
                    tail_silence_ms=tail_silence_ms if final else 0.0,
                    fade_in=first,
                    fade_out=final,
                )
            is_last = final and sentence.is_last
            if not pcm and not is_last:
                return
            chunk = AudioChunk(
                pcm=pcm,
                sample_rate=self.sample_rate,
                turn_id=sentence.turn_id,
                sentence_index=sentence.index,
                chunk_index=chunk_index,
                is_last=is_last,
            )
            is_sentence_first = chunk_index == 0
            chunk_index += 1
            # 通道滿會阻塞此執行緒 = 背壓；token 取消或 stop 設起時脫身
            self._put(audio_ch, chunk, loop, stop)
            self._note_sent(sentence.turn_id, len(pcm) // 2, is_sentence_first, set_lead)

        if not self._acquire_backend(token, stop):
            return
        try:
            gen = None
            t_start = time.perf_counter()
            wall_start = time.time()
            prev = self._last_synth_end
            prev_end = prev[1] if prev is not None and prev[0] == sentence.turn_id else None
            first_chunk_s = 0.0
            max_gap_s = 0.0
            synth_s = 0.0
            raw_samples = 0
            n_raw = 0
            try:
                gen = self._backend.stream(
                    speak_text, lang=self.language, speed=speed, **emotion_kwargs
                )
                while True:
                    # 只計 next() 的耗時：emit 的背壓等待不是合成時間
                    t_req = time.perf_counter()
                    try:
                        raw = next(gen)
                    except StopIteration:
                        break
                    t_got = time.perf_counter()
                    gen_s = t_got - t_req
                    if n_raw == 0:
                        first_chunk_s = t_got - t_start
                    else:
                        max_gap_s = max(max_gap_s, gen_s)
                    n_raw += 1
                    synth_s += gen_s
                    raw_samples += len(raw)
                    if token.is_cancelled or stop.is_set():
                        return
                    buf = np.concatenate([carry, self._resample(resampler, raw, last=False)])
                    if n_hold > 0:
                        # 尾端留 n_hold 個樣本，等句尾時才能正確淡出
                        if len(buf) <= n_hold:
                            carry = buf
                            continue
                        carry, buf = buf[-n_hold:], buf[:-n_hold]
                    else:
                        carry = buf[:0]
                    emit(buf, first=chunk_index == 0, final=False)
                if token.is_cancelled or stop.is_set():
                    return
                tail = np.concatenate([carry, self._resample(resampler, None, last=True)])
                emit(tail, first=chunk_index == 0, final=True)
                self._log_sentence(
                    sentence, speak_text, wall_start, t_start, prev_end, first_chunk_s,
                    synth_s, raw_samples, n_raw, max_gap_s, lead_ms,
                )
            except (CancelledError, ChannelClosed, concurrent.futures.CancelledError):
                return
            finally:
                # 取消 / 例外時關閉 generator，必須在建立它的這條執行緒
                if gen is not None:
                    with contextlib.suppress(Exception):
                        gen.close()
        finally:
            self._backend_lock.release()

    def _note_sent(self, turn_id: str, n_samples: int, sentence_first: bool, set_lead: Any) -> None:
        """記錄已送出的音訊量；句子第一個 chunk 送出時算 lead_ms。

        lead_ms = 本 turn 到此為止已送出的音訊長度 - 自本 turn 第一個 chunk 送出
        起經過的實際時間（假設即時播放；不含本 chunk）。負值 = 這個接縫一定欠流。
        n_samples 是 pcm16 位元組數的一半，即實際送出的輸出取樣率樣本數
        （重採樣、尾端保留、淡化與靜音墊都已包含）。
        """
        now = time.perf_counter()
        clock = self._play_clock
        if clock is None or clock[0] != turn_id:
            clock = (turn_id, now, 0)  # turn 的第一個 chunk：lead 為 0
        if sentence_first:
            elapsed = now - clock[1]
            set_lead((clock[2] / self.sample_rate - elapsed) * 1000)
        self._play_clock = (clock[0], clock[1], clock[2] + n_samples)

    def _log_sentence(
        self,
        sentence: Sentence,
        speak_text: str,
        wall_start: float,
        t_start: float,
        prev_end: float | None,
        first_chunk_s: float,
        synth_s: float,
        raw_samples: int,
        n_chunks: int,
        max_gap_s: float,
        lead_ms: float | None,
    ) -> None:
        """逐句一行 INFO，用來分辨斷感出在句間還是句內。只讀計時，不影響音訊。

        ``synth_ms`` 是 backend 產生音訊的累計耗時（不含通道背壓等待）；
        ``max_chunk_gap_ms`` 是首 chunk 之後、相鄰 chunk 之間最長的生成間隔
        （一個 chunk 約 160ms 音訊，超過就代表該段生成慢於即時）。
        """
        t_end = time.perf_counter()
        self._last_synth_end = (sentence.turn_id, t_end)
        audio_s = raw_samples / VOX_SAMPLE_RATE
        rtf = synth_s / audio_s if audio_s > 0 else 0.0
        gap = f"{(t_start - prev_end) * 1000:.0f}" if prev_end is not None else "-"
        start_txt = time.strftime("%H:%M:%S", time.localtime(wall_start))
        start_txt += f".{int((wall_start % 1) * 1000):03d}"
        logger.info(
            "vox sentence turn=%s idx=%d chars=%d reason=%s start=%s first_chunk_ms=%.0f "
            "synth_ms=%.0f audio_s=%.2f rtf=%.2f gap_prev_ms=%s chunks=%d "
            "max_chunk_gap_ms=%.0f lead_ms=%s",
            sentence.turn_id, sentence.index, len(speak_text),
            sentence.split_reason or "-", start_txt, first_chunk_s * 1000,
            synth_s * 1000, audio_s, rtf, gap, n_chunks, max_gap_s * 1000,
            f"{lead_ms:.0f}" if lead_ms is not None else "-",
        )

    def _acquire_backend(self, token: CancellationToken, stop: threading.Event) -> bool:
        """取得 backend lock。等待期間 token 取消或 stop 設起就放棄，不啟動推理。"""
        while not self._backend_lock.acquire(timeout=0.05):
            if token.is_cancelled or stop.is_set():
                return False
        if token.is_cancelled or stop.is_set():
            self._backend_lock.release()
            return False
        return True

    @staticmethod
    def _put(
        channel: StreamChannel[AudioChunk],
        chunk: AudioChunk,
        loop: asyncio.AbstractEventLoop,
        stop: threading.Event,
    ) -> None:
        """從工作執行緒寫入通道；滿時阻塞（背壓），但 stop 設起時會脫身。"""
        future = asyncio.run_coroutine_threadsafe(channel.put(chunk), loop)
        while True:
            try:
                future.result(timeout=0.05)
                return
            except concurrent.futures.TimeoutError:
                if stop.is_set():
                    future.cancel()
                    raise CancelledError(CancelReason.SHUTDOWN) from None

    # --- 重採樣 ---

    def _make_resampler(self) -> Any | None:
        """48kHz → 輸出取樣率的串流重採樣器（soxr）；取樣率相同時不需要。"""
        if self.sample_rate == VOX_SAMPLE_RATE:
            return None
        import soxr

        return soxr.ResampleStream(
            VOX_SAMPLE_RATE, self.sample_rate, 1, dtype="float32", quality="HQ"
        )

    @staticmethod
    def _to_mono_f32(raw: Any) -> Any:
        import numpy as np

        arr = np.asarray(raw, dtype="float32")
        arr = np.squeeze(arr)
        if arr.ndim > 1:
            arr = arr[0]
        return np.atleast_1d(arr)

    def _resample(self, resampler: Any | None, raw: Any | None, *, last: bool) -> Any:
        import numpy as np

        arr = np.zeros(0, dtype="float32") if raw is None else self._to_mono_f32(raw)
        if resampler is None:
            return arr
        return resampler.resample_chunk(arr, last=last).astype("float32", copy=False)


__all__ = [
    "VoxSpeakStage",
    "VOX_SAMPLE_RATE",
    "DEFAULT_VOX_EMOTION_MAP",
    "VOX_EMOTION_LABELS",
    "normalize_emotion_map",
]
