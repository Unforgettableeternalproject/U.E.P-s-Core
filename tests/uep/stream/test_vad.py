"""EnergyVad 的狀態機測試。

音訊全部合成（正弦波 = 語音、零 = 靜音），時間軸靠樣本數推進，
不依賴牆鐘——跑多快都不影響判定。
"""

from __future__ import annotations

import math
import struct

import pytest

from uep.stream.contracts.turn import VoiceState
from uep.stream.audio.vad import EnergyVad

SR = 16_000


def tone(seconds: float, amplitude: float = 0.3, freq: float = 220.0) -> bytes:
    n = int(SR * seconds)
    return struct.pack(
        f"<{n}h",
        *(
            int(amplitude * 32767 * math.sin(2 * math.pi * freq * i / SR))
            for i in range(n)
        ),
    )


def silence(seconds: float) -> bytes:
    return b"\x00\x00" * int(SR * seconds)


def chunks(pcm: bytes, chunk_ms: int = 50):
    step = int(SR * chunk_ms / 1000) * 2
    for i in range(0, len(pcm), step):
        yield pcm[i : i + step]


def feed(vad: EnergyVad, pcm: bytes, chunk_ms: int = 50):
    events = []
    for chunk in chunks(pcm, chunk_ms):
        events.append(vad.process(chunk))
    return events


class TestEnergyVad:
    def test_初始為靜音(self) -> None:
        vad = EnergyVad(sample_rate=SR)
        events = feed(vad, silence(0.3))
        assert all(e.state is VoiceState.SILENCE for e in events)

    def test_語音進入與時長累積(self) -> None:
        vad = EnergyVad(sample_rate=SR)
        events = feed(vad, tone(0.5))
        assert events[-1].state is VoiceState.SPEECH
        # 時長從進入 SPEECH 起累積，量級要對（不追求逐 chunk 精確）
        assert events[-1].duration_s == pytest.approx(0.5, abs=0.06)

    def test_hangover_內的短停頓不切回靜音(self) -> None:
        vad = EnergyVad(sample_rate=SR, hangover_s=0.15)
        feed(vad, tone(0.4))
        events = feed(vad, silence(0.10))  # 低於 hangover
        assert events[-1].state is VoiceState.SPEECH

    def test_超過_hangover_切回靜音且時長回溯(self) -> None:
        vad = EnergyVad(sample_rate=SR, hangover_s=0.15)
        feed(vad, tone(0.4))
        events = feed(vad, silence(0.5))
        assert events[-1].state is VoiceState.SILENCE
        # 靜音時長從能量掉下去那一刻起算（不含 hangover 的折損）
        assert events[-1].duration_s == pytest.approx(0.5, abs=0.06)

    def test_reset_不歸零時鐘(self) -> None:
        vad = EnergyVad(sample_rate=SR)
        feed(vad, tone(0.3))
        clock = vad.clock_s
        vad.reset()
        assert vad.state is VoiceState.SILENCE
        assert vad.clock_s == clock

    def test_能量在遲滯帶內不離開語音(self) -> None:
        vad = EnergyVad(sample_rate=SR, threshold=0.015, hysteresis=0.6, hangover_s=0.1)
        feed(vad, tone(0.3, amplitude=0.3))
        # 能量掉到進入閾值之下、離開閾值之上：仍是 SPEECH
        # （正弦波 RMS = A/√2：0.014 → RMS ≈ 0.0099，介於 0.009 與 0.015）
        events = feed(vad, tone(0.5, amplitude=0.014))
        assert events[-1].state is VoiceState.SPEECH
