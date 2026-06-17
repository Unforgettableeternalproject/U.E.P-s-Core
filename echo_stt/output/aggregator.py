"""
TranscriptAggregator — 彙整多通道辨識結果

接收來自 STTWorkerPool 的 TranscriptEntry，
按時間排序、解析說話人、管理 session 生命週期。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

from echo_stt.core.multi_channel_schemas import (
    AudioSegment,
    TranscriptEntry,
    TranscriptSession,
)
from echo_stt.output.diarization_bridge import DiarizationBridge
from echo_stt.output.formatter import TranscriptFormatter

logger = logging.getLogger(__name__)


class TranscriptAggregator:
    """彙整來自所有 channel 的 TranscriptEntry。

    職責：
    1. 接收 entry，按 start_ts 排序存入 TranscriptSession
    2. 透過 DiarizationBridge 解析說話人（local_mic 場景）
    3. 觸發 on_entry callback（即時輸出）
    4. 管理 session 生命週期（開始/結束/重置）

    Usage:
        agg = TranscriptAggregator(
            on_entry=lambda e: print(e.text),
            speaker_module=my_speaker_id,
        )
        agg.start_session()
        agg.add_entry(entry, segment)  # 由 STTWorkerPool callback 呼叫
        transcript = agg.format_readable()
    """

    def __init__(
        self,
        on_entry: Optional[Callable[[TranscriptEntry], None]] = None,
        speaker_module=None,
    ):
        self.on_entry = on_entry
        self.bridge = DiarizationBridge(speaker_module=speaker_module)
        self._session: Optional[TranscriptSession] = None
        self._lock = threading.Lock()

    def _create_session(
        self,
        session_id: Optional[str] = None,
        started_at: Optional[float] = None,
    ) -> TranscriptSession:
        """建立 TranscriptSession（不取鎖，供內部已持鎖路徑使用）。"""
        kwargs: dict = {}
        if session_id:
            kwargs["session_id"] = session_id
        if started_at is not None:
            kwargs["started_at"] = started_at
        return TranscriptSession(**kwargs)

    def start_session(
        self,
        session_id: Optional[str] = None,
        started_at: Optional[float] = None,
    ) -> TranscriptSession:
        """開始新的 session。

        Args:
            session_id: 自訂 session ID（可選）
            started_at: 自訂起始時間戳（可選，預設 time.time()）
        """
        with self._lock:
            self._session = self._create_session(session_id, started_at)
        logger.info("[Aggregator] Session started: %s", self._session.session_id)
        return self._session

    def add_entry(
        self,
        entry: TranscriptEntry,
        segment: Optional[AudioSegment] = None,
    ) -> None:
        """加入辨識結果。

        Args:
            entry: STTWorker 產生的 TranscriptEntry
            segment: 對應的 AudioSegment（用於 diarization bridge）
        """
        # 解析說話人
        if segment is not None:
            entry = self.bridge.resolve_entry(entry, segment)

        with self._lock:
            if self._session is None:
                # Auto-start：用 entry 的起始時間作為 session 基準
                self._session = self._create_session(started_at=entry.start_ts)
                logger.info(
                    "[Aggregator] Session auto-started: %s (base_ts=%.1f)",
                    self._session.session_id, entry.start_ts,
                )
            self._session.add_entry(entry)

        # 即時 callback
        if self.on_entry is not None:
            try:
                self.on_entry(entry)
            except Exception as e:
                logger.error("[Aggregator] on_entry callback 失敗: %s", e)

    def get_session(self) -> Optional[TranscriptSession]:
        """取得當前 session。"""
        return self._session

    def format_readable(self, since_ts: Optional[float] = None) -> str:
        """取得人類可讀的 transcript。"""
        with self._lock:
            if self._session is None:
                return "(no active session)"
            if since_ts is not None:
                entries = self._session.get_entries_since(since_ts)
                if not entries:
                    return "(no entries since requested time)"
                lines = []
                for entry in entries:
                    lines.append(
                        TranscriptFormatter.format_entry(entry, self._session.started_at)
                    )
                return "\n".join(lines)
            return self._session.format_readable()

    def format_srt(self) -> str:
        """SRT 字幕格式。"""
        with self._lock:
            if self._session is None:
                return ""
            return self._session.format_srt()

    def export_json(self) -> list:
        """JSON-friendly dict list。"""
        with self._lock:
            if self._session is None:
                return []
            return self._session.to_dict_list()

    def end_session(self) -> Optional[TranscriptSession]:
        """結束當前 session，回傳完成的 session。"""
        with self._lock:
            if self._session is None:
                return None
            self._session.end_session()
            finished = self._session
            self._session = None
        logger.info(
            "[Aggregator] Session ended: %s (%d entries)",
            finished.session_id, finished.total_segments,
        )
        return finished

    def reset(self) -> Optional[TranscriptSession]:
        """重置（結束當前並開始新 session）。"""
        finished = self.end_session()
        self.start_session()
        return finished

    @property
    def entry_count(self) -> int:
        if self._session is None:
            return 0
        return len(self._session.entries)

    @property
    def speakers(self) -> list[str]:
        if self._session is None:
            return []
        return self._session.get_speakers()
