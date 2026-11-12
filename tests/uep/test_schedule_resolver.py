"""行程解析器（PP-0，純函式）：VC-PP-1/3/4/5/7(純)/22(純) 與 Q-PP-4 藍圖／投影。"""
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from uep.contracts.schedule import (
    ActivityKind,
    OverrideView,
    PlanDelay,
    PlanItem,
    PlanSource,
)
from uep.schedule import resolver as r

TPE = ZoneInfo("Asia/Taipei")
TZ = "Asia/Taipei"


def tpe(y, mo, d, h=0, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=TPE)


def weekly(item_id, start, end, *, days=(1, 2, 3, 4, 5, 6, 7), tz=TZ, kind=ActivityKind.WORK,
           location="discord", role="user", priority=50, **kw):
    return PlanItem(item_id=item_id, activity_kind=kind, location=location, recurrence="weekly", role=role,
                    weekdays=days, local_start=start, local_end=end, tz=tz, priority=priority, **kw)


REST = weekly("rest", time(3, 0), time(5, 30), kind=ActivityKind.REST, location=None, role="rest_window",
              priority=10)


def _override(started, expires, round_end, loc="desktop", status="active"):
    return OverrideView("povr1", loc, ActivityKind.WORK, started, expires, round_end, status)


def test_empty_store_defaults_to_host_rest():  # VC-PP-1
    s = r.resolve([], None, tpe(2026, 11, 12, 14), TZ, plan_revision=3)
    assert (s.availability, s.location, s.activity_kind, s.source) == (
        "available", "host", ActivityKind.REST, PlanSource.DEFAULT)
    assert [x.mode for x in s.suggestions] == ["REST"] and s.item_ref is None and s.plan_revision == 3
    assert s.next_boundary_at is None


def test_null_location_item_is_host_rest_even_if_kind_work():  # PP-2 / X3
    it = PlanItem("a", ActivityKind.WORK, None, "once", once_start_at=tpe(2026, 11, 12, 9),
                  once_end_at=tpe(2026, 11, 12, 10))
    s = r.resolve([it], None, tpe(2026, 11, 12, 9, 30), TZ)
    assert (s.location, s.activity_kind, s.source) == ("host", ActivityKind.REST, PlanSource.ITEM)


def test_rest_window_cross_midnight_and_timezone():  # VC-PP-3 / VC-PP-6
    win = weekly("w", time(23, 0), time(1, 0), kind=ActivityKind.REST, location=None, role="rest_window")
    s = r.resolve([win], None, tpe(2026, 11, 13, 0, 30), TZ)
    assert s.source == PlanSource.REST_WINDOW
    assert [x.mode for x in s.suggestions] == ["SLEEP", "REST"]
    assert s.next_boundary_at == tpe(2026, 11, 13, 1)
    assert s.starts_at == tpe(2026, 11, 12, 23)
    assert r.resolve([win], None, tpe(2026, 11, 13, 1), TZ).source == PlanSource.DEFAULT


def test_weekdays_belong_to_start_day():
    # 2026-11-13 是週五（ISO 5）；週五 22:00–02:00 延伸到週六凌晨
    it = weekly("f", time(22), time(2), days=(5,))
    assert r.resolve([it], None, tpe(2026, 11, 14, 1), TZ).item_ref == "f"
    assert r.resolve([it], None, tpe(2026, 11, 15, 1), TZ).item_ref is None


def test_dst_zone_spring_forward():  # VC-PP-3：America/New_York 2026-03-08 02:00 跳 03:00
    ny = "America/New_York"
    it = weekly("n", time(21), time(23), days=(7,), tz=ny)  # 週日 21–23
    inside = datetime(2026, 3, 8, 21, 30, tzinfo=ZoneInfo(ny))
    s = r.resolve([it], None, inside, ny)
    assert s.item_ref == "n"
    assert s.next_boundary_at == datetime(2026, 3, 8, 23, tzinfo=ZoneInfo(ny)).astimezone(UTC)
    before = datetime(2026, 3, 1, 21, 30, tzinfo=ZoneInfo(ny))
    assert r.resolve([it], None, before, ny).item_ref == "n"
    assert before.astimezone(UTC).hour != inside.astimezone(UTC).hour  # 同一本地時間，UTC 偏移不同


def test_overlap_tiebreak_deterministic():  # VC-PP-4
    a = weekly("a", time(9), time(12), priority=50)
    b = weekly("b", time(9), time(12), priority=60)
    now = tpe(2026, 11, 12, 10)
    assert r.resolve([a, b], None, now, TZ).item_ref == "b"
    narrow = weekly("n", time(9), time(11), priority=50)
    assert r.resolve([a, narrow], None, now, TZ).item_ref == "n"
    c1 = weekly("c1", time(9), time(12), created_at=tpe(2026, 1, 1))
    c2 = weekly("c2", time(9), time(12), created_at=tpe(2026, 2, 1))
    assert r.resolve([c1, c2], None, now, TZ).item_ref == "c2"
    d1, d2 = weekly("d1", time(9), time(12)), weekly("d2", time(9), time(12))
    assert r.resolve([d2, d1], None, now, TZ).item_ref == "d1"
    assert r.resolve([d1, d2], None, now, TZ) == r.resolve([d2, d1], None, now, TZ)


def test_explicit_item_truncates_rest_window():  # VC-PP-6
    it = weekly("x", time(4), time(4, 30))
    items = [REST, it]
    assert r.resolve(items, None, tpe(2026, 11, 12, 3, 30), TZ).source == PlanSource.REST_WINDOW
    s = r.resolve(items, None, tpe(2026, 11, 12, 4, 10), TZ)
    assert s.item_ref == "x" and s.source == PlanSource.ITEM
    back = r.resolve(items, None, tpe(2026, 11, 12, 4, 40), TZ)
    assert back.source == PlanSource.REST_WINDOW and back.starts_at == tpe(2026, 11, 12, 4, 30)


def test_valid_window_limits():
    it = weekly("v", time(9), time(12), valid_until=tpe(2026, 11, 12, 10))
    s = r.resolve([it], None, tpe(2026, 11, 12, 9, 30), TZ)
    assert s.next_boundary_at == tpe(2026, 11, 12, 10)
    assert r.resolve([it], None, tpe(2026, 11, 12, 10, 30), TZ).source == PlanSource.DEFAULT


def test_override_beats_item_and_expiry_returns_to_plan():  # VC-PP-5
    it = weekly("w", time(9), time(12), location="discord")
    ov = _override(tpe(2026, 11, 12, 10), tpe(2026, 11, 12, 11), tpe(2026, 11, 13))
    s = r.resolve([it], ov, tpe(2026, 11, 12, 10, 30), TZ)
    assert (s.source, s.location, s.override_state, s.next_boundary_at) == (
        PlanSource.OVERRIDE, "desktop", "active", tpe(2026, 11, 12, 11))
    back = r.resolve([it], ov, tpe(2026, 11, 12, 11), TZ)
    assert back.item_ref == "w" and back.location == "discord"
    # 回到的是「此刻」的格：override 跨過窗口邊界後落在預設層
    ov2 = _override(tpe(2026, 11, 12, 11), tpe(2026, 11, 12, 13), tpe(2026, 11, 13))
    assert r.resolve([it], ov2, tpe(2026, 11, 12, 13), TZ).source == PlanSource.DEFAULT


def test_override_pending_state_exposed():  # VC-PP-18（純函式部分）
    ov = _override(tpe(2026, 11, 12, 10), tpe(2026, 11, 12, 11), tpe(2026, 11, 13), status="pending")
    assert r.resolve([], ov, tpe(2026, 11, 12, 10, 5), TZ).override_state == "pending"


def test_override_constructor_mirrors_db_check():
    with pytest.raises(ValueError):
        _override(tpe(2026, 11, 12, 10), tpe(2026, 11, 13, 1), tpe(2026, 11, 13))


def test_round_isolation():  # VC-PP-22（純函式部分）
    started = tpe(2026, 11, 12, 15)
    end = r.round_end_at(started, TZ)
    assert end == tpe(2026, 11, 13)
    exp = r.override_expires_at(started, TZ)
    assert exp == tpe(2026, 11, 12, 19)
    late = r.override_expires_at(tpe(2026, 11, 12, 22), TZ)  # 22:00 + 4h 被輪次封頂
    assert late == end
    ov = OverrideView("o", "desktop", ActivityKind.WORK, tpe(2026, 11, 12, 22), late, end)
    assert r.resolve([], ov, tpe(2026, 11, 12, 23), TZ).source == PlanSource.OVERRIDE
    nxt = r.resolve([], ov, tpe(2026, 11, 13, 22, 30), TZ)  # 次日同時段
    assert nxt.source == PlanSource.DEFAULT and nxt.location == "host"
    assert r.override_end_reason(ov, tpe(2026, 11, 13, 0, 1)) == "round_ended"
    assert r.override_end_reason(ov, tpe(2026, 11, 12, 23)) is None
    short = OverrideView("o2", "desktop", ActivityKind.WORK, started, exp, end)
    assert r.override_end_reason(short, tpe(2026, 11, 12, 19, 1)) == "expired"
    assert r.override_end_reason(short, tpe(2026, 11, 14)) == "expired"


def test_round_bounds_use_plan_timezone():
    ny = "America/New_York"
    at = datetime(2026, 11, 12, 3, 0, tzinfo=UTC)  # NY 11/11 22:00
    lo, hi = r.round_bounds(at, ny)
    assert lo == datetime(2026, 11, 11, 0, tzinfo=ZoneInfo(ny)).astimezone(UTC)
    assert hi == datetime(2026, 11, 12, 0, tzinfo=ZoneInfo(ny)).astimezone(UTC)
    assert r.round_bounds(at, TZ)[0] == tpe(2026, 11, 12)  # meta.tz 變更後日界跟著變


def test_unavailable_is_a_state_not_an_exception():  # VC-PP-7（純函式部分）
    s = r.resolve([REST], None, tpe(2026, 11, 12, 4), "Not/AZone", plan_revision=9)
    assert (s.availability, s.location, s.suggestions, s.source, s.plan_revision) == (
        "unavailable", "host", (), PlanSource.DEFAULT, 9)
    broken = PlanItem("b", ActivityKind.WORK, "discord", "weekly")  # 缺 tz／local 欄位
    assert r.resolve([broken], None, tpe(2026, 11, 12, 4), TZ).availability == "unavailable"
    with pytest.raises(ValueError):  # 無時區的 now 是呼叫端錯誤
        r.resolve([], None, datetime(2026, 11, 12), TZ)


def test_upcoming_lists_cells_in_order():
    it = weekly("w", time(9), time(12))
    cells = r.upcoming([it], None, tpe(2026, 11, 12, 8), 6 * 3600, TZ)
    assert [c.source for c in cells] == [PlanSource.DEFAULT, PlanSource.ITEM, PlanSource.DEFAULT]
    assert [c.starts_at for c in cells[1:]] == [tpe(2026, 11, 12, 9), tpe(2026, 11, 12, 12)]


# ---- Q-PP-4：藍圖／投影 delay（壓縮） ----

def _two_items():
    return (weekly("a", time(20), time(21), location="discord"),
            weekly("b", time(21), time(22), location="desktop"))


def test_delay_extends_current_and_defers_next_start_with_original_end():
    a, b = _two_items()
    d = PlanDelay("a", tpe(2026, 11, 12, 20), tpe(2026, 11, 12, 21, 30))
    s = r.resolve([a, b], None, tpe(2026, 11, 12, 21, 10), TZ, delay=d)
    assert s.item_ref == "a" and s.delay_s == 1800 and s.ends_at == tpe(2026, 11, 12, 21, 30)
    nxt = r.resolve([a, b], None, tpe(2026, 11, 12, 21, 31), TZ, delay=d)
    assert nxt.item_ref == "b" and nxt.starts_at == tpe(2026, 11, 12, 21, 30)
    assert nxt.ends_at == tpe(2026, 11, 12, 22)  # 壓縮：維持原結束
    assert nxt.delay_s == 1800
    assert r.resolve([a, b], None, tpe(2026, 11, 12, 21, 10), TZ).item_ref == "b"  # 藍圖不被改寫


def test_delay_eats_next_item_when_compressed_to_nothing():
    a, b = _two_items()
    d = PlanDelay("a", tpe(2026, 11, 12, 20), tpe(2026, 11, 12, 22, 30))
    assert r.resolve([a, b], None, tpe(2026, 11, 12, 22, 10), TZ, delay=d).item_ref == "a"
    assert r.resolve([a, b], None, tpe(2026, 11, 12, 22, 31), TZ, delay=d).source == PlanSource.DEFAULT


def test_delay_does_not_cross_round_and_ignored_for_unknown_slot():
    a = weekly("a", time(23), time(23, 30), location="discord")
    nxt_day = weekly("c", time(0, 10), time(1), location="desktop")
    d = PlanDelay("a", tpe(2026, 11, 12, 23), tpe(2026, 11, 13, 0, 40))
    s = r.resolve([a, nxt_day], None, tpe(2026, 11, 12, 23, 59), TZ, delay=d)
    assert s.item_ref == "a" and s.ends_at == tpe(2026, 11, 13)  # 封頂在輪次邊界
    after = r.resolve([a, nxt_day], None, tpe(2026, 11, 13, 0, 20), TZ, delay=d)
    assert after.item_ref == "c" and after.delay_s == 0
    ghost = PlanDelay("zzz", tpe(2026, 11, 12, 23), tpe(2026, 11, 13))
    assert r.resolve([a], None, tpe(2026, 11, 12, 23, 40), TZ, delay=ghost).source == PlanSource.DEFAULT


def test_make_delay_and_override_still_wins_over_delay():
    a, b = _two_items()
    now = tpe(2026, 11, 12, 21, 5)
    cur = r.resolve([a, b], None, tpe(2026, 11, 12, 20, 30), TZ)
    delay = r.make_delay(cur, tpe(2026, 11, 12, 20), now, r.DirectivePolicy(retry_s=600))
    assert delay.held_until == now + timedelta(seconds=600) and delay.item_ref == "a"
    assert r.make_delay(r.resolve([], None, now, TZ), now, now) is None
    ov = _override(tpe(2026, 11, 12, 21), tpe(2026, 11, 12, 23), tpe(2026, 11, 13), loc="website")
    assert r.resolve([a, b], ov, now, TZ, delay=delay).source == PlanSource.OVERRIDE


# ---- reconcile / defer（不重播過期項） ----

def test_reconcile_directive_boundaries_and_no_replay():
    it = weekly("w", time(9), time(12), location="discord")
    slot = r.resolve([it], None, tpe(2026, 11, 12, 10), TZ, plan_revision=4)
    d = r.reconcile_directive(slot, None, tpe(2026, 11, 12, 10))
    assert d.target == "discord" and d.expires_at == tpe(2026, 11, 12, 12) and d.not_before == tpe(2026, 11, 12, 10)
    assert d.directive_id == f"w:{slot.starts_at.astimezone(UTC).isoformat()}:4"
    assert r.reconcile_directive(slot, "discord", tpe(2026, 11, 12, 10)) is None  # 已一致
    assert r.reconcile_directive(slot, None, tpe(2026, 11, 12, 12, 1)) is None  # 格已過，不重播
    assert r.reconcile_directive(r.unavailable_slot(tpe(2026, 11, 12, 10)), "desktop", tpe(2026, 11, 12, 10)) is None
    ov = r.resolve([it], _override(tpe(2026, 11, 12, 9), tpe(2026, 11, 12, 11), tpe(2026, 11, 13)),
                   tpe(2026, 11, 12, 10), TZ)
    assert r.reconcile_directive(ov, None, tpe(2026, 11, 12, 10)) is None
    default = r.resolve([], None, tpe(2026, 11, 12, 10), TZ)
    dd = r.reconcile_directive(default, "desktop", tpe(2026, 11, 12, 10))
    assert dd.target == "host" and dd.expires_at == tpe(2026, 11, 12, 11)


def test_defer_decision_keeps_timer_and_schedules_next_eval():  # PP-14 / VC-PP-20（純函式部分）
    p = r.DirectivePolicy(defer_max_s=1800, retry_s=300)
    first = tpe(2026, 11, 12, 3)
    ends = tpe(2026, 11, 12, 5, 30)
    for minutes in (1, 5, 29):  # 多次 tick 不重置
        assert r.defer_decision(first, first + timedelta(minutes=minutes), ends, p).action == "wait"
    drop = r.defer_decision(first, first + timedelta(minutes=30), ends, p)
    assert (drop.action, drop.reason_code, drop.next_eval_at) == (
        "drop_attempt", "defer_max", first + timedelta(minutes=35))
    assert r.defer_decision(first, tpe(2026, 11, 12, 5, 28), ends, p).next_eval_at is None
    assert r.defer_decision(first, tpe(2026, 11, 12, 5, 30), ends, p).action == "drop_final"
