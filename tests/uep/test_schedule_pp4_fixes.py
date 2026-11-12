"""PP-4 審查修正（M1 aggregate 命名空間、M2 peek、m1 override 到期、m4 startup、m5 ttl_expired）。不碰 DB。"""
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from uep.contracts.schedule import ActivityKind, OverrideView, PlanDirective, PlanItem
from uep.schedule import resolver
from uep.schedule.events import ScheduleEvents
from uep.schedule.planner import PresencePlanner, PresenceReading

TZ = "Asia/Taipei"
TPE = ZoneInfo(TZ)


def tpe(h, mi=0, d=12):
    return datetime(2026, 11, d, h, mi, tzinfo=TPE)


class Reader:
    def __init__(self, items):
        self.items, self.override = list(items), None

    def resolve(self, at=None):
        return resolver.resolve(self.items, self.override, at, TZ, plan_revision=3)

    def upcoming(self, start, horizon_s):
        return ()

    def revision(self):
        return 3

    def active_override(self):
        return self.override


class Events:
    def __init__(self):
        self.log = []

    def slot_changed(self, slot):
        self.log.append(("slot_changed",))

    def slot_left(self, slot, reason):
        self.log.append(("slot_left", reason))

    def slot_entered(self, slot, reason):
        self.log.append(("slot_entered", reason))

    def plan_changed(self, revision, change_kind, ref_id=None):
        self.log.append(("plan_changed", change_kind))

    def directive_issued(self, directive):
        self.log.append(("directive_issued", directive.target, directive.reason))

    def directive_dropped(self, directive_id, drop_reason, next_eval_at=None):
        self.log.append(("directive_dropped", drop_reason))

    def divergence_observed(self, *args):
        self.log.append(("divergence_observed", *args))


class FakePublisher:
    def __init__(self):
        self.sent = []

    def publish(self, draft, trusted, **kwargs):
        self.sent.append(draft)


WORK = PlanItem("a", ActivityKind.WORK, "discord", "weekly", weekdays=(1, 2, 3, 4, 5, 6, 7), local_start=time(9),
                local_end=time(10), tz=TZ)


# ---- M1：不佔用共用的 (aggregate_id, aggregate_version) 命名空間 ----

def test_only_plan_changed_has_an_aggregate_and_it_is_namespaced():
    pub = FakePublisher()
    events = ScheduleEvents(pub, persona_id="uep")
    slot = resolver.resolve([WORK], None, tpe(9, 30), TZ, plan_revision=4)
    directive = PlanDirective("d:1", "host", "plan_boundary", slot, tpe(9, 30), tpe(10))
    events.plan_changed(4, "item_created", "plan_x")
    events.slot_changed(slot)
    events.slot_entered(slot, "boundary")
    events.slot_left(slot, "boundary")
    events.directive_issued(directive)
    events.directive_dropped("d:1", "slot_ended")
    events.divergence_observed("discord", "none", None, None)
    by_type = {d.event_type: d for d in pub.sent}
    assert len(by_type) == 7
    for event_type, draft in by_type.items():
        if event_type == "schedule.plan_changed":
            continue
        assert draft.aggregate_id is None and draft.aggregate_version is None and draft.aggregate_type is None, event_type
    plan = by_type["schedule.plan_changed"]
    assert (plan.aggregate_id, plan.aggregate_version, plan.idempotency_key) == ("uep:schedule", 4, "plan_changed:4")
    assert plan.aggregate_id != "uep"  # 不與 Presence／State 用的 persona_id 命名空間重疊


# ---- M2：Planner 只讀 Presence；到期的 ACTIVE 視同不明 ----

def planner_with(reading, items=(WORK,)):
    reader, events = Reader(items), Events()
    holder = [reading]
    return PresencePlanner(reader, lambda: holder[0], events, override_fn=reader.active_override), reader, events, holder


def test_expired_active_lease_is_unknown_and_keeps_startup_marker():  # M2 + m4
    expired = PresenceReading("ACTIVE", "desktop", expires_at=tpe(9, 0))
    planner, _, events, holder = planner_with(expired)
    planner.tick(tpe(9, 30))
    assert events.log == []  # 不發 directive、不記 divergence
    holder[0] = PresenceReading("NONE", None)
    planner.tick(tpe(9, 31))
    assert events.log == [("directive_issued", "discord", "startup_reconcile")]  # 第一次明確讀值才消耗 startup 標記
    planner.tick(tpe(9, 32))
    assert [e[0] for e in events.log] == ["directive_issued"]


def test_valid_active_lease_counts_as_actual_location():
    live = PresenceReading("ACTIVE", "discord", expires_at=tpe(12))
    planner, _, events, _ = planner_with(live)
    planner.tick(tpe(9, 30))
    assert events.log == []  # 已在 discord：沒有差異


# ---- m1：override 判斷看投影，不只看資料列 ----

def test_expired_override_row_does_not_suppress_directives():
    planner, reader, events, _ = planner_with(PresenceReading("NONE", None))
    reader.override = OverrideView("povr_1", "desktop", ActivityKind.WORK, tpe(8, 0), tpe(9, 0), tpe(0, 0, 13),
                                   "active")  # 資料列仍在，但 09:00 已過期（尚未 reconcile）
    planner.tick(tpe(9, 30))
    assert events.log == [("directive_issued", "discord", "startup_reconcile")]


# ---- m5：開放格到期用 ttl_expired ----

def test_open_cell_directive_expires_with_ttl_expired():
    planner, _, events, _holder = planner_with(PresenceReading("ACTIVE", "discord"), items=())
    planner.tick(tpe(12, 0))  # 預設格（host）但人在 discord → directive（開放格，沒有 ends_at）
    assert events.log == [("directive_issued", "host", "startup_reconcile")]
    planner.tick(tpe(12, 0) + timedelta(seconds=3601))
    assert ("directive_dropped", "ttl_expired") in events.log


def test_closed_cell_directive_still_uses_slot_ended():
    planner, _, events, _ = planner_with(PresenceReading("NONE", None))
    planner.tick(tpe(9, 30))
    planner.tick(tpe(10, 1))
    assert ("directive_dropped", "slot_ended") in events.log
