"""實用的 AudioSource 實作。

與 :mod:`uep.stream.io.sinks` 同理放頂層——不依賴任何子系統。
``MicrophoneSource`` 需要 sounddevice，但採 lazy import：
沒裝也不影響其他部分（測試環境、Web 前端都用不到麥克風）。
"""

from __future__ import annotations

import asyncio
import contextlib
import wave
from collections.abc import AsyncIterator
from pathlib import Path

from ..contracts.cancellation import CancellationToken
from ..contracts.types import STT_SAMPLE_RATE


class MicrophoneSource:
    """本地麥克風，產出 16kHz mono int16 PCM。

    ## 音訊 callback 絕不能阻塞

    PortAudio 的 callback 在音訊執行緒上跑，阻塞會直接掉音框。
    所以這裡**不用** ``put_threadsafe``（滿了會阻塞），而是
    ``call_soon_threadsafe`` + 滿了丟最舊——消費端追不上時，
    丟掉舊音訊比堵住麥克風正確：對話場景裡遲到的音訊沒有價值。
    """

    def __init__(
        self,
        device: int | str | None = None,
        sample_rate: int = STT_SAMPLE_RATE,
        chunk_ms: int = 50,
        queue_chunks: int = 64,
    ) -> None:
        self.device = device
        self._sample_rate = sample_rate
        self.chunk_ms = chunk_ms
        self.queue_chunks = queue_chunks
        """佇列容量（≈ chunk_ms × queue_chunks 的音訊）。64×50ms = 3.2s。"""

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    async def stream(self, token: CancellationToken) -> AsyncIterator[bytes]:
        import sounddevice as sd  # lazy：沒裝 sounddevice 的環境照常運作

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=self.queue_chunks)

        def enqueue(data: bytes) -> None:
            if queue.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()  # 丟最舊，別堵麥克風
            queue.put_nowait(data)

        def callback(indata, frames, time_info, status) -> None:  # noqa: ANN001, ARG001
            # 音訊執行緒：只做一次記憶體拷貝 + 排程，其他都不做
            loop.call_soon_threadsafe(enqueue, bytes(indata))

        blocksize = int(self._sample_rate * self.chunk_ms / 1000)
        raw_stream = sd.RawInputStream(
            samplerate=self._sample_rate,
            blocksize=blocksize,
            dtype="int16",
            channels=1,
            device=self.device,
            callback=callback,
        )
        with raw_stream:
            while not token.is_cancelled:
                get_task = asyncio.ensure_future(queue.get())
                cancel_task = asyncio.ensure_future(token.wait())
                try:
                    done, _ = await asyncio.wait(
                        {get_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED
                    )
                finally:
                    for task in (get_task, cancel_task):
                        if not task.done():
                            task.cancel()
                if get_task not in done:
                    return
                yield get_task.result()


class WavFileSource:
    """從 WAV 檔產出 PCM 流。整鏈路測試用（麥克風之前的可重現輸入）。

    ``realtime=True`` 依音訊時長節流——測 turn 判定時必須開，
    否則整個檔案瞬間灌完，靜音時長全部歸零，一個 turn 都切不出來。
    """

    def __init__(
        self,
        path: str | Path,
        chunk_ms: int = 50,
        realtime: bool = False,
    ) -> None:
        self.path = Path(path)
        self.chunk_ms = chunk_ms
        self.realtime = realtime
        with wave.open(str(self.path), "rb") as w:
            if w.getnchannels() != 1 or w.getsampwidth() != 2:
                raise ValueError(
                    f"{self.path} 必須是 mono int16 WAV（重採樣屬於 Frontend Adapter）"
                )
            self._sample_rate = w.getframerate()

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    async def stream(self, token: CancellationToken) -> AsyncIterator[bytes]:
        frames_per_chunk = int(self._sample_rate * self.chunk_ms / 1000)
        with wave.open(str(self.path), "rb") as w:
            while not token.is_cancelled:
                data = w.readframes(frames_per_chunk)
                if not data:
                    return
                yield data
                if self.realtime:
                    await asyncio.sleep(len(data) / 2 / self._sample_rate)


__all__ = ["MicrophoneSource", "WavFileSource"]
