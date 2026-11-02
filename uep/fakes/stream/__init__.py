"""Fake 實作——Phase 0 的骨架驗證用，零第三方依賴、不需要 GPU。"""

from .audio import FakeAudioSink, FakeAudioSource, sine_pcm
from .stages import (
    SECONDS_PER_WEIGHT,
    FakeInputStage,
    FakeSpeakStage,
    FakeThinkStage,
)

__all__ = [
    "FakeAudioSink",
    "FakeAudioSource",
    "sine_pcm",
    "FakeInputStage",
    "FakeSpeakStage",
    "FakeThinkStage",
    "SECONDS_PER_WEIGHT",
]
