"""工具註冊表單一來源（Agent 架構提案 §9.1 切片 A）：黃金測試、RA-1～16 斷言（含反例）、lockfile、fail-closed。"""
from __future__ import annotations

import ast
import dataclasses
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from uep.adapters.tool import (
    CAPABILITY_CEILING,
    VISIBLE_ORIGINS,
    CompositeSpec,
    CompositeStep,
    EnumerableToolSpecResolver,
    ToolDescriptor,
    ToolEntry,
    ToolScope,
    ToolSpec,
    ToolVariant,
)
from uep.echo.host import EchoPipelineHost
from uep.echo.memory_wiring import EchoMemoryToolBackend
from uep.memory import access
from uep.memory.access import MemoryAccessLayer
from uep.tasks.manager import PolicyViolation
from uep.tools.catalog.memory import (
    DEEP_RECALL_SCHEMA,
    FORCE_REMEMBER_DESCRIPTION,
    FORCE_REMEMBER_SCHEMA,
    GUILD_ROSTER_CONTEXT,
    MEMORY_OUTPUT_SCHEMA,
    memory_entries,
    memory_registry,
)
from uep.tools.lockfile import current_registry, render_lockfile
from uep.tools.registry import (
    LOCKFILE_PATH,
    StaticToolRegistry,
    ToolRegistry,
    ToolRegistryError,
    compute_schema_hash,
    load_lockfile,
)

GOLDEN = Path(__file__).parent / "fixtures" / "think_tools_golden.json"
ACCESS_SRC = Path(access.__file__)

_OBJ: dict[str, Any] = {
    "type": "object",
    "properties": {"path": {"type": "string"}, "n": {"type": "integer"}, "tags": {"type": "array"}},
    "required": ["path"],
}
_OUT: dict[str, Any] = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
_WRITE = {"side_effect_class": "reversible_local", "capability": "memory.write"}
_POST = {"side_effect_class": "external_effect", "capability": "chatroom.post", "approval_level": "per_action"}


def make_entry(name: str = "demo.tool", spec: dict[str, Any] | None = None, **over: Any) -> ToolEntry:
    base_spec: dict[str, Any] = {
        "executor": "fake", "side_effect_class": "internal_read", "idempotency": "native",
        "presence_bound": False, "tool_version": 1, "capability": "memory.read",
    }
    base_spec.update(spec or {})
    fields: dict[str, Any] = {
        "name": name, "spec": ToolSpec(**base_spec), "descriptor": ToolDescriptor("demo", _OBJ), "output_schema": _OUT,
    }
    fields.update(over)
    return ToolEntry(**fields)


def rule_of(*entries: ToolEntry, **kwargs: Any) -> str:
    with pytest.raises(ToolRegistryError) as exc:
        ToolRegistry.build(entries, **kwargs)
    return exc.value.rule


def _layer() -> MemoryAccessLayer:
    return MemoryAccessLayer(
        runtime=object(), identity=object(), tool_backend=EchoMemoryToolBackend(),  # type: ignore[arg-type]
        guild_membership=object(),  # type: ignore[arg-type]
    )


# ---- 1. 黃金測試：衍生的 tools JSON 與舊 think.tools 位元組級相等 ----

def _golden_text() -> str:
    return GOLDEN.read_text(encoding="utf-8")


def test_golden_registry_derivation_is_byte_identical():
    derived = json.dumps(memory_registry().llm_definitions(), ensure_ascii=False, indent=2) + "\n"
    assert derived.encode("utf-8") == _golden_text().encode("utf-8")


def test_golden_access_layer_tool_definitions_with_real_backend():
    derived = json.dumps(_layer().tool_definitions(), ensure_ascii=False, indent=2) + "\n"
    assert derived.encode("utf-8") == _golden_text().encode("utf-8")


@pytest.mark.parametrize("guild_id", [None, "", "123"])
def test_golden_host_refresh_tools_guild_gate(guild_id):
    """member_recall 只在有 guild 的 session 曝光；其餘輸出與黃金檔一致。"""
    fake = SimpleNamespace(
        _memory=_layer(), tool_executor=object(), think=SimpleNamespace(tools=[]),
        _session=SimpleNamespace(guild_id=guild_id),
    )
    EchoPipelineHost._refresh_tools(fake)  # type: ignore[arg-type]
    expected = json.loads(_golden_text())
    if guild_id is None:  # 空字串與舊版一致：視為有 guild（只有 None 才是沒有）
        expected = [d for d in expected if d["function"]["name"] != "member_recall"]
    assert json.dumps(fake.think.tools, ensure_ascii=False) == json.dumps(expected, ensure_ascii=False)


def test_access_layer_without_backend_or_membership_exposes_nothing():
    assert MemoryAccessLayer(runtime=object(), identity=object()).tool_definitions() == []  # type: ignore[arg-type]


# ---- 2. access.py 沒有獨立的 schema 字面值，分類由註冊表衍生 ----

def test_access_py_has_no_schema_literal():
    tree = ast.parse(ACCESS_SRC.read_text(encoding="utf-8"))
    schema_keys = {"parameters", "properties", "description"}
    banned = {"internal_read", "reversible_local", "deep_recall", "force_remember"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            keys = {k.value for k in node.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)}
            assert not (keys & schema_keys), f"access.py:{node.lineno} 有獨立的 schema 字面值"
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert node.value not in banned, f"access.py:{node.lineno} 有硬編碼的工具分類／名稱字面值"


def test_tool_classes_are_derived_from_registry():
    assert dict(access.TOOL_CLASSES) == memory_registry().tool_classes() == {
        "force_remember": "reversible_local", "deep_recall": "internal_read", "member_recall": "internal_read",
    }
    with pytest.raises(TypeError):
        access.TOOL_CLASSES["x"] = "internal_read"  # type: ignore[index]


def test_memory_entries_are_builtin_global():
    for entry in memory_registry().entries().values():
        assert entry.spec.origin == "builtin" and entry.spec.provider == "core" and entry.spec.targeting == "core_local"
        assert entry.scope.platforms == frozenset()  # global
    assert memory_registry().entry("member_recall").scope.requires_context == frozenset({GUILD_ROSTER_CONTEXT})


def test_catalog_schemas_match_subsystem_declarations():
    """漂移測試：凍結副本 vs 子系統現值（測試可 import 子系統，uep/ 不行）。"""
    from llm_integration.tools.definitions import TOOL_DECLARATIONS

    from uep.stream.adapters.memory_echo import (
        FORCE_REMEMBER_DESCRIPTION as ADAPTER_DESC,
    )
    from uep.stream.adapters.memory_echo import function_tool_definition

    known = {d["name"]: d for d in TOOL_DECLARATIONS}
    assert FORCE_REMEMBER_DESCRIPTION == ADAPTER_DESC
    for name in ("force_remember", "deep_recall"):
        assert memory_registry().llm_definition(name) == function_tool_definition(known[name])
    assert FORCE_REMEMBER_SCHEMA == known["force_remember"]["parameters"]
    assert DEEP_RECALL_SCHEMA == known["deep_recall"]["parameters"]


# ---- 4. fail-closed 預設 ----

def test_undeclared_concurrency_defaults_to_serial():
    assert make_entry().concurrency == "serial"
    assert ToolRegistry.build([make_entry()]).entry("demo.tool").concurrency == "serial"
    assert all(e.concurrency == "serial" for e in memory_registry().entries().values())


def test_fail_closed_defaults_on_new_fields():
    entry = make_entry()
    assert (entry.spec.approval_level, entry.spec.interruptible) == ("none", "non_interruptible")
    assert (entry.cancellable, entry.timeout_s, entry.conflict_keys, entry.disabled) == (False, None, (), False)
    assert (entry.requires, entry.load_cost, entry.warm_required) == ((), "none", False)
    assert entry.result_scope == "private" and entry.scope.data_scope == "private"


def test_old_tool_spec_construction_still_works_and_registry_is_resolver():
    spec = ToolSpec("e", "external_effect", "via_action_id", True, 3)  # D91：舊的五欄位建構不變
    assert (spec.origin, spec.provider, spec.targeting) == ("builtin", "core", "core_local")
    registry = memory_registry()
    assert isinstance(registry, EnumerableToolSpecResolver)
    assert registry.resolve("deep_recall") is registry.specs()["deep_recall"]
    with pytest.raises(PolicyViolation):
        registry.resolve("nope")
    with pytest.raises(TypeError):
        registry.specs()["x"] = spec  # type: ignore[index]
    assert StaticToolRegistry({"t": spec}).resolve("t") is spec  # 相容：StaticToolRegistry 保留


# ---- 3. RA 斷言（每條至少一個反例）----

def test_valid_registry_builds():
    assert ToolRegistry.build([make_entry()]).resolve("demo.tool").capability == "memory.read"


@pytest.mark.parametrize("bad", [
    {"origin": "weird"}, {"side_effect_class": "nuke"}, {"idempotency": "maybe"},
    {"approval_level": "always"}, {"interruptible": "yes"}, {"targeting": "cloud"},
])
def test_ra1_enum_domain(bad):
    assert rule_of(make_entry(spec=bad)) == "RA-1"


def test_ra1_entry_level_enums_and_version():
    assert rule_of(make_entry(concurrency="turbo")) == "RA-1"
    assert rule_of(make_entry(load_cost="huge")) == "RA-1"
    assert rule_of(make_entry(spec={"tool_version": 0})) == "RA-1"
    assert rule_of(make_entry(spec={"tool_version": True})) == "RA-1"
    assert rule_of(make_entry(scope=ToolScope(presence="bound"))) == "RA-1"  # 與 presence_bound 矛盾


@pytest.mark.parametrize("effect", ["external_effect", "high_impact"])
def test_ra2_external_requires_approval(effect):
    spec = {"side_effect_class": effect, "capability": "chatroom.post", "idempotency": "via_action_id"}
    assert rule_of(make_entry(spec=spec)) == "RA-2"
    if effect == "external_effect":
        ok = make_entry(spec={**spec, "approval_level": "per_action"}, output_schema=None)
        assert ToolRegistry.build([ok]).resolve("demo.tool").approval_level == "per_action"


def test_ra3_non_visible_origins_never_projected():
    entries = [
        make_entry("demo.visible"),
        make_entry("demo.found", spec={"origin": "discovered"}),
        make_entry("demo.declared", spec={"origin": "platform_declared"}),
        make_entry("demo.off", disabled=True),
    ]
    registry = ToolRegistry.build(entries)
    assert [e.name for e in registry.visible_entries()] == ["demo.visible"]
    assert list(registry.tool_classes()) == ["demo.visible"]
    assert [d["function"]["name"] for d in registry.llm_definitions()] == ["demo.visible"]
    assert registry.llm_definitions(["demo.found", "demo.declared", "demo.off"]) == []
    for name in ("demo.found", "demo.declared", "demo.off"):
        with pytest.raises(PolicyViolation):
            registry.llm_definition(name)
    assert VISIBLE_ORIGINS == {"builtin", "mcp_static", "platform_approved"}


def test_ra4_provider_targeting_iff():
    assert rule_of(make_entry(spec={"provider": "desktop"})) == "RA-4"
    assert rule_of(make_entry(spec={"targeting": "platform_remote"})) == "RA-4"
    ok = make_entry(spec={"provider": "desktop", "targeting": "platform_remote"})
    assert ToolRegistry.build([ok]).resolve("demo.tool").provider == "desktop"


def test_ra5_interruptible_requires_cancellable():
    assert rule_of(make_entry(spec={"interruptible": "interruptible"})) == "RA-5"
    ok = make_entry(spec={"interruptible": "checkpoint_only"}, cancellable=True)
    assert ToolRegistry.build([ok]).entry("demo.tool").cancellable


def test_ra6_capability_closed_set_and_ceiling():
    assert rule_of(make_entry(spec={"capability": ""})) == "RA-6"
    assert rule_of(make_entry(spec={"capability": "memory.*"})) == "RA-6"
    assert rule_of(make_entry(spec={"capability": "memory.read", "side_effect_class": "reversible_local"})) == "RA-6"
    assert CAPABILITY_CEILING["memory.write"] == "reversible_local"


def test_ra7_schema_linter_and_injected_params():
    def with_schema(schema: dict[str, Any], **kw: Any) -> ToolEntry:
        return make_entry(descriptor=ToolDescriptor("d", schema, **kw))

    assert rule_of(with_schema({"type": "string"})) == "RA-7"
    assert rule_of(with_schema({"type": "object", "properties": {"a": {"$ref": "#/x"}}})) == "RA-7"
    assert rule_of(with_schema({"type": "object", "properties": {"a": {"description": "no type"}}})) == "RA-7"
    assert rule_of(with_schema({"type": "object", "properties": {"a": {"type": "string"}}, "required": ["b"]})) == "RA-7"
    assert rule_of(with_schema(_OBJ, injected_params=("path",))) == "RA-7"
    ToolRegistry.build([with_schema(_OBJ, injected_params=("venue",))])  # 注入欄位不在 LLM 面：通過


def test_ra8_lockfile_monotonic():
    entry = make_entry()
    digest = compute_schema_hash(entry)
    lock = {"demo.tool": {"tool_version": 1, "schema_hash": digest}}
    ToolRegistry.build([entry], lockfile=lock)  # 一致：通過
    changed = make_entry(descriptor=ToolDescriptor("d", {**_OBJ, "required": []}))
    assert rule_of(changed, lockfile=lock) == "RA-8"  # hash 變而 version 未升
    bumped = dataclasses.replace(changed, spec=dataclasses.replace(changed.spec, tool_version=2))
    ToolRegistry.build([bumped], lockfile=lock)  # 升版：通過
    assert rule_of(make_entry(), lockfile={"demo.tool": {"tool_version": 2, "schema_hash": digest}}) == "RA-8"  # 倒退
    assert rule_of(make_entry(schema_hash="0" * 64)) == "RA-8"  # 不信任手填


def test_schema_hash_ignores_description_and_version_but_not_classification():
    base = make_entry()
    reworded = make_entry(descriptor=ToolDescriptor("another words", _OBJ))
    bumped = make_entry(spec={"tool_version": 9})
    reclassified = make_entry(spec={"approval_level": "per_action"})
    assert compute_schema_hash(base) == compute_schema_hash(reworded) == compute_schema_hash(bumped)
    assert compute_schema_hash(base) != compute_schema_hash(reclassified)


def test_ra9_name_unique_and_charset():
    assert rule_of(make_entry("demo.tool"), make_entry("demo.tool")) == "RA-9"
    for bad in ("Demo.tool", "1abc", "ab", "a" * 65, "demo-tool"):
        assert rule_of(make_entry(bad)) == "RA-9"


def test_ra10_mcp_static_cannot_point_to_echo_mcp():
    remote = {"origin": "mcp_static", "provider": "mcp", "targeting": "platform_remote"}
    assert rule_of(make_entry(spec={**remote, "executor": "echo-mcp"})) == "RA-10"
    ToolRegistry.build([make_entry(spec={**remote, "executor": "mcp_lore"})])


def test_ra11_concurrency_eligibility():
    safe = {"concurrency": "parallel_safe", "timeout_s": 5.0}
    ToolRegistry.build([make_entry(**safe)])
    assert rule_of(make_entry(concurrency="parallel_safe")) == "RA-11"  # 缺 timeout_s
    assert rule_of(make_entry(**safe, spec=_WRITE)) == "RA-11"  # 非 internal_read
    assert rule_of(make_entry(**safe, spec={"approval_level": "per_action"})) == "RA-11"  # 需核准者恆 serial
    one_shot: dict[str, Any] = {
        "concurrency": "parallel_one_shot", "timeout_s": 5.0, "cancellable": True,
        "conflict_keys": ("file:{path}",),
    }
    ok_spec = {**_WRITE, "idempotency": "via_action_id"}
    ToolRegistry.build([make_entry(**one_shot, spec=ok_spec)])
    assert rule_of(make_entry(**{**one_shot, "cancellable": False}, spec=ok_spec)) == "RA-11"
    assert rule_of(make_entry(**one_shot, spec={**_WRITE, "idempotency": "none"})) == "RA-11"
    assert rule_of(make_entry(**one_shot, spec={**_POST, "idempotency": "via_action_id"})) == "RA-11"


def test_ra12_conflict_keys():
    base: dict[str, Any] = {"concurrency": "parallel_one_shot", "timeout_s": 5.0, "cancellable": True}
    write = {**_WRITE, "idempotency": "native"}
    ToolRegistry.build([make_entry(**base, spec=write, conflict_keys=("file:{path}", "tool:{name}"))])
    assert rule_of(make_entry(**base, spec=write)) == "RA-12"  # 寫入者缺 conflict_keys
    assert rule_of(make_entry(conflict_keys=("file:{missing}",))) == "RA-12"  # 參數不存在
    assert rule_of(make_entry(conflict_keys=("tags:{tags}",))) == "RA-12"  # 非純量
    ToolRegistry.build([make_entry(**base)])  # 唯讀不強制


def test_ra13_variants_only_narrow():
    narrowed = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}
    scope = ToolScope(
        platforms=frozenset({"desktop"}), variants={"desktop": ToolVariant(description="桌面", input_schema=narrowed)}
    )
    registry = ToolRegistry.build([make_entry(scope=scope)])
    assert "demo.tool@desktop" in registry.schema_hashes()

    def variant_scope(schema: dict[str, Any], **kw: Any) -> ToolScope:
        return ToolScope(variants={"desktop": ToolVariant(input_schema=schema)}, **kw)

    wider = {"type": "object", "properties": {"path": {"type": "string"}, "extra": {"type": "string"}}}
    assert rule_of(make_entry(scope=variant_scope(wider))) == "RA-13"
    retyped = {"type": "object", "properties": {"path": {"type": "integer"}}}
    assert rule_of(make_entry(scope=variant_scope(retyped))) == "RA-13"
    more_required = {
        "type": "object", "properties": {"path": {"type": "string"}, "n": {"type": "integer"}},
        "required": ["path", "n"],
    }
    assert rule_of(make_entry(scope=variant_scope(more_required))) == "RA-13"
    other_platform = ToolScope(platforms=frozenset({"discord"}), variants={"desktop": ToolVariant()})
    assert rule_of(make_entry(scope=other_platform)) == "RA-13"
    with pytest.raises(TypeError):  # 變體根本無法宣告分類欄
        ToolVariant(side_effect_class="high_impact")  # type: ignore[call-arg]
    enum_schema = {"type": "object", "properties": {"m": {"type": "string", "enum": ["a", "b"]}}}
    wide_enum = {"type": "object", "properties": {"m": {"type": "string", "enum": ["a", "b", "c"]}}}
    enum_base = make_entry(descriptor=ToolDescriptor("d", enum_schema), scope=variant_scope(wide_enum))
    assert rule_of(enum_base) == "RA-13"


def test_ra14_composite():
    leaf_r = make_entry("demo.read")
    leaf_w = make_entry("demo.write", spec=_WRITE)
    steps = CompositeSpec(steps=(CompositeStep("demo.read"), CompositeStep("demo.write")))
    comp = make_entry("demo.combo", spec=_WRITE, composite=steps)
    ToolRegistry.build([leaf_r, leaf_w, comp])
    low = dataclasses.replace(
        comp, spec=dataclasses.replace(comp.spec, side_effect_class="internal_read", capability="memory.read")
    )
    assert rule_of(leaf_r, leaf_w, low) == "RA-14"  # 宣告低於子步驟最大值
    ghost = dataclasses.replace(comp, composite=CompositeSpec(steps=(CompositeStep("demo.ghost"),)))
    assert rule_of(leaf_r, ghost) == "RA-14"  # 子步驟未註冊
    nested = make_entry("demo.outer", spec=_WRITE, composite=CompositeSpec(steps=(CompositeStep("demo.combo"),)))
    assert rule_of(leaf_r, leaf_w, comp, nested) == "RA-14"  # 巢狀
    parallel = dataclasses.replace(comp, concurrency="parallel_safe", timeout_s=3.0)
    assert rule_of(leaf_r, leaf_w, parallel) in {"RA-11", "RA-14"}  # composite 不得並行
    leaf_p = make_entry("demo.appr", spec=_POST, output_schema=None)
    combo_p = make_entry("demo.combo2", spec=_POST, output_schema=None,
                         composite=CompositeSpec(steps=(CompositeStep("demo.appr"),)))
    ToolRegistry.build([leaf_p, combo_p])
    weak = dataclasses.replace(combo_p, spec=dataclasses.replace(combo_p.spec, approval_level="none"))
    assert rule_of(leaf_p, weak) in {"RA-2", "RA-14"}


def test_ra15_requires_and_readiness():
    assert rule_of(make_entry(warm_required=True)) == "RA-15"
    entry = make_entry(requires=("gpu:indextts",), load_cost="heavy", warm_required=True)
    assert ToolRegistry.build([entry]).readiness("demo.tool") == "unavailable"  # 未知模組：fail-closed
    assert ToolRegistry.build([entry], known_modules=["gpu:indextts"]).readiness("demo.tool") == "ready"
    assert ToolRegistry.build([make_entry()]).readiness("demo.tool") == "ready"


def test_ra16_output_schema_required_and_linted():
    assert rule_of(make_entry(output_schema=None)) == "RA-16"
    assert rule_of(make_entry(spec=_WRITE, output_schema=None)) == "RA-16"
    assert rule_of(make_entry(output_schema={"type": "array"})) == "RA-16"
    ToolRegistry.build([make_entry(spec=_POST, output_schema=None)])  # 其餘僅建議


def test_registry_is_frozen_copy():
    schema = json.loads(json.dumps(_OBJ))
    registry = ToolRegistry.build([make_entry(descriptor=ToolDescriptor("d", schema))])
    schema["properties"]["path"]["type"] = "integer"  # 建構後改原物件不影響註冊表
    assert registry.llm_definition("demo.tool")["function"]["parameters"]["properties"]["path"]["type"] == "string"
    derived = registry.llm_definition("demo.tool")
    derived["function"]["parameters"]["required"].append("zzz")  # 衍生是深複本
    assert registry.llm_definition("demo.tool")["function"]["parameters"]["required"] == ["path"]


# ---- lockfile（CI）----

def test_lockfile_in_sync_with_catalog():
    """tools.lock.json 入版控；改 schema／分類須先升 tool_version，再執行 ``python -m uep.tools.lockfile``。"""
    assert LOCKFILE_PATH.read_text(encoding="utf-8") == render_lockfile(current_registry()), (
        "tools.lock.json 與目錄不同步：升 tool_version 後執行 python -m uep.tools.lockfile"
    )


def test_catalog_builds_against_lockfile():
    ToolRegistry.build(memory_entries(), lockfile=load_lockfile())


def test_catalog_schema_change_without_version_bump_fails_lock():
    entries = memory_entries()
    first = entries[0]
    tampered_schema = json.loads(json.dumps(first.descriptor.input_schema))
    tampered_schema["properties"]["extra"] = {"type": "string"}
    tampered = dataclasses.replace(first, descriptor=ToolDescriptor(first.descriptor.description, tampered_schema))
    with pytest.raises(ToolRegistryError) as exc:
        ToolRegistry.build([tampered, *entries[1:]], lockfile=load_lockfile())
    assert exc.value.rule == "RA-8"


def test_memory_output_schema_shape():
    assert MEMORY_OUTPUT_SCHEMA["required"] == ["success", "result"]


# ---- 審查修正：基準 lockfile 比對、唯讀 schema、值域與錯誤轉換 ----

REPO_ROOT = Path(__file__).resolve().parents[2]
LOCK_REL = "uep/tools/tools.lock.json"


def _base_lockfile() -> dict[str, Any]:
    """基準分支的 lockfile：依序嘗試 env ``UEP_TOOL_LOCK_BASE``、``origin/develop``；取不到就 skip 並寫明原因。
    基準 ref 存在但還沒有 lockfile（本檔案由該 PR 新增）時，基準是空 lockfile。"""
    refs = [r for r in (os.environ.get("UEP_TOOL_LOCK_BASE"), "origin/develop") if r]
    reasons: list[str] = []
    for ref in refs:
        try:
            probe = subprocess.run(
                ["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
                cwd=REPO_ROOT, capture_output=True, text=True, timeout=30, check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            pytest.skip(f"無法執行 git（{type(exc).__name__}），略過基準 lockfile 比對")
        if probe.returncode != 0:
            reasons.append(f"{ref} 不存在（可能是 shallow clone 或未 fetch）")
            continue
        shown = subprocess.run(
            ["git", "show", f"{ref}:{LOCK_REL}"], cwd=REPO_ROOT, capture_output=True, text=True,
            encoding="utf-8", timeout=30, check=False,
        )
        if shown.returncode != 0:
            return {}  # 基準尚無 lockfile：沒有可比對的舊條目
        return json.loads(shown.stdout)
    pytest.skip("取不到基準 lockfile：" + "；".join(reasons or ["沒有可用的基準 ref"]))


def test_catalog_does_not_change_schema_without_version_bump_vs_base():
    """RA-8 的 PR 層防線：同一個 PR 改 schema 又重新產生 lockfile，單看 lockfile 同步測試擋不住；
    這裡改用基準分支的 lockfile 驗證 hash 變而 tool_version 未升。"""
    ToolRegistry.build(memory_entries(), lockfile=_base_lockfile())


def test_base_lockfile_comparison_catches_regenerated_lockfile():
    """上面那個測試的機制本身：基準 lockfile 與現況 hash 不同且版本相同 ⇒ 失敗。"""
    stale = {name: dict(info) for name, info in load_lockfile().items()}
    stale["deep_recall"]["schema_hash"] = "0" * 64
    with pytest.raises(ToolRegistryError) as exc:
        ToolRegistry.build(memory_entries(), lockfile=stale)
    assert exc.value.rule == "RA-8"
    stale["deep_recall"]["tool_version"] = 0  # 基準版本較低 = 本 PR 有升版：通過
    ToolRegistry.build(memory_entries(), lockfile=stale)


def test_entries_are_read_only_to_callers():
    registry = memory_registry()
    for entry in (registry.entry("deep_recall"), *registry.entries().values()):
        schema = entry.descriptor.input_schema
        with pytest.raises(TypeError):
            schema["properties"] = {}  # type: ignore[index]
        with pytest.raises(TypeError):
            next(iter(schema["properties"].values()))["type"] = "integer"  # type: ignore[index]
        assert isinstance(schema["required"], tuple)  # list 已轉 tuple，無法 append
        assert entry.output_schema is not None
        with pytest.raises(TypeError):
            entry.output_schema["properties"]["success"] = {}  # type: ignore[index]
    derived = registry.llm_definitions()
    derived[0]["function"]["parameters"]["properties"].clear()  # 衍生是新物件，改了也不影響註冊表
    assert registry.llm_definitions()[0]["function"]["parameters"]["properties"]


def test_oversized_schema_becomes_ra8_error():
    huge = {"type": "object", "properties": {f"p{i}": {"type": "string", "description": "x" * 2000} for i in range(200)}}
    assert rule_of(make_entry(descriptor=ToolDescriptor("d", huge))) == "RA-8"


def test_ra9_non_string_name():
    for bad in (None, 123, b"abc"):
        assert rule_of(make_entry(name=bad)) == "RA-9"  # type: ignore[arg-type]


def test_ra1_scope_domains():
    assert rule_of(make_entry(result_scope="everyone")) == "RA-1"
    assert rule_of(make_entry(result_scope=None)) == "RA-1"  # type: ignore[arg-type]
    assert rule_of(make_entry(scope=ToolScope(data_scope="wide_open"))) == "RA-1"
    ToolRegistry.build([make_entry(result_scope="group", scope=ToolScope(data_scope="scope_private_owner"))])
