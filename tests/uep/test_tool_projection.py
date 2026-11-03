"""Agent 架構提案 §9.1 切片 B：投影、render_tools、D60 啟動斷言、Gateway 參數驗證、readiness 事件。

全部是純記憶體測試（無 DB、無 Runtime 實作）。
"""
from __future__ import annotations

import ast
import json
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest

from tests.uep.test_tool_registry import GOLDEN, make_entry
from uep.adapters.runtime import RuntimeRequest, check_tools_match_capability
from uep.adapters.tool import CAPABILITY_CEILING, ToolDescriptor, ToolScope, ToolVariant
from uep.tools.catalog.memory import GUILD_ROSTER_CONTEXT, memory_registry
from uep.tools.gateway import (
    CAPABILITY_MISSING,
    INVALID_TOOL_ARGS,
    PARAM_NOT_ALLOWED,
    GatewayRejected,
    ToolGateway,
    visible_tools,
)
from uep.tools.projection import ProjectionContext
from uep.tools.registry import ToolRegistry, ToolRegistryError
from uep.tools.render import render_tools
from uep.tools.schema import lint_subset, strict_schema, validate_args

UEP_ROOT = Path(__file__).resolve().parents[2] / "uep"

SECRET = "SECRET-使用者原文-9f3a"

_NOTIFY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "message": {"type": "string", "minLength": 1, "maxLength": 200},
        "urgency": {"type": "string", "enum": ["low", "normal", "high"]},
        "tags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["message"],
}


def _caps(*, with_events: bool = False) -> list:
    """§5.6 表的五種條目（分類取最小可通過 RA 的值）。"""
    remote = {"provider": "desktop", "targeting": "platform_remote", "origin": "platform_approved"}
    return [
        make_entry("deep_recall"),  # memory.read，global
        make_entry("member_recall", scope=ToolScope(requires_context=frozenset({GUILD_ROSTER_CONTEXT}))),
        make_entry(
            "desktop.file_read", spec={**remote, "capability": "desktop.file_read", "presence_bound": True},
            scope=ToolScope(platforms=frozenset({"desktop"}), presence="bound"),
        ),
        make_entry(
            "notify.user", spec={"capability": "chatroom.read"},
            descriptor=ToolDescriptor("通知（基底）", _NOTIFY_SCHEMA),
            scope=ToolScope(variants={
                "desktop": ToolVariant(description="Desktop 彈通知"),
                "discord": ToolVariant(
                    description="Discord 送訊息",
                    input_schema={**_NOTIFY_SCHEMA, "properties": {
                        **_NOTIFY_SCHEMA["properties"],
                        "urgency": {"type": "string", "enum": ["low", "normal"]},
                    }},
                ),
            }),
        ),
        make_entry(
            "presentation.propose", spec={"capability": "presentation.propose", "side_effect_class": "reversible_local",
                                          "presence_bound": True},
            scope=ToolScope(presence="bound"),
        ),
    ]


ALL_TAGS = frozenset({"memory.read", "desktop.file_read", "chatroom.read", "presentation.propose"})


@pytest.fixture()
def registry() -> ToolRegistry:
    return ToolRegistry.build(_caps())


def names(capability_set) -> list[str]:
    return [t["tool"] for t in capability_set["tools"]]


# ---------------------------------------------------------------------------
# 投影：§5.6 表（同一工具在不同 (platform, presence) 下）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(("platform", "active", "context", "expected"), [
    # 全域工具到處都有；member_recall 要有 guild_roster；presence_bound 的要 Presence 在該場域 ACTIVE
    (None, frozenset(), frozenset(), ["deep_recall", "notify.user"]),
    ("desktop", frozenset(), frozenset(), ["deep_recall", "notify.user"]),  # Presence 不在場：desktop.file_read／presentation 不投出
    ("desktop", frozenset({"desktop"}), frozenset(), ["deep_recall", "desktop.file_read", "notify.user", "presentation.propose"]),
    ("discord", frozenset({"desktop"}), frozenset(), ["deep_recall", "notify.user"]),  # Presence 在別的場域
    ("discord", frozenset({"discord"}), frozenset({GUILD_ROSTER_CONTEXT}), ["deep_recall", "member_recall", "notify.user", "presentation.propose"]),
    ("website", frozenset({"website"}), frozenset(), ["deep_recall", "notify.user", "presentation.propose"]),
])
def test_projection_matches_scope_table(registry, platform, active, context, expected):
    ctx = ProjectionContext(platform=platform, presence_active_platforms=active, available_context=context)
    projected = registry.project(ALL_TAGS, ctx)
    assert sorted(names(projected)) == sorted(expected)


def test_variant_selected_by_platform_and_carries_variant_name(registry):
    discord = registry.project(ALL_TAGS, ProjectionContext(platform="discord"))
    desktop = registry.project(ALL_TAGS, ProjectionContext(platform="desktop"))
    other = registry.project(ALL_TAGS, ProjectionContext(platform="website"))
    by = lambda cs: {t["tool"]: t for t in cs["tools"]}
    assert by(discord)["notify.user"]["variant"] == "discord"
    assert by(discord)["notify.user"]["description"] == "Discord 送訊息"
    assert by(discord)["notify.user"]["input_schema"]["properties"]["urgency"]["enum"] == ("low", "normal")
    assert by(desktop)["notify.user"]["variant"] == "desktop"
    assert by(desktop)["notify.user"]["description"] == "Desktop 彈通知"
    assert by(desktop)["notify.user"]["input_schema"]["properties"]["urgency"]["enum"] == ("low", "normal", "high")
    assert by(other)["notify.user"]["variant"] is None  # 沒有該平台的變體：用基底
    assert by(other)["notify.user"]["description"] == "通知（基底）"


def test_platform_scoped_tool_never_falls_back_to_other_platform(registry):
    """§5.6-5：基底不適用於當前平台 ⇒ 不投出，不靜默降級。"""
    ctx = ProjectionContext(platform="discord", presence_active_platforms=frozenset({"discord", "desktop"}))
    assert "desktop.file_read" not in names(registry.project(ALL_TAGS, ctx))


def test_projection_is_filtered_by_tags_origin_and_disabled():
    import dataclasses

    entries = _caps()
    hidden = make_entry("cand.tool", spec={"origin": "discovered"})
    off = dataclasses.replace(make_entry("off.tool"), disabled=True)
    reg = ToolRegistry.build([*entries, hidden, off])
    out = names(reg.project({"memory.read"}, ProjectionContext()))
    assert out == ["deep_recall"]  # discovered 永不投出（RA-3）、disabled 不投出；其他標籤的不投出
    assert names(reg.project(set(), ProjectionContext())) == []


def test_audience_gate():
    reg = ToolRegistry.build([
        make_entry("wide.tool", scope=ToolScope(audience_min="group")),
        make_entry("deep_recall"),
    ])
    assert names(reg.project({"memory.read"}, ProjectionContext(audience="private"))) == ["deep_recall"]
    assert names(reg.project({"memory.read"}, ProjectionContext(audience="group"))) == ["wide.tool", "deep_recall"]
    with pytest.raises(ValueError):
        ProjectionContext(audience="galaxy")
    with pytest.raises(ToolRegistryError) as exc:
        ToolRegistry.build([make_entry(scope=ToolScope(audience_min="galaxy"))])
    assert exc.value.rule == "RA-1"


def test_capability_set_shape_is_readonly_and_without_internals(registry):
    cs = registry.project(ALL_TAGS, ProjectionContext(platform="desktop"))
    assert set(cs) == {"tools", "pending", "revision", "tags"}
    assert cs["tags"] == tuple(sorted(ALL_TAGS))
    for item in cs["tools"]:
        assert set(item) == {"tool", "version", "description", "input_schema", "variant"}
    with pytest.raises(TypeError):
        cs["tools"][0]["tool"] = "x"  # type: ignore[index]
    assert isinstance(cs, MappingProxyType)


def test_wildcard_and_unknown_tags_select_nothing(registry):
    ctx = ProjectionContext()
    assert names(registry.project({"memory.*", "*", "memory"}, ctx)) == []
    assert registry.project({"memory.*"}, ctx)["tags"] == ()  # 閉集外的標籤丟棄
    with pytest.raises(TypeError):
        registry.project("memory.read", ctx)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# AL-3：能力收窄只減不增
# ---------------------------------------------------------------------------

def test_al3_ceiling_never_widens(registry):
    ctx_pool = [
        ProjectionContext(platform=p, presence_active_platforms=frozenset(a), available_context=frozenset(c))
        for p in (None, "desktop", "discord", "website")
        for a in ((), ("desktop",), ("discord",), ("desktop", "discord"))
        for c in ((), (GUILD_ROSTER_CONTEXT,))
    ]
    universe = sorted(CAPABILITY_CEILING) + ["memory.*", "bogus"]
    rng = random.Random(20261103)
    for _ in range(300):
        frozen = frozenset(rng.sample(universe, rng.randint(0, len(universe))))
        ctx = ctx_pool[rng.randrange(len(ctx_pool))]
        base = registry.project(frozen, ctx)
        for _ in range(5):
            asked = frozenset(rng.sample(universe, rng.randint(0, len(universe))))  # 任意（可能更寬）的後續要求
            narrowed = registry.project(asked, ctx, ceiling=frozen)
            assert set(narrowed["tags"]) <= set(base["tags"])
            assert set(names(narrowed)) <= set(names(base))  # 同一脈絡下工具集合只減不增
            assert set(names(registry.project(asked & frozen, ctx))) == set(names(narrowed))
            # 逐步收窄：連續兩次投影，後一次 ⊆ 前一次
            again = registry.project(asked, ctx, ceiling=set(narrowed["tags"]))
            assert set(names(again)) <= set(names(narrowed))


def test_al3_ceiling_is_the_upper_bound_even_when_readiness_changes():
    """凍結範圍內的工具之後轉 ready 可以出現（不擴大範圍）；範圍外的永遠不會。"""
    reg = ToolRegistry.build(
        [make_entry("deep_recall"), make_entry("warm.tool", requires=("gpu:x",), warm_required=True,
                                                spec={"capability": "memory.read"})],
        known_modules=["gpu:x"],
    )
    ctx = ProjectionContext()
    reg.apply_module_event("module.warming", {"module_id": "gpu:x", "revision": 1})
    assert names(reg.project({"memory.read"}, ctx, ceiling={"memory.read"})) == ["deep_recall"]
    reg.apply_module_event("module.ready", {"module_id": "gpu:x", "revision": 2})
    assert names(reg.project({"memory.read"}, ctx, ceiling={"memory.read"})) == ["deep_recall", "warm.tool"]
    assert names(reg.project({"memory.read"}, ctx, ceiling={"memory.write"})) == []


# ---------------------------------------------------------------------------
# readiness：fake module coordinator 事件驅動（§5.8）
# ---------------------------------------------------------------------------

class FakeModuleCoordinator:
    """依序發送 module.* 事件給註冊表；revision 單調遞增，可手動指定以模擬重送／亂序。"""

    def __init__(self, registry: ToolRegistry) -> None:
        self.registry, self._rev = registry, 0

    def emit(self, state: str, module_id: str = "gpu:indextts", *, revision: int | None = None, **extra: Any) -> bool:
        if revision is None:
            self._rev += 1
            revision = self._rev
        return self.registry.apply_module_event(f"module.{state}", {"module_id": module_id, "revision": revision, **extra})


@pytest.fixture()
def warm_registry() -> ToolRegistry:
    heavy = make_entry(
        "speak.tool", spec={"capability": "memory.read"}, requires=("gpu:indextts",), load_cost="heavy",
        warm_required=True,
    )
    return ToolRegistry.build([make_entry("deep_recall"), heavy], known_modules=["gpu:indextts"])


def test_readiness_unavailable_is_not_projected_and_not_pending(warm_registry):
    mc = FakeModuleCoordinator(warm_registry)
    assert mc.emit("unavailable", reason_code="oom")
    cs = warm_registry.project({"memory.read"}, ProjectionContext())
    assert names(cs) == ["deep_recall"] and cs["pending"] == ()


def test_readiness_warming_is_not_projected_but_reported_pending(warm_registry):
    mc = FakeModuleCoordinator(warm_registry)
    mc.emit("warming")
    cs = warm_registry.project({"memory.read"}, ProjectionContext())
    assert names(cs) == ["deep_recall"]  # 模型不知其存在
    assert [dict(p) for p in cs["pending"]] == [{"tool": "speak.tool", "reason": "warming"}]
    assert "speak.tool" not in json.dumps(render_tools(cs))  # pending 不渲染給模型


def test_readiness_ready_is_projected_and_event_sequence_follows_latest_revision(warm_registry):
    mc = FakeModuleCoordinator(warm_registry)
    for state, projected in (("warming", False), ("ready", True), ("unavailable", False), ("ready", True)):
        assert mc.emit(state)
        got = "speak.tool" in names(warm_registry.project({"memory.read"}, ProjectionContext()))
        assert got is projected, state


def test_readiness_out_of_order_and_duplicate_events_use_latest_revision(warm_registry):
    mc = FakeModuleCoordinator(warm_registry)
    ctx = ProjectionContext()
    assert mc.emit("ready", revision=5)
    assert not mc.emit("unavailable", revision=3)  # 亂序舊事件：忽略
    assert not mc.emit("unavailable", revision=5)  # 同 revision 重送／衝突：忽略
    assert "speak.tool" in names(warm_registry.project({"memory.read"}, ctx))
    assert mc.emit("unavailable", revision=6)
    assert not mc.emit("ready", revision=4)  # 遲到的 ready 不得把工具救回來
    assert "speak.tool" not in names(warm_registry.project({"memory.read"}, ctx))


def test_readiness_bad_events_are_ignored_fail_closed(warm_registry):
    mc = FakeModuleCoordinator(warm_registry)
    mc.emit("unavailable")
    reg = warm_registry
    assert not reg.apply_module_event("module.exploded", {"module_id": "gpu:indextts", "revision": 99})
    assert not reg.apply_module_event("module.ready", {"module_id": "gpu:indextts"})  # 缺 revision
    assert not reg.apply_module_event("module.ready", {"module_id": "gpu:indextts", "revision": True})
    assert not reg.apply_module_event("module.ready", {"module_id": "gpu:unknown", "revision": 99})  # 未知模組
    assert reg.readiness("speak.tool") == "unavailable"


def test_readiness_and_over_requires():
    entry = make_entry("multi.tool", requires=("a", "b"), spec={"capability": "memory.read"})
    reg = ToolRegistry.build([entry], known_modules=["a", "b"])
    mc = FakeModuleCoordinator(reg)
    mc.emit("ready", "a")
    mc.emit("warming", "b")
    assert reg.readiness("multi.tool") == "warming"
    mc.emit("unavailable", "a")
    assert reg.readiness("multi.tool") == "unavailable"  # 任一 unavailable 勝過 warming
    mc.emit("ready", "a")
    mc.emit("ready", "b")
    assert reg.readiness("multi.tool") == "ready"
    unknown = ToolRegistry.build([entry], known_modules=["a"])  # b 未知：RA-15 fail-closed
    assert unknown.readiness("multi.tool") == "unavailable"


def test_pending_only_for_warming_tools_that_pass_scope_and_tags():
    scoped = make_entry("desk.tool", requires=("m",), spec={"capability": "memory.read"},
                        scope=ToolScope(platforms=frozenset({"desktop"})))
    other = make_entry("other.tool", requires=("m",), spec={"capability": "memory.write",
                                                          "side_effect_class": "reversible_local"})
    reg = ToolRegistry.build([scoped, other], known_modules=["m"])
    FakeModuleCoordinator(reg).emit("warming", "m")
    assert reg.project({"memory.read"}, ProjectionContext(platform="discord"))["pending"] == ()  # 場域不符
    assert reg.project({"memory.read"}, ProjectionContext(platform="desktop"))["pending"][0]["tool"] == "desk.tool"
    assert [p["tool"] for p in reg.project({"memory.read"}, ProjectionContext(platform="desktop"))["pending"]] == ["desk.tool"]  # 標籤外的不報


def test_capability_set_revision_moves_only_on_applied_events(warm_registry):
    mc = FakeModuleCoordinator(warm_registry)
    ctx = ProjectionContext()
    r0 = warm_registry.project({"memory.read"}, ctx)["revision"]
    mc.emit("ready", revision=2)
    r1 = warm_registry.project({"memory.read"}, ctx)["revision"]
    mc.emit("unavailable", revision=1)  # 被忽略
    assert warm_registry.project({"memory.read"}, ctx)["revision"] == r1 > r0


# ---------------------------------------------------------------------------
# render_tools 與 D60 啟動斷言
# ---------------------------------------------------------------------------

def _request(capability_set) -> RuntimeRequest:
    return RuntimeRequest(
        run_id="run_1", attempt_id="att_1", snapshot_ref="snap_1", capability_set=capability_set,
        cancel_scope={"task_run": "run_1"}, deadline=datetime.now(UTC) + timedelta(minutes=1), mode="direct_chat",
    )


def test_render_tools_is_exactly_the_capability_set(registry):
    cs = registry.project(ALL_TAGS, ProjectionContext(platform="discord", presence_active_platforms=frozenset({"discord"})))
    tools = render_tools(cs)
    assert [t["function"]["name"] for t in tools] == names(cs)
    assert all(set(t) == {"type", "function"} and t["type"] == "function" for t in tools)
    assert all(set(t["function"]) == {"name", "description", "parameters"} for t in tools)
    tools[0]["function"]["parameters"]["properties"]["injected"] = {}  # 深複本：改不到投影結果
    assert "injected" not in cs["tools"][0]["input_schema"]["properties"]
    assert check_tools_match_capability(_request(cs), render_tools(cs)) is None


def test_render_rejects_malformed_capability_set_and_dialect(registry):
    cs = registry.project({"memory.read"}, ProjectionContext())
    with pytest.raises(ValueError):
        render_tools(cs, "responses")
    with pytest.raises(ValueError):
        render_tools({"tools": [{"tool": "a"}]})
    dup = {"tools": [dict(cs["tools"][0]), dict(cs["tools"][0])]}
    with pytest.raises(ValueError):
        render_tools(dup)
    with pytest.raises(ValueError):
        render_tools({})


def test_render_adds_additional_properties_false_and_nothing_else(registry):
    """render 新路徑的差異只有 additionalProperties:false；舊路徑黃金檔仍是 S-A 的位元組（見 test_tool_registry）。"""
    reg = memory_registry()
    cs = reg.project({"memory.read", "memory.write"}, ProjectionContext(available_context=frozenset({GUILD_ROSTER_CONTEXT})))
    rendered = {t["function"]["name"]: t for t in render_tools(cs)}
    golden = {d["function"]["name"]: d for d in json.loads(GOLDEN.read_text(encoding="utf-8"))}
    assert set(rendered) == set(golden)
    for name, tool in rendered.items():
        expected = json.loads(json.dumps(golden[name]))
        expected["function"]["parameters"]["additionalProperties"] = False
        assert tool == expected, name
    # 舊路徑沒被動到
    legacy = json.dumps(reg.llm_definitions(), ensure_ascii=False, indent=2) + "\n"
    assert legacy.encode("utf-8") == GOLDEN.read_text(encoding="utf-8").encode("utf-8")
    assert "additionalProperties" not in legacy


def test_d60_counterexamples_extra_and_missing_tools(registry):
    cs = registry.project(ALL_TAGS, ProjectionContext())
    good = render_tools(cs)
    req = _request(cs)
    assert check_tools_match_capability(req, good) is None

    extra = [*good, {"type": "function", "function": {"name": "sneaky.tool", "parameters": {}}}]
    ev = check_tools_match_capability(req, extra, seq=3)
    assert ev is not None and ev.kind == "error" and ev.seq == 3 and ev.attempt_id == "att_1"
    assert ev.error["code"] == "CAPABILITY_MISSING" and ev.error["retryable"] is False
    assert "sneaky.tool" in ev.error["detail"]

    ev = check_tools_match_capability(req, good[:-1])
    assert ev is not None and ev.error["code"] == "CAPABILITY_MISSING"
    assert check_tools_match_capability(req, []) is not None  # 全部少給
    assert check_tools_match_capability(_request({"tools": []}), good) is not None  # capability_set 空卻送出工具
    assert check_tools_match_capability(_request({"tools": []}), []) is None  # 兩邊皆空：合法


def test_d60_rejects_hosted_builtin_duplicates_and_non_function(registry):
    cs = registry.project({"memory.read"}, ProjectionContext())
    good = render_tools(cs)
    req = _request(cs)
    hosted = [*good, {"type": "web_search"}]
    assert check_tools_match_capability(req, hosted).error["code"] == "CAPABILITY_MISSING"  # type: ignore[union-attr]
    assert check_tools_match_capability(req, good, builtin_tools=["code_interpreter"]) is not None
    assert check_tools_match_capability(req, [*good, good[0]]) is not None  # 重複名稱
    assert check_tools_match_capability(req, [{"type": "function"}]) is not None  # 無名稱
    assert check_tools_match_capability(req, ["deep_recall"]) is not None  # type: ignore[list-item]
    assert check_tools_match_capability(req, [{**good[0], "type": "custom"}]) is not None


# ---------------------------------------------------------------------------
# schema 子集：註冊時就失敗
# ---------------------------------------------------------------------------

def _entry_with(schema: dict[str, Any]):
    return make_entry(descriptor=ToolDescriptor("d", schema))


@pytest.mark.parametrize("bad", [
    {"type": "object", "properties": {"a": {"$ref": "#/$defs/x"}}},
    {"type": "object", "properties": {"a": {"type": "string"}}, "$defs": {"x": {"type": "string"}}},
    {"type": "object", "properties": {"a": {"oneOf": [{"type": "string"}, {"type": "integer"}]}}},
    {"type": "object", "properties": {"a": {"type": "string", "anyOf": [{"type": "string"}]}}},
    {"type": "object", "properties": {"a": {"type": "string", "allOf": [{"minLength": 1}]}}},
    {"type": "object", "oneOf": [{"required": ["a"]}], "properties": {"a": {"type": "string"}}},
    {"type": "object", "properties": {"a": {"type": "array", "items": {"$ref": "#/x"}}}},
    {"type": "object", "properties": {"a": {"type": "object", "properties": {"b": {"anyOf": []}}}}},
    {"type": "object", "properties": {"a": {"type": "string", "format": "email"}}},  # 驗證器不會執行的關鍵字
    {"type": "object", "properties": {"a": {"type": "string"}}, "additionalProperties": True},
    {"type": "object", "properties": {"a": {"type": "string"}}, "additionalProperties": {"type": "string"}},
    {"type": "object", "properties": {"a": {"type": "string", "pattern": "("}}},
    {"type": "object", "properties": {"a": {"type": "integer", "minimum": "1"}}},
    {"type": "object", "properties": {"a": {"type": "string", "x-uep-type": "weird"}}},
    {"type": "object", "properties": {"a": {"type": "string", "enum": []}}},
])
def test_schema_outside_subset_fails_at_registration(bad):
    assert lint_subset(bad) is not None
    with pytest.raises(ToolRegistryError) as exc:
        ToolRegistry.build([_entry_with(bad)])
    assert exc.value.rule == "RA-7"


def test_ref_in_output_schema_also_fails():
    with pytest.raises(ToolRegistryError) as exc:
        ToolRegistry.build([make_entry(output_schema={"type": "object", "properties": {"a": {"$ref": "#/x"}}})])
    assert exc.value.rule == "RA-16"


def test_variant_schema_with_ref_fails_at_registration():
    variant = ToolVariant(input_schema={"type": "object", "properties": {"path": {"$ref": "#/x"}}})
    with pytest.raises(ToolRegistryError) as exc:
        ToolRegistry.build([make_entry(scope=ToolScope(variants={"desktop": variant}))])
    assert exc.value.rule in {"RA-13", "RA-7"}


def test_subset_accepts_the_documented_keywords_and_explicit_false():
    ok = {
        "type": "object", "additionalProperties": False, "required": ["p"],
        "properties": {
            "p": {"type": "string", "x-uep-type": "path", "minLength": 1, "maxLength": 9, "pattern": "^/", "default": "/"},
            "n": {"type": "integer", "minimum": 0, "maximum": 5}, "x": {"type": "number"}, "b": {"type": "boolean"},
            "e": {"type": "string", "enum": ["a", "b"], "title": "T", "description": "d"},
            "arr": {"type": "array", "items": {"type": "object", "properties": {"k": {"type": "string"}}}},
        },
    }
    assert lint_subset(ok) is None
    ToolRegistry.build([_entry_with(ok)])


def test_strict_schema_closes_every_object_and_leaves_input_untouched():
    declared = {
        "type": "object",
        "properties": {"a": {"type": "array", "items": {"type": "object", "properties": {"k": {"type": "string"}}}}},
    }
    strict = strict_schema(declared)
    assert strict["additionalProperties"] is False
    assert strict["properties"]["a"]["items"]["additionalProperties"] is False
    assert "additionalProperties" not in declared and "additionalProperties" not in declared["properties"]["a"]["items"]


# ---------------------------------------------------------------------------
# Gateway：參數驗證、injected_params、reason_code／fields
# ---------------------------------------------------------------------------

@pytest.fixture()
def gw_rig():
    entries = [
        make_entry(
            "notify.user", spec={"capability": "chatroom.read"},
            descriptor=ToolDescriptor("n", _NOTIFY_SCHEMA, injected_params=("venue",)),
            scope=ToolScope(variants={"discord": ToolVariant(input_schema={
                **_NOTIFY_SCHEMA, "properties": {**_NOTIFY_SCHEMA["properties"],
                                                 "urgency": {"type": "string", "enum": ["low", "normal"]}},
            })}),
        ),
        make_entry("deep_recall", descriptor=ToolDescriptor("d", {
            "type": "object", "properties": {"query": {"type": "string"}, "top_k": {"type": "integer", "minimum": 1}},
            "required": ["query"],
        })),
    ]
    reg = ToolRegistry.build(entries)
    cs = reg.project({"memory.read", "chatroom.read"}, ProjectionContext(platform="desktop"))
    return reg, ToolGateway(reg), cs


def test_gateway_accepts_valid_args_and_returns_copy(gw_rig):
    _, gw, cs = gw_rig
    args = {"query": "天氣", "top_k": 3}
    out = gw.check_intent("deep_recall", args, cs)
    assert out == args and out is not args


def test_gateway_missing_required_is_rejected_with_fields_but_no_values(gw_rig):
    _, gw, cs = gw_rig
    with pytest.raises(GatewayRejected) as exc:
        gw.check_intent("deep_recall", {"top_k": 3}, cs)
    assert exc.value.code == INVALID_TOOL_ARGS and exc.value.fields == ("/query",)
    view = gw.rejection_view(exc.value)
    assert view["status"] == "rejected" and view["reason_code"] == "invalid_tool_args" and view["fields"] == ["/query"]


def test_gateway_extra_and_wrong_typed_fields_never_leak_values(gw_rig):
    _, gw, cs = gw_rig
    cases = [
        {"query": "ok", "extra": SECRET},  # 多餘欄位（值是使用者原文）
        {"query": SECRET, "top_k": SECRET},  # 型別錯誤
        {"query": ["x", SECRET]},
        {"query": "ok", "top_k": 0},  # 範圍
        {"query": "ok", "top_k": 1.0},  # 整數與浮點不混用
        {"query": "ok", "top_k": True},  # bool 不是整數
        {"query": "ok", "top_k": 2**60},
    ]
    for args in cases:
        with pytest.raises(GatewayRejected) as exc:
            gw.check_intent("deep_recall", args, cs)
        view = gw.rejection_view(exc.value)
        assert view["status"] == "rejected" and view["reason_code"] == "invalid_tool_args" and view["fields"]
        assert all(f.startswith("/") for f in view["fields"])
        assert SECRET not in json.dumps(view, ensure_ascii=False)
        assert SECRET not in str(exc.value) and str(exc.value) == "參數不符合 schema"  # 例外訊息也不含值
    with pytest.raises(GatewayRejected) as exc:
        gw.check_intent("deep_recall", {"query": "ok", "extra": SECRET}, cs)
    assert exc.value.fields == ("/extra",)


def test_gateway_nested_array_item_pointer(gw_rig):
    _, gw, cs = gw_rig
    with pytest.raises(GatewayRejected) as exc:
        gw.check_intent("notify.user", {"message": "hi", "tags": ["a", 5, SECRET], "venue": None}, cs, injected={"venue": "x"})
    assert exc.value.code == PARAM_NOT_ALLOWED  # 模型提供了 injected 欄位 -> 先擋
    with pytest.raises(GatewayRejected) as exc:
        gw.check_intent("notify.user", {"message": "hi", "tags": ["a", 5, SECRET]}, cs, injected={"venue": "x"})
    assert exc.value.fields == ("/tags/1",)


def test_gateway_injected_params_not_allowed_from_llm_and_injected_by_core(gw_rig):
    reg, gw, cs = gw_rig
    for supplied in ("model-venue", None, ""):
        with pytest.raises(GatewayRejected) as exc:
            gw.check_intent("notify.user", {"message": "hi", "venue": supplied}, cs, injected={"venue": "core"})
        assert exc.value.code == PARAM_NOT_ALLOWED and exc.value.fields == ("/venue",)
        assert "model-venue" not in json.dumps(gw.rejection_view(exc.value))
    out = gw.check_intent("notify.user", {"message": "hi"}, cs, injected={"venue": "core-desktop"})
    assert out == {"message": "hi", "venue": "core-desktop"}
    # LLM 面（capability_set／render）看不到 injected 欄位
    assert "venue" not in json.dumps(render_tools(cs))
    assert "venue" not in reg.effective_input_schema("notify.user")["properties"]
    # Core 沒提供注入值 -> 組裝錯誤，fail-closed
    with pytest.raises(GatewayRejected) as exc:
        gw.check_intent("notify.user", {"message": "hi"}, cs)
    assert exc.value.code == "injected_param_missing"


def test_gateway_uses_variant_from_capability_set_not_from_model(gw_rig):
    reg, gw, _ = gw_rig
    discord = reg.project({"chatroom.read"}, ProjectionContext(platform="discord"))
    desktop = reg.project({"chatroom.read"}, ProjectionContext(platform="desktop"))
    args = {"message": "hi", "urgency": "high"}
    with pytest.raises(GatewayRejected) as exc:
        gw.check_intent("notify.user", args, discord, injected={"venue": "d"})
    assert exc.value.fields == ("/urgency",)
    assert gw.check_intent("notify.user", args, desktop, injected={"venue": "d"})["urgency"] == "high"
    # capability_set 宣告了註冊表沒有的變體 -> 視為未註冊，不信任
    forged = {"tools": [{"tool": "notify.user", "variant": "nowhere"}]}
    with pytest.raises(GatewayRejected) as exc:
        gw.check_intent("notify.user", args, forged, injected={"venue": "d"})
    assert exc.value.code == "tool_unknown"


def test_gateway_capability_missing_still_first_and_without_registry_is_unchanged(gw_rig):
    _, gw, cs = gw_rig
    with pytest.raises(GatewayRejected) as exc:
        gw.check_intent("force_remember", {"content": "x"}, cs)
    assert exc.value.code == CAPABILITY_MISSING
    with pytest.raises(GatewayRejected) as exc:
        gw.check_intent("deep_recall", ["not", "a", "mapping"], cs)
    assert exc.value.code == "args_not_mapping"
    legacy = ToolGateway()  # 沒給 registry：維持切片 A 行為（只做 capability 檢查）
    assert legacy.check_intent("deep_recall", {"anything": 1}, cs) == {"anything": 1}
    with pytest.raises(GatewayRejected):
        legacy.check_intent("force_remember", {}, cs)


def test_result_view_shape_is_unchanged_unless_reason_given():
    gw = ToolGateway()
    assert set(gw.result_view("ok")) == {"items", "status", "degraded", "partial"}
    assert set(gw.result_view("rejected", reason_code="x", fields=["/a"])) == {
        "items", "status", "degraded", "partial", "reason_code", "fields",
    }
    assert "fields" not in gw.result_view("rejected", reason_code="capability_missing")


def test_validate_args_boundaries():
    schema = strict_schema({
        "type": "object", "required": ["s"],
        "properties": {
            "s": {"type": "string", "minLength": 2, "maxLength": 4, "pattern": "^[a-z]+$"},
            "e": {"type": "string", "enum": ["a", "b"]},
            "f": {"type": "number", "minimum": 0, "maximum": 1},
        },
    })
    assert validate_args(schema, {"s": "abc", "e": "a", "f": 1}) == []
    assert validate_args(schema, {"s": "a"}) == ["/s"]
    assert validate_args(schema, {"s": "abcde"}) == ["/s"]
    assert validate_args(schema, {"s": "ABC"}) == ["/s"]
    assert validate_args(schema, {"s": "abc", "e": "c"}) == ["/e"]
    assert validate_args(schema, {"s": "abc", "f": float("nan")}) == ["/f"]
    assert validate_args(schema, {"s": "abc", "f": 1.5}) == ["/f"]
    assert validate_args(schema, "nope") == ["/"]
    assert validate_args(schema, {"s": "abc", "a/b": 1}) == ["/a~1b"]  # RFC 6901 跳脫
    many = {f"k{i}": 1 for i in range(100)}
    assert len(validate_args(schema, {"s": "abc", **many})) == 16  # 回餵欄位數有上限


# ---------------------------------------------------------------------------
# 靜態：uep/ 內沒有手寫的 tools 清單（D60 CI 斷言）
# ---------------------------------------------------------------------------

# 白名單（路徑相對 uep/，值＝理由）。新增項目必須有理由並經審查；目標是隨 Runtime 落地（切片 C）逐步清空。
TOOLS_LIST_ALLOWLIST: dict[str, str] = {
    "tools/render.py": "D60 唯一產生點",
    "tools/registry.py": "S-A 保留的舊衍生路徑 llm_definition：由註冊表條目衍生，供 access.tool_definitions／host 的舊 think.tools 使用，"
                         "黃金測試鎖位元組；think_llm.tools 刪除（切片 C）後移除",
    "stream/adapters/memory_echo.py": "echo_memory 後端宣告的 OpenAI 包裝（function_tool_definition），只提供『哪些工具可用』的名稱給 "
                                       "access.tool_definitions，schema 一律改由註冊表衍生；後端宣告降為實作細節後移除",
}


def _hand_written_tool_dicts(source: str) -> list[int]:
    """找出形如 ``{"type": "function", "function": ...}`` 的 dict 字面值或 ``dict(type="function", ...)``；
    帶 ``id`` 的是 assistant 訊息歷史的 ``tool_calls`` 項目（不是 tools 清單），不算。"""
    hits: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Dict):
            pairs = {k.value: v for k, v in zip(node.keys, node.values, strict=True)
                     if isinstance(k, ast.Constant) and isinstance(k.value, str)}
            kind = pairs.get("type")
            if isinstance(kind, ast.Constant) and kind.value == "function" and "id" not in pairs:
                hits.append(node.lineno)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "dict":
            kw = {k.arg: k.value for k in node.keywords if k.arg}
            kind = kw.get("type")
            if isinstance(kind, ast.Constant) and kind.value == "function" and "id" not in kw:
                hits.append(node.lineno)
    return hits


def test_scanner_detects_hand_written_tools_and_skips_tool_call_history():
    assert _hand_written_tool_dicts('x = {"type": "function", "function": {"name": "a"}}')
    assert _hand_written_tool_dicts('x = [dict(type="function", function={})]')
    assert not _hand_written_tool_dicts('x = {"type": "function", "id": "c1", "function": {}}')
    assert not _hand_written_tool_dicts('x = {"type": "text"}')


def test_no_hand_written_tools_list_in_uep_outside_allowlist():
    offenders: dict[str, list[int]] = {}
    seen_allowed: set[str] = set()
    for path in sorted(UEP_ROOT.rglob("*.py")):
        rel = path.relative_to(UEP_ROOT).as_posix()
        hits = _hand_written_tool_dicts(path.read_text(encoding="utf-8"))
        if not hits:
            continue
        if rel in TOOLS_LIST_ALLOWLIST:
            seen_allowed.add(rel)
        else:
            offenders[rel] = hits
    assert not offenders, f"uep/ 內出現未列入白名單的手寫 tools 清單：{offenders}"
    assert seen_allowed == set(TOOLS_LIST_ALLOWLIST), "白名單有已失效的項目：請移除"


def test_render_py_is_the_only_new_path_that_builds_function_tools():
    """新路徑（投影 -> render）的 tools JSON 只來自 render.py：registry 內的 project 不產生 function 包裝。"""
    source = (UEP_ROOT / "tools" / "registry.py").read_text(encoding="utf-8")
    project_src = source[source.index("    def project("):]
    assert '"type": "function"' not in project_src
    assert visible_tools({"tools": [{"tool": "a"}]}) == {"a"}


def test_warm_required_is_warming_before_first_ready_event(warm_registry):
    """fail-closed：warm_required 工具在第一個 ready 事件前不投出、進 pending；其餘已知模組預設 ready。"""
    cs = warm_registry.project({"memory.read"}, ProjectionContext())
    assert names(cs) == ["deep_recall"]
    assert [dict(p) for p in cs["pending"]] == [{"tool": "speak.tool", "reason": "warming"}]
    FakeModuleCoordinator(warm_registry).emit("ready")
    assert names(warm_registry.project({"memory.read"}, ProjectionContext())) == ["deep_recall", "speak.tool"]
    plain = ToolRegistry.build([make_entry("plain.tool", requires=("m",))], known_modules=["m"])
    assert names(plain.project({"memory.read"}, ProjectionContext())) == ["plain.tool"]


# ---------------------------------------------------------------------------
# AgentCore 搭配 ToolGateway(registry)（無 DB：TaskManager 以記錄用替身取代）
# ---------------------------------------------------------------------------

class _StubTasks:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []
        self.executed: list[dict[str, Any]] = []

    def create_action(self, **kw: Any) -> None:
        self.created.append(kw)

    def execute_action(self, action_id: str, params: Any, *, worker_epoch: int):
        from uep.tasks.manager import ActionOutcome
        from uep.tasks.states import ActionStatus

        self.executed.append(dict(params))
        return ActionOutcome(action_id, ActionStatus.SUCCEEDED, True, None)


def _agent_rig(args: dict[str, Any], *, with_registry: bool = True):
    from uep.agent.core import AgentCore
    from uep.fakes import runtime as rt
    from uep.fakes.runtime import FakeRuntime

    reg = ToolRegistry.build([make_entry(
        "notify.user", spec={"capability": "chatroom.read"},
        descriptor=ToolDescriptor("n", _NOTIFY_SCHEMA, injected_params=("venue",)),
    )])
    cs = reg.project({"chatroom.read"}, ProjectionContext())
    runtime, tasks = FakeRuntime(), _StubTasks()
    request = _request(cs)
    runtime.script_for(request.attempt_id, [rt.tool_intent("notify.user", args), rt.result()])
    gateway = ToolGateway(reg if with_registry else None)
    core = AgentCore(runtime, tasks, gateway, worker_epoch=1, injected={"venue": "core-desktop"})  # type: ignore[arg-type]
    outcome = core.run_step(run_id="run_1", step_id="step_1", request=request)
    return outcome, tasks, runtime, request


def test_agent_core_injected_value_reaches_action_and_llm_value_does_not():
    outcome, tasks, _, _ = _agent_rig({"message": "hi"})
    assert outcome.intents[0].status == "unknown"  # 替身 outcome 無 receipt；重點在 params
    assert tasks.created[0]["params"] == {"message": "hi", "venue": "core-desktop"}
    assert tasks.executed == [{"message": "hi", "venue": "core-desktop"}]

    outcome, tasks, runtime, req = _agent_rig({"message": "hi", "venue": "model-venue"})
    assert outcome.intents[0].status == "rejected" and outcome.intents[0].reason == "param_not_allowed"
    assert tasks.created == [] and tasks.executed == []  # 不建 action
    view = runtime.tool_results[req.attempt_id][0][1]
    assert view["reason_code"] == "param_not_allowed" and view["fields"] == ["/venue"]
    assert "model-venue" not in json.dumps(view)


def test_agent_core_rejected_view_carries_reason_and_no_user_values():
    _outcome, tasks, runtime, req = _agent_rig({"message": SECRET, "urgency": SECRET, "extra": SECRET})
    assert tasks.created == []
    view = runtime.tool_results[req.attempt_id][0][1]
    assert view["status"] == "rejected" and view["reason_code"] == "invalid_tool_args"
    assert sorted(view["fields"]) == ["/extra", "/urgency"]
    assert SECRET not in json.dumps(view, ensure_ascii=False)


def test_agent_core_without_registry_keeps_legacy_behavior():
    _outcome, tasks, _runtime, _req = _agent_rig({"anything": 1}, with_registry=False)
    assert tasks.created[0]["params"] == {"anything": 1}  # 沒有 registry：原樣建 action
