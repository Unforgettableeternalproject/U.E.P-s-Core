"""切片 F：module.* 就緒事件訂閱（註冊表端；事件名暫定，module coordinator 待設計）。離線，不需 DB（除組裝一項）。

fake coordinator 經真 Bus 發事件 -> ``registry.readiness`` -> 投影（warming 進 pending、unavailable 不投出）
-> Gateway 執行前檢查（``tool_not_ready``）；亂序／重送忽略；handler 例外隔離；退訂後不再接收。
"""
from __future__ import annotations

import logging
from typing import Any

import pytest

from tests.uep.test_tool_registry import make_entry
from uep.assembly import CoreAssembly
from uep.config import UepSettings
from uep.events.bus import EventBus
from uep.events.envelope import EventDraft, TrustedContext, build_event
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

    def emit(self, state: str, revision: int, module_id: str = MODULE, **extra: Any) -> None:
        self._n += 1
        draft = EventDraft(
            event_type=f"module.{state}", idempotency_key=f"mc-{self._n}",
            payload={"module_id": module_id, "revision": revision, **extra},
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
    bus.subscribe("module.ready", lambda e: seen.append(e.event_type))  # 其他訂閱者
    subscribe_module_events(bus, registry)  # 同事件上的另一個正常 registry 訂閱者
    with caplog.at_level(logging.ERROR, logger="uep.tools.module_events"):
        FakeCoordinator(bus).emit("ready", 1)
    assert seen == ["module.ready"] and registry.readiness("speak.tool") == "ready"
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
        draft = EventDraft(event_type="module.ready", idempotency_key="mc-async", payload={"module_id": MODULE, "revision": 1})
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
