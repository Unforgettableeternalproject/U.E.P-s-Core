"""ControlChannel 協定與程序內實作。

控制訊號（stop／abort／revoke）走獨立高優先通道，不排在輸出資料、outbox 或 Bus queue 後面（P-8）：
- 每個端點有自己的信箱；信箱內 stop／abort／revoke 各有獨立的 lane（各自的佇列與執行緒）。
  ``send`` 只把訊號放進對應 lane 並喚醒，不執行 handler、不碰 DB。因此 stop 的 handler 卡住時，
  revoke／abort 仍立即送達；一個端點的 handler 卡住也不影響 ``send`` 的呼叫者與其他端點。
- **handler 不得阻塞，只設旗標／喚醒事件**（例如設 stop flag、解鎖 writer）。不同 kind 的 handler 可能
  在不同執行緒上並行呼叫，須自行保證執行緒安全。同一 kind 的訊號對同一端點依序處理。
- ``control_id`` 為冪等鍵；同一端點重複收到同一 ``control_id`` 只處理一次。
- 同一 ``endpoint_id`` 重新訂閱（重連）會取代舊訂閱；舊信箱未處理的訊號不會被新連線撿回，
  但 ``send`` 遇到已被取代的信箱會重新解析目前信箱並重試一次。
"""
from __future__ import annotations

import logging
import threading
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from uep.control.journal import ControlJournal
from uep.output.intent import ControlKind, ControlSignal

logger = logging.getLogger("uep.control.channel")

ControlHandler = Callable[[ControlSignal], None]
_SEEN_LIMIT = 4096


@dataclass(frozen=True)
class ControlSendResult:
    control_id: str
    delivered_to: tuple[str, ...]  # 已放入信箱（或先前已收過）的端點；未訂閱或信箱已關閉者不在其中
    duplicate: bool  # 有目標且所有目標端點先前都已收過此 control_id


class ControlSubscription(Protocol):
    endpoint_id: str

    def close(self) -> None: ...


class ControlChannel(Protocol):
    def subscribe(self, endpoint_id: str, handler: ControlHandler) -> ControlSubscription:
        """端點以 endpoint_id 註冊 handler；handler 在端點專屬執行緒上呼叫，不得阻塞（只設旗標）。"""
        ...

    def send(self, signal: ControlSignal, *, endpoint_id: str | None = None) -> ControlSendResult:
        """送出給指定端點；``endpoint_id`` 為 None 表示廣播給所有已訂閱端點。不阻塞。"""
        ...


class _Put(Enum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    CLOSED = "closed"


class _Lane:
    """單一 kind 的佇列與專屬執行緒。"""

    def __init__(self, name: str, handler: ControlHandler) -> None:
        self._handler = handler
        self._items: deque[ControlSignal] = deque()
        self._cond = threading.Condition()
        self._closed = False
        self._thread = threading.Thread(target=self._run, daemon=True, name=name)
        self._thread.start()

    def put(self, signal: ControlSignal) -> None:
        with self._cond:
            self._items.append(signal)
            self._cond.notify()

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._items.clear()
            self._cond.notify_all()

    def _run(self) -> None:
        while True:
            with self._cond:
                while not self._items and not self._closed:
                    self._cond.wait()
                if self._closed:
                    return
                signal = self._items.popleft()
            try:
                self._handler(signal)
            except Exception:
                logger.exception("控制訊號 handler 錯誤（%s）", signal.kind.value)


class _Mailbox:
    def __init__(self, endpoint_id: str, handler: ControlHandler, on_close: Callable[[_Mailbox], None]):
        self.endpoint_id = endpoint_id
        self._on_close = on_close
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._lock = threading.Lock()
        self._closed = False
        self._lanes = {
            kind: _Lane(f"UepControl[{endpoint_id}:{kind.value}]", handler) for kind in ControlKind
        }

    def put(self, signal: ControlSignal) -> _Put:
        with self._lock:
            if self._closed:
                return _Put.CLOSED
            if signal.control_id in self._seen:
                return _Put.DUPLICATE
            self._seen[signal.control_id] = None
            if len(self._seen) > _SEEN_LIMIT:
                self._seen.popitem(last=False)
            self._lanes[signal.kind].put(signal)
        return _Put.ACCEPTED

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        for lane in self._lanes.values():
            lane.close()
        self._on_close(self)


class _Subscription:
    def __init__(self, mailbox: _Mailbox) -> None:
        self._mailbox = mailbox
        self.endpoint_id = mailbox.endpoint_id

    def close(self) -> None:
        self._mailbox.close()


class InProcessControlChannel:
    """程序內控制通道；可選的 ``journal`` 於送出後在背景補記，不阻塞送達。"""

    def __init__(self, journal: ControlJournal | None = None) -> None:
        self._journal = journal
        self._lock = threading.Lock()
        self._mailboxes: dict[str, _Mailbox] = {}

    def subscribe(self, endpoint_id: str, handler: ControlHandler) -> ControlSubscription:
        if not endpoint_id:
            raise ValueError("endpoint_id 不可為空")
        with self._lock:
            old = self._mailboxes.get(endpoint_id)
            mailbox = _Mailbox(endpoint_id, handler, self._forget)
            self._mailboxes[endpoint_id] = mailbox
        if old is not None:
            old.close()  # 重連取代舊訂閱
        return _Subscription(mailbox)

    def _current(self, endpoint_id: str) -> _Mailbox | None:
        with self._lock:
            return self._mailboxes.get(endpoint_id)

    def _deliver(self, endpoint_id: str, box: _Mailbox | None, signal: ControlSignal) -> _Put:
        """放入信箱；信箱已被取代（關閉）時重新解析目前信箱並重試一次，避免重連競態漏送。"""
        for attempt in range(2):
            if box is None:
                return _Put.CLOSED
            outcome = box.put(signal)
            if outcome is not _Put.CLOSED:
                return outcome
            if attempt == 0:
                box = self._current(endpoint_id)
        return _Put.CLOSED

    def send(self, signal: ControlSignal, *, endpoint_id: str | None = None) -> ControlSendResult:
        with self._lock:
            if endpoint_id is None:
                targets = list(self._mailboxes.items())
            else:
                targets = [(endpoint_id, self._mailboxes.get(endpoint_id))]
        delivered: list[str] = []
        duplicates = 0
        for target_id, box in targets:
            outcome = self._deliver(target_id, box, signal)
            if outcome is _Put.CLOSED:
                continue
            delivered.append(target_id)
            if outcome is _Put.DUPLICATE:
                duplicates += 1
        result = ControlSendResult(
            control_id=signal.control_id,
            delivered_to=tuple(delivered),
            duplicate=bool(delivered) and duplicates == len(delivered),
        )
        if self._journal is not None:
            self._journal.submit(signal, result.delivered_to)  # 送出之後才補記
        return result

    def _forget(self, mailbox: _Mailbox) -> None:
        with self._lock:
            if self._mailboxes.get(mailbox.endpoint_id) is mailbox:
                del self._mailboxes[mailbox.endpoint_id]
