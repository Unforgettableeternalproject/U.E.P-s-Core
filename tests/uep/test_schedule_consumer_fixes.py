"""消費端過期判斷與推窗終點補正的回歸測試（M1～M5）。"""
import logging
from datetime import datetime, time
from zoneinfo import ZoneInfo

import pytest

from uep.contracts.schedule import (
    AbandonWorkSignal,
    ActivityKind,
    PlanDelay,
    PlanItem,
    PlanSource,
    SlotMark,
)
from uep.schedule import abandon as ab
from uep.schedule import resolver as r

TZ = "Asia/Taipei"
TPE = ZoneInfo(TZ)


def tpe(y, mo, d, h=0, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=TPE)


def weekly(item_id, start, end, *, days=(1, 2, 3, 4, 5, 6, 7), kind=ActivityKind.WORK, location="discord"):
    return PlanItem(item_id=item_id, activity_kind=kind, location=location, recurrence="weekly",
                    weekdays=days, local_start=start, local_end=end, tz=TZ)


@pytest.fixture(autouse=True)
def _clear_warned():
    r._warned_keys.clear()
    yield
    r._warned_keys.clear()


def _signal(decided_at):
    return AbandonWorkSignal(signal_id="abandon:u1", actor_ref="discord:1", actor_authenticated=True,
                             source_platform="discord", path="turn", decided_at=decided_at,
                             decider_id="rules.abandon_work", decider_version="1")


def test_m1_abandon_fails_closed_when_tz_unavailable():
    a = weekly("a", time(20), time(22))
    now = tpe(2026, 11, 12, 21)
    slot = r.resolve([a], None, now, TZ)
    res = ab.consume_abandon(_signal(now), is_owner=True, now=now, slot=slot, tz="Not/AZone")
    assert (res.status, res.reason_code, res.mark) == ("IGNORED", "tz_unavailable", None)
    # not_owner 仍在前
    assert ab.consume_abandon(_signal(now), is_owner=False, now=now, slot=slot,
                              tz="Not/AZone").reason_code == "not_owner"


def test_m4_signal_older_than_slot_start_is_stale():
    a = weekly("a", time(20), time(22))
    now = tpe(2026, 11, 12, 21)
    slot = r.resolve([a], None, now, TZ)
    old = _signal(tpe(2026, 11, 12, 19, 30))  # 早於本工作格的投影起點（20:00）
    assert ab.consume_abandon(old, is_owner=True, now=now, slot=slot, tz=TZ).reason_code == "stale"
    ok = _signal(tpe(2026, 11, 12, 20))
    assert ab.consume_abandon(ok, is_owner=True, now=now, slot=slot, tz=TZ).status == "ACCEPTED"


def test_m3_upcoming_fills_previous_slot_end_when_window_advances():
    it = weekly("w", time(9), time(10), days=(5,))  # 週五；2026-11-13
    cells = r.upcoming([it], None, tpe(2026, 11, 14, 12), 6 * 86400, TZ)
    assert [c.item_ref for c in cells] == [None, "w", None]
    first = cells[0]
    assert first.ends_at == first.next_boundary_at == cells[1].starts_at == tpe(2026, 11, 20, 9)
    assert cells[1].ends_at == tpe(2026, 11, 20, 10)


def test_m2_pause_is_limited_to_round_and_returns_to_blueprint_after_midnight():
    a = weekly("a", time(22), time(2))  # 跨午夜
    mark = SlotMark("pause", "a", tpe(2026, 11, 12, 22), tpe(2026, 11, 12, 23))
    s = r.resolve([a], None, tpe(2026, 11, 12, 23, 30), TZ, marks=[mark])
    assert s.slot_state == "paused" and s.location == "discord"
    assert s.next_boundary_at == tpe(2026, 11, 13)  # 輪次結束 → 回藍圖
    after = r.resolve([a], None, tpe(2026, 11, 13, 0, 30), TZ, marks=[mark])
    assert after.slot_state == "normal" and after.source == PlanSource.ITEM and after.suggestions[0].mode == "WORK"
    cells = r.upcoming([a], None, tpe(2026, 11, 12, 22, 30), 4 * 3600, TZ, marks=[mark])
    assert [c.slot_state for c in cells][:3] == ["normal", "paused", "normal"]


def test_m5_warnings_are_deduplicated(caplog):
    a = weekly("a", time(20), time(21))
    broken = PlanItem("b", ActivityKind.WORK, "discord", "weekly")
    ghost = PlanDelay("zzz", tpe(2026, 11, 12, 20), tpe(2026, 11, 12, 22))
    with caplog.at_level(logging.WARNING, logger="uep.schedule.resolver"):
        for _ in range(5):
            r.resolve([a, broken], None, tpe(2026, 11, 12, 20, 30), TZ, delay=ghost)
    msgs = [rec.message for rec in caplog.records]
    assert sum("item=b" in m for m in msgs) == 1
    assert sum("anchor" in m for m in msgs) == 1
    # 不同錯誤類別／不同項仍各記一次
    other = PlanItem("c", ActivityKind.WORK, "discord", "weekly")
    with caplog.at_level(logging.WARNING, logger="uep.schedule.resolver"):
        r.resolve([other], None, tpe(2026, 11, 12, 20, 30), TZ)
    assert sum("item=c" in rec.message for rec in caplog.records) == 1
