"""judge_now：唯讀的「現在在場判決」同步查詢（仲裁器與 sink 代理）。不碰 DB。"""
import threading
from datetime import datetime, time
from zoneinfo import ZoneInfo

from uep.contracts.schedule import ActivityKind, InterruptFacts, PlanItem
from uep.schedule import resolver
from uep.schedule.arbiter_shadow import PresenceArbiterShadow
from uep.schedule.planner import PresenceReading
from uep.schedule.sink_proxy import PresenceSinkProxy

TZ = "Asia/Taipei"
TPE = ZoneInfo(TZ)
NOW = datetime(2026, 11, 12, 9, 30, tzinfo=TPE)
WORK = PlanItem("work", ActivityKind.WORK, "discord", "weekly", weekdays=(1, 2, 3, 4, 5, 6, 7), local_start=time(9),
                local_end=time(10), tz=TZ)
KW = {"target": "desktop", "is_owner": True, "actor_ref": "discord:1", "actor_authenticated": True}


REST = PlanItem("rest", ActivityKind.REST, None, "weekly", weekdays=(1, 2, 3, 4, 5, 6, 7), local_start=time(13),
                local_end=time(14), tz=TZ)
REST_NOW = datetime(2026, 11, 12, 13, 30, tzinfo=TPE)


class Reader:
    def __init__(self, fail=False, items=(WORK,)):
        self.fail, self.items = fail, list(items)

    def resolve(self, at=None):
        if self.fail:
            raise RuntimeError("db down")
        return resolver.resolve(self.items, None, at, TZ, plan_revision=1)


class Boom:
    """任何被呼叫就算違規的寫入端。"""

    def __getattr__(self, name):
        raise AssertionError(f"judge_now 不該呼叫 {name}")


def make(probe=None, fail=False, poke=None, items=(WORK,), now=NOW):
    shadow = PresenceArbiterShadow(
        Reader(fail, items), lambda: PresenceReading("ACTIVE", "discord"), lambda: None,
        lambda: probe or InterruptFacts(), clock=lambda: now, mode="on", events=Boom(), mark_writer=Boom(), poke=poke)
    return shadow


def test_owner_request_in_work_slot_is_rejected():
    shadow = make()
    decision = shadow.judge_now(**KW)
    assert (decision.outcome, decision.reason_code) == ("REJECTED", "work_in_progress")


def test_uninterruptible_work_is_deferred():
    decision = make(probe=InterruptFacts(has_uninterruptible_work=True), items=(REST,), now=REST_NOW).judge_now(**KW)
    assert (decision.outcome, decision.reason_code) == ("DEFERRED", "uninterruptible_work")


def test_judge_now_has_no_side_effects():
    called = []
    shadow = make(poke=lambda: called.append(1))
    shadow._seq = 7
    shadow.judge_now(**KW)
    assert len(shadow.judgments) == 0 and shadow._seq == 7 and not called and shadow._queue.empty()


def test_none_cases():
    shadow = make()
    assert shadow.judge_now(**{**KW, "is_owner": False}) is None  # member
    assert shadow.judge_now(**{**KW, "actor_authenticated": False}) is None
    assert shadow.judge_now(**{**KW, "target": "mars"}) is None
    assert make(fail=True).judge_now(**KW) is None  # 例外不外拋
    shadow.stop()
    assert shadow.judge_now(**KW) is None


def test_proxy_unbound_returns_none_and_bound_forwards():
    counts = {}
    proxy = PresenceSinkProxy(metrics_inc=lambda n: counts.__setitem__(n, counts.get(n, 0) + 1))
    assert proxy.judge_now(**KW) is None and proxy.dropped == 0
    assert counts == {"presence_sink.judge_now.unbound": 1}
    proxy.bind(make())
    assert proxy.judge_now(**KW).reason_code == "work_in_progress"
    proxy.unbind()
    assert proxy.judge_now(**KW) is None


def test_proxy_swallows_target_errors():
    class Bad:
        def judge_now(self, **kw):
            raise RuntimeError("boom")

    proxy = PresenceSinkProxy()
    proxy.bind(Bad())
    assert proxy.judge_now(**KW) is None


def test_unavailable_slot_returns_none():
    shadow = make()
    shadow._reader.resolve = lambda at=None: resolver.unavailable_slot(at, 0, "store_unavailable")
    assert shadow.judge_now(**KW) is None


def test_probe_degradation_returns_none_but_background_probe_unchanged():
    def strict():
        raise RuntimeError("tasks down")

    shadow = PresenceArbiterShadow(
        Reader(items=(REST,)), lambda: PresenceReading("ACTIVE", "discord"), lambda: None,
        lambda: InterruptFacts(has_uninterruptible_work=True), clock=lambda: REST_NOW, strict_probe_fn=strict)
    assert shadow.judge_now(**KW) is None
    shadow._handle_presence_request(  # 背景路徑仍用降級後的 probe_fn
        type("P", (), {"intent": "presence_request", "target_location": "desktop"})(), request_id="r", actor_ref="a",
        actor_authenticated=True, is_owner=True, source_platform="discord")
    assert shadow.judgments[-1].decision.reason_code == "uninterruptible_work"


def test_unhashable_target_returns_none():
    assert make().judge_now(**{**KW, "target": ["desktop"]}) is None


def test_shadow_mode_without_events_works():
    shadow = PresenceArbiterShadow(Reader(), lambda: PresenceReading("ACTIVE", "discord"), lambda: None,
                                   lambda: InterruptFacts(), clock=lambda: NOW)
    assert shadow.mode == "shadow" and shadow.judge_now(**KW).reason_code == "work_in_progress"


def test_same_decision_as_judge_request_path():
    shadow = make(items=(REST,), now=REST_NOW, probe=InterruptFacts(has_uninterruptible_work=True))
    decision = shadow.judge_now(**KW)
    shadow._mode = "shadow"
    shadow._handle_presence_request(
        type("P", (), {"intent": "presence_request", "target_location": "desktop"})(), request_id="r", actor_ref="a",
        actor_authenticated=True, is_owner=True, source_platform="discord")
    recorded = shadow.judgments[-1].decision
    assert (decision.outcome, decision.reason_code, decision.priority, decision.override_action,
            decision.transfer_target) == (recorded.outcome, recorded.reason_code, recorded.priority,
                                          recorded.override_action, recorded.transfer_target)


def test_proxy_concurrency_cap():
    entered, release = threading.Event(), threading.Event()
    counts = {}

    class Slow:
        def judge_now(self, **kw):
            entered.set()
            release.wait(5)
            return "done"

    proxy = PresenceSinkProxy(metrics_inc=lambda n: counts.__setitem__(n, counts.get(n, 0) + 1))
    proxy.bind(Slow())
    results = []
    t = threading.Thread(target=lambda: results.append(proxy.judge_now(**KW)))
    t.start()
    assert entered.wait(5)
    assert proxy.judge_now(**KW) is None and counts["presence_sink.judge_now.busy"] == 1
    release.set()
    t.join(5)
    assert results == ["done"] and proxy.judge_now(**KW) == "done"
