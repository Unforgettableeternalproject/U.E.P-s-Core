"""控制訊號的 journal 補記（E-12）：送出後才在背景寫入，永不阻塞送達。

control 事件的 delivery_class 為 control，不走 ``EventPublisher``（它只收 durable／interaction）；
這裡直接用 Gateway 重建後 append 進 journal，不寫 outbox。控制訊號本身不含私人內容，
故以 ``scope_public`` 記錄；以 ``control_id`` 為 idempotency_key，重送不會多記。
"""
from __future__ import annotations

import logging
import queue
import threading
from dataclasses import dataclass

from sqlalchemy.orm import Session, sessionmaker

from uep.events import journal
from uep.events.envelope import FENCE_KEYS, EventDraft, TrustedContext, build_event
from uep.output.intent import ControlSignal

logger = logging.getLogger("uep.control.journal")

SOURCE = "core.control"
ACTOR = "uep:control_channel"
_STOP = object()


@dataclass(frozen=True)
class _Job:
    signal: ControlSignal
    endpoints: tuple[str, ...]


class ControlJournal:
    def __init__(
        self, session_factory: sessionmaker[Session], *, persona_id: str = "uep", max_pending: int = 10000
    ) -> None:
        self._sf = session_factory
        self._persona_id = persona_id
        self._queue: queue.Queue[object] = queue.Queue(maxsize=max_pending)
        self.dropped = 0
        self._thread = threading.Thread(target=self._run, daemon=True, name="UepControlJournal")
        self._thread.start()
        self.written = 0
        self.failed = 0

    def submit(self, signal: ControlSignal, endpoints: tuple[str, ...]) -> None:
        """非阻塞：只入佇列，由背景執行緒寫入；佇列滿時丟棄補記並計數（不阻塞送達）。"""
        try:
            self._queue.put_nowait(_Job(signal, endpoints))
        except queue.Full:
            self.dropped += 1
            logger.error("控制訊號 journal 佇列已滿，捨棄補記（累計 %d）", self.dropped)

    def flush(self, timeout: float = 5.0) -> bool:
        """等待已提交的工作寫完（供測試與關機使用）；回傳是否在期限內完成。"""
        done = threading.Event()
        try:
            self._queue.put(done, timeout=timeout)
        except queue.Full:
            return False
        return done.wait(timeout)

    def close(self, timeout: float = 5.0) -> None:
        try:
            self._queue.put(_STOP, timeout=timeout)
        except queue.Full:
            return
        self._thread.join(timeout)

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is _STOP:
                return
            if isinstance(item, threading.Event):
                item.set()
                continue
            try:
                self._write(item)  # type: ignore[arg-type]
                self.written += 1
            except Exception:
                self.failed += 1
                logger.exception("控制訊號補記 journal 失敗")

    def _write(self, job: _Job) -> None:
        signal = job.signal
        draft = EventDraft(
            event_type=f"control.{signal.kind.value}",
            occurred_at=signal.issued_at,
            idempotency_key=signal.control_id,
            fence={k: v for k, v in signal.target_fence.items() if k in FENCE_KEYS},
            payload={
                "kind": signal.kind.value,
                "scope": signal.scope.value,
                "control_id": signal.control_id,
                "count": len(job.endpoints),
            },
        )
        trusted = TrustedContext(
            source=SOURCE, actor_id=ACTOR, scope_ref="scope_public", persona_id=self._persona_id
        )
        event = build_event(draft, trusted)
        with self._sf() as session, session.begin():
            journal.append(session, event)
