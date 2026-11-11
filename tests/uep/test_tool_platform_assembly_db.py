"""組裝層：production 路徑確實把 Host 的投影平台帶到 AgentToolTurns、LoopLauncher 與 shadow 基準。需 PostgreSQL。"""
from __future__ import annotations

from tests.uep.echo_support import fast_speak, make_identity, make_session
from tests.uep.test_agent_loop_entry_db import _agent_pieces
from tests.uep.test_agent_tool_path import FakePort
from uep.assembly import assemble_core
from uep.control.channel import InProcessControlChannel
from uep.echo.host import EchoPipelineHost
from uep.events.bus import EventBus
from uep.events.publisher import EventPublisher
from uep.presence.epoch import HostEpochStore
from uep.stream.tracer import LatencyTracer
from uep.tools.catalog.memory import memory_registry


def _core(session_factory, tmp_path, **over):
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
    return core, host, think


def test_db_production_path_carries_host_platform(session_factory, tmp_path):
    core, host, think = _core(session_factory, tmp_path)
    try:
        # 注入的是 Host 的 projection_platform（Lease 與 location），bridge 與 launcher 兩條都要有
        assert think._port._platform_source == host.projection_platform
        assert core.loop_launcher._platform_source == host.projection_platform
        core.start()
        assert host.projection_platform() == "desktop" and think._port._current_platform() == "desktop"
        assert core.loop_launcher._projection_context(None).platform == "desktop"
    finally:
        core.shutdown()


def test_db_shadow_baseline_uses_host_platform(session_factory, tmp_path):
    core, host, _ = _core(session_factory, tmp_path, platform_shadow=True)
    try:
        production = host._platform_shadow._production
        assert production.projection_platform == "desktop"
        assert production.presence_active_platforms == frozenset({"desktop"})
    finally:
        core.shutdown()
