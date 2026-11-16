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

## 句首冷啟動雜音與過長前導空白（VoxLeadTrimmer）

每一句開頭都有約 20~35ms 的小爆發（峰值約 -36 dBFS），之後 330~870ms 近乎無聲
才開口。:class:`VoxLeadTrimmer` 在 48kHz 原始串流上、重採樣與段緣平滑之前處理：
特徵完全符合才刪爆發、前導超過保留長度才縮短，且只動開口點之前；任何不確定一律原樣。
判斷開口點需要多看幾個 chunk，所以句首會暫存（上限 ``lead_trim_max_hold_s``）。
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


LEAD_WINDOW_MS = 5.0
"""前導分析的視窗長度（取每個視窗的峰值）。"""
LEAD_QUIET_PEAK = 80
"""視窗峰值（int16 LSB）不超過此值視為「安靜」（底噪約 20~30）；超過即視為開口點的候選。
比離線 B 版的 120 保守：寧可少刪。"""
BURST_PEAK_MIN = 200
BURST_PEAK_MAX = 1200
"""句首爆發的峰值範圍（LSB）。實測約 370~684；超出範圍不當成爆發。"""
BURST_MAX_MS = 50.0
"""爆發最長持續時間；持續更久代表是語音或其他內容。實測約 20~35ms。"""
BURST_MIN_QUIET_MS = 80.0
"""爆發之後必須接一段至少這麼長的安靜區，否則不刪（實測 330ms 以上）。"""
LEAD_KEEP_MS = 150.0
"""開口點前保留的前導長度；前導不超過它就不縮短。"""
LEAD_FADE_MS = 8.0
"""刪除後新句首的餘弦淡入長度。"""
LEAD_MAX_HOLD_S = 1.0
"""句首最多暫存多久的音訊來找開口點；超過就原樣放行。"""


class VoxLeadTrimmer:
    """每句一個：刪除句首冷啟動爆發並縮短過長的前導空白（48kHz float32 單聲道）。

    保護規則（Bernie 硬性要求，不得截到主要音訊）：

    * 只有「句首第一個視窗就有短爆發（峰值落在範圍內、持續不超過 :data:`BURST_MAX_MS`）、
      接著至少 :data:`BURST_MIN_QUIET_MS` 的安靜區」才算爆發；不符合就不動。
    * 前導只縮短到「開口點前 keep_ms」，且被刪的區段全部已確認是安靜視窗，絕不越過開口點。
    * 看不出來（第一段就有持續能量、找不到開口點、暫存到上限）一律原樣放行。

    用法：逐 chunk :meth:`feed`，回傳可以送出的樣本（決定前為空）；來源結束時 :meth:`flush`。
    """

    def __init__(
        self,
        sample_rate: int = VOX_SAMPLE_RATE,
        *,
        keep_ms: float = LEAD_KEEP_MS,
        fade_ms: float = LEAD_FADE_MS,
        max_hold_s: float = LEAD_MAX_HOLD_S,
    ) -> None:
        self.sample_rate = sample_rate
        self._win = max(1, int(sample_rate * LEAD_WINDOW_MS / 1000))
        self._keep = int(sample_rate * keep_ms / 1000)
        self._fade = int(sample_rate * fade_ms / 1000)
        self._cap = int(sample_rate * max_hold_s)
        self._max_burst_win = max(1, int(BURST_MAX_MS / LEAD_WINDOW_MS))
        self._min_quiet_win = max(1, int(BURST_MIN_QUIET_MS / LEAD_WINDOW_MS))
        self._parts: list[Any] = []
        self._held = 0
        self._decided = False
        self.fade_applied = False
        """True＝已在句首做過淡入，呼叫端不要再疊 smooth_segment_edges 的淡入。"""
        self.trimmed_samples = 0
        """從句首刪掉的樣本數（爆發＋縮短的空白）。"""
        self.info: dict[str, Any] = {"reason": "undecided", "burst": False}
        """逐句診斷：reason、burst、burst_ms、cut_ms、orig_lead_ms、kept_lead_ms。"""

    def feed(self, samples: Any) -> Any:
        """餵入一個 chunk；回傳此刻可送出的樣本（尚在暫存、等更多資料時為空陣列）。"""
        import numpy as np

        if self._decided:
            return samples
        self._parts.append(np.asarray(samples, dtype="float32"))
        self._held += len(samples)
        buf = np.concatenate(self._parts)
        result = self._analyze(buf, final=False)
        if result is None and self._held >= self._cap:
            result = self._analyze(buf, final=True, reason="hold_cap")
        if result is None:
            return buf[:0]
        return self._release(buf, result)

    def flush(self) -> Any:
        """來源結束：把還在暫存的樣本放出（資料不足以判斷就原樣）。"""
        import numpy as np

        if self._decided or not self._parts:
            self._decided = True
            return np.zeros(0, dtype="float32")
        buf = np.concatenate(self._parts)
        return self._release(buf, self._analyze(buf, final=True, reason="stream_ended"))

    def _release(self, buf: Any, result: tuple[int, dict[str, Any]]) -> Any:
        import numpy as np

        cut_end, info = result
        self._decided = True
        self._parts = []
        self.info = info
        out = buf[cut_end:]
        if cut_end > 0:
            out = out.copy()
            n = min(self._fade, len(out))
            if n > 0:
                out[:n] *= 0.5 * (1.0 - np.cos(np.pi * np.arange(n) / n))
            self.fade_applied = True
            self.trimmed_samples = cut_end
        return out

    def _analyze(
        self, buf: Any, *, final: bool, reason: str = "undecided"
    ) -> tuple[int, dict[str, Any]] | None:
        """回傳 (刪除的句首樣本數, 診斷)；資料還不夠決定時回 None（final 時一律有結果）。"""
        import numpy as np

        win, rate = self._win, self.sample_rate
        nw = len(buf) // win
        if nw == 0:
            return (0, {"reason": reason, "burst": False}) if final else None
        peaks = (np.abs(buf[: nw * win]) * 32768.0).reshape(nw, win).max(axis=1)

        def keep(why: str, **extra: Any) -> tuple[int, dict[str, Any]]:
            return 0, {"reason": why, "burst": False, **extra}

        def undecided() -> tuple[int, dict[str, Any]] | None:
            return keep(reason) if final else None

        def ms(n_samples: int) -> float:
            return n_samples * 1000.0 / rate

        loud = np.flatnonzero(peaks > LEAD_QUIET_PEAK)
        if peaks[0] > LEAD_QUIET_PEAK:
            # 第一個視窗就有能量：只有「短爆發＋之後一段安靜」才是冷啟動爆發
            if peaks[0] > BURST_PEAK_MAX:
                return keep("loud_start")
            quiet_idx = np.flatnonzero(peaks <= LEAD_QUIET_PEAK)
            if quiet_idx.size == 0:
                return keep("sustained_energy") if nw > self._max_burst_win else undecided()
            k = int(quiet_idx[0])  # 爆發佔前 k 個視窗
            if k > self._max_burst_win:
                return keep("sustained_energy")
            burst_peak = float(peaks[:k].max())
            if not BURST_PEAK_MIN <= burst_peak <= BURST_PEAK_MAX:
                return keep("burst_peak_out_of_range", burst_peak=burst_peak)
            after = loud[loud >= k]
            if after.size and int(after[0]) - k < self._min_quiet_win:
                return keep("burst_without_quiet", burst_peak=burst_peak)
            if not after.size:
                return undecided()  # 還沒看到開口點
            j = int(after[0])  # 開口點視窗
            burst_end = k * win
            lead = (j - k) * win
            cut_end = burst_end
            if lead > self._keep:
                cut_end = j * win - self._keep
            return cut_end, {
                "reason": "burst_trimmed" if cut_end == burst_end else "burst_and_lead_trimmed",
                "burst": True,
                "burst_ms": ms(burst_end),
                "burst_peak": burst_peak,
                "orig_lead_ms": ms(lead),
                "kept_lead_ms": ms(j * win - cut_end),
                "cut_ms": ms(cut_end),
            }

        # 沒有爆發：只有前導超過保留長度才縮短，且只刪到開口點前 keep
        if not loud.size:
            return undecided()
        lead = int(loud[0]) * win
        if lead <= self._keep:
            return keep("short_lead", orig_lead_ms=ms(lead))
        cut_end = lead - self._keep
        return cut_end, {
            "reason": "lead_trimmed",
            "burst": False,
            "orig_lead_ms": ms(lead),
            "kept_lead_ms": ms(self._keep),
            "cut_ms": ms(cut_end),
        }


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
        lead_trim_enabled: bool = True,
        lead_trim_keep_ms: float = LEAD_KEEP_MS,
        lead_trim_max_hold_s: float = LEAD_MAX_HOLD_S,
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
        self.lead_trim_enabled = lead_trim_enabled
        """句首冷啟動雜音／過長前導處理開關（UEP_INFER_VOX_LEAD_TRIM_ENABLED）；False＝完全原樣輸出。"""
        self.lead_trim_keep_ms = lead_trim_keep_ms
        self.lead_trim_max_hold_s = lead_trim_max_hold_s
        """找開口點時句首最多暫存的音訊長度；超過就原樣放行。"""
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
        trimmer = (
            VoxLeadTrimmer(
                VOX_SAMPLE_RATE,
                keep_ms=self.lead_trim_keep_ms,
                max_hold_s=self.lead_trim_max_hold_s,
            )
            if self.lead_trim_enabled
            else None
        )

        def set_lead(value: float) -> None:
            nonlocal lead_ms
            lead_ms = value

        def push(mono: Any) -> None:
            """重採樣、保留尾端、送出一個 chunk。"""
            nonlocal carry
            buf = np.concatenate([carry, self._resample(resampler, mono, last=False)])
            if n_hold > 0:
                # 尾端留 n_hold 個樣本，等句尾時才能正確淡出
                if len(buf) <= n_hold:
                    carry = buf
                    return
                carry, buf = buf[-n_hold:], buf[:-n_hold]
            else:
                carry = buf[:0]
            emit(buf, first=chunk_index == 0, final=False)

        def emit(samples: Any, *, first: bool, final: bool) -> None:
            nonlocal chunk_index
            pcm = tensor_to_pcm16(samples) if len(samples) else b""
            if self.edge_fade_ms > 0 or tail_silence_ms > 0:
                pcm = smooth_segment_edges(
                    pcm,
                    self.sample_rate,
                    fade_ms=self.edge_fade_ms,
                    tail_silence_ms=tail_silence_ms if final else 0.0,
                    # 句首已被 trimmer 淡入時不再疊 4ms 淡入
                    fade_in=first and not (trimmer is not None and trimmer.fade_applied),
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
                    if trimmer is None:
                        push(raw)
                    else:
                        # 句首暫存期間回傳空陣列；決定後才把（可能已裁過的）樣本送進後段
                        released = trimmer.feed(self._to_mono_f32(raw))
                        if len(released):
                            push(released)
                if trimmer is not None:
                    released = trimmer.flush()  # 句子太短、來不及判斷時原樣放出
                    if len(released):
                        push(released)
                if token.is_cancelled or stop.is_set():
                    return
                tail = np.concatenate([carry, self._resample(resampler, None, last=True)])
                emit(tail, first=chunk_index == 0, final=True)
                trimmed = trimmer.trimmed_samples if trimmer is not None else 0
                if trimmer is not None:
                    self._log_lead_trim(sentence, trimmer)
                self._log_sentence(
                    sentence, speak_text, wall_start, t_start, prev_end, first_chunk_s,
                    synth_s, raw_samples, n_raw, max_gap_s, lead_ms, trimmed,
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
        trimmed_samples: int = 0,
    ) -> None:
        """逐句一行 INFO，用來分辨斷感出在句間還是句內。只讀計時，不影響音訊。

        ``synth_ms`` 是 backend 產生音訊的累計耗時（不含通道背壓等待）；
        ``audio_s`` 是實際送出的音訊長度（已扣掉 trim 刪掉的句首）；``rtf`` 仍以
        backend 產出的原始長度計，``trim_ms`` 為刪掉的句首毫秒數。
        ``max_chunk_gap_ms`` 是首 chunk 之後、相鄰 chunk 之間最長的生成間隔
        （一個 chunk 約 160ms 音訊，超過就代表該段生成慢於即時）。
        """
        t_end = time.perf_counter()
        self._last_synth_end = (sentence.turn_id, t_end)
        raw_s = raw_samples / VOX_SAMPLE_RATE
        audio_s = (raw_samples - trimmed_samples) / VOX_SAMPLE_RATE
        rtf = synth_s / raw_s if raw_s > 0 else 0.0
        gap = f"{(t_start - prev_end) * 1000:.0f}" if prev_end is not None else "-"
        start_txt = time.strftime("%H:%M:%S", time.localtime(wall_start))
        start_txt += f".{int((wall_start % 1) * 1000):03d}"
        logger.info(
            "vox sentence turn=%s idx=%d chars=%d reason=%s start=%s first_chunk_ms=%.0f "
            "synth_ms=%.0f audio_s=%.2f rtf=%.2f gap_prev_ms=%s chunks=%d "
            "max_chunk_gap_ms=%.0f lead_ms=%s trim_ms=%.0f",
            sentence.turn_id, sentence.index, len(speak_text),
            sentence.split_reason or "-", start_txt, first_chunk_s * 1000,
            synth_s * 1000, audio_s, rtf, gap, n_chunks, max_gap_s * 1000,
            f"{lead_ms:.0f}" if lead_ms is not None else "-",
            trimmed_samples * 1000.0 / VOX_SAMPLE_RATE,
        )

    @staticmethod
    def _log_lead_trim(sentence: Sentence, trimmer: VoxLeadTrimmer) -> None:
        """逐句 DEBUG：有沒有偵測到爆發、刪了多少、原本與保留的前導長度。"""
        if not logger.isEnabledFor(logging.DEBUG):
            return
        info = trimmer.info
        logger.debug(
            "vox lead_trim turn=%s idx=%d reason=%s burst=%s burst_ms=%.0f cut_ms=%.0f "
            "orig_lead_ms=%s kept_lead_ms=%s",
            sentence.turn_id, sentence.index, info.get("reason", "-"), bool(info.get("burst")),
            info.get("burst_ms", 0.0), trimmer.trimmed_samples * 1000.0 / VOX_SAMPLE_RATE,
            f"{info['orig_lead_ms']:.0f}" if "orig_lead_ms" in info else "-",
            f"{info['kept_lead_ms']:.0f}" if "kept_lead_ms" in info else "-",
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
    "VoxLeadTrimmer",
    "VOX_SAMPLE_RATE",
    "DEFAULT_VOX_EMOTION_MAP",
    "VOX_EMOTION_LABELS",
    "normalize_emotion_map",
]
