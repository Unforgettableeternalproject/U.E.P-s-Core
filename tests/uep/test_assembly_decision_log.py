"""組裝層接上決策日誌（無 DB：session_factory 為替身，DbSink 換成記憶體 sink，不碰資料庫）。"""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from tests.uep import echo_support
from tests.uep.echo_support import fast_think, make_identity, make_session
from uep import assembly
from uep.assembly import AssemblyError, assemble_core
from uep.config import get_settings
from uep.console.rig import build_fake_runtime
from uep.control.channel import InProcessControlChannel
from uep.decision.policy_publish import channel_mode_spec, policy_snapshot_spec
from uep.echo.host import EchoPipelineHost
from uep.events.bus import EventBus
from uep.events.publisher import EventPublisher
from uep.fakes.output import FakeOutputAdapter
from uep.fakes.tool import FakeToolExecutor
from uep.fakes.tool_spec import FakeToolSpecResolver
from uep.presence.epoch import HostEpochStore
from uep.presence.gate import ALLOW_ALL

KIND_POLICY_SNAPSHOT = policy_snapshot_spec().kind
KIND_CHANNEL_MODE = channel_mode_spec().kind


class MemorySink:
    """記憶體 sink：記錄寫入的日誌列（取代 DbSink）。"""

    def __init__(self) -> None:
        self.rows: list[dict] = []

    def write(self, rows) -> None:
        self.rows.extend(rows)


@pytest.fixture
def sinks(monkeypatch):
    """換掉組裝層的 DbSink，並收集所有建出的 sink。"""
    made: list[MemorySink] = []

    def factory(session_factory):
        sink = MemorySink()
        made.append(sink)
        return sink

    monkeypatch.setattr(assembly, "DbSink", factory)
    monkeypatch.setattr(get_settings(), "decision_log", "")
    return made


@pytest.fixture
def cores():
    made = []
    yield made
    for core in made:  # 收尾：關掉測試自己開的 writer
        core.shutdown()


def _host(publisher) -> EchoPipelineHost:
    return EchoPipelineHost(
        publisher=publisher, think=fast_think(None), speak=None, sink_factory=lambda v, t: "x",
        session=make_session(), identity=make_identity(),
    )


def _kw(tmp_path, publisher, bus, host=None, **over):
    base = {
        "bus": bus, "publisher": publisher, "control": InProcessControlChannel(),
        "adapter": host.adapter if host is not None else FakeOutputAdapter(),
        "executors": {"fake": FakeToolExecutor()}, "tool_resolver": FakeToolSpecResolver(**{"files.read": {}}),
        "turn_generation_source": host.generation_source if host is not None else ALLOW_ALL,
        "presence_audience": ALLOW_ALL, "allow_gate_bypass": True, "tool_path": "legacy",
        "epoch_store": HostEpochStore(tmp_path / "host_epoch.json"), "maintenance_interval": None,
        "echo": host,
    }
    base.update(over)
    return base


@pytest.fixture
def build(tmp_path, cores):
    def _build(*, with_echo=True, **over):
        bus = EventBus()
        publisher = EventPublisher(MagicMock(), bus)
        host = _host(publisher) if with_echo else None
        core = assemble_core(MagicMock(), **_kw(tmp_path, publisher, bus, host, **over))
        cores.append(core)
        return core, host

    return _build


def _observed(hub) -> bool:
    return len(hub._observers) > 0


def _policy_registered(hub) -> bool:
    return hub.engine(KIND_POLICY_SNAPSHOT) is not None and hub.engine(KIND_CHANNEL_MODE) is not None


# ---- 組裝：開關語意 ----

def test_default_env_unset_attaches_writer(build, sinks):
    core, host = build()
    assert core.decision_log is not None and core.decision_log.enabled
    assert _observed(host.decision_hub) and _policy_registered(host.decision_hub)
    assert len(sinks) == 1


def test_env_off_does_not_attach(build, sinks, monkeypatch):
    for value in ("0", "off", "False", "no"):
        monkeypatch.setattr(get_settings(), "decision_log", value)
        core, host = build()
        assert core.decision_log is None
        assert not _observed(host.decision_hub) and not _policy_registered(host.decision_hub)
    assert sinks == []


def test_flag_false_does_not_attach(build, sinks):
    core, host = build(decision_log=False)
    assert core.decision_log is None and not _observed(host.decision_hub) and sinks == []


def test_flag_true_overrides_env_off(build, sinks, monkeypatch):
    monkeypatch.setattr(get_settings(), "decision_log", "off")
    core, host = build(decision_log=True)
    assert core.decision_log is not None and _observed(host.decision_hub)


def test_no_echo_has_no_writer(build, sinks):
    core, _ = build(with_echo=False, decision_log=True)
    assert core.decision_log is None and sinks == []


def test_assembly_error_never_creates_writer(tmp_path, monkeypatch, sinks):
    created: list[object] = []
    monkeypatch.setattr(assembly, "DecisionLogWriter", lambda *a, **k: created.append(1))
    bus = EventBus()
    publisher = EventPublisher(MagicMock(), bus)
    host = _host(publisher)
    with pytest.raises(AssemblyError):  # Gate 的 generation_source 不是 echo 的
        assemble_core(MagicMock(), **_kw(tmp_path, publisher, bus, host, turn_generation_source=ALLOW_ALL))
    with pytest.raises(AssemblyError):  # 工具路徑不一致
        assemble_core(MagicMock(), **_kw(tmp_path, publisher, bus, host, tool_path="agent"))
    assert created == [] and sinks == []


# ---- 關閉順序與容錯 ----

def test_shutdown_order_echo_memory_then_decision_log(tmp_path, sinks, cores):
    bus = EventBus()
    publisher = EventPublisher(MagicMock(), bus)
    host = _host(publisher)
    memory = SimpleNamespace(publisher=publisher, start=lambda: None, stop=lambda: None, dream_enabled=False, dream_probe=None)
    core = assemble_core(MagicMock(), **_kw(tmp_path, publisher, bus, host, memory=memory))
    cores.append(core)
    core.shutdown()
    assert core.start_log == ["echo.stop", "memory.stop", "decision_log.close"]
    core.shutdown()  # 冪等：再關一次不拋錯
    assert not core.decision_log.enabled


def test_close_failure_is_logged_not_raised(build, sinks, caplog):
    core, _ = build()

    def boom(timeout=5.0):
        raise RuntimeError("close 失敗")

    core.decision_log.close = boom  # type: ignore[method-assign]
    core.shutdown()
    assert "決策日誌寫入器關閉失敗" in caplog.text and "decision_log.close" in core.start_log


def test_blocked_sink_does_not_hang_shutdown(build, sinks, monkeypatch):
    release = threading.Event()

    class Blocking:
        def write(self, rows) -> None:
            release.wait(10)

    monkeypatch.setattr(assembly, "DbSink", lambda sf: Blocking())
    monkeypatch.setattr(assembly, "DECISION_LOG_CLOSE_TIMEOUT", 0.3)
    core, _ = build()
    try:
        core.decision_log._enqueue({"decision_id": "dec_x"})  # 觸發背景執行緒並卡在 sink
        time.sleep(0.1)
        start = time.monotonic()
        core.shutdown()
        assert time.monotonic() - start < 3.0
    finally:
        release.set()


# ---- 實際寫入行為 ----

def test_rig_wires_writer_and_rows_reach_sink(tmp_path, sinks):
    """(a) console rig 開：core.decision_log 非 None、hub 有觀察者、兩個政策決策點已登記、決策會寫進 sink。"""
    rt = build_fake_runtime(MagicMock(), tmp_path / "rig", start=False)
    try:
        hub = rt.host.decision_hub
        assert rt.core.decision_log is not None and rt.decision_log is rt.core.decision_log
        assert _observed(hub) and _policy_registered(hub)
        rt.host.policy.publish()  # 送一筆政策發布決策
        assert rt.core.decision_log.flush(5.0)
        assert sinks and any(row.get("kind") in {KIND_POLICY_SNAPSHOT, KIND_CHANNEL_MODE} for row in sinks[0].rows)
    finally:
        rt.close()


def test_test_rig_default_is_off(tmp_path, sinks, monkeypatch):
    """(b) tests build_rig 預設關：decision_log 預設 False，core.decision_log 為 None、hub 無觀察者、政策未登記。"""
    captured: dict = {}
    real = echo_support.assemble_core

    def spy(session_factory, **kw):
        captured.update(kw)
        return real(session_factory, **kw)

    monkeypatch.setattr(echo_support, "assemble_core", spy)
    rig = echo_support.build_rig(MagicMock(), tmp_path, start=False)
    try:
        assert captured["decision_log"] is False
        assert rig.core.decision_log is None
        assert not _observed(rig.host.decision_hub) and not _policy_registered(rig.host.decision_hub)
    finally:
        rig.core.shutdown()
