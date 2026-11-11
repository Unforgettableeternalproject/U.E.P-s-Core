"""組裝層：原生工具（lore／chatroom／web／image）經 assemble_core 的 agent 路徑，投影與執行看同一份註冊表。需 PostgreSQL。

驗證：direct_chat 投影（Host 的 ``think.tools``）只含唯讀原生工具；agent_loop 的投影含 route 表允許的寫入類；
未設定的工具不投出；TaskManager／Gateway／loop 與 Host 用的是同一個 ToolRegistry 實例。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from tests.uep.test_tool_platform_assembly_db import _core
from uep.config import get_settings
from uep.decision.capability import ROUTE_DEFAULT_CAPABILITIES
from uep.echo.agent_wiring import make_capability_tags_for
from uep.tools.projection import ProjectionContext

LORE_READ = {"lore.recall", "lore.get", "lore.list"}
LORE_WRITE = {"lore.write", "lore.update"}
CHATROOM_READ = {"chatroom.read", "chatroom.wait"}


def _configure(monkeypatch, *, lore=False, chatroom=False, web=True, lore_delete=False) -> None:
    settings = get_settings()
    # 先清掉可能來自環境的設定，再依參數開啟（fake 值只用於組裝，不會連線）
    for name, value in (
        ("lore_url", None), ("lore_token", None), ("lore_dev_vault", None), ("lore_vaults", None),
        ("chatroom_url", None), ("chatroom_session_key", None), ("chatroom_token", None), ("chatroom_rooms", None),
        ("chatroom_token_file", None), ("lore_delete_enabled", lore_delete),
    ):
        monkeypatch.setattr(settings, name, value)
    if lore:
        monkeypatch.setattr(settings, "lore_url", "http://127.0.0.1:9")
        monkeypatch.setattr(settings, "lore_token", SecretStr("fake-token"))
        monkeypatch.setattr(settings, "lore_dev_vault", "fake/vault")
    if chatroom:
        monkeypatch.setattr(settings, "chatroom_url", "http://127.0.0.1:9")
        monkeypatch.setattr(settings, "chatroom_session_key", "fake-key")
        monkeypatch.setattr(settings, "chatroom_token", SecretStr("fake-token"))
        monkeypatch.setattr(settings, "chatroom_rooms", "room_fake")
    if not web:
        monkeypatch.setattr(settings, "web_fetch_enabled", False)
        monkeypatch.setattr(settings, "web_search_providers", "")
        monkeypatch.setattr(settings, "web_search_openai_fallback", False)


def _direct_chat_names(think) -> set[str]:
    return {t["function"]["name"] for t in think.tools}


def _loop_names(core, host) -> set[str]:
    registry = core.loop_launcher._registry
    tags = make_capability_tags_for(registry)(
        SimpleNamespace(turn_id="t1"), SimpleNamespace(route="agent_loop", capability_hint=None),
    )
    ctx = ProjectionContext.for_platform("desktop", audience="scope_private_owner")
    return {t["tool"] for t in registry.project(tags, ctx)["tools"]}


def test_db_configured_native_tools_are_projected_per_route(session_factory, tmp_path, monkeypatch):
    _configure(monkeypatch, lore=True, chatroom=True, web=True)
    core, host, think = _core(session_factory, tmp_path)
    try:
        core.start()
        direct = _direct_chat_names(think)
        assert LORE_READ <= direct and CHATROOM_READ <= direct
        assert {"web.search", "web.fetch"} <= direct
        assert not (direct & (LORE_WRITE | {"lore.delete", "chatroom.post"}))  # direct_chat 不含寫入類
        loop = _loop_names(core, host)
        assert (LORE_WRITE | {"chatroom.post"} | LORE_READ) <= loop
        assert "lore.delete" not in loop  # 刪除開關預設關閉
    finally:
        core.shutdown()


def test_db_lore_delete_only_when_switch_enabled(session_factory, tmp_path, monkeypatch):
    _configure(monkeypatch, lore=True, lore_delete=True, web=False)
    core, host, think = _core(session_factory, tmp_path)
    try:
        core.start()
        assert "lore.delete" in _loop_names(core, host)
        assert "lore.delete" not in _direct_chat_names(think)
    finally:
        core.shutdown()


def test_db_unconfigured_native_tools_are_not_projected(session_factory, tmp_path, monkeypatch):
    _configure(monkeypatch, web=False)
    core, host, think = _core(session_factory, tmp_path)
    try:
        core.start()
        native = LORE_READ | LORE_WRITE | CHATROOM_READ | {"lore.delete", "chatroom.post", "web.search", "web.fetch"}
        assert not (_direct_chat_names(think) & native)
        assert not (_loop_names(core, host) & native)
    finally:
        core.shutdown()


def test_db_projection_and_execution_share_one_registry(session_factory, tmp_path, monkeypatch):
    _configure(monkeypatch, lore=True, chatroom=True)
    core, host, think = _core(session_factory, tmp_path)
    try:
        tasks_registry = core.tasks.tool_resolver
        assert host._tool_registry is tasks_registry
        assert think._port._registry is tasks_registry
        assert core.loop_launcher._registry is tasks_registry
        assert think._port._tags == ROUTE_DEFAULT_CAPABILITIES["direct_chat"]  # 回合工具 Port 的標籤取自 route 表
        # 投影出來的工具，TaskManager 的 resolver 都查得到
        for name in _direct_chat_names(think) | _loop_names(core, host):
            assert name in tasks_registry.entries()
    finally:
        core.shutdown()


@pytest.mark.parametrize("module", ["lore", "chatroom"])
def test_db_module_event_reaches_assembled_registry(session_factory, tmp_path, monkeypatch, module):
    # 就緒事件接到組裝後的註冊表：lore／chatroom 的條目要存在於該註冊表（先前只接到記憶註冊表）
    _configure(monkeypatch, lore=True, chatroom=True)
    core, host, think = _core(session_factory, tmp_path)
    try:
        assert any(name.startswith(module + ".") for name in core.tasks.tool_resolver.entries())
    finally:
        core.shutdown()
