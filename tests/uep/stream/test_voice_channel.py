"""VoiceChannel 的短噪音碎片處理測試。

turn 偵測器與聲道對「太短」必須同一個定義，否則零碎 burst 會卡住 turn。
"""

from __future__ import annotations

import struct
from types import SimpleNamespace

from uep.stream.contracts.turn import TurnPolicy
from uep.stream.audio.channel_engine import VoiceChannel
from uep.stream.turn_detector import SilenceTurnDetector
from uep.stream.audio.vad import EnergyVad

CHUNK_S = 0.02
SAMPLE_RATE = 16000


def _chunk(loud: bool) -> bytes:
    n = int(SAMPLE_RATE * CHUNK_S)
    return struct.pack(f"<{n}h", *([3000 if loud else 0] * n))


def _make_channel() -> VoiceChannel:
    policy = TurnPolicy(silence_threshold_s=0.8, max_wait_s=12, min_utterance_s=0.3)
    cfg = SimpleNamespace(
        policy=policy,
        preroll_s=0.2,
        partial_silence_s=0.35,
        partial_min_speech_s=2.0,
        on_partial=None,
        on_voice_event=None,
    )
    return VoiceChannel(EnergyVad(), SilenceTurnDetector(policy), cfg, "x")


class _Feeder:
    def __init__(self, channel: VoiceChannel) -> None:
        self.channel = channel
        self.now = 0.0
        self.segments: list = []

    def feed(self, loud: bool, seconds: float) -> None:
        for _ in range(round(seconds / CHUNK_S)):
            self.now += CHUNK_S
            self.segments += self.channel.push(_chunk(loud), self.now)


def test_多個短_burst_加長靜音_turn_不卡住() -> None:
    """兩個各 <0.3s、總和 >=0.3s 的 burst：偵測器判 too_short，聲道也要丟掉。"""
    channel = _make_channel()
    feeder = _Feeder(channel)
    feeder.feed(True, 0.04)
    feeder.feed(False, 0.3)
    feeder.feed(True, 0.04)
    feeder.feed(False, 5.0)

    assert feeder.segments == []
    assert not channel.active
    assert len(channel._buffer) == 0
    assert channel._speech_start_clock is None
    assert channel._partial_mark == 0
    assert channel.turn_detector._speech_accum_s == 0.0


def test_噪音碎片被丟棄後_真語音的時間標記不含噪音() -> None:
    channel = _make_channel()
    feeder = _Feeder(channel)
    feeder.feed(True, 0.04)
    feeder.feed(False, 0.3)
    feeder.feed(True, 0.04)
    feeder.feed(False, 280.0)
    speech_start = feeder.now
    feeder.feed(True, 2.0)
    feeder.feed(False, 2.0)

    assert len(feeder.segments) == 1
    seg = feeder.segments[0]
    assert seg.final
    pcm_s = len(seg.pcm) / 2 / SAMPLE_RATE
    assert 2.0 <= pcm_s <= 2.6, "只含真語音（加 preroll／hangover），不含噪音"
    assert abs(seg.started_at - speech_start) < 0.5, "起點對應真語音而非 280 秒前的噪音"
    assert seg.ended_at - seg.started_at < 3.0


def test_單一夠長的_burst_照常成_turn() -> None:
    channel = _make_channel()
    feeder = _Feeder(channel)
    feeder.feed(True, 1.0)
    feeder.feed(False, 2.0)

    assert len(feeder.segments) == 1
    assert feeder.segments[0].final


def test_長語音後接短語氣詞仍結束_turn() -> None:
    """結尾的短 burst 不能讓前面的長語音被當成太短而卡住或遺失。"""
    channel = _make_channel()
    feeder = _Feeder(channel)
    feeder.feed(True, 1.5)
    feeder.feed(False, 0.3)
    feeder.feed(True, 0.04)
    feeder.feed(False, 2.0)

    assert len(feeder.segments) == 1
    assert len(feeder.segments[0].pcm) / 2 / SAMPLE_RATE >= 1.5


def test_decode_stats_跨段取最差值且略過缺值():
    from types import SimpleNamespace as NS

    from uep.stream.audio.channel_engine import decode_stats

    parts = [
        NS(temperature=0.0, avg_logprob=-0.2, compression_ratio=1.1),
        NS(temperature=0.6, avg_logprob=-0.9, compression_ratio=None),
        NS(),  # 舊 backend：完全沒有這些欄位
    ]
    assert decode_stats(parts) == {
        "temperature": 0.6, "avg_logprob": -0.9, "compression_ratio": 1.1,
    }
    assert decode_stats([NS(text="x")]) == {}
