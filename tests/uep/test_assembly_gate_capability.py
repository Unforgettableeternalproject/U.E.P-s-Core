"""組裝層把出站端點的 interaction 宣告接進 Output Gate（平台端點契約 E3；不需 DB，session 以 MagicMock 代替）。

宣告來源：ES 內建端點與 endpoint socket 的 secrets 鍵；其餘 endpoint_id 查無宣告，shadow 下計數。預設仍是 shadow。
"""
from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest

from tests.uep.echo_support import fast_think, make_identity, make_session
from uep.assembly import assemble_core
from uep.control.channel import InProcessControlChannel
from uep.echo.host import EchoPipelineHost
from uep.events.bus import EventBus
from uep.events.publisher import EventPublisher
from uep.fakes.output import FakeOutputAdapter
from uep.fakes.tool import FakeToolExecutor
from uep.fakes.tool_spec import FakeToolSpecResolver
from uep.presence.epoch import HostEpochStore
from uep.presence.gate import ALLOW_ALL


@pytest.fixture
def build(tmp_path):
    made = []

    def _build(*, with_echo=True, **over):
        bus = EventBus()
        publisher = EventPublisher(MagicMock(), bus)
        host = None
        if with_echo:
            host = EchoPipelineHost(
                publisher=publisher, think=fast_think(None), speak=None, sink_factory=lambda v, t: "x",
                session=make_session(), identity=make_identity(),
            )
        kw = {
            "bus": bus, "publisher": publisher, "control": InProcessControlChannel(),
            "adapter": host.adapter if host is not None else FakeOutputAdapter(),
            "executors": {"fake": FakeToolExecutor()}, "tool_resolver": FakeToolSpecResolver(**{"files.read": {}}),
            "turn_generation_source": host.generation_source if host is not None else ALLOW_ALL,
            "presence_audience": ALLOW_ALL, "allow_gate_bypass": True, "tool_path": "legacy", "decision_log": False,
            "epoch_store": HostEpochStore(tmp_path / "host_epoch.json"), "maintenance_interval": None, "echo": host,
        }
        kw.update(over)
        core = assemble_core(MagicMock(), **kw)
        made.append(core)
        return core

    yield _build
    for core in made:
        core.shutdown()


def test_echo_endpoint_is_declared_interaction_and_not_counted(build):
    core = build()
    assert core.gate._has_interaction("ep_echo")
    assert core.gate.not_interaction_count == 0


def test_socket_endpoints_declared_interaction(build):
    core = build(endpoint_socket_port=0, endpoint_secrets={"ep_cli": b"s" * 32, "ep_discord": b"t" * 32})
    assert core.gate._has_interaction("ep_cli") and core.gate._has_interaction("ep_discord")


def test_undeclared_endpoint_still_counts(build):
    core = build(endpoint_socket_port=0, endpoint_secrets={"ep_cli": b"s" * 32})
    assert not core.gate._has_interaction("ep_stranger")
    assert core.gate._has_interaction("ep_echo")  # 內建端點不受影響
    core.gate._check_interaction("ep_stranger")  # 預設 shadow：回 None（放行）並計數
    assert core.gate.not_interaction_by_endpoint() == {"ep_stranger": 1}


def test_no_echo_no_secrets_declares_nothing(build):
    core = build(with_echo=False)
    assert not core.gate._has_interaction("ep_echo")


def test_endpoint_with_unsafe_id_is_not_declared_but_assembles(build):
    core = build(endpoint_socket_port=0, endpoint_secrets={"EP_Upper": b"s" * 32})
    assert not core.gate._has_interaction("EP_Upper")


def test_default_stays_shadow(build, monkeypatch):
    monkeypatch.delenv("UEP_GATE_REQUIRE_INTERACTION", raising=False)
    core = build()
    assert core.gate._require_interaction is False
    assert core.gate._check_interaction("ep_stranger") is None  # 未宣告也不拒絕


def test_shutdown_logs_shadow_summary_once(build, caplog):
    core = build()
    core.gate._check_interaction("ep_stranger")
    with caplog.at_level(logging.INFO, logger="uep.assembly"):
        core.shutdown()
        core.shutdown()  # 重複呼叫不應壞掉
    lines = [r.getMessage() for r in caplog.records if "Output Gate shadow 摘要" in r.getMessage()]
    assert lines and "not_interaction=1" in lines[0] and "ep_stranger" in lines[0]
