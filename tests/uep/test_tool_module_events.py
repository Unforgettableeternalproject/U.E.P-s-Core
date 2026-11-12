"""切片 F：module.* 就緒事件訂閱（註冊表端；D108 R2：單一事件類型 module.state_changed）。離線，不需 DB（除組裝一項）。

fake coordinator 經真 Bus 發事件 -> ``registry.readiness`` -> 投影（warming 進 pending、unavailable 不投出）
-> Gateway 執行前檢查（``tool_not_ready``）；亂序／重送忽略；handler 例外隔離；退訂後不再接收。
"""
from __future__ import annotations

import logging
import time
from typing import Any

import pytest

from tests.uep.module_fakes import FakeClock, FakeHandle, FakeObserver
from tests.uep.test_tool_registry import make_entry
from uep.assembly import CoreAssembly
from uep.config import UepSettings
from uep.events.bus import EventBus
from uep.events.envelope import EventDraft, TrustedContext, build_event
from uep.lifecycle.coordinator import ModuleCoordinator
from uep.lifecycle.descriptor import ModuleDescriptor
from uep.tools.gateway import GatewayRejected, ToolGateway
from uep.tools.module_events import subscribe_module_events
from uep.tools.projection import ProjectionContext
from uep.tools.registry import ToolRegistry

MODULE = "gpu:indextts"


def _registry() -> ToolRegistry:
    heavy = make_entry(
        "speak.tool", spec={"capability": "memory.read"}, requires=(MODULE,), load_cost="heavy", warm_required=True,
    )
    return ToolRegistry.build([make_entry("deep_recall"), heavy], known_modules=[MODULE])


class FakeCoordinator:
    """經真 Bus 發 module.* 事件；``sync=True`` 在呼叫線程分發，結果可確定性斷言。"""

    def __init__(self, bus: EventBus) -> None:
        self.bus, self._n = bus, 0

    def emit(self, readiness: str, revision: int, module_id: str = MODULE, **extra: Any) -> None:
        self._n += 1
        draft = EventDraft(
            event_type="module.state_changed", idempotency_key=f"mc-{self._n}",
            payload={"module_id": module_id, "revision": revision, "readiness": readiness, **extra},
        )
        trusted = TrustedContext(source="module.coordinator", actor_id="system", scope_ref="scope_public", external=False)
        self.bus.publish(build_event(draft, trusted), sync=True)


def _names(cap: dict) -> list[str]:
    return sorted(t["tool"] for t in cap["tools"])


def test_events_drive_readiness_projection_and_gateway():
    registry, bus = _registry(), EventBus()
    subscribe_module_events(bus, registry)
    mc = FakeCoordinator(bus)
    ctx = ProjectionContext()
    cap_before = registry.project({"memory.read"}, ctx)
    assert _names(cap_before) == ["deep_recall"] and registry.readiness("speak.tool") == "warming"  # warm_required 預設 warming

    mc.emit("ready", 1)
    cap = registry.project({"memory.read"}, ctx)
    assert registry.readiness("speak.tool") == "ready" and _names(cap) == ["deep_recall", "speak.tool"]

    mc.emit("warming", 2)  # 已投出後轉 warming：進 pending、不投出、Gateway 擋
    cap_warm = registry.project({"memory.read"}, ctx)
    assert _names(cap_warm) == ["deep_recall"]
    assert [dict(p) for p in cap_warm["pending"]] == [{"tool": "speak.tool", "reason": "warming"}]
    with pytest.raises(GatewayRejected) as exc:
        ToolGateway(registry).check_intent("speak.tool", {"path": "x"}, cap)
    assert exc.value.code == "tool_not_ready"

    mc.emit("unavailable", 3, reason_code="oom")  # unavailable：不投出也不 pending
    cap_gone = registry.project({"memory.read"}, ctx)
    assert _names(cap_gone) == ["deep_recall"] and cap_gone["pending"] == ()
    with pytest.raises(GatewayRejected) as exc:
        ToolGateway(registry).check_intent("speak.tool", {"path": "x"}, cap)
    assert exc.value.code == "tool_not_ready"


def test_out_of_order_and_duplicate_events_are_ignored():
    registry, bus = _registry(), EventBus()
    subscribe_module_events(bus, registry)
    mc = FakeCoordinator(bus)
    mc.emit("ready", 5)
    mc.emit("unavailable", 3)  # 亂序（舊 revision）
    mc.emit("warming", 5)  # 重送（相同 revision）
    assert registry.readiness("speak.tool") == "ready"
    mc.emit("unavailable", 1, module_id="gpu:unknown")  # 未知模組忽略
    assert registry.readiness("speak.tool") == "ready"
    mc.emit("warming", 1)  # epoch 預設 0，revision 1 < 5：忽略
    mc.emit("warming", 1, epoch=1)  # coordinator 重啟（epoch 變大）後 revision 重新計：採用
    assert registry.readiness("speak.tool") == "warming"


def test_handler_exception_is_isolated_from_bus_and_other_subscribers(caplog):
    bus = EventBus()

    class Boom:
        def apply_module_event(self, event_type, payload):
            raise RuntimeError("私人內容不該出現在 log")

    registry = _registry()
    seen: list[str] = []
    subscribe_module_events(bus, Boom())
    bus.subscribe("module.state_changed", lambda e: seen.append(e.event_type))  # 其他訂閱者
    subscribe_module_events(bus, registry)  # 同事件上的另一個正常 registry 訂閱者
    with caplog.at_level(logging.ERROR, logger="uep.tools.module_events"):
        FakeCoordinator(bus).emit("ready", 1)
    assert seen == ["module.state_changed"] and registry.readiness("speak.tool") == "ready"
    assert "RuntimeError" in caplog.text and "私人內容" not in caplog.text
    assert bus.get_stats()["handler_errors"] == 0  # 已在 handler 內吞掉，不外溢給 Bus


def test_unsubscribe_stops_receiving_and_is_idempotent():
    registry, bus = _registry(), EventBus()
    unsubscribe = subscribe_module_events(bus, registry)
    mc = FakeCoordinator(bus)
    mc.emit("ready", 1)
    unsubscribe()
    unsubscribe()
    mc.emit("unavailable", 2)
    assert registry.readiness("speak.tool") == "ready"


def test_real_consumer_thread_delivers_events():
    registry, bus = _registry(), EventBus()
    subscribe_module_events(bus, registry)
    bus.start()
    try:
        trusted = TrustedContext(source="module.coordinator", actor_id="system", scope_ref="scope_public", external=False)
        draft = EventDraft(event_type="module.state_changed", idempotency_key="mc-async",
            payload={"module_id": MODULE, "revision": 1, "readiness": "ready"},
        )
        bus.publish(build_event(draft, trusted))
    finally:
        bus.stop()  # 排空佇列
    assert registry.readiness("speak.tool") == "ready"


def test_flag_defaults_off():
    assert UepSettings.model_fields["tool_module_events"].default is False


def test_core_shutdown_unsubscribes_module_events():
    registry, bus = _registry(), EventBus()
    core = object.__new__(CoreAssembly)
    core.module_events_unsubscribe = subscribe_module_events(bus, registry)
    core.endpoint_server, core.start_log = None, []
    core._stop_intake()
    assert core.module_events_unsubscribe is None
    FakeCoordinator(bus).emit("unavailable", 1)
    assert registry.readiness("speak.tool") == "warming"  # 退訂後事件不再生效（維持預設）


# --- R4／R5（D109）---

def _degrade_registry() -> ToolRegistry:
    strict = make_entry("strict.tool", requires=(MODULE,))
    soft = make_entry("soft.tool", requires=(MODULE,), degradable=True)
    return ToolRegistry.build([strict, soft], known_modules=[MODULE])


def test_degraded_dependency_only_keeps_degradable_tools_ready():
    registry, bus = _degrade_registry(), EventBus()
    subscribe_module_events(bus, registry)
    mc = FakeCoordinator(bus)
    mc.emit("ready", 1, state="ready")
    assert (registry.readiness("strict.tool"), registry.readiness("soft.tool")) == ("ready", "ready")
    mc.emit("ready", 2, state="degraded", reason_code="probe_degraded")
    assert (registry.readiness("strict.tool"), registry.readiness("soft.tool")) == ("unavailable", "ready")
    mc.emit("ready", 3, state="ready")  # 恢復
    assert (registry.readiness("strict.tool"), registry.readiness("soft.tool")) == ("ready", "ready")


def test_config_or_authorization_denial_beats_degraded_to_ready():
    registry, bus = _degrade_registry(), EventBus()
    subscribe_module_events(bus, registry)
    mc = FakeCoordinator(bus)
    mc.emit("ready", 1, state="degraded", reason_code="delete_disabled")  # degradable 也不能被設定禁止覆蓋
    assert registry.readiness("soft.tool") == "unavailable"
    mc.emit("ready", 2, state="degraded", reason_code="probe_degraded")
    assert registry.readiness("soft.tool") == "ready"


def test_event_without_state_still_works_on_readiness_alone():
    registry, bus = _degrade_registry(), EventBus()
    subscribe_module_events(bus, registry)
    FakeCoordinator(bus).emit("ready", 1)  # 沒有 state：只靠 readiness，不套用 degraded 規則
    assert (registry.readiness("strict.tool"), registry.readiness("soft.tool")) == ("ready", "ready")


def test_unloaded_module_is_not_in_enabled_tags_but_loading_is():
    only = make_entry("only.tool", spec={"capability": "memory.read"}, requires=(MODULE,))
    registry, bus = ToolRegistry.build([only], known_modules=[MODULE]), EventBus()
    subscribe_module_events(bus, registry)
    mc = FakeCoordinator(bus)
    assert "memory.read" in registry.enabled_tags()  # 首事件前 ready（未宣告 warm_required）
    mc.emit("unavailable", 1, state="unloaded", reason_code="not_loaded")
    assert "memory.read" not in registry.enabled_tags()  # R4=A：unloaded 不入標籤
    mc.emit("warming", 2, state="loading")
    assert "memory.read" in registry.enabled_tags()  # loading／warming 仍納入
    mc.emit("unavailable", 3, state="unloading", reason_code="unloaded")
    assert "memory.read" not in registry.enabled_tags()


# --- 真 coordinator＋真 Bus＋真 registry＋Gateway ---

def _bus_sink(bus: EventBus, drop: list[int] | None = None):
    trusted = TrustedContext(source="module.coordinator", actor_id="system", scope_ref="scope_public", external=False)
    sent: list[dict[str, Any]] = []

    def sink(event_type: str, payload: Any) -> None:
        sent.append(dict(payload))
        if drop and drop[0] > 0:
            drop[0] -= 1  # 模擬 Bus 丟事件
            return
        bus.publish(build_event(EventDraft(event_type=event_type, payload=dict(payload)), trusted), sync=True)

    return sink, sent


def _real_rig(handle: Any, *, probe_interval_s: float = 0.0, drop: list[int] | None = None):
    bus = EventBus()
    sink, sent = _bus_sink(bus, drop)
    clock = FakeClock()
    mc = ModuleCoordinator(
        sink, epoch_source=lambda: 1, clock=clock, run_scheduler=False, resync_interval_s=0,
        probe_interval_s=probe_interval_s,
    )
    mc.register(ModuleDescriptor(MODULE, autoload=True), handle)
    registry = ToolRegistry.build(
        [make_entry("deep_recall"), make_entry("speak.tool", requires=(MODULE,), warm_required=True),
         make_entry("soft.tool", requires=(MODULE,), degradable=True)],
        known_modules=mc.known_ids(),  # known modules 取自 coordinator
    )
    return bus, mc, registry, clock, sent


def _wait_state(mc: ModuleCoordinator, state: str) -> None:
    deadline = time.monotonic() + 5
    while mc.status(MODULE).state != state:
        assert time.monotonic() < deadline, "等待背景工作逾時"
        time.sleep(0.005)


def test_real_coordinator_snapshot_then_load_drives_registry_and_gateway():
    handle = FakeHandle()
    gate = handle.block("load")
    bus, mc, registry, _clock, sent = _real_rig(handle)
    subscribe_module_events(bus, registry)  # 先訂閱再 start
    try:
        mc.start()
        assert sent[0]["cause"] == "startup"  # 啟動快照先發
        assert handle.entered["load"].wait(5)
        assert registry.readiness("soft.tool") == "warming"  # 快照與 loading 都已被套用，沒有 fail-open 窗口
        cap = registry.project({"memory.read"}, ProjectionContext())
        assert sorted(p["tool"] for p in cap["pending"]) == ["soft.tool", "speak.tool"]
        gate.set()
        assert mc.wait_settled(5)
        ready_cap = registry.project({"memory.read"}, ProjectionContext())
        assert _names(ready_cap) == ["deep_recall", "soft.tool", "speak.tool"]
        ToolGateway(registry).check_intent("speak.tool", {"path": "x"}, ready_cap)  # ready：通過
    finally:
        handle.release_all()
        mc.stop(timeout_s=2.0)
    assert registry.readiness("speak.tool") == "unavailable"  # 停機的最終事件（cause=shutdown）已套用
    with pytest.raises(GatewayRejected) as exc:
        ToolGateway(registry).check_intent("speak.tool", {"path": "x"}, ready_cap)
    assert exc.value.code == "tool_not_ready"


def test_real_coordinator_snapshot_applied_before_any_load_completes():
    handle = FakeHandle()
    gate = handle.block("load")
    bus, mc, registry, _clock, _sent = _real_rig(handle)
    first: list[str] = []
    bus.subscribe("module.state_changed", lambda e: first.append(e.payload["state"]))
    subscribe_module_events(bus, registry)
    try:
        mc.start()
        assert first[0] == "unloaded" and registry.readiness("soft.tool") in ("unavailable", "warming")
    finally:
        gate.set()
        mc.stop(timeout_s=2.0)


def test_real_coordinator_degraded_differs_by_tool_degradability():
    obs = FakeObserver("ok")
    bus, mc, registry, clock, _sent = _real_rig(obs, probe_interval_s=1.0)
    subscribe_module_events(bus, registry)
    try:
        mc.start()
        assert mc.wait_settled(5)
        assert registry.readiness("soft.tool") == "ready"
        obs.result = "degraded"
        clock.advance(1)
        mc.tick()
        _wait_state(mc, "degraded")
        assert (registry.readiness("speak.tool"), registry.readiness("soft.tool")) == ("unavailable", "ready")
    finally:
        mc.stop(timeout_s=2.0)


def test_lost_events_are_compensated_by_resync_and_stale_replays_are_ignored():
    handle = FakeHandle()
    drop = [0]
    bus, mc, registry, _clock, sent = _real_rig(handle, drop=drop)
    subscribe_module_events(bus, registry)
    try:
        mc.start()
        assert mc.wait_settled(5)
        assert registry.readiness("soft.tool") == "ready"
        drop[0] = 99  # 之後的事件全被丟掉：registry 停在舊狀態
        assert mc.request_unload(MODULE).accepted
        _wait_state(mc, "unloaded")
        assert registry.readiness("soft.tool") == "ready"  # 過期的視圖
        drop[0] = 0
        mc.resync()  # 補償：遞增 revision 的當前狀態
        assert registry.readiness("soft.tool") == "unavailable"
        old = next(e for e in sent if e["state"] == "ready")  # 亂序重送：舊 revision 不能把視圖拉回去
        trusted = TrustedContext(source="module.coordinator", actor_id="system", scope_ref="scope_public", external=False)
        bus.publish(build_event(EventDraft(event_type="module.state_changed", payload=old), trusted), sync=True)
        assert registry.readiness("soft.tool") == "unavailable"
    finally:
        mc.stop(timeout_s=2.0)
