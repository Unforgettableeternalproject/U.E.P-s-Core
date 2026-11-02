"""多聲道 InputStage adapter：薄包裝 :class:`~uep.stream.audio.multichannel.ChannelEngine`。

聲道狀態機、轉錄池、無封包補靜音等平台無關邏輯都在 ``core/``；
這一層只負責 adapters 的事：

* 組態（與單聲道 :class:`SttInputStage` 同名的欄位，語意相同）；
* backend 載入——宿主可**預載後注入**（多個 stage／多個 session 共享同一個模型），
  ``prepare()`` 不會重載已注入的 backend；沒注入才建立真的 echo_stt backend；
* 生命週期（``prepare`` / ``open`` / ``aclose``）。

輸入一律是 16kHz mono int16。Discord 的 48kHz stereo 由 Discord source adapter
轉換後再 ``feed``，這裡不做重採樣。
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator, Callable
from typing import Any

from . import config
from ..contracts.cancellation import CancellationToken
from ..contracts.turn import TurnDetector, TurnPolicy, VoiceEvent
from ..contracts.types import STT_SAMPLE_RATE, Utterance
from ..audio.multichannel import ChannelEngine
from ..audio.vad import EnergyVad
from .input_stt import TranscriberBackend, _EchoSttBackend


class MultiChannelSttInputStage:
    """多聲道 STT 輸入：``feed(speaker_id, pcm)`` 進、``open(token)`` 出 Utterance 流。

    ``on_voice_event(VoiceEvent)`` 每個 chunk 觸發（STT 之前），事件帶
    ``speaker_id`` / ``is_voiced`` / 牆鐘 ``timestamp``，在 loop 執行緒呼叫，必須快。
    ``on_partial(speaker_id, text, index)`` 啟用增量切分（None 則不切）。
    """

    name = "stt_multichannel_input"

    def __init__(
        self,
        *,
        backend: TranscriberBackend | None = None,
        stt_repo: str | None = None,
        policy: TurnPolicy | None = None,
        language: str | None = None,
        min_confidence: float | None = None,
        preroll_s: float = 0.2,
        on_voice_event: Callable[[VoiceEvent], None] | None = None,
        on_partial: Callable[[str, str, int], None] | None = None,
        partial_silence_s: float = 0.35,
        partial_min_speech_s: float = 2.0,
        max_pending_segments: int = 4,
        queue_size: int = 8,
        chunk_queue_max: int = 256,
        idle_evict_s: float = 60.0,
        max_channels: int = 32,
        max_concurrency: int = 1,
        job_timeout_s: float | None = None,
        job_timeout_per_audio_s: float = 0.0,
        tick_interval_s: float = 0.1,
        silence_fill_s: float = 0.1,
        clock: Callable[[], float] = time.perf_counter,
        vad_factory: Callable[[], EnergyVad] | None = None,
        detector_factory: Callable[[TurnPolicy], TurnDetector] | None = None,
    ) -> None:
        self._backend = backend
        self._owns_backend = backend is None
        self._stt_repo = stt_repo
        self.policy = policy or TurnPolicy()
        self.language = language or config.get("ECHO_STREAM_STT_LANGUAGE")
        self.min_confidence = (
            min_confidence
            if min_confidence is not None
            else config.get_float("ECHO_STREAM_STT_MIN_CONFIDENCE", 0.35)
        )
        self.preroll_s = preroll_s
        self.on_voice_event = on_voice_event
        self.on_partial = on_partial
        self.partial_silence_s = partial_silence_s
        self.partial_min_speech_s = partial_min_speech_s
        self._prepared = False
        self.engine = ChannelEngine(
            self,
            backend=backend,
            sample_rate=STT_SAMPLE_RATE,
            max_pending_segments=max_pending_segments,
            queue_size=queue_size,
            chunk_queue_max=chunk_queue_max,
            idle_evict_s=idle_evict_s,
            max_channels=max_channels,
            max_concurrency=max_concurrency,
            job_timeout_s=job_timeout_s,
            job_timeout_per_audio_s=job_timeout_per_audio_s,
            tick_interval_s=tick_interval_s,
            silence_fill_s=silence_fill_s,
            clock=clock,
            vad_factory=vad_factory,
            detector_factory=detector_factory,
        )

    # --- 生命週期 ---

    async def prepare(self) -> None:
        """載入模型（僅在沒有注入 backend 時）。已注入的 backend 不會被重載。"""
        if self._prepared:
            return
        if self._backend is None:
            loop = asyncio.get_running_loop()
            self._backend = await loop.run_in_executor(None, self._build_backend)
            self._owns_backend = True
        self.engine.backend = self._backend
        self._prepared = True

    def _build_backend(self) -> TranscriberBackend:
        """建立真的 echo_stt backend。**只有這裡 import echo_stt**（經 input_stt）。"""
        config.ensure_subsystem("STT", "echo_stt", self._stt_repo)
        return _EchoSttBackend(language=self.language)

    async def aclose(self) -> None:
        await self.engine.shutdown()
        backend = self._backend
        # 注入的 backend 屬於宿主（可能被別的 stage 共用），不在這裡關
        if self._owns_backend and backend is not None:
            close = getattr(backend, "close", None)
            if callable(close):
                loop = asyncio.get_running_loop()
                with contextlib.suppress(Exception):
                    await loop.run_in_executor(None, close)
            self._backend = None
        self._prepared = False

    # --- MultiChannelInputStage ---

    async def open(self, token: CancellationToken) -> AsyncIterator[Utterance]:
        await self.prepare()
        async for utterance in self.engine.open(token):
            yield utterance

    def feed(self, speaker_id: str, pcm: bytes, ts: float | None = None) -> None:
        self.engine.feed(speaker_id, pcm, ts)

    def join(self, speaker_id: str, **opts: Any) -> None:
        self.engine.join(speaker_id, **opts)

    def leave(self, speaker_id: str, flush: bool = True) -> None:
        self.engine.leave(speaker_id, flush)

    def discard(self, speaker_id: str | None = None) -> None:
        self.engine.discard(speaker_id)


__all__ = ["MultiChannelSttInputStage"]
