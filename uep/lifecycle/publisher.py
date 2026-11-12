"""模組事件發布器（Module Coordinator 設計稿 §8、§9.1；PR-2）。

職責：
- 事件在 coordinator 鎖內「入佇列」（只做記憶體 append，保住 revision 順序），在鎖外由 ``drain`` 逐一送出（可能含 DB／Bus I/O）。
- 送出失敗不回滾狀態：只記錄例外型別名稱並計數，之後靠 resync 補償（E-13：Bus best-effort）。
- 單一 drainer：同一時間只有一條執行緒在送，後到者只入佇列；順序與入佇列一致。

``sink`` 由組裝層注入（離線測試可直接接 Bus；組裝層用 ``bus_sink`` 經 ``EventPublisher`` 落 journal 再同步分發），
所以本檔不需要 DB。
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from typing import Any

from uep.events.envelope import EventDraft, TrustedContext
from uep.lifecycle.states import (
    MODULE_PROVISION_RESOLVED,
    MODULE_STATE_CHANGED,
    validate_provision_resolved_payload,
    validate_state_changed_payload,
)

logger = logging.getLogger("uep.lifecycle")

Sink = Callable[[str, Mapping[str, Any]], None]
MODULE_SOURCE = "module.coordinator"
MODULE_ACTOR = "uep:module_coordinator"


def bus_sink(publisher: Any, *, persona_id: str = "uep", sync: bool = True) -> Sink:
    """組裝層用：經 ``EventPublisher.publish`` 落 journal 並分發到 Bus。

    ``sync=True`` 在呼叫線程分發，啟動快照才能保證先於 Core READY 被訂閱者套用（佇列分發沒有此保證）。"""

    trusted = TrustedContext(source=MODULE_SOURCE, actor_id=MODULE_ACTOR, scope_ref="scope_private_core", persona_id=persona_id)

    def sink(event_type: str, payload: Mapping[str, Any]) -> None:
        publisher.publish(EventDraft(event_type=event_type, payload=dict(payload)), trusted, sync=sync)

    return sink


class ModuleEventPublisher:
    def __init__(self, sink: Sink) -> None:
        self._sink = sink
        self._queue: deque[tuple[str, dict[str, Any]]] = deque()
        self._cond = threading.Condition(threading.Lock())
        self._draining = False
        self.published = 0
        self.failed = 0

    def enqueue(self, event_type: str, payload: Mapping[str, Any]) -> None:
        """入佇列（記憶體內，可在 coordinator 鎖內呼叫）。payload 不合規拋 ``ValueError``，不入佇列。"""
        if event_type == MODULE_STATE_CHANGED:
            validate_state_changed_payload(payload)
        elif event_type == MODULE_PROVISION_RESOLVED:
            validate_provision_resolved_payload(payload)
        else:
            raise ValueError(f"不是 module 事件：{event_type!r}")
        with self._cond:
            self._queue.append((event_type, dict(payload)))

    def pending(self) -> int:
        with self._cond:
            return len(self._queue)

    def drain(self) -> None:
        """送出佇列內所有事件；已有 drainer 在送時立即返回（它會把新入佇列的一併送完）。不得在持有 coordinator 鎖時呼叫。"""
        with self._cond:
            if self._draining:
                return
            self._draining = True
        try:
            while True:
                with self._cond:
                    if not self._queue:
                        self._draining = False
                        self._cond.notify_all()
                        return
                    event_type, payload = self._queue.popleft()
                try:
                    self._sink(event_type, payload)
                    self.published += 1
                except Exception as exc:  # noqa: BLE001  不回滾狀態，靠 resync 補償；只記例外型別
                    self.failed += 1
                    logger.error("模組事件發布失敗（%s）：%s", event_type, type(exc).__name__)
        except BaseException:
            with self._cond:
                self._draining = False
                self._cond.notify_all()
            raise

    def flush(self, timeout: float = 5.0) -> bool:
        """等佇列送空；回傳是否在時限內送完。"""
        end = time.monotonic() + timeout
        while True:
            self.drain()
            with self._cond:
                if not self._queue and not self._draining:
                    return True
                remaining = end - time.monotonic()
                if remaining <= 0:
                    return False
                self._cond.wait(min(remaining, 0.05))
