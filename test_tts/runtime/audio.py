from __future__ import annotations

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
