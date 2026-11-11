"""平台宣告注入端（R0 第 1～4 步，零行為變化）：型別、映射表、provider 版本判定、F-1 退路、shadow 差異、鎖測試。

鎖測試保證：三個消費點（ProjectionContext、loop_consult、ProactiveContext）的實際輸入與 develop 相同，
platform 脈絡只被 shadow 讀取，不被任何消費點使用。
"""
from __future__ import annotations

import ast
import logging
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from uep.contracts.platform import (
    LEGACY_PLATFORM_ALIASES,
    ORIGIN_COARSE_FALLBACK,
    ORIGIN_REGISTRY_STATIC,
    SAFE_REF,
    ExecutorDescriptor,
    PlatformContext,
    PlatformDeclaration,
    platform_from_endpoint_location,
    platform_from_presence_location,
    normalize_platform,
    platform_from_session,
)
from uep.contracts.presence import PresenceLocation, PresenceStatus
from uep.decision.loop_consult import _SAFE_REF as LOOP_CONSULT_SAFE_REF
from uep.echo.metrics import Metrics
from uep.echo.platform_context import (
    CoarseExecutorSource,
    PlatformContextProvider,
    PlatformShadow,
    ProductionInputs,
    diff_against_production,
)
from uep.presence.manager import PresenceNotStarted, PresenceView

UEP_ROOT = Path(__file__).resolve().parents[2] / "uep"
NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)


def _view(status=PresenceStatus.ACTIVE, location="desktop", generation=1, row_version=3, expires_at=None) -> PresenceView:
    return PresenceView(
        persona_id="uep", status=status, location=location, endpoint_id="ep", lease_id="l1", core_epoch=1,
        fencing_epoch=1, generation=generation, expires_at=expires_at, row_version=row_version, transfer_id=None,
        unquiesced_endpoint_id=None, recovery_reason=None,
    )


class FakePresence:
    def __init__(self, view: PresenceView | Exception) -> None:
        self.view = view
        self.calls = 0

    def peek(self) -> PresenceView:
        self.calls += 1
        if isinstance(self.view, Exception):
            raise self.view
        return self.view


class FakeExecutors:
    def __init__(self, refs: dict[str, list[str]] | Exception) -> None:
        self.refs = refs

    def executors_for(self, platform):
        if isinstance(self.refs, Exception):
            raise self.refs
        return tuple(
            ExecutorDescriptor(r, r.split(":")[0], platform, ORIGIN_REGISTRY_STATIC) for r in self.refs.get(platform, [])
        )


def _provider(view, executors=None, **kw) -> tuple[PlatformContextProvider, FakePresence]:
    presence = FakePresence(view)
    return PlatformContextProvider(presence, executors, clock=lambda: NOW, **kw), presence


# ---- 型別與映射表 ----

def test_safe_ref_charset_matches_loop_consult():
    assert SAFE_REF.pattern == LOOP_CONSULT_SAFE_REF.pattern


def test_types_are_frozen_and_validated():
    ctx = PlatformContext(
        version=1, presence_generation=0, presence_row_version=0, presence_status=PresenceStatus.NONE,
        current_platform=None, presence_active_platforms=frozenset(), session_platform=None, available_executors=(),
    )
    with pytest.raises(FrozenInstanceError):
        ctx.version = 2  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        ExecutorDescriptor("a:b", "a", "b", ORIGIN_REGISTRY_STATIC).tool = "x"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        PlatformDeclaration("discord", True).online = False  # type: ignore[misc]


@pytest.mark.parametrize("ref", ["tool@discord", "Tool:discord", "", "a" * 65, "tool discord"])
def test_executor_ref_rejects_unsafe_values(ref):
    with pytest.raises(ValueError):
        ExecutorDescriptor(ref, "tool", "discord", ORIGIN_REGISTRY_STATIC)


def test_executor_ref_accepts_tool_colon_platform():
    d = ExecutorDescriptor("member_recall:discord", "member_recall", "discord", ORIGIN_REGISTRY_STATIC)
    assert SAFE_REF.fullmatch(d.executor_ref)


def test_descriptor_and_declaration_validation():
    with pytest.raises(ValueError):
        ExecutorDescriptor("a:b", "a", "b", "unknown_origin")
    with pytest.raises(ValueError):
        ExecutorDescriptor("a:b", "", "b", ORIGIN_REGISTRY_STATIC)
    with pytest.raises(ValueError):
        PlatformDeclaration("Discord", True)
    with pytest.raises(ValueError):
        PlatformDeclaration("discord", True, declaration_revision=-1)


def test_platform_context_validation():
    base = {
        "version": 1, "presence_generation": 0, "presence_row_version": 0,
        "presence_status": PresenceStatus.NONE, "session_platform": None,
    }
    with pytest.raises(ValueError):
        PlatformContext(**base, current_platform=None, presence_active_platforms=frozenset({"desktop"}), available_executors=())
    with pytest.raises(ValueError):
        PlatformContext(**base, current_platform="desktop", presence_active_platforms=frozenset({"desktop"}), available_executors=("a@b",))


def test_legacy_alias_table_only_maps_web_to_website():
    # DQ-3-B：詞彙已統一為 PresenceLocation；相容層只剩 web -> website
    assert dict(LEGACY_PLATFORM_ALIASES) == {"web": "website"}
    assert all(SAFE_REF.fullmatch(v) for v in LEGACY_PLATFORM_ALIASES.values())


def test_mapping_functions():
    assert platform_from_session("web") == "website"  # 舊值經相容層
    assert platform_from_session(" Web ") == "website"
    for loc in PresenceLocation:
        assert platform_from_session(loc.value) == loc.value
        assert platform_from_presence_location(loc.value) == loc.value
        assert platform_from_endpoint_location(loc.value) == loc.value
    assert platform_from_session("unknown") is None and platform_from_session(None) is None
    assert platform_from_presence_location("mars") is None and platform_from_presence_location(None) is None
    assert platform_from_endpoint_location("web") is None  # endpoint 已用新詞彙，不收舊值


def test_normalize_platform_only_rewrites_legacy_web():
    assert normalize_platform("web") == "website"
    assert normalize_platform("WEB ") == "website"
    for value in ("website", "desktop", "discord", "mars", ""):
        assert normalize_platform(value) == value
    assert normalize_platform(None) is None


def test_session_context_default_and_legacy_web_normalized():
    from uep.turn.models import SessionContext

    base = dict(conversation_id="c", segment_id="s", audience_scope="a", session_ref="r")
    assert SessionContext(**base).platform == "website"
    assert SessionContext(**base, platform="web").platform == "website"  # 舊值讀取相容
    for loc in PresenceLocation:
        assert SessionContext(**base, platform=loc.value).platform == loc.value


# ---- provider：三態與失敗 ----

def test_active_state_yields_platform_and_coarse_executor_by_default():
    provider, _ = _provider(_view(location="discord"))
    ctx = provider.snapshot("website")
    assert ctx.presence_status is PresenceStatus.ACTIVE
    assert ctx.current_platform == "discord" and ctx.presence_active_platforms == frozenset({"discord"})
    assert ctx.session_platform == "website"  # 兩者並存、不混用（DQ-1 暫採 C）
    assert ctx.available_executors == ("discord",)  # F-1：預設來源只回 current_platform


@pytest.mark.parametrize("status", [PresenceStatus.NONE, PresenceStatus.RECOVERING])
def test_non_active_states_have_no_platform(status):
    provider, _ = _provider(_view(status=status, location="desktop"))
    ctx = provider.snapshot()
    assert ctx.current_platform is None and ctx.presence_active_platforms == frozenset()
    assert ctx.available_executors == () and ctx.presence_status is status


def test_expired_active_lease_is_treated_as_none():
    provider, _ = _provider(_view(expires_at=NOW - timedelta(seconds=1)))
    ctx = provider.snapshot()
    assert ctx.presence_status is PresenceStatus.NONE and ctx.current_platform is None


def test_unmappable_location_gives_no_platform():
    provider, _ = _provider(_view(location="mars"))
    assert provider.snapshot().current_platform is None


@pytest.mark.parametrize("error", [PresenceNotStarted("x"), RuntimeError("db down")])
def test_presence_failure_is_fail_closed(error, caplog):
    provider, _ = _provider(error)
    with caplog.at_level(logging.WARNING, logger="uep.echo.platform_context"):
        ctx = provider.snapshot("website")
    assert ctx.presence_status is PresenceStatus.NONE and ctx.current_platform is None
    assert ctx.available_executors == () and ctx.session_platform == "website"
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def test_snapshot_reads_presence_once_per_call():
    provider, presence = _provider(_view())
    provider.snapshot()
    assert presence.calls == 1


# ---- provider：版本判定（status + row_version）----

def test_version_stable_for_same_input_and_bumps_on_each_relevant_change():
    provider, presence = _provider(_view(row_version=3))
    v1 = provider.snapshot().version
    assert provider.snapshot().version == v1  # 相同輸入不遞增
    presence.view = _view(row_version=4)  # generation 不變，只有 row_version（例如 renew）也算變動
    v2 = provider.snapshot().version
    assert v2 == v1 + 1
    presence.view = _view(status=PresenceStatus.NONE, row_version=4)  # 同 generation、同 row_version，只有 status 變
    assert provider.snapshot().version == v2 + 1
    presence.view = _view(status=PresenceStatus.NONE, row_version=5)  # 撤銷／過期不遞增 generation，但 row_version 會
    assert provider.snapshot().version == v2 + 2


def test_version_bumps_on_generation_location_executors_and_registry_revision():
    registry_rev = {"v": 0}
    executors = FakeExecutors({"desktop": ["a:desktop"], "discord": ["a:discord"]})
    provider, presence = _provider(_view(), executors, registry_revision=lambda: registry_rev["v"])
    v = provider.snapshot().version
    presence.view = _view(generation=2)
    assert provider.snapshot().version == v + 1
    presence.view = _view(generation=2, location="discord")
    assert provider.snapshot().version == v + 2
    registry_rev["v"] = 1
    assert provider.snapshot().version == v + 3
    executors.refs = {"discord": ["a:discord", "b:discord"]}
    assert provider.snapshot().version == v + 4


def test_session_platform_alone_does_not_bump_version():
    provider, _ = _provider(_view())
    v = provider.snapshot("website").version
    assert provider.snapshot("discord").version == v


# ---- provider：executors 來源與 F-1 退路 ----

def test_custom_executor_source_filters_to_current_platform_sorted_unique():
    class Mixed:
        def executors_for(self, platform):
            return (
                ExecutorDescriptor("zeta:desktop", "zeta", "desktop", ORIGIN_REGISTRY_STATIC),
                ExecutorDescriptor("alpha:desktop", "alpha", "desktop", ORIGIN_REGISTRY_STATIC),
                ExecutorDescriptor("alpha:desktop", "alpha", "desktop", ORIGIN_REGISTRY_STATIC),
                ExecutorDescriptor("other:discord", "other", "discord", ORIGIN_REGISTRY_STATIC),
            )

    provider, _ = _provider(_view(), Mixed())
    assert provider.snapshot().available_executors == ("alpha:desktop", "zeta:desktop")


def test_executor_source_failure_falls_back_to_coarse_f1(caplog):
    provider, _ = _provider(_view(location="desktop"), FakeExecutors(RuntimeError("boom")))
    with caplog.at_level(logging.WARNING, logger="uep.echo.platform_context"):
        ctx = provider.snapshot()
    assert ctx.available_executors == ("desktop",)
    assert any("F-1" in r.getMessage() for r in caplog.records)


def test_coarse_source_returns_single_platform_descriptor():
    (d,) = CoarseExecutorSource().executors_for("discord")
    assert d.executor_ref == "discord" and d.origin == ORIGIN_COARSE_FALLBACK


def test_no_executors_when_no_current_platform():
    provider, _ = _provider(_view(status=PresenceStatus.NONE), FakeExecutors({"desktop": ["a:desktop"]}))
    assert provider.snapshot().available_executors == ()


# ---- shadow 差異計數 ----

def test_diff_against_production_none_state_has_no_diff():
    provider, _ = _provider(_view(status=PresenceStatus.NONE))
    assert diff_against_production(provider.snapshot()) == ()


def test_diff_against_production_active_state_lists_all_differences():
    provider, _ = _provider(_view(location="desktop"))
    assert set(diff_against_production(provider.snapshot())) == {
        "projection.platform", "projection.presence_active_platforms", "placement.current_platform",
        "placement.available_executors", "proactive.current_platform",
    }


def test_shadow_counts_turns_and_diffs_without_raising(caplog):
    provider, presence = _provider(_view(status=PresenceStatus.NONE))
    metrics = Metrics()
    shadow = PlatformShadow(provider, metrics)
    shadow.observe("t1", "website")
    assert metrics["platform_shadow.turns"] == 1 and metrics["platform_shadow.current_platform_none"] == 1
    assert metrics["platform_shadow.diff_turns"] == 0
    presence.view = _view(location="desktop", row_version=9)
    with caplog.at_level(logging.INFO, logger="uep.echo.platform_context"):
        ctx = shadow.observe("t2", "website")
    assert ctx is not None and ctx.current_platform == "desktop"
    assert metrics["platform_shadow.turns"] == 2 and metrics["platform_shadow.diff_turns"] == 1
    assert metrics["platform_shadow.diff.projection.platform"] == 1
    assert metrics["platform_shadow.version_changes"] == 2
    record = next(r for r in caplog.records if "turn=t2" in r.getMessage())
    assert f"platform_context_version={ctx.version}" in record.getMessage() and "projection.platform" in record.getMessage()


def test_shadow_swallows_provider_errors():
    class Boom:
        def snapshot(self, session_platform=None):
            raise RuntimeError("boom")

    metrics = Metrics()
    assert PlatformShadow(Boom(), metrics).observe("t1") is None  # type: ignore[arg-type]
    assert metrics["platform_shadow.errors"] == 1


def test_shadow_uses_custom_production_baseline():
    provider, _ = _provider(_view(location="desktop"))
    prod = ProductionInputs(
        projection_platform="desktop", presence_active_platforms=frozenset({"desktop"}),
        placement_current_platform="desktop", placement_available_executors=("desktop",),
        proactive_current_platform="desktop",
    )
    assert diff_against_production(provider.snapshot(), prod) == ()


# ---- 鎖測試：三個消費點的實際輸入不變 ----

def _parse(rel: str) -> ast.AST:
    return ast.parse((UEP_ROOT / rel).read_text(encoding="utf-8"))


def _call_keywords(tree: ast.AST, func_name: str) -> list[set[str]]:
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == func_name:
            out.append({k.arg for k in node.keywords if k.arg})
    return out


# 工具投影的平台只能來自 Host 的 Lease＋location（platform_source／Host.projection_platform）；
# 不得取自模型輸出、ToolCall 參數、SessionContext.platform 或請求內容。PlatformContext 的 peek 只供 consult 建議。
_PLATFORM_SOURCE_ATTRS = {"projection_platform", "_current_platform", "_platform_source"}
_PLATFORM_KEYWORDS = {"platform", "presence_active_platforms"}


def _is_self_call(node: ast.AST | None, attrs: set[str]) -> bool:
    return (
        isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in attrs
        and isinstance(node.func.value, ast.Name) and node.func.value.id == "self"
    )


def _is_none(node: ast.AST | None) -> bool:
    return isinstance(node, ast.Constant) and node.value is None


def _enclosing_function(tree: ast.AST, target: ast.AST) -> ast.AST:
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(n is target for n in ast.walk(fn)):
            return fn
    raise AssertionError("找不到包含呼叫的函式")


def _is_trusted_platform_arg(tree: ast.AST, call: ast.Call, arg: ast.AST) -> bool:
    """arg 必須是平台來源的回傳值：直接呼叫來源，或僅由 None／來源回傳值賦值而來的區域變數。"""
    if _is_self_call(arg, _PLATFORM_SOURCE_ATTRS):
        return True
    if not isinstance(arg, ast.Name):
        return False
    fn = _enclosing_function(tree, call)
    assigned = []
    other_writes = []
    for n in ast.walk(fn):
        targets = n.targets if isinstance(n, ast.Assign) else [n.target] if isinstance(n, ast.AnnAssign) else []
        if any(isinstance(t, ast.Name) and t.id == arg.id for t in targets):
            assigned.append(n.value)
        elif isinstance(n, (ast.AugAssign, ast.For, ast.With, ast.NamedExpr)) and any(
            isinstance(x, ast.Name) and x.id == arg.id and isinstance(x.ctx, ast.Store) for x in ast.walk(n)
        ):
            other_writes.append(n)
    return bool(assigned) and not other_writes and all(
        _is_none(v) or _is_self_call(v, {"_platform_source"}) for v in assigned
    )


@pytest.mark.parametrize("rel", ["agent/bridge.py", "echo/host.py", "echo/agent_wiring.py"])
def test_projection_context_call_sites_take_platform_only_from_platform_source(rel):
    tree = _parse(rel)
    # 不得直接建構 ProjectionContext：一律走 for_platform，讓平台與 presence_active_platforms 同源
    assert not _call_keywords(tree, "ProjectionContext"), f"{rel} 不得直接建構 ProjectionContext（改用 for_platform）"
    calls = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "for_platform"
        and getattr(n.func.value, "id", None) == "ProjectionContext"
    ]
    assert calls, f"{rel} 應該有 ProjectionContext.for_platform 建構點"
    for call in calls:
        kws = {k.arg for k in call.keywords}
        assert not (kws & _PLATFORM_KEYWORDS), f"{rel} 不得以關鍵字覆寫平台：{kws}"
        assert len(call.args) == 1, f"{rel} 平台必須是 for_platform 的唯一位置參數"
        assert _is_trusted_platform_arg(tree, call, call.args[0]), (
            f"{rel} 的 ProjectionContext 平台只能取自 platform_source／Host.projection_platform：{ast.unparse(call.args[0])}"
        )


def _method(rel: str, name: str) -> ast.FunctionDef:
    for c in ast.walk(_parse(rel)):
        if isinstance(c, ast.ClassDef):
            for f in c.body:
                if isinstance(f, ast.FunctionDef) and f.name == name:
                    return f
    raise AssertionError(f"{rel} 找不到 {name}")


def test_platform_sources_return_only_lease_location_or_none():
    # bridge：_current_platform 只回 platform_source() 或 None（失敗 fail-closed）
    bridge_fn = _method("agent/bridge.py", "_current_platform")
    for r in (n for n in ast.walk(bridge_fn) if isinstance(n, ast.Return)):
        assert _is_none(r.value) or _is_self_call(r.value, {"_platform_source"}), ast.unparse(r)
    # Host：projection_platform 只回 None 或 platform_from_presence_location(self._location)，不讀 session／請求內容
    host_fn = _method("echo/host.py", "projection_platform")
    returns = [n for n in ast.walk(host_fn) if isinstance(n, ast.Return)]
    assert returns
    for r in returns:
        v = r.value
        assert _is_none(v) or (
            isinstance(v, ast.Call) and getattr(v.func, "id", None) == "platform_from_presence_location"
            and len(v.args) == 1 and ast.unparse(v.args[0]) == "self._location"
        ), ast.unparse(r)
    refs = {n.attr for n in ast.walk(host_fn) if isinstance(n, ast.Attribute)}
    assert not (refs & {"_session", "platform", "audience_scope"}), refs


def test_assembly_wires_platform_source_from_host_projection_platform():
    tree = _parse("assembly.py")
    assigns = [
        n for n in ast.walk(tree) if isinstance(n, ast.Assign)
        and any(getattr(t, "id", None) == "platform_source" for t in n.targets)
    ]
    assert len(assigns) == 1
    v = assigns[0].value
    assert (
        isinstance(v, ast.Call) and getattr(v.func, "id", None) == "getattr" and len(v.args) == 3
        and ast.unparse(v.args[0]) == "echo" and ast.unparse(v.args[1]) == "'projection_platform'"
    ), ast.unparse(v)
    # 所有 platform_source= 關鍵字都只傳這個區域變數；PlatformContextProvider 的 peek 不得流入投影
    seen = 0
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call):
            continue
        for k in n.keywords:
            if k.arg == "platform_source":
                seen += 1
                assert ast.unparse(k.value) == "platform_source", ast.unparse(n)
        if getattr(n.func, "id", None) in {"attach_agent_tools", "build_loop_entry"}:
            exprs = [*n.args, *[k.value for k in n.keywords]]
            names = {x.id for a in exprs for x in ast.walk(a) if isinstance(x, ast.Name)}
            assert "platform_provider" not in names, "PlatformContext peek 不得流入工具投影"
    assert seen >= 2


def test_projection_context_defaults_unchanged():
    from uep.tools.projection import ProjectionContext

    ctx = ProjectionContext()
    assert ctx.platform is None and ctx.presence_active_platforms == frozenset() and not ctx.presence_active()


def _importers(module: str) -> set[str]:
    hits = set()
    for path in UEP_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            mods = []
            if isinstance(node, ast.ImportFrom) and node.module:
                mods = [node.module] + [f"{node.module}.{a.name}" for a in node.names]
            elif isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            if module in mods:
                hits.add(path.relative_to(UEP_ROOT).as_posix())
    return hits


def test_platform_context_is_read_only_by_shadow_wiring():
    # shadow／consult 只讀 PlatformContext：只有組裝層與 Host 可 import 它，消費點不得 import
    assert _importers("uep.echo.platform_context") == {"assembly.py", "echo/host.py"}
    # contracts.platform 的 importer：shadow、相容層邊界（SessionContext 建構、HostPolicySource）、
    # E3 Output Gate 前置檢查；Host／組裝層只為把 Presence location 轉成平台（工具投影的授權來源）而 import
    assert _importers("uep.contracts.platform") == {
        "echo/platform_context.py", "echo/policy.py", "presence/gate.py", "turn/models.py", "echo/host.py", "assembly.py",
    }
    for rel in ("echo/host.py", "assembly.py"):
        names = {
            a.name for n in ast.walk(_parse(rel))
            if isinstance(n, ast.ImportFrom) and n.module == "uep.contracts.platform" for a in n.names
        }
        assert names == {"platform_from_presence_location"}, (rel, names)
    # shadow 只觀察：observe() 的回傳值不得被使用（不寫回）
    tree = _parse("echo/host.py")
    observes = 0
    for p in ast.walk(tree):
        for c in ast.iter_child_nodes(p):
            if isinstance(c, ast.Call) and getattr(c.func, "attr", None) == "observe":
                observes += 1
                assert isinstance(p, ast.Expr), "shadow.observe 的回傳值不得被使用"
    assert observes >= 1
    # Host 只 import PlatformShadow，不碰 PlatformContextProvider（peek 只給決策層 consult 當建議）
    imported = {
        a.name for n in ast.walk(tree)
        if isinstance(n, ast.ImportFrom) and n.module == "uep.echo.platform_context" for a in n.names
    }
    assert imported == {"PlatformShadow"}, imported


def test_consumers_do_not_reference_platform_context_names():
    # 第 5 步（決策層側）：loop_consult 已改為注入點，改由下一個測試鎖定它只讀快照、不碰組裝層
    for rel in ("decision/proactive.py", "tools/projection.py", "tools/registry.py", "agent/bridge.py"):
        text = (UEP_ROOT / rel).read_text(encoding="utf-8")
        assert "PlatformContext" not in text and "platform_context" not in text, rel


def test_loop_consult_injection_point_stays_decoupled_from_assembly_layer():
    text = (UEP_ROOT / "decision" / "loop_consult.py").read_text(encoding="utf-8")
    assert "uep.echo" not in text and "uep.presence" not in text and "peek" not in text


def test_contract_layer_has_no_upward_imports():
    tree = _parse("contracts/platform.py")
    imported = {
        n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module and n.level == 0
    } | {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    uep_imports = {m for m in imported if m.split(".")[0] == "uep"}
    assert uep_imports <= {"uep.contracts.presence"}, uep_imports


def test_registry_is_untouched_by_executors_for():
    # 工具層尚未接上 executors_for（歸工具層 PR4 前後）；這次不得動 registry
    assert "executors_for" not in (UEP_ROOT / "tools" / "registry.py").read_text(encoding="utf-8")


# ---- 平台端點契約 E2：monitor_platforms（shadow 觀察，不分發；EC-3）----

def test_monitor_platforms_default_is_empty_and_changes_nothing():
    provider, _ = _provider(_view(status=PresenceStatus.NONE))
    ctx = provider.snapshot()
    assert ctx.monitor_platforms == frozenset()
    assert diff_against_production(ctx) == ()
    assert ProductionInputs().monitor_platforms == frozenset()


def test_monitor_online_does_not_make_presence():
    # 監控面在線不代表她在場：Presence=NONE 時 current_platform／presence_active_platforms 不受影響
    provider, _ = _provider(_view(status=PresenceStatus.NONE), monitor_platforms=lambda: ["discord", "website"])
    ctx = provider.snapshot()
    assert ctx.monitor_platforms == frozenset({"discord", "website"})
    assert ctx.current_platform is None and ctx.presence_active_platforms == frozenset()
    assert ctx.available_executors == ()
    assert diff_against_production(ctx) == ("monitor_platforms",)


def test_monitor_platforms_independent_of_active_presence():
    provider, _ = _provider(_view(location="desktop"), monitor_platforms=lambda: {"discord"})
    ctx = provider.snapshot()
    assert ctx.current_platform == "desktop" and ctx.presence_active_platforms == frozenset({"desktop"})
    assert ctx.monitor_platforms == frozenset({"discord"})
    # 同平台同時有監控也合法，但不互相推論
    provider2, _ = _provider(_view(location="discord"), monitor_platforms=lambda: {"discord"})
    assert provider2.snapshot().current_platform == "discord"


def test_monitor_platforms_source_failure_and_invalid_refs_are_swallowed():
    def boom():
        raise RuntimeError("monitor source down")

    provider, _ = _provider(_view(location="desktop"), monitor_platforms=boom)
    ctx = provider.snapshot()
    assert ctx.monitor_platforms == frozenset() and ctx.current_platform == "desktop"
    provider, _ = _provider(_view(status=PresenceStatus.NONE), monitor_platforms=lambda: ["discord", "Bad Ref", "a@b"])
    assert provider.snapshot().monitor_platforms == frozenset({"discord"})


def test_monitor_platforms_change_bumps_version():
    current: set[str] = set()
    provider, _ = _provider(_view(status=PresenceStatus.NONE), monitor_platforms=lambda: current)
    v1 = provider.snapshot().version
    assert provider.snapshot().version == v1
    current.add("discord")
    v2 = provider.snapshot().version
    assert v2 == v1 + 1
    assert provider.snapshot().version == v2


def test_platform_context_rejects_invalid_monitor_platform():
    base = {
        "version": 1, "presence_generation": 0, "presence_row_version": 0, "presence_status": PresenceStatus.NONE,
        "current_platform": None, "presence_active_platforms": frozenset(), "session_platform": None,
        "available_executors": (),
    }
    PlatformContext(**base, monitor_platforms=frozenset({"discord"}))
    with pytest.raises(ValueError):
        PlatformContext(**base, monitor_platforms=frozenset({"Discord"}))


def test_shadow_counts_monitor_platforms_diff_without_touching_other_fields():
    provider, _ = _provider(_view(status=PresenceStatus.NONE), monitor_platforms=lambda: {"discord"})
    metrics = Metrics()
    ctx = PlatformShadow(provider, metrics).observe("t1")
    assert ctx is not None and ctx.monitor_platforms == frozenset({"discord"})
    assert metrics["platform_shadow.diff.monitor_platforms"] == 1 and metrics["platform_shadow.diff_turns"] == 1
    assert metrics["platform_shadow.current_platform_none"] == 1
    assert not any(k.startswith("platform_shadow.diff.") and k != "platform_shadow.diff.monitor_platforms" for k in dict(metrics))


def test_shadow_production_baseline_matches_monitor_platforms():
    provider, _ = _provider(_view(status=PresenceStatus.NONE), monitor_platforms=lambda: {"discord"})
    prod = ProductionInputs(monitor_platforms=frozenset({"discord"}))
    assert diff_against_production(provider.snapshot(), prod) == ()


def test_consumers_do_not_reference_monitor_platforms():
    # monitor_platforms 只供 shadow 觀察：除型別定義與 provider／shadow 外，uep/ 內沒有任何引用
    allowed = {"contracts/platform.py", "echo/platform_context.py"}
    offenders = [
        path.relative_to(UEP_ROOT).as_posix()
        for path in UEP_ROOT.rglob("*.py")
        if "monitor_platforms" in path.read_text(encoding="utf-8") and path.relative_to(UEP_ROOT).as_posix() not in allowed
    ]
    assert offenders == []
