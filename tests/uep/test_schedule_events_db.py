"""PP-4：schedule.* 事件寫入真的 journal（需要 UEP_TEST_DATABASE_URL；用真的 EventPublisher）。

重點：plan_changed 進 journal，而且與 Presence／State 共用的 ``(aggregate_id, aggregate_version)`` 唯一鍵互不影響。
"""
from datetime import datetime, time
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from uep.contracts.schedule import ActivityKind, PlanItem
from uep.events.bus import EventBus
from uep.events.envelope import EventDraft, TrustedContext
from uep.events.models import EventRow
from uep.events.publisher import EventPublisher
from uep.schedule import resolver
from uep.schedule.events import ScheduleEvents

TZ = "Asia/Taipei"


@pytest.fixture
def publisher(session_factory):
    return EventPublisher(session_factory, EventBus())


def rows(session_factory, event_type):
    with session_factory() as s:
        return list(s.scalars(select(EventRow).where(EventRow.event_type == event_type)))


def presence_event(publisher, version):
    draft = EventDraft(event_type="presence.granted", aggregate_type="presence", aggregate_id="uep",
                       aggregate_version=version, payload={"status": "ACTIVE", "reason_code": "test"})
    trusted = TrustedContext(source="core.presence", actor_id="uep:presence_manager", scope_ref="scope_private_owner")
    return publisher.publish(draft, trusted)


def test_plan_changed_reaches_journal_and_is_idempotent_per_revision(session_factory, publisher):
    events = ScheduleEvents(publisher, persona_id="uep")
    events.plan_changed(4, "item_created", "plan_x")
    events.plan_changed(4, "observed", None)  # Planner 補發：同一 revision 的 idempotency key 去重
    [row] = rows(session_factory, "schedule.plan_changed")
    assert (row.source, row.aggregate_id, row.aggregate_version) == ("core.schedule", "uep:schedule", 4)
    assert row.idempotency_key == "plan_changed:4"
    assert row.payload["revision"] == 4 and row.payload["change_kind"] == "item_created"
    events.plan_changed(5, "item_updated", "plan_x")
    assert len(rows(session_factory, "schedule.plan_changed")) == 2


def test_presence_events_with_same_version_are_unaffected(session_factory, publisher):
    events = ScheduleEvents(publisher, persona_id="uep")
    presence_event(publisher, 1)
    events.plan_changed(1, "item_created", "plan_x")  # 同樣的 version 數字，但 aggregate_id 不同命名空間
    presence_event(publisher, 2)
    events.plan_changed(2, "item_updated", "plan_x")
    assert len(rows(session_factory, "presence.granted")) == 2
    assert len(rows(session_factory, "schedule.plan_changed")) == 2


def test_planner_events_without_aggregate_never_collide(session_factory, publisher):
    events = ScheduleEvents(publisher, persona_id="uep")
    item = PlanItem("plan_x", ActivityKind.WORK, "discord", "weekly", weekdays=(1, 2, 3, 4, 5, 6, 7),
                    local_start=time(9), local_end=time(10), tz=TZ)
    slot = resolver.resolve([item], None, datetime(2026, 11, 12, 9, 30, tzinfo=ZoneInfo(TZ)),
                            TZ, plan_revision=1)
    presence_event(publisher, 1)
    for _ in range(3):  # 相同 plan_revision 重複發 slot_changed 也不會撞唯一鍵
        events.slot_changed(slot)
        events.slot_entered(slot, "boundary")
    changed = rows(session_factory, "schedule.slot_changed")
    assert len(changed) == 3 and all(r.aggregate_id is None and r.aggregate_version is None for r in changed)
    assert len(rows(session_factory, "schedule.slot_entered")) == 3
    assert len(rows(session_factory, "presence.granted")) == 1
