"""AR-2：在場請求仲裁 shadow（只記錄判決；不呼叫 Presence 寫入、不寫 override）。不碰 DB。"""
from datetime import datetime, time
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from uep.contracts.schedule import ActivityKind, InterruptFacts, OverrideView, PlanItem
from uep.echo.schedule_wiring import attach_presence_arbiter, parse_arbiter_mode
from uep.schedule import resolver
from uep.schedule.arbiter_shadow import PresenceArbiterShadow
from uep.schedule.planner import PresenceReading

TZ = "Asia/Taipei"
TPE = ZoneInfo(TZ)
NOW = datetime(2026, 11, 12, 9, 30, tzinfo=TPE)
WORK = PlanItem("work", ActivityKind.WORK, "discord", "weekly", weekdays=(1, 2, 3, 4, 5, 6, 7), local_start=time(9),
                local_end=time(10), tz=TZ)
REST = PlanItem("rest", ActivityKind.REST, None, "weekly", weekdays=(1, 2, 3, 4, 5, 6, 7), local_start=time(13),
                local_end=time(14), tz=TZ)


class Reader:
    def __init__(self, items=(WORK,), fail=False):
        self.items, self.fail = list(items), fail

    def resolve(self, at=None):
        if self.fail:
            raise RuntimeError("db down")
        return resolver.resolve(self.items, None, at, TZ, plan_revision=1)


class Metrics:
    def __init__(self):
        self.counts = {}

    def inc(self, name):
        self.counts[name] = self.counts.get(name, 0) + 1


def make(items=(WORK,), presence=None, override=None, probe=None, fail=False):
    presence = presence or PresenceReading("NONE", None)
    probe = probe or InterruptFacts()
    metrics = Metrics()
    shadow = PresenceArbiterShadow(Reader(items, fail), lambda: presence, lambda: override, lambda: probe,
                                   clock=lambda: NOW, metrics_inc=metrics.inc)
    return shadow, metrics


def directive_event(items=(WORK,), now=NOW):
    slot = resolver.resolve(list(items), None, now, TZ, plan_revision=1)
    directive = resolver.reconcile_directive(slot, None, now)
    return SimpleNamespace(payload={"directive_id": directive.directive_id})


def test_directive_event_is_judged_and_recorded_without_side_effects():
    shadow, metrics = make()
    shadow.on_directive_event(directive_event())
    [judgment] = shadow.judgments
    assert judgment.source == "directive" and judgment.decision.outcome == "ACCEPTED_TRANSFER"
    assert judgment.decision.priority == 0 and judgment.decision.override_action == "none"
    assert metrics.counts == {"schedule.arbiter.shadow.directive.accepted_transfer": 1,
                              "schedule.arbiter.shadow.directive.reason.accepted": 1}


def test_directive_judgments_follow_arbiter_rules():
    ov = OverrideView("povr_1", "desktop", ActivityKind.WORK, NOW, datetime(2026, 11, 12, 11, tzinfo=TPE),
                      datetime(2026, 11, 13, tzinfo=TPE), "active", "r", "owner", 200)
    shadow, _ = make(override=ov)
    shadow.on_directive_event(directive_event())
    assert shadow.judgments[-1].decision.reason_code == "override_active"
    shadow, _ = make(probe=InterruptFacts(dream_in_flight=True))
    shadow.on_directive_event(directive_event())
    assert shadow.judgments[-1].decision.outcome == "DEFERRED"


def test_stale_or_unknown_directive_is_counted_not_judged():
    shadow, metrics = make()
    shadow.on_directive_event(SimpleNamespace(payload={"directive_id": "someone-else"}))
    unknown, m2 = make(presence=PresenceReading("RECOVERING", None))
    unknown.on_directive_event(directive_event())
    assert not shadow.judgments and not unknown.judgments
    assert metrics.counts == {"schedule.arbiter.shadow.directive.stale": 1}
    assert m2.counts == {"schedule.arbiter.shadow.directive.stale": 1}


def test_turn_skipped_without_target_is_only_counted():
    shadow, metrics = make()
    shadow.on_turn_skipped(SimpleNamespace(event_id="evt_1", payload={"presence_request": True, "is_owner": "true",
                                                                       "actor_authenticated": True}))
    shadow.on_turn_skipped(SimpleNamespace(event_id="evt_2", payload={"presence_request": False}))
    shadow.on_turn_skipped(SimpleNamespace(event_id="evt_3", payload={"presence_request": None}))
    assert not shadow.judgments
    assert metrics.counts == {"schedule.arbiter.shadow.presence_request_observed": 1,
                              "schedule.arbiter.shadow.request.no_target": 1}


def test_turn_skipped_with_target_is_judged_when_authenticated():
    shadow, metrics = make(items=(REST,), presence=PresenceReading("ACTIVE", "discord"))
    now = datetime(2026, 11, 12, 13, 30, tzinfo=TPE)
    shadow._clock = lambda: now
    event = SimpleNamespace(event_id="evt_9", payload={
        "presence_request": True, "is_owner": "true", "actor_authenticated": True, "target_location": "desktop",
        "actor_id": "discord:1"})
    shadow.on_turn_skipped(event)
    [judgment] = shadow.judgments
    assert judgment.source == "request" and judgment.ref == "observed:evt_9"
    assert (judgment.decision.outcome, judgment.decision.priority, judgment.decision.override_action) == (
        "ACCEPTED_TRANSFER", 200, "begin")
    unauth = SimpleNamespace(event_id="evt_10", payload={
        "presence_request": True, "is_owner": "true", "actor_authenticated": False, "target_location": "desktop"})
    shadow.on_turn_skipped(unauth)
    assert len(shadow.judgments) == 1 and metrics.counts["schedule.arbiter.shadow.request.unauthenticated"] == 1


def test_errors_never_escape_the_bus_handler():
    shadow, metrics = make(fail=True)
    shadow.on_directive_event(directive_event())
    shadow.on_turn_skipped(SimpleNamespace(event_id="e", payload={"presence_request": True, "is_owner": "true",
                                                                   "actor_authenticated": True,
                                                                   "target_location": "desktop"}))
    assert metrics.counts["schedule.arbiter.shadow.error"] == 2 and not shadow.judgments


# ---- 旗標與組裝 ----

class FakeBus:
    def __init__(self):
        self.handlers = {}

    def subscribe(self, event_type, handler):
        self.handlers.setdefault(event_type, []).append(handler)

    def unsubscribe(self, event_type, handler):
        self.handlers[event_type].remove(handler)


class FakeState:
    def __init__(self):
        self.requests = []

    def current(self, axis):
        return SimpleNamespace(state="IDLE", version=1)

    def request_transition(self, req):
        self.requests.append(req)


def fake_core():
    view = SimpleNamespace(status=SimpleNamespace(value="NONE"), location=None, transfer_id=None, expires_at=None)
    # presence 只有 peek：任何寫入方法（acquire／revoke…）都會 AttributeError，測試因此能證明 shadow 沒有呼叫
    return SimpleNamespace(bus=FakeBus(), state=FakeState(), presence=SimpleNamespace(peek=lambda: view),
                           memory=None, tasks=None, echo=SimpleNamespace(metrics=Metrics()))


def test_parse_arbiter_mode():
    assert parse_arbiter_mode(None) == "off" and parse_arbiter_mode(" Shadow ") == "shadow"
    with pytest.raises(ValueError):
        parse_arbiter_mode("bogus")


def test_attach_is_off_by_default():
    from uep.config import UepSettings

    assert UepSettings.model_fields["presence_arbiter"].default == "off"
    core = fake_core()
    for settings in (SimpleNamespace(), SimpleNamespace(presence_arbiter="off")):
        assert attach_presence_arbiter(settings, core, session_factory=None, reader=Reader()) is None
    assert core.bus.handlers == {}
    with pytest.raises(ValueError):
        attach_presence_arbiter(SimpleNamespace(presence_arbiter="bogus"), core, session_factory=None,
                                reader=Reader())


def test_attach_shadow_subscribes_judges_and_never_writes():
    core = fake_core()
    shadow = attach_presence_arbiter(SimpleNamespace(presence_arbiter="shadow"), core, session_factory=None, start=False,
                                     reader=Reader())
    assert set(core.bus.handlers) == {"schedule.directive_issued", "turn.skipped"}
    [handler] = core.bus.handlers["schedule.directive_issued"]
    handler(SimpleNamespace(payload={"directive_id": "unknown"}))  # 對不上的 directive：只驗證不丟例外、不寫入
    for handler in core.bus.handlers["turn.skipped"]:
        handler(SimpleNamespace(event_id="e", payload={"presence_request": True}))
    assert core.state.requests == []  # 沒有任何 State／Presence 寫入
    assert core.echo.metrics.counts.get("schedule.arbiter.shadow.presence_request_observed") == 1
    shadow.stop()
    assert core.bus.handlers["schedule.directive_issued"] == [] and core.bus.handlers["turn.skipped"] == []


def test_non_owner_requests_are_never_built_as_member_requests():  # m5：拿不到 platform_links 的已登記事實
    shadow, metrics = make(items=(REST,), presence=PresenceReading("ACTIVE", "discord"))
    shadow._clock = lambda: datetime(2026, 11, 12, 13, 30, tzinfo=TPE)
    for owner_flag in ("false", "unknown", None, True):
        shadow.on_turn_skipped(SimpleNamespace(event_id="e", payload={
            "presence_request": True, "is_owner": owner_flag, "actor_authenticated": True,
            "target_location": "desktop", "actor_id": "discord:2"}))
    assert not shadow.judgments
    assert metrics.counts["schedule.arbiter.shadow.request.member_unverified"] == 4
