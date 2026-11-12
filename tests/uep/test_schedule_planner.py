"""PP-4b：PresencePlanner（純單元測試，不碰 DB；事件 sink 與 Presence 皆為替身）。"""
import logging
import threading
import time as _time
from datetime import datetime, time, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from uep.contracts.schedule import ActivityKind, OverrideView, PlanDelay, PlanItem
from uep.echo.schedule_wiring import attach_schedule_planner, parse_planner_mode
from uep.schedule import resolver
from uep.schedule.planner import PresencePlanner, PresenceReading

TZ = "Asia/Taipei"
TPE = ZoneInfo(TZ)


def tpe(h, mi=0, d=12):
    return datetime(2026, 11, d, h, mi, tzinfo=TPE)


def item(item_id, start, end, location="discord", kind=ActivityKind.WORK):
    return PlanItem(item_id, kind, location, "weekly", weekdays=(1, 2, 3, 4, 5, 6, 7), local_start=start,
                    local_end=end, tz=TZ)


class Reader:
    def __init__(self, items, tz=TZ, revision=1):
        self.items, self.tz, self.revision_value, self.override = list(items), tz, revision, None
        self.delay = None

    def resolve(self, at=None):
        return resolver.resolve(self.items, self.override, at, self.tz, plan_revision=self.revision_value,
                                delay=self.delay)

    def upcoming(self, start, horizon_s):
        return ()

    def revision(self):
        return self.revision_value

    def active_override(self):
        return self.override


class Events:
    def __init__(self):
        self.log = []

    def slot_changed(self, slot):
        self.log.append(("slot_changed", slot.item_ref))

    def slot_left(self, slot, reason):
        self.log.append(("slot_left", slot.item_ref, reason))

    def slot_entered(self, slot, reason):
        self.log.append(("slot_entered", slot.item_ref, reason))

    def directive_issued(self, directive):
        self.log.append(("directive_issued", directive.target, directive.reason))

    def directive_dropped(self, directive_id, drop_reason, next_eval_at=None):
        self.log.append(("directive_dropped", drop_reason))

    def divergence_observed(self, plan_location, actual_location, override_state, override_id):
        self.log.append(("divergence_observed", plan_location, actual_location, override_state, override_id))

    def kinds(self):
        return [entry[0] for entry in self.log]


class Presence:
    def __init__(self, status="NONE", location=None, transfer=False):
        self.reading = PresenceReading(status, location, transfer)

    def __call__(self):
        return self.reading


def make(items, presence=None, **kw):
    reader = Reader(items)
    events = Events()
    presence = presence or Presence()
    planner = PresencePlanner(reader, presence, events, override_fn=reader.active_override, **kw)
    return planner, reader, events, presence


A = item("a", time(9), time(10), "discord")
B = item("b", time(10), time(11), "desktop")


def test_startup_reconcile_sets_baseline_without_replaying_past_boundaries():
    planner, _, events, _ = make([A, B])
    planner.tick(tpe(9, 30))
    # 啟動：不補發 slot_changed／entered／left；位置不一致（主機 vs discord）→ 一個 startup_reconcile directive
    assert events.log == [("directive_issued", "discord", "startup_reconcile")]
    planner.tick(tpe(9, 40))
    assert events.kinds() == ["directive_issued"]  # 同一 directive 只發一次，startup_reconcile 只做一次


def test_boundary_emits_slot_changed_left_entered_and_new_directive_after_dropping_old():
    planner, _, events, _ = make([A, B])
    planner.tick(tpe(9, 30))
    events.log.clear()
    planner.tick(tpe(10, 0))
    assert events.log == [
        ("slot_changed", "b"), ("slot_left", "a", "boundary"), ("slot_entered", "b", "boundary"),
        ("directive_dropped", "slot_ended"), ("directive_issued", "desktop", "plan_boundary")]


def test_satisfied_directive_is_dropped_and_no_new_one_when_aligned():
    planner, _, events, presence = make([A])
    planner.tick(tpe(9, 30))
    presence.reading = PresenceReading("ACTIVE", "discord")
    planner.tick(tpe(9, 40))
    assert events.log[-1] == ("directive_dropped", "satisfied")
    planner.tick(tpe(9, 50))
    assert events.kinds().count("directive_issued") == 1


def test_default_slot_and_host_presence_issue_nothing():
    planner, _, events, _ = make([A])
    planner.tick(tpe(12, 0))
    assert events.log == []


def test_override_blocks_directive_and_divergence_is_recorded_once_per_change():
    planner, reader, events, presence = make([A])
    reader.override = OverrideView("povr_1", "desktop", ActivityKind.WORK, tpe(9, 20), tpe(9, 50), tpe(0, 0, 13),
                                   "pending")
    planner.tick(tpe(9, 30))
    assert events.log == [("divergence_observed", "desktop", "none", "pending", "povr_1")]  # NONE → "none"
    planner.tick(tpe(9, 31))
    assert events.kinds() == ["divergence_observed"]  # 沒有改變不重發
    presence.reading = PresenceReading("ACTIVE", "desktop")
    planner.tick(tpe(9, 35))
    assert events.kinds() == ["divergence_observed"]  # 已到位：差異消失，不發 directive
    assert "directive_issued" not in events.kinds()


def test_transfer_in_progress_records_divergence_not_directive():
    planner, _, events, _ = make([A], presence=Presence("NONE", None, transfer=True))
    planner.tick(tpe(9, 30))
    assert events.log == [("divergence_observed", "discord", "none", None, None)]


def test_recovering_presence_is_unknown_and_unavailable_plan_is_silent(caplog):
    planner, reader, events, presence = make([A], presence=Presence("RECOVERING", None))
    planner.tick(tpe(9, 30))
    assert events.log == []
    presence.reading = PresenceReading("NONE", None)
    reader.tz = "Not/AZone"
    with caplog.at_level(logging.WARNING, logger="uep.schedule.planner"):
        for _ in range(3):
            slot = planner.tick(tpe(9, 40))
    assert slot.availability == "unavailable" and events.log == []
    assert sum("行程不可用" in r.message and r.name == "uep.schedule.planner" for r in caplog.records) == 1
    reader.tz = TZ
    planner.tick(tpe(9, 45))
    assert events.kinds() == ["directive_issued"]


def test_delay_extension_is_not_a_slot_change():
    planner, reader, events, _ = make([A, B], presence=Presence("ACTIVE", "discord"))
    planner.tick(tpe(9, 30))
    reader.delay = PlanDelay("a", tpe(9, 0), tpe(10, 30))
    planner.tick(tpe(10, 5))  # 延長中：仍是 a
    assert events.log == []


def test_plan_revision_change_issues_plan_changed_directive_and_supersedes():
    planner, reader, events, _ = make([A])
    planner.tick(tpe(9, 30))
    reader.revision_value = 2
    planner.tick(tpe(9, 31))
    assert events.log[-2:] == [("directive_dropped", "superseded"), ("directive_issued", "discord", "plan_changed")]


def test_next_wake_is_min_of_boundary_and_poll_with_floor():
    planner, _, _, _ = make([A])
    planner.tick(tpe(8, 58))
    assert planner.next_wake_s == pytest.approx(30.0)  # 邊界在 2 分鐘後 → 取 30 秒輪詢
    planner.tick(tpe(9, 0) - timedelta(seconds=10))
    assert planner.next_wake_s == pytest.approx(10.0)
    planner.tick(tpe(9, 0) - timedelta(milliseconds=10))
    assert planner.next_wake_s == 0.2  # 下限，避免空轉
    planner.tick(tpe(12, 0))
    assert planner.next_wake_s == 30.0  # 沒有邊界


def test_tick_safe_swallows_errors_and_limits_logs(caplog):
    clock = [0.0]

    def broken():
        raise RuntimeError("presence unreadable")

    planner = PresencePlanner(Reader([A]), broken, Events(), monotonic=lambda: clock[0])
    with caplog.at_level(logging.WARNING, logger="uep.schedule.planner"):
        for _ in range(4):
            assert planner.tick_safe() is None
        clock[0] = 61.0
        planner.tick_safe()
    records = [r for r in caplog.records if r.name == "uep.schedule.planner"]
    assert len(records) == 2 and records[0].exc_info is not None and records[1].exc_info is None


def test_background_loop_poke_and_cleanup():
    ticked = threading.Event()
    planner, reader, _, _ = make([A], clock=lambda: tpe(12, 0), poll_s=30.0)
    original = reader.resolve

    def resolve(at=None):
        ticked.set()
        return original(at)

    reader.resolve = resolve
    cleaned = []
    planner.add_cleanup(lambda: cleaned.append(1))
    planner.start()
    assert ticked.wait(2.0)
    ticked.clear()
    planner.poke()  # 不必等 30 秒
    assert ticked.wait(2.0)
    planner.stop()
    planner.stop()
    assert cleaned == [1] and planner._thread is None


# ---- 旗標與組裝 ----

def test_parse_planner_mode():
    assert parse_planner_mode(None) == "off" and parse_planner_mode(" Shadow ") == "shadow"
    with pytest.raises(ValueError):
        parse_planner_mode("on")


class FakeBus:
    def __init__(self):
        self.handlers = {}

    def subscribe(self, event_type, handler):
        self.handlers.setdefault(event_type, []).append(handler)

    def unsubscribe(self, event_type, handler):
        self.handlers[event_type].remove(handler)


class FakePublisher:
    def __init__(self):
        self.sent = []

    def publish(self, draft, trusted, **kwargs):
        self.sent.append((draft, trusted))


def fake_core():
    view = SimpleNamespace(status=SimpleNamespace(value="NONE"), location=None, transfer_id=None)
    return SimpleNamespace(persona_id="uep", publisher=FakePublisher(), bus=FakeBus(),
                           presence=SimpleNamespace(current=lambda: view),
                           echo=SimpleNamespace(metrics=SimpleNamespace(inc=lambda *_: None)))


def test_attach_is_off_by_default_and_invalid_flag_raises(monkeypatch):
    monkeypatch.delenv("UEP_SCHEDULE_PLANNER", raising=False)
    core = fake_core()
    assert attach_schedule_planner(SimpleNamespace(), core, session_factory=None, reader=Reader([A])) is None
    assert core.bus.handlers == {}
    monkeypatch.setenv("UEP_SCHEDULE_PLANNER", "bogus")
    with pytest.raises(ValueError):
        attach_schedule_planner(SimpleNamespace(), core, session_factory=None, reader=Reader([A]))


def test_attach_shadow_publishes_schedule_events_and_unsubscribes(monkeypatch):
    monkeypatch.setenv("UEP_SCHEDULE_PLANNER", "shadow")
    core = fake_core()

    class Store(Reader):
        def __init__(self):
            super().__init__([item("a", time(0), time(23, 59))])
            self.on_change = None

        def set_on_change(self, cb):
            self.on_change = cb

    store = Store()
    planner = attach_schedule_planner(SimpleNamespace(), core, session_factory=None, reader=store)
    try:
        deadline = _time.time() + 3.0
        while not core.publisher.sent and _time.time() < deadline:
            _time.sleep(0.02)
        types = {d.event_type for d, _ in core.publisher.sent}
        assert "schedule.directive_issued" in types  # 主機 vs discord → 啟動調和的 directive
        assert all(t.source == "core.schedule" for _, t in core.publisher.sent)
        assert "schedule.plan_changed" in core.bus.handlers
        store.on_change(5, "item_created", "plan_x")  # store 寫入後 → plan_changed 事件
        assert core.publisher.sent[-1][0].event_type == "schedule.plan_changed"
    finally:
        planner.stop()
    assert core.bus.handlers["schedule.plan_changed"] == []
