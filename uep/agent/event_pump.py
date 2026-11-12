"""``EventPump``：在專用執行緒上拉取同步事件迭代器，讓呼叫端能帶逾時等待（Step 逾時、取消旗標）。

模型串流的 ``next()`` 會阻塞到下一個事件；AgentLoop 在等待期間仍要檢查取消旗標與 Step 逾時，所以把 ``next`` 放到專用的
daemon 執行緒（整個 attempt 的串流都在同一條執行緒上驅動，Runtime 的私有 event loop 不會被跨執行緒並用）。
逾時只是放棄等待，進行中的 ``next`` 留給呼叫端的 ``Runtime.cancel`` 收尾；不 join 卡住的執行緒。
"""
from __future__ import annotations

import queue
import threading
from collections.abc import Iterator
from typing import Any

TIMED_OUT = object()  # ``get`` 逾時（進行中的 next 尚未回來）
_END = object()
_STOP = object()


class EventPump:
    def __init__(self, events: Iterator[Any]) -> None:
        self._events = events
        self._requests: queue.Queue[Any] = queue.Queue()
        self._results: queue.Queue[Any] = queue.Queue()
        self._outstanding = False
        self._thread = threading.Thread(target=self._run, name="uep-event-pump", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while self._requests.get() is not _STOP:
            try:
                item = next(self._events, _END)
            except BaseException as exc:  # noqa: BLE001  交給呼叫端重新拋出
                item = exc
            self._results.put(item)

    def get(self, timeout: float) -> Any:
        """下一個事件；串流結束回 None；``timeout`` 秒內沒有回 ``TIMED_OUT``（請求保留，下次 ``get`` 繼續等同一個）。"""
        if not self._outstanding:
            self._requests.put(True)
            self._outstanding = True
        try:
            item = self._results.get(timeout=max(timeout, 0.0))
        except queue.Empty:
            return TIMED_OUT
        self._outstanding = False
        if isinstance(item, BaseException):
            raise item
        return None if item is _END else item

    def close(self) -> None:
        """結束執行緒（若仍卡在 next，next 回來後才會結束）。"""
        self._requests.put(_STOP)
