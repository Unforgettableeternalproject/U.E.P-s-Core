"""Host 全域做夢迴圈 DL-1：DreamLoop、ForegroundProbe、MemoryHost 整合（設計 §8：VC-DL-1／2／3／4／7／8／9／10／11／15／16）。

- 迴圈本體以假時鐘＋假 Probe＋fake engine 同步驅動 ``tick()``（不開執行緒）；
- MemoryHost 整合（啟動序、停機序、不擋前景、單一寫入）用真執行緒與極小門檻；
- Activity 案例需 PostgreSQL（未設 UEP_TEST_DATABASE_URL 時 skip）。
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select

from tests.uep.state_support import make_stack
from uep.events.bus import EventBus
from uep.fakes.memory import FakeAdapterFactory, FakeEngineFactory
from uep.memory import IdentityDirectory, MemoryHost, MemoryHostError, NamespaceSpec
from uep.memory.activity import COMPONENT, DreamActivityRecorder
from uep.memory.dream_loop import DreamLoop, DreamTarget, UnavailableProbe
from uep.memory.dream_planner import (
    DreamLoopConfig,
    FixedWindowPolicy,
    ForegroundSnapshot,
)
from uep.memory.foreground import StateBusForegroundProbe
from uep.memory.metrics import MemoryMetrics
from uep.tasks.models import ActivityRow

OWNER_NS = "core_owner"
OTHER_NS = "core_two"
SCOPE_NS = "scope-g123"
BASE = DreamLoopConfig(enabled=True, shadow=False, startup_grace_s=0.0)
IDLE_FG = ForegroundSnapshot("IDLE", "CHAT", 0, 1e9, False, True)
TPE_NOON = datetime(2026, 11, 1, 4, 0, tzinfo=UTC)  # 台北 12:00：不在休息窗口
TPE_REST = datetime(2026, 11, 1, 20, 0, tzinfo=UTC)  # 台北 04:00：在休息窗口內（剩 1.5 小時）


class Clock:
    def __init__(self, start: float = 10000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class Probe:
    """可設定的前景快照；``script`` 非空時依序消耗（用來模擬 tick 與呼叫之間前景變活躍）。"""

    def __init__(self, snap: ForegroundSnapshot = IDLE_FG) -> None:
        self.snap = snap
        self.script: list[ForegroundSnapshot] = []
        self.calls = 0
        self.raises: Exception | None = None

    def snapshot(self, now: float) -> ForegroundSnapshot:
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        return self.script.pop(0) if self.script else self.snap

    def set(self, **over) -> None:
        self.snap = replace(self.snap, **over)


class Rig:
    """同步驅動的迴圈：fake engine 的 listener 轉給 ``reports``（模擬 Host 的 report_sink）。"""

    def __init__(self, tmp_path: Path, names=(OWNER_NS,), pending=30, cfg: DreamLoopConfig = BASE, **loop_kw) -> None:
        self.factory = FakeEngineFactory()
        self.clock = Clock()
        self.wall = TPE_NOON
        self.probe = Probe()
        self.metrics = MemoryMetrics()
        self.reports: list[tuple[str, dict]] = []
        self.health: list[str] = []
        self.targets = []
        for name in names:
            engine = self.factory(name, tmp_path)
            engine.readiness["pending"] = pending
            engine.set_dream_listener(lambda r, n=name: self.reports.append((n, r)))
            self.targets.append(DreamTarget(name, engine, threading.Lock()))
        self.loop = DreamLoop(
            cfg, targets=lambda: self.targets, probe=self.probe, metrics=self.metrics,
            report_sink=lambda n, r: self.reports.append((n, dict(r))), health_sink=self.health.append,
            clock=self.clock, wall_clock=lambda: self.wall, **loop_kw,
        )
        self.loop.mark_started()
        self.clock.advance(1.0)

    def engine(self, name: str = OWNER_NS):
        return self.factory.engines[name]

    def wait_reason(self) -> str:
        return self.loop.tick().reason

    def tick_after(self, seconds: float):
        self.clock.advance(seconds)
        return self.loop.tick()


# ---- VC-DL-3：前景互斥（不開始）與呼叫前最後一次檢查 ----

def test_foreground_activity_blocks_start_without_calling_dream(tmp_path):
    rig = Rig(tmp_path)
    for over, reason in [
        ({"interaction_state": "THINKING"}, "foreground_active"),
        ({"open_episodes": 1}, "foreground_active"),
        ({"global_stop_engaged": True}, "global_stop"),
        ({"runtime_ready": False}, "runtime_not_ready"),
    ]:
        rig.probe.snap = replace(IDLE_FG, **over)
        assert rig.wait_reason() == reason
    assert rig.engine().dream_count == 0
    assert rig.engine().call_log == [(OWNER_NS, "set_dream_listener", rig.engine().dream_listener)]  # 沒查 readiness


def test_quiet_threshold_boundaries_through_the_loop(tmp_path):
    rig = Rig(tmp_path, pending=20)
    rig.probe.set(quiet_for_s=119.0)
    assert rig.wait_reason() == "quiet_short"
    rig.probe.set(quiet_for_s=120.0)
    d = rig.tick_after(61)  # 超過 readiness 快取
    assert (d.action, d.trigger) == ("run", "host_daydream")
    assert rig.engine().dream_count == 1


def test_foreground_turning_active_between_tick_and_call_is_caught_by_final_check(tmp_path):
    rig = Rig(tmp_path)
    busy = replace(IDLE_FG, interaction_state="LISTENING")
    # 第 1 次（tick 取 snapshot）閒置、第 2 次（呼叫前最後一次檢查）已活躍
    rig.probe.script = [IDLE_FG, busy]
    d = rig.loop.tick()
    assert (d.action, d.reason) == ("wait", "foreground_active")
    assert rig.engine().dream_count == 0
    assert rig.metrics.get("memory.dream.decision.wait.foreground_active") == 1


# ---- VC-DL-16：暫停與寬限 ----

def test_startup_grace_pause_and_global_stop_then_resume(tmp_path):
    rig = Rig(tmp_path, cfg=replace(BASE, startup_grace_s=300.0))
    assert rig.wait_reason() == "startup_grace"
    rig.clock.advance(300)
    rig.loop.pause("test")
    assert rig.wait_reason() == "paused"
    status = rig.loop.status()  # 暫停期間 status() 仍可讀
    assert status["paused"] is True and status["pause_reason"] == "test"
    assert rig.engine().dream_count == 0
    rig.loop.resume("test")
    rig.probe.set(global_stop_engaged=True)
    assert rig.wait_reason() == "global_stop"
    rig.probe.set(global_stop_engaged=False)
    assert rig.loop.tick().action == "run"
    assert rig.engine().dream_count == 1


# ---- VC-DL-15：shadow ----

def test_shadow_records_would_run_without_calling_dream_then_switch_to_real(tmp_path):
    rig = Rig(tmp_path, cfg=replace(BASE, shadow=True), pending=6)
    rig.probe.set(quiet_for_s=1800.0)
    d = rig.loop.tick()
    assert (d.action, d.trigger) == ("run", "host_idle")
    assert rig.engine().dream_count == 0
    assert rig.metrics.get("memory.dream.shadow_would_run.host_idle") == 1
    # 同一 namespace 依 min_gap 節流，不會每個 tick 重複記
    assert rig.tick_after(61).reason == "min_gap"
    assert rig.metrics.get("memory.dream.shadow_would_run.host_idle") == 1
    rig.loop.set_shadow(False)
    d = rig.tick_after(rig.loop.config.min_gap_s)
    assert (d.action, d.trigger) == ("run", "host_idle")
    assert rig.engine().dream_count == 1
    assert rig.loop.status()["shadow"] is False


# ---- VC-DL-8：失敗隔離與退避 ----

def test_error_backoff_isolated_per_namespace_and_other_namespace_still_runs(tmp_path):
    rig = Rig(tmp_path, names=(OWNER_NS, OTHER_NS), cfg=replace(BASE, min_gap_s=1.0))
    rig.engine(OWNER_NS).dream_result = {"error": "boom"}
    d1 = rig.loop.tick()
    assert (d1.namespace_id, rig.engine(OWNER_NS).dream_count) == (OWNER_NS, 1)
    d2 = rig.tick_after(2)
    assert d2.namespace_id == OTHER_NS and rig.engine(OTHER_NS).dream_count == 1  # A 退避中，B 照常
    # A 在退避期間（600 秒）不會被選中
    for _ in range(3):
        rig.tick_after(70)
    assert rig.engine(OWNER_NS).dream_count == 1
    rig.tick_after(600)
    assert rig.engine(OWNER_NS).dream_count == 2
    hist = rig.loop.status()["namespaces"][OWNER_NS]
    assert hist["consecutive_failures"] == 2


def test_repeated_errors_degrade_once_then_success_recovers(tmp_path):
    rig = Rig(tmp_path, cfg=replace(BASE, min_gap_s=1.0, error_backoff_base_s=1.0, error_backoff_max_s=2.0))
    engine = rig.engine()
    engine.dream_result = {"error": "boom"}
    for _ in range(4):
        rig.loop.tick()
        rig.clock.advance(100)
    assert engine.dream_count == 4
    assert rig.health == ["degraded"]  # 第 3 次升級，之後冪等不重發
    engine.dream_result = {"error": None}
    rig.loop.tick()
    assert rig.health == ["degraded", "ready"]
    assert rig.metrics.get("memory.dream.health.degraded") == 1 and rig.metrics.get("memory.dream.health.recovered") == 1


def test_dream_raising_runtime_error_does_not_kill_the_loop(tmp_path):
    rig = Rig(tmp_path, cfg=replace(BASE, min_gap_s=1.0))

    def boom(_):
        raise RuntimeError("engine 已關閉")

    rig.engine().dream_hook = boom
    d = rig.loop.tick()
    assert d.action == "run"
    assert rig.reports[-1][1]["status"] == "error"  # listener 沒被呼叫的例外路徑由迴圈補上單一寫入
    assert rig.reports[-1][1]["error"] == "RuntimeError"
    assert rig.loop.status()["namespaces"][OWNER_NS]["consecutive_failures"] == 1
    rig.engine().dream_hook = None
    assert rig.tick_after(700).action == "run"  # 退避 600 秒後再度評估


def test_tick_exceptions_pause_the_loop_and_degrade_after_limit(tmp_path):
    rig = Rig(tmp_path)
    rig.probe.raises = RuntimeError("probe 壞了")
    for i in range(4):
        assert rig.loop.tick().reason == "tick_error"
        assert rig.loop.status()["paused"] is False, i
    rig.loop.tick()
    status = rig.loop.status()
    assert status["paused"] and status["pause_reason"] == "tick_failures"
    assert rig.health == ["degraded"]
    rig.probe.raises = None
    rig.loop.resume("手動處理")
    assert rig.health == ["degraded", "ready"]
    assert rig.loop.tick().action == "run"


def test_readiness_failure_skips_that_namespace_only(tmp_path):
    rig = Rig(tmp_path, names=(OWNER_NS, OTHER_NS))

    def bad():
        raise RuntimeError("readiness 壞了")

    rig.engine(OWNER_NS).dream_readiness = bad  # type: ignore[method-assign]
    d = rig.loop.tick()
    assert (d.action, d.namespace_id) == ("run", OTHER_NS)


def test_readiness_skipped_when_engine_lock_is_busy(tmp_path):
    rig = Rig(tmp_path)
    lock = rig.targets[0].lock
    lock.acquire()
    try:
        started = time.monotonic()
        d = rig.loop.tick()
        assert d.reason == "readiness_unavailable" and time.monotonic() - started < 3
    finally:
        lock.release()
    assert rig.engine().dream_count == 0


# ---- VC-DL-7：租約結果分類（迴圈層）----

@pytest.mark.parametrize("code", ["lease_held", "lease_timeout"])
def test_lease_skips_degrade_on_third_consecutive_once_and_recover_on_success(tmp_path, code):
    rig = Rig(tmp_path, cfg=replace(BASE, min_gap_s=1.0))
    engine = rig.engine()
    engine.dream_result = {"skipped_reason": code, "skipped_detail": "任意文字 A"}
    for i in range(4):
        engine.dream_result["skipped_detail"] = f"detail-{i}"  # 只比對代碼，不解析 detail
        d = rig.loop.tick()
        assert d.action == "run"
        rig.clock.advance(1900)  # 超過 lease_backoff_s
    assert engine.dream_count == 4 and rig.health == ["degraded"]
    # skipped 不吃 min_gap／每日週期數
    assert rig.loop.status()["cycles_24h"] == 0
    engine.dream_result = {}
    rig.loop.tick()
    assert rig.health == ["degraded", "ready"]


def test_lease_io_error_degrades_on_first_and_uses_long_backoff(tmp_path):
    rig = Rig(tmp_path, cfg=replace(BASE, min_gap_s=1.0))
    engine = rig.engine()
    engine.dream_result = {"skipped_reason": "lease_io_error"}
    rig.loop.tick()
    assert rig.health == ["degraded"]
    rig.tick_after(3000)
    assert engine.dream_count == 1  # 退避 3600 秒
    rig.tick_after(700)
    assert engine.dream_count == 2 and rig.health == ["degraded"]


def test_expected_peer_lease_held_never_degrades(tmp_path):
    rig = Rig(tmp_path, cfg=replace(BASE, min_gap_s=1.0, expected_lease_peers=frozenset({OWNER_NS})))
    rig.engine().dream_result = {"skipped_reason": "lease_held"}
    for _ in range(5):
        rig.loop.tick()
        rig.clock.advance(1900)
    assert rig.engine().dream_count == 5 and rig.health == []


def test_already_running_and_window_completed_are_plain_skips(tmp_path):
    rig = Rig(tmp_path, cfg=replace(BASE, min_gap_s=1.0))
    for code in ("already_running", "window_completed"):
        rig.engine().dream_result = {"skipped_reason": code}
        rig.loop.tick()
        rig.clock.advance(400)
    assert rig.engine().dream_count == 2 and rig.health == []
    assert rig.loop.status()["namespaces"][OWNER_NS]["consecutive_failures"] == 0


# ---- 每日上限與休息時段（Phase A 強制週期數）----

def test_daily_cycle_cap_is_enforced_with_rolling_24_hours(tmp_path):
    rig = Rig(tmp_path, cfg=replace(BASE, daily_cycle_cap=3, min_gap_s=1.0))
    for _ in range(3):
        assert rig.loop.tick().action == "run"
        rig.clock.advance(100)
    assert rig.wait_reason() == "daily_cap"
    assert rig.engine().dream_count == 3
    rig.clock.advance(86400 - 300 + 1)  # 最舊一筆滑出 24 小時
    assert rig.loop.tick().action == "run"


def test_rest_window_runs_host_rest_up_to_cycle_cap_and_ignores_daily_cap(tmp_path):
    rig = Rig(tmp_path, pending=2, cfg=replace(BASE, daily_cycle_cap=1, rest_max_cycles=2, min_gap_s=1.0,
                                               rest_quiet_s=1.0))
    rig.wall = TPE_REST
    rig.probe.set(quiet_for_s=10.0)
    for _ in range(2):
        d = rig.loop.tick()
        assert (d.action, d.trigger) == ("run", "host_rest")
        rig.clock.advance(5)
    assert rig.wait_reason() == "rest_cycle_cap"
    assert rig.engine().dream_count == 2
    assert rig.loop.status()["rest_cycles"] == 2
    # 窗口結束後回到一般規則（pending 2 < min_pending：不做夢），隔天進入新窗口計數重設
    rig.wall = TPE_NOON
    assert rig.wait_reason() == "not_enough_pending"
    rig.wall = TPE_REST.replace(day=2)
    assert rig.loop.tick().trigger == "host_rest"


def test_rest_window_does_not_start_cycle_when_remaining_below_deadline(tmp_path):
    rig = Rig(tmp_path, pending=2, cfg=replace(BASE, rest_quiet_s=1.0))
    rig.wall = datetime(2026, 11, 1, 21, 20, tzinfo=UTC)  # 台北 05:20，剩 10 分鐘 < 900 秒
    rig.probe.set(quiet_for_s=10.0)
    assert rig.wait_reason() == "rest_window_ending"


def test_custom_rest_policy_is_used(tmp_path):
    from datetime import time

    rig = Rig(tmp_path, pending=2, cfg=replace(BASE, rest_quiet_s=1.0),
              rest_policy=FixedWindowPolicy(start=time(11, 0), end=time(14, 0)))
    rig.probe.set(quiet_for_s=10.0)
    assert rig.loop.tick().trigger == "host_rest"  # TPE_NOON 落在自訂窗口


# ---- VC-DL-5（迴圈層）：backlog 預設停用、欄位缺失不報錯；啟用時隨週期下降 ----

def test_backlog_disabled_by_default_and_missing_field_never_errors(tmp_path):
    rig = Rig(tmp_path, pending=0)
    rig.engine().readiness["unscanned_pending"] = 91
    assert rig.wait_reason() == "not_enough_pending"
    on = Rig(tmp_path / "x", pending=0, cfg=replace(BASE, backlog_enabled=True))  # 欄位缺失（M-D1 前）
    assert on.wait_reason() == "not_enough_pending"
    assert on.engine().dream_count == 0


def test_backlog_runs_when_enabled_and_respects_replay_skip(tmp_path):
    cfg = replace(BASE, backlog_enabled=True, min_gap_s=1.0, backlog_min_gap_s=10.0)
    rig = Rig(tmp_path, pending=0, cfg=cfg)
    engine = rig.engine()
    engine.readiness["unscanned_pending"] = 91

    def consume(_trigger):
        engine.readiness["unscanned_pending"] -= 8

    engine.dream_hook = consume
    for expected in (83, 75):
        assert rig.loop.tick().trigger == "host_backlog"
        assert engine.readiness["unscanned_pending"] == expected
        rig.clock.advance(100)
    # replay 整輪 skip：一則積壓都沒碰，不計入每日上限並退避
    engine.dream_hook = lambda _t: {"replay": {"skipped_reason": "no_llm"}}
    rig.loop.tick()
    assert rig.metrics.get("memory.dream.backlog_replay_skipped.no_llm") == 1
    assert rig.wait_reason() == "backoff"
    assert len(rig.loop._backlog_starts[OWNER_NS]) == 2


# ---- Probe：State＋Bus ----

class FakeBus:
    def __init__(self) -> None:
        self.handlers: dict[str, list] = {}

    def subscribe(self, event_type, handler) -> None:
        self.handlers.setdefault(event_type, []).append(handler)

    def unsubscribe(self, event_type, handler) -> None:
        self.handlers[event_type].remove(handler)

    def fire(self, event_type: str) -> None:
        for handler in list(self.handlers.get(event_type, [])):
            handler(object())


@dataclass
class _View:
    state: str


class FakeState:
    def __init__(self) -> None:
        self.axes = {"Interaction": "IDLE", "Behavior": "CHAT", "Runtime": "READY"}
        self.fail: set[str] = set()

    def current(self, axis, subject: str = ""):
        if str(axis) in self.fail:
            raise RuntimeError("db 壞了")
        return _View(self.axes[str(axis)])


def make_probe(clock: Clock, **kw):
    state, bus = FakeState(), FakeBus()
    probe = StateBusForegroundProbe(state, bus, clock=clock, **kw)
    probe.start()
    return probe, state, bus


def test_probe_quiet_counts_from_construction_and_resets_on_activity_events():
    clock = Clock(100.0)
    probe, _, bus = make_probe(clock)
    clock.advance(500)
    assert probe.snapshot(clock()).quiet_for_s == 500.0  # 沒收過事件：保守地從建立當下算
    for event in ("input.text_final", "input.utterance_final", "turn.started", "turn.completed", "turn.cancelled",
                  "conversation.participant_joined"):
        clock.advance(100)
        bus.fire(event)
        clock.advance(7)
        assert probe.snapshot(clock()).quiet_for_s == 7.0, event


def test_probe_open_episodes_and_global_stop_tracking():
    clock = Clock()
    probe, _, bus = make_probe(clock)
    bus.fire("conversation.episode_started")
    bus.fire("conversation.episode_started")
    assert probe.snapshot(clock()).open_episodes == 2
    bus.fire("conversation.episode_ended")
    bus.fire("conversation.episode_ended")
    bus.fire("conversation.episode_ended")  # 多餘的 ended 不得變負數
    assert probe.snapshot(clock()).open_episodes == 0
    bus.fire("core.global_stop_engaged")
    assert probe.snapshot(clock()).global_stop_engaged
    bus.fire("core.global_stop_released")
    assert not probe.snapshot(clock()).global_stop_engaged


def test_probe_reads_state_axes_and_is_conservative_on_failure():
    clock = Clock()
    probe, state, _ = make_probe(clock)
    snap = probe.snapshot(clock())
    assert (snap.interaction_state, snap.behavior_state, snap.runtime_ready) == ("IDLE", "CHAT", True)
    state.axes.update({"Interaction": "THINKING", "Behavior": "SLEEP", "Runtime": "DEGRADED"})
    snap = probe.snapshot(clock())
    assert (snap.interaction_state, snap.behavior_state, snap.runtime_ready) == ("THINKING", "SLEEP", True)
    for runtime in ("STARTING", "STOPPING"):
        state.axes["Runtime"] = runtime
        assert probe.snapshot(clock()).runtime_ready is False
    state.fail = {"Interaction", "Runtime"}
    snap = probe.snapshot(clock())
    assert snap.interaction_state == "UNKNOWN" and snap.runtime_ready is False  # Planner 視為前景活躍


def test_probe_voice_activity_and_global_stop_sources_are_injectable():
    clock = Clock(1000.0)
    voice = [None]
    stop = [False]
    probe, _, _ = make_probe(clock, voice_activity_fn=lambda: voice[0], global_stop_fn=lambda: stop[0])
    clock.advance(600)
    assert probe.snapshot(clock()).quiet_for_s == 600.0
    voice[0] = clock() - 5  # VAD 說話中：取較新者
    assert probe.snapshot(clock()).quiet_for_s == 5.0
    stop[0] = True
    assert probe.snapshot(clock()).global_stop_engaged
    stop[0] = False

    def broken():
        raise RuntimeError("x")

    probe2, _, _ = make_probe(clock, global_stop_fn=broken)
    assert probe2.snapshot(clock()).global_stop_engaged  # 查不到旗標視為已停止（保守）


def test_probe_subscribe_and_close_are_idempotent_and_work_with_real_event_bus():
    bus = EventBus()
    probe = StateBusForegroundProbe(FakeState(), bus)
    probe.start()
    probe.start()
    assert len(bus._handlers["turn.started"]) == 1
    probe.close()
    probe.close()
    assert not bus._handlers.get("turn.started")


def test_unavailable_probe_is_conservative_default(tmp_path):
    rig = Rig(tmp_path)
    rig.loop._probe = UnavailableProbe()
    assert rig.wait_reason() == "runtime_not_ready"
    assert rig.engine().dream_count == 0


# ---- MemoryHost 整合 ----

FAST = DreamLoopConfig(
    enabled=True, shadow=False, startup_grace_s=0.0, tick_interval_s=0.01, poll_interval_s=0.01, min_gap_s=0.05,
    idle_quiet_s=1.0, daydream_quiet_s=1.0, rest_quiet_s=1.0, dream_stop_wait_s=5.0,
)
SPECS = (NamespaceSpec(OWNER_NS, owner_id="owner"), NamespaceSpec(OTHER_NS, owner_id="other"), NamespaceSpec(SCOPE_NS))


def make_host(tmp_path: Path, factory=None, *, probe=None, **over):
    factory = factory or FakeEngineFactory()
    kwargs = {
        "engine_factory": factory, "data_root": tmp_path / "core_memory", "namespaces": SPECS,
        "identity": IdentityDirectory(platform_to_actor={}, actor_to_owner={}), "llm_fn": lambda p: "ok",
        "drain_timeout_s": 1.0, "dream_loop": FAST, "dream_probe": probe or Probe(),
    }
    kwargs.update(over)
    return MemoryHost(**kwargs), factory


def wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_vc_dl_1_enabling_both_scheduler_and_loop_is_rejected_at_construction(tmp_path):
    with pytest.raises(MemoryHostError, match="不得同時啟用"):
        make_host(tmp_path, enable_dream_scheduler=True)
    host, _ = make_host(tmp_path, enable_dream_scheduler=True, dream_loop=replace(FAST, enabled=False))
    assert host.dream_status() == {"enabled": False}  # 迴圈未啟用時舊路徑照常


def test_vc_dl_1_start_order_and_scope_namespace_is_never_a_target(tmp_path, monkeypatch):
    factory = FakeEngineFactory()
    original = DreamLoop.start

    def traced(self):
        factory.call_log.append(("*", "loop.start", None))
        original(self)

    monkeypatch.setattr(DreamLoop, "start", traced)
    probe = Probe()
    probe.set(quiet_for_s=1e9)
    host, _ = make_host(tmp_path, factory, probe=probe)
    host.start()
    try:
        for name in (OWNER_NS, OTHER_NS):
            factory.engines[name].readiness["pending"] = 30
        names = [n for _, n, _ in factory.call_log]
        last_listener = max(i for i, n in enumerate(names) if n == "set_dream_listener")
        loop_start = names.index("loop.start")
        assert loop_start > last_listener  # 迴圈最後才啟動
        for name in (OWNER_NS, OTHER_NS):
            calls = factory.calls(name)
            assert "start_dream_scheduler" not in calls
            assert calls.index("configure_dream") < calls.index("set_llm_fn") < calls.index("set_dream_listener")
            assert factory.engines[name].dream_overrides == {"dream_enable_idle": False, "dream_enable_cron": False}
        assert wait_until(lambda: factory.engines[OWNER_NS].dream_count >= 1 and factory.engines[OTHER_NS].dream_count >= 1)
        scope_calls = factory.calls(SCOPE_NS)
        assert "dream" not in scope_calls and "dream_readiness" not in scope_calls
        assert "start_dream_scheduler" not in scope_calls
    finally:
        host.stop()


def test_vc_dl_2_at_most_one_dream_in_flight_across_namespaces(tmp_path):
    probe = Probe()
    lock, state = threading.Lock(), {"now": 0, "peak": 0}

    def slow(_trigger):
        with lock:
            state["now"] += 1
            state["peak"] = max(state["peak"], state["now"])
        time.sleep(0.05)
        with lock:
            state["now"] -= 1

    factory = FakeEngineFactory()
    host, _ = make_host(tmp_path, factory, probe=probe)
    host.start()
    try:
        for name in (OWNER_NS, OTHER_NS):
            factory.engines[name].readiness["pending"] = 30
            factory.engines[name].dream_hook = slow
        assert wait_until(lambda: factory.engines[OWNER_NS].dream_count >= 2 and factory.engines[OTHER_NS].dream_count >= 2)
        assert state["peak"] == 1
    finally:
        host.stop()


def test_vc_dl_4_blocked_dream_does_not_hold_engine_lock_or_executor(tmp_path):
    release = threading.Event()
    entered = threading.Event()
    factory = FakeEngineFactory()
    adapters = FakeAdapterFactory()
    host, _ = make_host(tmp_path, factory, probe=Probe(), adapter_factory=adapters)
    host.start()
    try:
        engine = factory.engines[OWNER_NS]
        engine.episodes.append("hello world")

        def block(_trigger):
            entered.set()
            release.wait(10)

        engine.dream_hook = block
        engine.readiness["pending"] = 30
        assert entered.wait(5)
        assert host.dream_status()["in_flight"]["namespace_id"] == OWNER_NS
        started = time.monotonic()
        assert host.call(OWNER_NS, lambda e: e.retrieve("hello"), timeout_s=1.0).texts == ["hello world"]

        async def store(adapter):
            return await adapter.store("u", "s", session_id="sess", idempotency_key="k1")

        assert host.adapter_call(OWNER_NS, store, timeout_s=1.0)["episode_id"]
        assert time.monotonic() - started < 1.0
    finally:
        release.set()
        host.stop()


def _trace_stop_order(factory: FakeEngineFactory, host: MemoryHost, ns: str = OWNER_NS) -> None:
    engine = factory.engines[ns]
    original_drain = engine.event_emitter.drain

    def drain(timeout=2.0):
        factory.call_log.append((ns, "drain", None))
        return original_drain(timeout)

    engine.event_emitter.drain = drain  # type: ignore[method-assign]
    loop = host.dream_loop
    assert loop is not None
    original_stop = loop.stop

    def stop(timeout=None):
        result = original_stop(timeout)
        factory.call_log.append(("*", "loop.stopped", None))
        return result

    loop.stop = stop  # type: ignore[method-assign]


def test_vc_dl_9_stop_waits_for_in_flight_dream_before_any_other_shutdown_step(tmp_path):
    release, entered = threading.Event(), threading.Event()
    factory = FakeEngineFactory()
    host, _ = make_host(tmp_path, factory, probe=Probe(), adapter_factory=FakeAdapterFactory())
    host.start()
    engine = factory.engines[OWNER_NS]

    def block(_trigger):
        entered.set()
        release.wait(10)

    engine.dream_hook = block
    engine.readiness["pending"] = 30
    assert entered.wait(5)
    _trace_stop_order(factory, host)
    stopper = threading.Thread(target=host.stop)
    stopper.start()
    time.sleep(0.3)
    names = [n for _, n, _ in factory.call_log]
    assert "stop_dream_scheduler" not in names and "loop.stopped" not in names  # 仍在等 dream 返回
    release.set()
    stopper.join(10)
    assert not stopper.is_alive()
    names = [n for ns, n, _ in factory.call_log if ns in ("*", OWNER_NS)]
    order = [names.index(x) for x in ("loop.stopped", "stop_dream_scheduler", "drain", "adapter.aclose", "close")]
    assert order == sorted(order) and len(set(order)) == 5
    count = engine.dream_count
    time.sleep(0.2)
    assert engine.dream_count == count  # 停機後無新 dream
    host.stop()  # 冪等
    assert host.dream_status() == {"enabled": False}


def test_vc_dl_9_stop_timeout_continues_shutdown_and_logs_warning(tmp_path, caplog):
    release, entered = threading.Event(), threading.Event()
    factory = FakeEngineFactory()
    host, _ = make_host(tmp_path, factory, probe=Probe(), dream_loop=replace(FAST, dream_stop_wait_s=0.2))
    host.start()
    engine = factory.engines[OWNER_NS]

    def block(_trigger):
        entered.set()
        release.wait(10)

    engine.dream_hook = block
    engine.readiness["pending"] = 30
    assert entered.wait(5)
    with caplog.at_level(logging.WARNING, logger="uep.memory.dream_loop"):
        started = time.monotonic()
        host.stop()
        assert time.monotonic() - started < 3
    assert any("停止做夢迴圈逾時" in r.message and OWNER_NS in r.message for r in caplog.records)
    names = factory.calls(OWNER_NS)
    assert "stop_dream_scheduler" in names and "close" in names  # 逾時後仍繼續後續關閉
    release.set()


def test_vc_dl_9_start_failure_cleanup_when_loop_not_yet_created(tmp_path):
    factory = FakeEngineFactory()
    host, _ = make_host(tmp_path, factory)
    original = factory.__call__

    def failing(namespace_id, data_root):
        engine = original(namespace_id, data_root)
        if namespace_id == OTHER_NS:
            engine.fail_on.add("set_candidate_sink")
        return engine

    host._engine_factory = failing
    with pytest.raises(RuntimeError, match="注入的失敗"):
        host.start()
    assert host.dream_loop is None and not host.started
    host.stop()  # 迴圈未建立也不炸


# ---- 事件面與單一寫入（VC-DL-10／11）----

class CapturingPublisher:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict, str]] = []

    def publish(self, draft, trusted, **kw):
        self.events.append((draft.event_type, dict(draft.payload), trusted.source))


def dream_health(pub: CapturingPublisher) -> list[tuple[dict, str]]:
    return [(p, s) for t, p, s in pub.events if t == "core.component_health_changed" and p.get("ref_id") == "memory.dream"]


def test_vc_dl_11_no_core_events_for_success_skipped_error_and_health_shape(tmp_path):
    pub = CapturingPublisher()
    factory = FakeEngineFactory()
    cfg = replace(FAST, min_gap_s=0.02, skip_backoff_s=0.01, error_backoff_base_s=60.0, error_backoff_max_s=120.0)
    host, _ = make_host(tmp_path, factory, publisher=pub, dream_loop=cfg, namespaces=(SPECS[0], SPECS[2]))
    host.start()
    try:
        engine = factory.engines[OWNER_NS]
        engine.readiness["pending"] = 30
        for result in ({}, {"skipped_reason": "already_running"}, {"error": "boom"}):
            before = engine.dream_count
            engine.dream_result = result
            assert wait_until(lambda b=before: engine.dream_count > b)
        host.stop()
    finally:
        host.stop()
    types = {t for t, _, _ in pub.events}
    assert "memory.dream_completed" not in types  # Host 不重發（記憶端 emitter 才是來源）
    assert types <= {"core.component_health_changed"}
    assert dream_health(pub) == []  # 單次 error 不升 health；skipped/error 只走 metrics 與 log
    assert host.metrics.get("memory.dream.skipped.already_running") >= 1 and host.metrics.get("memory.dream.error") >= 1


def test_vc_dl_11_lease_io_error_publishes_degraded_once_with_core_health_source(tmp_path):
    pub = CapturingPublisher()
    factory = FakeEngineFactory()
    host, _ = make_host(tmp_path, factory, publisher=pub, namespaces=(SPECS[0], SPECS[2]))
    host.start()
    try:
        engine = factory.engines[OWNER_NS]
        engine.dream_result = {"skipped_reason": "lease_io_error", "skipped_detail": "路徑含記憶內容也不得外流"}
        engine.readiness["pending"] = 30
        assert wait_until(lambda: dream_health(pub))
        time.sleep(0.1)
    finally:
        host.stop()
    events = dream_health(pub)
    assert events == [({"ref_id": "memory.dream", "status": "degraded"}, "core.health")]
    assert host.metrics.get("memory.dream.skipped.lease_io_error") >= 1


def test_vc_dl_10_single_write_counts_and_activity_rows(tmp_path, session_factory):
    factory = FakeEngineFactory()
    cfg = replace(FAST, min_gap_s=60.0, error_backoff_base_s=60.0, skip_backoff_s=60.0)
    probe = Probe()
    host, _ = make_host(tmp_path, factory, probe=probe, session_factory=session_factory, dream_loop=cfg,
                        namespaces=(SPECS[0], SPECS[2]))
    host.start()
    try:
        engine = factory.engines[OWNER_NS]
        engine.readiness["pending"] = 6  # idle 級：host_idle
        probe.set(quiet_for_s=1e9)
        assert wait_until(lambda: host.metrics.get("memory.dream.runs.host_idle") >= 1)
        time.sleep(0.2)
    finally:
        host.stop()
    assert host.metrics.get("memory.dream.runs.host_idle") == 1  # 只 +1：listener 為單一寫入點，迴圈不重複計
    rows = _activity_rows(session_factory)
    assert len(rows) == 1
    assert (rows[0].status, rows[0].origin_ref["outcome"], rows[0].origin_ref["triggered_by"]) == (
        "ENDED", "completed", "host_idle")
    assert rows[0].presence_bound is False and rows[0].ended_at is not None and rows[0].version == 2


@pytest.mark.parametrize(("result", "outcome"), [
    ({"error": "boom"}, "failed"),
    ({"skipped_reason": "lease_held"}, "skipped_lease_held"),
    ({"skipped_reason": "already_running"}, "skipped_already_running"),
])
def test_vc_dl_10_failed_and_skipped_end_the_same_row_without_dangling_active(tmp_path, session_factory, result, outcome):
    factory = FakeEngineFactory()
    probe = Probe()
    host, _ = make_host(tmp_path, factory, probe=probe, session_factory=session_factory,
                        dream_loop=replace(FAST, min_gap_s=60.0, error_backoff_base_s=60.0, skip_backoff_s=60.0,
                                           lease_backoff_s=60.0),
                        namespaces=(SPECS[0], SPECS[2]))
    host.start()
    try:
        engine = factory.engines[OWNER_NS]
        engine.dream_result = result
        engine.readiness["pending"] = 30
        assert wait_until(lambda: engine.dream_count >= 1)
        time.sleep(0.2)
    finally:
        host.stop()
    rows = _activity_rows(session_factory)
    assert len(rows) == 1
    assert (rows[0].status, rows[0].origin_ref["outcome"]) == ("ENDED", outcome)


def test_vc_dl_10_listener_not_called_path_still_ends_the_activity_row(tmp_path, session_factory):
    """engine.dream() 拋例外或舊 engine 沒呼叫 listener：迴圈補單一寫入，不留懸空 ACTIVE。"""
    factory = FakeEngineFactory()
    host, _ = make_host(tmp_path, factory, session_factory=session_factory, namespaces=(SPECS[0], SPECS[2]),
                        dream_loop=replace(FAST, min_gap_s=60.0, error_backoff_base_s=60.0))
    host.start()
    try:
        engine = factory.engines[OWNER_NS]

        def boom(_trigger):
            raise RuntimeError("engine 已關閉")

        engine.dream_hook = boom
        engine.readiness["pending"] = 30
        assert wait_until(lambda: host.metrics.get("memory.dream.error") >= 1)
    finally:
        host.stop()
    rows = _activity_rows(session_factory)
    assert [(r.status, r.origin_ref["outcome"]) for r in rows] == [("ENDED", "failed")]


def test_vc_dl_10_crash_leftover_is_swept_as_interrupted_and_active_maintenance_does_not_block_entry(
    tmp_path, session_factory
):
    recorder = DreamActivityRecorder(session_factory)
    activity_id = recorder.record_started(OWNER_NS, "host_idle")
    rows = _activity_rows(session_factory)
    assert rows[0].status == "ACTIVE" and rows[0].activity_id == activity_id
    # ACTIVE maintenance 不阻擋 task／action 進入（State Coordinator 的閘門不讀它）
    stack = make_stack(session_factory, tmp_path, start=False)
    assert stack.state.task_entry_blocked() is None
    with session_factory() as s:
        assert stack.state.action_entry_blocked(s) is None
    # 崩潰後下次啟動 sweep_stale 標 interrupted
    assert DreamActivityRecorder(session_factory).sweep_stale() == 1
    rows = _activity_rows(session_factory)
    assert (rows[0].status, rows[0].origin_ref["outcome"]) == ("ENDED", "interrupted")
    assert recorder.finish(activity_id, "completed") is False  # 已結束的列不再被改寫


def _activity_rows(session_factory) -> list[ActivityRow]:
    with session_factory() as s:
        rows = list(s.scalars(select(ActivityRow).where(ActivityRow.origin_ref["component"].as_string() == COMPONENT)))
        s.expunge_all()
        return rows


def test_dream_status_reports_loop_state_and_namespaces(tmp_path):
    factory = FakeEngineFactory()
    host, _ = make_host(tmp_path, factory, probe=Probe(), namespaces=(SPECS[0], SPECS[2]))
    host.start()
    try:
        factory.engines[OWNER_NS].readiness["pending"] = 30
        assert wait_until(lambda: factory.engines[OWNER_NS].dream_count >= 1)
        status = host.dream_status()
        assert status["enabled"] and status["running"] and status["shadow"] is False
        assert OWNER_NS in status["namespaces"] and SCOPE_NS not in status["namespaces"]
        assert status["wants_intake"] is False
    finally:
        host.stop()
    assert host.dream_loop is None
