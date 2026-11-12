"""PP-2：行程提供的休息時段（PlanBackedRestWindowPolicy）與旗標組裝。純單元測試，不碰 DB。"""
import logging
from datetime import datetime, time
from zoneinfo import ZoneInfo

import pytest

from uep.config import UepSettings
from uep.contracts.schedule import ActivityKind, PlanItem
from uep.echo.schedule_wiring import (
    PlanBackedRestWindowPolicy,
    build_rest_window_policy,
)
from uep.memory.dream_planner import FixedWindowPolicy, WindowOutcome
from uep.schedule import resolver

TZ = "Asia/Taipei"
TPE = ZoneInfo(TZ)


def tpe(y, mo, d, h=0, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=TPE)


def window_item(start, end, tz=TZ):
    return PlanItem("rw", ActivityKind.REST, None, "weekly", role="rest_window", weekdays=(1, 2, 3, 4, 5, 6, 7),
                    local_start=start, local_end=end, tz=tz, priority=10)


class FakeReader:
    def __init__(self, items, tz=TZ):
        self.items, self.tz = items, tz

    def resolve(self, at=None):
        return resolver.resolve(self.items, None, at, self.tz)

    def upcoming(self, start, horizon_s):
        return resolver.upcoming(self.items, None, start, horizon_s, self.tz)

    def revision(self):
        return 0


def test_window_inside_outside_and_cross_midnight():  # VC-PP-6
    policy = PlanBackedRestWindowPolicy(FakeReader([window_item(time(3), time(5, 30))]))
    w = policy.window_at(tpe(2026, 11, 12, 4))
    assert (w.start, w.end, w.tz) == (tpe(2026, 11, 12, 3), tpe(2026, 11, 12, 5, 30), TZ)
    assert policy.window_at(tpe(2026, 11, 12, 6)) is None
    night = PlanBackedRestWindowPolicy(FakeReader([window_item(time(23), time(1))]))
    w = night.window_at(tpe(2026, 11, 13, 0, 30))
    assert (w.start, w.end) == (tpe(2026, 11, 12, 23), tpe(2026, 11, 13, 1))
    # 與現行 FixedWindowPolicy 在同一時間點給出同樣的窗口
    fixed = FixedWindowPolicy(start=time(23), end=time(1), tz=TZ)
    assert fixed.window_at(tpe(2026, 11, 13, 0, 30)) == w


def test_window_matches_fixed_policy_over_a_day():
    policy = PlanBackedRestWindowPolicy(FakeReader([window_item(time(3), time(5, 30))]))
    fixed = FixedWindowPolicy()
    for hour in range(24):
        for minute in (0, 15, 45):
            at = tpe(2026, 11, 12, hour, minute)
            a, b = policy.window_at(at), fixed.window_at(at)
            assert (a is None) == (b is None)
            if a is not None:
                assert (a.start, a.end) == (b.start, b.end)


def test_explicit_item_truncates_window():  # VC-PP-6
    work = PlanItem("w", ActivityKind.WORK, "discord", "weekly", weekdays=(1, 2, 3, 4, 5, 6, 7),
                    local_start=time(4), local_end=time(4, 30), tz=TZ, priority=50)
    policy = PlanBackedRestWindowPolicy(FakeReader([window_item(time(3), time(5, 30)), work]))
    assert policy.window_at(tpe(2026, 11, 12, 3, 30)).end == tpe(2026, 11, 12, 4)
    assert policy.window_at(tpe(2026, 11, 12, 4, 10)) is None  # 明確行程期間不視為休息時段
    assert policy.window_at(tpe(2026, 11, 12, 5)).start == tpe(2026, 11, 12, 4, 30)


def test_on_window_end_never_extends():
    policy = PlanBackedRestWindowPolicy(FakeReader([window_item(time(3), time(5, 30))]))
    window = policy.window_at(tpe(2026, 11, 12, 4))
    decision = policy.on_window_end(window, WindowOutcome(cycles_run=3))
    assert (decision.extend_until, decision.wants_intake) == (None, False)


def test_naive_now_rejected():
    with pytest.raises(ValueError):
        PlanBackedRestWindowPolicy(FakeReader([])).window_at(datetime(2026, 11, 12, 4))


def test_unavailable_plan_falls_back_to_fixed_and_logs_once(caplog):  # VC-PP-7
    reader = FakeReader([window_item(time(9), time(10))], tz="Not/AZone")  # 時區不可用 → unavailable
    fixed = FixedWindowPolicy()
    policy = PlanBackedRestWindowPolicy(reader, fallback=fixed)
    with caplog.at_level(logging.WARNING, logger="uep.echo.schedule_wiring"):
        a = policy.window_at(tpe(2026, 11, 12, 4))
        policy.window_at(tpe(2026, 11, 12, 4, 5))
    assert a == fixed.window_at(tpe(2026, 11, 12, 4))  # 保住夜間做夢
    assert policy.window_at(tpe(2026, 11, 12, 12)) is None
    assert sum("行程不可用" in r.message for r in caplog.records) == 1  # 進入降級只記一次


def test_reader_exception_falls_back_and_recovers(caplog):
    class Flaky(FakeReader):
        broken = True

        def resolve(self, at=None):
            if self.broken:
                raise RuntimeError("db down")
            return super().resolve(at)

    reader = Flaky([window_item(time(1), time(2))])
    policy = PlanBackedRestWindowPolicy(reader)  # fallback＝預設 03:00–05:30
    with caplog.at_level(logging.INFO, logger="uep.echo.schedule_wiring"):
        assert policy.window_at(tpe(2026, 11, 12, 4)) is not None  # fixed 的窗口
        reader.broken = False
        assert policy.window_at(tpe(2026, 11, 12, 4)) is None  # 行程恢復：行程窗口是 01–02
        assert policy.window_at(tpe(2026, 11, 12, 1, 30)) is not None
    assert any("恢復可用" in r.message for r in caplog.records)


# ---- 旗標組裝 ----

class FakeStore(FakeReader):
    def __init__(self, items=(), fail=False):
        super().__init__(list(items))
        self.seeded, self.fail = [], fail

    def seed_rest_window(self, start, end, tz, *, overwrite=False):
        if self.fail:
            raise RuntimeError("seed failed")
        self.seeded.append((start, end, tz, overwrite))
        return "created"


class Settings:
    def __init__(self, mode="fixed", **kw):
        self.schedule_rest_window = mode
        self.dream_window_start = self.dream_window_end = self.dream_window_tz = None
        self.__dict__.update(kw)


def test_default_flag_is_fixed():
    assert UepSettings.model_fields["schedule_rest_window"].default == "fixed"


def test_fixed_mode_returns_policy_unchanged():
    fixed = FixedWindowPolicy(start=time(9), end=time(18))
    assert build_rest_window_policy(Settings("fixed"), fixed, None) is fixed
    assert build_rest_window_policy(Settings("fixed"), None, None) is None
    assert build_rest_window_policy(Settings(""), fixed, None) is fixed  # 空字串視同預設


def test_invalid_flag_fails_fast():
    with pytest.raises(ValueError):
        build_rest_window_policy(Settings("planz"), None, None)


def test_plan_mode_seeds_and_returns_plan_policy():
    store = FakeStore([window_item(time(3), time(5, 30))])
    policy = build_rest_window_policy(Settings("plan"), None, None, store=store)
    assert isinstance(policy, PlanBackedRestWindowPolicy)
    assert store.seeded == [(time(3), time(5, 30), TZ, False)]  # 預設值播種、不覆寫
    assert policy.window_at(tpe(2026, 11, 12, 4)) is not None


def test_plan_mode_explicit_dream_window_overwrites():
    store = FakeStore()
    fixed = FixedWindowPolicy(start=time(9), end=time(18), tz=TZ)
    settings = Settings("plan", dream_window_start="09:00")
    build_rest_window_policy(settings, fixed, None, store=store)
    assert store.seeded == [(time(9), time(18), TZ, True)]  # 明確設定 → 覆寫（供白天驗證）


def test_plan_mode_seed_failure_or_no_db_keeps_fixed(caplog):
    fixed = FixedWindowPolicy(start=time(9), end=time(18))
    assert build_rest_window_policy(Settings("plan"), fixed, None, store=FakeStore(fail=True)) is fixed
    assert build_rest_window_policy(Settings("plan"), fixed, None) is fixed  # 沒有 DB


# ---- PP-2 補修：遲滯、窗口自己的 tz、缺項降級 ----

class Controllable(FakeReader):
    def __init__(self, items, tz=TZ, exists=True):
        super().__init__(items, tz)
        self.broken, self.exists, self.checks = False, exists, 0

    def resolve(self, at=None):
        if self.broken:
            raise RuntimeError("db down")
        return super().resolve(at)

    def has_rest_window(self):
        self.checks += 1
        return self.exists


def test_cached_window_is_kept_until_it_ends_when_reader_fails():  # m1 遲滯
    reader = Controllable([window_item(time(1), time(2))])  # 行程窗口 01–02；fixed 預設 03:00–05:30
    policy = PlanBackedRestWindowPolicy(reader)
    inside = policy.window_at(tpe(2026, 11, 12, 1, 10))
    assert inside is not None
    reader.broken = True
    assert policy.window_at(tpe(2026, 11, 12, 1, 30)) == inside  # 故障中沿用到窗口結束
    assert policy.window_at(tpe(2026, 11, 12, 2, 0)) is None  # 快取窗口已結束 → 退回 fixed（此時不在 03:00 窗口）
    fixed_hit = policy.window_at(tpe(2026, 11, 12, 4))
    assert fixed_hit is not None and fixed_hit.start == tpe(2026, 11, 12, 3)  # 之後走 fixed
    reader.broken = False
    assert policy.window_at(tpe(2026, 11, 12, 1, 10)) is not None  # 恢復


def test_window_tz_comes_from_the_item_then_default():  # m2
    ny = "America/New_York"
    policy = PlanBackedRestWindowPolicy(FakeReader([window_item(time(3), time(5, 30), tz=ny)], tz=ny), tz="UTC")
    at = datetime(2026, 11, 12, 4, tzinfo=ZoneInfo(ny))
    assert policy.window_at(at).tz == ny
    once = PlanItem("rw", ActivityKind.REST, None, "once", role="rest_window", once_start_at=tpe(2026, 11, 12, 3),
                    once_end_at=tpe(2026, 11, 12, 5))
    # once 項沒有自己的 tz → 用固定字串（不呼叫任何綁定方法）
    assert PlanBackedRestWindowPolicy(FakeReader([once]), tz="Asia/Taipei").window_at(tpe(2026, 11, 12, 4)).tz == TZ


def test_missing_rest_window_item_degrades_to_fixed_with_one_warning(caplog):  # m3
    now = [0.0]
    reader = Controllable([], exists=False)
    policy = PlanBackedRestWindowPolicy(reader, recheck_s=60.0, monotonic=lambda: now[0])
    with caplog.at_level(logging.WARNING, logger="uep.echo.schedule_wiring"):
        assert policy.window_at(tpe(2026, 11, 12, 4)) is not None  # fixed 03:00–05:30
        assert policy.window_at(tpe(2026, 11, 12, 6)) is None
        policy.window_at(tpe(2026, 11, 12, 4))
    assert sum("沒有 rest_window 項" in r.message for r in caplog.records) == 1
    assert reader.checks == 1  # 60 秒內不重複查 DB
    now[0] = 61.0
    policy.window_at(tpe(2026, 11, 12, 4))
    assert reader.checks == 2
    reader.exists = True
    reader.items = [window_item(time(1), time(2))]
    now[0] = 130.0
    assert policy.window_at(tpe(2026, 11, 12, 4)) is None  # 項已出現：改由行程判定
    assert policy.window_at(tpe(2026, 11, 12, 1, 30)) is not None


def test_recovery_rechecks_item_existence():  # m3：每次恢復時檢查
    reader = Controllable([window_item(time(1), time(2))])
    policy = PlanBackedRestWindowPolicy(reader)
    policy.window_at(tpe(2026, 11, 12, 6))
    assert reader.checks == 1
    reader.broken = True
    policy.window_at(tpe(2026, 11, 12, 6))
    reader.broken = False
    policy.window_at(tpe(2026, 11, 12, 6))
    assert reader.checks == 2


def test_build_plan_policy_verifies_on_start(caplog):
    store = FakeStore()
    store.has_rest_window = lambda: False
    with caplog.at_level(logging.WARNING, logger="uep.echo.schedule_wiring"):
        policy = build_rest_window_policy(Settings("plan"), None, None, store=store)
    assert isinstance(policy, PlanBackedRestWindowPolicy)
    assert any("沒有 rest_window 項" in r.message for r in caplog.records)
