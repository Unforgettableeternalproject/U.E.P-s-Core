"""
TranscriptFormatter — 格式化輸出工具

提供時間戳轉換和 entry/session 的文字格式化。
"""

from __future__ import annotations

from echo_stt.core.multi_channel_schemas import TranscriptEntry, TranscriptSession


class TranscriptFormatter:
    """格式化工具（純靜態方法）。"""

    @staticmethod
    def to_relative_time(abs_ts: float, session_start: float) -> str:
        """絕對時間 → '[MM:SS.s]' 格式的相對時間。"""
        t = max(0.0, abs_ts - session_start)
        minutes = int(t // 60)
        seconds = t % 60
        return f"[{minutes:02d}:{seconds:04.1f}]"

    @staticmethod
    def format_entry(entry: TranscriptEntry, session_start: float) -> str:
        """單一 entry → '說話人A [00:01.2]: text' 格式。"""
        time_str = TranscriptFormatter.to_relative_time(entry.start_ts, session_start)
        name = entry.speaker_name if entry.speaker_name != "Unknown" else entry.speaker_label
        return f"{name} {time_str}: {entry.text}"

    @staticmethod
    def format_session(session: TranscriptSession) -> str:
        """整個 session → 多行文字。"""
        return session.format_readable(use_relative_time=True)

    @staticmethod
    def format_entry_rich(entry: TranscriptEntry, session_start: float) -> str:
        """Rich markup 格式（供 CLI 用）。"""
        t = max(0.0, entry.start_ts - session_start)
        minutes = int(t // 60)
        seconds = t % 60

        conf = entry.confidence
        conf_color = "green" if conf >= 0.8 else "yellow" if conf >= 0.5 else "red"
        name = entry.speaker_name if entry.speaker_name != "Unknown" else entry.speaker_label

        return (
            f"  [{conf_color}]●[/{conf_color}] "
            f"[dim][{minutes:02d}:{seconds:04.1f}][/dim] "
            f"[bold]{name}[/]: {entry.text}"
        )
