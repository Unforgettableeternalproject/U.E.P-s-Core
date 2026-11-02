"""InputStage adapter：真 STT（Phase 3）。

## 這一層在解什麼問題

與 TTS adapter 是鏡像問題：後端（faster-whisper 系）是**同步阻塞**推理，
契約是 **async pull**（``AsyncIterator[Utterance]``）。另外多兩件事：

1. **turn 邊界是這一層決定的，不是 STT 子系統。**
   echo_stt 自己有 VAD + segment 切分，但那是「一段可辨識的音訊」，
   不是「使用者講完一輪了」——後者是管線的職責（§7.5），而且
   之後要換 Smart Turn v3，控制權必須在這裡。
   所以走 ``transcribe_segment``（直接辨識、跳過子系統 VAD），
   VAD 與 turn 判定用 Echo Stream 自己的
   :class:`~uep.stream.audio.vad.EnergyVad` + ``TurnDetector``。

2. **音訊消費不能停。** ``stream()`` 是 async generator，只有 runner
   在等它時才會推進；但 barge-in 要求機器人講話期間麥克風照聽。
   所以音訊處理放背景 task，經 :class:`StreamChannel` 出貨——
   generator 沒被拉動時，背景 task 照樣跑 VAD、照樣觸發
   ``on_voice_event``（前端據此接 InterruptionDetector → runner.interrupt）。

## 模型無關的邊界（2026-08-20 調查後的設計要求）

調查結論：Whisper 系暫時不換，但 FunASR Paraformer-streaming
（中文 CER ~10%、真串流、首字 ~600ms）是明確的 pilot 候選。
所以 transcriber 邊界收斂成 :class:`TranscriberBackend`——
**bytes 進、`TranscriptionResult` 出**，不滲漏任何子系統型別。
換 FunASR = 寫一個新 backend，這個 adapter 與管線契約都不動。

## 取樣率

依 §7.6，這裡假設 source 已是 16kHz mono int16（``STT_SAMPLE_RATE``）。
重採樣是 Frontend Adapter 的責任，不在這層做。
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from . import config
from ..contracts.cancellation import CancellationToken, CancelledError
from ..contracts.stages import AudioSource
from ..contracts.turn import TurnDetector, TurnPolicy, VoiceEvent
from ..contracts.types import STT_SAMPLE_RATE, Utterance, new_turn_id
from ..channel import StreamChannel
from ..audio.channel_engine import Segment as _Segment
from ..audio.channel_engine import VoiceChannel as _Channel
from ..audio.channel_engine import decode_stats, merge_parts
from ..turn_detector import SilenceTurnDetector
from ..audio.vad import EnergyVad


@dataclass(slots=True)
class TranscriptionResult:
    """一次轉錄的產物。backend 無關的最小集合。"""

    text: str
    language: str | None = None
    confidence: float = 1.0
    # Whisper 解碼品質（保留段落中最差的值）：temperature 取最大、avg_logprob 取最小、
    # compression_ratio 取最大。backend 拿不到就留 None，給尾端延遲分析用
    temperature: float | None = None
    avg_logprob: float | None = None
    compression_ratio: float | None = None


@runtime_checkable
class TranscriberBackend(Protocol):
    """STT 後端的最小形狀。

    **同步阻塞**呼叫——adapter 負責丟進 executor。輸入是原始 PCM bytes
    而不是 numpy array，numpy 只活在各 backend 的內部。
    回傳 None 表示這段音訊沒有可用的轉錄（幻覺被過濾、純噪音）。
    """

    def transcribe(self, pcm: bytes, sample_rate: int) -> TranscriptionResult | None: ...


class SttInputStage:
    """把「PCM 流 → VAD → turn 判定 → 轉錄」包成 InputStage。

    ``backend`` 可注入——理由同 TTS adapter：橋接邏輯要能在沒有 GPU、
    沒有 echo_stt 的環境下測試，Phase 3 的多數 bug 會出在
    turn 切分與時間標記，不在模型。
    """

    name = "stt_input"

    def __init__(
        self,
        *,
        backend: TranscriberBackend | None = None,
        stt_repo: str | None = None,
        turn_detector: TurnDetector | None = None,
        policy: TurnPolicy | None = None,
        vad: EnergyVad | None = None,
        language: str | None = None,
        min_confidence: float | None = None,
        preroll_s: float = 0.2,
        max_pending_segments: int = 4,
        queue_size: int = 2,
        on_voice_event: Callable[[VoiceEvent], None] | None = None,
        on_partial: Callable[[str, int], None] | None = None,
        partial_silence_s: float = 0.35,
        partial_min_speech_s: float = 2.0,
    ) -> None:
        self._backend = backend
        self._stt_repo = stt_repo
        self.policy = policy or TurnPolicy()
        self.turn_detector = turn_detector or SilenceTurnDetector(self.policy)
        self.vad = vad or EnergyVad()
        self.language = language or config.get("ECHO_STREAM_STT_LANGUAGE")
        self.min_confidence = (
            min_confidence
            if min_confidence is not None
            else config.get_float("ECHO_STREAM_STT_MIN_CONFIDENCE", 0.35)
        )
        """低於此信心值的轉錄直接丟棄，不進 LLM。
        雜訊誤轉錄（實測 "Hello!" conf=0.2 之類）會觸發一整輪
        LLM + TTS，浪費之外還會污染對話歷史。實測正常語音 conf 0.6+。"""
        self.preroll_s = preroll_s
        """語音起點前保留的音訊量。VAD 判定總比實際起音晚一點，
        不補前導的話每句的第一個字會被削掉。"""

        self.max_pending_segments = max_pending_segments
        self.queue_size = queue_size
        self.on_voice_event = on_voice_event
        """每個 VAD 事件的同步 callback。前端在這裡接 InterruptionDetector
        （機器人講話中偵測插話 → ``runner.interrupt()``）。
        必須快——它在音訊路徑上。"""

        self.on_partial = on_partial
        """增量轉錄的 callback ``(text, index)``。turn 進行中每轉出一段就
        呼叫一次——前端據此逐句條列使用者的話，不必等整個 turn 講完。
        在 event loop 上呼叫，必須快。``None`` 表示不啟用增量切分。"""

        self.partial_silence_s = partial_silence_s
        """觸發增量切分的停頓長度。要**小於** turn 的 silence_threshold
        （否則 turn 先結束了），又不能太小——太小會在氣口處切出破碎段，
        Whisper 對半句話的辨識品質明顯較差。"""

        self.partial_min_speech_s = partial_min_speech_s
        """距上次切分至少累積這麼多語音才再切。短句一次轉完就好，
        增量切分是給長獨白用的——順便把長 turn 的最終轉錄成本攤平
        （殘餘段很短，turn 結束後的 STT 延遲不再隨講話長度線性成長）。"""

        self._prepared = False
        self._generation = 0
        self._segments_ref: asyncio.Queue[_Segment | None] | None = None

    def discard_current(self) -> None:
        """丟棄進行中的輸入：累積中的音訊、排隊與轉錄中的段全部作廢。

        用途：旁邊有別人講話、誤觸發——這次輸入**不算在對話裡**。
        **必須在 event loop 執行緒呼叫**（web server 走 call_soon_threadsafe）。

        機制是世代號：discard 把世代 +1，讀取迴圈在下一個 chunk 察覺後
        清空本地狀態；已排隊的段直接抽掉；正在 executor 轉錄的段無法中斷，
        但結果回來時世代對不上就丟棄。
        """
        self._generation += 1
        queue_ref = self._segments_ref
        if queue_ref is not None:
            while True:
                try:
                    item = queue_ref.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if item is None:  # 結束哨兵不能吞——放回去
                    with contextlib.suppress(asyncio.QueueFull):
                        queue_ref.put_nowait(None)
                    break

    # --- 生命週期 ---

    async def prepare(self) -> None:
        """載入模型。很慢（Whisper 載入數秒到數十秒），只做一次。"""
        if self._prepared:
            return
        loop = asyncio.get_running_loop()
        if self._backend is None:
            self._backend = await loop.run_in_executor(None, self._build_backend)
        self._prepared = True

    def _build_backend(self) -> TranscriberBackend:
        """建立真的 echo_stt backend。**只有這裡 import echo_stt。**"""
        config.ensure_subsystem("STT", "echo_stt", self._stt_repo)
        return _EchoSttBackend(language=self.language)

    async def aclose(self) -> None:
        backend = self._backend
        if backend is not None:
            close = getattr(backend, "close", None)
            if callable(close):
                loop = asyncio.get_running_loop()
                with contextlib.suppress(Exception):
                    await loop.run_in_executor(None, close)
        self._prepared = False

    # --- 串流 ---

    async def stream(
        self, source: AudioSource, token: CancellationToken
    ) -> AsyncIterator[Utterance]:
        await self.prepare()
        if source.sample_rate != self.vad.sample_rate:
            raise ValueError(
                f"source 取樣率 {source.sample_rate} 與 VAD 的 "
                f"{self.vad.sample_rate} 不符。重採樣屬於 Frontend Adapter"
                "（§7.6），不在這層做。"
            )
        loop = asyncio.get_running_loop()

        # 待轉錄段用有界 queue：轉錄追不上講話速度時，背壓回讀取端。
        # 順序靠單一 worker 保證——轉錄不能並行，否則 turn 會亂序進管線。
        segments: asyncio.Queue[_Segment | None] = asyncio.Queue(
            maxsize=self.max_pending_segments
        )
        out: StreamChannel[Utterance] = StreamChannel(maxsize=self.queue_size, token=token)

        self._segments_ref = segments
        reader = asyncio.ensure_future(self._read(source, token, segments))
        worker = asyncio.ensure_future(self._transcribe_worker(segments, out, loop, token))

        try:
            async for utterance in out:
                yield utterance
        except CancelledError:
            # 與 FakeInputStage 行為一致：session 取消 → 安靜結束，
            # 善後由 finally 做。取消不是 InputStage 的錯誤。
            return
        finally:
            self._segments_ref = None
            for task in (reader, worker):
                if not task.done():
                    task.cancel()
            for task in (reader, worker):
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    await task

    async def _read(
        self,
        source: AudioSource,
        token: CancellationToken,
        segments: asyncio.Queue[_Segment | None],
    ) -> None:
        """音訊讀取迴圈：VAD → turn 判定 → 切出待轉錄段。

        這個 task 的壽命 = 整個 session。它**不做**任何耗時工作，
        轉錄在另一個 worker——否則辨識的一秒鐘裡 VAD 是聾的，
        插話偵測會有空窗。
        """
        channel = _Channel(self.vad, self.turn_detector, self)
        channel.gen = self._generation

        try:
            async for chunk in source.stream(token):
                if token.is_cancelled:
                    return
                if not chunk:
                    continue

                # discard_current 把世代推前了：手上的累積全是被丟棄的輸入
                if self._generation != channel.gen:
                    channel.gen = self._generation
                    channel.reset()

                for segment in channel.push(chunk, time.perf_counter()):
                    await segments.put(segment)

            # source 正常結束（檔案播完 / 麥克風關閉）：flush 殘餘。
            # 世代對不上代表殘餘是被丟棄的輸入——停止前按了捨棄，不該補送
            if self._generation == channel.gen:
                tail = channel.flush(time.perf_counter())
                if tail is not None:
                    await segments.put(tail)
        except CancelledError:
            return
        finally:
            with contextlib.suppress(asyncio.QueueFull):
                segments.put_nowait(None)

    async def _transcribe_worker(
        self,
        segments: asyncio.Queue[_Segment | None],
        out: StreamChannel[Utterance],
        loop: asyncio.AbstractEventLoop,
        token: CancellationToken,
    ) -> None:
        """依序轉錄。單一 worker——段的順序不能亂，GPU 也不能並行。

        增量段（``final=False``）的結果先推 ``on_partial``；turn 的
        final 段到達時把累積的增量結果與殘餘段拼成完整 Utterance。
        每段各自過信心閘門——雜訊段被剔除，不污染整段轉錄。
        """
        parts: list[TranscriptionResult] = []
        parts_gen = -1

        def _passes(result: TranscriptionResult | None) -> bool:
            return (
                result is not None
                and bool(result.text.strip())
                and result.confidence >= self.min_confidence
            )

        def _emit_partial(text: str) -> None:
            if self.on_partial is None:
                return
            try:
                self.on_partial(text, len(parts) - 1)
            except Exception:  # noqa: BLE001 - 訂閱者壞掉不能斷轉錄
                pass

        try:
            while True:
                segment = await segments.get()
                if segment is None:
                    break
                token.raise_if_cancelled()
                if segment.gen != self._generation:
                    continue  # 被 discard 作廢的段——連轉錄都省下來
                if segment.gen != parts_gen:
                    parts = []  # 上一個世代殘留的增量結果一併作廢
                    parts_gen = segment.gen
                final_started = time.perf_counter()
                result = (
                    await loop.run_in_executor(
                        None, self._backend.transcribe, segment.pcm, self.vad.sample_rate
                    )
                    if segment.pcm
                    else None
                )
                if segment.gen != self._generation:
                    continue  # 轉錄期間被 discard——結果作廢
                if _passes(result):
                    parts.append(result)
                    _emit_partial(result.text.strip())
                if not segment.final:
                    continue

                if not parts:
                    continue  # 整個 turn 都是雜訊
                text, confidence, language = merge_parts(parts)
                stats_parts = parts
                n_parts = len(parts)
                parts = []
                done_ts = time.perf_counter()
                await out.put(
                    Utterance(
                        text=text,
                        turn_id=new_turn_id(),
                        is_final=True,
                        confidence=confidence,
                        language=language,
                        started_at=segment.started_at,
                        ended_at=segment.ended_at,
                        metadata={
                            "speech_start_ts": segment.started_at,
                            "speech_end_ts": segment.ended_at,
                            "turn_end_ts": segment.turn_end_ts,
                            "stt_done_ts": done_ts,
                            "stt_ms": (done_ts - final_started) * 1000.0,
                            "queue_ms": max(0.0, final_started - segment.turn_end_ts) * 1000.0,
                            "channel_segments": n_parts,
                            **decode_stats(stats_parts),
                        },
                    )
                )
        except CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001 - 傳給消費端而不是消失在 task 裡
            out.fail(exc)
            return
        out.close()


class _EchoSttBackend:
    """echo_stt（faster-whisper 系）的 TranscriberBackend 實作。

    走 ``MultiChannelSTTEngine.transcribe_segment``——同步直接辨識，
    跳過子系統自己的 VAD / channel 管線（turn 切分已由 adapter 做掉），
    但保留它的後處理（hallucination 過濾 + 繁簡轉換），那是實測踩過坑
    才加的東西，不該因為換了入口就丟掉。
    """

    def __init__(self, language: str | None = None) -> None:
        from echo_stt.core.config import load_config
        from echo_stt.core.multi_channel_engine import MultiChannelSTTEngine

        # load_config() 回傳的是 STT 模組的全域快取 dict——先深拷貝再改，
        # 否則「強制打開 multi_channel」會污染同行程內其他使用者看到的設定。
        cfg = copy.deepcopy(load_config())
        # transcribe_segment 需要引擎初始化，而初始化被 enabled 旗標擋著。
        # 這裡是程式化使用，不受子系統 yaml 的預設值管——強制打開。
        cfg.setdefault("multi_channel", {})["enabled"] = True
        self._language = language
        self._engine = MultiChannelSTTEngine(config=cfg)
        if not self._engine.initialize():
            raise RuntimeError(
                "MultiChannelSTTEngine 初始化失敗——檢查 echo_stt 的模型與設定"
            )

    def transcribe(self, pcm: bytes, sample_rate: int) -> TranscriptionResult | None:
        import numpy as np

        if sample_rate != STT_SAMPLE_RATE:
            raise ValueError(f"echo_stt 只吃 {STT_SAMPLE_RATE}Hz，收到 {sample_rate}")
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        entry = self._engine.transcribe_segment(
            audio, channel_id="echo_stream", language=self._language
        )
        if entry is None:
            return None
        return TranscriptionResult(
            text=entry.text,
            language=entry.language,
            confidence=entry.confidence,
        )

    def close(self) -> None:
        self._engine.shutdown()


__all__ = [
    "SttInputStage",
    "TranscriberBackend",
    "TranscriptionResult",
]
