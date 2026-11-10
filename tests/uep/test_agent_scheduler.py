"""平行排程器（切片 D1，§6.4）：分波、屏障、衝突序列化、確定性；純函式，不需 DB。"""
from __future__ import annotations

from typing import Any

import pytest

from uep.adapters.tool import ToolDescriptor, ToolEntry, ToolSpec
from uep.agent.scheduler import (
    DEFERRED_BATCH_LIMIT,
    DEFERRED_BUDGET,
    SchedItem,
    defer_tail,
    plan_waves,
    resource_keys,
)
from uep.tools.registry import ToolRegistry

_IN: dict[str, Any] = {
    "type": "object", "properties": {"path": {"type": "string"}, "n": {"type": "integer"}}, "required": ["path"],
}
_OUT: dict[str, Any] = {"type": "object", "properties": {"ok": {"type": "boolean"}}}


def _entry(name: str, *, concurrency="parallel_safe", keys=("file:{path}",), approval="none", side="internal_read",
           capability="memory.read") -> ToolEntry:
    spec = ToolSpec(
        executor="fake", side_effect_class=side, idempotency="native", presence_bound=False, tool_version=1,
        capability=capability, approval_level=approval,
    )
    return ToolEntry(
        name=name, spec=spec, descriptor=ToolDescriptor("d", _IN), output_schema=_OUT, concurrency=concurrency,
        conflict_keys=tuple(keys), timeout_s=1.0 if concurrency != "serial" else None,
        cancellable=concurrency == "parallel_one_shot",
    )


REGISTRY = ToolRegistry.build([
    _entry("read.a"),
    _entry("read.b"),
    _entry("read.free", keys=()),
    _entry("read.tool", keys=("tool:{name}",)),
    _entry("once.a", concurrency="parallel_one_shot", side="reversible_local", capability="memory.write"),
    _entry("once.b", concurrency="parallel_one_shot", side="reversible_local", capability="memory.write"),
    _entry("write.s", concurrency="serial", side="reversible_local", capability="memory.write"),
    _entry("post.ask", concurrency="serial", approval="per_action", side="external_effect", capability="chatroom.post"),
])


def item(index: int, tool: str, path: str = "x") -> SchedItem:
    return SchedItem(index, tool, {"path": path})


def shape(waves) -> list[tuple[bool, tuple[int, ...]]]:
    return [(w.parallel, w.indexes) for w in waves]


def test_independent_parallel_safe_share_one_wave():
    waves = plan_waves([item(0, "read.a", "p1"), item(1, "read.b", "p2"), item(2, "read.free")], REGISTRY, max_parallel=8)
    assert shape(waves) == [(True, (0, 1, 2))]


def test_conflicting_keys_split_into_ordered_waves():
    batch = [item(0, "read.a", "same"), item(1, "read.b", "same"), item(2, "read.a", "other")]
    # 0 與 1 同鍵 file:same -> 不同波；2 與當前波（含 1）不相交 -> 併入 1 的波
    assert shape(plan_waves(batch, REGISTRY, max_parallel=8)) == [(True, (0,)), (True, (1, 2))]


def test_serial_item_is_barrier_on_both_sides():
    batch = [item(0, "read.a", "1"), item(1, "write.s"), item(2, "read.b", "2"), item(3, "read.free")]
    assert shape(plan_waves(batch, REGISTRY, max_parallel=8)) == [(True, (0,)), (False, (1,)), (True, (2, 3))]


def test_approval_item_is_serial_barrier():
    batch = [item(0, "read.a", "1"), item(1, "post.ask"), item(2, "read.b", "2")]
    assert shape(plan_waves(batch, REGISTRY, max_parallel=8)) == [(True, (0,)), (False, (1,)), (True, (2,))]


def test_unknown_tool_is_serial():
    assert shape(plan_waves([item(0, "ghost"), item(1, "read.a")], REGISTRY, max_parallel=8)) == [(False, (0,)), (True, (1,))]


def test_max_parallel_caps_wave_size_and_one_means_all_singletons():
    batch = [item(i, "read.free") for i in range(5)]
    assert shape(plan_waves(batch, REGISTRY, max_parallel=2)) == [(True, (0, 1)), (True, (2, 3)), (True, (4,))]
    assert shape(plan_waves(batch, REGISTRY, max_parallel=1)) == [(True, (i,)) for i in range(5)]


def test_tool_name_key_serializes_same_tool_only():
    batch = [item(0, "read.tool"), item(1, "read.tool"), item(2, "read.a")]
    assert shape(plan_waves(batch, REGISTRY, max_parallel=8)) == [(True, (0,)), (True, (1, 2))]


def test_deterministic_and_order_preserving_regardless_of_input_order():
    batch = [item(3, "read.a", "q"), item(0, "read.a", "q"), item(2, "write.s"), item(1, "read.b", "r")]
    first = plan_waves(batch, REGISTRY, max_parallel=4)
    assert shape(first) == shape(plan_waves(list(reversed(batch)), REGISTRY, max_parallel=4))
    flat = [i for w in first for i in w.indexes]
    assert flat == [0, 1, 2, 3]  # 衝突項不會被重排到更前面


def test_missing_placeholder_param_fails_closed_to_serial():
    entry = REGISTRY.entry("read.a")
    assert resource_keys(entry, {"n": 1}) is None and resource_keys(entry, {"path": ["x"]}) is None
    waves = plan_waves([SchedItem(0, "read.a", {"n": 1}), item(1, "read.b", "z")], REGISTRY, max_parallel=8)
    assert shape(waves) == [(False, (0,)), (True, (1,))]


def test_duplicate_index_and_bad_parallel_rejected():
    with pytest.raises(ValueError):
        plan_waves([item(0, "read.a"), item(0, "read.b")], REGISTRY)
    with pytest.raises(ValueError):
        plan_waves([], REGISTRY, max_parallel=0)


def test_defer_tail():
    kept, deferred = defer_tail([1, 2, 3, 4], 2, DEFERRED_BATCH_LIMIT)
    assert kept == [1, 2] and deferred == [(3, DEFERRED_BATCH_LIMIT), (4, DEFERRED_BATCH_LIMIT)]
    assert defer_tail([1], 0, DEFERRED_BUDGET) == ([], [(1, DEFERRED_BUDGET)])


def test_parallel_one_shot_is_eligible_and_mixes_with_parallel_safe():
    waves = plan_waves([item(0, "once.a", "p1"), item(1, "read.a", "p2"), item(2, "once.b", "p3")], REGISTRY, max_parallel=8)
    assert shape(waves) == [(True, (0, 1, 2))]


def test_one_shot_conflicting_keys_split_waves_in_index_order():
    batch = [item(0, "once.a", "same"), item(1, "once.b", "same"), item(2, "read.a", "other")]
    assert shape(plan_waves(batch, REGISTRY, max_parallel=8)) == [(True, (0,)), (True, (1, 2))]


def test_one_shot_with_serial_barrier_and_max_parallel():
    batch = [item(0, "once.a", "1"), item(1, "write.s"), item(2, "once.b", "2")]
    assert shape(plan_waves(batch, REGISTRY, max_parallel=8)) == [(True, (0,)), (False, (1,)), (True, (2,))]
    assert shape(plan_waves([item(0, "once.a", "1"), item(1, "once.b", "2")], REGISTRY, max_parallel=1)) == [
        (True, (0,)), (True, (1,)),
    ]
