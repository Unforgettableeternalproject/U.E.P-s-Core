"""信封驗證（E-3、E-6、VC-1-3 的信封部分）與 Bus（D22）；不需要資料庫。"""
import threading

import pytest
from pydantic import ValidationError

from uep.events.bus import EventBus
from uep.events.envelope import (
    EventDraft,
    ScopeWideningError,
    TrustedContext,
    build_event,
    scope_rank,
)
from uep.events.registry import (
    DeliveryClass,
    EventRegistry,
    Priority,
    UnregisteredEventType,
)
from uep.ids import is_valid_id


def _trusted(**kw):
    base = {"source": "adapter.website", "actor_id": "guest_1", "scope_ref": "scope_public", "external": True}
    base.update(kw)
    return TrustedContext(**base)


def _event(event_type="input.text_final", **kw):
    draft = EventDraft(event_type=event_type, idempotency_key=kw.pop("idempotency_key", "k1"), **kw)
    return build_event(draft, _trusted())


def test_build_event_fills_trusted_fields():
    event = _event()
    assert is_valid_id(event.event_id, "evt")
    assert is_valid_id(event.trace_id, "trc")
    assert event.delivery_class is DeliveryClass.INTERACTION
    assert event.source == "adapter.website" and event.actor_id == "guest_1"
    assert event.persona_id == "uep"


def test_spoofed_fields_in_payload_are_ignored():
    """VC-1-3：payload 夾帶 priority／actor_id 不生效，只當資料保留。"""
    event = _event(payload={"priority": "urgent", "actor_id": "owner"})
    assert event.priority is Priority.NORMAL
    assert event.actor_id == "guest_1"
    assert event.payload == {"priority": "urgent", "actor_id": "owner"}


@pytest.mark.parametrize("field", ["priority", "actor_id", "scope_ref", "source", "delivery_class", "event_id"])
def test_draft_cannot_carry_trusted_fields(field):
    with pytest.raises(ValidationError):
        EventDraft(event_type="input.text_final", **{field: "x"})


def test_unregistered_event_type_rejected():
    with pytest.raises(UnregisteredEventType):
        build_event(EventDraft(event_type="made.up", idempotency_key="k"), _trusted())


def test_registry_extension():
    registry = EventRegistry({})
    with pytest.raises(UnregisteredEventType):
        registry.lookup("task.created")


def test_external_source_requires_idempotency_key():
    with pytest.raises(ValueError):
        build_event(EventDraft(event_type="input.text_final"), _trusted())
    # 內部事件不強制
    build_event(EventDraft(event_type="core.started"), _trusted(external=False))


def test_aggregate_fields_all_or_none():
    with pytest.raises(ValueError):
        build_event(EventDraft(event_type="task.created", aggregate_id="task_x"), _trusted(external=False))


def test_scope_only_narrows():
    """E-6：衍生事件 scope 不得比起因事件寬。"""
    assert scope_rank("scope_private_owner") < scope_rank("scope_group_g1") < scope_rank("scope_public")
    _trusted(external=False, scope_ref="scope_private_owner", parent_scope_ref="scope_group_g1")
    with pytest.raises((ScopeWideningError, ValidationError)):
        _trusted(external=False, scope_ref="scope_public", parent_scope_ref="scope_private_owner")


def test_bad_scope_ref_rejected():
    with pytest.raises(ValueError):
        scope_rank("everyone")


def test_event_id_must_be_ulid_format():
    event = _event()
    with pytest.raises(ValidationError):
        type(event)(**{**event.model_dump(), "event_id": "evt_1761436800000"})


# ---- Bus ----

def test_bus_subscribe_by_str_sync_dispatch():
    bus, got = EventBus(), []
    bus.subscribe("input.text_final", got.append)
    bus.publish(_event(), sync=True)
    bus.publish(_event("core.started", idempotency_key="k2"), sync=True)  # 無訂閱者
    assert [e.event_type for e in got] == ["input.text_final"]


def test_bus_unsubscribe():
    bus, got = EventBus(), []
    bus.subscribe("input.text_final", got.append)
    bus.unsubscribe("input.text_final", got.append)
    bus.unsubscribe("input.text_final", got.append)  # 重複取消不報錯
    bus.publish(_event(), sync=True)
    assert got == []


def test_bus_handler_exception_isolated():
    bus, got = EventBus(), []

    def boom(_):
        raise RuntimeError("x")

    bus.subscribe("input.text_final", boom)
    bus.subscribe("input.text_final", got.append)
    bus.publish(_event(), sync=True)
    assert len(got) == 1


def test_bus_async_dispatch_on_consumer_thread():
    bus, done, seen = EventBus(), threading.Event(), {}

    def handler(event):
        seen["thread"] = threading.current_thread().name
        done.set()

    bus.subscribe("input.text_final", handler)
    bus.start()
    bus.start()  # 冪等
    try:
        bus.publish(_event())
        assert done.wait(timeout=5)
    finally:
        bus.stop()
    assert seen["thread"] == "UepEventBus"


def test_bus_public_surface_is_narrow():
    public = {n for n in dir(EventBus) if not n.startswith("_")}
    assert public == {"subscribe", "unsubscribe", "publish", "start", "stop", "get_stats"}
