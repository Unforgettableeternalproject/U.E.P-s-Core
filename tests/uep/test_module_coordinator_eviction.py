"""Module Coordinator PR-3 後段：需求驅動驅逐、閒置 TTL、heavy 序列化、宣告 vs 觀測校正、卸載後釋放驗證（離線，fake handle＋模擬量測）。

不需 DB、不碰 GPU：VRAM 量測一律走 ``FakeProbe``；``SimHandle`` 在 load／unload 時改動 ``probe.free``，模擬真實的占用與釋放。
時間用假時鐘＋手動 ``tick()``，只輪詢背景 worker 的結果（有時限）。
"""
from __future__ import annotations

import logging
import random
import threading
import time
from typing import Any

import pytest

from tests.uep.module_fakes import FakeHandle
from tests.uep.test_module_coordinator import Rig, wait_until
from tests.uep.test_module_coordinator_resources import (
    FakeProbe,
    gpu,
    make_budget,
    settle,
    step,
)
from uep.lifecycle.budget import UNLOAD_VERIFY_MIN_MB
from uep.lifecycle.handles import ObservedResource, ResourceObservable

CAP = 12000  # 顯式預算上限（讓放行由預算側決定，量測側寬鬆）


class SimHandle(FakeHandle):
    """載入時占用 ``probe.free``、卸載時釋放 ``release`` 比例；可回報觀測用量、載入耗時（推進假時鐘）。"""

    def __init__(
        self, probe: FakeProbe, mb: int, *, release: float = 1.0, observed: int | None = None, clock: Any = None,
        load_s: float = 0.0, log: list[tuple[str, str]] | None = None, name: str = "",
    ) -> None:
        super().__init__()
        self.gpu, self.mb, self.release, self.observed = probe, mb, release, observed
        self.clock, self.load_s, self.log, self.name = clock, load_s, log, name
        self.observe_error: BaseException | None = None

    def load(self) -> None:
        if self.log is not None:
            self.log.append(("load_start", self.name))
        super().load()
        if self.clock is not None and self.load_s:
            self.clock.advance(self.load_s)
        self.gpu.free -= self.mb
        if self.log is not None:
            self.log.append(("load_done", self.name))

    def unload(self) -> None:
        if self.log is not None:
            self.log.append(("unload_start", self.name))
        super().unload()
        self.gpu.free += int(self.mb * self.release)
        if self.log is not None:
            self.log.append(("unload_done", self.name))

    def observed_resource(self) -> ObservedResource | None:
        if self.observe_error is not None:
            raise self.observe_error
        return None if self.observed is None else ObservedResource(self.observed)


class Env:
    """一組 rig＋probe＋共用呼叫記錄，與建立模組的捷徑。"""

    def __init__(self, make: Any, *, budget_mb: int | None = CAP, free: int = 24000, ratio: float = 0.5, **kw: Any) -> None:
        self.probe = FakeProbe(total=24000, free=free)
        self.log: list[tuple[str, str]] = []
        self.r: Rig = make(budget=make_budget(self.probe, budget_mb=budget_mb, unload_release_ratio=ratio), **kw)

    def add(self, mid: str, mb: int | None = 6000, *deps: str, priority: str = "on_demand", handle_kw: dict[str, Any] | None = None, **desc: Any) -> SimHandle:
        h = SimHandle(self.probe, mb or 0, clock=self.r.clock, log=self.log, name=mid, **(handle_kw or {}))
        gpu(self.r, mid, mb, *deps, handle=h, priority=priority, **desc)
        return h

    def load(self, *mids: str, advance: float = 1.0) -> None:
        """依序載入（每個之間推進時鐘，讓 last_used 有先後）；呼叫前 coordinator 必須已啟動。"""
        for mid in mids:
            res = self.r.mc.request_load(mid)
            assert res.accepted, res
            self.r.until(mid, "ready")
            step(self.r, advance)

    def evicted(self) -> list[str]:
        return [e["module_id"] for e in self.r.rec.events if e["state"] == "unloading" and e["cause"] == "eviction"]


@pytest.fixture
def make():
    made: list[Rig] = []

    def _make(**kw: Any) -> Rig:
        r = Rig(**kw)
        made.append(r)
        return r

    yield _make
    for r in made:
        r.close()


@pytest.fixture
def env(make):
    def _env(**kw: Any) -> Env:
        return Env(make, **kw)

    return _env


# --- 規則 3～5：需求驅動驅逐 ---

def test_demand_load_evicts_lower_class_waits_for_release_then_loads(env):
    e = env()
    e.add("s", 6000, priority="speculative")
    e.add("b", 6000, priority="background")
    e.add("d", 6000)
    r = e.r
    r.mc.start()
    e.load("s", "b")
    res = r.mc.request_load("d")  # 空間不足：先驅逐 speculative（P2 先），登記預留，非同步續作
    assert res.accepted and res.reason in ("evicting", "accepted")
    r.until("d", "ready")
    assert e.evicted() == ["s"] and r.state("b") == "ready" and r.state("s") == "unloaded"
    # 原載入要等候選卸載完成並確認釋放才放行
    order = [x for x in e.log if x[1] in ("s", "d")]
    assert order.index(("unload_done", "s")) < order.index(("load_start", "d"))
    assert r.rec.states("d") == ["unloaded", "loading", "ready"]
    assert r.mc.status("d").waiting_eviction is False and r.mc.counters["evicted_for_demand"] == 1
    sev = next(x for x in r.rec.of("s") if x["state"] == "unloading")
    assert sev["cause"] == "eviction" and sev["reason_code"] == "evicted_demand"
    # 被驅逐者在冷卻內不得重載（與壓力收回相同的 evict_hold）
    assert r.mc.request_load("s").reason == "rejected:evict_hold"


def test_background_is_evicted_after_speculative_and_only_as_much_as_needed(env):
    e = env(budget_mb=18000, free=24000)
    e.add("s", 6000, priority="speculative")
    e.add("b", 6000, priority="background")
    e.add("d1", 6000)
    e.add("d2", 12000)
    e.r.mc.start()
    e.load("s", "b", "d1")
    assert e.r.mc.request_load("d2").accepted  # 缺 6000 + 6000：兩個候選都要
    e.r.until("d2", "ready")
    assert e.evicted() == ["s", "b"] and e.r.state("d1") == "ready"


def test_eviction_is_all_or_nothing_when_candidates_cannot_cover_the_shortfall(env):
    e = env()
    e.add("s", 3000, priority="speculative")
    e.add("pin", 9000, priority="speculative", pinned=True)
    e.add("d", 6000)
    e.r.mc.start()
    e.load("pin", "s")
    res = e.r.mc.request_load("d")  # 缺口 6000，可驅逐的只有 s(3000)：湊不夠就一個都不動
    assert not res.accepted and res.reason == "rejected:insufficient_resource"
    assert e.r.state("s") == "ready" and e.evicted() == []
    assert e.r.mc.status("d").admission_reason == "insufficient_resource"
    assert e.r.handles["d"].calls == []


def test_eviction_still_insufficient_after_release_is_rejected_without_queueing(env):
    e = env(ratio=0.0)  # 不驗證釋放：只靠新樣本確認
    e.add("s", 6000, priority="speculative", handle_kw={"release": 0.0})  # 卸載後 VRAM 其實沒還
    e.add("d", 6000)
    e.add("filler", 6000, priority="speculative", pinned=True)
    e.r.mc.start()
    e.load("s", "filler")
    # 預算側已用 12000；擠掉 s 後預算側夠，但實測側（free=24000-12000=12000，水位 2000）仍夠？改用實測側不夠的情境
    e.probe.free = 7000  # 外部占用讓實測側只剩 5000 < 6000；s 卸載後 free 仍不變（release=0）
    step(e.r, 1)
    res = e.r.mc.request_load("d")
    assert res.accepted and res.reason == "evicting"
    e.r.until("s", "unloaded")
    step(e.r, 1)  # 新樣本確認：沒有釋放 -> 仍不足 -> 被拒，不排隊
    wait_until(lambda: e.r.mc.status("d").admission_reason == "insufficient_resource")
    st = e.r.mc.status("d")
    assert st.state == "unloaded" and not st.waiting_eviction
    step(e.r, 100)
    settle(e.r)
    assert e.r.state("d") == "unloaded" and e.r.handles["d"].calls == []


def test_eviction_waits_for_a_sample_newer_than_the_unload_when_not_verifying(env):
    e = env(ratio=0.0)
    e.add("s", 6000, priority="speculative")
    e.add("b", 6000, priority="speculative", pinned=True)
    e.add("d", 6000)
    e.r.mc.start()
    e.load("s", "b")
    e.probe.free -= 5000  # 外部占用：舊樣本的實測側只剩 5000 < 6000，所以放行取決於「卸載之後的新樣本」
    step(e.r, 1)
    assert e.r.mc.request_load("d").accepted
    e.r.until("s", "unloaded")
    assert e.r.state("d") == "unloaded" and e.r.mc.status("d").waiting_eviction  # 還沒有比卸載更新的樣本
    step(e.r, 1)
    e.r.until("d", "ready")

    # 反例：預算側是唯一限制時，舊樣本已足以證明有空間（兩側取較保守者），不必多等一個樣本
    e2 = env(ratio=0.0)
    e2.add("s", 6000, priority="speculative")
    e2.add("b", 6000, priority="speculative", pinned=True)
    e2.add("d", 6000)
    e2.r.mc.start()
    e2.load("s", "b")
    assert e2.r.mc.request_load("d").accepted
    e2.r.until("d", "ready")


@pytest.mark.parametrize(
    "case, specs, expected",
    [
        # 類別優先：speculative 先於 background，即使 background 更久沒用
        ("class_first", [("b_old", "background", 0, 0), ("s_new", "speculative", 5, 0)], "s_new"),
        # 同類別：last_used 最舊者先（id 排序在後也一樣）
        ("oldest_first", [("s_z_old", "speculative", 0, 0), ("s_a_new", "speculative", 5, 0)], "s_z_old"),
        # last_used 相同：warm_p50 小者先（重載成本低先被驅逐）
        ("cheap_reload_first", [("s_a", "speculative", 0, 9.0), ("s_b", "speculative", 0, 1.0)], "s_b"),
        # 全部相同：module_id 決定（確定性）
        ("id_tiebreak", [("s_b", "speculative", 0, 0), ("s_a", "speculative", 0, 0)], "s_a"),
    ],
)
def test_victim_ordering_is_deterministic(env, case, specs, expected):
    e = env(budget_mb=12000)
    for mid, prio, _, load_s in specs:
        e.add(mid, 6000, priority=prio, handle_kw={"load_s": load_s})
    e.add("d", 6000)
    r = e.r
    r.mc.start()
    for mid, _, _, _ in specs:  # 載入順序固定為宣告順序，載入耗時推進假時鐘（形成 warm_p50）
        e.load(mid, advance=0.0)
    # 統一用 lease 觸碰把 last_used 設為指定的相對時間，不受載入先後影響
    base = r.clock.now
    for mid, _, at, _ in sorted(specs, key=lambda s: s[2]):
        r.clock.now = base + at
        lease = r.mc.acquire_lease(mid, "t")
        assert lease.granted and r.mc.release_lease(lease.lease_id)
    r.clock.now = base + 10
    step(r, 0)
    assert r.mc.request_load("d").accepted
    r.until("d", "ready")
    assert e.evicted() == [expected], case


def test_protected_modules_block_eviction_even_when_everything_else_is_evictable(env):
    e = env(budget_mb=12000)
    e.add("s_pin", 3000, priority="speculative", pinned=True)
    e.add("s_lease", 3000, priority="speculative")
    e.add("s_op", 3000, priority="speculative")
    e.add("s_base", 3000, priority="speculative")
    e.add("d", 3000)
    r = e.r
    r.add("child", "s_base")  # 不佔 VRAM 的依賴者：s_base 因此受保護
    r.mc.start()
    e.load("s_pin", "s_lease", "s_op", "s_base")
    assert r.mc.request_load("child").accepted
    r.until("child", "ready")
    lease = r.mc.acquire_lease("s_lease", "run-1")
    assert lease.granted and r.mc.pin("s_op").accepted
    step(r, 1)
    assert {m: r.mc.status(m).protected for m in ("s_pin", "s_lease", "s_op", "s_base")} == {
        "s_pin": "pinned", "s_lease": "lease", "s_op": "pinned", "s_base": "has_dependents",
    }
    res = r.mc.request_load("d")  # 預算已滿，唯一的候選全被保護
    assert not res.accepted and res.reason == "rejected:insufficient_resource"
    assert e.evicted() == [] and all(r.state(m) == "ready" for m in ("s_pin", "s_lease", "s_op", "s_base"))
    # 保護解除後才可驅逐：釋放 lease、解除 pin
    r.mc.release_lease(lease.lease_id)
    r.mc.unpin("s_op")
    step(r, 1)
    assert r.mc.request_load("d").accepted
    r.until("d", "ready")
    assert e.evicted() == ["s_op"]  # 兩個都可驅逐時取 last_used 較舊者（s_lease 剛被觸碰過）；s_pin／s_base 仍受保護


def test_requesters_own_dependencies_are_never_evicted(env):
    e = env(budget_mb=12000)
    e.add("base", 6000, priority="speculative")
    e.add("other", 6000, priority="speculative")
    e.add("d", 6000, "base")
    e.r.mc.start()
    e.load("base", "other")  # base 最舊，但它是 d 的依賴
    assert e.r.mc.request_load("d").accepted
    e.r.until("d", "ready")
    assert e.evicted() == ["other"] and e.r.state("base") == "ready"


def test_modules_with_in_flight_operations_are_not_candidates(env):
    e = env(budget_mb=6000, probe_interval_s=1.0)
    h = e.add("s", 6000, priority="speculative")
    e.add("d", 6000)
    e.r.mc.start()
    e.load("s")
    gate = h.block("probe")
    step(e.r, 2)  # 到期探活：probe 卡在 worker 上（在途操作）
    wait_until(h.entered["probe"].is_set)
    assert e.r.mc.status("s").in_flight
    res = e.r.mc.request_load("d")
    assert not res.accepted and res.reason == "rejected:insufficient_resource" and e.evicted() == []
    gate.set()


# --- 規則 2：預載不得驅逐 ---

def test_speculative_background_and_non_demand_loads_never_evict(env):
    e = env(budget_mb=12000)
    e.add("b1", 6000, priority="background")
    e.add("b2", 6000, priority="background")
    e.add("sp", 6000, priority="speculative")
    e.add("od", 6000)
    e.add("od_boot", 6000, autoload=True)  # startup 自動載入也不是 demand
    e.r.mc.start()
    settle(e.r)
    e.load("b1")
    assert e.r.state("od_boot") == "ready"
    # 預算已滿（b1 + od_boot）：speculative／background 請求撿不到空位 -> 直接被拒，不驅逐任何人
    for mid in ("sp", "b2"):
        res = e.r.mc.request_load(mid)
        assert not res.accepted and res.reason == "rejected:insufficient_resource", mid
    # on_demand 但起因是預載提議（proposal）：同樣只撿空位
    res = e.r.mc.request_load("od", cause="proposal")
    assert not res.accepted and res.reason == "rejected:insufficient_resource"
    assert e.evicted() == [] and e.r.state("b1") == "ready" and e.r.state("od_boot") == "ready"
    # 同一個 on_demand 以 demand 起因才會驅逐（b1 是較低類別）
    assert e.r.mc.request_load("od").accepted
    e.r.until("od", "ready")
    assert e.evicted() == ["b1"]


def test_resident_and_on_demand_are_never_victims_of_equal_or_lower_requesters(env):
    e = env(budget_mb=6000)
    e.add("res", 6000, priority="resident", autoload=True)
    e.add("od2", 6000)
    e.r.mc.start()
    settle(e.r)
    assert e.r.state("res") == "ready"
    res = e.r.mc.request_load("od2")  # resident 不在可驅逐類別
    assert not res.accepted and res.reason == "rejected:insufficient_resource"
    assert e.evicted() == []


# --- 兩個載入搶空間 ---

def test_two_demand_loads_cannot_take_the_same_freed_space(env):
    e = env(budget_mb=9000)
    h = e.add("s", 6000, priority="speculative")
    e.add("d1", 6000)
    e.add("d2", 6000)
    e.r.mc.start()
    e.load("s")
    gate = h.block("unload")
    first = e.r.mc.request_load("d1")
    assert first.accepted
    wait_until(h.entered["unload"].is_set)  # s 正在卸載中，空間還沒騰出
    second = e.r.mc.request_load("d2")  # 同一塊空間已被 d1 預留，也沒有其他候選
    assert not second.accepted and second.reason == "rejected:insufficient_resource"
    gate.set()
    e.r.until("d1", "ready")
    assert e.r.state("d2") == "unloaded" and e.r.handles["d2"].calls == []
    assert e.evicted() == ["s"]


def test_claim_blocks_other_loads_after_the_victim_is_gone_but_before_the_claimer_loads(env):
    e = env(budget_mb=9000, ratio=0.0)
    e.add("s", 6000, priority="speculative")
    e.add("d1", 6000)
    e.add("sp2", 3200, priority="speculative")
    e.r.mc.start()
    e.load("s")
    e.probe.free -= 12500  # 外部占用讓舊樣本的實測側不足：d1 要等卸載後的新樣本，這段期間 s 騰出的空間必須被預留
    step(e.r, 1)
    assert e.r.mc.request_load("d1").accepted
    e.r.until("s", "unloaded")  # 沒有新樣本：d1 還在等；s 的空間已預留給 d1
    assert e.r.mc.status("d1").waiting_eviction
    res = e.r.mc.request_load("sp2")  # 預載只撿空位：預算 9000 - 預留 6000 = 3000 < 3200，不能偷（沒有預留的話舊樣本的實測側 3500 會放行它）
    assert not res.accepted and res.reason == "rejected:insufficient_resource"
    step(e.r, 1)
    e.r.until("d1", "ready")  # 預留沒被偷走：d1 仍然載入成功
    assert e.r.state("sp2") == "unloaded"


def test_two_victims_can_serve_two_requesters(env):
    e = env(budget_mb=12000)
    e.add("s1", 6000, priority="speculative")
    e.add("s2", 6000, priority="speculative")
    e.add("d1", 6000)
    e.add("d2", 6000)
    e.r.mc.start()
    e.load("s1", "s2")
    assert e.r.mc.request_load("d1").accepted
    assert e.r.mc.request_load("d2").accepted  # d1 的預留由 s1 覆蓋，d2 能再挑 s2
    e.r.until("d1", "ready")
    e.r.until("d2", "ready")
    assert sorted(e.evicted()) == ["s1", "s2"]
    held = sum(6000 for m in ("s1", "s2", "d1", "d2") if e.r.state(m) in ("ready", "loading", "unloading"))
    assert held <= 12000


# --- 規則 6：閒置 TTL ---

def test_idle_ttl_unloads_speculative_and_on_demand_with_a_recognisable_reason(env):
    e = env(budget_mb=30000)
    e.add("sp", 1000, priority="speculative", idle_ttl_s=100)
    e.add("od", 1000, priority="on_demand", idle_ttl_s=100)
    e.add("no_ttl", 1000, priority="speculative")
    e.add("bg", 1000, priority="background", idle_ttl_s=100)  # 不在 TTL 類別（P3 由壓力收回）
    r = e.r
    r.mc.start()
    e.load("sp", "od", "no_ttl", "bg", advance=0.0)
    step(r, 99)
    assert all(r.state(m) == "ready" for m in ("sp", "od", "no_ttl", "bg"))
    step(r, 2)
    wait_until(lambda: r.state("sp") == "unloaded" and r.state("od") == "unloaded")
    ev = next(x for x in r.rec.of("sp") if x["state"] == "unloading")
    assert ev["cause"] == "idle" and ev["reason_code"] == "idle_ttl"
    assert r.state("no_ttl") == "ready" and r.state("bg") == "ready"
    step(r, 500)  # 不會自動重載
    settle(r)
    assert r.state("sp") == "unloaded" and r.mc.counters["idle_ttl_unload"] == 2


def test_idle_ttl_spares_pinned_resident_leased_and_depended_modules(env):
    e = env(budget_mb=30000)
    e.add("pinned", 1000, priority="speculative", idle_ttl_s=10, pinned=True)
    e.add("resident", 1000, priority="resident", idle_ttl_s=10, autoload=True)
    e.add("leased", 1000, priority="speculative", idle_ttl_s=10)
    e.add("base", 1000, priority="speculative", idle_ttl_s=10)
    e.add("child", 1000, "base", priority="on_demand")
    e.add("op", 1000, priority="speculative", idle_ttl_s=10)
    r = e.r
    r.mc.start()
    settle(r)
    e.load("pinned", "leased", "base", "child", "op", advance=0.0)
    lease = r.mc.acquire_lease("leased", "run", ttl_s=3000)
    assert lease.granted and r.mc.pin("op").accepted
    step(r, 100)
    settle(r)
    assert all(r.state(m) == "ready" for m in ("pinned", "resident", "leased", "base", "child", "op"))
    assert r.mc.counters.get("idle_ttl_unload", 0) == 0
    # 保護解除後立即計入閒置：釋放 lease 才開始計時（不是當場被卸載）
    r.mc.release_lease(lease.lease_id)
    step(r, 1)
    assert r.state("leased") == "ready"
    step(r, 20)
    wait_until(lambda: r.state("leased") == "unloaded")
    assert r.state("pinned") == "ready" and r.state("resident") == "ready"


def test_idle_ttl_is_inert_without_a_budget(make):
    r = make()
    r.add("svc", priority="speculative", idle_ttl_s=1, autoload=True)
    r.mc.start()
    settle(r)
    step(r, 100)
    settle(r)
    assert r.state("svc") == "ready"


# --- heavy 全域序列化 ---

def test_heavy_loads_are_serialised_light_loads_are_not(env):
    e = env(budget_mb=30000)
    h1 = e.add("h1", 1000, load_cost="heavy")
    h2 = e.add("h2", 1000, load_cost="heavy")
    l1 = e.add("l1", 1000, load_cost="light")
    r = e.r
    gate = h1.block("load")
    r.mc.start()
    assert r.mc.request_load("h1").accepted
    wait_until(h1.entered["load"].is_set)
    blocked = r.mc.request_load("h2")  # 被拒不排隊
    assert not blocked.accepted and blocked.reason == "rejected:heavy_load_in_flight"
    assert r.mc.status("h2").admission_reason == "heavy_load_in_flight"
    assert r.mc.request_load("l1").accepted  # light 不受限
    r.until("l1", "ready")
    assert r.state("h2") == "unloaded" and h2.calls == []
    gate.set()
    r.until("h1", "ready")
    step(r, 100)
    settle(r)
    assert r.state("h2") == "unloaded"  # 沒有排隊
    assert r.mc.request_load("h2").accepted
    r.until("h2", "ready")
    assert l1.calls == ["load"]


def test_heavy_autoload_modules_load_one_after_another(env):
    e = env(budget_mb=30000)
    running = {"now": 0, "max": 0}

    class Tracker(SimHandle):
        def load(self) -> None:
            running["now"] += 1
            running["max"] = max(running["max"], running["now"])
            try:
                time.sleep(0.05)  # 拉長載入，沒有序列化時一定會重疊
                super().load()
            finally:
                running["now"] -= 1

    for mid in ("h1", "h2", "h3"):
        h = Tracker(e.probe, 1000, name=mid)
        gpu(e.r, mid, 1000, handle=h, load_cost="heavy", autoload=True)
    e.r.mc.start()
    settle(e.r)
    assert all(e.r.state(m) == "ready" for m in ("h1", "h2", "h3"))
    assert running["max"] == 1


def test_heavy_is_not_serialised_when_budget_is_not_enabled(make):
    r = make()
    h1 = r.add("h1", load_cost="heavy")
    h2 = r.add("h2", load_cost="heavy")
    g1, g2 = h1.block("load"), h2.block("load")
    r.mc.start()
    assert r.mc.request_load("h1").accepted and r.mc.request_load("h2").accepted
    wait_until(lambda: h1.entered["load"].is_set() and h2.entered["load"].is_set())  # 預算未啟用：行為不變
    g1.set()
    g2.set()


# --- 宣告值 vs 觀測值 ---

def test_observed_deviation_over_25_percent_warns_records_and_admission_uses_the_max(env, caplog):
    e = env(budget_mb=17000)
    h = e.add("g1", 8000, handle_kw={"observed": 11000})
    e.add("g2", 8000)
    assert isinstance(h, ResourceObservable)
    e.r.mc.start()
    with caplog.at_level(logging.WARNING, logger="uep.lifecycle"):
        e.load("g1")
    recs = [x for x in caplog.records if getattr(x, "event", None) == "vram_deviation"]
    assert len(recs) == 1 and recs[0].module_id == "g1" and "declared_mb=8000" in recs[0].getMessage()
    assert e.r.mc.status("g1").observed_vram_mb == 11000 and e.r.mc.counters["vram_deviation"] == 1
    # 以宣告值算 17000-8000=9000 夠 g2；以 max(宣告, 觀測)=11000 算只剩 6000 -> 被拒
    res = e.r.mc.request_load("g2")
    assert not res.accepted and res.reason == "rejected:insufficient_resource"


def test_observed_within_tolerance_or_below_declared_does_not_shrink_the_cost(env, caplog):
    e = env(budget_mb=17000)
    e.add("near", 8000, handle_kw={"observed": 9000})  # 12.5%：不警告，但記錄，之後以 9000 計
    e.add("low", 2000, handle_kw={"observed": 500})  # 偏差 75% 警告，但成本仍是宣告 2000（max）
    e.add("none", 1000)  # 沒回報
    e.add("broken", 1000)
    e.add("x4000", 4000)
    e.add("x4001", 4001)
    e.r.handles["broken"].observe_error = RuntimeError("boom")
    e.r.mc.start()
    with caplog.at_level(logging.WARNING, logger="uep.lifecycle"):
        e.load("near", "low", "none", "broken")
    assert e.r.mc.counters["vram_deviation"] == 1
    assert e.r.mc.status("near").observed_vram_mb == 9000 and e.r.mc.status("low").observed_vram_mb == 500
    assert e.r.mc.status("none").observed_vram_mb is None and e.r.mc.status("broken").observed_vram_mb is None
    # 持有量 = near 9000（觀測較大）+ low 2000（宣告較大）+ 1000 + 1000 = 13000，剩 4000
    assert e.r.mc.request_load("x4000").accepted
    e.r.until("x4000", "ready")
    res = e.r.mc.request_load("x4001")
    assert not res.accepted and res.reason == "rejected:insufficient_resource"


def test_observed_value_is_read_after_warm_for_needs_warm_modules(env):
    e = env(budget_mb=30000)
    h = e.add("g", 4000, needs_warm=True, handle_kw={"observed": 8000})
    e.r.mc.start()
    e.load("g")
    assert h.calls == ["load", "warm"] and e.r.mc.status("g").observed_vram_mb == 8000


# --- 卸載後釋放量驗證（unload_incomplete）---

def test_unload_incomplete_keeps_the_unreleased_amount_until_a_later_sample_shows_release(env, caplog):
    e = env(budget_mb=10000)
    e.add("g1", 8000, handle_kw={"release": 0.2})  # 卸載後只還了 1600
    e.add("g2", 8000)
    e.r.mc.start()
    e.load("g1")
    with caplog.at_level(logging.WARNING, logger="uep.lifecycle"):
        assert e.r.mc.request_unload("g1").accepted
        wait_until(lambda: e.r.state("g1") == "unloaded")
    assert e.r.mc.counters["unload_incomplete"] == 1
    assert any(getattr(x, "event", None) == "unload_incomplete" for x in caplog.records)
    assert e.r.mc.status("g1").unreleased_mb == 6400
    step(e.r, 1)
    res = e.r.mc.request_load("g2")  # 預算保守保留 6400：10000-6400=3600 < 8000
    assert not res.accepted and res.reason == "rejected:insufficient_resource"
    step(e.r, 1)
    assert e.r.mc.status("g1").unreleased_mb == 6400  # 樣本沒有改善：維持
    e.probe.free += 6400  # 之後釋放（例如 allocator 還回去）
    step(e.r, 1)
    assert e.r.mc.status("g1").unreleased_mb == 0 and e.r.mc.counters["unload_recovered"] == 1
    assert e.r.mc.request_load("g2").accepted
    e.r.until("g2", "ready")


def test_full_release_small_modules_and_disabled_verification_do_not_flag(env):
    e = env(budget_mb=30000)
    e.add("full", 8000)
    e.add("tiny", UNLOAD_VERIFY_MIN_MB - 1, handle_kw={"release": 0.0})  # 量不出差異的小模組不判定
    e.r.mc.start()
    e.load("full", "tiny")
    for mid in ("full", "tiny"):
        assert e.r.mc.request_unload(mid).accepted
        wait_until(lambda m=mid: e.r.state(m) == "unloaded")
    assert e.r.mc.counters.get("unload_incomplete", 0) == 0
    assert e.r.mc.status("full").unreleased_mb == 0

    off = env(budget_mb=30000, ratio=0.0)  # 預設關閉：不取樣、不判定
    off.add("g", 8000, handle_kw={"release": 0.0})
    off.r.mc.start()
    off.load("g")
    reads = off.probe.reads
    assert off.r.mc.request_unload("g").accepted
    wait_until(lambda: off.r.state("g") == "unloaded")
    assert off.probe.reads == reads and off.r.mc.counters.get("unload_incomplete", 0) == 0


def test_unload_verification_is_not_judged_when_measurement_is_unavailable(env):
    e = env(budget_mb=10000)
    e.add("g1", 8000, handle_kw={"release": 0.0})
    e.add("g2", 8000)
    e.r.mc.start()
    e.load("g1")
    e.probe.error = RuntimeError("nvml down")
    assert e.r.mc.request_unload("g1").accepted
    wait_until(lambda: e.r.state("g1") == "unloaded")
    assert e.r.mc.counters.get("unload_incomplete", 0) == 0  # 量不到就不判定
    step(e.r, 1)
    assert e.r.mc.request_load("g2").reason == "rejected:measurement_failed"  # 但放行仍 fail-closed（保守）
    e.probe.error = None
    step(e.r, 1)
    assert e.r.mc.request_load("g2").accepted


def test_concurrent_gpu_operations_pollute_the_delta_so_the_unload_is_not_judged(env):
    e = env(budget_mb=30000)
    a = e.add("a", 6000, priority="speculative")
    e.add("x", 6000)
    e.r.mc.start()
    e.load("a")
    gate = a.block("unload")
    assert e.r.mc.request_unload("a").accepted
    wait_until(a.entered["unload"].is_set)
    assert e.r.mc.request_load("x").accepted  # a 卸載期間 x 載入並占走 6000：差值 = 0，但不是 a 沒釋放
    e.r.until("x", "ready")
    gate.set()
    wait_until(lambda: e.r.state("a") == "unloaded")
    assert e.r.mc.counters.get("unload_incomplete", 0) == 0 and e.r.mc.counters["unload_unverified_concurrent"] == 1
    assert e.r.mc.status("a").unreleased_mb == 0


def test_demand_eviction_whose_victim_did_not_really_release_is_rejected(env):
    e = env(budget_mb=12000)
    e.add("s", 6000, priority="speculative", handle_kw={"release": 0.0})
    e.add("b", 6000, priority="background")
    e.add("d", 6000)
    e.r.mc.start()
    e.load("s", "b")
    assert e.r.mc.request_load("d").accepted
    wait_until(lambda: e.r.mc.status("d").admission_reason == "insufficient_resource")
    assert e.r.state("s") == "unloaded" and e.r.mc.status("s").unreleased_mb == 6000
    assert e.r.state("d") == "unloaded" and e.r.handles["d"].calls == [] and e.r.mc.counters["unload_incomplete"] == 1
    assert not e.r.mc.status("d").waiting_eviction


# --- 不變式（固定種子隨機序列）---

def _protected_before(r: Rig, mids: list[str]) -> dict[str, str | None]:
    return {m: r.mc.status(m).protected for m in mids}


def test_random_sequences_never_evict_protected_modules_and_preloads_never_evict(env):
    """永不驅逐 pinned／lease／P0／有依賴者；預載（非 demand／speculative／background）永不驅逐；持有量不超過預算。"""
    prios = ("on_demand", "speculative", "background")
    total_evicted = 0
    for seed in range(8):
        rnd = random.Random(seed)
        e = env(budget_mb=CAP, ratio=0.5, lease_ttl_s=1e6, lease_max_ttl_s=1e6, workers=6, max_hung_calls=5)
        costs: dict[str, int] = {}
        prio_of: dict[str, str] = {}
        for i in range(7):
            mid = f"m{i}"
            costs[mid] = rnd.choice((2000, 3000, 4000))
            prio_of[mid] = rnd.choice(prios)
            e.add(mid, costs[mid], priority=prio_of[mid], pinned=(i == 0))
        e.add("res", 1000, priority="resident", autoload=True)
        costs["res"] = 1000
        mids = [m for m in costs if m != "res"]
        r = e.r
        r.mc.start()
        settle(r)
        leases: list[str] = []
        seen = 0
        demand_requested = False
        for phase in ("preload", "demand"):
            for _ in range(45):
                op = rnd.choice(("load",) * 5 + ("lease", "release", "pin", "unpin", "unload", "tick"))
                target = rnd.choice(mids)
                before = _protected_before(r, list(costs))
                is_preload_only = phase == "preload"
                if op == "load":
                    cause = "proposal" if is_preload_only else rnd.choice(("demand", "proposal"))
                    if cause == "demand" and prio_of[target] == "on_demand":
                        demand_requested = True
                    r.mc.request_load(target, cause=cause)
                elif op == "lease":
                    res = r.mc.acquire_lease(target, f"run-{rnd.randrange(3)}")
                    if res.granted and res.lease_id:
                        leases.append(res.lease_id)
                elif op == "release" and leases:
                    r.mc.release_lease(leases.pop(rnd.randrange(len(leases))))
                elif op == "pin":
                    r.mc.pin(target)
                elif op == "unpin":
                    r.mc.unpin(target)
                elif op == "unload":
                    r.mc.request_unload(target)
                step(r, rnd.choice((0.5, 1, 7)))
                settle(r)
                new = r.rec.events[seen:]
                seen = len(r.rec.events)
                for ev in new:
                    if ev["state"] == "unloading" and ev["cause"] in ("eviction", "pressure", "idle"):
                        mid = ev["module_id"]
                        assert before[mid] is None, (seed, phase, op, mid, before[mid])  # 不驅逐 pinned／lease／P0／有依賴者
                        assert prio_of.get(mid) in ("speculative", "background"), (seed, mid)
                    if ev["cause"] == "eviction" and ev["state"] == "unloading":
                        assert phase == "demand" and demand_requested, (seed, phase, ev)  # 預載階段永不驅逐
                        total_evicted += 1
                held = sum(
                    costs[m] for m in costs
                    if r.state(m) in ("loading", "warming", "ready", "degraded", "unloading") or r.mc.status(m).occupied
                )
                assert held <= CAP, (seed, phase, op, held)
                assert r.state("res") == "ready" and r.state("m0") != "unloading"
    assert total_evicted > 0  # 不是空轉：隨機序列確實觸發過需求驅逐


# --- 審查補強：預留生命週期 ---

def test_requesters_dependency_is_protected_from_other_requesters(env):
    e = env(budget_mb=10000, ratio=0.0)
    hs = e.add("s", 5000, priority="speculative")
    e.add("base", 3000, priority="speculative")
    e.add("d", 6000, "base")
    e.add("d2", 3000)
    r = e.r
    r.mc.start()
    e.load("s", "base")
    gate = hs.block("unload")
    assert r.mc.request_load("d").accepted  # 驅逐 s（base 是 d 的依賴）
    wait_until(hs.entered["unload"].is_set)
    res = r.mc.request_load("d2")  # 不能把 d 所依賴的 base 驅逐掉
    assert not res.accepted and res.reason == "rejected:insufficient_resource"
    assert r.state("base") == "ready" and e.evicted() == ["s"]
    gate.set()
    r.until("d", "ready")


def test_claim_is_dropped_when_the_requesters_dependency_stops_being_ready(env):
    e = env(budget_mb=10000, ratio=0.0)
    hs = e.add("s", 5000, priority="speculative")
    e.add("base", 3000, priority="speculative")
    e.add("d", 6000, "base")
    e.add("x", 5000, priority="speculative")
    r = e.r
    r.mc.start()
    e.load("s", "base")
    gate = hs.block("unload")
    assert r.mc.request_load("d").accepted
    wait_until(hs.entered["unload"].is_set)
    assert r.mc.status("d").waiting_eviction
    assert r.mc.request_unload("base").accepted  # d 還沒載入，base 沒有依賴者保護；依賴消失後 d 永遠開始不了
    step(r, 1)
    st = r.mc.status("d")
    assert not st.waiting_eviction and st.admission_reason == "dependency_not_ready" and r.mc.counters["evict_claim_dropped"] == 1
    gate.set()
    wait_until(lambda: r.state("s") == "unloaded")
    step(r, 1)
    assert r.mc.request_load("x").accepted  # 預留沒有永久洩漏
    r.until("x", "ready")
    step(r, 200)
    settle(r)
    assert r.state("d") == "unloaded"


def test_claim_expires_when_release_can_never_be_confirmed(env):
    e = env(budget_mb=9000, ratio=0.0)
    e.add("s", 6000, priority="speculative")
    e.add("d", 6000)
    r = e.r
    r.mc.start()
    e.load("s")
    e.probe.free -= 12500  # 舊樣本實測側不足：放行取決於卸載後的新樣本
    step(r, 1)
    assert r.mc.request_load("d").accepted
    r.until("s", "unloaded")
    assert r.mc.status("d").waiting_eviction
    e.probe.error = RuntimeError("nvml down")  # 之後再也量不到：新樣本永遠來不了
    step(r, 10)
    assert r.mc.status("d").waiting_eviction  # 量測失敗也先等（候選未確認釋放），不是立刻放棄
    step(r, 40)  # 超過 卸載逾時 + 樣本新鮮度上限 的期限
    st = r.mc.status("d")
    assert not st.waiting_eviction and st.admission_reason == "eviction_expired" and st.state == "unloaded"
    e.probe.error = None
    step(r, 1)
    step(r, 100)
    settle(r)
    assert r.state("d") == "unloaded"  # 放棄就是放棄，不排隊


def test_new_load_request_does_not_reuse_a_stale_claim_and_stop_releases_it(env):
    e = env(budget_mb=9000, ratio=0.0)
    hs = e.add("s", 6000, priority="speculative")
    e.add("d", 6000)
    r = e.r
    r.mc.start()
    e.load("s")
    gate = hs.block("unload")
    assert r.mc.request_load("d").accepted
    wait_until(hs.entered["unload"].is_set)
    assert r.mc.status("d").waiting_eviction
    assert r.mc.request_unload("d").accepted  # 請求者不要了：預留立即釋放
    assert not r.mc.status("d").waiting_eviction
    res = r.mc.request_load("d")  # 再次請求：重新評估（s 已在卸載中、沒有其他候選），不沿用舊名單回 evicting
    assert not res.accepted and res.reason == "rejected:insufficient_resource"
    gate.set()
    wait_until(lambda: r.state("s") == "unloaded")
    assert r.state("d") == "unloaded"

    e2 = env(budget_mb=9000, ratio=0.0)  # stop：等待中的預留一併清掉，不會在關閉途中載入
    h2 = e2.add("s", 6000, priority="speculative")
    e2.add("d", 6000)
    e2.r.mc.start()
    e2.load("s")
    g2 = h2.block("unload")
    assert e2.r.mc.request_load("d").accepted
    wait_until(h2.entered["unload"].is_set)
    g2.set()
    e2.r.mc.stop(timeout_s=2.0)
    assert not e2.r.mc.status("d").waiting_eviction and e2.r.handles["d"].calls == []


def test_measurement_failure_or_pressure_during_eviction_keeps_waiting_until_victims_settle(env):
    e = env(budget_mb=9000, ratio=0.0)
    h = e.add("s", 6000, priority="speculative")
    e.add("d", 6000)
    r = e.r
    r.mc.start()
    e.load("s")
    gate = h.block("unload")
    assert r.mc.request_load("d").accepted
    wait_until(h.entered["unload"].is_set)
    e.probe.error = RuntimeError("nvml down")
    step(r, 1)
    e.probe.error = None
    step(r, 1)
    assert r.mc.status("d").waiting_eviction  # 候選還在卸載：量測失敗不放棄請求
    gate.set()
    wait_until(lambda: r.state("s") == "unloaded")
    step(r, 1)
    r.until("d", "ready")

    p = env(budget_mb=9000, ratio=0.5)  # 壓力：候選卸載中遇到壓力仍等；結算後壓力還在才依最新樣本拒絕
    hp = p.add("s", 6000, priority="speculative")
    p.add("d", 6000)
    p.r.mc.start()
    p.load("s")
    gp = hp.block("unload")
    assert p.r.mc.request_load("d").accepted
    wait_until(hp.entered["unload"].is_set)
    p.probe.free = 500  # 外部壓力（低於保留水位）
    step(p.r, 1)
    assert p.r.mc.status("d").waiting_eviction
    gp.set()
    wait_until(lambda: p.r.state("s") == "unloaded")
    wait_until(lambda: p.r.mc.status("d").admission_reason == "pressure")
    assert not p.r.mc.status("d").waiting_eviction and p.r.state("d") == "unloaded"


def test_heavy_requester_waiting_for_eviction_is_not_overtaken_by_another_heavy(env):
    e = env(budget_mb=9000, ratio=0.0)
    hs = e.add("s", 6000, priority="speculative")
    e.add("d", 6000, load_cost="heavy")
    e.add("h2", 1000, load_cost="heavy")
    r = e.r
    r.mc.start()
    e.load("s")
    gate = hs.block("unload")
    assert r.mc.request_load("d").accepted
    wait_until(hs.entered["unload"].is_set)
    res = r.mc.request_load("h2")  # d 已在預留，別的 heavy 不得插隊（否則 d 之後會被 heavy_load_in_flight 拒絕）
    assert not res.accepted and res.reason == "rejected:heavy_load_in_flight"
    gate.set()
    r.until("d", "ready")


def test_hung_heavy_load_does_not_block_other_heavy_loads_forever(env):
    e = env(budget_mb=30000)
    h1 = e.add("h1", 1000, load_cost="heavy")
    e.add("h2", 1000, load_cost="heavy")
    r = e.r
    h1.block("load")
    r.mc.start()
    assert r.mc.request_load("h1").accepted
    wait_until(h1.entered["load"].is_set)
    assert r.mc.request_load("h2").reason == "rejected:heavy_load_in_flight"
    step(r, 31)  # h1 逾時：failed，handle 仍卡住（hung），占用留在預算
    assert r.state("h1") == "failed" and r.mc.status("h1").occupied
    assert r.mc.request_load("h2").accepted
    r.until("h2", "ready")


# --- 審查補強：候選卸載失敗、lease、觀測逾時、leak 恢復 ---

def test_victim_whose_unload_fails_is_retried_for_cleanup_even_though_it_is_not_wanted(env):
    e = env(budget_mb=12000)
    hs = e.add("s", 6000, priority="speculative")
    e.add("b", 6000, priority="speculative", pinned=True)
    e.add("d", 6000)
    r = e.r
    r.mc.start()
    e.load("s", "b")
    hs.fail["unload"] = RuntimeError("busy")
    assert r.mc.request_load("d").accepted
    r.until("s", "failed")
    wait_until(lambda: r.mc.status("d").admission_reason == "insufficient_resource")  # 沒釋放：被拒
    assert r.mc.status("s").occupied
    hs.fail.clear()
    step(r, 6)  # 退避到期：重試清理（不重載）
    r.until("s", "unloaded")
    assert not r.mc.status("s").occupied
    step(r, 100)
    settle(r)
    assert r.state("s") == "unloaded" and hs.calls.count("load") == 1


def test_lease_expiry_restarts_the_idle_clock(env):
    e = env(budget_mb=30000)
    e.add("sp", 1000, priority="speculative", idle_ttl_s=100)
    r = e.r
    r.mc.start()
    e.load("sp", advance=0.0)
    assert r.mc.acquire_lease("sp", "run", ttl_s=50).granted
    step(r, 120)  # lease 在 tick 內到期：閒置從到期起算，不是從 lease 取得時起算
    assert r.state("sp") == "ready"
    step(r, 101)
    wait_until(lambda: r.state("sp") == "unloaded")


def test_hanging_observed_resource_does_not_turn_a_successful_load_into_a_timeout(env):
    release = threading.Event()

    class Hanging(SimHandle):
        def observed_resource(self) -> ObservedResource | None:
            release.wait(timeout=5)
            return ObservedResource(1)

    e = env(budget_mb=30000)
    h = Hanging(e.probe, 1000, clock=e.r.clock, name="g")
    gpu(e.r, "g", 1000, handle=h)
    e.r.mc.start()
    try:
        assert e.r.mc.request_load("g").accepted
        e.r.until("g", "ready")
        assert e.r.mc.status("g").observed_vram_mb is None and e.r.mc.counters.get("load_timeout", 0) == 0
    finally:
        release.set()


def test_leak_recovery_discounts_other_modules_loads_and_unloads(env):
    e = env(budget_mb=20000)
    e.add("g1", 8000, handle_kw={"release": 0.2})
    e.add("g2", 6000)
    r = e.r
    r.mc.start()
    e.load("g1")
    assert r.mc.request_unload("g1").accepted
    wait_until(lambda: r.state("g1") == "unloaded")
    step(r, 1)
    assert r.mc.status("g1").unreleased_mb == 6400
    e.load("g2")  # 另一個模組載入造成的空閒下降不是 g1 的問題
    assert r.mc.status("g1").unreleased_mb == 6400  # 沒有膨脹到宣告值 8000
    assert r.mc.request_unload("g2").accepted
    wait_until(lambda: r.state("g2") == "unloaded")
    step(r, 1)
    assert r.mc.status("g1").unreleased_mb == 6400
    e.load("g2")
    e.probe.free += 6400  # g1 的未釋放量真的還回去（此時 g2 正占用著）
    step(r, 1)
    assert r.mc.status("g1").unreleased_mb == 0 and r.mc.counters["unload_recovered"] == 1


def test_operator_can_clear_unreleased_budget(env):
    e = env(budget_mb=10000)
    e.add("g1", 8000, handle_kw={"release": 0.0})
    e.add("g2", 8000)
    r = e.r
    r.mc.start()
    e.load("g1")
    assert r.mc.request_unload("g1").accepted
    wait_until(lambda: r.state("g1") == "unloaded")
    assert r.mc.status("g1").unreleased_mb == 8000
    assert r.mc.clear_unreleased("nope").reason == "unknown_module"
    assert r.mc.clear_unreleased("g1").reason == "cleared" and r.mc.clear_unreleased("g1").reason == "nothing_to_clear"
    step(r, 1)
    assert r.mc.request_load("g2").accepted


def test_request_load_drops_a_leftover_candidate_list_when_the_requester_was_not_waiting(env):
    from uep.lifecycle.coordinator import _EvictWait

    e = env(budget_mb=9000, ratio=0.0)
    e.add("s", 6000, priority="speculative")
    e.add("d", 6000)
    r = e.r
    r.mc.start()
    e.load("s")
    rec = r.mc._recs["d"]
    rec.evict_wait = _EvictWait(("s",), 6000, 1e12)  # 殘留的過期名單（wanted=False，沒有人在等）
    assert not rec.wanted
    assert r.mc.request_load("d").accepted
    assert e.evicted() == ["s"]  # 重新挑選並真的驅逐了 s，而不是直接沿用名單裝作已在等
    r.until("d", "ready")


def test_victim_cleanup_retry_does_not_need_its_dependencies_to_be_ready(env):
    e = env(budget_mb=12000)
    hb = e.add("base", 3000, priority="speculative")
    hs = e.add("s", 5000, "base", priority="speculative")
    e.add("d", 6000)
    r = e.r
    r.mc.start()
    e.load("base", "s")
    hs.fail["unload"] = RuntimeError("busy")
    hb.fail["unload"] = RuntimeError("busy")
    assert r.mc.request_load("d").accepted  # 只驅逐 s（base 被 s 依賴，受保護）
    r.until("s", "failed")
    # 讓依賴 base 進入非 ready（白箱：直接標為 failed）
    r.mc._recs["base"].state = "failed"
    hs.fail.clear()
    step(r, 6)
    r.until("s", "unloaded")  # 依賴不 ready 也能重試清理
    assert not r.mc.status("s").occupied
