"""Core 工作緒：ES 回呼（在 ES loop 上）只入佇列，這裡依序寫 Gateway／DB。

單一執行緒、FIFO：同一 turn 的事件、intent dispatch、證據回報、終結事件因此保持先後順序
（證據回報先於 ``turn.completed``）。例外只記 log 與計數，不中斷後續工作。
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from collections import Counter
from collections.abc import Callable

logger = logging.getLogger("uep.echo.worker")

_STOP = object()


class CoreWorker:
    def __init__(self, name: str = "uep-echo-worker") -> None:
        self._queue: queue.Queue[object] = queue.Queue()
        self._name = name
        self._thread: threading.Thread | None = None
        self._cond = threading.Condition()
        self._pending = 0
        self._closing = False
        self.metrics: Counter[str] = Counter()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        with self._cond:
            self._closing = False
        self._thread = threading.Thread(target=self._run, name=self._name, daemon=True)
        self._thread.start()

    def submit(self, fn: Callable[[], None], label: str = "job") -> bool:
        """任何執行緒（含 ES loop）；只入佇列，不阻塞。

        回傳是否入佇列：工作緒已關閉（或尚未啟動）回 False，由呼叫端改走 inline，不遺失任務。
        判斷與計數在同一把鎖內，關閉旗標之後不會再有任務被收進佇列。"""
        with self._cond:
            if self._closing or not self.running:
                return False
            self._pending += 1
            self._queue.put((fn, label))
        return True

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is _STOP:
                return
            fn, label = item  # type: ignore[misc]
            try:
                fn()
            except Exception:
                self.metrics["echo.worker.errors"] += 1
                logger.exception("Core 工作緒任務失敗：%s", label)
            finally:
                with self._cond:
                    self._pending -= 1
                    self._cond.notify_all()

    def drain(self, timeout: float = 5.0) -> bool:
        """等佇列清空（已入佇列的都做完）；逾時回 False。"""
        deadline = time.monotonic() + timeout
        with self._cond:
            while self._pending > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cond.wait(remaining)
        return True

    def stop(self, timeout: float = 5.0) -> None:
        with self._cond:
            self._closing = True
        thread = self._thread
        if thread is None:
            return
        self._queue.put(_STOP)
        thread.join(timeout)
        self._thread = None
