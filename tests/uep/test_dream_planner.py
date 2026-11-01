"""Host 全域做夢迴圈 DL-0：DreamPlanner 純函式與休息窗口（設計 §2、§3.1、§4.2、§6.1；VC-DL-3／5／6／7／12 的 Planner 層）。

全部以假時鐘（snapshot.now 單調秒數）驅動，沒有執行緒也沒有 IO；門檻邊界測 T-1／T／T+1。
"""
from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, time, timedelta

import pytest

from uep.memory.dream_planner import (
    TRIGGER_BACKLOG,
    TRIGGER_DAYDREAM,
    TRIGGER_IDLE,
    TRIGGER_REST,
    DreamLoopConfig,
    DreamPlanner,
    DreamSnapshot,
    FixedWindowPolicy,
    ForegroundSnapshot,
    NamespaceHistory,
    NamespaceInput,
    NamespaceReadiness,
    apply_outcome,
    error_backoff_s,
)

CFG = DreamLoopConfig(enabled=True, shadow=False)
NOW = 100000.0


def fg(**over) -> ForegroundSnapshot:
    base = {
        "interaction_state": "IDLE", "behavior_state": "CHAT", "open_episodes": 0, "quiet_for_s": 99999.0,
        "global_stop_engaged": False, "runtime_ready": True,
    }
    base.update(over)
    return ForegroundSnapshot(**base)


def ns(name, pending=0, unscanned=None, *, held=False, dreaming=False, ready=True, **hist) -> NamespaceInput:
    readiness = NamespaceReadiness(name, pending, unscanned, held, dreaming) if ready else None
    return NamespaceInput(NamespaceHistory(name, **hist), readiness)


def snap(*namespaces, **over) -> DreamSnapshot:
    base = {"now": NOW, "uptime_s": 10000.0, "foreground": fg(), "namespaces": tuple(namespaces)}
    base.update(over)
    return DreamSnapshot(**base)


def decide(cfg=CFG, *namespaces, **over):
    return DreamPlanner(cfg).decide(snap(*namespaces, **over))


# ---- 全域閘門（VC-DL-3、16 的 Planner 層）----

@pytest.mark.parametrize(("over", "reason"), [
    ({"uptime_s": None}, "not_started"),
    ({"paused": True}, "paused"),
    ({"in_flight": True}, "dream_in_flight"),
    ({"foreground": fg(interaction_state="THINKING")}, "foreground_active"),
    ({"foreground": fg(interaction_state="SPEAKING", quiet_for_s=99999.0)}, "foreground_active"),
    ({"foreground": fg(open_episodes=1, quiet_for_s=999999.0)}, "foreground_active"),  # 語音頻道有人但沒人講話
    ({"foreground": fg(global_stop_engaged=True)}, "global_stop"),
    ({"foreground": fg(runtime_ready=False)}, "runtime_not_ready"),  # STARTING／STOPPING
])
def test_global_gate_blocks_with_fixed_reason(over, reason):
    d = decide(CFG, ns("a", pending=50), **over)
    assert (d.action, d.reason, d.namespace_id) == ("wait", reason, None)


def test_startup_grace_boundary():
    assert decide(CFG, ns("a", pending=50), uptime_s=299.0).reason == "startup_grace"
    d = decide(CFG, ns("a", pending=50), uptime_s=299.0)
    assert d.not_before == pytest.approx(NOW + 1.0)
    assert decide(CFG, ns("a", pending=50), uptime_s=300.0).action == "run"
    assert decide(CFG, ns("a", pending=50), uptime_s=301.0).action == "run"


def test_global_gate_is_available_without_readiness():
    planner = DreamPlanner(CFG)
    assert planner.global_gate(snap(foreground=fg(interaction_state="LISTENING"))).reason == "foreground_active"
    assert planner.global_gate(snap()) is None


# ---- 觸發門檻（VC-DL-3 的門檻獨立、T-1／T／T+1）----

@pytest.mark.parametrize(("pending", "quiet", "expect"), [
    (20, 119.0, ("wait", "quiet_short")),
    (20, 120.0, ("run", TRIGGER_DAYDREAM)),
    (20, 121.0, ("run", TRIGGER_DAYDREAM)),
    (19, 1799.0, ("wait", "quiet_short")),   # 19 只夠 idle：要 1800
    (19, 1800.0, ("run", TRIGGER_IDLE)),
    (5, 1799.0, ("wait", "quiet_short")),
    (5, 1800.0, ("run", TRIGGER_IDLE)),
    (5, 1801.0, ("run", TRIGGER_IDLE)),
    (4, 99999.0, ("wait", "not_enough_pending")),
    (0, 99999.0, ("wait", "not_enough_pending")),
    (20, 1800.0, ("run", TRIGGER_DAYDREAM)),  # 兩者皆成立時 daydream 優先
])
def test_daydream_and_idle_thresholds_are_independent(pending, quiet, expect):
    d = decide(CFG, ns("a", pending=pending), foreground=fg(quiet_for_s=quiet))
    if expect[0] == "run":
        assert (d.action, d.trigger, d.namespace_id) == ("run", expect[1], "a")
    else:
        assert (d.action, d.reason) == expect


def test_quiet_short_reports_not_before():
    d = decide(CFG, ns("a", pending=5), foreground=fg(quiet_for_s=1700.0))
    assert d.reason == "quiet_short" and d.not_before == pytest.approx(NOW + 100.0)


def test_sleep_behavior_relaxes_idle_quiet_to_rest_quiet():
    assert decide(CFG, ns("a", pending=5), foreground=fg(quiet_for_s=300.0)).action == "wait"
    d = decide(CFG, ns("a", pending=5), foreground=fg(quiet_for_s=300.0, behavior_state="SLEEP"))
    assert (d.action, d.trigger) == ("run", TRIGGER_IDLE)


def test_lease_held_by_other_and_dreaming_namespace_are_skipped():
    assert decide(CFG, ns("a", pending=50, held=True)).reason == "lease_held_by_other"
    assert decide(CFG, ns("a", pending=50, dreaming=True)).reason == "namespace_dreaming"
    d = decide(CFG, ns("a", pending=50, held=True), ns("b", pending=50))
    assert d.namespace_id == "b"


def test_readiness_unavailable_is_skipped_not_fatal():
    d = decide(CFG, ns("a", ready=False), ns("b", pending=7))
    assert (d.action, d.namespace_id) == ("run", "b")
    assert decide(CFG, ns("a", ready=False)).reason == "readiness_unavailable"


def test_no_targets():
    assert decide(CFG).reason == "no_targets"


# ---- min_gap／退避／每日上限 ----

def test_min_gap_boundary_per_namespace():
    start = NOW - CFG.min_gap_s
    assert decide(CFG, ns("a", pending=50, last_started_at=start + 1)).reason == "min_gap"
    d = decide(CFG, ns("a", pending=50, last_started_at=start + 1))
    assert d.not_before == pytest.approx(NOW + 1)
    assert decide(CFG, ns("a", pending=50, last_started_at=start)).action == "run"
    assert decide(CFG, ns("a", pending=50, last_started_at=start - 1)).action == "run"


def test_backoff_is_per_namespace_and_other_namespace_proceeds():
    d = decide(CFG, ns("a", pending=50, backoff_until=NOW + 5), ns("b", pending=6))
    assert (d.action, d.namespace_id) == ("run", "b")
    only_a = decide(CFG, ns("a", pending=50, backoff_until=NOW + 5))
    assert (only_a.reason, only_a.not_before) == ("backoff", NOW + 5)
    assert decide(CFG, ns("a", pending=50, backoff_until=NOW)).action == "run"


def test_daily_cap_blocks_outside_rest_only():
    assert decide(CFG, ns("a", pending=50), daily_cycles=7).action == "run"
    assert decide(CFG, ns("a", pending=50), daily_cycles=8).reason == "daily_cap"
    # 休息時段內的 host_rest 不受每日上限約束（daily_cycles 已不含休息時段）
    d = decide(CFG, ns("a", pending=1), daily_cycles=99, rest_remaining_s=7200.0)
    assert (d.action, d.trigger) == ("run", TRIGGER_REST)


# ---- host_backlog（VC-DL-5 Planner 層；D86）----

BACKLOG = replace(CFG, backlog_enabled=True)


def test_backlog_triggers_on_unscanned_when_pending_below_min():
    d = decide(BACKLOG, ns("a", pending=0, unscanned=91), foreground=fg(quiet_for_s=1800.0))
    assert (d.action, d.trigger, d.namespace_id) == ("run", TRIGGER_BACKLOG, "a")
    assert decide(BACKLOG, ns("a", pending=0, unscanned=91), foreground=fg(quiet_for_s=1799.0)).reason == "quiet_short"


def test_backlog_needs_a_full_batch():
    assert decide(BACKLOG, ns("a", pending=0, unscanned=8)).trigger == TRIGGER_BACKLOG
    assert decide(BACKLOG, ns("a", pending=0, unscanned=7)).reason == "not_enough_pending"


def test_backlog_unavailable_without_md1_field_or_when_disabled():
    # 記憶端尚無 unscanned_pending（M-D1）：觸發停用且不報錯
    assert decide(BACKLOG, ns("a", pending=0, unscanned=None)).reason == "not_enough_pending"
    # DL-1 預設 backlog_enabled=False：即使有數字也不觸發
    assert decide(CFG, ns("a", pending=0, unscanned=91)).reason == "not_enough_pending"


def test_backlog_defers_to_idle_when_pending_reaches_min():
    d = decide(BACKLOG, ns("a", pending=5, unscanned=91))
    assert d.trigger == TRIGGER_IDLE  # 不同時觸發兩次：host_idle 已會做夢（replay 順便輪替積壓）


def test_backlog_min_gap_and_daily_limit():
    gap = BACKLOG.backlog_min_gap_s
    kw = {"pending": 0, "unscanned": 91}
    assert decide(BACKLOG, ns("a", **kw, last_backlog_started_at=NOW - gap + 1)).reason == "backlog_throttled"
    assert decide(BACKLOG, ns("a", **kw, last_backlog_started_at=NOW - gap)).trigger == TRIGGER_BACKLOG
    assert decide(BACKLOG, ns("a", **kw, backlog_starts_24h=3)).trigger == TRIGGER_BACKLOG
    assert decide(BACKLOG, ns("a", **kw, backlog_starts_24h=4)).reason == "backlog_throttled"


# ---- 排序與公平（VC-DL-6）----

def test_ordering_class_then_oldest_then_pending_then_name():
    # 類別：daydream 先於 idle
    d = decide(CFG, ns("a", pending=6), ns("b", pending=25))
    assert d.namespace_id == "b"
    # 同類別：last_finished_at 最舊先（從未做夢者最優先）
    d = decide(CFG, ns("a", pending=6, last_finished_at=10.0), ns("b", pending=6, last_finished_at=5.0),
               ns("c", pending=6, last_finished_at=7.0))
    assert d.namespace_id == "b"
    d = decide(CFG, ns("a", pending=6, last_finished_at=10.0), ns("z", pending=6))
    assert d.namespace_id == "z"
    # 同樣舊：pending 多者先；再同：名稱字典序
    assert decide(CFG, ns("a", pending=6), ns("b", pending=9)).namespace_id == "b"
    assert decide(CFG, ns("b", pending=6), ns("a", pending=6)).namespace_id == "a"


def test_rotation_covers_all_namespaces_and_respects_min_gap():
    planner = DreamPlanner(CFG)
    hist = {n: NamespaceHistory(n) for n in "abc"}
    now, order = NOW, []

    def inputs():
        return tuple(NamespaceInput(hist[n], NamespaceReadiness(n, 8)) for n in "abc")

    for _ in range(3):
        d = planner.decide(snap(*inputs(), now=now))
        assert d.action == "run"
        order.append(d.namespace_id)
        hist[d.namespace_id] = apply_outcome(
            hist[d.namespace_id], now=now + 60, started_at=now, status="completed", skipped_reason=None, config=CFG,
        ).history
        now += 61  # 遠小於 min_gap：剛做過的不得再被選中
    assert order == ["a", "b", "c"]
    assert planner.decide(snap(*inputs(), now=now)).reason == "min_gap"  # 三個都還在 min_gap 內
    # 跨過 min_gap 後從最舊者（a）重新輪起
    later = snap(*inputs(), now=NOW + CFG.min_gap_s + 1)
    assert planner.decide(later).namespace_id == "a"


def test_same_namespace_not_selected_again_within_min_gap():
    planner = DreamPlanner(CFG)
    h = NamespaceHistory("a", last_started_at=NOW - 10, last_finished_at=NOW - 5)
    d = planner.decide(snap(NamespaceInput(h, NamespaceReadiness("a", 30))))
    assert d.reason == "min_gap"


def test_daydream_class_namespace_cannot_starve_idle_class_namespace():
    planner = DreamPlanner(CFG)
    last, streak, now, picks = None, 0, NOW, []
    hist = {"a": NamespaceHistory("a"), "b": NamespaceHistory("b")}
    for _ in range(8):
        inputs = (
            NamespaceInput(hist["a"], NamespaceReadiness("a", 30)),  # 永遠 daydream 級
            NamespaceInput(hist["b"], NamespaceReadiness("b", 6)),   # 只有 idle 級
        )
        d = planner.decide(snap(*inputs, now=now, last_trigger=last, last_trigger_streak=streak))
        if d.action == "wait":
            now += 60
            continue
        picks.append((d.namespace_id, d.trigger))
        streak = streak + 1 if last == d.trigger else 1
        last = d.trigger
        hist[d.namespace_id] = apply_outcome(hist[d.namespace_id], now=now + 1, started_at=now, status="completed",
                                             skipped_reason=None, config=CFG).history
        now += CFG.min_gap_s  # 每次都跨過 min_gap，兩個 namespace 都再度 eligible
    assert ("b", TRIGGER_IDLE) in picks[:4]


def test_max_consecutive_same_class_forces_other_class():
    inputs = (ns("a", pending=30), ns("b", pending=6))
    assert decide(CFG, *inputs, last_trigger=TRIGGER_DAYDREAM, last_trigger_streak=2).namespace_id == "a"
    d = decide(CFG, *inputs, last_trigger=TRIGGER_DAYDREAM, last_trigger_streak=3)
    assert (d.namespace_id, d.trigger) == ("b", TRIGGER_IDLE)
    # 沒有其他類別 eligible 時不強制
    assert decide(CFG, ns("a", pending=30), last_trigger=TRIGGER_DAYDREAM, last_trigger_streak=9).namespace_id == "a"


# ---- 休息時段（VC-DL-12 Planner 層）----

def test_rest_trigger_thresholds_are_relaxed():
    kw = {"rest_remaining_s": 7200.0}
    d = decide(CFG, ns("a", pending=1), foreground=fg(quiet_for_s=300.0), **kw)
    assert (d.action, d.trigger) == ("run", TRIGGER_REST)
    assert decide(CFG, ns("a", pending=1), foreground=fg(quiet_for_s=299.0), **kw).reason == "quiet_short"
    assert decide(CFG, ns("a", pending=0), **kw).reason == "not_enough_pending"
    # unscanned 也算有料（需 M-D1）；欄位缺失時不算
    assert decide(CFG, ns("a", pending=0, unscanned=1), **kw).trigger == TRIGGER_REST
    assert decide(CFG, ns("a", pending=0, unscanned=None), **kw).action == "wait"
    # 前景仍是閘門：休息時段不放寬
    assert decide(CFG, ns("a", pending=9), foreground=fg(interaction_state="THINKING"), **kw).reason == "foreground_active"


def test_rest_orders_by_oldest_then_pending_then_name_ignoring_class():
    kw = {"rest_remaining_s": 7200.0}
    d = decide(CFG, ns("a", pending=30, last_finished_at=50.0), ns("b", pending=1, last_finished_at=40.0), **kw)
    assert d.namespace_id == "b"
    d = decide(CFG, ns("a", pending=2), ns("b", pending=9), **kw)
    assert d.namespace_id == "b"


def test_rest_cycle_cap_and_window_remaining():
    kw = {"foreground": fg()}
    assert decide(CFG, ns("a", pending=3), rest_remaining_s=7200.0, rest_cycles=7, **kw).action == "run"
    assert decide(CFG, ns("a", pending=3), rest_remaining_s=7200.0, rest_cycles=8, **kw).reason == "rest_cycle_cap"
    # 窗口剩餘 < cycle_deadline_s（900）不開新週期
    assert decide(CFG, ns("a", pending=3), rest_remaining_s=899.0).reason == "rest_window_ending"
    assert decide(CFG, ns("a", pending=3), rest_remaining_s=900.0).action == "run"
    assert decide(CFG, ns("a", pending=3), rest_remaining_s=901.0).action == "run"


# ---- 結果分類與退避表（VC-DL-7、8 的純函式層）----

def outcome(hist, status, reason=None, *, cfg=CFG, now=1000.0, started=900.0, backlog=False):
    return apply_outcome(hist, now=now, started_at=started, status=status, skipped_reason=reason, config=cfg,
                         is_backlog=backlog)


H0 = NamespaceHistory("a")


def test_completed_clears_failures_and_recovers():
    h = replace(H0, consecutive_failures=2, consecutive_lease_skips=2, degraded=True, backoff_until=5000.0)
    v = outcome(h, "completed")
    assert (v.kind, v.counts_as_run, v.recover, v.degrade, v.backoff_s) == ("completed", True, True, False, 0.0)
    assert v.history.consecutive_failures == 0 and v.history.consecutive_lease_skips == 0
    assert v.history.backoff_until == 0.0 and not v.history.degraded
    assert (v.history.last_started_at, v.history.last_finished_at) == (900.0, 1000.0)
    assert outcome(H0, "completed").recover is False


def test_error_backoff_is_exponential_with_cap_and_degrades_at_three():
    assert [error_backoff_s(n, CFG) for n in (0, 1, 2, 3, 4, 5, 6, 40)] == [
        0.0, 600.0, 1200.0, 2400.0, 4800.0, 9600.0, 14400.0, 14400.0]
    h, seen = H0, []
    for _ in range(4):
        v = outcome(h, "error")
        h = v.history
        seen.append((h.consecutive_failures, v.backoff_s, v.degrade))
        assert v.counts_as_run and h.backoff_until == 1000.0 + v.backoff_s
    assert seen == [(1, 600.0, False), (2, 1200.0, False), (3, 2400.0, True), (4, 4800.0, False)]  # degrade 只升一次


def test_already_running_is_normal_skip_without_failure():
    v = outcome(H0, "skipped", "already_running")
    assert (v.kind, v.backoff_s, v.degrade, v.counts_as_run) == ("skipped", 300.0, False, False)
    assert v.history.consecutive_failures == 0 and v.history.last_started_at is None  # skipped 不吃 min_gap
    assert v.history.backoff_until == 1000.0 + 300.0


def test_lease_held_degrades_on_third_consecutive_only_once_and_recovers():
    h, flags = H0, []
    for _ in range(4):
        v = outcome(h, "skipped", "lease_held")
        h = v.history
        flags.append((v.degrade, v.backoff_s))
    assert flags == [(False, 1800.0), (False, 1800.0), (True, 1800.0), (False, 1800.0)]
    assert h.degraded
    assert outcome(h, "completed").recover is True


def test_lease_timeout_shares_the_consecutive_counter():
    h = outcome(outcome(H0, "skipped", "lease_held").history, "skipped", "lease_timeout").history
    v = outcome(h, "skipped", "lease_timeout")
    assert v.degrade and v.backoff_s == 1800.0


def test_lease_io_error_degrades_on_first_occurrence():
    v = outcome(H0, "skipped", "lease_io_error")
    assert (v.degrade, v.backoff_s) == (True, 3600.0)
    assert outcome(v.history, "skipped", "lease_io_error").degrade is False  # 冪等


def test_expected_lease_peer_lease_held_does_not_degrade():
    cfg = replace(CFG, expected_lease_peers=frozenset({"a"}))
    h = H0
    for _ in range(5):
        v = outcome(h, "skipped", "lease_held", cfg=cfg)
        h = v.history
        assert not v.degrade and v.backoff_s == 1800.0
    assert not h.degraded
    # 但 lease_io_error 即使是預期對象也照升
    assert outcome(H0, "skipped", "lease_io_error", cfg=cfg).degrade


def test_window_completed_and_unknown_codes_use_skip_backoff():
    for code in ("window_completed", "weird_new_code", None):
        v = outcome(H0, "skipped", code)
        assert (v.backoff_s, v.degrade) == (300.0, False)


def test_backlog_start_recorded_only_when_flagged():
    assert outcome(H0, "completed", backlog=True).history.last_backlog_started_at == 900.0
    assert outcome(H0, "completed", backlog=False).history.last_backlog_started_at is None


# ---- FixedWindowPolicy（D97）----

TPE = "Asia/Taipei"


def at_taipei(hh, mm, ss=0, day=1):
    return datetime(2026, 11, day, hh, mm, ss, tzinfo=UTC) - timedelta(hours=8)  # 台北本地時間 hh:mm:ss


def test_fixed_window_boundaries():
    p = FixedWindowPolicy()
    assert p.window_at(at_taipei(2, 59, 59)) is None
    w = p.window_at(at_taipei(3, 0))
    assert w is not None and (w.end - w.start) == timedelta(hours=2, minutes=30) and w.tz == TPE
    assert p.window_at(at_taipei(5, 29, 59)) is not None
    assert p.window_at(at_taipei(5, 30)) is None
    assert p.window_at(at_taipei(12, 0)) is None


def test_fixed_window_uses_explicit_timezone_not_host_local():
    p = FixedWindowPolicy()
    assert p.window_at(datetime(2026, 11, 1, 19, 0, tzinfo=UTC)) is not None  # UTC 19:00 = 台北 03:00
    assert p.window_at(datetime(2026, 11, 1, 3, 0, tzinfo=UTC)) is None


def test_fixed_window_crossing_midnight():
    p = FixedWindowPolicy(start=time(22, 0), end=time(2, 0))
    assert p.window_at(at_taipei(23, 0)) is not None
    w = p.window_at(at_taipei(1, 0, day=2))
    assert w is not None and w.start.day == 1 and w.end.day == 2
    assert p.window_at(at_taipei(2, 0, day=2)) is None and p.window_at(at_taipei(21, 59)) is None


def test_fixed_window_rejects_naive_datetime_and_empty_window():
    with pytest.raises(ValueError, match="tz-aware"):
        FixedWindowPolicy().window_at(datetime(2026, 11, 1, 3, 0))
    with pytest.raises(ValueError, match="不得相同"):
        FixedWindowPolicy(start=time(3, 0), end=time(3, 0))


def test_default_on_window_end_never_extends():
    p = FixedWindowPolicy()
    w = p.window_at(at_taipei(4, 0))
    from uep.memory.dream_planner import WindowOutcome

    decision = p.on_window_end(w, WindowOutcome())
    assert decision.extend_until is None and decision.wants_intake is False


# ---- 設定驗證 ----

def test_config_defaults_match_decisions_d93_to_d98():
    c = DreamLoopConfig()
    assert (c.idle_quiet_s, c.daydream_quiet_s, c.rest_quiet_s, c.startup_grace_s) == (1800, 120, 300, 300)
    assert (c.min_pending, c.daydream_pending, c.min_gap_s) == (5, 20, 1800)
    assert (c.backlog_batch, c.backlog_min_gap_s, c.backlog_max_per_day) == (8, 10800, 4)
    assert (c.cycle_deadline_s, c.max_llm_calls_per_cycle, c.daily_cycle_cap, c.rest_max_cycles) == (900, 40, 8, 8)
    assert c.daily_llm_call_cap == 300
    assert (c.lease_degraded_after, c.lease_backoff_s, c.io_error_backoff_s, c.skip_backoff_s) == (3, 1800, 3600, 300)
    assert c.expected_lease_peers == frozenset() and c.enabled is False and c.backlog_enabled is False
    assert c.error_degraded_after == 3 and c.error_backoff_base_s == 600 and c.error_backoff_max_s == 14400


@pytest.mark.parametrize("bad", [{"tick_interval_s": 0}, {"min_pending": 0}, {"startup_grace_s": -1},
                                 {"daydream_pending": 3}])
def test_config_rejects_invalid_values(bad):
    with pytest.raises(ValueError, match="DreamLoopConfig|daydream_pending"):
        DreamLoopConfig(**bad)
