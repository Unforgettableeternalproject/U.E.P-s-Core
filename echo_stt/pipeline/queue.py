"""
TranscriptionQueue — 帶優先權的 AudioSegment 佇列

按 abs_timestamp 排序（越早說越早辨識），確保 transcript 時序正確。

溢出策略可配置：
- drop_oldest（預設）：丟棄佇列中最舊的 segment，保留最新語音（即時場景）
- drop_newest：拒收新 segment，保留佇列中已有的（離線/批次場景）
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from echo_stt.core.multi_channel_schemas import AudioSegment, QueueMetrics

logger = logging.getLogger(__name__)

# 合法的溢出策略
OVERFLOW_POLICIES = ("drop_oldest", "drop_newest")


@dataclass(order=True)
class _QueueItem:
    """佇列內部排序用的包裝。priority 越小越優先。"""
    priority: float
    sequence: int = field(compare=True)  # 同 priority 時用 sequence 保持穩定排序
    segment: AudioSegment = field(compare=False)
    enqueued_at: float = field(default_factory=time.time, compare=False)


class TranscriptionQueue:
    """帶優先權的 AudioSegment 佇列。

    排序邏輯：
    1. priority = segment.start_timestamp（越早說越優先）
    2. 同時間則按 sequence_num（先進先出）

    溢出策略：
    - drop_oldest（預設）：佇列滿時丟棄最舊（時間最早）的 segment，
      騰出空間給新的。適合即時場景——WebRTC jitter buffer、VoIP
      封包佇列的業界標準做法。
    - drop_newest：佇列滿時拒收新 segment。適合離線分析——
      不能遺失任何段落。

    Thread-safe，可由多個 ChannelManager 線程同時 put。
    """

    def __init__(
        self,
        max_size: int = 50,
        overflow_policy: str = "drop_oldest",
    ):
        if overflow_policy not in OVERFLOW_POLICIES:
            raise ValueError(
                f"overflow_policy 必須是 {OVERFLOW_POLICIES} 之一，"
                f"收到: {overflow_policy!r}"
            )

        self.max_size = max_size
        self.overflow_policy = overflow_policy
        self._queue: queue.PriorityQueue = queue.PriorityQueue(maxsize=0)
        self._lock = threading.Lock()

        # 統計
        self._total_enqueued = 0
        self._total_dequeued = 0
        self._total_dropped = 0
        self._wait_times: list[float] = []

    def put(self, segment: AudioSegment) -> bool:
        """放入 segment。

        Returns:
            True 若成功放入，False 若因溢出策略而丟棄。
        """
        with self._lock:
            if self._queue.qsize() >= self.max_size:
                if self.overflow_policy == "drop_newest":
                    # 拒收新的 segment
                    self._total_dropped += 1
                    logger.warning(
                        "[Queue] 佇列已滿 (%d/%d)，丟棄新 segment %s from channel %s",
                        self._queue.qsize(), self.max_size,
                        segment.segment_id, segment.channel_id,
                    )
                    return False

                # drop_oldest：取出最舊的丟棄，騰出空間
                try:
                    evicted = self._queue.get_nowait()
                    self._total_dropped += 1
                    logger.warning(
                        "[Queue] 佇列已滿 (%d/%d)，逐出最舊 segment "
                        "(ts=%.2f) 以容納新 segment %s from channel %s",
                        self.max_size, self.max_size,
                        evicted.priority, segment.segment_id, segment.channel_id,
                    )
                except queue.Empty:
                    pass  # 理論上不會走到這裡

            item = _QueueItem(
                priority=segment.start_timestamp,
                sequence=segment.sequence_num,
                segment=segment,
            )
            self._queue.put_nowait(item)
            self._total_enqueued += 1
            return True

    def get(self, timeout: Optional[float] = None) -> Optional[AudioSegment]:
        """取出優先權最高的 segment。

        Args:
            timeout: 最長等待秒數（None = 阻塞直到有 item）

        Returns:
            AudioSegment 或 None（若超時）
        """
        try:
            item: _QueueItem = self._queue.get(timeout=timeout)
            self._total_dequeued += 1

            wait_time = time.time() - item.enqueued_at
            self._wait_times.append(wait_time)
            # 只保留最近 100 筆
            if len(self._wait_times) > 100:
                self._wait_times.pop(0)

            return item.segment
        except queue.Empty:
            return None

    def qsize(self) -> int:
        return self._queue.qsize()

    def is_empty(self) -> bool:
        return self._queue.empty()

    def is_full(self) -> bool:
        return self._queue.qsize() >= self.max_size

    def get_metrics(self) -> QueueMetrics:
        """取得佇列統計快照。"""
        avg_wait = (
            sum(self._wait_times) / len(self._wait_times)
            if self._wait_times else 0.0
        )
        return QueueMetrics(
            queue_size=self._queue.qsize(),
            max_size=self.max_size,
            total_enqueued=self._total_enqueued,
            total_dequeued=self._total_dequeued,
            total_dropped=self._total_dropped,
            avg_wait_time=round(avg_wait, 4),
        )

    def clear(self) -> int:
        """清空佇列，回傳被清掉的數量。"""
        count = 0
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
                count += 1
            except queue.Empty:
                break
        return count
