"""PR-4 C3：requires 與描述子的雙向覆蓋檢查、探活間隔設定（離線；標 _db 的需要 UEP_TEST_DATABASE_URL）。"""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from tests.uep.module_fakes import FakeHandle
from tests.uep.test_module_coordinator_assembly import _db_core, _s_group
from uep.assembly import AssemblyError, check_module_coverage
from uep.config import UepSettings
from uep.lifecycle.descriptor import ModuleDescriptor
from uep.tools.catalog.chatroom import with_chatroom
from uep.tools.catalog.lore import with_lore
from uep.tools.catalog.memory import memory_registry
from uep.tools.module_handles import S_GROUP_MODULES, build_s_group_registrations
from uep.tools.web_image import with_web_image


def entry(*requires: str) -> SimpleNamespace:
    return SimpleNamespace(requires=tuple(requires))


def reg(module_id: str, kind: str = "service") -> tuple[ModuleDescriptor, FakeHandle]:
    return ModuleDescriptor(module_id, kind=kind, pinned=kind in ("model", "stage")), FakeHandle()


# --- 純函式 ---


def test_forward_missing_requires_hard_fails_only_when_enforced(caplog: pytest.LogCaptureFixture) -> None:
    entries = {"a.tool": entry("svc_a", "svc_missing"), "b.tool": entry()}
    with pytest.raises(AssemblyError, match="a.tool->svc_missing"):
        check_module_coverage(entries, {"svc_a"}, [reg("svc_a")], enforce=True)
    with caplog.at_level(logging.WARNING, logger="uep.assembly"):
        missing, _ = check_module_coverage(entries, {"svc_a"}, [reg("svc_a")], enforce=False)  # 觀測模式：只警告
    assert missing == [("a.tool", "svc_missing")] and "svc_missing" in caplog.text


def test_reverse_unreferenced_service_is_reported_and_m_group_exempt(caplog: pytest.LogCaptureFixture) -> None:
    entries = {"a.tool": entry("svc_a")}
    regs = [reg("svc_a"), reg("orphan_svc"), reg("model:x", "model"), reg("stage:y", "stage"), reg("platform_z", "platform")]
    with caplog.at_level(logging.WARNING, logger="uep.assembly"):
        missing, unreferenced = check_module_coverage(entries, {d.module_id for d, _ in regs}, regs, enforce=True)
    assert missing == [] and unreferenced == ["orphan_svc"]  # 只有 service 受反向檢查；M 群與 platform 不報
    assert "orphan_svc" in caplog.text


def test_clean_coverage_returns_empty() -> None:
    entries = {"a.tool": entry("svc_a"), "b.tool": entry("svc_a")}
    assert check_module_coverage(entries, {"svc_a"}, [reg("svc_a")], enforce=True) == ([], [])


# --- 真實 catalog 與 S 群預設表互相對得上 ---


def test_real_catalog_and_s_group_cover_each_other() -> None:
    settings = UepSettings(_env_file=None, lore_url="http://lore.invalid", lore_token=SecretStr("t"))
    registry = with_chatroom(memory_registry(), configured=True)
    registry, _ = with_web_image(registry, settings)
    registry = with_lore(registry, configured=True, delete_enabled=True)
    registrations = build_s_group_registrations(settings)
    known = {d.module_id for d, _ in registrations}
    missing, unreferenced = check_module_coverage(registry.entries(), known, registrations, enforce=True)
    assert missing == [] and unreferenced == []
    referenced = {m for e in registry.entries().values() for m in e.requires}
    assert referenced == set(S_GROUP_MODULES)  # catalog 的 requires 恰好就是 S 群，沒有多也沒有少


# --- 設定 ---


def test_probe_interval_setting_defaults_to_zero_and_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("UEP_MODULE_PROBE_INTERVAL_S", raising=False)
    assert UepSettings(_env_file=None).module_probe_interval_s == 0.0
    monkeypatch.setenv("UEP_MODULE_PROBE_INTERVAL_S", "7.5")
    assert UepSettings(_env_file=None).module_probe_interval_s == 7.5
    with pytest.raises(ValueError):
        UepSettings(_env_file=None, UEP_MODULE_PROBE_INTERVAL_S=-1)


# --- 真 assemble_core（需 DB）---


def test_db_driving_registry_without_s_group_is_assembly_error(session_factory, tmp_path):
    with pytest.raises(AssemblyError, match="requires"):
        _db_core(
            session_factory, tmp_path, module_coordinator=True, tool_module_events=True,
            module_registrations=[(ModuleDescriptor("plain_svc", autoload=True), FakeHandle())], module_sink=lambda t, p: None,
        )


def test_db_observe_mode_only_warns_about_missing_requires(session_factory, tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="uep.assembly"):
        core, _ = _db_core(
            session_factory, tmp_path, module_coordinator=True, tool_module_events=False,
            module_registrations=[(ModuleDescriptor("plain_svc", autoload=True), FakeHandle())], module_sink=lambda t, p: None,
        )
    try:
        assert core.module_coordinator.mode == "observe"
        assert "requires 含未登記" in caplog.text and "plain_svc" in caplog.text
    finally:
        core.shutdown()


def test_db_probe_interval_is_passed_to_coordinator(session_factory, tmp_path, monkeypatch):
    import uep.assembly as assembly_mod

    monkeypatch.setattr(assembly_mod, "get_settings", lambda: UepSettings(_env_file=None, UEP_MODULE_PROBE_INTERVAL_S=9.0))
    core, _ = _db_core(
        session_factory, tmp_path, module_coordinator=True, tool_module_events=True,
        module_registrations=_s_group(), module_sink=lambda t, p: None,
    )
    try:
        assert core.module_coordinator._probe_interval == 9.0  # noqa: SLF001
    finally:
        core.shutdown()
    monkeypatch.setattr(assembly_mod, "get_settings", lambda: UepSettings(_env_file=None))
    core, _ = _db_core(
        session_factory, tmp_path, module_coordinator=True, tool_module_events=True,
        module_registrations=_s_group(), module_sink=lambda t, p: None,
    )
    try:
        assert core.module_coordinator._probe_interval == 0.0  # noqa: SLF001  預設 = 現況
    finally:
        core.shutdown()
