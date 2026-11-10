"""做夢迴圈接進組裝（設定 -> wiring -> MemoryHost -> assemble_core 注入 Probe）。

- 設定與 wiring 不需 DB；
- 組裝案例需 PostgreSQL（未設 UEP_TEST_DATABASE_URL 時 skip）。
"""
from __future__ import annotations

import time
from dataclasses import replace
from datetime import UTC, datetime
from datetime import time as dtime
from pathlib import Path

import pytest

from uep.config import UepSettings
from uep.echo.memory_wiring import (
    DEFAULT_ENGINE_EMBEDDING_MODEL,
    build_dream_loop_config,
    build_memory_wiring,
)
from uep.events.bus import EventBus
from uep.events.publisher import EventPublisher
from uep.fakes.memory import FakeEngineFactory
from uep.memory import IdentityDirectory, MemoryHost, NamespaceSpec
from uep.memory.dream_planner import DreamLoopConfig, FixedWindowPolicy

NS = "core_owner"
DREAM_ENV = (
    "UEP_DREAM_ENABLED", "UEP_DREAM_SHADOW", "UEP_DREAM_WINDOW_START", "UEP_DREAM_WINDOW_END", "UEP_DREAM_WINDOW_TZ",
)


def settings_from(monkeypatch, **env: str) -> UepSettings:
    for name in DREAM_ENV:
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return UepSettings(_env_file=None)


# ---- 設定 -> DreamLoopConfig ----

def test_defaults_enabled_and_shadow_with_default_window(monkeypatch):
    config, policy = build_dream_loop_config(settings_from(monkeypatch))
    assert config.enabled is True and config.shadow is True  # D100：預設啟用但先 shadow
    assert policy is None  # 沒覆寫窗口＝DreamLoop 用 FixedWindowPolicy 預設（03:00–05:30 Asia/Taipei）


def test_flags_and_window_overrides(monkeypatch):
    settings = settings_from(
        monkeypatch, UEP_DREAM_ENABLED="0", UEP_DREAM_SHADOW="false", UEP_DREAM_WINDOW_START="09:00",
        UEP_DREAM_WINDOW_END="18:30", UEP_DREAM_WINDOW_TZ="UTC",
    )
    config, policy = build_dream_loop_config(settings)
    assert config.enabled is False and config.shadow is False
    assert policy == FixedWindowPolicy(start=dtime(9, 0), end=dtime(18, 30), tz="UTC")
    assert policy.window_at(datetime(2026, 11, 11, 12, 0, tzinfo=UTC)) is not None


def test_partial_window_override_keeps_the_other_default(monkeypatch):
    _, policy = build_dream_loop_config(settings_from(monkeypatch, UEP_DREAM_WINDOW_START="13:00"))
    assert (policy.start, policy.end, policy.tz) == (dtime(13, 0), dtime(5, 30), "Asia/Taipei")  # 跨午夜


@pytest.mark.parametrize("env", [
    {"UEP_DREAM_WINDOW_START": "不是時間"},
    {"UEP_DREAM_WINDOW_START": "03:00", "UEP_DREAM_WINDOW_END": "03:00"},
    {"UEP_DREAM_WINDOW_TZ": "Nowhere/Land"},
])
def test_bad_window_fails_fast(monkeypatch, env):
    with pytest.raises(Exception):  # noqa: B017 - ValueError／ZoneInfoNotFoundError：重點是不靜默退回預設
        build_dream_loop_config(settings_from(monkeypatch, **env))


def test_engine_embedding_default_matches_settings_default():
    assert DEFAULT_ENGINE_EMBEDDING_MODEL == "BAAI/bge-m3"
    assert UepSettings(_env_file=None).memory_embedding_model == DEFAULT_ENGINE_EMBEDDING_MODEL


# ---- wiring／real_rig 把 DreamLoopConfig 交給 MemoryHost ----

def test_wiring_host_kwargs_carry_dream_loop_only_when_given():
    assert "dream_loop" not in build_memory_wiring().host_kwargs()
    config, policy = DreamLoopConfig(enabled=True), FixedWindowPolicy()
    kwargs = build_memory_wiring(dream_loop=config, dream_rest_policy=policy).host_kwargs()
    assert kwargs["dream_loop"] is config and kwargs["dream_rest_policy"] is policy


def test_real_rig_builds_memory_host_with_dream_loop(monkeypatch, tmp_path):
    from uep.console import real_rig

    settings = settings_from(monkeypatch, UEP_DREAM_WINDOW_START="09:00", UEP_DREAM_WINDOW_END="18:00")
    host = real_rig.build_memory_host(
        settings, tmp_path / "mem", IdentityDirectory({}, {}), publisher=None, session_factory=None, warmups={},
    )
    assert host.dream_enabled  # 建構不載模型、不啟動；只檢查帶進 Host 的設定
    assert host._dream_config.shadow is True
    assert host._dream_rest_policy == FixedWindowPolicy(start=dtime(9, 0), end=dtime(18, 0), tz="Asia/Taipei")
    off = settings_from(monkeypatch, UEP_DREAM_ENABLED="0")
    host_off = real_rig.build_memory_host(
        off, tmp_path / "mem2", IdentityDirectory({}, {}), publisher=None, session_factory=None, warmups={},
    )
    assert not host_off.dream_enabled


# ---- 組裝：Probe 注入、啟停、shadow ----

FAST = DreamLoopConfig(
    enabled=True, shadow=True, startup_grace_s=0.0, tick_interval_s=0.01, poll_interval_s=0.01, idle_quiet_s=0.01,
    daydream_quiet_s=0.01, rest_quiet_s=0.01, min_gap_s=0.05,
)


def assemble(session_factory, tmp_path: Path, dream_loop: DreamLoopConfig | None, **host_over):
    from tests.uep.test_assembly import kwargs as assembly_kwargs
    from uep.assembly import assemble_core

    bus = EventBus()
    publisher = EventPublisher(session_factory, bus)
    factory = FakeEngineFactory()
    host = MemoryHost(
        engine_factory=factory, data_root=tmp_path / "mem", namespaces=(NamespaceSpec(NS, owner_id="owner"),),
        identity=IdentityDirectory({}, {}), publisher=publisher, llm_fn=lambda p: "ok", drain_timeout_s=1.0,
        dream_loop=dream_loop, **host_over,
    )
    core = assemble_core(
        session_factory, **assembly_kwargs(session_factory, tmp_path, bus=bus, publisher=publisher, memory=host),
    )
    return core, host, factory


def wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def would_run(host: MemoryHost) -> int:
    return sum(v for k, v in host.metrics.snapshot().items() if k.startswith("memory.dream.shadow_would_run."))


@pytest.mark.usefixtures("session_factory")
def test_assembly_gives_memory_host_a_dream_loop_and_probe(session_factory, tmp_path):
    core, host, _ = assemble(session_factory, tmp_path, FAST)
    try:
        assert core.dream_probe is not None and host.dream_probe is core.dream_probe
        core.start()
        assert host.dream_loop is not None and host.dream_status()["running"] is True
        log = core.start_log
        assert log.index("dream_probe.start") < log.index("memory.start")
    finally:
        core.shutdown()
    assert host.dream_loop is None and not host.started
    assert core.start_log.index("memory.stop") < core.start_log.index("dream_probe.close")


@pytest.mark.usefixtures("session_factory")
def test_assembly_with_dream_disabled_starts_no_loop_and_no_probe(session_factory, tmp_path):
    core, host, _ = assemble(session_factory, tmp_path, replace(FAST, enabled=False))
    try:
        assert core.dream_probe is None
        core.start()
        assert host.dream_loop is None and host.dream_status() == {"enabled": False}
    finally:
        core.shutdown()


@pytest.mark.usefixtures("session_factory")
def test_assembly_without_dream_config_behaves_as_before(session_factory, tmp_path):
    core, host, _ = assemble(session_factory, tmp_path, None)
    try:
        assert core.dream_probe is None
        core.start()
        assert host.dream_loop is None
    finally:
        core.shutdown()


@pytest.mark.usefixtures("session_factory")
def test_assembly_keeps_a_probe_the_host_already_has(session_factory, tmp_path):
    from tests.uep.test_dream_loop import Probe

    own = Probe()
    core, host, _ = assemble(session_factory, tmp_path, FAST, dream_probe=own)
    try:
        assert core.dream_probe is None and host.dream_probe is own
    finally:
        core.shutdown()


@pytest.mark.usefixtures("session_factory")
def test_shadow_loop_decides_through_real_probe_but_never_runs_dream(session_factory, tmp_path):
    core, host, factory = assemble(session_factory, tmp_path, FAST)
    try:
        core.start()
        factory.engines[NS].readiness = {
            "pending": 9, "min_pending": 5, "daydream_pending": 20, "suggested_trigger": None,
        }
        assert wait_until(lambda: would_run(host) >= 1), host.dream_status()
        assert host.dream_status()["shadow"] is True
        assert "dream" not in factory.calls(NS)  # shadow：只決策，不呼叫 engine.dream()
    finally:
        core.shutdown()
    assert "dream" not in factory.calls(NS)
