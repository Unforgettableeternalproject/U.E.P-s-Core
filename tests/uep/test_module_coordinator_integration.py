"""PR-4 C4：真 Bus + 真 ToolRegistry + 真 ModuleCoordinator + 真 S 群 handle（fake 設定）+ 假 M 群持有者（離線，不需 DB）。

時間用假時鐘＋手動 ``tick()``；背景 worker 的結果用有時限的輪詢等待。涵蓋啟動快照、轉移、亂序、丟事件後 resync、停機，
以及旗標關閉（無 coordinator）時與現況一致的回歸。真 GPU 測試只在 ``UEP_E2E_ALLOW_GPU`` 設定時才跑。
"""
from __future__ import annotations

import os
import threading
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import SecretStr

from tests.uep.module_fakes import FakeClock
from tests.uep.test_module_coordinator import wait_until
from uep.config import UepSettings
from uep.echo.module_handles import MODULE_LAYA, MODULE_TRANSLATE, build_m_group_registrations
from uep.events.bus import EventBus
from uep.events.envelope import EventDraft, TrustedContext, build_event
from uep.lifecycle.coordinator import ModuleCoordinator
from uep.tools.catalog.chatroom import TOOL_READ, with_chatroom
from uep.tools.catalog.lore import TOOL_DELETE, TOOL_RECALL, with_lore
from uep.tools.catalog.memory import memory_registry
from uep.tools.module_events import seed_fail_closed, subscribe_module_events
from uep.tools.module_handles import build_s_group_registrations
from uep.tools.web_image import with_web_image

TRUSTED = TrustedContext(source="module.coordinator", actor_id="system", scope_ref="scope_public", external=False)
TOOL_SEARCH, TOOL_IMAGE = "web.search", "image.read"
FAKE_VISION = lambda timeout: object()  # noqa: E731  有視覺模型即算可用，不建立真 client


class Rig:
    """依 ``assemble_core`` 的順序接線：with_* 併入（帶 coordinator）-> seed_fail_closed -> 訂閱 -> start。"""

    def __init__(self, *, health: dict[str, Any] | None = None, lore: bool = True, probe_interval_s: float = 10.0) -> None:
        self.settings = UepSettings(
            _env_file=None, web_search_providers="duckduckgo", web_fetch_enabled=True, lore_delete_enabled=False,
            **({"lore_url": "http://lore.invalid", "lore_token": SecretStr("t")} if lore else {}),
            chatroom_url="http://hub.invalid", chatroom_session_key="k", chatroom_token=SecretStr("t"), chatroom_rooms="r1",
        )
        self.bus, self.clock = EventBus(), FakeClock()
        self.sent: list[dict[str, Any]] = []
        self.drop = threading.Event()  # set 時 sink 丟棄事件（模擬 Bus 丟事件）
        self.mc = ModuleCoordinator(
            self._sink, epoch_source=lambda: 1, clock=self.clock, run_scheduler=False, resync_interval_s=0,
            probe_interval_s=probe_interval_s,
        )
        self.holders = SimpleNamespace(
            laya=SimpleNamespace(loaded=False, load_error=None),
            translator=SimpleNamespace(available=False, load_error=None, cpu_fallback_used=False),
        )
        for desc, handle in [
            *build_s_group_registrations(self.settings, health=health, client_factory=FAKE_VISION),
            *build_m_group_registrations(laya=self.holders.laya, translator=self.holders.translator),
        ]:
            self.mc.register(desc, handle)
        registry = with_chatroom(memory_registry(), configured=True, coordinator=self.mc)
        registry, _ = with_web_image(registry, self.settings, coordinator=self.mc, client_factory=FAKE_VISION)
        registry = with_lore(registry, configured=lore, delete_enabled=False, coordinator=self.mc)
        seed_fail_closed(registry, self.mc)
        self.registry = registry
        self.unsubscribe = subscribe_module_events(self.bus, registry)

    def _sink(self, event_type: str, payload: Any) -> None:
        if self.drop.is_set():
            return
        self.sent.append(dict(payload))
        self.bus.publish(build_event(EventDraft(event_type=event_type, payload=dict(payload)), TRUSTED), sync=True)

    def ready(self, *tools: str) -> bool:
        return all(self.registry.readiness(t) == "ready" for t in tools)

    def settle(self, module_id: str) -> None:
        wait_until(lambda: not self.mc._recs[module_id].in_flight)  # noqa: SLF001

    def tick(self, seconds: float = 0.0) -> None:
        self.clock.advance(seconds)
        self.mc.tick()

    def states(self, module_id: str) -> list[str]:
        return [e["state"] for e in self.sent if e["module_id"] == module_id]


@pytest.fixture
def rig():
    r = Rig()
    yield r
    r.mc.stop(timeout_s=2.0)
    r.unsubscribe()


def start(r: Rig) -> None:
    r.mc.start()
    wait_until(lambda: r.mc.status("lore").state == "ready" and r.mc.status("web_search").state == "ready")


def test_fail_closed_before_start_then_snapshot_makes_configured_tools_ready(rig: Rig) -> None:
    assert not rig.ready(TOOL_RECALL, TOOL_READ, TOOL_SEARCH)  # start 前：fail-closed 底
    start(rig)
    assert rig.ready(TOOL_RECALL, TOOL_READ, TOOL_SEARCH, TOOL_IMAGE)
    assert rig.registry.readiness(TOOL_DELETE) == "unavailable"  # delete 被設定關閉，probe 成功也不覆蓋
    assert rig.sent[0]["cause"] == "startup"  # 啟動快照先於任何載入轉移
    first_by_module = {}
    for e in rig.sent:
        first_by_module.setdefault(e["module_id"], e["cause"])
    assert set(first_by_module.values()) == {"startup"}


def test_unconfigured_lore_stays_unavailable_and_never_loads() -> None:
    r = Rig(lore=False)
    try:
        r.mc.start()
        wait_until(lambda: r.mc.status("web_search").state == "ready")
        assert r.registry.readiness(TOOL_RECALL) == "unavailable"
        assert r.mc.status("lore").state == "unloaded" and "loading" not in r.states("lore")
    finally:
        r.mc.stop(timeout_s=2.0)
        r.unsubscribe()


def test_health_failure_transitions_tools_out_and_back(rig: Rig) -> None:
    healthy = {"lore": True}
    rig2 = Rig(health={"lore": lambda: healthy["lore"]})
    try:
        start(rig2)
        assert rig2.ready(TOOL_RECALL)
        healthy["lore"] = False
        for _ in range(3):  # probe_dead_threshold 次連續 dead 才判 failed
            rig2.tick(10.5)
            rig2.settle("lore")
        wait_until(lambda: rig2.mc.status("lore").state == "failed")
        assert rig2.registry.readiness(TOOL_RECALL) == "unavailable" and rig2.ready(TOOL_READ)  # 只影響 lore
        healthy["lore"] = True
        rig2.tick(6.0)  # 退避後重試
        wait_until(lambda: rig2.mc.status("lore").state == "ready")
        assert rig2.ready(TOOL_RECALL)
    finally:
        rig2.mc.stop(timeout_s=2.0)
        rig2.unsubscribe()


def test_out_of_order_and_replayed_events_are_ignored(rig: Rig) -> None:
    start(rig)
    stale = next(e for e in rig.sent if e["module_id"] == "lore")  # revision 最小的舊事件
    newer = [e for e in rig.sent if e["module_id"] == "lore"][-1]
    assert stale["revision"] < newer["revision"]
    bad = {**stale, "state": "failed", "readiness": "unavailable", "reason_code": "probe_dead"}
    rig.bus.publish(build_event(EventDraft(event_type="module.state_changed", payload=bad), TRUSTED), sync=True)
    rig.bus.publish(build_event(EventDraft(event_type="module.state_changed", payload=dict(newer)), TRUSTED), sync=True)  # 重送
    assert rig.registry.readiness(TOOL_RECALL) == "ready"


def test_dropped_events_stale_view_is_repaired_by_resync() -> None:
    healthy = {"lore": True}
    r = Rig(health={"lore": lambda: healthy["lore"]})
    try:
        start(r)
        r.drop.set()  # 之後的事件全丟
        healthy["lore"] = False
        for _ in range(3):
            r.tick(10.5)
            r.settle("lore")
        wait_until(lambda: r.mc.status("lore").state == "failed")
        assert r.registry.readiness(TOOL_RECALL) == "ready"  # 註冊表沒收到 failed：視圖過時
        r.drop.clear()
        r.mc.resync()
        assert r.registry.readiness(TOOL_RECALL) == "unavailable"  # resync 補償後對齊
        assert r.sent[-1]["cause"] == "resync" or any(e["cause"] == "resync" for e in r.sent)
    finally:
        r.mc.stop(timeout_s=2.0)
        r.unsubscribe()


def test_m_group_observed_warming_to_ready_and_cpu_fallback_degraded(rig: Rig) -> None:
    start(rig)
    wait_until(lambda: rig.mc.status(MODULE_LAYA).state == "warming" and rig.mc.status(MODULE_TRANSLATE).state == "warming")
    rig.holders.laya.loaded = True
    rig.holders.translator.available, rig.holders.translator.cpu_fallback_used = True, True
    rig.tick(10.5)  # probe_interval_s=10：warming 的重探沿用它
    wait_until(lambda: rig.mc.status(MODULE_LAYA).state == "ready" and rig.mc.status(MODULE_TRANSLATE).state == "degraded")
    assert rig.states(MODULE_LAYA)[-3:] == ["loading", "warming", "ready"]
    assert rig.mc.status(MODULE_LAYA).failures == 0  # warming 期間沒燒重試次數


def test_shutdown_unloads_everything_and_tools_go_unavailable(rig: Rig) -> None:
    start(rig)
    rig.mc.stop(timeout_s=2.0)
    assert all(s.state == "unloaded" for s in rig.mc.snapshot().values())
    assert not rig.ready(TOOL_RECALL, TOOL_READ, TOOL_SEARCH)
    assert rig.states("lore")[-1] == "unloaded"
    assert rig.states("lore_delete").index("unloaded") >= 0


# --- 旗標關閉（沒有 coordinator）時與現況一致 ---


def test_without_coordinator_registry_matches_ready_path() -> None:
    settings = UepSettings(
        _env_file=None, web_search_providers="duckduckgo", lore_url="http://lore.invalid", lore_token=SecretStr("t"),
        lore_delete_enabled=False, chatroom_url="http://hub.invalid", chatroom_session_key="k", chatroom_token=SecretStr("t"),
        chatroom_rooms="r1",
    )
    plain = with_chatroom(memory_registry(), configured=True)
    plain, _ = with_web_image(plain, settings, client_factory=FAKE_VISION)
    plain = with_lore(plain, configured=True, delete_enabled=False)
    r = Rig()
    try:
        start(r)
        assert set(plain.entries()) == set(r.registry.entries())
        for tool in plain.entries():
            if tool == TOOL_DELETE:
                assert plain.readiness(tool) == r.registry.readiness(tool) == "unavailable"
            elif not tool.startswith(("memory", "deep_recall")) and tool in (TOOL_RECALL, TOOL_READ, TOOL_SEARCH, TOOL_IMAGE):
                assert plain.readiness(tool) == r.registry.readiness(tool) == "ready", tool
    finally:
        r.mc.stop(timeout_s=2.0)
        r.unsubscribe()


# --- 真 GPU（預設 skip）---


@pytest.mark.skipif(not os.environ.get("UEP_E2E_ALLOW_GPU"), reason="真 GPU 測試需要 UEP_E2E_ALLOW_GPU")
def test_real_gpu_observation_smoke() -> None:
    """有 GPU 環境才跑：NVML 預算探針可建立並讀值（只讀，不載入任何模型）。"""
    from uep.lifecycle.budget import NvmlVramProbe

    reading = NvmlVramProbe(0).read()
    assert reading.free_mb >= 0
