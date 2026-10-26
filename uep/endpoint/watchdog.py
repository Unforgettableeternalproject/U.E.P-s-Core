"""本地 Lease watchdog（VC-4-4）。

只依賴本地單調時鐘與「最後一次成功續約換算出的期限」，不依賴網路、revoke 送達或 Core 的任何回應：
期限一到，不論 Core 是否可達，端點都必須自行停止輸出。時鐘可注入，測試不需等待真時間。
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable


class LeaseWatchdog:
    def __init__(self, on_expire: Callable[[], None], *, clock: Callable[[], float] = time.monotonic) -> None:
        self._on_expire = on_expire
        self._clock = clock
        self._lock = threading.Lock()
        self._deadline: float | None = None

    def arm(self, deadline: float) -> None:
        """設定（或延後）期限；單調時鐘的絕對值。"""
        with self._lock:
            self._deadline = deadline

    def disarm(self) -> None:
        with self._lock:
            self._deadline = None

    def remaining(self) -> float | None:
        """剩餘秒數（可為負）；未啟動時為 None。"""
        with self._lock:
            return None if self._deadline is None else self._deadline - self._clock()

    def check(self) -> bool:
        """已過期則解除武裝並呼叫 ``on_expire``（每次 arm 只觸發一次）；回傳是否觸發。"""
        with self._lock:
            if self._deadline is None or self._clock() < self._deadline:
                return False
            self._deadline = None
        self._on_expire()
        return True
