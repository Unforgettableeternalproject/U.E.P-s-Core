"""PP-3b：BehaviorDriver 與組裝（純單元測試，不碰 DB）。"""
import threading
import time as _time
from datetime import UTC, datetime, time, timedelta
from types import SimpleNamespace

import pytest

from uep.contracts.schedule import ActivityKind, PlanItem
from uep.echo.schedule_wiring import attach_behavior_driver
from uep.schedule import resolver
from uep.schedule.driver import BehaviorDriver, BehaviorFacts, parse_driver_mode
from uep.state.coordinator import Axis

TZ = "Asia/Taipei"
NOW = datetime(2026, 11, 12, 10, tzinfo=UTC)  # 台北 18:00


class Reader:
    def __init__(self, items=()):
        self.items = list(items)

    def resolve(self, at=None):
        return resolver.resolve(self.items, None, at, TZ)

    def upcoming(self, start, horizon_s):
        return ()

    def revision(self):
        return 0


def work_item(start=time(17), end=time(19)):
    return PlanItem("w", ActivityKind.WORK, "discord", "weekly", weekdays=(1, 2, 3, 4, 5, 6, 7), local_start=start,
                    local_end=end, tz=TZ)


def facts(**kw):
    base = {"presence_status": "ACTIVE", "current_behavior": "REST", "behavior_version": 1, "interaction_idle": True,
            "global_stop": False, "runtime_ready": True}
    base.update(kw)
    return BehaviorFacts(**base)


class Recorder:
    def __init__(self, result="ACCEPTED"):
        self.calls, self.counts, self.result, self.attempts = [], {}, result, []

    def submit(self, frm, to, version, reason, attempt=1):
        self.calls.append((frm, to, version, reason))
        self.attempts.append(attempt)
        return self.result

    def inc(self, name):
        self.counts[name] = self.counts.get(name, 0) + 1


def make(mode, fact_box, rec, items=None):
    items = (work_item(),) if items is None else items
    return BehaviorDriver(Reader(items), lambda now: fact_box[0], rec.submit, mode=mode, metrics_inc=rec.inc,
                          clock=lambda: NOW)


def test_parse_driver_mode():
    assert parse_driver_mode(None) == "shadow" and parse_driver_mode("") == "shadow"
    assert parse_driver_mode(" ON ") == "on" and parse_driver_mode("off") == "off"
    with pytest.raises(ValueError):
        parse_driver_mode("auto")


def test_off_does_nothing():
    rec = Recorder()
    driver = make("off", [facts()], rec)
    assert driver.tick() is None and rec.calls == [] and rec.counts == {}
    driver.start()
    assert driver._thread is None  # off 不開執行緒


def test_shadow_records_but_never_submits():
    rec = Recorder()
    driver = make("shadow", [facts()], rec)
    decision = driver.tick()
    assert decision.outcome == "ACCEPT_SUGGESTION" and decision.target == "CHAT"  # REST→WORK 先經 CHAT
    assert rec.calls == [] and rec.counts == {"schedule.behavior.would_transition": 1}


def test_on_submits_first_step_with_expected_version():
    rec = Recorder()
    driver = make("on", [facts(current_behavior="REST", behavior_version=7)], rec)
    decision = driver.tick()
    # REST → WORK 需先經 CHAT（轉換表不允許直達）
    assert decision.path == ("CHAT", "WORK")
    assert rec.calls == [("REST", "CHAT", 7, "plan_work")]
    assert rec.counts == {"schedule.behavior.submitted.accepted": 1}


def test_on_rejected_or_failing_submit_does_not_crash(caplog):
    rec = Recorder(result="REJECTED_ILLEGAL")
    driver = make("on", [facts()], rec)
    assert driver.tick() is not None and rec.counts == {"schedule.behavior.submitted.rejected_illegal": 1}

    def boom(*_):
        raise RuntimeError("db down")

    failing = BehaviorDriver(Reader([work_item()]), lambda now: facts(), boom, mode="on", metrics_inc=rec.inc,
                             clock=lambda: NOW)
    assert failing.tick() is not None and rec.counts["schedule.behavior.submit_error"] == 1


def test_hold_does_not_submit_and_noop_does_not_count():
    rec = Recorder()
    # 離開 CHAT 時 Interaction 非 IDLE → HOLD
    driver = make("on", [facts(presence_status="NONE", current_behavior="CHAT", interaction_idle=False)], rec, items=())
    decision = driver.tick()
    assert decision.outcome == "HOLD" and rec.calls == [] and rec.counts == {"schedule.behavior.hold": 1}


def test_presence_none_prefers_rest_family():
    rec = Recorder()
    driver = make("on", [facts(presence_status="NONE", current_behavior="CHAT")], rec)
    driver.tick()
    assert rec.calls == [("CHAT", "REST", 1, "presence_fallback")]


def test_entered_at_is_approximated_by_version_change_for_debounce():
    rec = Recorder()
    box = [facts(current_behavior="REST", behavior_version=1)]
    driver = make("on", box, rec)
    driver.tick(NOW)  # 第一次觀察：進入時間未知，不防抖 → 提請求
    assert len(rec.calls) == 1
    box[0] = facts(current_behavior="CHAT", behavior_version=2)  # version 變了 → 重設進入時間；CHAT→WORK 非硬約束
    decision = driver.tick(NOW + timedelta(seconds=10))
    assert decision.outcome == "HOLD" and decision.reason_code == "min_dwell"


def test_sleep_is_kept_inside_rest_window_when_flag_set():  # N4
    rest_window = PlanItem("rw", ActivityKind.REST, None, "weekly", role="rest_window", weekdays=(1, 2, 3, 4, 5, 6, 7),
                           local_start=time(17), local_end=time(19), tz=TZ)
    work = work_item(time(17, 30), time(18, 30))  # 明確行程截斷窗口 → slot 是 WORK
    rec = Recorder()
    driver = make("on", [facts(presence_status="NONE", current_behavior="SLEEP", in_rest_window=True)], rec,
                  items=(rest_window, work))
    decision = driver.tick()
    assert decision.reason_code == "sleep_rest_window" and rec.calls == []


def test_tick_safe_swallows_fact_errors():
    rec = Recorder()

    def broken(now):
        raise RuntimeError("state unreadable")

    driver = BehaviorDriver(Reader(), broken, rec.submit, mode="on", metrics_inc=rec.inc, clock=lambda: NOW)
    assert driver.tick_safe() is None and rec.calls == []  # fail-closed：不提請求
    assert rec.counts == {"schedule.behavior.tick_error": 1}


def test_background_loop_and_poke():
    rec = Recorder()
    ticked = threading.Event()
    box = [facts()]

    def facts_fn(now):
        ticked.set()
        return box[0]

    driver = BehaviorDriver(Reader(), facts_fn, rec.submit, mode="shadow", metrics_inc=rec.inc, interval_s=30.0)
    driver.start()
    assert ticked.wait(2.0)
    ticked.clear()
    driver.poke()  # 事件觸發：不必等 30 秒
    assert ticked.wait(2.0)
    driver.stop()
    assert driver._thread is None
    _time.sleep(0.05)


# ---- 組裝 ----

class Counter2:
    def __init__(self):
        self.counts = {}

    def inc(self, key, amount=1):
        self.counts[key] = self.counts.get(key, 0) + amount


class FakeState:
    def __init__(self):
        self.axes = {Axis.PRESENCE: ("ACTIVE", 1), Axis.BEHAVIOR: ("REST", 3), Axis.INTERACTION: ("IDLE", 1),
                     Axis.RUNTIME: ("READY", 1)}
        self.requests = []

    def current(self, axis):
        state, version = self.axes[axis]
        return SimpleNamespace(state=state, version=version)

    def request_transition(self, req):
        self.requests.append(req)
        return SimpleNamespace(decision=SimpleNamespace(value="ACCEPTED"))


class FakeBus:
    def __init__(self):
        self.subscribed = []

    def subscribe(self, event_type, handler):
        self.subscribed.append(event_type)

    def unsubscribe(self, event_type, handler):
        self.subscribed.remove(event_type)


def fake_core(in_flight=None):
    memory = SimpleNamespace(dream_status=lambda: {"in_flight": in_flight})
    return SimpleNamespace(state=FakeState(), stop=SimpleNamespace(is_engaged=lambda: False), memory=memory,
                           bus=FakeBus(), echo=SimpleNamespace(metrics=Counter2()))


def test_attach_off_returns_none_and_invalid_raises():
    core = fake_core()
    assert attach_behavior_driver(SimpleNamespace(schedule_behavior_driver="off"), core, session_factory=None,
                                  rest_policy=None, reader=Reader()) is None
    assert core.bus.subscribed == []
    with pytest.raises(ValueError):
        attach_behavior_driver(SimpleNamespace(schedule_behavior_driver="x"), core, session_factory=None,
                               rest_policy=None, reader=Reader())


def test_attach_on_submits_core_policy_request_and_subscribes_events():
    core = fake_core()
    driver = attach_behavior_driver(SimpleNamespace(schedule_behavior_driver="on"), core, session_factory=None,
                                    rest_policy=None, reader=Reader([work_item(time(0), time(23, 59))]))
    try:
        assert {"presence.activated", "state.changed"} <= set(core.bus.subscribed)
        deadline = _time.time() + 2.0
        while not core.state.requests and _time.time() < deadline:
            _time.sleep(0.02)
        [req] = core.state.requests[:1]
        assert (req.axis, req.proposer, req.from_state, req.to_state, req.expected_version) == (
            "Behavior", "core.policy", "REST", "CHAT", 3)
        assert req.request_id == "policy_behavior:REST>CHAT:v3" and req.reason["source"] == "behavior_driver"
    finally:
        driver.stop()


def test_attach_shadow_never_requests():
    core = fake_core(in_flight={"namespace_id": "n", "trigger": "t", "elapsed_s": 1})
    driver = attach_behavior_driver(SimpleNamespace(schedule_behavior_driver="shadow"), core, session_factory=None,
                                    rest_policy=None, reader=Reader([work_item(time(0), time(23, 59))]))
    try:
        assert driver.mode == "shadow"
        driver.tick()
        assert core.state.requests == []
        f = driver._facts_fn(NOW)
        assert f.dream_in_flight is True and f.presence_status == "ACTIVE" and f.runtime_ready is True
        assert f.in_rest_window is False  # 預設 FixedWindowPolicy 03:00–05:30，NOW＝台北 18:00
    finally:
        driver.stop()


# ---- 審查修正 ----

def test_rejected_submission_retries_with_increasing_attempt_after_backoff():  # M1
    rec = Recorder(result="DEFERRED")
    clock = [0.0]
    driver = BehaviorDriver(Reader([work_item()]), lambda now: facts(), rec.submit, mode="on", metrics_inc=rec.inc,
                            clock=lambda: NOW, retry_backoff_s=30.0, monotonic=lambda: clock[0])
    driver.tick()
    driver.tick()  # backoff 內：不重送
    assert rec.attempts == [1]
    clock[0] = 31.0
    driver.tick()
    clock[0] = 100.0
    driver.tick()
    assert rec.attempts == [1, 2, 3]  # 同一組 (from,to,version) 的嘗試序號遞增
    rec.result = "ACCEPTED"
    clock[0] = 200.0
    driver.tick()
    assert rec.attempts[-1] == 4
    clock[0] = 300.0
    driver.tick()  # ACCEPTED 後歸零（實務上 version 也變了）
    assert rec.attempts[-1] == 1


def test_plan_unavailable_holds_without_presence_fallback():  # M2
    class Down(Reader):
        def resolve(self, at=None):
            return resolver.unavailable_slot(at, 0, "store_unavailable")

    rec = Recorder()
    driver = BehaviorDriver(Down(), lambda now: facts(presence_status="NONE", current_behavior="CHAT"), rec.submit,
                            mode="on", metrics_inc=rec.inc, clock=lambda: NOW)
    decision = driver.tick()
    assert (decision.outcome, decision.reason_code, decision.target) == ("HOLD", "plan_unavailable", None)
    assert rec.calls == []  # 不套用 Presence fallback（CHAT→REST）


def test_in_rest_window_comes_from_own_slot_when_none():  # m1
    rest_window = PlanItem("rw", ActivityKind.REST, None, "weekly", role="rest_window", weekdays=(1, 2, 3, 4, 5, 6, 7),
                           local_start=time(17), local_end=time(19), tz=TZ)
    rec = Recorder()
    # facts.in_rest_window=None → 依 driver 自己 resolve 的 slot（REST_WINDOW）；SLEEP 不被切出
    driver = make("on", [facts(presence_status="NONE", current_behavior="CHAT", in_rest_window=None)], rec,
                  items=(rest_window,))
    assert driver.tick().target == "SLEEP"  # 窗口內建議 [SLEEP, REST]
    # slot 是 REST_WINDOW 但明確建議 REST：窗口內 SLEEP 不被行程建議切出
    pinned = PlanItem("rw", ActivityKind.REST, None, "weekly", role="rest_window", weekdays=(1, 2, 3, 4, 5, 6, 7),
                      local_start=time(17), local_end=time(19), tz=TZ, suggested_behaviors=("REST",))
    sleeping = make("on", [facts(presence_status="NONE", current_behavior="SLEEP", in_rest_window=None)], rec,
                    items=(pinned,))
    assert sleeping.tick().reason_code == "sleep_rest_window"
    # 被明確行程截斷的窗口（slot 不是 REST_WINDOW）：不再視為窗口內
    cut = make("on", [facts(presence_status="NONE", current_behavior="SLEEP", in_rest_window=None)], rec,
               items=(rest_window, work_item(time(17, 30), time(18, 30))))
    assert cut.tick().reason_code != "sleep_rest_window"


def test_tick_errors_are_logged_once_per_minute(caplog):  # m3
    import logging

    clock = [0.0]

    def broken(now):
        raise RuntimeError("state unreadable")

    driver = BehaviorDriver(Reader(), broken, lambda *a: "ACCEPTED", mode="shadow", monotonic=lambda: clock[0])
    with caplog.at_level(logging.WARNING, logger="uep.schedule.driver"):
        for _ in range(5):
            driver.tick_safe()
        clock[0] = 61.0
        driver.tick_safe()
    records = [r for r in caplog.records if r.name == "uep.schedule.driver"]
    assert len(records) == 2 and records[0].exc_info is not None and records[1].exc_info is None


def test_stop_runs_cleanups_once():  # m4
    driver = BehaviorDriver(Reader(), lambda now: facts(), lambda *a: "ACCEPTED", mode="shadow")
    called = []
    driver.add_cleanup(lambda: called.append(1))
    driver.stop()
    driver.stop()
    assert called == [1]


def test_attach_unsubscribes_on_stop_uses_echo_metrics_and_attempt_suffix():  # m4 / m2 / M1
    core = fake_core()
    core.state.request_transition = lambda req: (core.state.requests.append(req) or SimpleNamespace(
        decision=SimpleNamespace(value="DEFERRED")))
    driver = attach_behavior_driver(SimpleNamespace(schedule_behavior_driver="on"), core, session_factory=None,
                                    rest_policy=None, reader=Reader([work_item(time(0), time(23, 59))]))
    try:
        driver.stop()  # 停掉背景執行緒，測試只用手動 tick（確定性）
        ticks = iter(range(10**9, 10**9 + 10_000, 100))
        driver._monotonic = lambda: float(next(ticks))  # 每次呼叫前進 100 秒，越過 backoff
        driver.tick()
        driver.tick()
        ids = [r.request_id for r in core.state.requests]
        assert ids[0] == "policy_behavior:REST>CHAT:v3" and ids[1] == "policy_behavior:REST>CHAT:v3:a2"
        assert core.echo.metrics.counts.get("schedule.behavior.submitted.deferred", 0) >= 2
    finally:
        driver.stop()
    assert core.bus.subscribed == []  # 已退訂


def test_state_policy_version_pinned():
    from uep.state.coordinator import STATE_POLICY_VERSION

    assert STATE_POLICY_VERSION == 2  # v2＝Behavior 軸新增 REST 邊
