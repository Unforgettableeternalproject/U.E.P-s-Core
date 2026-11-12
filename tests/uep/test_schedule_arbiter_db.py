"""AR-3a DB 整合（需要 UEP_TEST_DATABASE_URL；用真的 PlanStore／EventPublisher）。

signal → mark → resolver 投影轉 REST → 事件寫入 journal；同一 signal_id 重送冪等；非 WORK／非 owner 一律 IGNORED；
仲裁 on 的判決事件與 mark_abandon 落地。
"""
from datetime import datetime, time
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from uep.contracts.schedule import AbandonWorkSignal, ActivityKind, InterruptFacts
from uep.events.bus import EventBus
from uep.events.models import EventRow
from uep.events.publisher import EventPublisher
from uep.events.registry import DEFAULT_REGISTRY
from uep.schedule.abandon_executor import AbandonExecutor
from uep.schedule.arbiter_shadow import PresenceArbiterShadow
from uep.schedule.events import ScheduleEvents
from uep.schedule.planner import PresenceReading
from uep.schedule.store import PlanActor, PlanItemCommand, PlanStore

TZ = "Asia/Taipei"
TPE = ZoneInfo(TZ)
OWNER = PlanActor("owner_console:dev", "owner_console", True, True)


def tpe(h, mi=0):
    return datetime(2026, 11, 12, h, mi, tzinfo=TPE)


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock(tpe(9, 30))


@pytest.fixture
def store(session_factory, clock):
    return PlanStore(session_factory, clock=clock)


@pytest.fixture
def events(session_factory):
    return ScheduleEvents(EventPublisher(session_factory, EventBus()), persona_id="uep")


def add_item(store, kind, start, end, location="discord"):
    cmd = PlanItemCommand(activity_kind=kind, location=location, recurrence="weekly", weekdays=(1, 2, 3, 4, 5, 6, 7),
                          local_start=start, local_end=end, tz=TZ)
    res = store.create_item(OWNER, cmd, expected_revision=store.revision())
    assert res.status == "OK", res
    return res.ref_id


def signal(signal_id="abandon:u1", **kw):
    base = {"signal_id": signal_id, "actor_ref": "discord:1", "actor_authenticated": True,
            "source_platform": "discord", "path": "turn", "decided_at": tpe(9, 20),
            "decider_id": "rules.abandon_work", "decider_version": "1"}
    base.update(kw)
    return AbandonWorkSignal(**base)


def rows(session_factory, event_type):
    with session_factory() as s:
        return list(s.scalars(select(EventRow).where(EventRow.event_type == event_type)))


def test_signal_to_mark_to_projection_to_journal_and_idempotent_replay(session_factory, store, events, clock):
    item_id = add_item(store, "work", time(9), time(10))
    poked = []
    executor = AbandonExecutor(store, store, events, is_owner_fn=lambda s: True, tz_fn=store.tz, clock=clock,
                               poke=lambda: poked.append(1))
    result = executor.handle(signal())
    assert (result.status, result.reason_code) == ("ACCEPTED", "accepted") and poked == [1]
    [mark] = store.active_marks()
    assert (mark.kind, mark.item_ref, mark.cause_request_id) == ("abandon", item_id, "abandon:u1")
    clock.now = tpe(9, 40)
    projected = store.resolve()
    assert (projected.slot_state, projected.location, projected.activity_kind) == ("abandoned", "host", ActivityKind.REST)

    [marked] = rows(session_factory, "schedule.slot_marked")
    [abandon] = rows(session_factory, "schedule.abandon_work")
    assert marked.source == abandon.source == "core.schedule"
    assert marked.payload == {"mark_id": mark.mark_id, "kind": "abandon", "item_ref": item_id,
                              "cause_request_id": "abandon:u1"}
    assert abandon.payload == {"signal_id": "abandon:u1", "item_ref": item_id, "result": "accepted",
                               "reason_code": "accepted", "path": "turn", "decider_id": "rules.abandon_work",
                               "decider_version": "1"}
    for row in (marked, abandon):
        assert set(row.payload) <= DEFAULT_REGISTRY.lookup(row.event_type).payload_keys

    # 同一 signal_id 重送：冪等（同一個 mark、沒有第二組事件）
    again = executor.handle(signal())
    assert (again.status, again.reason_code) == ("ACCEPTED", "duplicate") and again.mark.mark_id == mark.mark_id
    assert len(store.active_marks()) == 1 and poked == [1]
    assert len(rows(session_factory, "schedule.slot_marked")) == 1
    assert len(rows(session_factory, "schedule.abandon_work")) == 1


def test_non_work_and_non_owner_signals_are_ignored(session_factory, store, events, clock):
    add_item(store, "social", time(9), time(10))
    owner = AbandonExecutor(store, store, events, is_owner_fn=lambda s: True, tz_fn=store.tz, clock=clock)
    res = owner.handle(signal("abandon:s1"))
    assert (res.status, res.reason_code) == ("IGNORED", "not_in_work")  # 非 WORK 不准放寬（PP-21）
    store.delete_item(OWNER, store.resolve().item_ref, expected_revision=store.revision())
    add_item(store, "work", time(9), time(10))
    stranger = AbandonExecutor(store, store, events, is_owner_fn=lambda s: False, tz_fn=store.tz, clock=clock)
    assert stranger.handle(signal("abandon:s2")).reason_code == "not_owner"
    assert store.active_marks() == ()
    ignored = rows(session_factory, "schedule.abandon_work")
    assert sorted((r.payload["signal_id"], r.payload["result"], r.payload["reason_code"]) for r in ignored) == [
        ("abandon:s1", "ignored", "not_in_work"), ("abandon:s2", "ignored", "not_owner")]
    assert rows(session_factory, "schedule.slot_marked") == []


def test_arbitration_on_writes_decision_events_and_only_executes_mark_abandon(session_factory, store, events, clock):
    social_id = add_item(store, "social", time(9), time(10))
    shadow = PresenceArbiterShadow(
        store, lambda: PresenceReading("ACTIVE", "discord"), store.active_override, InterruptFacts, clock=clock,
        mode="on", events=events, mark_writer=store)
    proposal = SimpleNamespace(intent="presence_request", target_location="host")
    shadow.on_presence_request(proposal, request_id="req-1", actor_ref="discord:1", actor_authenticated=True,
                               is_owner=True, source_platform="discord")
    [mark] = store.active_marks()
    assert (mark.item_ref, mark.cause_request_id) == (social_id, "req-1")
    [decided] = rows(session_factory, "schedule.arbitration_decided")
    assert decided.payload == {
        "source": "request", "outcome": "ACCEPTED_TRANSFER", "reason_code": "accepted", "band": 200,
        "override_action": "mark_abandon", "transfer_target": "host", "request_id": "req-1", "executed": False,
        "exec_reason": "mark_written_transfer_not_implemented", "mark_written": True}  # 部分完成：移轉沒做
    assert set(decided.payload) <= DEFAULT_REGISTRY.lookup("schedule.arbitration_decided").payload_keys
    # begin 判決不執行：沒有 override
    store.delete_item(OWNER, social_id, expected_revision=store.revision())
    shadow.on_presence_request(SimpleNamespace(intent="presence_request", target_location="desktop"),
                               request_id="req-2", actor_ref="discord:1", actor_authenticated=True, is_owner=True,
                               source_platform="discord")
    second = [r for r in rows(session_factory, "schedule.arbitration_decided") if r.payload["request_id"] == "req-2"]
    assert second[0].payload["executed"] is False and second[0].payload["exec_reason"] == "transfer_not_implemented"
    assert second[0].payload["mark_written"] is False
    assert store.active_override() is None


# ---- 重送偵測唯讀（find_mark_by_cause）----

def test_find_mark_by_cause_is_read_only_and_includes_ended_marks(store, clock):
    item_id = add_item(store, "work", time(9), time(10))
    planned = store.resolve().blueprint_start_at
    mark_id = store.begin_mark(kind="abandon", item_ref=item_id, planned_start_at=planned, cause_request_id="c-1")
    revision = store.revision()
    found = store.find_mark_by_cause("c-1")
    assert (found.mark_id, found.kind, found.item_ref, found.planned_start_at, found.ended_at) == (
        mark_id, "abandon", item_id, planned, None)
    assert store.find_mark_by_cause("nope") is None
    assert store.revision() == revision  # 唯讀：沒有 bump
    store.end_mark(mark_id, "item_ended")
    ended = store.find_mark_by_cause("c-1")
    assert ended.mark_id == mark_id and ended.ended_at is not None  # 已結束的也查得到


def test_replay_of_an_ended_mark_is_a_duplicate_with_zero_writes(session_factory, store, events, clock):
    add_item(store, "work", time(9), time(10))
    executor = AbandonExecutor(store, store, events, tz_fn=store.tz, clock=clock)
    first = executor.handle(signal(), is_owner=True)
    assert first.reason_code == "accepted"
    store.end_mark(first.mark.mark_id, "item_ended")
    revision, events_before = store.revision(), len(rows(session_factory, "schedule.abandon_work"))
    again = executor.handle(signal(), is_owner=True)
    assert (again.status, again.reason_code, again.mark.mark_id) == ("ACCEPTED", "duplicate", first.mark.mark_id)
    assert store.revision() == revision and store.active_marks() == ()  # 沒有寫入、沒有新 mark
    assert len(rows(session_factory, "schedule.abandon_work")) == events_before


def test_other_causes_abandoned_slot_replayed_by_another_signal_writes_nothing(session_factory, store, events, clock):
    item_id = add_item(store, "work", time(9), time(10))
    planned = store.resolve().blueprint_start_at
    store.begin_mark(kind="abandon", item_ref=item_id, planned_start_at=planned, cause_request_id="req-x")
    clock.now = tpe(9, 40)
    assert store.resolve().slot_state == "abandoned"
    revision = store.revision()
    executor = AbandonExecutor(store, store, events, tz_fn=store.tz, clock=clock)
    result = executor.handle(signal("abandon:other", decided_at=tpe(9, 35)), is_owner=True)
    assert (result.status, result.reason_code) == ("IGNORED", "not_in_work")
    assert store.revision() == revision  # revision 不變
    assert [m.cause_request_id for m in store.active_marks()] == ["req-x"]  # mark 數量不變
    assert store.find_mark_by_cause("abandon:other") is None
    [ignored] = rows(session_factory, "schedule.abandon_work")
    assert ignored.payload["result"] == "ignored" and ignored.payload["reason_code"] == "not_in_work"
