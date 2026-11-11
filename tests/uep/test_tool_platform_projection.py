"""production 注入點的投影平台：ProjectionContext.platform 取自 Core 持有的 Presence 來源，不取自模型輸入。

純記憶體測試（無 DB）。涵蓋：``ProjectionContext.for_platform`` 的 fail-closed 語意、AgentToolTurns（bridge）、
LoopLauncher、Host.projection_platform 三個注入點。
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from tests.uep.test_tool_projection import ALL_TAGS, _caps, names
from uep.agent.bridge import AgentToolTurns, _Turn, _TurnDb
from uep.echo.agent_wiring import LoopLauncher
from uep.echo.host import EchoPipelineHost
from uep.tools.projection import ProjectionContext
from uep.tools.registry import ToolRegistry

GLOBAL_ONLY = ["deep_recall", "notify.user"]  # 全域工具；notify.user 的平台差異只在 variant，不在是否投出


@pytest.fixture()
def registry() -> ToolRegistry:
    return ToolRegistry.build(_caps())


# ---- ProjectionContext.for_platform ----

@pytest.mark.parametrize(("platform", "expected"), [
    ("desktop", ["deep_recall", "desktop.file_read", "notify.user", "presentation.propose"]),
    ("discord", ["deep_recall", "notify.user", "presentation.propose"]),  # desktop 限定工具不投
    ("website", ["deep_recall", "notify.user", "presentation.propose"]),
    (None, GLOBAL_ONLY),  # Presence 為 NONE：平台限定與 presence_bound 一律不投
])
def test_for_platform_projects_platform_scoped_tools_only_on_that_platform(registry, platform, expected):
    projected = registry.project(ALL_TAGS, ProjectionContext.for_platform(platform))
    assert sorted(names(projected)) == sorted(expected)


def test_for_platform_none_has_no_active_platforms_and_keeps_other_fields():
    ctx = ProjectionContext.for_platform(None, audience="group", available_context=frozenset({"guild_roster"}))
    assert ctx.platform is None and ctx.presence_active_platforms == frozenset() and not ctx.presence_active()
    assert ctx.audience == "group" and ctx.available_context == frozenset({"guild_roster"})


def test_global_tools_unchanged_without_platform(registry):
    legacy = registry.project(ALL_TAGS, ProjectionContext())
    assert sorted(names(legacy)) == sorted(names(registry.project(ALL_TAGS, ProjectionContext.for_platform(None))))


# ---- bridge（AgentToolTurns._run_tool）----

class _Stop(Exception):
    """spy 抓到投影脈絡後中止 _run_tool（不需要 DB／AgentCore）。"""


class _SpyRegistry:
    def __init__(self) -> None:
        self.ctx: ProjectionContext | None = None

    def project(self, tags: Any, ctx: ProjectionContext, **kw: Any) -> Any:
        self.ctx = ctx
        raise _Stop


def _bridge_ctx(source, context_extra: dict[str, Any] | None = None) -> ProjectionContext:
    spy = _SpyRegistry()
    turns = AgentToolTurns(None, spy, None, platform_source=source)  # type: ignore[arg-type]
    turns._db = lambda turn, context: _TurnDb("t", "r", "s", 1)  # type: ignore[method-assign]
    context = {"audience": "private", **(context_extra or {})}
    with pytest.raises(_Stop):
        turns._run_tool(_Turn("turn_1"), "deep_recall", {}, context)
    assert spy.ctx is not None
    return spy.ctx


def test_bridge_projects_with_presence_platform_from_source():
    ctx = _bridge_ctx(lambda: "desktop")
    assert ctx.platform == "desktop" and ctx.presence_active()


def test_bridge_without_source_or_none_is_fail_closed():
    assert _bridge_ctx(None).platform is None
    assert _bridge_ctx(lambda: None).platform is None


def test_bridge_source_failure_is_fail_closed():
    def boom() -> str:
        raise RuntimeError("lease gone")

    assert _bridge_ctx(boom).platform is None


def test_bridge_ignores_platform_claimed_in_context_or_model_args():
    # context 裡自帶 platform（例如模型或外部輸入塞入）不被採信，只認 platform_source
    assert _bridge_ctx(None, {"platform": "desktop", "current_platform": "desktop"}).platform is None
    assert _bridge_ctx(lambda: "discord", {"platform": "desktop"}).platform == "discord"


# ---- LoopLauncher ----

def _launcher(source) -> LoopLauncher:
    launcher = LoopLauncher.__new__(LoopLauncher)
    launcher._platform_source = source
    return launcher


def test_loop_launcher_projection_context_uses_source_each_call():
    state = {"platform": "desktop"}
    launcher = _launcher(lambda: state["platform"])
    ticket = SimpleNamespace(audience_scope="group")
    first = launcher._projection_context(ticket)
    assert first.platform == "desktop" and first.audience == "group"
    state["platform"] = None  # Presence 轉移後下一步不再帶原平台
    assert launcher._projection_context(ticket).platform is None


def test_loop_launcher_projection_context_fail_closed():
    def boom() -> str:
        raise RuntimeError("x")

    ticket = SimpleNamespace(audience_scope="private")
    assert _launcher(None)._projection_context(ticket).platform is None
    assert _launcher(boom)._projection_context(ticket).platform is None


# ---- Host.projection_platform ----

def _host(location: str, lease: object | None) -> EchoPipelineHost:
    host = EchoPipelineHost.__new__(EchoPipelineHost)
    host._location = location
    host.endpoint = SimpleNamespace(lease_auth=lambda: lease)
    return host


def test_host_projection_platform_follows_lease_and_location():
    assert _host("desktop", object()).projection_platform() == "desktop"
    assert _host("desktop", None).projection_platform() is None  # 沒有 Lease：她在主機，不在任何平台
    assert _host("somewhere_else", object()).projection_platform() is None  # 未知 location fail-closed
