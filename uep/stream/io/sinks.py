"""實用的 AudioSink 實作。

放在頂層而不是 `adapters/`，因為這些**不依賴任何子系統**——
只用標準庫的 `wave`。
"""

from __future__ import annotations

import asyncio
import time
import wave
from pathlib import Path

from ..contracts.types import AudioChunk


class WavFileSink:
    """把音訊塊累積起來寫成單一 WAV 檔。

    ``realtime=True`` 會依音訊時長模擬播放進度。看似多餘（寫檔哪需要等），
    但 barge-in 善後靠 ``played_duration_s`` 推算「使用者實際聽到什麼」——
    不模擬的話，插話測試會誤判成整段都播出去了。
    """

    def __init__(
        self,
        path: str | Path,
        sample_rate: int | None = None,
        channels: int = 1,
        realtime: bool = False,
    ) -> None:
        self.path = Path(path)
        self.sample_rate = sample_rate
        self.channels = channels
        self.realtime = realtime
        self._buffers: list[bytes] = []
        self.written_duration_s = 0.0
        self._start_at: float | None = None
        self._stopped = False
        self._frozen_played: float | None = None

    async def write(self, chunk: AudioChunk) -> None:
        if self._stopped:
            return
        if self.sample_rate is None:
            self.sample_rate = chunk.sample_rate
        if self._start_at is None:
            self._start_at = time.perf_counter()
        self._buffers.append(chunk.pcm)
        self.written_duration_s += chunk.duration_s

    def stop(self) -> None:
        if self._stopped:
            return
        self._frozen_played = self._compute_played()
        self._stopped = True

    async def drain(self) -> None:
        if self._stopped or self._start_at is None or not self.realtime:
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
        return min(self.written_duration_s, time.perf_counter() - self._start_at)

    def flush(self) -> Path | None:
        """把累積的音訊寫成 WAV 檔。沒有音訊時回傳 None。

        只寫**實際播出**的部分——被 barge-in 中止時，緩衝裡沒播到的
        不該出現在檔案裡，否則聽檔案跟聽現場會是兩回事。
        """
        if not self._buffers or self.sample_rate is None:
            return None

        pcm = b"".join(self._buffers)
        if self._frozen_played is not None:
            frame_bytes = 2 * self.channels
            keep = int(self._frozen_played * self.sample_rate) * frame_bytes
            pcm = pcm[: max(0, keep)]
        if not pcm:
            return None

        self.path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(self.path), "wb") as w:
            w.setnchannels(self.channels)
            w.setsampwidth(2)
            w.setframerate(self.sample_rate)
            w.writeframes(pcm)
        return self.path


class NullSink:
    """丟棄所有音訊。純量測延遲時用——排除播放本身的影響。"""

    def __init__(self) -> None:
        self.written_duration_s = 0.0

    async def write(self, chunk: AudioChunk) -> None:
        self.written_duration_s += chunk.duration_s

    def stop(self) -> None:
        return

    async def drain(self) -> None:
        return

    @property
    def played_duration_s(self) -> float:
        return self.written_duration_s


__all__ = ["WavFileSink", "NullSink"]
