"""AR-3a 審查修正：owner_unverifiable、abandon_work error、判決 idempotency、關閉語意、executed 的意義。不碰 DB。"""
import logging
import threading
from datetime import datetime, time
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from uep.contracts.schedule import (
    AbandonWorkSignal,
    ActivityKind,
    ArbitrationDecision,
    PlanItem,
)
from uep.events.registry import DEFAULT_REGISTRY
from uep.schedule.abandon_executor import AbandonExecutor
from uep.schedule.events import ScheduleEvents
from uep.schedule.planner import PresenceReading

TZ = "Asia/Taipei"
TPE = ZoneInfo(TZ)
WORK = PlanItem("work", ActivityKind.WORK, "discord", "weekly", weekdays=(1, 2, 3, 4, 5, 6, 7), local_start=time(9),
                local_end=time(10), tz=TZ)


def tpe(h, mi=0):
    return datetime(2026, 11, 12, h, mi, tzinfo=TPE)


def signal(signal_id="abandon:u1"):
    return AbandonWorkSignal(
        signal_id=signal_id, actor_ref="discord:1", actor_authenticated=True, source_platform="discord", path="turn",
        decided_at=tpe(9, 20), decider_id="rules.abandon_work", decider_version="1")


class Marks:
    def __init__(self, error=None):
        self.error, self.calls, self.marks = error, [], []

    def begin_mark(self, *, kind, item_ref, planned_start_at, cause_request_id):
        self.calls.append(cause_request_id)
        if self.error is not None:
            raise self.error
        self.marks.append(SimpleNamespace(mark_id="pmark_1", cause_request_id=cause_request_id, item_ref=item_ref))
        return "pmark_1"

    def active_marks(self):
        return tuple(self.marks)


class Reader:
    def __init__(self, marks):
        self.marks = marks

    def resolve(self, at=None):
        from uep.schedule import resolver

        return resolver.resolve([WORK], None, at, TZ, plan_revision=1, marks=[])

    def tz(self):
        return TZ


class Events:
    def __init__(self):
        self.log = []

    def abandon_work(self, signal, result, reason_code, item_ref):
        self.log.append(("abandon_work", result, reason_code))

    def slot_marked(self, *args):
        self.log.append(("slot_marked",))


def make(marks=None, **kw):
    marks = marks or Marks()
    events = Events()
    return AbandonExecutor(Reader(marks), marks, events, clock=lambda: tpe(9, 30), **kw), marks, events


# ---- M2：is_owner 三態 ----

def test_explicit_is_owner_is_passed_through_and_none_is_unverifiable_without_a_fn():
    ex, marks, events = make()
    assert ex.handle(signal("a1"), is_owner=True).status == "ACCEPTED"
    assert ex.handle(signal("a2"), is_owner=False).reason_code == "not_owner"  # 確定不是 owner
    unknown = ex.handle(signal("a3"))  # 無法判定：和 not_owner 是不同的原因
    assert (unknown.status, unknown.reason_code) == ("IGNORED", "owner_unverifiable")
    assert events.log[-1] == ("abandon_work", "ignored", "owner_unverifiable") and len(marks.calls) == 1


def test_none_falls_back_to_the_fn_and_explicit_value_wins_over_it():
    ex, _, _ = make(is_owner_fn=lambda s: True)
    assert ex.handle(signal("b1")).status == "ACCEPTED"  # None → 用 fn
    ex2, _, _ = make(is_owner_fn=lambda s: True)
    assert ex2.handle(signal("b2"), is_owner=False).reason_code == "not_owner"  # 明確值優先


# ---- m2：寫入失敗發 error 事件 ----

def test_non_value_error_from_begin_mark_emits_error_event_without_raising():
    ex, _marks, events = make(marks=Marks(error=RuntimeError("db down")))
    result = ex.handle(signal(), is_owner=True)
    assert (result.status, result.reason_code) == ("IGNORED", "mark_write_failed")
    assert events.log == [("abandon_work", "error", "mark_write_failed")]


def test_abandon_work_result_set_is_closed():
    published = []
    pub = SimpleNamespace(publish=lambda draft, trusted, **kw: published.append(draft))
    events = ScheduleEvents(pub)
    for result in ("accepted", "ignored", "error"):
        events.abandon_work(signal(), result, "x", None)
    assert [d.payload["result"] for d in published] == ["accepted", "ignored", "error"]
    with pytest.raises(ValueError):
        events.abandon_work(signal(), "maybe", "x", None)


# ---- m3：判決 idempotency key 納入 outcome／reason_code ----

def test_arbitration_idempotency_key_distinguishes_outcomes_and_reasons():
    published = []
    events = ScheduleEvents(SimpleNamespace(publish=lambda draft, trusted, **kw: published.append(draft)))
    queued = ArbitrationDecision("QUEUED", "transfer_in_progress", 200)
    accepted = ArbitrationDecision("ACCEPTED_TRANSFER", "accepted", 200, "begin", "desktop", "desktop")
    for decision in (queued, accepted, accepted):
        events.arbitration_decided(source="request", ref="r-1", decision=decision, executed=False,
                                   exec_reason="transfer_not_implemented")
    keys = [d.idempotency_key for d in published]
    assert keys[0] != keys[1] and keys[1] == keys[2]  # 同 ref 不同判決不同 key；相同判決才去重
    assert "mark_written" in DEFAULT_REGISTRY.lookup("schedule.arbitration_decided").payload_keys
    assert published[0].payload["mark_written"] is False


# ---- m4：關閉語意 ----

def shadow_for_tests():
    from uep.schedule.arbiter_shadow import PresenceArbiterShadow

    return PresenceArbiterShadow(Reader(Marks()), lambda: PresenceReading("NONE", None), None, lambda: None,
                                 clock=lambda: tpe(9, 30), mode="shadow")


def test_calls_after_stop_are_dropped_and_counted_not_processed_inline():
    shadow = shadow_for_tests()
    counts = {}
    shadow._metrics_inc = lambda name: counts.__setitem__(name, counts.get(name, 0) + 1)
    seen = []
    shadow._handle_abandon = lambda signal, is_owner=None: seen.append(signal)
    shadow.on_abandon_signal(signal("c1"))
    assert len(seen) == 1  # 未啟動執行緒（測試）：同步處理
    shadow.stop()
    shadow.on_abandon_signal(signal("c2"))
    assert len(seen) == 1 and counts["schedule.arbiter.shadow.closed_drop"] == 1


def test_stop_logs_how_many_queued_items_were_dropped(caplog):
    shadow = shadow_for_tests()
    gate = threading.Event()
    shadow._process = lambda kind, args, kwargs: gate.wait(2.0)  # 卡住背景執行緒
    shadow.start()
    for i in range(4):
        shadow.on_abandon_signal(signal(f"d{i}"))
    with caplog.at_level(logging.WARNING, logger="uep.schedule.arbiter_shadow"):
        gate.set()
        shadow._stop_flag.set()  # 讓執行緒在處理完手上這筆後離開
        shadow.stop()
    # 執行緒結束後若佇列還有項目，會記一筆數量；沒有剩下時不記
    dropped = [r for r in caplog.records if "丟棄" in r.message and "未處理" in r.message]
    assert len(dropped) <= 1


def test_queue_full_warning_is_rate_limited(caplog):
    shadow = shadow_for_tests()
    shadow._queue = type(shadow._queue)(maxsize=1)
    shadow._thread = threading.Thread(target=lambda: threading.Event().wait(1.0), daemon=True)
    shadow._thread.start()
    with caplog.at_level(logging.WARNING, logger="uep.schedule.arbiter_shadow"):
        for i in range(6):
            shadow.on_abandon_signal(signal(f"e{i}"))
    full = [r for r in caplog.records if "佇列已滿" in r.message]
    assert len(full) == 1  # 5 次丟棄只記一次 warning


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_worker_crash_is_logged(caplog):
    shadow = shadow_for_tests()

    class Boom(BaseException):
        pass

    def explode(*a, **k):
        raise Boom()

    shadow._queue.get = explode
    with caplog.at_level(logging.ERROR, logger="uep.schedule.arbiter_shadow"):
        thread = threading.Thread(target=shadow._run)
        thread.start()
        thread.join(2.0)
    assert any("異常結束" in r.message for r in caplog.records)
