"""MemoryHost 的簡單計數器（階段 Y 的 E4 觀察期要用：skipped_lease、runs{trigger} 等）。"""
from __future__ import annotations

import threading
from collections import Counter


class MemoryMetrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counts: Counter[str] = Counter()

    def inc(self, name: str, amount: int = 1) -> None:
        with self._lock:
            self._counts[name] += amount

    def get(self, name: str) -> int:
        with self._lock:
            return self._counts[name]

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counts)
