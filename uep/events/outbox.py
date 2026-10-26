"""Outbox 與 Dispatcher（契約 §1.2、E-2）。

- ``enqueue`` 在呼叫者的交易內寫入，與觸發它的狀態變更一起提交。
- ``OutboxDispatcher`` 至少一次投遞：handler 成功才標 ``acked_at``；崩潰於 ACK 前則下次重送，
  由 consumer 的 Inbox 去重。失敗以指數退避＋jitter 重試，超過上限進 dead letter。
- 不承諾 exactly-once。
- 錯誤只記例外型別（截斷），不存或輸出例外訊息全文，避免私人字串經由例外進到 DB／log。
"""
from __future__ import annotations

import logging
import random
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from uep.events.envelope import UEPEvent
from uep.events.journal import row_to_event
from uep.events.models import EventRow, OutboxRow
from uep.ids import new_id

logger = logging.getLogger("uep.events.outbox")

OutboxHandler = Callable[[UEPEvent], None]

MAX_ERROR_CHARS = 200


def enqueue(session: Session, event_id: str, target: str) -> str:
    """於呼叫者交易內加入一筆待投遞；回傳 outbox_id。"""
    outbox_id = new_id("obx")
    session.add(OutboxRow(outbox_id=outbox_id, event_id=event_id, target=target))
    session.flush()
    return outbox_id


@dataclass
class DispatchStats:
    delivered: int = 0
    failed: int = 0
    dead_lettered: int = 0


class OutboxDispatcher:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        max_attempts: int = 5,
        base_delay: timedelta = timedelta(seconds=1),
        max_delay: timedelta = timedelta(seconds=60),
        jitter: float = 0.2,
    ) -> None:
        self._session_factory = session_factory
        self._handlers: dict[str, OutboxHandler] = {}
        self._max_attempts = max_attempts
        self._base_delay = base_delay
        self._max_delay = max_delay
        self._jitter = jitter

    def register(self, target: str, handler: OutboxHandler) -> None:
        self._handlers[target] = handler

    def _backoff(self, attempts: int) -> timedelta:
        delay = min(self._max_delay, self._base_delay * (2 ** max(attempts - 1, 0)))
        factor = 1 + random.uniform(-self._jitter, self._jitter)
        return delay * factor

    @staticmethod
    def _due_conditions(targets: list[str]):
        return (
            OutboxRow.acked_at.is_(None),
            OutboxRow.dead_letter_reason.is_(None),
            OutboxRow.target.in_(targets),
            or_(OutboxRow.retry_at.is_(None), OutboxRow.retry_at <= func.now()),
        )

    def dispatch_once(self, limit: int = 50) -> DispatchStats:
        """處理最多 limit 筆到期的 outbox。

        每批先查出一次 id 清單，每筆最多處理一次；每筆各自一個交易，以 SKIP LOCKED 與其他
        dispatcher 互斥。沒有註冊 handler 的 target 不取列，保持 pending、不耗 attempts。

        注意：handler 在持有該列鎖的交易內執行。handler 不應做長時間操作或占滿同一個連線池；
        lease 模型留待 P2。
        """
        stats = DispatchStats()
        targets = list(self._handlers)
        if not targets:
            return stats
        with self._session_factory() as session:
            ids = list(
                session.scalars(
                    select(OutboxRow.outbox_id)
                    .where(*self._due_conditions(targets))
                    .order_by(OutboxRow.seq)
                    .limit(limit)
                )
            )
        for outbox_id in ids:
            with self._session_factory() as session, session.begin():
                row = session.execute(
                    select(OutboxRow)
                    .where(OutboxRow.outbox_id == outbox_id, *self._due_conditions(targets))
                    .with_for_update(skip_locked=True)
                ).scalar_one_or_none()
                if row is None:  # 已被其他 dispatcher 處理或鎖住
                    continue
                self._deliver(session, row, stats)
        return stats

    def _deliver(self, session: Session, row: OutboxRow, stats: DispatchStats) -> None:
        error: str | None = None
        handler = self._handlers[row.target]
        event_row = session.get(EventRow, row.event_id)
        try:
            handler(row_to_event(event_row))
        except Exception as exc:  # noqa: BLE001  handler 例外隔離；只記型別，不記訊息
            error = type(exc).__name__[:MAX_ERROR_CHARS]
            logger.error(
                "outbox 投遞失敗：event_id=%s target=%s error=%s", row.event_id, row.target, error
            )

        row.attempts += 1
        if error is None:
            row.acked_at = func.now()
            row.last_error = None
            stats.delivered += 1
        elif row.attempts >= self._max_attempts:
            row.dead_letter_reason = error
            row.last_error = error
            stats.dead_lettered += 1
        else:
            row.last_error = error
            row.retry_at = func.now() + self._backoff(row.attempts)
            stats.failed += 1
