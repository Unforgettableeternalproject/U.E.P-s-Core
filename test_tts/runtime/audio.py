from __future__ import annotations

from pathlib import Path
from typing import Protocol


class AudioPlayer(Protocol):
    def play(self, audio_path: Path) -> None:
        ...


class NullAudioPlayer:
    def play(self, audio_path: Path) -> None:
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
