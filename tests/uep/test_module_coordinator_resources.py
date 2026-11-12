"""Module Coordinator PR-3：pin／lease、資源預算、外部壓力驅逐、依賴 failed、停機反序、hung 上限（離線，fake handle＋模擬量測）。

不需 DB、不碰 GPU：VRAM 量測一律走 ``FakeProbe``（NVML 只用假模組測 import 容錯）。時間用假時鐘＋手動 ``tick()``；
只輪詢背景 worker 的結果（有時限）。每個測試結束一定釋放所有 gate 並停 coordinator。
"""
from __future__ import annotations

import random
import sys
from types import SimpleNamespace
from typing import Any

import pytest

from tests.uep.module_fakes import FakeHandle, FakeObserver
from tests.uep.test_module_coordinator import Rig, wait_until
from uep.lifecycle import budget as budget_mod
from uep.lifecycle.budget import (
    BudgetConfig,
    NvmlVramProbe,
    ProbeUnavailable,
    ResourceBudget,
    VramReading,
)
from uep.lifecycle.coordinator import ModuleCoordinator
from uep.lifecycle.descriptor import ModuleDescriptor

TOTAL = 24000
HEADROOM = 2000


class FakeProbe:
    """模擬 VRAM 量測；測試直接改 ``free``／``error`` 來模擬外部占用與量測失敗。"""

    def __init__(self, total: int = TOTAL, free: int = 20000) -> None:
        self.total, self.free = total, free
        self.error: BaseException | None = None
        self.reads = 0

    def read(self) -> VramReading:
        self.reads += 1
        if self.error is not None:
            raise self.error
        return VramReading(self.total, self.free)


def make_budget(probe: FakeProbe, **over: Any) -> ResourceBudget:
    cfg = {"headroom_mb": HEADROOM, "reading_max_age_s": 5.0, "recover_margin_mb": 256, "evict_cooldown_s": 60.0, **over}
    return ResourceBudget(BudgetConfig(**cfg), probe)


@pytest.fixture
def rig():
    made: list[Rig] = []

    def make(**kw: Any) -> Rig:
        r = Rig(**kw)
        made.append(r)
        return r

    yield make
    for r in made:
        r.close()


def gpu(r: Rig, module_id: str, mb: int | None = 2000, *deps: str, handle: Any = None, **desc: Any) -> Any:
    return r.add(module_id, *deps, handle=handle, uses_gpu=True, vram_mb=mb, **desc)


def step(r: Rig, seconds: float = 1.0) -> None:
    """推進時鐘並 tick（會重新取樣）。"""
    r.clock.advance(seconds)
    r.mc.tick()


def settle(r: Rig) -> None:
    assert r.mc.wait_settled(5)


# --- 純預算邏輯 ---

def test_config_and_reading_validation():
    with pytest.raises(ValueError):
        BudgetConfig(headroom_mb=-1)
    with pytest.raises(ValueError):
        BudgetConfig(headroom_mb=0, budget_mb=0)
    with pytest.raises(ValueError):
        BudgetConfig(headroom_mb=0, reading_max_age_s=0)
    with pytest.raises(ValueError):
        VramReading(total_mb=10, free_mb=11)


def test_evaluate_takes_the_more_conservative_side_and_counts_each_thing_once():
    probe = FakeProbe(total=24000, free=20000)
    b = make_budget(probe)
    assert b.refresh(100.0)
    # 預算上限未設 = 實測總量 - 水位 = 22000；預算側 22000-held，實測側 20000-2000-pending
    assert b.evaluate(cost_mb=3000, held_mb=0, pending_mb=0, now=100.0).effective_free_mb == 18000
    # held 很大而實測空閒也大：預算側較保守
    assert b.evaluate(cost_mb=1000, held_mb=21500, pending_mb=0, now=100.0).reason == "insufficient_resource"
    # 載入中的預留：實測側額外扣 pending（外部占用已在 free 裡，不再扣第二次）
    adm = b.evaluate(cost_mb=1000, held_mb=3000, pending_mb=3000, now=100.0)
    assert adm.ok and adm.effective_free_mb == min(22000 - 3000, 20000 - 2000 - 3000)
    # 顯式預算上限
    b2 = make_budget(probe, budget_mb=5000)
    b2.refresh(100.0)
    assert b2.evaluate(cost_mb=2000, held_mb=3500, pending_mb=0, now=100.0).reason == "insufficient_resource"
    assert b2.evaluate(cost_mb=1500, held_mb=3500, pending_mb=0, now=100.0).ok


def test_evaluate_fails_closed_on_unknown_cost_failed_or_stale_measurement():
    probe = FakeProbe()
    b = make_budget(probe)
    assert b.evaluate(cost_mb=100, held_mb=0, pending_mb=0, now=0.0).reason == "measurement_failed"  # 從未量測
    assert b.evaluate(cost_mb=None, held_mb=0, pending_mb=0, now=0.0).reason == "unmeasured_cost"
    b.refresh(10.0)
    assert b.evaluate(cost_mb=100, held_mb=0, pending_mb=0, now=14.9).ok
    assert b.evaluate(cost_mb=100, held_mb=0, pending_mb=0, now=15.1).reason == "measurement_stale"
    probe.error = RuntimeError("boom")
    assert not b.refresh(16.0) and b.failures == 1
    assert b.evaluate(cost_mb=100, held_mb=0, pending_mb=0, now=16.1).reason == "measurement_failed"
    probe.error = None
    assert b.refresh(17.0) and b.evaluate(cost_mb=100, held_mb=0, pending_mb=0, now=17.0).ok


def test_pressure_has_hysteresis_and_ignores_stale_samples():
    probe = FakeProbe(free=5000)
    b = make_budget(probe)
    b.refresh(0.0)
    assert not b.update_pressure(0.0)
    probe.free = 1999
    b.refresh(1.0)
    assert b.update_pressure(1.0)
    probe.free = 2100  # 高於水位但未達回復水位（2000+256）：維持壓力，不在水位線上抖動
    b.refresh(2.0)
    assert b.update_pressure(2.0) and b.evaluate(cost_mb=1, held_mb=0, pending_mb=0, now=2.0).reason == "pressure"
    probe.free = 2256
    b.refresh(3.0)
    assert not b.update_pressure(3.0)
    probe.free = 100
    b.refresh(4.0)
    assert b.update_pressure(4.0)
    assert b.update_pressure(100.0)  # 樣本過期：不憑舊資料改變旗標


def test_nvml_probe_tolerates_missing_pynvml_and_does_not_import_at_module_level(monkeypatch):
    assert "pynvml" not in budget_mod.__dict__  # 沒有頂層 import

    def missing(name: str):
        raise ImportError(name)

    with pytest.raises(ProbeUnavailable):
        NvmlVramProbe(0, module_loader=missing).read()
    calls: list[str] = []
    fake = SimpleNamespace(
        nvmlInit=lambda: calls.append("init"),
        nvmlDeviceGetHandleByIndex=lambda i: ("h", i),
        nvmlDeviceGetMemoryInfo=lambda h: SimpleNamespace(total=24000 * 1024 * 1024, free=9000 * 1024 * 1024),
    )
    probe = NvmlVramProbe(1, module_loader=lambda name: fake)
    assert probe.read() == VramReading(24000, 9000) and probe.read() == VramReading(24000, 9000)
    assert calls == ["init"]  # 只初始化一次
    broken = SimpleNamespace(nvmlInit=lambda: (_ for _ in ()).throw(RuntimeError("no driver")))
    with pytest.raises(ProbeUnavailable):
        NvmlVramProbe(0, module_loader=lambda name: broken).read()
    monkeypatch.setitem(sys.modules, "pynvml", None)  # 讓真正的 import 失敗，也只會是 ProbeUnavailable
    with pytest.raises(ProbeUnavailable):
        NvmlVramProbe(0).read()


# --- 預算放行（coordinator）---

def test_budget_admits_gpu_load_and_rejects_when_it_does_not_fit_without_queueing(rig):
    probe = FakeProbe(free=20000)
    r = rig(budget=make_budget(probe))
    gpu(r, "g1", 9500, autoload=True)
    gpu(r, "g2", 9500, autoload=True)  # 兩個載入搶最後空間：9000+9000 > 20000-2000 的實測側（含 pending）-> 只有一個成功
    r.mc.start()
    settle(r)
    states = {m: r.state(m) for m in ("g1", "g2")}
    assert sorted(states.values()) == ["ready", "unloaded"]
    loser = next(m for m, s in states.items() if s == "unloaded")
    assert r.mc.status(loser).admission_reason == "insufficient_resource"
    assert r.mc.counters["rejected_insufficient_resource"] == 1
    # 顯式請求被拒：回 rejected 且不排隊（之後有空間也不會自己載）
    res = r.mc.request_load(loser)
    assert not res.accepted and res.reason == "rejected:insufficient_resource"
    probe.free = 24000
    step(r, 10)
    settle(r)
    assert r.state(loser) == "unloaded"
    assert r.handles[loser].calls == []


def test_unknown_cost_blocks_gpu_load_but_not_services(rig):
    probe = FakeProbe()
    r = rig(budget=make_budget(probe))
    gpu(r, "g_unknown", None)
    r.add("svc", autoload=True)
    r.mc.start()
    settle(r)
    assert r.state("svc") == "ready" and r.state("g_unknown") == "unloaded"
    assert r.mc.request_load("g_unknown").reason == "rejected:unmeasured_cost"
    assert r.handles["g_unknown"].calls == []


def test_measurement_failure_and_staleness_reject_gpu_only_and_recover(rig):
    probe = FakeProbe()
    probe.error = RuntimeError("nvml down")
    r = rig(budget=make_budget(probe))
    gpu(r, "g1", 1000)
    r.add("svc")
    r.mc.start()
    assert r.mc.request_load("g1").reason == "rejected:measurement_failed"
    assert r.mc.request_load("svc").accepted
    r.until("svc", "ready")
    probe.error = None
    step(r)
    assert r.mc.request_load("g1").accepted
    r.until("g1", "ready")
    # 之後 tick 沒有執行（樣本過期）：新的 GPU 載入被拒；服務不受影響（同一個 coordinator 啟動後不能再登記，另開一個）
    r2 = rig(budget=make_budget(FakeProbe()))
    gpu(r2, "g2", 1000)
    r2.add("svc2")
    r2.mc.start()
    r2.clock.advance(6)  # 超過 reading_max_age_s 且沒有 tick 重新取樣
    assert r2.mc.request_load("g2").reason == "rejected:measurement_stale"
    assert r2.mc.request_load("svc2").accepted
    step(r2, 0)
    assert r2.mc.request_load("g2").accepted


def test_budget_not_configured_is_fail_closed_for_gpu_only(rig):
    r = rig()  # 沒有 budget
    gpu(r, "g1", 100)
    r.add("svc", autoload=True)
    r.mc.start()
    settle(r)
    assert r.state("svc") == "ready"
    assert r.mc.request_load("g1").reason == "rejected:gpu_budget_unconfigured"
    assert r.handles["g1"].calls == []


def test_observation_only_gpu_handle_is_not_gated(rig):
    r = rig(budget=make_budget(FakeProbe(free=0)))
    obs = FakeObserver()
    r.add("stt", handle=obs, kind="model", pinned=True, autoload=True, uses_gpu=True, vram_mb=None)
    r.mc.start()
    settle(r)
    assert r.state("stt") == "ready" and obs.probes >= 1  # 觀測型不載入，預算閘不適用


def test_unconfirmed_unload_does_not_return_budget(rig):
    probe = FakeProbe(free=20000)
    r = rig(budget=make_budget(probe, budget_mb=10000))
    h1 = gpu(r, "g1", 8000, autoload=True)
    gpu(r, "g2", 8000)
    r.mc.start()
    settle(r)
    assert r.state("g1") == "ready"
    h1.fail["unload"] = RuntimeError("busy")
    assert r.mc.request_unload("g1").accepted
    r.until("g1", "failed")
    assert r.mc.status("g1").occupied
    step(r)
    assert r.mc.request_load("g2").reason == "rejected:insufficient_resource"  # g1 的占用未確認釋放，預算不退
    h1.fail.clear()
    r.mc.reset_retries("g1")
    for _ in range(12):  # 清殘留 -> 確認釋放後才退預算
        step(r, 6)
        if r.state("g1") in ("unloaded", "ready"):
            break
    wait_until(lambda: not r.mc.status("g1").in_flight)
    r.mc.request_unload("g1")
    wait_until(lambda: r.state("g1") == "unloaded" and not r.mc.status("g1").occupied)
    step(r)
    assert r.mc.request_load("g2").accepted


def test_timeout_keeps_reservation_until_handle_returns(rig):
    probe = FakeProbe(free=20000)
    r = rig(budget=make_budget(probe, budget_mb=10000))
    h1 = gpu(r, "g1", 8000, autoload=True)
    gpu(r, "g2", 8000)
    gate = h1.block("load")
    r.mc.start()
    wait_until(h1.entered["load"].is_set)
    step(r, 31)  # load_timeout -> failed，保留占用
    assert r.state("g1") == "failed" and r.mc.status("g1").occupied
    assert r.mc.request_load("g2").reason == "rejected:insufficient_resource"
    gate.set()
    wait_until(lambda: not r.mc.status("g1").in_flight)
    assert r.mc.status("g1").occupied  # 晚完成：資源其實被取走，仍保留


# --- 外部壓力與驅逐 ---

def _pressure_rig(rig, **budget_over: Any):
    probe = FakeProbe(free=20000)
    r = rig(budget=make_budget(probe, **budget_over))
    return r, probe


def _unloaded_by_pressure(r: Rig, module_id: str) -> bool:
    return any(e["cause"] == "pressure" for e in r.rec.of(module_id))


def test_pressure_blocks_new_loads_and_evicts_p3_then_p2_oldest_first_one_at_a_time(rig):
    r, probe = _pressure_rig(rig)
    gpu(r, "bg", priority="background", autoload=True)
    gpu(r, "sp_old", priority="speculative", autoload=True)
    gpu(r, "sp_new", priority="speculative", autoload=True)
    gpu(r, "od", priority="on_demand", autoload=True)
    gpu(r, "late", priority="on_demand")
    r.mc.start()
    settle(r)
    assert all(r.state(m) == "ready" for m in ("bg", "sp_old", "sp_new", "od"))
    step(r, 5)
    lease = r.mc.acquire_lease("sp_new", "run-1")  # sp_new 較近被使用（lease 釋放後仍是較新的 last_used）
    assert lease.granted and r.mc.release_lease(lease.lease_id)
    probe.free = 1000  # 外部壓力：低於保留水位
    step(r)
    assert r.mc.request_load("late").reason == "rejected:pressure"  # 擋新載入
    wait_until(lambda: r.state("bg") == "unloaded")
    assert r.mc.counters["evicted"] == 1 and r.state("sp_old") == "ready"  # 一次只收一個，P3 先
    step(r, 1)  # 冷卻中且樣本不比收回完成更新
    assert r.state("sp_old") == "ready"
    step(r, 61)
    wait_until(lambda: r.state("sp_old") == "unloaded")  # P2：最久未使用者先
    assert r.state("sp_new") == "ready"
    step(r, 1)  # 結案上一次收回（開始冷卻）
    step(r, 61)
    wait_until(lambda: r.state("sp_new") == "unloaded")
    step(r, 1)
    step(r, 61)
    settle(r)
    assert r.state("od") == "ready" and not _unloaded_by_pressure(r, "od")  # on_demand 不被壓力收回
    assert r.mc.counters["evicted"] == 3
    causes = [e["cause"] for e in r.rec.of("bg") if e["state"] == "unloading"]
    assert causes == ["pressure"] and r.rec.of("bg")[-1]["state"] == "unloaded"


def test_pressure_never_touches_protected_or_observed_or_busy_modules(rig):
    r, probe = _pressure_rig(rig)
    gpu(r, "pinned_m", priority="speculative", pinned=True, autoload=True)
    gpu(r, "op_pinned", priority="background", autoload=True)
    gpu(r, "leased", priority="background", autoload=True)
    gpu(r, "res", priority="resident", autoload=True)
    gpu(r, "base", priority="speculative", autoload=True)
    r.add("user_of_base", "base", autoload=True)  # 作用中的依賴者 => base 不可收回
    r.add("stt", handle=FakeObserver(), kind="model", pinned=True, autoload=True, uses_gpu=True, vram_mb=None, priority="background")
    r.mc.start()
    settle(r)
    assert r.mc.pin("op_pinned").accepted
    assert r.mc.acquire_lease("leased", "run-1", ttl_s=3000).granted
    probe.free = 100
    for _ in range(4):
        step(r, 61)
        settle(r)
    assert r.mc.counters.get("evicted", 0) == 0
    for m in ("pinned_m", "op_pinned", "leased", "res", "base", "user_of_base", "stt"):
        assert r.state(m) == "ready", m
    assert {m: r.mc.status(m).protected for m in ("pinned_m", "op_pinned", "leased", "res", "base")} == {
        "pinned_m": "pinned", "op_pinned": "pinned", "leased": "lease", "res": "resident", "base": "has_dependents",
    }


def test_pressure_waits_for_a_fresher_sample_after_each_eviction(rig):
    r, probe = _pressure_rig(rig, evict_cooldown_s=0)
    for m in ("a", "b", "c"):
        gpu(r, m, priority="speculative", autoload=True)
    r.mc.start()
    settle(r)
    probe.free = 100
    step(r, 1)
    wait_until(lambda: sum(r.state(m) == "unloaded" for m in "abc") == 1)
    r.mc.tick()  # 同一時刻（樣本時間戳不比收回完成新）：不得連續收
    r.mc.tick()
    assert sum(r.state(m) == "unloaded" for m in "abc") == 1 and r.mc.counters["evicted"] == 1
    step(r, 1)
    wait_until(lambda: sum(r.state(m) == "unloaded" for m in "abc") == 2)


def test_pressure_does_not_act_on_stale_or_failed_measurements(rig):
    r, probe = _pressure_rig(rig)
    gpu(r, "sp", priority="speculative", autoload=True)
    r.mc.start()
    settle(r)
    probe.free = 100
    step(r, 1)
    wait_until(lambda: r.state("sp") == "unloaded")  # 有效樣本：收回
    r2, probe2 = _pressure_rig(rig)
    gpu(r2, "sp2", priority="speculative", autoload=True)
    r2.mc.start()
    settle(r2)
    probe2.free = 100
    probe2.error = RuntimeError("down")
    for _ in range(3):
        step(r2, 61)
    assert r2.state("sp2") == "ready" and r2.mc.counters.get("evicted", 0) == 0  # 量測失敗：不憑舊資料動作


def test_evicted_module_is_held_back_until_cooldown_and_recovery(rig):
    r, probe = _pressure_rig(rig)
    gpu(r, "sp", priority="speculative", autoload=True)
    r.mc.start()
    settle(r)
    probe.free = 100
    step(r, 1)
    wait_until(lambda: r.state("sp") == "unloaded")
    probe.free = 20000  # 壓力解除（高於水位＋回復餘裕）
    step(r, 1)
    assert r.mc.request_load("sp").reason == "rejected:evict_hold"  # 冷卻內不重載（防反覆卸載重載）
    step(r, 61)
    assert r.mc.request_load("sp").accepted
    r.until("sp", "ready")


def test_observation_mode_never_evicts_or_admits(rig):
    probe = FakeProbe(free=20000)
    r = rig(budget=make_budget(probe), mode="observe")
    h = r.add("sp", handle=FakeHandle(), uses_gpu=True, vram_mb=1000, priority="speculative", autoload=True)
    r.mc.start()
    settle(r)
    probe.free = 100
    step(r, 61)
    assert r.state("sp") == "ready" and "unload" not in h.calls and r.mc.counters.get("evicted", 0) == 0


# --- R8：優先序只看描述子 ---

def test_urgency_and_deadline_hints_do_not_change_priority_or_protection(rig):
    r, probe = _pressure_rig(rig)
    gpu(r, "bg", priority="background", autoload=True)
    gpu(r, "sp", priority="speculative", autoload=True)
    r.mc.start()
    settle(r)
    assert r.mc.request_load("bg", urgency=1.0, deadline_ms=1).reason == "already_ready"  # 提示被接受但不生效
    probe.free = 100
    step(r, 1)
    wait_until(lambda: r.state("bg") == "unloaded")  # 優先序仍由描述子決定：background 先被收回
    assert r.state("sp") == "ready"
    assert r.mc.status("sp").protected is None  # 沒有任何提示能讓它得到保護


# --- pin／lease ---

def test_lease_requires_ready_blocks_unload_and_eviction_and_expires_by_clock(rig):
    r = rig()
    r.add("m", autoload=True)
    r.add("cold")
    r.mc.start()
    settle(r)
    assert r.mc.acquire_lease("cold", "run-1").reason == "not_ready"
    assert r.mc.acquire_lease("nope", "run-1").reason == "unknown_module"
    assert r.mc.acquire_lease("m", "").reason == "bad_owner"
    assert r.mc.acquire_lease("m", "run-1", ttl_s=-1).reason == "bad_ttl"
    got = r.mc.acquire_lease("m", "run-1", ttl_s=30)
    assert got.granted and r.mc.status("m").leases == 1 and r.mc.status("m").protected == "lease"
    res = r.mc.request_unload("m")
    assert not res.accepted and res.reason == "protected:lease" and r.state("m") == "ready"
    r.clock.advance(31)  # 沒有 tick：有效性在檢查當下比時鐘
    assert r.mc.status("m").leases == 0 and r.mc.status("m").protected is None
    r.clock.advance(0)
    assert r.mc.request_unload("m").accepted
    r.until("m", "unloaded")
    r.mc.tick()
    assert r.mc.counters.get("lease_expired", 0) + r.mc.counters.get("lease_revoked", 0) >= 1


def test_lease_ttl_is_capped_renewable_and_lost_when_module_fails(rig):
    r = rig(lease_max_ttl_s=100.0, lease_ttl_s=10.0)
    r.add("m", autoload=True)
    r.mc.start()
    settle(r)
    lease = r.mc.acquire_lease("m", "run-1", ttl_s=10_000)
    assert lease.expires_at == r.clock.now + 100.0
    r.clock.advance(90)
    assert r.mc.renew_lease(lease.lease_id).granted  # 預設 TTL 10
    r.clock.advance(11)
    assert r.mc.renew_lease(lease.lease_id).reason == "lease_expired"
    assert r.mc.renew_lease(lease.lease_id).reason == "lease_unknown"  # 已被回收
    lease2 = r.mc.acquire_lease("m", "run-1")
    assert lease2.granted and lease2.expires_at == r.clock.now + 10.0
    r.handles["m"].probe_result = "dead"
    # 模組失敗 => lease 作廢，持有者 renew 時得知
    r2 = rig(probe_interval_s=5.0, probe_dead_threshold=1)
    r2.add("m", autoload=True)
    r2.mc.start()
    settle(r2)
    held = r2.mc.acquire_lease("m", "run-1", ttl_s=1000)
    r2.handles["m"].probe_result = "dead"
    step(r2, 6)
    r2.until("m", "failed")
    assert r2.mc.renew_lease(held.lease_id).reason == "lease_unknown"


def test_release_is_idempotent_and_release_by_owner(rig):
    r = rig()
    for m in ("a", "b"):
        r.add(m, autoload=True)
    r.mc.start()
    settle(r)
    l1 = r.mc.acquire_lease("a", "run-1")
    l2 = r.mc.acquire_lease("b", "run-1")
    l3 = r.mc.acquire_lease("b", "run-2")
    assert r.mc.release_lease(l1.lease_id) and not r.mc.release_lease(l1.lease_id)
    assert r.mc.release_by_owner("run-1") == 1 and r.mc.release_by_owner("run-1") == 0
    assert not r.mc.release_lease(l2.lease_id)
    assert r.mc.status("b").leases == 1
    assert r.mc.release_by_owner("run-2") == 1 and not r.mc.release_lease(l3.lease_id)


def test_pin_blocks_unload_unpin_restores_descriptor_pin_is_permanent(rig):
    r = rig()
    r.add("m", autoload=True)
    r.add("fixed", autoload=True, pinned=True)
    r.add("res", autoload=True, priority="resident")
    r.mc.start()
    settle(r)
    assert r.mc.pin("m").accepted and r.mc.request_unload("m").reason == "protected:pinned"
    assert r.mc.unpin("m").accepted and r.mc.request_unload("m").accepted
    assert r.mc.request_unload("fixed").reason == "protected:pinned"
    assert r.mc.unpin("fixed").reason == "descriptor_pinned" and r.mc.request_unload("fixed").reason == "protected:pinned"
    assert r.mc.request_unload("res").reason == "protected:resident"
    assert r.mc.pin("nope").reason == "unknown_module"


def test_request_unload_refuses_while_active_dependents_exist(rig):
    r = rig()
    r.add("base", autoload=True)
    r.add("user", "base", autoload=True)
    r.mc.start()
    settle(r)
    assert r.mc.request_unload("base").reason == "protected:has_dependents"
    assert r.mc.request_unload("user").accepted
    r.until("user", "unloaded")
    assert r.mc.request_unload("base").accepted


def test_stop_force_releases_leases(rig):
    r = rig()
    r.add("m", autoload=True)
    r.mc.start()
    settle(r)
    lease = r.mc.acquire_lease("m", "run-1", ttl_s=1000)
    r.mc.stop(timeout_s=3)
    assert r.state("m") == "unloaded"  # 有 lease 也要停（停機例外）
    assert r.mc.renew_lease(lease.lease_id).reason == "lease_unknown"
    assert r.mc.acquire_lease("m", "run-2").reason == "not_running"


def test_protected_modules_are_never_unloaded_by_user_or_pressure_paths_randomized(rig):
    """屬性測試（固定種子，無 hypothesis）：任何操作序列下，操作前受保護且作用中的模組，操作後仍作用中。"""
    for seed in range(6):
        rnd = random.Random(seed)
        probe = FakeProbe(free=20000)
        r = rig(budget=make_budget(probe, evict_cooldown_s=0))
        gpu(r, "p", priority="speculative", pinned=True, autoload=True)
        gpu(r, "res", priority="resident", autoload=True)
        gpu(r, "x1", priority="background", autoload=True)
        gpu(r, "x2", priority="speculative", autoload=True)
        r.add("dep", "x2", autoload=True)
        r.mc.start()
        settle(r)
        names = ("p", "res", "x1", "x2", "dep")
        lease_ids: list[str] = []
        for _ in range(50):
            before = {m for m in names if r.mc.status(m).protected is not None and r.state(m) in ("ready", "degraded")}
            op = rnd.choice(("lease", "release", "unload", "load", "pressure", "calm", "tick", "advance", "pin", "unpin"))
            target = rnd.choice(names)
            if op == "lease":
                got = r.mc.acquire_lease(target, f"o{rnd.randint(0, 2)}", ttl_s=rnd.choice((5, 50)))
                if got.granted:
                    lease_ids.append(got.lease_id)
            elif op == "release" and lease_ids:
                r.mc.release_lease(lease_ids.pop())
            elif op == "unload":
                r.mc.request_unload(target)
            elif op == "load":
                r.mc.request_load(target)
            elif op == "pressure":
                probe.free = 100
            elif op == "calm":
                probe.free = 20000
            elif op == "pin":
                r.mc.pin(target)
            elif op == "unpin":
                r.mc.unpin(target)
            if op == "advance":
                r.clock.advance(rnd.choice((1, 6, 61)))
            elif op in ("tick", "pressure", "calm"):
                step(r, rnd.choice((1, 61)))
            r.mc.wait_settled(5)
            for m in before:
                # 只有 advance（lease 可能自然過期）以外的操作，才要求保護仍然有效
                if op != "advance":
                    assert r.state(m) in ("ready", "degraded"), (seed, op, target, m)


# --- 依賴 failed ---

def _drive(r: Rig, pred, seconds: float = 6.0, rounds: int = 25) -> None:
    for _ in range(rounds):
        if pred():
            return
        step(r, seconds)
        r.mc.wait_settled(2)
    assert pred()


def test_dependency_failure_degrades_dependents_transitively_without_burning_their_retries(rig):
    r = rig(probe_interval_s=10.0, probe_dead_threshold=1)
    r.add("a", autoload=True)
    r.add("b", "a", autoload=True)
    r.add("c", "b", autoload=True)
    r.mc.start()
    settle(r)
    r.handles["a"].probe_result = "dead"
    step(r, 11)
    wait_until(lambda: r.state("a") == "failed" and r.state("b") == "degraded" and r.state("c") == "degraded")
    assert r.mc.status("b").reason_code == "dependency_failed" and r.mc.status("c").reason_code == "dependency_failed"
    assert r.mc.status("b").failures == 0 and r.mc.status("c").failures == 0  # 不燒依賴者自己的重試次數
    assert r.mc.status("b").readiness == "ready"  # degraded 的消費端行為（degradable）由 registry 決定，coordinator 只報狀態
    r.handles["a"].fail["load"] = RuntimeError("still down")  # a 重試失敗期間，依賴者不翻轉
    for _ in range(3):
        step(r, 11)  # 依賴者的探活為 ok 也不能洗掉標記
        r.mc.wait_settled(2)
        assert r.state("b") == "degraded" and r.state("c") == "degraded"
    r.handles["a"].fail.clear()
    r.handles["a"].probe_result = "ok"
    r.mc.reset_retries("a")
    _drive(r, lambda: r.state("a") == "ready" and r.state("b") == "ready" and r.state("c") == "ready")
    assert [e["state"] for e in r.rec.of("b") if e["state"] in ("ready", "degraded")] == ["ready", "degraded", "ready"]  # 不翻轉


# --- 停機反序 ---

class OrderedHandle(FakeHandle):
    def __init__(self, name: str, log: list[str]) -> None:
        super().__init__()
        self._name, self._log = name, log

    def unload(self) -> None:
        self._log.append(self._name)
        super().unload()


def test_shutdown_unloads_dependents_before_dependencies(rig):
    r = rig()
    order: list[str] = []
    for name, deps in (("a", ()), ("b", ("a",)), ("c", ("b",)), ("z", ())):
        r.add(name, *deps, handle=OrderedHandle(name, order), autoload=True)
    r.mc.start()
    settle(r)
    r.mc.stop(timeout_s=5)
    assert order.index("c") < order.index("b") < order.index("a") and set(order) == {"a", "b", "c", "z"}
    idx = {m: [e["state"] for e in r.rec.of(m)].index("unloading") for m in "abc"}
    pos = {(e["module_id"], e["state"]): i for i, e in enumerate(r.rec.events)}
    assert pos[("c", "unloaded")] < pos[("b", "unloading")] and pos[("b", "unloaded")] < pos[("a", "unloading")]
    assert idx["a"] > 0 and all(r.state(m) == "unloaded" for m in "abcz")
    assert all(e["cause"] == "shutdown" for m in "abc" for e in r.rec.of(m)[-2:])


def test_shutdown_with_hung_dependent_gives_up_after_timeout_and_keeps_occupancy(rig):
    r = rig()
    log: list[str] = []
    r.add("a", handle=OrderedHandle("a", log), autoload=True)
    c = OrderedHandle("c", log)
    r.add("c", "a", handle=c, autoload=True)
    r.mc.start()
    settle(r)
    c.block("unload")
    r.mc.stop(timeout_s=0.4)
    assert r.state("c") == "failed" and r.mc.status("c").occupied
    assert r.state("a") == "failed" and log == ["c"]  # a 在等 c：逾時後不假裝已卸載，也沒有在 c 之前卸載
    c.release_all()


# --- hung ---

def test_hung_calls_over_the_limit_alarm_and_stop_new_loads_until_they_return(rig):
    r = rig(workers=2)
    blocked = []
    for name in ("m1", "m2"):
        h = r.add(name, autoload=True)
        h.block("load")
        blocked.append(h)
    r.add("m3")
    r.mc.start()
    for h in blocked:
        wait_until(h.entered["load"].is_set)
    step(r, 31)
    status = r.mc.pool_status()
    assert status["hung"] == 2 and status["saturated"] and status["max_hung"] == 1
    assert r.mc.counters["hung_pool_alarm"] == 1 and r.mc.counters["hung_call"] == 2
    res = r.mc.request_load("m3")
    assert not res.accepted and res.reason == "rejected:worker_pool_saturated"
    for h in blocked:
        h.release_all()
    wait_until(lambda: r.mc.pool_status()["hung"] == 0)
    assert r.mc.request_load("m3").accepted
    r.until("m3", "ready")
    assert r.mc.counters["hung_pool_alarm"] == 1  # 解除後恢復，不重複告警


def test_job_that_timed_out_in_queue_never_calls_the_handle(rig):
    r = rig(workers=1)
    h1 = r.add("m1", autoload=True)
    h2 = r.add("m2", autoload=True)
    h1.block("load")
    r.mc.start()
    wait_until(h1.entered["load"].is_set)
    step(r, 31)  # m1 在 worker 上卡住（hung）；m2 還在佇列就逾時
    assert r.state("m1") == "failed" and r.state("m2") == "failed"
    assert r.mc.counters["hung_call"] == 1 and r.mc.counters["queued_timeout"] == 1
    h1.release_all()
    wait_until(lambda: not r.mc.status("m2").in_flight and not r.mc.status("m1").in_flight)
    assert h2.calls == []  # 工作開始前就被丟棄：不會事後才真的 load
    assert r.mc.counters["stale_before_start"] == 1
    assert r.mc.pool_status()["hung"] == 0


def test_probes_pause_while_the_pool_is_saturated(rig):
    r = rig(workers=2, probe_interval_s=5.0)
    h1 = r.add("m1", autoload=True)
    r.add("m2", autoload=True)
    r.add("ok", autoload=True)
    r.mc.start()
    settle(r)
    h1.block("probe")
    step(r, 6)
    wait_until(h1.entered["probe"].is_set)
    step(r, 31)
    assert r.mc.pool_status()["hung"] >= 1
    ok_probes = r.handles["ok"].calls.count("probe")
    step(r, 6)
    assert r.handles["ok"].calls.count("probe") == ok_probes  # 飽和時不再新增探活
    h1.release_all()


def test_constructor_validation():
    with pytest.raises(ValueError):
        ModuleCoordinator(lambda *_: None, epoch_source=lambda: 1, max_hung_calls=0)
    with pytest.raises(ValueError):
        ModuleCoordinator(lambda *_: None, epoch_source=lambda: 1, lease_ttl_s=10, lease_max_ttl_s=5)


def test_descriptor_resource_fields_validate():
    assert ModuleDescriptor("x").pinned is False and ModuleDescriptor("x").uses_gpu is False
    with pytest.raises(ValueError):
        ModuleDescriptor("x", vram_mb=100)  # 宣告成本卻沒宣告 uses_gpu
    with pytest.raises(ValueError):
        ModuleDescriptor("x", uses_gpu=True, vram_mb=-1)
    with pytest.raises(ValueError):
        ModuleDescriptor("x", pinned="yes")  # type: ignore[arg-type]
    assert ModuleDescriptor("x", uses_gpu=True).vram_mb is None
