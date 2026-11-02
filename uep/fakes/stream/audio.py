"""Fake 音訊來源與輸出。

零第三方依賴（math + array），所以 Phase 0 不需要建環境、不需要 GPU。
"""

from __future__ import annotations

import array
import asyncio
import math
import time
from collections.abc import AsyncIterator

from uep.stream.contracts.cancellation import CancellationToken
from uep.stream.contracts.output import PlayedEvidence
from uep.stream.contracts.types import STT_SAMPLE_RATE, TTS_SAMPLE_RATE, AudioChunk


def sine_pcm(
    duration_s: float,
    sample_rate: int = TTS_SAMPLE_RATE,
    freq: float = 220.0,
    amplitude: float = 0.25,
    fade_s: float = 0.01,
) -> bytes:
    """產生 int16 單聲道正弦波。

    頭尾各做一小段淡入淡出——即使是 fake 音訊也要避免爆音，因為 Phase 1
    接真 TTS 後段落拼接會用同樣的處理（weighted overlap-add / Hann 窗），
    先在 fake 階段就把介面與聽感基準對齊，換真模組時才好比較。
    """
    total = max(0, int(duration_s * sample_rate))
    fade = min(int(fade_s * sample_rate), total // 2)
    buf = array.array("h")
    for n in range(total):
        gain = 1.0
        if fade > 0:
            if n < fade:
                gain = 0.5 - 0.5 * math.cos(math.pi * n / fade)
            elif n >= total - fade:
                k = total - 1 - n
                gain = 0.5 - 0.5 * math.cos(math.pi * k / fade)
        value = amplitude * gain * math.sin(2.0 * math.pi * freq * n / sample_rate)
        buf.append(int(max(-1.0, min(1.0, value)) * 32767))
    return buf.tobytes()


class FakeAudioSource:
    """假的音訊來源。實際上不產生有意義的音訊——

    Phase 0 的 FakeInputStage 根本不做轉錄（它直接吐預設文字），
    這個 source 只是為了讓契約完整、讓 Phase 3 換真 STT 時介面不變。
    """

    def __init__(self, sample_rate: int = STT_SAMPLE_RATE, chunk_ms: int = 20) -> None:
        self._sample_rate = sample_rate
        self._chunk_ms = chunk_ms

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    async def stream(self, token: CancellationToken) -> AsyncIterator[bytes]:
        frames = int(self._sample_rate * self._chunk_ms / 1000)
        silence = bytes(frames * 2)
        while not token.is_cancelled:
            await asyncio.sleep(self._chunk_ms / 1000)
            yield silence


class FakeAudioSink:
    """模擬即時播放的 sink。

    **重點在 :attr:`played_duration_s`**：它按真實時間推進，不是按寫入量。
    這樣 barge-in 善後的推算（「使用者實際聽到了什麼」）才有東西可驗證——
    寫進 sink 的音訊有一部分還在緩衝區排隊，被中止時那部分使用者從沒聽到。

    沒有這個模擬，Phase 0 就驗不出 :meth:`PipelineRunner._resolve_spoken`
    的正確性，而那正是 barge-in 最容易寫錯的地方。
    """

    def __init__(self, realtime: bool = True) -> None:
        self.realtime = realtime
        self.chunks: list[AudioChunk] = []
        self.written_duration_s = 0.0
        self._start_at: float | None = None
        self._stopped = False
        self._frozen_played: float | None = None

    async def write(self, chunk: AudioChunk) -> None:
        if self._stopped:
            return
        if self._start_at is None:
            self._start_at = time.perf_counter()
        self.chunks.append(chunk)
        self.written_duration_s += chunk.duration_s

    def stop(self) -> None:
        """立刻停止並丟棄緩衝。同步、冪等——barge-in 等不了 event loop。"""
        if self._stopped:
            return
        self._frozen_played = self._compute_played()
        self._stopped = True

    async def drain(self) -> None:
        """等剩餘緩衝播完。"""
        if self._stopped or self._start_at is None:
            return
        remaining = self.written_duration_s - self._compute_played()
        if remaining > 0:
            await asyncio.sleep(remaining)

    @property
    def played_duration_s(self) -> float:
        if self._frozen_played is not None:
            return self._frozen_played
        return self._compute_played()

    def _compute_played(self) -> float:
        if self._start_at is None:
            return 0.0
        if not self.realtime:
            return self.written_duration_s
        elapsed = time.perf_counter() - self._start_at
        return min(self.written_duration_s, elapsed)

    # --- 選用能力（見 contracts.output.OptionalSinkCapabilities）---

    played_evidence = PlayedEvidence.PLAYBACK_CONFIRMED
    """fake 以真實時間模擬播放游標，視同播放確認。"""

    @property
    def first_frame_at(self) -> float | None:
        """第一個 chunk 寫入的時刻；尚未寫入為 None。"""
        return self._start_at

    @property
    def is_active(self) -> bool:
        """已開始出聲、未被 stop、且緩衝尚未播完。"""
        if self._stopped or self._start_at is None:
            return False
        return self._compute_played() < self.written_duration_s

    async def confirm_stopped(self) -> None:
        """fake 的 stop 當下就確定播放量，無需等待。"""
        return

    def reset(self) -> None:
        self.chunks.clear()
        self.written_duration_s = 0.0
        self._start_at = None
        self._stopped = False
        self._frozen_played = None
