"""程序內 Event Bus。

由 ``core/event_bus.py`` 複製而來（A4／A7，不 import legacy）。語意沿用：
單一消費者線程分發、handler 例外隔離。與 legacy 的差異：
- 訂閱鍵由 ``SystemEvent`` enum 改為 ``str``（D22，即 ``UEPEvent.event_type``）。
- 對外介面收斂為 subscribe／unsubscribe／publish／start／stop／get_stats（P3 要橋接
  Echo Stream 的 AsyncIterator，故不散出其他 API）。
- 不內建歷史與全域單例；落 journal 屬 ``EventPublisher``，Bus 只負責分發。

契約 §8.4（T-23）：
- 佇列有上限（``max_queue``）。佇列滿時的行為是**背壓**（非消費者線程的 ``publish`` 最多等
  ``put_timeout`` 秒）→ 仍滿則**丟棄本次事件並記錄**（error log 帶 event_id／journal_seq，``get_stats`` 的
  ``dropped``／``dropped_by_type`` 累計），絕不靜默丟棄。
- **丟棄的保證範圍（如實）**：Bus 是 best-effort 通知（E-13）。被丟棄的 durable／interaction 事件只保證
  **仍留在 journal**，需要不遺漏的消費者應以 ``JournalTailer`` 重建，或走 outbox target；Bus 本身沒有任何
  自動補送。目前自動補償只涵蓋 Presence 投影（``PresenceMaintenance`` 每個 tick 重新投影 State 的 Presence
  軸）；Health 軸等其他只靠 Bus 訂閱的消費者，丟棄後會停在舊值直到下一個事件。
- **消費者線程內 publish 的風險**：handler 在消費者線程內 publish 時不等待（等待會自我死結），佇列滿就直接
  丟棄。新增會在 handler 內衍生事件的訂閱者時，要把這個丟棄風險算進去（衍生事件不可是唯一依據）。
- ``stop()`` 先排空佇列（逾時 ``drain_timeout``），逾時後記錄並累計未處理數 ``undelivered_on_stop``；
  stop 之後（尚未再 ``start``）的非消費者 publish 直接計入 ``dropped``，不等待。
- ``stop()`` 逾時後舊消費者線程若仍存活（handler 卡住），``start()`` 拋 ``RuntimeError`` 拒絕啟動，避免雙消費者；
  舊線程結束後即可重新啟動。
- handler 例外只記型別名稱，不記訊息與 traceback。
- stats 在 ``_stats_lock`` 內更新。
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from queue import Empty, Full, Queue

from uep.events.envelope import UEPEvent

logger = logging.getLogger("uep.events.bus")

Handler = Callable[[UEPEvent], None]


DEFAULT_MAX_QUEUE = 10_000
DEFAULT_PUT_TIMEOUT = 0.5  # 佇列滿時的背壓等待秒數
DEFAULT_DRAIN_TIMEOUT = 5.0  # stop() 排空佇列的最長秒數


class EventBus:
    def __init__(
        self,
        *,
        max_queue: int = DEFAULT_MAX_QUEUE,
        put_timeout: float = DEFAULT_PUT_TIMEOUT,
        drain_timeout: float = DEFAULT_DRAIN_TIMEOUT,
    ) -> None:
        if max_queue < 1:
            raise ValueError("max_queue 必須 >= 1")
        self._handlers: dict[str, list[Handler]] = {}
        self._lock = threading.Lock()
        self._queue: Queue[UEPEvent] = Queue(maxsize=max_queue)
        self._max_queue = max_queue
        self._put_timeout = put_timeout
        self._drain_timeout = drain_timeout
        self._thread: threading.Thread | None = None
        self._orphan: threading.Thread | None = None  # stop() 逾時後仍存活的舊消費者線程
        self._stop_event = threading.Event()
        self._abort_event = threading.Event()  # 排空逾時後通知消費者線程立即結束
        self._stopped = False  # stop() 之後、下次 start() 之前為 True：publish 直接丟棄
        self._stats_lock = threading.Lock()
        self._stats = {
            "published": 0, "dispatched": 0, "handler_errors": 0, "dropped": 0, "undelivered_on_stop": 0,
        }
        self._dropped_by_type: dict[str, int] = {}

    def start(self) -> None:
        """啟動事件處理線程（冪等）。"""
        if self._thread and self._thread.is_alive():
            return
        if self._orphan is not None:
            if self._orphan.is_alive():
                raise RuntimeError("舊的事件處理線程仍存活（stop() 排空逾時），拒絕啟動以免雙消費者")
            self._orphan = None
        self._stop_event.clear()
        self._abort_event.clear()
        self._stopped = False
        self._thread = threading.Thread(target=self._run, daemon=True, name="UepEventBus")
        self._thread.start()

    def stop(self) -> None:
        """停止事件處理線程：先排空佇列（逾時 ``drain_timeout``），逾時後記錄並累計未處理數。"""
        if not self._thread:
            return
        self._stopped = True
        self._stop_event.set()  # 消費者線程在佇列清空後才會結束
        self._thread.join(timeout=self._drain_timeout)
        if self._thread.is_alive():
            self._abort_event.set()
            self._thread.join(timeout=2.0)  # 等正在執行的 handler 收尾
            remaining = self._queue.qsize()
            with self._stats_lock:
                self._stats["undelivered_on_stop"] += remaining
            logger.error("事件處理線程排空逾時：尚有 %d 個事件未處理", remaining)
            if self._thread.is_alive():
                logger.error("事件處理線程未能正常結束；在它結束前 start() 會拒絕啟動")
                self._orphan = self._thread
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
        """同步則於呼叫線程分發；否則入佇列由消費者線程分發。

        佇列滿：非消費者線程最多背壓等待 ``put_timeout``，仍滿則丟棄本次事件並記錄（見模組說明）。
        """
        with self._stats_lock:
            self._stats["published"] += 1
        if sync:
            self._dispatch(event)
            return
        on_consumer = threading.current_thread() is self._thread
        if self._stopped and not on_consumer:
            self._record_drop(event, "Event Bus 已停止")
            return
        try:
            if on_consumer:
                self._queue.put_nowait(event)
            else:
                self._queue.put(event, timeout=self._put_timeout)
        except Full:
            self._record_drop(event, "Event Bus 佇列已滿")

    def _record_drop(self, event: UEPEvent, why: str) -> None:
        with self._stats_lock:
            self._stats["dropped"] += 1
            self._dropped_by_type[event.event_type] = self._dropped_by_type.get(event.event_type, 0) + 1
        logger.error(
            "%s，丟棄事件（事件仍在 journal，消費者可用 JournalTailer 重建；Bus 不自動補送）："
            "event_id=%s type=%s journal_seq=%s",
            why, event.event_id, event.event_type, event.journal_seq,
        )

    def get_stats(self) -> dict[str, object]:
        """統計快照：published／dispatched／handler_errors／dropped／undelivered_on_stop／queue_size／max_queue。"""
        with self._stats_lock:
            snapshot: dict[str, object] = dict(self._stats)
            snapshot["dropped_by_type"] = dict(self._dropped_by_type)
        snapshot["queue_size"] = self._queue.qsize()
        snapshot["max_queue"] = self._max_queue
        return snapshot

    def _run(self) -> None:
        while not self._abort_event.is_set():
            try:
                event = self._queue.get(timeout=0.2)
            except Empty:
                if self._stop_event.is_set():
                    return  # 已要求停止且佇列已排空
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
        errors = 0
        for handler in handlers:
            try:
                handler(event)
            except Exception as exc:  # noqa: BLE001  隔離 handler 例外，只記型別
                errors += 1
                # 只記例外型別，不記訊息與 traceback（可能夾帶私人內容；與 Outbox 一致）
                logger.error("事件處理器錯誤（%s）：%s", event.event_type, type(exc).__name__)
        with self._stats_lock:
            self._stats["dispatched"] += 1
            self._stats["handler_errors"] += errors
