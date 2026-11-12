"""Module Coordinator PR-2：旗標、catalog 接線與組裝順序。

離線部分（不需 DB）：旗標預設、policy 檢查、catalog 的 coordinator 路徑、``CoreAssembly`` 啟停順序（用替身）。
標 ``_db`` 的測試走真 ``assemble_core``，需要 ``UEP_TEST_DATABASE_URL``（由 conftest 的 ``session_factory`` 決定是否 skip）。
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from tests.uep.module_fakes import FakeHandle
from tests.uep.test_tool_registry import make_entry
from uep.assembly import (
    AssemblyError,
    CoreAssembly,
    assemble_core,
    build_module_budget,
    check_m_group_pinned,
    check_module_events_policy,
)
from uep.config import UepSettings
from uep.events.bus import EventBus
from uep.events.envelope import EventDraft, TrustedContext, build_event
from uep.lifecycle.coordinator import ModuleCoordinator
from uep.lifecycle.descriptor import ModuleDescriptor
from uep.tools.catalog.chatroom import CHATROOM_MODULE, TOOL_READ, with_chatroom
from uep.tools.catalog.lore import (
    LORE_DELETE_MODULE,
    LORE_MODULE,
    TOOL_DELETE,
    TOOL_RECALL,
    with_lore,
)
from uep.tools.catalog.memory import memory_registry
from uep.tools.module_events import seed_fail_closed, subscribe_module_events
from uep.tools.registry import ToolRegistry

# --- 旗標與 policy ---

def test_both_flags_default_off():
    assert UepSettings.model_fields["module_coordinator"].default is False
    assert UepSettings.model_fields["tool_module_events"].default is False
    assert UepSettings.model_fields["module_coordinator"].validation_alias == "UEP_MODULE_COORDINATOR"


@pytest.mark.parametrize("subscriber,producer,fails", [
    (False, False, False), (False, True, False), (True, True, False), (True, False, True),
])
def test_subscriber_without_producer_is_an_assembly_error(subscriber, producer, fails):
    if fails:
        with pytest.raises(AssemblyError, match="producer"):
            check_module_events_policy(subscriber, producer)
    else:
        check_module_events_policy(subscriber, producer)


def _bare_kwargs() -> dict:
    return {
        "bus": EventBus(), "publisher": MagicMock(), "control": MagicMock(), "adapter": MagicMock(), "executors": {},
        "tool_resolver": memory_registry(), "turn_generation_source": lambda: 0,
        "presence_audience": lambda: "scope_private_owner",
        "maintenance_interval": None, "decision_log": False, "supervisor": False,
    }


def test_assemble_core_rejects_subscriber_without_producer_before_touching_anything():
    with pytest.raises(AssemblyError, match="UEP_MODULE_COORDINATOR"):
        assemble_core(MagicMock(), tool_module_events=True, module_coordinator=False, **_bare_kwargs())


def test_assemble_core_rejects_registrations_without_producer():
    desc = ModuleDescriptor("lore")
    with pytest.raises(AssemblyError, match="module_registrations"):
        assemble_core(
            MagicMock(), module_coordinator=False, tool_module_events=False,
            module_registrations=[(desc, FakeHandle())], **_bare_kwargs(),
        )


# --- catalog 的 coordinator 路徑 ---

def _coordinator(*ids: str, sent=None):
    bus = EventBus()
    trusted = TrustedContext(source="module.coordinator", actor_id="system", scope_ref="scope_public", external=False)
    log = sent if sent is not None else []

    def sink(event_type, payload):
        log.append(dict(payload))
        bus.publish(build_event(EventDraft(event_type=event_type, payload=dict(payload)), trusted), sync=True)

    mc = ModuleCoordinator(sink, epoch_source=lambda: 1, run_scheduler=False, resync_interval_s=0, probe_interval_s=0)
    handles = {}
    for module_id in ids:
        handles[module_id] = FakeHandle()
        handles[module_id].probe_result = "ok"
        mc.register(ModuleDescriptor(module_id, autoload=True), handles[module_id])
    return bus, mc, handles


def _base() -> ToolRegistry:
    return ToolRegistry.build([make_entry("deep_recall")], known_modules=[])


def test_catalog_known_modules_come_from_coordinator_and_gates_beat_probe_ok():
    bus, mc, handles = _coordinator(LORE_MODULE, LORE_DELETE_MODULE)
    registry = with_lore(_base(), configured=False, delete_enabled=False, coordinator=mc)
    seed_fail_closed(registry, mc)
    assert {LORE_MODULE, LORE_DELETE_MODULE} <= set(registry._known_modules)
    assert registry.readiness(TOOL_RECALL) == "unavailable"  # start 前：fail-closed 底
    subscribe_module_events(bus, registry)
    try:
        mc.start()
        assert mc.wait_settled(5)
        mc.resync()
        # probe／load 回報成功也不能覆蓋設定未允許
        assert registry.readiness(TOOL_RECALL) == "unavailable" and registry.readiness(TOOL_DELETE) == "unavailable"
        assert all(h.calls == [] for h in handles.values())
    finally:
        mc.stop(timeout_s=2.0)


def test_catalog_configured_lore_becomes_ready_but_delete_ban_holds():
    bus, mc, handles = _coordinator(LORE_MODULE, LORE_DELETE_MODULE)
    registry = with_lore(_base(), configured=True, delete_enabled=False, coordinator=mc)
    seed_fail_closed(registry, mc)
    subscribe_module_events(bus, registry)
    try:
        mc.start()
        assert mc.wait_settled(5)
        assert registry.readiness(TOOL_RECALL) == "ready"
        assert registry.readiness(TOOL_DELETE) == "unavailable"  # delete 禁令不被 lore 的 ready 帶動
        assert handles[LORE_DELETE_MODULE].calls == [] and handles[LORE_MODULE].calls == ["load"]
    finally:
        mc.stop(timeout_s=2.0)


def test_registered_is_not_usable_and_unregistered_is_unknown():
    # chatroom 已登記但未設定：已知（不是 RA-15 的未知）卻 unavailable
    bus, mc, _ = _coordinator(CHATROOM_MODULE)
    registered = with_chatroom(_base(), configured=False, coordinator=mc)
    seed_fail_closed(registered, mc)
    assert CHATROOM_MODULE in registered._known_modules and registered.readiness(TOOL_READ) == "unavailable"
    subscribe_module_events(bus, registered)
    try:
        mc.start()
        assert mc.wait_settled(5)
        assert registered.readiness(TOOL_READ) == "unavailable"
    finally:
        mc.stop(timeout_s=2.0)
    # 沒登記：不在 known modules，恆為 unavailable（fail-closed）
    _, empty, _ = _coordinator()
    unknown = with_chatroom(_base(), configured=True, coordinator=empty)
    assert CHATROOM_MODULE not in unknown._known_modules and unknown.readiness(TOOL_READ) == "unavailable"


def test_snapshot_failure_keeps_tools_fail_closed():
    _, mc, _ = _coordinator(LORE_MODULE, LORE_DELETE_MODULE)
    registry = with_lore(_base(), configured=True, delete_enabled=True, coordinator=mc)
    seed_fail_closed(registry, mc)
    # 沒人訂閱（等同事件全丟）：已設定的 lore 也不會因「預設 ready」而 fail-open
    mc.start()
    try:
        assert mc.wait_settled(5)
        assert registry.readiness(TOOL_RECALL) == "unavailable"
    finally:
        mc.stop(timeout_s=2.0)


def test_catalog_without_coordinator_is_unchanged():
    base = _base()
    off = with_lore(base, configured=False)
    assert off.readiness(TOOL_RECALL) == "unavailable"
    on = with_lore(base, configured=True, delete_enabled=False)
    assert on.readiness(TOOL_RECALL) == "ready" and on.readiness(TOOL_DELETE) == "unavailable"
    assert with_chatroom(base, configured=False).readiness(TOOL_READ) == "unavailable"


# --- CoreAssembly 啟停順序（替身）---

class _Probe:
    def __init__(self, order: list[str], name: str) -> None:
        self._order, self._name = order, name

    def __getattr__(self, attr: str):
        def call(*a, **k):
            self._order.append(f"{self._name}.{attr}")
            return MagicMock()

        return call


def _stub_core(order: list[str], coordinator) -> CoreAssembly:
    core = object.__new__(CoreAssembly)
    core.persona_id = "uep"
    core.presence = _Probe(order, "presence")
    core.bus = _Probe(order, "bus")
    core.state = _Probe(order, "state")
    core.bridge = _Probe(order, "bridge")
    core.memory = _Probe(order, "memory")
    core.echo = _Probe(order, "echo")
    core.loop_launcher = _Probe(order, "loop_launcher")
    core.endpoint_server = _Probe(order, "endpoint_server")
    core.stop_api = core.maintenance = core.supervisor_loop = core.dream_probe = core.decision_log = None
    core.supervisor = None
    core.relay_starters = ()
    core.start_log = []
    core.started = False
    core.module_coordinator = coordinator
    core.module_events_unsubscribe = lambda: order.append("unsubscribe")
    return core


class _RecordingCoordinator:
    def __init__(self, order: list[str]) -> None:
        self._order = order

    def start(self) -> None:
        self._order.append("coordinator.start")

    def wait_settled(self, timeout: float) -> bool:
        self._order.append("coordinator.settled")
        return True

    def stop(self) -> None:
        self._order.append("coordinator.stop")


def test_start_publishes_snapshot_before_echo_endpoint_and_ready():
    order: list[str] = []
    core = _stub_core(order, _RecordingCoordinator(order))
    core.state.set_runtime = lambda status: order.append(f"runtime.{getattr(status, 'name', status)}")
    core.start()
    assert order.index("presence.start") < order.index("bus.start") < order.index("coordinator.start")
    assert order.index("coordinator.settled") < order.index("memory.start") < order.index("echo.start")
    assert order.index("coordinator.settled") < order.index("endpoint_server.start") < order.index("runtime.READY")


def test_shutdown_stops_intake_then_coordinator_then_unsubscribes_then_bus():
    order: list[str] = []
    core = _stub_core(order, _RecordingCoordinator(order))
    core._stop_intake()
    assert order.index("loop_launcher.close") < order.index("echo.stop") < order.index("memory.stop")
    assert order.index("memory.stop") < order.index("coordinator.stop") < order.index("unsubscribe")
    assert core.module_events_unsubscribe is None
    core._stop_infrastructure()  # Bus 在退訂之後才關
    assert order.index("unsubscribe") < order.index("bus.stop")


def test_shutdown_without_producer_keeps_legacy_order_unsubscribe_first():
    order: list[str] = []
    core = _stub_core(order, None)
    core._stop_intake()
    assert order[0] == "unsubscribe" and "coordinator.stop" not in order
    assert "coordinator" not in " ".join(core.start_log) and core.start_log[0] == "module_events.unsubscribe"


def test_close_inputs_copes_with_coordinator_that_never_started():
    order: list[str] = []
    mc = ModuleCoordinator(lambda *a: None, epoch_source=lambda: 1, run_scheduler=False)
    core = _stub_core(order, mc)
    core._close_inputs()  # 沒啟動過的 coordinator：stop 是 no-op，不拋例外
    assert "unsubscribe" in order and "bus.stop" in order


def test_failed_start_closes_inputs_even_if_coordinator_start_raises():
    order: list[str] = []

    class Boom(_RecordingCoordinator):
        def start(self) -> None:
            raise RuntimeError("boom")

    core = _stub_core(order, Boom(order))
    core.state.set_runtime = lambda status: None
    with pytest.raises(RuntimeError):
        core.start()
    assert "unsubscribe" in order and not core.started


# --- 真 assemble_core（需 DB）---

def _db_core(session_factory, tmp_path, **over):
    from tests.uep.echo_support import fast_speak, make_identity, make_session
    from tests.uep.test_agent_loop_entry_db import _agent_pieces
    from tests.uep.test_agent_tool_path import FakePort
    from uep.control.channel import InProcessControlChannel
    from uep.echo.host import EchoPipelineHost
    from uep.events.publisher import EventPublisher
    from uep.presence.epoch import HostEpochStore
    from uep.stream.tracer import LatencyTracer

    bus = EventBus()
    publisher = EventPublisher(session_factory, bus)
    think, _, host_kw = _agent_pieces("agent_loop")
    host = EchoPipelineHost(
        publisher=publisher, think=think, speak=fast_speak(), sink_factory=lambda v, t: "x", session=make_session(),
        identity=make_identity(), tracer=LatencyTracer(), endpoint_kwargs={"tick_interval": 0.05}, **host_kw,
    )
    core = assemble_core(
        session_factory, bus=bus, publisher=publisher, control=InProcessControlChannel(), adapter=host.adapter,
        executors={}, tool_resolver=memory_registry(), turn_generation_source=host.generation_source,
        presence_audience=lambda: "scope_private_owner", epoch_store=HostEpochStore(tmp_path / "host_epoch.json"),
        maintenance_interval=None, echo=host, tool_path="agent", model_port=FakePort([]), agent_loop_entry=True,
        decision_log=False, supervisor=False, **over,
    )
    return core, bus


def test_db_flags_off_assembles_without_coordinator(session_factory, tmp_path):
    core, _ = _db_core(session_factory, tmp_path, module_coordinator=False, tool_module_events=False)
    try:
        assert core.module_coordinator is None and core.module_events_unsubscribe is None
        core.start()
        assert not any(entry.startswith("module_") for entry in core.start_log)
    finally:
        core.shutdown()


def test_db_producer_only_is_observation_mode_and_epoch_comes_from_presence(session_factory, tmp_path):
    seen: list[dict] = []
    desc = ModuleDescriptor("lore", autoload=True)
    handle = FakeHandle()
    core, _ = _db_core(
        session_factory, tmp_path, module_coordinator=True, tool_module_events=False,
        module_registrations=[(desc, handle)], module_sink=lambda t, p: seen.append(dict(p)),
    )
    try:
        assert core.module_coordinator.mode == "observe" and core.module_events_unsubscribe is None
        core.start()
        assert core.module_coordinator.wait_settled(5)
        assert seen and all(e["epoch"] == core.presence.core_epoch >= 1 for e in seen)
        assert "load" not in handle.calls and "unload" not in handle.calls  # 觀測模式不 provisioning
    finally:
        core.shutdown()


def test_db_both_flags_start_snapshot_precedes_ready_and_shutdown_order(session_factory, tmp_path):
    seen: list[dict] = []
    desc = ModuleDescriptor("lore", autoload=True)
    core, _ = _db_core(
        session_factory, tmp_path, module_coordinator=True, tool_module_events=True,
        module_registrations=[(desc, FakeHandle())], module_sink=lambda t, p: seen.append(dict(p)),
    )
    try:
        assert core.module_coordinator.mode == "manage" and core.module_events_unsubscribe is not None
        core.start()
        log = core.start_log
        assert log.index("module_coordinator.start") < log.index("echo.start") < log.index("state.runtime.ready")
        assert seen[0]["cause"] == "startup"
    finally:
        core.shutdown()
    log = core.start_log
    assert log.index("echo.stop") < log.index("module_coordinator.stop") < log.index("module_events.unsubscribe")


def test_web_image_with_coordinator_registers_all_but_gates_by_config(monkeypatch: pytest.MonkeyPatch):
    from tests.uep.test_tool_web_image import settings
    from uep.tools.web_image import with_web_image

    ids = ("web_search", "web_fetch", "image_read")
    bus, mc, handles = _coordinator(*ids)
    cfg = settings(monkeypatch, web_search_providers="", web_search_openai_fallback=False, web_fetch_enabled=False)
    registry, _ = with_web_image(memory_registry(), cfg, coordinator=mc)
    seed_fail_closed(registry, mc)
    assert set(ids) <= set(registry._known_modules)
    subscribe_module_events(bus, registry)
    try:
        mc.start()
        assert mc.wait_settled(5)
        # 三者都被設定閘住（無 provider、fetch 關閉、無視覺模型）：probe／load 都不得被呼叫，也不可用
        assert all(registry.readiness(t) == "unavailable" for t in ("web.search", "web.fetch", "image.read"))
        assert all(h.calls == [] for h in handles.values())
        assert mc.blocked_ids() == {
            "web_search": "not_configured", "web_fetch": "disabled", "image_read": "not_configured"}
    finally:
        mc.stop(timeout_s=2.0)


def test_observation_mode_still_applies_config_gates_without_touching_registry():
    _bus, mc, handles = _coordinator(LORE_MODULE, LORE_DELETE_MODULE)
    registry = with_lore(_base(), configured=False, delete_enabled=False, coordinator=mc, drive_registry=False)
    assert mc.blocked_ids() == {LORE_MODULE: "not_configured", LORE_DELETE_MODULE: "delete_disabled"}
    assert registry.readiness(TOOL_RECALL) == "unavailable"  # 原本的靜態路徑
    try:
        mc.start()
        assert mc.wait_settled(5)
        assert all(h.calls == [] for h in handles.values())  # 未設定的服務 probe／load 都不會發 ready
        assert all(mc.status(m).state == "unloaded" for m in handles)
    finally:
        mc.stop(timeout_s=2.0)


def test_assemble_core_wraps_registration_errors_in_assembly_error():
    desc = ModuleDescriptor("lore")
    with pytest.raises(AssemblyError, match="module_registrations"):
        assemble_core(
            MagicMock(), module_coordinator=True, tool_module_events=False,
            module_registrations=[(desc, FakeHandle()), (desc, FakeHandle())], **_bare_kwargs(),
        )


# --- PR-3：預算設定、M 群 pinned 檢查（離線）---

def test_budget_settings_default_off_and_budget_management_disabled_without_headroom():
    fields = UepSettings.model_fields
    assert fields["module_vram_headroom_mb"].default is None and fields["module_vram_budget_mb"].default is None
    assert build_module_budget(UepSettings()) is None  # 未設定 = 不啟用預算管理（不建立 NVML probe）


def test_budget_only_without_headroom_is_an_assembly_error_and_headroom_builds_budget():
    with pytest.raises(AssemblyError, match="HEADROOM"):
        build_module_budget(UepSettings(UEP_MODULE_VRAM_BUDGET_MB=8000))
    probe = MagicMock()
    budget = build_module_budget(UepSettings(UEP_MODULE_VRAM_HEADROOM_MB=2000, UEP_MODULE_VRAM_BUDGET_MB=8000), probe)
    assert budget is not None and budget.config.headroom_mb == 2000 and budget.config.budget_mb == 8000
    assert budget.config.recover_margin_mb == 256 and budget.config.evict_cooldown_s == 60.0
    probe.read.assert_not_called()  # 建立預算不量測


def test_probe_without_headroom_is_an_assembly_error_not_silently_dropped():
    with pytest.raises(AssemblyError, match="module_vram_probe"):
        build_module_budget(UepSettings(), MagicMock())


def test_m_group_descriptors_must_be_pinned():
    check_m_group_pinned([(ModuleDescriptor("stt", kind="model", pinned=True), FakeHandle()), (ModuleDescriptor("lore"), FakeHandle())])
    for kind in ("model", "stage"):
        with pytest.raises(AssemblyError, match="M 群"):
            check_m_group_pinned([(ModuleDescriptor("stt", kind=kind), FakeHandle())])


def test_assemble_core_rejects_unpinned_m_group_and_probe_without_producer():
    with pytest.raises(AssemblyError, match="M 群"):
        assemble_core(
            MagicMock(), module_coordinator=True, tool_module_events=False,
            module_registrations=[(ModuleDescriptor("stt", kind="model"), FakeHandle())], **_bare_kwargs(),
        )
    with pytest.raises(AssemblyError, match="module_vram_probe"):
        assemble_core(
            MagicMock(), module_coordinator=False, tool_module_events=False, module_vram_probe=MagicMock(), **_bare_kwargs(),
        )


# --- PR-3：真 assemble_core 的預算接線（需 DB；PR 閘門才跑）---

def test_db_budget_is_wired_into_coordinator_and_admits_gpu_load(session_factory, tmp_path, monkeypatch):
    import uep.assembly as assembly_mod
    from tests.uep.test_module_coordinator_resources import FakeProbe

    probe = FakeProbe(free=20000)
    desc = ModuleDescriptor("gpu_svc", uses_gpu=True, vram_mb=4000, autoload=True)
    handle = FakeHandle()
    monkeypatch.setattr(assembly_mod, "get_settings", lambda: UepSettings(UEP_MODULE_VRAM_HEADROOM_MB=2000))
    core, _ = _db_core(
        session_factory, tmp_path, module_coordinator=True, tool_module_events=True,
        module_registrations=[(desc, handle)], module_sink=lambda t, p: None, module_vram_probe=probe,
    )
    try:
        core.start()
        assert core.module_coordinator.wait_settled(5)
        assert core.module_coordinator.status("gpu_svc").state == "ready" and probe.reads >= 1
    finally:
        core.shutdown()


def test_db_budget_not_configured_rejects_gpu_load_but_keeps_services(session_factory, tmp_path):
    gpu_desc = ModuleDescriptor("gpu_svc", uses_gpu=True, vram_mb=4000, autoload=True)
    svc_desc = ModuleDescriptor("plain_svc", autoload=True)  # 不用 lore：真組裝下 lore 會被設定閘住
    core, _ = _db_core(
        session_factory, tmp_path, module_coordinator=True, tool_module_events=True,
        module_registrations=[(gpu_desc, FakeHandle()), (svc_desc, FakeHandle())], module_sink=lambda t, p: None,
    )
    try:
        core.start()
        assert core.module_coordinator.wait_settled(5)
        assert core.module_coordinator.status("plain_svc").state == "ready"
        assert core.module_coordinator.status("gpu_svc").state == "unloaded"
        assert core.module_coordinator.status("gpu_svc").admission_reason == "gpu_budget_unconfigured"
    finally:
        core.shutdown()
