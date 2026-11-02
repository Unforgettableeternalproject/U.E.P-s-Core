"""
多通道 STT schemas 測試

覆蓋：
- AudioSegment: 建立、序列化、numpy 轉換、float32 轉換
- TranscriptEntry: 建立、duration 計算
- TranscriptSession: 排序、格式化輸出、SRT、查詢方法
- ChannelStatus / QueueMetrics / EngineMetrics: 基本建立
"""

import json
import time

import numpy as np
import pytest

from echo_stt.core.multi_channel_schemas import (
    AudioSegment,
    ChannelState,
    ChannelStatus,
    ChannelType,
    EngineMetrics,
    QueueMetrics,
    SegmentPriority,
    TranscriptEntry,
    TranscriptSession,
)


# ──────────────────────────────────────────────────────────────
#  Fixtures
# ──────────────────────────────────────────────────────────────


@pytest.fixture
def sample_audio_int16():
    """1 秒 16kHz 正弦波 (440Hz)，int16 格式"""
    sr = 16000
    t = np.linspace(0, 1.0, sr, endpoint=False)
    audio = (np.sin(2 * np.pi * 440 * t) * 16000).astype(np.int16)
    return audio


@pytest.fixture
def sample_audio_float32():
    """1 秒 16kHz 正弦波 (440Hz)，float32 格式"""
    sr = 16000
    t = np.linspace(0, 1.0, sr, endpoint=False)
    return (np.sin(2 * np.pi * 440 * t)).astype(np.float32)


@pytest.fixture
def base_timestamp():
    """固定的基準時間戳"""
    return 1718600000.0  # 2024-06-17 ~


@pytest.fixture
def make_entry(base_timestamp):
    """工廠函式：建立 TranscriptEntry"""
    def _make(
        text="test",
        speaker="spk_A",
        offset_start=0.0,
        offset_end=2.0,
        channel="ch_0",
        confidence=0.9,
    ):
        return TranscriptEntry(
            segment_id="seg_test",
            text=text,
            language="zh",
            confidence=confidence,
            channel_id=channel,
            channel_type=ChannelType.LOCAL_MIC,
            speaker_label=speaker,
            speaker_name=speaker,
            start_ts=base_timestamp + offset_start,
            end_ts=base_timestamp + offset_end,
        )
    return _make


# ──────────────────────────────────────────────────────────────
#  AudioSegment 測試
# ──────────────────────────────────────────────────────────────


class TestAudioSegment:

    def test_create_from_bytes(self, sample_audio_int16, base_timestamp):
        seg = AudioSegment(
            channel_id="mic_0",
            channel_type=ChannelType.LOCAL_MIC,
            start_timestamp=base_timestamp,
            end_timestamp=base_timestamp + 1.0,
            audio_data=sample_audio_int16.tobytes(),
            sample_rate=16000,
            num_samples=len(sample_audio_int16),
        )
        assert seg.channel_id == "mic_0"
        assert seg.channel_type == ChannelType.LOCAL_MIC
        assert seg.duration == pytest.approx(1.0)
        assert seg.num_samples == 16000
        assert seg.segment_id  # auto-generated

    def test_from_numpy_int16(self, sample_audio_int16, base_timestamp):
        seg = AudioSegment.from_numpy(
            sample_audio_int16,
            channel_id="mic_0",
            channel_type=ChannelType.LOCAL_MIC,
            start_timestamp=base_timestamp,
            end_timestamp=base_timestamp + 1.0,
        )
        assert seg.num_samples == 16000
        assert len(seg.audio_data) == 16000 * 2  # int16 = 2 bytes per sample

    def test_from_numpy_float32(self, sample_audio_float32, base_timestamp):
        seg = AudioSegment.from_numpy(
            sample_audio_float32,
            channel_id="mic_0",
            channel_type=ChannelType.LOCAL_MIC,
            start_timestamp=base_timestamp,
            end_timestamp=base_timestamp + 1.0,
        )
        assert seg.num_samples == 16000
        recovered = seg.to_float32()
        # float32 → int16 → float32 有量化誤差，但應在 1/32768 以內
        np.testing.assert_allclose(recovered, sample_audio_float32, atol=1.0 / 32768 + 1e-4)

    def test_to_numpy_roundtrip(self, sample_audio_int16, base_timestamp):
        seg = AudioSegment.from_numpy(
            sample_audio_int16,
            channel_id="mic_0",
            channel_type=ChannelType.LOCAL_MIC,
            start_timestamp=base_timestamp,
            end_timestamp=base_timestamp + 1.0,
        )
        recovered = seg.to_numpy()
        np.testing.assert_array_equal(recovered, sample_audio_int16)

    def test_duration_from_samples(self, sample_audio_int16, base_timestamp):
        seg = AudioSegment.from_numpy(
            sample_audio_int16,
            channel_id="mic_0",
            channel_type=ChannelType.LOCAL_MIC,
            start_timestamp=base_timestamp,
            end_timestamp=base_timestamp + 1.5,  # 故意不一致
            sample_rate=16000,
        )
        assert seg.duration == pytest.approx(1.5)  # 從時間戳算
        assert seg.duration_from_samples == pytest.approx(1.0)  # 從樣本數算

    def test_discord_channel_with_speaker(self, base_timestamp):
        seg = AudioSegment(
            channel_id="discord_123",
            channel_type=ChannelType.DISCORD,
            speaker_label="user_456",
            speaker_name="Xavier",
            start_timestamp=base_timestamp,
            end_timestamp=base_timestamp + 3.0,
            audio_data=b"\x00" * 100,
            num_samples=50,
        )
        assert seg.channel_type == ChannelType.DISCORD
        assert seg.speaker_label == "user_456"
        assert seg.speaker_name == "Xavier"

    def test_file_channel_type(self, base_timestamp):
        seg = AudioSegment(
            channel_id="file_test",
            channel_type=ChannelType.FILE,
            start_timestamp=base_timestamp,
            end_timestamp=base_timestamp + 5.0,
            audio_data=b"\x00" * 200,
        )
        assert seg.channel_type == ChannelType.FILE

    def test_priority_default_is_normal(self, base_timestamp):
        seg = AudioSegment(
            channel_id="ch_0",
            channel_type=ChannelType.LOCAL_MIC,
            start_timestamp=base_timestamp,
            end_timestamp=base_timestamp + 1.0,
            audio_data=b"\x00" * 100,
        )
        assert seg.priority == SegmentPriority.NORMAL

    def test_serialization_json(self, sample_audio_int16, base_timestamp):
        seg = AudioSegment.from_numpy(
            sample_audio_int16[:160],  # 10ms，小一點
            channel_id="mic_0",
            channel_type=ChannelType.LOCAL_MIC,
            start_timestamp=base_timestamp,
            end_timestamp=base_timestamp + 0.01,
        )
        json_str = seg.model_dump_json()
        loaded = json.loads(json_str)
        assert loaded["channel_id"] == "mic_0"
        assert loaded["channel_type"] == "local_mic"
        assert loaded["num_samples"] == 160

    def test_empty_audio(self, base_timestamp):
        seg = AudioSegment(
            channel_id="ch_0",
            channel_type=ChannelType.LOCAL_MIC,
            start_timestamp=base_timestamp,
            end_timestamp=base_timestamp,
            audio_data=b"",
            num_samples=0,
        )
        assert seg.duration == 0.0
        assert seg.duration_from_samples == 0.0
        assert len(seg.to_numpy()) == 0


# ──────────────────────────────────────────────────────────────
#  TranscriptEntry 測試
# ──────────────────────────────────────────────────────────────


class TestTranscriptEntry:

    def test_create_basic(self, make_entry):
        entry = make_entry(text="你好世界", speaker="spk_A")
        assert entry.text == "你好世界"
        assert entry.speaker_label == "spk_A"
        assert entry.duration == pytest.approx(2.0)

    def test_default_values(self):
        entry = TranscriptEntry(
            segment_id="seg_0",
            text="hello",
            channel_id="ch_0",
            channel_type=ChannelType.LOCAL_MIC,
            start_ts=100.0,
            end_ts=102.0,
        )
        assert entry.speaker_label == "unknown"
        assert entry.speaker_name == "Unknown"
        assert entry.confidence == 0.0
        assert entry.language is None

    def test_serialization(self, make_entry):
        entry = make_entry(text="測試")
        data = entry.model_dump()
        assert data["text"] == "測試"
        assert data["channel_type"] == "local_mic"

        # 反序列化
        recovered = TranscriptEntry(**data)
        assert recovered.text == "測試"
        assert recovered.channel_type == ChannelType.LOCAL_MIC


# ──────────────────────────────────────────────────────────────
#  TranscriptSession 測試
# ──────────────────────────────────────────────────────────────


class TestTranscriptSession:

    def test_create_empty(self):
        session = TranscriptSession()
        assert len(session.entries) == 0
        assert session.total_segments == 0
        assert session.session_id  # auto-generated

    def test_add_entry_sorted(self, make_entry, base_timestamp):
        session = TranscriptSession(started_at=base_timestamp)

        # 故意亂序加入
        session.add_entry(make_entry("第二句", "spk_B", 3.0, 5.0))
        session.add_entry(make_entry("第一句", "spk_A", 0.0, 2.0))
        session.add_entry(make_entry("第三句", "spk_A", 6.0, 8.0))

        assert len(session.entries) == 3
        assert session.entries[0].text == "第一句"
        assert session.entries[1].text == "第二句"
        assert session.entries[2].text == "第三句"
        assert session.total_segments == 3

    def test_total_audio_duration(self, make_entry, base_timestamp):
        session = TranscriptSession(started_at=base_timestamp)
        session.add_entry(make_entry("a", "spk_A", 0, 2))   # 2s
        session.add_entry(make_entry("b", "spk_B", 3, 5.5)) # 2.5s
        assert session.total_audio_duration == pytest.approx(4.5)

    def test_channels_used(self, make_entry, base_timestamp):
        session = TranscriptSession(started_at=base_timestamp)
        session.add_entry(make_entry("a", channel="ch_0"))
        session.add_entry(make_entry("b", channel="ch_1"))
        session.add_entry(make_entry("c", channel="ch_0"))
        assert session.channels_used == ["ch_0", "ch_1"]

    def test_get_entries_since(self, make_entry, base_timestamp):
        session = TranscriptSession(started_at=base_timestamp)
        session.add_entry(make_entry("a", offset_start=1.0, offset_end=2.0))
        session.add_entry(make_entry("b", offset_start=5.0, offset_end=6.0))
        session.add_entry(make_entry("c", offset_start=10.0, offset_end=11.0))

        recent = session.get_entries_since(base_timestamp + 4.0)
        assert len(recent) == 2
        assert recent[0].text == "b"

    def test_get_entries_by_speaker(self, make_entry, base_timestamp):
        session = TranscriptSession(started_at=base_timestamp)
        session.add_entry(make_entry("a", speaker="Alice", offset_start=0))
        session.add_entry(make_entry("b", speaker="Bob", offset_start=2))
        session.add_entry(make_entry("c", speaker="Alice", offset_start=4))

        alice = session.get_entries_by_speaker("Alice")
        assert len(alice) == 2
        assert all(e.speaker_label == "Alice" for e in alice)

    def test_get_entries_by_channel(self, make_entry, base_timestamp):
        session = TranscriptSession(started_at=base_timestamp)
        session.add_entry(make_entry("a", channel="ch_0", offset_start=0))
        session.add_entry(make_entry("b", channel="ch_1", offset_start=1))
        session.add_entry(make_entry("c", channel="ch_0", offset_start=2))

        ch0 = session.get_entries_by_channel("ch_0")
        assert len(ch0) == 2

    def test_get_speakers(self, make_entry, base_timestamp):
        session = TranscriptSession(started_at=base_timestamp)
        session.add_entry(make_entry("a", speaker="Alice", offset_start=0))
        session.add_entry(make_entry("b", speaker="Bob", offset_start=1))
        session.add_entry(make_entry("c", speaker="Alice", offset_start=2))

        speakers = session.get_speakers()
        assert speakers == ["Alice", "Bob"]

    def test_format_readable_relative(self, make_entry, base_timestamp):
        session = TranscriptSession(started_at=base_timestamp)
        session.add_entry(make_entry("你好", "Alice", 1.2, 3.0))
        session.add_entry(make_entry("嗨", "Bob", 3.5, 4.5))

        output = session.format_readable(use_relative_time=True)
        assert "Alice [00:01.2]: 你好" in output
        assert "Bob [00:03.5]: 嗨" in output

    def test_format_readable_absolute(self, make_entry, base_timestamp):
        session = TranscriptSession(started_at=base_timestamp)
        session.add_entry(make_entry("test", "Alice", 75.2, 77.0))

        output = session.format_readable(use_relative_time=True)
        assert "[01:15.2]" in output

    def test_format_readable_empty(self):
        session = TranscriptSession()
        assert session.format_readable() == "(no transcript entries)"

    def test_format_readable_speaker_name_preference(self, base_timestamp):
        """speaker_name != 'Unknown' 時優先顯示 speaker_name"""
        session = TranscriptSession(started_at=base_timestamp)
        entry = TranscriptEntry(
            segment_id="seg_0",
            text="hello",
            channel_id="ch_0",
            channel_type=ChannelType.DISCORD,
            speaker_label="user_123",
            speaker_name="Xavier",
            start_ts=base_timestamp + 1.0,
            end_ts=base_timestamp + 2.0,
        )
        session.add_entry(entry)

        output = session.format_readable()
        assert "Xavier" in output
        assert "user_123" not in output

    def test_format_srt(self, make_entry, base_timestamp):
        session = TranscriptSession(started_at=base_timestamp)
        session.add_entry(make_entry("第一句", "Alice", 1.0, 3.5))
        session.add_entry(make_entry("第二句", "Bob", 4.0, 6.2))

        srt = session.format_srt()
        assert "1\n" in srt
        assert "00:00:01,000 --> 00:00:03,500" in srt
        assert "[Alice] 第一句" in srt
        assert "2\n" in srt
        assert "[Bob] 第二句" in srt

    def test_format_srt_empty(self):
        session = TranscriptSession()
        assert session.format_srt() == ""

    def test_to_dict_list(self, make_entry, base_timestamp):
        session = TranscriptSession(started_at=base_timestamp)
        session.add_entry(make_entry("你好", "Alice", 1.0, 2.5, confidence=0.95))

        dicts = session.to_dict_list()
        assert len(dicts) == 1
        d = dicts[0]
        assert d["speaker"] == "Alice"
        assert d["start"] == 1.0
        assert d["end"] == 2.5
        assert d["text"] == "你好"
        assert d["confidence"] == 0.95

        # 可以 json.dumps
        json_str = json.dumps(dicts, ensure_ascii=False)
        assert "你好" in json_str

    def test_end_session(self):
        session = TranscriptSession()
        assert session.ended_at is None
        session.end_session()
        assert session.ended_at is not None
        assert session.ended_at >= session.started_at


# ──────────────────────────────────────────────────────────────
#  ChannelStatus / QueueMetrics / EngineMetrics 測試
# ──────────────────────────────────────────────────────────────


class TestStatusModels:

    def test_channel_status_defaults(self):
        status = ChannelStatus(
            channel_id="mic_0",
            channel_type=ChannelType.LOCAL_MIC,
        )
        assert status.state == ChannelState.IDLE
        assert status.segments_produced == 0
        assert status.vad_is_speech is False

    def test_channel_status_speech(self):
        status = ChannelStatus(
            channel_id="mic_0",
            channel_type=ChannelType.LOCAL_MIC,
            state=ChannelState.SPEECH,
            vad_is_speech=True,
            current_energy=0.85,
        )
        assert status.state == ChannelState.SPEECH
        assert status.vad_is_speech is True

    def test_queue_metrics_defaults(self):
        m = QueueMetrics()
        assert m.queue_size == 0
        assert m.max_size == 50
        assert m.total_dropped == 0

    def test_engine_metrics_defaults(self):
        m = EngineMetrics()
        assert m.active_channels == 0
        assert m.queue.max_size == 50

    def test_engine_metrics_nested(self):
        m = EngineMetrics(
            active_channels=3,
            queue=QueueMetrics(queue_size=5, total_enqueued=100),
            model_name="large-v3",
        )
        assert m.active_channels == 3
        assert m.queue.queue_size == 5
        assert m.queue.total_enqueued == 100
        assert m.model_name == "large-v3"


# ──────────────────────────────────────────────────────────────
#  Enum 測試
# ──────────────────────────────────────────────────────────────


class TestEnums:

    def test_channel_type_values(self):
        assert ChannelType.LOCAL_MIC.value == "local_mic"
        assert ChannelType.DISCORD.value == "discord"
        assert ChannelType.FILE.value == "file"

    def test_channel_state_values(self):
        assert ChannelState.IDLE.value == "idle"
        assert ChannelState.SPEECH.value == "speech"
        assert ChannelState.ERROR.value == "error"

    def test_segment_priority_ordering(self):
        assert SegmentPriority.REALTIME < SegmentPriority.NORMAL
        assert SegmentPriority.NORMAL < SegmentPriority.BACKFILL
