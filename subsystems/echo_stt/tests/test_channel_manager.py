"""
通道管理測試

使用 FileChannel（不需要真實麥克風）測試：
- AudioChannel 基本行為（push_audio → VAD → segment_callback）
- FileChannel 背景推送
- ChannelManager 生命週期、多通道、overlapping detection
"""

from __future__ import annotations

import threading
import time

import numpy as np
import pytest

from echo_stt.core.multi_channel_schemas import (
    AudioSegment,
    ChannelState,
    ChannelType,
)
from echo_stt.channel.base import FileChannel
from echo_stt.channel.manager import ChannelManager
from echo_stt.vad.advanced_vad import AdvancedVAD


# ──────────────────────────────────────────────────────────────
#  Helpers
# ──────────────────────────────────────────────────────────────


def make_speech_audio(duration_s: float = 1.0, freq: float = 440, sample_rate: int = 16000) -> np.ndarray:
    """產生模擬語音的正弦波（能量足夠觸發 energy VAD）"""
    t = np.linspace(0, duration_s, int(duration_s * sample_rate), endpoint=False)
    return (np.sin(2 * np.pi * freq * t) * 0.5 * 32768).astype(np.int16)


def make_silence(duration_s: float = 1.0, sample_rate: int = 16000) -> np.ndarray:
    return np.zeros(int(duration_s * sample_rate), dtype=np.int16)


def make_speech_then_silence(
    speech_s: float = 0.5,
    silence_s: float = 1.0,
    freq: float = 440,
) -> np.ndarray:
    """語音 + 靜音（會觸發 VAD emit）"""
    return np.concatenate([
        make_speech_audio(speech_s, freq),
        make_silence(silence_s),
    ])


def create_vad(use_silero: bool = False, **kwargs) -> AdvancedVAD:
    """建立測試用 VAD（預設不用 Silero，避免依賴）"""
    defaults = dict(
        use_silero=use_silero,
        sensitivity=0.3,
        trailing_silence_ms=300,
        max_segment_ms=10000,
        min_speech_ms=100,
        adaptive_threshold=False,
    )
    defaults.update(kwargs)
    vad = AdvancedVAD(**defaults)
    vad.initialize()
    return vad


# ──────────────────────────────────────────────────────────────
#  FileChannel 測試
# ──────────────────────────────────────────────────────────────


class TestFileChannel:

    def test_produces_segments(self):
        """FileChannel 推送語音+靜音後應產生 AudioSegment"""
        audio = make_speech_then_silence(speech_s=0.5, silence_s=1.0)
        segments = []

        ch = FileChannel(
            channel_id="test_file",
            audio_data=audio,
            vad=create_vad(),
            segment_callback=lambda seg: segments.append(seg),
            chunk_duration_ms=50,
        )
        ch.start()
        ch.wait_for_completion(timeout=5.0)
        ch.stop()

        assert len(segments) >= 1
        seg = segments[0]
        assert isinstance(seg, AudioSegment)
        assert seg.channel_id == "test_file"
        assert seg.channel_type == ChannelType.FILE
        assert seg.num_samples > 0

    def test_speaker_label_passthrough(self):
        """speaker_label 和 speaker_name 應傳遞到 AudioSegment"""
        audio = make_speech_then_silence()
        segments = []

        ch = FileChannel(
            channel_id="test",
            audio_data=audio,
            vad=create_vad(),
            segment_callback=lambda seg: segments.append(seg),
            speaker_label="spk_alice",
            speaker_name="Alice",
        )
        ch.start()
        ch.wait_for_completion(timeout=5.0)
        ch.stop()

        assert len(segments) >= 1
        assert segments[0].speaker_label == "spk_alice"
        assert segments[0].speaker_name == "Alice"

    def test_silence_only_no_segments(self):
        """純靜音不應產生 segment"""
        audio = make_silence(1.0)
        segments = []

        ch = FileChannel(
            channel_id="silent",
            audio_data=audio,
            vad=create_vad(),
            segment_callback=lambda seg: segments.append(seg),
        )
        ch.start()
        ch.stop()

        assert len(segments) == 0

    def test_state_transitions(self):
        """狀態應從 IDLE → LISTENING → ... → STOPPED"""
        audio = make_speech_then_silence(speech_s=0.3, silence_s=0.5)

        ch = FileChannel(
            channel_id="state_test",
            audio_data=audio,
            vad=create_vad(),
            segment_callback=lambda seg: None,
        )
        assert ch.state == ChannelState.IDLE

        ch.start()
        # 等一下讓背景執行緒跑
        time.sleep(0.1)
        assert ch.is_active or ch.state == ChannelState.STOPPED  # 短音檔可能已跑完

        ch.stop()
        assert ch.state == ChannelState.STOPPED

    def test_get_status(self):
        """get_status 回傳合理的 ChannelStatus"""
        audio = make_speech_then_silence()
        ch = FileChannel(
            channel_id="status_test",
            audio_data=audio,
            vad=create_vad(),
            segment_callback=lambda seg: None,
        )

        status = ch.get_status()
        assert status.channel_id == "status_test"
        assert status.channel_type == ChannelType.FILE
        assert status.state == ChannelState.IDLE

    def test_multiple_speech_segments(self):
        """多段語音+靜音應產生多個 segment"""
        audio = np.concatenate([
            make_speech_then_silence(speech_s=0.4, silence_s=0.8, freq=440),
            make_speech_then_silence(speech_s=0.4, silence_s=0.8, freq=880),
        ])
        segments = []

        ch = FileChannel(
            channel_id="multi_seg",
            audio_data=audio,
            vad=create_vad(),
            segment_callback=lambda seg: segments.append(seg),
        )
        ch.start()
        ch.wait_for_completion(timeout=5.0)
        ch.stop()

        assert len(segments) >= 2


# ──────────────────────────────────────────────────────────────
#  ChannelManager 測試
# ──────────────────────────────────────────────────────────────


class TestChannelManager:

    def test_add_file_channel(self):
        audio = make_speech_then_silence()
        mgr = ChannelManager(segment_callback=lambda seg: None)

        cid = mgr.add_file_channel("ch_0", audio)
        assert cid == "ch_0"
        assert mgr.channel_count == 1

    def test_add_duplicate_raises(self):
        audio = make_silence(0.5)
        mgr = ChannelManager(segment_callback=lambda seg: None)
        mgr.add_file_channel("ch_0", audio)

        with pytest.raises(ValueError, match="already exists"):
            mgr.add_file_channel("ch_0", audio)

    def test_max_channels_enforced(self):
        mgr = ChannelManager(segment_callback=lambda seg: None, max_channels=2)
        mgr.add_file_channel("ch_0", make_silence(0.1))
        mgr.add_file_channel("ch_1", make_silence(0.1))

        with pytest.raises(RuntimeError, match="Max channels"):
            mgr.add_file_channel("ch_2", make_silence(0.1))

    def test_remove_channel(self):
        mgr = ChannelManager(segment_callback=lambda seg: None)
        mgr.add_file_channel("ch_0", make_silence(0.1))

        assert mgr.remove_channel("ch_0") is True
        assert mgr.channel_count == 0
        assert mgr.remove_channel("ch_0") is False  # 已刪除

    def test_start_stop_all(self):
        segments = []
        mgr = ChannelManager(
            segment_callback=lambda seg: segments.append(seg),
            vad_config={"use_silero": False, "sensitivity": 0.3,
                        "trailing_silence_ms": 300, "min_speech_ms": 100,
                        "adaptive_threshold": False},
        )

        mgr.add_file_channel("ch_A", make_speech_then_silence(0.5, 1.0, freq=440),
                             speaker_name="Alice")
        mgr.add_file_channel("ch_B", make_speech_then_silence(0.5, 1.0, freq=880),
                             speaker_name="Bob")

        mgr.start_all()
        time.sleep(0.5)  # 給背景執行緒時間處理
        mgr.stop_all()

        # 兩個通道都應產生 segment
        assert len(segments) >= 2
        channel_ids = {seg.channel_id for seg in segments}
        assert "ch_A" in channel_ids
        assert "ch_B" in channel_ids

    def test_segments_from_different_channels(self):
        """不同通道的 segment 應帶各自的 channel_id 和 speaker"""
        segments = []
        mgr = ChannelManager(
            segment_callback=lambda seg: segments.append(seg),
            vad_config={"use_silero": False, "sensitivity": 0.3,
                        "trailing_silence_ms": 300, "min_speech_ms": 100,
                        "adaptive_threshold": False},
        )

        mgr.add_file_channel("alice", make_speech_then_silence(0.5, 1.0),
                             speaker_label="spk_A", speaker_name="Alice")
        mgr.add_file_channel("bob", make_speech_then_silence(0.5, 1.0),
                             speaker_label="spk_B", speaker_name="Bob")

        mgr.start_all()
        time.sleep(0.5)
        mgr.stop_all()

        alice_segs = [s for s in segments if s.channel_id == "alice"]
        bob_segs = [s for s in segments if s.channel_id == "bob"]

        assert len(alice_segs) >= 1
        assert len(bob_segs) >= 1
        assert alice_segs[0].speaker_label == "spk_A"
        assert bob_segs[0].speaker_label == "spk_B"

    def test_no_state_leakage(self):
        """一個通道的 VAD 狀態不應影響另一個"""
        segments = []
        mgr = ChannelManager(
            segment_callback=lambda seg: segments.append(seg),
            vad_config={"use_silero": False, "sensitivity": 0.3,
                        "trailing_silence_ms": 300, "min_speech_ms": 100,
                        "adaptive_threshold": False},
        )

        # ch_A 有語音，ch_B 純靜音
        mgr.add_file_channel("ch_A", make_speech_then_silence(0.5, 1.0))
        mgr.add_file_channel("ch_B", make_silence(2.0))

        mgr.start_all()
        time.sleep(0.5)
        mgr.stop_all()

        # 只有 ch_A 應該產生 segment
        ch_ids = {s.channel_id for s in segments}
        assert "ch_A" in ch_ids
        assert "ch_B" not in ch_ids

    def test_get_all_status(self):
        mgr = ChannelManager(segment_callback=lambda seg: None)
        mgr.add_file_channel("ch_0", make_silence(0.1))
        mgr.add_file_channel("ch_1", make_silence(0.1))

        status = mgr.get_all_status()
        assert len(status) == 2
        assert "ch_0" in status
        assert "ch_1" in status

    def test_get_active_channels(self):
        mgr = ChannelManager(
            segment_callback=lambda seg: None,
            vad_config={"use_silero": False},
        )
        mgr.add_file_channel("ch_0", make_silence(2.0))

        assert mgr.get_active_channels() == []
        mgr.start_all()
        time.sleep(0.1)
        # FileChannel 可能已跑完短音檔
        mgr.stop_all()
        assert mgr.get_active_channels() == []

    def test_total_segments_counter(self):
        segments = []
        mgr = ChannelManager(
            segment_callback=lambda seg: segments.append(seg),
            vad_config={"use_silero": False, "sensitivity": 0.3,
                        "trailing_silence_ms": 300, "min_speech_ms": 100,
                        "adaptive_threshold": False},
        )

        mgr.add_file_channel("ch_0", make_speech_then_silence(0.5, 1.0))
        mgr.start_all()
        time.sleep(0.5)
        mgr.stop_all()

        assert mgr.total_segments == len(segments)
        assert mgr.total_segments >= 1

    def test_shutdown_clears_everything(self):
        mgr = ChannelManager(segment_callback=lambda seg: None)
        mgr.add_file_channel("ch_0", make_silence(0.1))
        mgr.add_file_channel("ch_1", make_silence(0.1))

        mgr.shutdown()
        assert mgr.channel_count == 0
