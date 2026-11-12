"""PP-0b／審查修正的回歸測試：directive_id 穩定、rest_window 不順延、逐項隔離、藍圖錨、upcoming 推窗、
延後上限、SLEEP 休息時段、暫停／放棄（SlotMark）、standby、通知、abandon 訊號消費。"""
import logging
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from uep.contracts.schedule import (
    AbandonWorkSignal,
    ActivityKind,
    BehaviorSuggestion,
    OverrideView,
    PlanDelay,
    PlanItem,
    PlanSlot,
    PlanSource,
    SlotMark,
)
from uep.schedule import abandon as ab
from uep.schedule import behavior as b
from uep.schedule import resolver as r

TZ = "Asia/Taipei"
TPE = ZoneInfo(TZ)


def tpe(y, mo, d, h=0, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=TPE)


def weekly(item_id, start, end, *, days=(1, 2, 3, 4, 5, 6, 7), kind=ActivityKind.WORK, location="discord",
           role="user", priority=50, **kw):
    return PlanItem(item_id=item_id, activity_kind=kind, location=location, recurrence="weekly", role=role,
                    weekdays=days, local_start=start, local_end=end, tz=TZ, priority=priority, **kw)


REST = weekly("rest", time(3), time(5, 30), kind=ActivityKind.REST, location=None, role="rest_window", priority=10)


# ---- 1. 預設層 directive_id 穩定 ----

def test_default_slot_directive_id_does_not_drift_with_time():
    d1 = r.reconcile_directive(r.resolve([], None, tpe(2026, 11, 12, 10), TZ), "desktop", tpe(2026, 11, 12, 10))
    d2 = r.reconcile_directive(r.resolve([], None, tpe(2026, 11, 14, 16), TZ), "desktop", tpe(2026, 11, 14, 16))
    assert d1.directive_id == d2.directive_id  # 沒有任何邊界：固定錨，不隨日期變


def test_default_slot_id_stable_across_days_after_last_item():
    once = PlanItem("o", ActivityKind.WORK, "discord", "once", once_start_at=tpe(2026, 11, 12, 9),
                    once_end_at=tpe(2026, 11, 12, 10))
    ids = {r.reconcile_directive(r.resolve([once], None, t, TZ), "desktop", t).directive_id
           for t in (tpe(2026, 11, 12, 11), tpe(2026, 11, 14, 11), tpe(2026, 11, 17, 11))}
    assert len(ids) == 1  # 7 天內都錨在 10:00 那個邊界


# ---- 2. rest_window 不順延 ----

def test_rest_window_is_not_delayed_nor_eaten_and_cannot_anchor():
    a = weekly("a", time(2), time(3), location="discord")
    delay = PlanDelay("a", tpe(2026, 11, 12, 2), tpe(2026, 11, 12, 4))
    s = r.resolve([a, REST], None, tpe(2026, 11, 12, 3, 30), TZ, delay=delay)
    assert s.source == PlanSource.ITEM and s.item_ref == "a"  # a 延長（priority 50 > 10），窗口藍圖未動
    # 窗口在藍圖時間仍存在：延長結束後立即是窗口，起點仍是 03:00 藍圖
    after = r.resolve([a, REST], None, tpe(2026, 11, 12, 4, 1), TZ, delay=delay)
    assert after.source == PlanSource.REST_WINDOW and after.delay_s == 0
    # rest_window 當 anchor：delay 被忽略
    bad = PlanDelay("rest", tpe(2026, 11, 12, 3), tpe(2026, 11, 12, 7))
    s = r.resolve([REST], None, tpe(2026, 11, 12, 6), TZ, delay=bad)
    assert s.source == PlanSource.DEFAULT
    cur = r.resolve([REST], None, tpe(2026, 11, 12, 4), TZ)
    step = r.make_delay(cur, tpe(2026, 11, 12, 5, 31), tpe(2026, 11, 12, 5, 30), None)
    assert (step.delay, step.action, step.reason_code) == (None, "not_eligible", "rest_window_fixed")


# ---- 3. 逐項隔離 ----

def test_bad_item_is_skipped_with_warning_and_others_still_resolve(caplog):
    good = weekly("good", time(9), time(12))
    broken = PlanItem("b", ActivityKind.WORK, "discord", "weekly")  # 缺 tz／local
    badloc = weekly("c", time(9), time(12), location="mars")
    badtz = PlanItem("d", ActivityKind.WORK, "discord", "weekly", weekdays=(1,), local_start=time(9),
                     local_end=time(12), tz="Not/AZone")
    ov = OverrideView("o", "desktop", ActivityKind.WORK, tpe(2026, 11, 12, 14), tpe(2026, 11, 12, 16),
                      tpe(2026, 11, 13))
    with caplog.at_level(logging.WARNING, logger="uep.schedule.resolver"):
        s = r.resolve([broken, badloc, badtz, good], None, tpe(2026, 11, 12, 10), TZ)
        o = r.resolve([broken, good], ov, tpe(2026, 11, 12, 15), TZ)
    assert s.availability == "available" and s.item_ref == "good"
    assert o.source == PlanSource.OVERRIDE  # override 不受壞項影響
    assert sum("行程項資料無效" in rec.message for rec in caplog.records) >= 4


def test_programming_errors_propagate():
    bad = weekly("x", time(9), time(12))
    object.__setattr__(bad, "weekdays", None)  # 程式錯誤：TypeError 不可被吞掉
    with pytest.raises(TypeError):
        r.resolve([bad], None, tpe(2026, 11, 12, 10), TZ)


def test_unavailable_carries_reason_code():
    s = r.resolve([], None, tpe(2026, 11, 12, 10), "Not/AZone")
    assert s.availability == "unavailable" and s.reason_code == "tz_unavailable"


# ---- 4. 藍圖錨 ----

def test_blueprint_start_exposed_and_missing_anchor_is_observable(caplog):
    a = weekly("a", time(20), time(21))
    s = r.resolve([a], None, tpe(2026, 11, 12, 20, 30), TZ)
    assert s.blueprint_start_at == tpe(2026, 11, 12, 20)
    stripped = PlanSlot(**{**s.__dict__, "blueprint_start_at": None})
    with pytest.raises(r.DelayAnchorError):
        r.make_delay(stripped, tpe(2026, 11, 12, 21), tpe(2026, 11, 12, 21), None)
    ghost = PlanDelay("a", tpe(2026, 11, 11, 3), tpe(2026, 11, 12, 22))  # anchor 不在藍圖
    with caplog.at_level(logging.WARNING, logger="uep.schedule.resolver"):
        r.resolve([a], None, tpe(2026, 11, 12, 20, 30), TZ, delay=ghost)
    assert any("anchor" in rec.message for rec in caplog.records)


# ---- 5. upcoming 推窗 ----

def test_upcoming_continues_past_scan_window():
    it = weekly("w", time(9), time(10), days=(5,))  # 每週五；2026-11-13 是週五
    cells = r.upcoming([it], None, tpe(2026, 11, 14, 12), 6 * 86400, TZ)  # 週六起 6 天，含下週五
    assert [c.item_ref for c in cells] == [None, "w", None]
    assert cells[1].starts_at == tpe(2026, 11, 20, 9)
    slot = r.resolve([it], None, tpe(2026, 11, 14, 12), TZ)
    assert slot.next_boundary_at is None or slot.next_boundary_at > tpe(2026, 11, 14, 12)


# ---- 6. 延後上限與 delay ----

def test_make_delay_respects_defer_cap_and_gives_next_eval():
    a = weekly("a", time(20), time(21))
    cur = r.resolve([a], None, tpe(2026, 11, 12, 20, 30), TZ)
    p = r.DirectivePolicy(defer_max_s=1800, retry_s=300)
    first = tpe(2026, 11, 12, 21)
    hard_end = tpe(2026, 11, 12, 23)  # 目標格自己的結束，與 delay 投影終點分開
    wait = r.make_delay(cur, first + timedelta(minutes=10), first, hard_end, p)
    assert wait.action == "wait" and wait.delay.held_until == first + timedelta(minutes=15)
    drop = r.make_delay(cur, first + timedelta(minutes=30), first, hard_end, p)
    assert drop.delay is None and drop.action == "drop_attempt"
    assert drop.next_eval_at == first + timedelta(minutes=35)  # 不會因投影終點而永遠為 None
    assert r.make_delay(cur, hard_end, first, hard_end, p).action == "drop_final"


# ---- #8 SLEEP 與休息時段 / #7 全域停止 ----

NOW = datetime(2026, 11, 12, 4, tzinfo=TPE)


def _slot(source=PlanSource.ITEM, kind=ActivityKind.WORK):
    return PlanSlot("available", "discord", kind, source, "x", r.InterruptPolicy.SAFE_POINT,
                    r.suggestions_for(kind, source), NOW, None, None, 1)


def _run(slot, presence, current, **kw):
    return b.rationalize(b.RationalizeInput(slot=slot, presence_status=presence, current_behavior=current,
                                            now=NOW, **kw))


def test_sleep_not_switched_out_inside_rest_window_except_override():
    work = _slot()
    d = _run(work, "NONE", "SLEEP", in_rest_window=True)
    assert (d.outcome, d.reason_code) == ("HOLD", "sleep_rest_window")
    assert _run(work, "NONE", "SLEEP", in_rest_window=False).outcome != "HOLD"  # 時段已過
    assert _run(_slot(PlanSource.OVERRIDE), "NONE", "SLEEP", in_rest_window=True).reason_code != "sleep_rest_window"
    # 建議仍是 SLEEP 時不受影響
    win = _slot(PlanSource.REST_WINDOW, ActivityKind.REST)
    assert _run(win, "NONE", "SLEEP", in_rest_window=True).outcome == "NOOP"


def test_global_stop_does_not_force_out_of_work():
    s = _slot()
    d = _run(s, "ACTIVE", "WORK", global_stop=True)
    assert d.outcome == "NOOP" and d.target == "WORK"
    d = _run(s, "ACTIVE", "CHAT", global_stop=True)  # 進入仍被擋
    assert d.target == "CHAT"


# ---- PP-0b：SlotMark（暫停／放棄）、standby ----

def test_pause_keeps_location_and_suggests_chat_and_blocks_delay():
    a = weekly("a", time(20), time(22))
    mark = SlotMark("pause", "a", tpe(2026, 11, 12, 20), tpe(2026, 11, 12, 20, 40))
    s = r.resolve([a], None, tpe(2026, 11, 12, 21), TZ, marks=[mark])
    assert (s.slot_state, s.location, s.activity_kind, s.standby) == ("paused", "discord", ActivityKind.WORK, True)
    assert [x.mode for x in s.suggestions] == ["CHAT"] and s.suggestions[0].reason_code == "work_paused"
    assert r.make_delay(s, tpe(2026, 11, 12, 21), tpe(2026, 11, 12, 21), None).reason_code == "slot_paused"
    # pause 還沒開始前是 normal；格結束後 mark 無影響
    assert r.resolve([a], None, tpe(2026, 11, 12, 20, 10), TZ, marks=[mark]).slot_state == "normal"
    assert r.resolve([a], None, tpe(2026, 11, 12, 22, 10), TZ, marks=[mark]).source == PlanSource.DEFAULT
    # pause 優先於 delay：被暫停的出現不延長
    d = PlanDelay("a", tpe(2026, 11, 12, 20), tpe(2026, 11, 12, 23))
    assert r.resolve([a], None, tpe(2026, 11, 12, 22, 10), TZ, delay=d, marks=[mark]).source == PlanSource.DEFAULT


def test_abandon_turns_occurrence_into_host_rest_within_round_only():
    a = weekly("a", time(22), time(2))  # 跨午夜
    mark = SlotMark("abandon", "a", tpe(2026, 11, 12, 22), tpe(2026, 11, 12, 23))
    before = r.resolve([a], None, tpe(2026, 11, 12, 22, 30), TZ, marks=[mark])
    assert before.slot_state == "normal" and before.location == "discord"
    s = r.resolve([a], None, tpe(2026, 11, 12, 23, 30), TZ, marks=[mark])
    assert (s.slot_state, s.location, s.activity_kind, s.availability, s.item_ref, s.standby) == (
        "abandoned", "host", ActivityKind.REST, "available", "a", True)
    assert [x.reason_code for x in s.suggestions] == ["plan_abandoned"]
    assert s.next_boundary_at == tpe(2026, 11, 13)  # 輪次結束後依藍圖
    nxt = r.resolve([a], None, tpe(2026, 11, 13, 0, 30), TZ, marks=[mark])
    assert nxt.slot_state == "normal" and nxt.location == "discord"
    assert r.make_delay(s, tpe(2026, 11, 12, 23, 40), tpe(2026, 11, 12, 23, 40), None).reason_code == "slot_abandoned"
    # 藍圖不變：不帶 mark 仍是工作
    assert r.resolve([a], None, tpe(2026, 11, 12, 23, 30), TZ).location == "discord"
    # abandon 令 delay 失效
    d = PlanDelay("a", tpe(2026, 11, 12, 22), tpe(2026, 11, 13, 0, 30))
    assert r.resolve([a], None, tpe(2026, 11, 12, 23, 30), TZ, delay=d, marks=[mark]).slot_state == "abandoned"


def test_override_beats_marks():
    a = weekly("a", time(20), time(22))
    mark = SlotMark("abandon", "a", tpe(2026, 11, 12, 20), tpe(2026, 11, 12, 20, 30))
    ov = OverrideView("o", "website", ActivityKind.WORK, tpe(2026, 11, 12, 21), tpe(2026, 11, 12, 21, 30),
                      tpe(2026, 11, 13))
    assert r.resolve([a], ov, tpe(2026, 11, 12, 21, 10), TZ, marks=[mark]).source == PlanSource.OVERRIDE


def test_override_rejects_host_location():  # PP-17
    with pytest.raises(ValueError):
        OverrideView("o", "host", ActivityKind.REST, tpe(2026, 11, 12, 20), tpe(2026, 11, 12, 21),
                     tpe(2026, 11, 13))


def test_standby_flags():
    work = weekly("w", time(9), time(10))
    social = weekly("s", time(10), time(11), kind=ActivityKind.SOCIAL)
    rest_item = weekly("r", time(11), time(12), kind=ActivityKind.REST, location="host")
    def at(h, mi=0, items=None):
        return r.resolve(items or [work, social, rest_item, REST], None, tpe(2026, 11, 12, h, mi), TZ)
    assert at(9, 30).standby is False  # WORK
    assert at(10, 30).standby is False  # SOCIAL
    assert at(11, 30).standby is True  # REST
    assert at(4).standby is False and at(4).source == PlanSource.REST_WINDOW  # SLEEP 時段
    assert at(15).standby is True  # 預設 REST
    assert PlanSource.OVERRIDE and r.unavailable_slot(tpe(2026, 11, 12, 1)).standby is True


def test_suggestions_for_state_branches():
    assert [s.mode for s in r.suggestions_for(ActivityKind.WORK, PlanSource.ITEM, slot_state="paused")] == ["CHAT"]
    assert [s.reason_code for s in r.suggestions_for(ActivityKind.REST, PlanSource.ITEM, slot_state="abandoned")] == [
        "plan_abandoned"]


def test_slot_transition_notices():
    a = weekly("a", time(20), time(21))
    s_def = r.resolve([a], None, tpe(2026, 11, 12, 19), TZ)
    s_a = r.resolve([a], None, tpe(2026, 11, 12, 20, 10), TZ)
    notes = r.slot_transition(s_def, s_a)
    assert [(n.kind, n.slot.item_ref) for n in notes] == [("left", None), ("entered", "a")]
    assert r.slot_transition(None, s_a)[0].kind == "entered"
    # delay 延長同一項不算變化
    d = PlanDelay("a", tpe(2026, 11, 12, 20), tpe(2026, 11, 12, 21, 30))
    assert r.slot_transition(s_a, r.resolve([a], None, tpe(2026, 11, 12, 21, 10), TZ, delay=d)) == ()
    mark = SlotMark("pause", "a", tpe(2026, 11, 12, 20), tpe(2026, 11, 12, 20, 15))
    paused = r.resolve([a], None, tpe(2026, 11, 12, 20, 20), TZ, marks=[mark])
    assert r.slot_transition(s_a, paused)[-1].reason == "paused"
    assert r.slot_transition(paused, s_a)[-1].reason == "resumed"


# ---- abandon 訊號 ----

def _signal(**kw):
    base = {"signal_id": "abandon:utt-1", "actor_ref": "discord:123", "actor_authenticated": True,
            "source_platform": "discord", "path": "turn", "decided_at": tpe(2026, 11, 12, 21),
            "decider_id": "rules.abandon_work", "decider_version": "1"}
    base.update(kw)
    return AbandonWorkSignal(**base)


def test_abandon_signal_construction_is_fail_closed():
    for bad in ({"signal_id": "bad id"}, {"actor_ref": "Bernie Display"}, {"decided_at": datetime(2026, 11, 12)},
                {"reason_ref": "free text here"}, {"confidence": 1.5}, {"path": "other"}, {"decider_version": ""}):
        with pytest.raises(ValueError):
            _signal(**bad)
    assert _signal(confidence=0.9, reason_ref="blob:r1").confidence == 0.9


def test_consume_abandon_rules():
    a = weekly("a", time(20), time(22))
    now = tpe(2026, 11, 12, 21)
    slot = r.resolve([a], None, now, TZ)
    ok = ab.consume_abandon(_signal(), is_owner=True, now=now, slot=slot, tz=TZ)
    assert ok.status == "ACCEPTED" and ok.mark.kind == "abandon" and ok.mark.cause_request_id == "abandon:utt-1"
    assert ok.mark.planned_start_at == tpe(2026, 11, 12, 20) and ok.mark.item_ref == "a"
    assert ab.consume_abandon(_signal(), is_owner=False, now=now, slot=slot, tz=TZ).reason_code == "not_owner"
    assert ab.consume_abandon(_signal(actor_authenticated=False), is_owner=True, now=now, slot=slot,
                              tz=TZ).reason_code == "not_owner"
    stale = _signal(decided_at=tpe(2026, 11, 11, 21))
    assert ab.consume_abandon(stale, is_owner=True, now=now, slot=slot, tz=TZ).reason_code == "stale_round"
    idle = r.resolve([], None, now, TZ)
    assert ab.consume_abandon(_signal(), is_owner=True, now=now, slot=idle, tz=TZ).reason_code == "not_in_work"
    ov = OverrideView("o", "website", ActivityKind.WORK, tpe(2026, 11, 12, 20, 30), tpe(2026, 11, 12, 22),
                      tpe(2026, 11, 13))
    emergency = r.resolve([a], ov, now, TZ)  # WORK 中系統緊急只形成 override，不是放棄
    assert ab.consume_abandon(_signal(), is_owner=True, now=now, slot=emergency, tz=TZ).reason_code == "not_in_work"
    # 接受後的 mark 餵回投影 → fallback REST
    s2 = r.resolve([a], None, now + timedelta(minutes=1), TZ, marks=[ok.mark])
    assert s2.slot_state == "abandoned" and s2.location == "host"
    again = ab.consume_abandon(_signal(signal_id="abandon:utt-2"), is_owner=True, now=now + timedelta(minutes=1),
                               slot=s2, tz=TZ)
    assert again.reason_code == "not_in_work"


def test_behavior_suggestion_dataclass_unchanged():
    assert BehaviorSuggestion("REST", 0, "x").mode == "REST"
