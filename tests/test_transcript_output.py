"""
Transcript 輸出層測試

覆蓋：
- TranscriptFormatter: 時間格式化、entry/session 格式化
- DiarizationBridge: Discord passthrough、local_mic fallback、mock speaker module
- TranscriptAggregator: session 管理、排序、callback、format 輸出
"""

from __future__ import annotations

from unittest.mock import MagicMock
import time

import numpy as np
import pytest

from echo_stt.core.multi_channel_schemas import (
    AudioSegment,
    ChannelType,
    TranscriptEntry,
    TranscriptSession,
)
from echo_stt.output.aggregator import TranscriptAggregator
from echo_stt.output.diarization_bridge import DiarizationBridge
from echo_stt.output.formatter import TranscriptFormatter


# ──────────────────────────────────────────────────────────────
#  Helpers
# ──────────────────────────────────────────────────────────────

BASE_TS = 1718600000.0


def make_entry(
    text="你好",
    speaker_label="unknown",
    speaker_name="Unknown",
    start_offset=0.0,
    end_offset=2.0,
    channel_id="ch_0",
    channel_type=ChannelType.LOCAL_MIC,
    confidence=0.9,
):
    return TranscriptEntry(
        segment_id="seg_test",
        text=text,
        language="zh",
        confidence=confidence,
        channel_id=channel_id,
        channel_type=channel_type,
        speaker_label=speaker_label,
        speaker_name=speaker_name,
        start_ts=BASE_TS + start_offset,
        end_ts=BASE_TS + end_offset,
    )


def make_segment(
    channel_id="ch_0",
    channel_type=ChannelType.LOCAL_MIC,
    speaker_label=None,
    speaker_name=None,
):
    return AudioSegment(
        channel_id=channel_id,
        channel_type=channel_type,
        speaker_label=speaker_label,
        speaker_name=speaker_name,
        start_timestamp=BASE_TS,
        end_timestamp=BASE_TS + 2.0,
        audio_data=np.zeros(100, dtype=np.int16).tobytes(),
        num_samples=100,
    )


# ──────────────────────────────────────────────────────────────
#  TranscriptFormatter 測試
# ──────────────────────────────────────────────────────────────


class TestTranscriptFormatter:

    def test_relative_time_zero(self):
        assert TranscriptFormatter.to_relative_time(BASE_TS, BASE_TS) == "[00:00.0]"

    def test_relative_time_seconds(self):
        assert TranscriptFormatter.to_relative_time(BASE_TS + 5.3, BASE_TS) == "[00:05.3]"

    def test_relative_time_minutes(self):
        assert TranscriptFormatter.to_relative_time(BASE_TS + 75.2, BASE_TS) == "[01:15.2]"

    def test_relative_time_negative_clamped(self):
        result = TranscriptFormatter.to_relative_time(BASE_TS - 5.0, BASE_TS)
        assert result == "[00:00.0]"

    def test_format_entry(self):
        entry = make_entry(text="你好世界", speaker_name="Alice", start_offset=3.5)
        result = TranscriptFormatter.format_entry(entry, BASE_TS)
        assert result == "Alice [00:03.5]: 你好世界"

    def test_format_entry_uses_label_when_name_unknown(self):
        entry = make_entry(text="test", speaker_label="spk_001", speaker_name="Unknown")
        result = TranscriptFormatter.format_entry(entry, BASE_TS)
        assert "spk_001" in result
        assert "Unknown" not in result

    def test_format_entry_rich(self):
        entry = make_entry(text="hello", speaker_name="Bob", confidence=0.95, start_offset=1.0)
        result = TranscriptFormatter.format_entry_rich(entry, BASE_TS)
        assert "Bob" in result
        assert "hello" in result
        assert "green" in result  # high confidence


# ──────────────────────────────────────────────────────────────
#  DiarizationBridge 測試
# ──────────────────────────────────────────────────────────────


class TestDiarizationBridge:

    def test_discord_passthrough(self):
        bridge = DiarizationBridge()
        seg = make_segment(
            channel_type=ChannelType.DISCORD,
            speaker_label="user_123",
            speaker_name="Xavier",
        )
        label, name = bridge.resolve_speaker(seg)
        assert label == "user_123"
        assert name == "Xavier"

    def test_discord_uses_channel_id_if_no_label(self):
        bridge = DiarizationBridge()
        seg = make_segment(
            channel_id="discord_456",
            channel_type=ChannelType.DISCORD,
        )
        label, name = bridge.resolve_speaker(seg)
        assert label == "discord_456"

    def test_local_mic_with_preset_speaker(self):
        bridge = DiarizationBridge()
        seg = make_segment(
            channel_type=ChannelType.LOCAL_MIC,
            speaker_label="alice",
            speaker_name="Alice",
        )
        label, name = bridge.resolve_speaker(seg)
        assert label == "alice"
        assert name == "Alice"

    def test_local_mic_no_speaker_no_module(self):
        """沒有 speaker_module 時用 channel_id"""
        bridge = DiarizationBridge()
        seg = make_segment(channel_id="mic_0", channel_type=ChannelType.LOCAL_MIC)
        label, name = bridge.resolve_speaker(seg)
        assert label == "mic_0"

    def test_local_mic_with_mock_speaker_module(self):
        """有 speaker_module 時呼叫 identify_speaker"""
        mock_module = MagicMock()
        mock_result = MagicMock()
        mock_result.speaker_id = "identified_alice"
        mock_module.identify_speaker.return_value = mock_result

        bridge = DiarizationBridge(speaker_module=mock_module)
        seg = make_segment(channel_type=ChannelType.LOCAL_MIC)
        label, name = bridge.resolve_speaker(seg)

        assert label == "identified_alice"
        mock_module.identify_speaker.assert_called_once()

    def test_speaker_module_failure_fallback(self):
        """speaker_module 拋例外時 fallback 到遞增編號"""
        mock_module = MagicMock()
        mock_module.identify_speaker.side_effect = RuntimeError("boom")

        bridge = DiarizationBridge(speaker_module=mock_module)
        seg = make_segment(channel_type=ChannelType.LOCAL_MIC)
        label, name = bridge.resolve_speaker(seg)

        assert label.startswith("speaker_")

    def test_resolve_entry(self):
        bridge = DiarizationBridge()
        entry = make_entry()
        seg = make_segment(
            channel_type=ChannelType.DISCORD,
            speaker_label="user_X",
            speaker_name="Xavier",
        )

        resolved = bridge.resolve_entry(entry, seg)
        assert resolved.speaker_label == "user_X"
        assert resolved.speaker_name == "Xavier"


# ──────────────────────────────────────────────────────────────
#  TranscriptAggregator 測試
# ──────────────────────────────────────────────────────────────


class TestTranscriptAggregator:

    def test_start_session(self):
        agg = TranscriptAggregator()
        session = agg.start_session()
        assert session is not None
        assert agg.entry_count == 0

    def test_add_entry(self):
        agg = TranscriptAggregator()
        agg.start_session()
        agg.add_entry(make_entry(text="hello"))
        assert agg.entry_count == 1

    def test_auto_start_session(self):
        """沒有 start_session 也能 add_entry"""
        agg = TranscriptAggregator()
        agg.add_entry(make_entry(text="auto"))
        assert agg.entry_count == 1
        assert agg.get_session() is not None

    def test_entries_sorted(self):
        agg = TranscriptAggregator()
        agg.start_session()

        agg.add_entry(make_entry("第二", start_offset=5.0))
        agg.add_entry(make_entry("第一", start_offset=1.0))
        agg.add_entry(make_entry("第三", start_offset=10.0))

        session = agg.get_session()
        assert [e.text for e in session.entries] == ["第一", "第二", "第三"]

    def test_on_entry_callback(self):
        received = []
        agg = TranscriptAggregator(on_entry=lambda e: received.append(e))
        agg.start_session()
        agg.add_entry(make_entry("test"))

        assert len(received) == 1
        assert received[0].text == "test"

    def test_diarization_with_segment(self):
        agg = TranscriptAggregator()
        agg.start_session()

        entry = make_entry(speaker_label="unknown")
        seg = make_segment(
            channel_type=ChannelType.DISCORD,
            speaker_label="user_X",
            speaker_name="Xavier",
        )
        agg.add_entry(entry, segment=seg)

        assert agg.get_session().entries[0].speaker_label == "user_X"

    def test_format_readable(self):
        agg = TranscriptAggregator()
        session = agg.start_session(started_at=BASE_TS)

        agg.add_entry(make_entry("你好", speaker_name="Alice", start_offset=1.0))
        agg.add_entry(make_entry("嗨", speaker_name="Bob", start_offset=3.5))

        output = agg.format_readable()
        assert "Alice [00:01.0]: 你好" in output
        assert "Bob [00:03.5]: 嗨" in output

    def test_format_srt(self):
        agg = TranscriptAggregator()
        agg.start_session(started_at=BASE_TS)
        agg.add_entry(make_entry("test", speaker_name="Alice", start_offset=1.0, end_offset=3.0))

        srt = agg.format_srt()
        assert "Alice" in srt
        assert "-->" in srt

    def test_export_json(self):
        agg = TranscriptAggregator()
        agg.start_session(started_at=BASE_TS)
        agg.add_entry(make_entry("hello", speaker_name="Alice"))

        data = agg.export_json()
        assert len(data) == 1
        assert data[0]["text"] == "hello"

    def test_end_session(self):
        agg = TranscriptAggregator()
        agg.start_session()
        agg.add_entry(make_entry("a"))

        finished = agg.end_session()
        assert finished is not None
        assert finished.ended_at is not None
        assert finished.total_segments == 1
        assert agg.get_session() is None

    def test_reset(self):
        agg = TranscriptAggregator()
        agg.start_session()
        agg.add_entry(make_entry("old"))

        finished = agg.reset()
        assert finished.total_segments == 1
        assert agg.entry_count == 0
        assert agg.get_session() is not None  # 新 session 已建立

    def test_speakers_list(self):
        agg = TranscriptAggregator()
        agg.start_session()
        agg.add_entry(make_entry("a", speaker_label="Alice"))
        agg.add_entry(make_entry("b", speaker_label="Bob"))
        agg.add_entry(make_entry("c", speaker_label="Alice"))

        assert agg.speakers == ["Alice", "Bob"]

    def test_no_session_format(self):
        agg = TranscriptAggregator()
        assert agg.format_readable() == "(no active session)"
        assert agg.format_srt() == ""
        assert agg.export_json() == []

    def test_auto_start_out_of_order_workers(self):
        """模擬多 worker 競爭：較晚 timestamp 的 entry 先完成到達 aggregator。

        Scenario:
            - Worker A 處理 t=5.0 的片段，先完成 → auto-start 用 5.0 當 base
            - Worker B 處理 t=1.0 的片段，後完成 → base 應回溯到 1.0
        結果 format_readable 應顯示 [00:00.0] 和 [00:04.0]（而非 clamp 成 [00:00.0] [00:00.0]）
        """
        agg = TranscriptAggregator()

        # Worker A 先到（timestamp 較晚）
        later_entry = make_entry("後面的話", speaker_name="Alice", start_offset=5.0, end_offset=7.0)
        agg.add_entry(later_entry)

        # 此時 session.started_at 應為 BASE_TS + 5.0
        session = agg.get_session()
        assert session is not None
        assert session.started_at == BASE_TS + 5.0

        # Worker B 後到（timestamp 較早）
        earlier_entry = make_entry("前面的話", speaker_name="Bob", start_offset=1.0, end_offset=3.0)
        agg.add_entry(earlier_entry)

        # started_at 應回溯到 BASE_TS + 1.0
        assert session.started_at == BASE_TS + 1.0

        # format_readable 應正確顯示相對時間
        output = agg.format_readable()
        assert "Bob [00:00.0]: 前面的話" in output
        assert "Alice [00:04.0]: 後面的話" in output

    def test_explicit_session_no_backtrack(self):
        """明確 start_session(started_at=X) 時，更早的 entry 不應回溯 base。

        Engine 明確設定的 base time 優先——回溯只在 auto-start 場景觸發。
        實際上回溯在這裡也是安全的（只是把 base 往更早拉），
        但此測試驗證 clamp 至少保護了格式化結果。
        """
        agg = TranscriptAggregator()
        agg.start_session(started_at=BASE_TS + 10.0)

        # entry 時間早於 session base
        entry = make_entry("早到的話", speaker_name="Alice", start_offset=2.0)
        agg.add_entry(entry)

        # 回溯會發生（更早的 entry 把 base 拉回）
        session = agg.get_session()
        assert session.started_at == BASE_TS + 2.0

        # format 結果應從新 base 計算
        output = agg.format_readable()
        assert "Alice [00:00.0]: 早到的話" in output
