"""Behavior driver 切 on 之前的三項：boot namespace（N1）、指數退避（N2）、Task 層不可中斷工作。不碰 DB。"""
import time as _time
from datetime import UTC, datetime, time
from types import SimpleNamespace

import pytest

from uep.contracts.schedule import ActivityKind, PlanItem
from uep.echo.schedule_wiring import attach_behavior_driver, has_uninterruptible_work
from uep.ids import is_valid_id, new_id
from uep.schedule import resolver
from uep.schedule.driver import BehaviorDriver, BehaviorFacts
from uep.state.coordinator import Axis

TZ = "Asia/Taipei"
NOW = datetime(2026, 11, 12, 10, tzinfo=UTC)


class Reader:
    def __init__(self, items=()):
        self.items = list(items)

    def resolve(self, at=None):
        return resolver.resolve(self.items, None, at, TZ)

    def upcoming(self, start, horizon_s):
        return ()

    def revision(self):
        return 0


WORK = PlanItem("w", ActivityKind.WORK, "discord", "weekly", weekdays=(1, 2, 3, 4, 5, 6, 7), local_start=time(0),
                local_end=time(23, 59), tz=TZ)


def facts():
    return BehaviorFacts(presence_status="ACTIVE", current_behavior="REST", behavior_version=1, interaction_idle=True,
                         global_stop=False, runtime_ready=True)


# ---- N2：指數退避 ----

def test_backoff_doubles_is_capped_and_jittered():
    driver = BehaviorDriver(Reader(), lambda now: facts(), lambda *a: "ACCEPTED", retry_backoff_s=30.0,
                            retry_backoff_max_s=200.0, backoff_jitter=0.2, rng=lambda: 0.5)
    assert [driver._backoff(n) for n in (1, 2, 3, 4, 5, 40)] == [30.0, 60.0, 120.0, 200.0, 200.0, 200.0]
    low = BehaviorDriver(Reader(), lambda now: facts(), lambda *a: "ACCEPTED", rng=lambda: 0.0)
    high = BehaviorDriver(Reader(), lambda now: facts(), lambda *a: "ACCEPTED", rng=lambda: 0.999999)
    assert low._backoff(1) == pytest.approx(30.0 * 0.8) and high._backoff(1) == pytest.approx(30.0 * 1.2, rel=1e-3)
    assert low._backoff(30) == pytest.approx(600.0 * 0.8)  # 上限本身也套用抖動


def test_retries_follow_exponential_schedule():
    clock = [0.0]
    attempts = []

    def submit(frm, to, version, reason, attempt):
        attempts.append(attempt)
        return "DEFERRED"

    driver = BehaviorDriver(Reader([WORK]), lambda now: facts(), submit, mode="on", clock=lambda: NOW,
                            retry_backoff_s=10.0, retry_backoff_max_s=40.0, rng=lambda: 0.5,
                            monotonic=lambda: clock[0])
    for t in (0.0, 5.0, 10.0, 25.0, 30.0, 70.0, 100.0, 109.0, 110.0):
        clock[0] = t
        driver.tick()
    # 失敗後等待 10、20、40、40…：t=0(a1)→10(a2)→30(a3)→70(a4)→110(a5)
    assert attempts == [1, 2, 3, 4, 5]


# ---- N1：boot namespace ----

class FakeState:
    def __init__(self):
        self.requests = []

    def current(self, axis):
        values = {Axis.PRESENCE: "ACTIVE", Axis.BEHAVIOR: "REST", Axis.INTERACTION: "IDLE", Axis.RUNTIME: "READY"}
        return SimpleNamespace(state=values[axis], version=7)

    def request_transition(self, req):
        self.requests.append(req)
        return SimpleNamespace(decision=SimpleNamespace(value="ACCEPTED"))


def fake_core(tasks=None):
    return SimpleNamespace(state=FakeState(), stop=SimpleNamespace(is_engaged=lambda: False), memory=None,
                           bus=None, echo=None, tasks=tasks)


def first_request_id(core):
    driver = attach_behavior_driver(SimpleNamespace(schedule_behavior_driver="on"), core, session_factory=None,
                                    rest_policy=None, reader=Reader([WORK]))
    try:
        deadline = _time.time() + 3.0
        while not core.state.requests and _time.time() < deadline:
            _time.sleep(0.02)
        return core.state.requests[0].request_id
    finally:
        driver.stop()


def test_each_boot_gets_its_own_request_id_namespace():
    first, second = first_request_id(fake_core()), first_request_id(fake_core())
    assert first != second  # 同樣的 (from,to,version)，不同開機 → 不同 request_id，不撞上一輪的冪等紀錄
    prefix, boot, rest = first.split(":", 2)
    assert prefix == "policy_behavior" and is_valid_id(boot, "boot") and rest == "REST>CHAT:v7"
    assert is_valid_id(new_id("boot"), "boot")


# ---- Task 層 ----

def task(policy, status):
    return SimpleNamespace(interrupt_policy=policy, status=status)


class Tasks:
    def __init__(self, *live):
        self.live = list(live)
        self.calls = 0

    def list_live_tasks(self):
        self.calls += 1
        return self.live


def test_has_uninterruptible_work_filters_policy_and_status():
    assert has_uninterruptible_work(None) is False
    assert has_uninterruptible_work(Tasks()) is False
    assert has_uninterruptible_work(Tasks(task("interruptible", "RUNNING"))) is False  # 回合用的 Task
    assert has_uninterruptible_work(Tasks(task("checkpoint_only", "RUNNING"))) is False
    assert has_uninterruptible_work(Tasks(task("non_interruptible", "PAUSED"))) is False
    assert has_uninterruptible_work(Tasks(task("non_interruptible", "PENDING"))) is False
    assert has_uninterruptible_work(Tasks(task("non_interruptible", "RUNNING"))) is True
    assert has_uninterruptible_work(Tasks(task("interruptible", "RUNNING"), task("non_interruptible",
                                                                                  "WAITING_APPROVAL"))) is True


def test_attach_reads_uninterruptible_work_from_tasks_each_tick():
    tasks = Tasks(task("non_interruptible", "RUNNING"))
    core = fake_core(tasks)
    driver = attach_behavior_driver(SimpleNamespace(schedule_behavior_driver="shadow"), core, session_factory=None,
                                    rest_policy=None, reader=Reader(), boot_id="boot_T")
    try:
        assert driver._facts_fn(NOW).has_uninterruptible_work is True
        tasks.live = []
        assert driver._facts_fn(NOW).has_uninterruptible_work is False
    finally:
        driver.stop()
    broken = SimpleNamespace(list_live_tasks=lambda: (_ for _ in ()).throw(RuntimeError("db down")))
    core2 = fake_core(broken)
    d2 = attach_behavior_driver(SimpleNamespace(schedule_behavior_driver="shadow"), core2, session_factory=None,
                                rest_policy=None, reader=Reader(), boot_id="boot_T")
    try:
        assert d2.tick_safe() is None  # 查詢失敗：fail-closed，本次不採取動作
    finally:
        d2.stop()
