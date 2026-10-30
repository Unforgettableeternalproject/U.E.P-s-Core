"""轉接層的計數器：多個執行緒（ES loop、工作緒、端點執行緒）都會累加，``+=`` 不是原子操作，所以用鎖保護。

沿用 ``Counter`` 的讀取介面（``metrics["key"]``、``dict(metrics)``）；累加一律走 ``inc``。
"""
from __future__ import annotations

import threading
from collections import Counter


class Metrics(Counter):  # type: ignore[type-arg]
    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()

    def inc(self, key: str, amount: int = 1) -> None:
        with self._lock:
            self[key] += amount
