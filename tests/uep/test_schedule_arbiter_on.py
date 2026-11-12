"""AR-3a：放棄消費端執行器、仲裁判決落地（on）、決策層 sink（佇列 + 背景執行緒）。純單元測試，假 store／假事件，不碰 DB。"""
import threading
from datetime import datetime, time
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from uep.contracts.schedule import (
    AbandonWorkSignal,
    ActivityKind,
    InterruptFacts,
    PlanItem,
    SlotMark,
)
from uep.schedule import resolver
from uep.schedule.abandon_executor import AbandonExecutor
from uep.schedule.arbiter_shadow import PresenceArbiterShadow
from uep.schedule.planner import PresenceReading

TZ = "Asia/Taipei"
TPE = ZoneInfo(TZ)
WORK = PlanItem("work", ActivityKind.WORK, "discord", "weekly", weekdays=(1, 2, 3, 4, 5, 6, 7), local_start=time(9),
                local_end=time(10), tz=TZ)
SOCIAL = PlanItem("social", ActivityKind.SOCIAL, "discord", "weekly", weekdays=(1, 2, 3, 4, 5, 6, 7),
                  local_start=time(11), local_end=time(12), tz=TZ)
REST = PlanItem("rest", ActivityKind.REST, None, "weekly", weekdays=(1, 2, 3, 4, 5, 6, 7), local_start=time(13),
                local_end=time(14), tz=TZ)


def tpe(h, mi=0):
    return datetime(2026, 11, 12, h, mi, tzinfo=TPE)


class Reader:
    def __init__(self, items, marks):
        self.items, self.store = list(items), marks

    def resolve(self, at=None):
        return resolver.resolve(self.items, None, at, TZ, plan_revision=1, marks=self.store.active_marks())

    def tz(self):
        return TZ


class FakeMarks:
    """假的 SlotMarkWriter：同 cause 冪等，記錄呼叫。"""

    def __init__(self, fail=False):
        self.marks, self.calls, self.fail = [], [], fail

    def begin_mark(self, *, kind, item_ref, planned_start_at, cause_request_id):
        self.calls.append((kind, item_ref, cause_request_id))
        if self.fail:
            raise RuntimeError("db down")
        for m in self.marks:
            if m.cause_request_id == cause_request_id:
                if (m.item_ref, m.planned_start_at) != (item_ref, planned_start_at):
                    raise ValueError("conflict")
                return m.mark_id
        mark_id = f"pmark_{len(self.marks) + 1}"
        self.marks.append(SlotMark(kind, item_ref, planned_start_at, tpe(9, 30), mark_id, cause_request_id))
        return mark_id

    def active_marks(self):
        return tuple(self.marks)


class Events:
    def __init__(self):
        self.log, self.threads, self.mark_written = [], set(), []

    def _note(self, *entry):
        self.threads.add(threading.get_ident())
        self.log.append(entry)

    def abandon_work(self, signal, result, reason_code, item_ref):
        self._note("abandon_work", signal.signal_id, result, reason_code, item_ref)

    def slot_marked(self, mark_id, kind, item_ref, cause_request_id):
        self._note("slot_marked", mark_id, kind, item_ref, cause_request_id)

    def arbitration_decided(self, *, source, ref, decision, executed, exec_reason, mark_written=False):
        self.mark_written.append(mark_written)
        self._note("arbitration_decided", source, ref, decision.outcome, decision.override_action, executed,
                   exec_reason)


def signal(signal_id="abandon:u1", decided_at=None, **kw):
    base = {"signal_id": signal_id, "actor_ref": "discord:1", "actor_authenticated": True,
            "source_platform": "discord", "path": "turn", "decided_at": decided_at or tpe(9, 20),
            "decider_id": "rules.abandon_work", "decider_version": "1"}
    base.update(kw)
    return AbandonWorkSignal(**base)


def executor(items=(WORK,), now=None, owner=True, marks=None, poke=None):
    now = now or tpe(9, 30)
    marks = marks or FakeMarks()
    events = Events()
    ex = AbandonExecutor(Reader(items, marks), marks, events, is_owner_fn=(lambda s: owner), clock=lambda: now,
                         poke=poke)
    return ex, marks, events


# ---- 放棄消費端執行器 ----

def test_accepted_signal_writes_idempotent_mark_events_and_pokes_planner():
    poked = []
    ex, marks, events = executor(poke=lambda: poked.append(1))
    result = ex.handle(signal())
    assert (result.status, result.reason_code) == ("ACCEPTED", "accepted")
    assert marks.calls == [("abandon", "work", "abandon:u1")]
    assert events.log == [("slot_marked", "pmark_1", "abandon", "work", "abandon:u1"),
                          ("abandon_work", "abandon:u1", "accepted", "accepted", "work")]
    assert poked == [1]
    # 同一 signal_id 重送：冪等，不重複寫、不重複發事件
    again = ex.handle(signal())
    assert (again.status, again.reason_code) == ("ACCEPTED", "duplicate") and again.mark.mark_id == "pmark_1"
    assert len(marks.marks) == 1 and len(events.log) == 2 and poked == [1]  # 沒有第二個 mark、沒有重複事件


def test_mark_flows_into_projection_as_fallback_rest():
    ex, marks, _ = executor()
    ex.handle(signal())
    slot = Reader([WORK], marks).resolve(tpe(9, 40))
    assert (slot.slot_state, slot.location, slot.activity_kind) == ("abandoned", "host", ActivityKind.REST)


@pytest.mark.parametrize(("kwargs", "reason"), [
    ({"owner": False}, "not_owner"),
    ({"items": (SOCIAL,), "now": tpe(11, 30)}, "not_in_work"),  # 非 WORK 一律忽略（PP-21）
    ({"items": (REST,), "now": tpe(13, 30)}, "not_in_work"),
    ({"items": ()}, "not_in_work"),
])
def test_ignored_signals_only_emit_abandon_work_ignored(kwargs, reason):
    poked = []
    ex, marks, events = executor(poke=lambda: poked.append(1), **kwargs)
    sig = signal(decided_at=kwargs.get("now", tpe(9, 30)))
    result = ex.handle(sig)
    assert (result.status, result.reason_code) == ("IGNORED", reason)
    assert marks.calls == [] and poked == []
    assert events.log == [("abandon_work", "abandon:u1", "ignored", reason, None)]


def test_stale_round_and_owner_failure_and_conflict():
    ex, marks, events = executor()
    assert ex.handle(signal(decided_at=datetime(2026, 11, 11, 9, 20, tzinfo=TPE))).reason_code == "stale_round"
    boom = AbandonExecutor(Reader([WORK], marks), marks, events, is_owner_fn=lambda s: 1 / 0, clock=lambda: tpe(9, 30))
    assert boom.handle(signal("abandon:u2")).reason_code == "not_owner"  # 判定失敗＝非 owner（fail-closed）
    default = AbandonExecutor(Reader([WORK], marks), marks, events, clock=lambda: tpe(9, 30))
    assert default.handle(signal("abandon:u3")).reason_code == "owner_unverifiable"  # 沒有任何 owner 來源＝無法判定
    conflict_marks = FakeMarks()
    conflict_marks.marks.append(SlotMark("abandon", "other", tpe(1), tpe(1), "pmark_x", "abandon:u4"))
    # 同 signal_id 以不同參數建立過 mark：由 begin_mark 的 ValueError 擋下（不靠預查）
    ex2 = AbandonExecutor(Reader([WORK], FakeMarks()), conflict_marks, Events(), is_owner_fn=lambda s: True,
                          clock=lambda: tpe(9, 30))
    assert ex2.handle(signal("abandon:u4")).reason_code == "signal_id_conflict"
    hidden = FakeMarks()
    hidden.active_marks = lambda: ()  # 查不到但 begin_mark 發現衝突
    hidden.marks.append(SlotMark("abandon", "other", tpe(1), tpe(1), "pmark_y", "abandon:u5"))
    ex3 = AbandonExecutor(Reader([WORK], FakeMarks()), hidden, Events(), is_owner_fn=lambda s: True,
                          clock=lambda: tpe(9, 30))
    assert ex3.handle(signal("abandon:u5")).reason_code == "signal_id_conflict"


# ---- 仲裁 on：判決事件與有限落地 ----

def arbiter(items, presence, mode="on", now=None, probe=None, marks=None, owner_signal=True):
    now = now or tpe(11, 30)
    marks = marks or FakeMarks()
    events = Events()
    reader = Reader(items, marks)
    ex = AbandonExecutor(reader, marks, events, is_owner_fn=lambda s: owner_signal, clock=lambda: now)
    shadow = PresenceArbiterShadow(
        reader, lambda: presence, None, lambda: probe or InterruptFacts(), clock=lambda: now, mode=mode,
        events=events if mode == "on" else None, mark_writer=marks if mode == "on" else None,
        abandon_executor=ex if mode == "on" else None, metrics_inc=None)
    return shadow, marks, events


def proposal(intent="presence_request", target="host"):
    return SimpleNamespace(intent=intent, target_location=target)


def request(shadow, p, request_id="r-1", **kw):
    base = {"request_id": request_id, "actor_ref": "discord:1", "actor_authenticated": True, "is_owner": True,
            "source_platform": "discord"}
    base.update(kw)
    shadow.on_presence_request(p, **base)


def test_mark_abandon_decision_is_executed_in_on_mode():
    shadow, marks, events = arbiter((SOCIAL,), PresenceReading("ACTIVE", "discord"))
    request(shadow, proposal(target="host"))
    assert marks.calls == [("abandon", "social", "r-1")]
    assert events.log == [
        ("slot_marked", "pmark_1", "abandon", "social", "r-1"),
        ("arbitration_decided", "request", "r-1", "ACCEPTED_TRANSFER", "mark_abandon", False,
         "mark_written_transfer_not_implemented")]  # 只寫了 mark、移轉沒做：部分完成，executed=False
    assert events.mark_written == [True]
    at_host, marks2, events2 = arbiter((SOCIAL,), PresenceReading("NONE", None))
    request(at_host, proposal(target="host"))
    assert events2.log[-1][-2:] == (True, "mark_written") and marks2.calls  # 人已在主機：整個判決完成
    assert events2.mark_written == [True]


def test_other_accepted_decisions_are_never_executed():
    shadow, marks, events = arbiter((REST,), PresenceReading("NONE", None), now=tpe(13, 30))
    request(shadow, proposal(target="desktop"))
    assert marks.calls == []
    assert events.log == [("arbitration_decided", "request", "r-1", "ACCEPTED_TRANSFER", "begin", False,
                           "transfer_not_implemented")]
    rejected, m3, e3 = arbiter((WORK,), PresenceReading("ACTIVE", "discord"), now=tpe(9, 30))
    request(rejected, proposal(target="desktop"))
    assert e3.log == [("arbitration_decided", "request", "r-1", "REJECTED", "none", False, "not_accepted")]
    assert m3.calls == []


def test_mark_write_failure_is_reported_not_raised():
    shadow, _marks, events = arbiter((SOCIAL,), PresenceReading("ACTIVE", "discord"), marks=FakeMarks(fail=True))
    request(shadow, proposal(target="host"))
    assert events.log == [("arbitration_decided", "request", "r-1", "ACCEPTED_TRANSFER", "mark_abandon", False,
                           "mark_write_failed")]


def test_directive_decisions_are_event_only():
    shadow, marks, events = arbiter((WORK,), PresenceReading("NONE", None), now=tpe(9, 30))
    slot = Reader((WORK,), marks).resolve(tpe(9, 30))
    directive = resolver.reconcile_directive(slot, "host", tpe(9, 30))
    shadow.on_directive_event(SimpleNamespace(payload={"directive_id": directive.directive_id}))
    assert events.log == [("arbitration_decided", "directive", directive.directive_id, "ACCEPTED_TRANSFER", "none",
                           False, "transfer_not_implemented")]
    assert marks.calls == []


def test_shadow_mode_emits_nothing_and_writes_nothing():
    shadow, marks, events = arbiter((SOCIAL,), PresenceReading("ACTIVE", "discord"), mode="shadow")
    request(shadow, proposal(target="host"))
    shadow.on_abandon_signal(signal(decided_at=tpe(11, 25)))
    assert len(shadow.judgments) == 1 and shadow.judgments[0].executed is False
    assert marks.calls == [] and events.log == []


def test_on_mode_requires_events_and_writer_and_known_mode():
    with pytest.raises(ValueError):
        PresenceArbiterShadow(Reader((), FakeMarks()), lambda: None, None, InterruptFacts, mode="on")
    with pytest.raises(ValueError):
        PresenceArbiterShadow(Reader((), FakeMarks()), lambda: None, None, InterruptFacts, mode="bogus")


# ---- 決策層 sink：只排佇列、背景執行緒消化 ----

class FakeOrchestrator:
    """模擬決策層：組裝時把核心的 sink 掛上，之後 turn 路徑呼叫它們。"""

    def __init__(self):
        self.on_abandon_signal = None
        self.on_presence_request = None

    def emit_request(self, p, **kw):
        if self.on_presence_request is not None:
            self.on_presence_request(p, **kw)

    def emit_abandon(self, sig):
        if self.on_abandon_signal is not None:
            self.on_abandon_signal(sig)


def test_sinks_enqueue_and_a_background_thread_does_the_work():
    shadow, _marks, events = arbiter((SOCIAL, WORK), PresenceReading("ACTIVE", "discord"), now=tpe(9, 30))
    orchestrator = FakeOrchestrator()
    orchestrator.on_abandon_signal, orchestrator.on_presence_request = shadow.on_abandon_signal, shadow.on_presence_request
    shadow.start()
    try:
        main_thread = threading.get_ident()
        orchestrator.emit_abandon(signal())
        orchestrator.emit_request(proposal(target="host"), request_id="r-9", actor_ref="discord:1",
                                  actor_authenticated=True, is_owner=True, source_platform="discord")
        assert shadow.drain(5.0)
    finally:
        shadow.stop()
    assert main_thread not in events.threads and events.threads  # 呼叫端執行緒沒做 DB／發事件
    types = [e[0] for e in events.log]
    assert "abandon_work" in types and "arbitration_decided" in types
    assert any(e[:3] == ("abandon_work", "abandon:u1", "accepted") for e in events.log)


def test_presence_request_sink_gating():
    shadow, _marks, events = arbiter((SOCIAL,), PresenceReading("ACTIVE", "discord"))
    counts = {}
    shadow._metrics_inc = lambda name: counts.__setitem__(name, counts.get(name, 0) + 1)
    request(shadow, proposal(intent="message", target="host"), request_id="a")  # 不是 presence_request
    request(shadow, proposal(target=None), request_id="b")  # 沒有 target
    request(shadow, proposal(target="mars"), request_id="c")  # target 不合法
    request(shadow, proposal(), request_id="d", actor_authenticated=False)
    request(shadow, proposal(), request_id="e", is_owner=False)  # member 先不建
    assert events.log == [] and not shadow.judgments
    base = "schedule.arbiter.shadow.request."
    assert counts[base + "not_a_move"] == 2 and counts[base + "no_target"] == 1
    assert counts[base + "unauthenticated"] == 1 and counts[base + "member_unverified"] == 1
    request(shadow, proposal(), request_id="ok")
    assert len(shadow.judgments) == 1


def test_queue_overflow_is_counted_not_blocking():
    shadow, _, _ = arbiter((SOCIAL,), PresenceReading("NONE", None))
    counts = {}
    shadow._metrics_inc = lambda name: counts.__setitem__(name, counts.get(name, 0) + 1)
    shadow._queue = type(shadow._queue)(maxsize=1)
    shadow._thread = threading.Thread(target=lambda: threading.Event().wait(2.0), daemon=True)
    shadow._thread.start()  # 模擬「背景執行緒存活但沒在消化」
    for i in range(3):
        request(shadow, proposal(), request_id=f"q{i}")
    assert counts["schedule.arbiter.shadow.queue_full"] == 2
