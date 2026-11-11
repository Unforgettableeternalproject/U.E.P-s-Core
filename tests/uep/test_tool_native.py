"""RA-2／RA-2a：原生工具（native）免 Core 內部核准的註冊期斷言（離線）。執行期見 test_tool_native_db.py。"""
from __future__ import annotations

import json

import pytest

from tests.uep.test_tool_registry import _OBJ, make_entry, rule_of
from uep.adapters.tool import CAPABILITY_CEILING, NATIVE_EXEMPT_CAPABILITIES
from uep.tasks.manager import params_hash
from uep.tools.catalog.memory import memory_registry
from uep.tools.lockfile import current_registry
from uep.tools.registry import ToolRegistry, compute_schema_hash, load_lockfile

_NATIVE = {
    "side_effect_class": "external_effect", "capability": "chatroom.post", "idempotency": "via_action_id", "native": True,
}


def test_exempt_set_is_the_closed_pair():
    assert NATIVE_EXEMPT_CAPABILITIES == frozenset({"lore.write", "chatroom.post"})
    assert NATIVE_EXEMPT_CAPABILITIES <= set(CAPABILITY_CEILING)


def test_native_external_effect_registers_without_approval():
    spec = ToolRegistry.build([make_entry(spec=_NATIVE)]).resolve("demo.tool")
    assert (spec.native, spec.side_effect_class, spec.approval_level) == (True, "external_effect", "none")  # 照實分類
    lore = {**_NATIVE, "capability": "lore.write"}  # lore.write 上限已調為 external_effect
    assert ToolRegistry.build([make_entry(spec=lore)]).resolve("demo.tool").capability == "lore.write"


def test_non_native_external_effect_still_needs_approval():
    assert rule_of(make_entry(spec={**_NATIVE, "native": False})) == "RA-2"


def test_native_with_non_closed_capability_fails():
    # test.approval 上限雖是 external_effect，也不在閉集內
    assert rule_of(make_entry(spec={**_NATIVE, "capability": "test.approval"})) == "RA-2a"
    assert rule_of(
        make_entry(spec={**_NATIVE, "side_effect_class": "reversible_local", "capability": "memory.write"})
    ) == "RA-2a"


def test_native_high_impact_fails():
    # 即使 capability 在閉集內也不豁免（上限同時擋 RA-6；RA-2 先於 RA-6）
    assert rule_of(make_entry(spec={**_NATIVE, "side_effect_class": "high_impact"})) == "RA-2"
    assert rule_of(
        make_entry(spec={**_NATIVE, "side_effect_class": "high_impact", "approval_level": "per_action"})
    ) == "RA-2a"


@pytest.mark.parametrize("bad", [
    {"origin": "mcp_static"}, {"origin": "platform_approved"}, {"approval_level": "per_action"},
    {"provider": "mcp_x", "targeting": "platform_remote"},
])
def test_native_requires_builtin_core_local_and_no_approval(bad):
    assert rule_of(make_entry(spec={**_NATIVE, **bad})) == "RA-2a"


def test_lore_write_ceiling_raised_and_memory_tools_unaffected():
    assert CAPABILITY_CEILING["lore.write"] == "external_effect"
    assert CAPABILITY_CEILING["memory.write"] == "reversible_local"
    assert not any(s.native for s in memory_registry().specs().values())  # 既有記憶工具（含 force_remember）皆非 native


def test_native_enters_schema_hash_only_when_true():
    plain = make_entry(spec={**_NATIVE, "native": False, "approval_level": "per_action"})
    assert compute_schema_hash(plain) != compute_schema_hash(make_entry(spec=_NATIVE))
    # native=False 與舊公式（無 native 鍵）逐位元相同：既有 lockfile 條目的 hash 不變
    legacy = params_hash("schema_hash", 0, {
        "name": plain.name, "input_schema": json.loads(json.dumps(_OBJ)), "side_effect_class": "external_effect",
        "idempotency": "via_action_id", "approval_level": "per_action", "targeting": "core_local",
    })
    assert compute_schema_hash(plain) == legacy


def test_existing_lockfile_hashes_unchanged():
    lock = load_lockfile()
    snap = current_registry().lock_snapshot()
    assert set(snap) == set(lock)
    assert all(snap[k]["schema_hash"] == lock[k]["schema_hash"] for k in lock)
