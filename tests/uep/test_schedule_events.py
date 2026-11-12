"""PP-4a：schedule.* 事件登記與發布（不碰 DB，publisher 為替身）。"""
from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

import pytest

from uep.contracts.schedule import ActivityKind, PlanItem
from uep.events.registry import DEFAULT_REGISTRY, SCHEDULE_EVENT_KEYS, DeliveryClass
from uep.schedule import resolver
from uep.schedule.events import SOURCE, ScheduleEvents, plan_change_kind, slot_ref

TZ = "Asia/Taipei"
TPE = ZoneInfo(TZ)
PUBLISHED = {
    "schedule.plan_changed", "schedule.slot_changed", "schedule.slot_entered", "schedule.slot_left",
    "schedule.directive_issued", "schedule.directive_dropped", "schedule.divergence_observed",
}


class FakePublisher:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def publish(self, draft, trusted, **kwargs):
        if self.fail:
            raise RuntimeError("journal down")
        self.sent.append((draft, trusted))


def work_slot():
    item = PlanItem("plan_x", ActivityKind.WORK, "discord", "weekly", weekdays=(1, 2, 3, 4, 5, 6, 7),
                    local_start=time(9), local_end=time(12), tz=TZ)
    return resolver.resolve([item], None, datetime(2026, 11, 12, 10, tzinfo=TPE), TZ, plan_revision=4)


def test_all_schedule_events_are_registered_durable_with_whitelists():
    assert PUBLISHED < set(SCHEDULE_EVENT_KEYS)  # 登記比發布多（override／mark／abandon 先只登記）
    for event_type, keys in SCHEDULE_EVENT_KEYS.items():
        spec = DEFAULT_REGISTRY.lookup(event_type)
        assert spec.delivery_class is DeliveryClass.DURABLE and spec.payload_keys == keys
        assert not {"summary", "expires_at", "note", "text"} & set(keys)  # 不含自由文字


def test_published_events_use_core_schedule_source_and_whitelisted_keys():
    pub = FakePublisher()
    events = ScheduleEvents(pub, clock=lambda: datetime(2026, 11, 12, 2, tzinfo=UTC))
    slot = work_slot()
    events.plan_changed(4, "item_created", "plan_x")
    events.slot_changed(slot)
    events.slot_entered(slot, "boundary")
    events.slot_left(slot, "boundary")
    from uep.contracts.schedule import PlanDirective

    directive = PlanDirective("plan_x@x:1", "host", "plan_boundary", slot, datetime(2026, 11, 12, tzinfo=UTC),
                              datetime(2026, 11, 13, tzinfo=UTC))
    events.directive_issued(directive)
    events.directive_dropped("plan_x@x:1", "slot_ended", datetime(2026, 11, 12, 3, tzinfo=UTC))
    events.divergence_observed("discord", "none", "pending", "povr_1")
    assert {d.event_type for d, _ in pub.sent} == PUBLISHED
    for draft, trusted in pub.sent:
        assert trusted.source == SOURCE == "core.schedule" and not trusted.source.startswith("presence.")
        assert set(draft.payload) <= DEFAULT_REGISTRY.lookup(draft.event_type).payload_keys, draft.event_type
        assert draft.aggregate_type == "schedule"
    by_type = {d.event_type: d for d, _ in pub.sent}
    assert by_type["schedule.plan_changed"].payload == {"revision": 4, "change_kind": "item_created", "ref_id": "plan_x"}
    assert by_type["schedule.slot_changed"].payload["slot_ref"] == slot_ref(slot)
    assert by_type["schedule.slot_entered"].payload["slot_state"] == "normal"
    key = by_type["schedule.directive_issued"].idempotency_key
    assert key.startswith("directive:") and len(key) == len("directive:") + 32  # 同一 directive_id → 同一個 key
    assert "next_eval_at" in by_type["schedule.directive_dropped"].payload


def test_none_values_are_omitted_from_payload():
    pub = FakePublisher()
    ScheduleEvents(pub).divergence_observed("discord", "host", None, None)
    assert pub.sent[0][0].payload == {"plan_location": "discord", "actual_location": "host"}


def test_publish_failure_never_raises():
    ScheduleEvents(FakePublisher(fail=True)).plan_changed(1, "item_created", None)


def test_store_changes_map_to_plan_changed_only_for_plan_kinds():
    pub = FakePublisher()
    events = ScheduleEvents(pub)
    for kind in ("item_created", "item_updated", "item_deleted", "rest_window", "tz_changed", "reconcile"):
        events.on_store_change(3, kind, "ref")
    for kind in ("override_started", "override_activated", "override_ended", "mark_started", "mark_ended"):
        events.on_store_change(4, kind, "ref")  # 這些屬仲裁器階段的事件，這裡不發
    assert [d.payload["change_kind"] for d, _ in pub.sent] == [
        "item_created", "item_updated", "item_deleted", "rest_window", "tz_changed", "reconcile"]
    assert plan_change_kind("override_started") is None


def test_slot_ref_is_stable_and_default_cell_has_anchor():
    slot = work_slot()
    assert slot_ref(slot) == slot_ref(slot) and slot_ref(slot).startswith("plan_x@")
    default = resolver.resolve([], None, datetime(2026, 11, 12, 10, tzinfo=TPE), TZ)
    assert slot_ref(default).startswith("default@")


@pytest.mark.parametrize("event_type", sorted(SCHEDULE_EVENT_KEYS))
def test_registry_lookup(event_type):
    assert DEFAULT_REGISTRY.lookup(event_type).delivery_class is DeliveryClass.DURABLE
