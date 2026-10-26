"""程序內 Event Bus。

由 ``core/event_bus.py`` 複製而來（A4／A7，不 import legacy）。語意沿用：
單一消費者線程分發、handler 例外隔離。與 legacy 的差異：
- 訂閱鍵由 ``SystemEvent`` enum 改為 ``str``（D22，即 ``UEPEvent.event_type``）。
- 對外介面收斂為 subscribe／unsubscribe／publish／start／stop（P3 要橋接
  Echo Stream 的 AsyncIterator，故不散出其他 API）。
- 不內建歷史、統計與全域單例；落 journal 屬 ``EventPublisher``，Bus 只負責分發。
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from queue import Empty, Queue

from uep.events.envelope import UEPEvent

logger = logging.getLogger("uep.events.bus")

Handler = Callable[[UEPEvent], None]


class EventBus:
    def __init__(self) -> None:
        self._handlers: dict[str, list[Handler]] = {}
        self._lock = threading.Lock()
        self._queue: Queue[UEPEvent] = Queue()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    def start(self) -> None:
        """啟動事件處理線程（冪等）。"""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="UepEventBus")
        self._thread.start()

    def stop(self) -> None:
        """停止事件處理線程。"""
        if not self._thread:
            return
        self._stop_event.set()
        self._thread.join(timeout=5.0)
        if self._thread.is_alive():
            logger.error("事件處理線程未能正常結束")
        self._thread = None

    def subscribe(self, event_type: str, handler: Handler) -> None:
        with self._lock:
            self._handlers.setdefault(event_type, []).append(handler)

    def unsubscribe(self, event_type: str, handler: Handler) -> None:
        with self._lock:
            handlers = self._handlers.get(event_type)
            if handlers and handler in handlers:
                handlers.remove(handler)

    def publish(self, event: UEPEvent, *, sync: bool = False) -> None:
        """同步則於呼叫線程分發；否則入佇列由消費者線程分發。"""
        if sync:
            self._dispatch(event)
        else:
            self._queue.put(event)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                event = self._queue.get(timeout=0.2)
            except Empty:
                continue
            try:
                self._dispatch(event)
            except Exception:  # 防禦：_dispatch 已隔離 handler 例外
                logger.exception("事件處理循環錯誤")
            finally:
                self._queue.task_done()

    def _dispatch(self, event: UEPEvent) -> None:
        with self._lock:
            handlers = list(self._handlers.get(event.event_type, ()))
        for handler in handlers:
            try:
                handler(event)
            except Exception:
                logger.exception("事件處理器錯誤（%s）", event.event_type)
