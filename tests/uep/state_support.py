"""CC-7／全域停止測試共用 helper（需 PostgreSQL；由 test_state_*、test_global_stop*、test_assembly* 匯入）。"""
from __future__ import annotations

import queue
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import SecretStr
from sqlalchemy import select

from tests.uep.presence_support import FakeClock
from uep.control.channel import InProcessControlChannel
from uep.events.bus import EventBus
from uep.events.envelope import UEPEvent
from uep.events.journal import row_to_event
from uep.events.models import EventRow
from uep.events.publisher import EventPublisher
from uep.output.intent import ControlSignal
from uep.presence.epoch import HostEpochStore
from uep.presence.manager import PresenceManager
from uep.state.coordinator import Axis, StateCoordinator
from uep.stop.service import GlobalStop, make_flag_blocker

TOKEN = "test-stop-token"
# 測試用提議者名單（S-1 fail-closed：沒列在這裡的提議者一律被拒）
PROPOSERS = {
    Axis.INTERACTION: frozenset({"nlp", "proactive", "workflow", *(f"p{i}" for i in range(16))}),
    Axis.BEHAVIOR: frozenset({"nlp", "proactive", "workflow", "policy"}),
}
WAIT = 3.0


class Collector:
    """端點 handler：只把控制訊號放進佇列（handler 不得阻塞）。"""

    def __init__(self) -> None:
        self.q: queue.Queue[ControlSignal] = queue.Queue()

    def __call__(self, signal: ControlSignal) -> None:
        self.q.put(signal)

    def get(self, timeout: float = WAIT) -> ControlSignal:
        return self.q.get(timeout=timeout)

    def empty(self, wait: float = 0.3) -> bool:
        try:
            self.q.get(timeout=wait)
        except queue.Empty:
            return True
        return False


@dataclass
class Stack:
    session_factory: Any
    bus: EventBus
    publisher: EventPublisher
    control: InProcessControlChannel
    manager: PresenceManager
    stop: GlobalStop
    state: StateCoordinator
    clock: FakeClock
    store: HostEpochStore
    tmp_path: Path
    extra: dict[str, Any] = field(default_factory=dict)

    def new_manager(self) -> PresenceManager:
        """模擬 Core 重啟：同一 DB、同一 Host epoch 檔，全新的 PresenceManager（已 start）。"""
        manager = PresenceManager(
            self.session_factory, epoch_store=self.store, publisher=self.publisher, control=self.control,
            clock=self.clock, acquire_guard=make_flag_blocker("uep"),
        )
        manager.start()
        return manager


def make_stack(session_factory, tmp_path: Path, *, start: bool = True, token: str | None = TOKEN) -> Stack:
    clock = FakeClock()
    store = HostEpochStore(tmp_path / "host_epoch.json")
    bus = EventBus()
    publisher = EventPublisher(session_factory, bus)
    control = InProcessControlChannel()
    manager = PresenceManager(
        session_factory, epoch_store=store, publisher=publisher, control=control, clock=clock,
        acquire_guard=make_flag_blocker("uep"),
    )
    if start:
        manager.start()
    stop = GlobalStop(
        session_factory, manager, control, publisher=publisher, clock=clock,
        token=SecretStr(token) if token is not None else SecretStr(""),
    )
    state = StateCoordinator(
        session_factory, publisher, clock=clock, global_stop_check=make_flag_blocker("uep"),
        authorized_proposers=PROPOSERS,
    )
    return Stack(session_factory, bus, publisher, control, manager, stop, state, clock, store, tmp_path)


def activate_endpoint(manager: PresenceManager, endpoint_id: str = "ep_desktop"):
    outcome = manager.acquire(endpoint_id, "desktop")
    assert outcome.grant is not None, outcome.result
    manager.confirm_activated(outcome.grant.lease_id)
    return outcome.grant


def journal_events(session_factory, event_type: str) -> list[UEPEvent]:
    with session_factory() as s:
        rows = s.scalars(select(EventRow).where(EventRow.event_type == event_type).order_by(EventRow.journal_seq))
        return [row_to_event(r) for r in rows]
