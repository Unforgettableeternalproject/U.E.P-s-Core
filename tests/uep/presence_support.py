"""CC-4 測試共用 helper（需 PostgreSQL；由各 test_presence_*.py 匯入）。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

from tests.uep.factories import NOW, make_intent
from uep.contracts.presence import AuthorizationTuple
from uep.events.bus import EventBus
from uep.events.models import EventRow, OutboxRow
from uep.events.publisher import EventPublisher
from uep.output.intent import OutputIntent
from uep.presence.epoch import HostEpochStore
from uep.presence.gate import ALLOW_ALL, OutputGate
from uep.presence.manager import PresenceManager


class FakeClock:
    """可注入、可推進的 clock（tz-aware）；起點與 factories.NOW 相同，intent 的 valid_until 才合理。"""

    def __init__(self, start: datetime = NOW) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@dataclass
class Rig:
    manager: PresenceManager
    gate: OutputGate
    clock: FakeClock
    store: HostEpochStore
    publisher: EventPublisher
    bus: EventBus
    session_factory: Any


def make_rig(session_factory, tmp_path: Path, *, control=None, **gate_kw: Any) -> Rig:
    clock = FakeClock()
    store = HostEpochStore(tmp_path / "host_epoch.json")
    bus = EventBus()
    publisher = EventPublisher(session_factory, bus)
    manager = PresenceManager(
        session_factory, epoch_store=store, publisher=publisher, control=control, clock=clock
    )
    manager.start()
    gate_kw.setdefault("turn_generation_source", ALLOW_ALL)
    gate_kw.setdefault("presence_audience", ALLOW_ALL)
    gate = OutputGate(session_factory, manager, clock=clock, publisher=publisher, **gate_kw)
    return Rig(manager, gate, clock, store, publisher, bus, session_factory)


def activate(rig: Rig, endpoint_id: str = "ep_desktop", location: str = "desktop") -> AuthorizationTuple:
    """acquire + confirm_activated，回傳 ACTIVE 的授權六元組。"""
    outcome = rig.manager.acquire(endpoint_id, location)
    assert outcome.grant is not None, outcome.result
    rig.manager.confirm_activated(outcome.grant.lease_id)
    auth = rig.manager.authorization()
    assert auth is not None
    return auth


def intent_for(auth: AuthorizationTuple, **overrides: Any) -> OutputIntent:
    data: dict[str, Any] = {
        "persona_id": auth.persona_id, "endpoint_id": auth.endpoint_id, "lease_id": auth.lease_id,
        "fencing_epoch": auth.fencing_epoch, "core_epoch": auth.core_epoch,
        "presence_generation": auth.presence_generation,
    }
    data.update(overrides)
    return make_intent(**data)


def count_events(session_factory, event_type: str) -> int:
    with session_factory() as s:
        return s.scalar(select(func.count()).select_from(EventRow).where(EventRow.event_type == event_type)) or 0


def count_outbox(session_factory) -> int:
    with session_factory() as s:
        return s.scalar(select(func.count()).select_from(OutboxRow)) or 0
