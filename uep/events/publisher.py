"""EventPublisher：Gateway 重建 -> journal（＋outbox）-> Bus（E-1）。

- ``record``：在呼叫者的交易內重建信封、落 journal、寫 outbox，不碰 Bus。
  呼叫者提交後再呼叫 ``dispatch``，確保「journal 先於投遞」，回滾時也不會有副作用。
- ``publish``：自開交易的便利組合（record -> commit -> dispatch）。
- P1 範圍只處理 durable／interaction。control 走獨立高優先通道、telemetry 不落 journal，
  皆不在此路徑，收到時拒絕而不是悄悄吞掉。
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy.orm import Session, sessionmaker

from uep.events import journal, outbox
from uep.events.bus import EventBus
from uep.events.envelope import EventDraft, TrustedContext, UEPEvent, build_event
from uep.events.registry import DEFAULT_REGISTRY, DeliveryClass, EventRegistry

_JOURNALED = {DeliveryClass.DURABLE, DeliveryClass.INTERACTION}


class UnsupportedDeliveryClass(ValueError):
    """此路徑不處理 control／telemetry。"""


@dataclass(frozen=True)
class RecordedEvent:
    event: UEPEvent
    duplicate: bool  # 外部重送命中既有事件；此時不重複投遞


class EventPublisher:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        bus: EventBus,
        registry: EventRegistry = DEFAULT_REGISTRY,
    ) -> None:
        self._session_factory = session_factory
        self._bus = bus
        self._registry = registry

    def record(
        self,
        session: Session,
        draft: EventDraft,
        trusted: TrustedContext,
        *,
        outbox_targets: Sequence[str] = (),
    ) -> RecordedEvent:
        """於呼叫者交易內落 journal 與 outbox。失敗直接上拋，Bus 不會收到事件。"""
        event = build_event(draft, trusted, self._registry)
        if event.delivery_class not in _JOURNALED:
            raise UnsupportedDeliveryClass(
                f"{event.event_type} 為 {event.delivery_class.value}，不走 journal 發布路徑"
            )
        result = journal.append(session, event, draft.payload_blob)
        if not result.duplicate:
            for target in outbox_targets:
                outbox.enqueue(session, result.event.event_id, target)
        return RecordedEvent(result.event, result.duplicate)

    def dispatch(self, recorded: RecordedEvent, *, sync: bool = False) -> bool:
        """交易提交後投遞程序內 Bus；重送（duplicate）不再投遞。回傳是否投遞。"""
        if recorded.duplicate:
            return False
        self._bus.publish(recorded.event, sync=sync)
        return True

    def publish(
        self,
        draft: EventDraft,
        trusted: TrustedContext,
        *,
        outbox_targets: Sequence[str] = (),
        sync: bool = False,
    ) -> RecordedEvent:
        with self._session_factory() as session, session.begin():
            recorded = self.record(session, draft, trusted, outbox_targets=outbox_targets)
        self.dispatch(recorded, sync=sync)
        return recorded
