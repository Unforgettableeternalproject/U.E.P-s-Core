from __future__ import annotations

from dataclasses import dataclass
from statistics import mean
from typing import Iterable


@dataclass(frozen=True)
class LatencySample:
    """One measured TTS synthesis sample.

    Durations are stored in milliseconds. `audio_duration_ms` is the generated
    waveform length, not wall-clock synthesis time.
    """

    text_chars: int
    audio_duration_ms: float
    gpt_ms: float
    s2mel_ms: float
    vocoder_ms: float
    first_audio_ms: float

    @property
    def total_ms(self) -> float:
        return self.gpt_ms + self.s2mel_ms + self.vocoder_ms

    @property
    def rtf(self) -> float:
        if self.audio_duration_ms <= 0:
            return 0.0
        return self.total_ms / self.audio_duration_ms


def summarize_latency(samples: Iterable[LatencySample]) -> dict[str, float | int]:
    collected = list(samples)
    if not collected:
        return {
            "sample_count": 0,
            "avg_total_ms": 0.0,
            "avg_first_audio_ms": 0.0,
            "avg_rtf": 0.0,
        }

    return {
        "sample_count": len(collected),
        "avg_total_ms": mean(sample.total_ms for sample in collected),
        "avg_first_audio_ms": mean(sample.first_audio_ms for sample in collected),
        "avg_rtf": mean(sample.rtf for sample in collected),
    }
