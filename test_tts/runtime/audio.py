from __future__ import annotations

import queue
import threading
from pathlib import Path
from typing import Protocol


class AudioPlayer(Protocol):
    def play(self, audio_path: Path) -> None:
        ...

    def play_tensor(self, audio_tensor, sample_rate: int = 22050) -> None:
        """Play audio directly from a tensor without writing to disk."""
        ...


class NullAudioPlayer:
    def play(self, audio_path: Path) -> None:
        return None

    def play_tensor(self, audio_tensor, sample_rate: int = 22050) -> None:
        return None


class SimpleAudioPlayer:
    """Blocking WAV playback using soundfile + simpleaudio.

    This mirrors the current U.E.P TTS module playback path closely enough for
    the standalone test project. Imports stay inside play() so light tests do
    not require audio dependencies.
    """

    def play(self, audio_path: Path) -> None:
        import numpy as np
        import soundfile as sf
        import simpleaudio as sa

        data, sample_rate = sf.read(audio_path)
        if data.ndim > 1:
            channels = data.shape[1]
        else:
            channels = 1
        data_int16 = (data * 32767).astype(np.int16)
        play_obj = sa.play_buffer(
            data_int16.tobytes(),
            num_channels=channels,
            bytes_per_sample=2,
            sample_rate=sample_rate,
        )
        play_obj.wait_done()

    def play_tensor(self, audio_tensor, sample_rate: int = 22050) -> None:
        """Play audio directly from a torch tensor (shape: [channels, samples])."""
        import numpy as np
        import simpleaudio as sa

        # Convert torch tensor to numpy
        if hasattr(audio_tensor, "numpy"):
            data = audio_tensor.numpy()
        else:
            data = audio_tensor

        # Handle shape: [channels, samples] -> [samples, channels] or just [samples]
        if data.ndim == 2:
            channels = data.shape[0]
            data = data.T  # [samples, channels]
        else:
            channels = 1

        data_int16 = (data * 32767).astype(np.int16)
        play_obj = sa.play_buffer(
            data_int16.tobytes(),
            num_channels=channels,
            bytes_per_sample=2,
            sample_rate=sample_rate,
        )
        play_obj.wait_done()


_SENTINEL = object()


class StreamingAudioPlayer:
    """Non-blocking Producer/Consumer audio player.

    Segments are enqueued from the synthesis thread and played back on a
    dedicated consumer thread.  The bounded queue (default ``maxsize=3``)
    provides back-pressure so the producer doesn't race too far ahead.

    Usage in ``session.say()``::

        player.start_stream()
        # ... engine calls on_segment_audio → player.enqueue_tensor(t)
        player.wait_stream()   # blocks until all enqueued segments are played
    """

    def __init__(self, maxsize: int = 3, sample_rate: int = 22050):
        self._default_sr = sample_rate
        self._queue: queue.Queue = queue.Queue(maxsize=maxsize)
        self._thread: threading.Thread | None = None
        self._error: BaseException | None = None
        self._segments_played = 0

    # -- public API -----------------------------------------------------------

    def start_stream(self) -> None:
        """Start the consumer thread.  Call once before enqueuing segments."""
        self._error = None
        self._segments_played = 0
        # Drain any leftover items from a previous run
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
        self._thread = threading.Thread(target=self._consumer_loop, daemon=True)
        self._thread.start()

    def enqueue_tensor(self, audio_tensor, sample_rate: int | None = None) -> None:
        """Push a segment into the playback queue (may block if queue is full)."""
        sr = sample_rate or self._default_sr
        self._queue.put((audio_tensor, sr))

    def wait_stream(self) -> int:
        """Block until all enqueued segments have been played.

        Returns the number of segments played.  Raises if the consumer hit an
        error.
        """
        self._queue.put(_SENTINEL)
        if self._thread is not None:
            self._thread.join()
            self._thread = None
        if self._error is not None:
            raise self._error
        return self._segments_played

    @property
    def segments_played(self) -> int:
        return self._segments_played

    # -- fallback for non-streaming paths ------------------------------------

    def play(self, audio_path: Path) -> None:
        """Blocking file playback (fallback, delegates to SimpleAudioPlayer)."""
        SimpleAudioPlayer().play(audio_path)

    def play_tensor(self, audio_tensor, sample_rate: int = 22050) -> None:
        """Blocking tensor playback (fallback when not in streaming mode)."""
        SimpleAudioPlayer().play_tensor(audio_tensor, sample_rate)

    # -- internals ------------------------------------------------------------

    def _consumer_loop(self) -> None:
        import time
        import numpy as np
        import simpleaudio as sa

        try:
            while True:
                item = self._queue.get()
                if item is _SENTINEL:
                    break

                audio_tensor, sample_rate = item

                # Convert tensor → int16 bytes
                if hasattr(audio_tensor, "numpy"):
                    data = audio_tensor.numpy()
                else:
                    data = audio_tensor

                if data.ndim == 2:
                    channels = data.shape[0]
                    data = data.T
                else:
                    channels = 1

                data_int16 = (data * 32767).astype(np.int16)
                play_obj = sa.play_buffer(
                    data_int16.tobytes(),
                    num_channels=channels,
                    bytes_per_sample=2,
                    sample_rate=sample_rate,
                )
                play_obj.wait_done()
                # Small buffer after each segment to prevent audio driver
                # from cutting off the tail (simpleaudio sometimes returns
                # from wait_done slightly before playback fully completes)
                time.sleep(0.05)
                self._segments_played += 1

        except Exception as exc:
            self._error = exc
