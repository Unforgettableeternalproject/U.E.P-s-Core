"""DL-4 PR2：route 表與 ``narrow_capability`` 純函式。

共用 venv 未安裝 hypothesis，屬性測試改用參數化窮舉：在小詞彙上枚舉所有 base／snapshot／hint 組合
（含表外標籤、None），逐一檢查不變量。
"""
from __future__ import annotations

import ast
import itertools
import logging
from collections import Counter
from pathlib import Path

import pytest

from uep.agent.trigger import LoopTrigger
from uep.decision.capability import (
    CONSULT_TAG,
    COUNT_DEGRADED,
    COUNT_HINT_DROPPED,
    COUNT_HINT_EMPTY_MODEL,
    COUNT_HINT_IGNORED_WIDER,
    COUNT_WIDEN_REJECTED,
    HINT_SOURCE_MODEL,
    REASON_CAPABILITY_EMPTY,
    REASON_SNAPSHOT_UNAVAILABLE,
    READ_ONLY_NATIVE_TAGS,
    ROUTE_DEFAULT_CAPABILITIES,
    WRITE_NATIVE_TAGS,
    NarrowResult,
    narrow_capability,
    record_narrow,
    route_base,
)
from uep.decision.contracts import DecisionCapability, DecisionPlan

_UEP = Path(__file__).resolve().parents[2] / "uep"

# 小詞彙：KNOWN 為已知閉集；BOGUS 為表外標籤
KNOWN = frozenset({"memory.read", "memory.write", "desktop.file_read", CONSULT_TAG})
BOGUS = "evil.exec"
# 原生工具標籤（seq 420 裁決）：唯讀類進 direct_chat 與 agent_loop，寫入類只進 agent_loop
_NATIVE_READ = frozenset({"lore.read", "chatroom.read", "web.search", "web.fetch", "image.read"})
_NATIVE_WRITE = frozenset({"lore.write", "chatroom.post"})
UNIVERSE_NATIVE = frozenset({"memory.read", CONSULT_TAG, *_NATIVE_READ, *_NATIVE_WRITE})
UNIVERSE = sorted(KNOWN | {BOGUS})


def _subsets(items) -> list[frozenset[str]]:
    items = sorted(items)
    return [frozenset(c) for r in range(len(items) + 1) for c in itertools.combinations(items, r)]


BASES = _subsets(KNOWN)
SNAPSHOTS: list[frozenset[str] | None] = [None, *_subsets(KNOWN)]
HINTS: list[tuple[str, ...] | None] = [None, *(tuple(sorted(s)) for s in _subsets(UNIVERSE))]
ROUTES = ("direct_chat", "agent_loop", "task_request", "ignore", "unknown_route", None)


def _all_cases():
    yield from itertools.product(ROUTES, BASES, SNAPSHOTS, HINTS)


# ---- route 表 ----

def test_route_table_initial_content_d4_4():
    assert dict(ROUTE_DEFAULT_CAPABILITIES) == {
        "direct_chat": frozenset({"memory.read", *_NATIVE_READ}),
        "agent_loop": frozenset({"memory.read", CONSULT_TAG, *_NATIVE_READ, *_NATIVE_WRITE}),
        "task_request": frozenset(),
        "ignore": frozenset(),
    }
    assert all(isinstance(v, frozenset) for v in ROUTE_DEFAULT_CAPABILITIES.values())
    with pytest.raises(TypeError):
        ROUTE_DEFAULT_CAPABILITIES["agent_loop"] = frozenset({"web.fetch"})  # type: ignore[index]


def test_route_table_covers_all_routes_in_proposer():
    from uep.turn.proposer import ROUTES as PROPOSER_ROUTES

    assert set(ROUTE_DEFAULT_CAPABILITIES) == set(PROPOSER_ROUTES)


def test_route_base_unknown_route_is_none():
    assert route_base("nope") is None and route_base(None) is None and route_base(3) is None  # type: ignore[arg-type]
    assert route_base("direct_chat") == frozenset({"memory.read", *_NATIVE_READ})


def test_direct_chat_result_equals_host_constant():
    """direct_chat 在寬鬆快照下算出 memory.read＋原生唯讀標籤，與 host.py 讀表的常數一致。"""
    r = narrow_capability(route_base("direct_chat"), None, UNIVERSE_NATIVE, UNIVERSE_NATIVE, route="direct_chat")
    assert r.tags == frozenset({"memory.read", *_NATIVE_READ}) and r.reason is None and r.route == "direct_chat"


def test_native_tags_direct_chat_read_only_agent_loop_all():
    """seq 420：direct_chat 不含任何寫入類；agent_loop 含全部 7 個原生標籤。"""
    direct = ROUTE_DEFAULT_CAPABILITIES["direct_chat"]
    loop = ROUTE_DEFAULT_CAPABILITIES["agent_loop"]
    assert not (direct & _NATIVE_WRITE) and _NATIVE_READ <= direct
    assert (_NATIVE_READ | _NATIVE_WRITE) <= loop
    assert READ_ONLY_NATIVE_TAGS == _NATIVE_READ and WRITE_NATIVE_TAGS == _NATIVE_WRITE
    assert all(not (ROUTE_DEFAULT_CAPABILITIES[r] & (_NATIVE_READ | _NATIVE_WRITE)) for r in ("task_request", "ignore"))


def test_native_tags_still_intersected_with_snapshot_and_known():
    """D4-5：未註冊（不在 known）或未 enabled（不在快照）的原生標籤仍被交集掉；hint 不能新增寫入類給 direct_chat。"""
    base = route_base("agent_loop")
    snap = frozenset({"memory.read", "lore.read", CONSULT_TAG})
    r = narrow_capability(base, None, snap, UNIVERSE_NATIVE, route="agent_loop")
    assert r.tags == snap
    r = narrow_capability(base, None, UNIVERSE_NATIVE, frozenset({"memory.read", "lore.read", CONSULT_TAG}), route="agent_loop")
    assert r.tags == frozenset({"memory.read", "lore.read", CONSULT_TAG})
    r = narrow_capability(route_base("direct_chat"), ("lore.write", "chatroom.post", "lore.read"), UNIVERSE_NATIVE,
                          UNIVERSE_NATIVE, route="direct_chat")
    assert r.tags == frozenset({"lore.read"}) and set(r.ignored_wider) == {"lore.write", "chatroom.post"}


# ---- 窮舉不變量 ----

def test_invariants_exhaustive():
    checked = 0
    for route, base, snap, hint in _all_cases():
        r = narrow_capability(base, hint, snap, KNOWN, route=route)
        snap_set = snap if snap is not None else frozenset()
        assert r.tags <= base, (route, base, snap, hint)  # hint 不能新增
        assert r.tags <= snap_set, (route, base, snap, hint)
        assert r.tags <= KNOWN
        assert r.tags <= (set(hint) if hint is not None else base) | ({CONSULT_TAG} if route == "agent_loop" else set())
        if r.tags:  # 只剩 consult 視為空
            assert r.tags - {CONSULT_TAG}
        else:
            assert r.reason in (REASON_CAPABILITY_EMPTY, REASON_SNAPSHOT_UNAVAILABLE)
        # agent_loop 空 -> direct_chat；非空保持 agent_loop；其他 route 原樣
        assert r.route == ("direct_chat" if route == "agent_loop" and not r.tags else route)
        # 表外標籤必被丟棄並逐標籤計
        expected_dropped = tuple(sorted(set(hint or ()) - KNOWN))
        assert r.dropped == expected_dropped
        # 表內但不在 base 的 hint 標籤計 ignored_wider
        assert r.ignored_wider == tuple(sorted((set(hint or ()) & KNOWN) - base))
        checked += 1
    assert checked == len(ROUTES) * len(BASES) * len(SNAPSHOTS) * len(HINTS)


def test_hint_never_adds_tags_vs_no_hint():
    for route, base, snap in itertools.product(ROUTES, BASES, SNAPSHOTS):
        without = narrow_capability(base, None, snap, KNOWN, route=route).tags
        for hint in HINTS:
            with_hint = narrow_capability(base, hint, snap, KNOWN, route=route).tags
            assert with_hint <= without, (route, base, snap, hint)


def test_hint_narrowing_is_monotone_in_hint():
    """hint 越小結果越小：hint_a ⊆ hint_b 則 tags_a ⊆ tags_b（規則 proposer 路徑）。"""
    sets = [h for h in HINTS if h is not None]
    for route, snap in itertools.product(("agent_loop", "direct_chat"), SNAPSHOTS):
        base = frozenset({"memory.read", "desktop.file_read", CONSULT_TAG})
        for a, b in itertools.product(sets, sets):
            if set(a) <= set(b):
                ta = narrow_capability(base, a, snap, KNOWN, route=route).tags
                tb = narrow_capability(base, b, snap, KNOWN, route=route).tags
                assert ta <= tb or not ta, (route, snap, a, b)


# ---- 個別規則 ----

def test_none_hint_applies_table_and_snapshot_only():
    r = narrow_capability(route_base("agent_loop"), None, KNOWN, KNOWN, route="agent_loop")
    assert r.tags == frozenset({"memory.read", CONSULT_TAG}) and not (r.dropped or r.ignored_wider)
    r = narrow_capability(route_base("agent_loop"), None, {"memory.read"}, KNOWN, route="agent_loop")
    assert r.tags == frozenset({"memory.read"}) and r.route == "agent_loop"  # 快照缺 consult：不補回


def test_out_of_table_tag_dropped_and_counted_without_invalidating():
    r = narrow_capability(route_base("agent_loop"), ("memory.read", BOGUS), KNOWN, KNOWN, route="agent_loop")
    assert r.dropped == (BOGUS,)
    assert r.tags == frozenset({"memory.read", CONSULT_TAG})  # 其餘照算，consult 恆保留
    m: Counter = Counter()
    record_narrow(r, m)
    assert m[COUNT_HINT_DROPPED] == 1 and m[COUNT_DEGRADED] == 0


def test_hint_wider_than_base_is_ignored_and_counted():
    r = narrow_capability(route_base("agent_loop"), ("memory.read", "desktop.file_read"), KNOWN, KNOWN, route="agent_loop")
    assert "desktop.file_read" not in r.tags and r.ignored_wider == ("desktop.file_read",)
    m: Counter = Counter()
    record_narrow(r, m)
    assert m[COUNT_HINT_IGNORED_WIDER] == 1


def test_empty_hint_means_empty_set_not_unrestricted():
    r = narrow_capability(route_base("agent_loop"), (), KNOWN, KNOWN, route="agent_loop")
    assert r.tags == frozenset() and r.reason == REASON_CAPABILITY_EMPTY
    assert r.route == "direct_chat" and r.degraded


def test_empty_intersection_degrades_agent_loop():
    r = narrow_capability(route_base("agent_loop"), ("desktop.file_read",), KNOWN, KNOWN, route="agent_loop")
    assert r.tags == frozenset() and r.route == "direct_chat" and r.reason == REASON_CAPABILITY_EMPTY and r.degraded


def test_consult_always_kept_for_agent_loop_but_consult_alone_is_empty():
    base = frozenset({"memory.read", "desktop.file_read", CONSULT_TAG})
    r = narrow_capability(base, ("memory.read",), KNOWN, KNOWN, route="agent_loop")
    assert r.tags == frozenset({"memory.read", CONSULT_TAG})  # hint 沒列 consult 也保留
    only = narrow_capability(base, (CONSULT_TAG,), KNOWN, KNOWN, route="agent_loop")
    assert only.tags == frozenset() and only.reason == REASON_CAPABILITY_EMPTY and only.route == "direct_chat"
    only2 = narrow_capability(frozenset({CONSULT_TAG}), None, KNOWN, KNOWN, route="agent_loop")
    assert only2.tags == frozenset()  # 表本身只剩 consult 也視為空
    # 非 agent_loop 不自動保留 consult
    r = narrow_capability(base, ("memory.read",), KNOWN, KNOWN, route="task_request")
    assert CONSULT_TAG not in r.tags


def test_consult_not_added_when_not_in_base_or_snapshot():
    r = narrow_capability(frozenset({"memory.read"}), ("memory.read",), KNOWN, KNOWN, route="agent_loop")
    assert CONSULT_TAG not in r.tags
    r = narrow_capability(route_base("agent_loop"), None, {"memory.read"}, KNOWN, route="agent_loop")
    assert CONSULT_TAG not in r.tags


@pytest.mark.parametrize("route", ["unknown_route", None, "ignore", "task_request"])
def test_unknown_or_empty_route_is_empty(route):
    r = narrow_capability(route_base(route), ("memory.read",), KNOWN, KNOWN, route=route)
    assert r.tags == frozenset() and r.reason == REASON_CAPABILITY_EMPTY and r.route == route


def test_unknown_route_and_goal_code_count_as_degraded_but_ignore_does_not():
    assert narrow_capability(route_base("zzz"), None, KNOWN, KNOWN, route="zzz").degraded
    assert narrow_capability(None, None, KNOWN, KNOWN, route=None).degraded  # capability_lookup(goal_code) 查不到
    assert not narrow_capability(route_base("ignore"), None, KNOWN, KNOWN, route="ignore").degraded


def test_snapshot_unavailable_or_malformed_fails_closed():
    base = route_base("agent_loop")
    for bad in (None, "memory.read", ["memory.read"], {1, 2}, 5):
        r = narrow_capability(base, None, bad, KNOWN, route="agent_loop")  # type: ignore[arg-type]
        assert r.tags == frozenset() and r.reason == REASON_SNAPSHOT_UNAVAILABLE and r.degraded, bad
        assert r.route == "direct_chat"
    m: Counter = Counter()
    record_narrow(narrow_capability(base, None, None, KNOWN, route="agent_loop"), m)
    assert m[COUNT_DEGRADED] == 1 and m[f"{COUNT_DEGRADED}.{REASON_SNAPSHOT_UNAVAILABLE}"] == 1


@pytest.mark.parametrize("bad_hint", ["memory.read", ("memory.read", 1), 5, b"x"])
def test_malformed_hint_fails_closed_even_for_model(bad_hint):
    for source in ("rule", HINT_SOURCE_MODEL):
        r = narrow_capability(route_base("agent_loop"), bad_hint, KNOWN, KNOWN, route="agent_loop", hint_source=source)
        assert r.tags == frozenset() and r.route == "direct_chat"


def test_known_filters_base_and_snapshot():
    r = narrow_capability({"memory.read", BOGUS}, None, {"memory.read", BOGUS}, KNOWN, route="direct_chat")
    assert r.tags == frozenset({"memory.read"})


# ---- D4-6：模型 hint 不得為空集合 ----

def test_model_empty_hint_is_dropped_and_counted_not_degraded():
    base = route_base("agent_loop")
    r = narrow_capability(base, (), KNOWN, KNOWN, route="agent_loop", hint_source=HINT_SOURCE_MODEL)
    assert r.hint_empty_model and r.tags == frozenset({"memory.read", CONSULT_TAG}) and r.route == "agent_loop"
    assert not r.degraded
    m: Counter = Counter()
    record_narrow(r, m)
    assert m[COUNT_HINT_EMPTY_MODEL] == 1 and m[COUNT_DEGRADED] == 0
    # 規則 proposer 的空 hint 仍是明確要求空集合
    assert narrow_capability(base, (), KNOWN, KNOWN, route="agent_loop").tags == frozenset()
    # 模型的非空 hint 與規則 proposer 相同處理
    a = narrow_capability(base, ("memory.read",), KNOWN, KNOWN, route="agent_loop", hint_source=HINT_SOURCE_MODEL)
    b = narrow_capability(base, ("memory.read",), KNOWN, KNOWN, route="agent_loop")
    assert a == b


# ---- 單調性：refine 不放寬 ----

def test_refine_never_widens_exhaustive():
    base = frozenset({"memory.read", "desktop.file_read", CONSULT_TAG})
    firsts = [narrow_capability(base, h, KNOWN, KNOWN, route="agent_loop").tags for h in HINTS]
    for prev, snap, hint in itertools.product(set(firsts), SNAPSHOTS, HINTS):
        r = narrow_capability(base, hint, snap, KNOWN, route="agent_loop", previous_tags=prev)
        assert r.tags <= prev, (prev, snap, hint)
        assert r.tags <= (snap or frozenset()) and r.tags <= base  # 限制後仍不超過當前快照與表


def test_refine_widen_is_rejected_and_counted():
    base = route_base("agent_loop") | {"desktop.file_read"}
    prev = narrow_capability(base, ("memory.read",), KNOWN, KNOWN, route="agent_loop").tags
    assert prev == frozenset({"memory.read", CONSULT_TAG})
    r = narrow_capability(base, None, KNOWN, KNOWN, route="agent_loop", previous_tags=prev)  # 後版移除 hint：想放寬
    assert r.tags == prev and r.widen_rejected
    m: Counter = Counter()
    record_narrow(r, m)
    assert m[COUNT_WIDEN_REJECTED] == 1
    ok = narrow_capability(base, ("memory.read",), KNOWN, KNOWN, route="agent_loop", previous_tags=prev)
    assert not ok.widen_rejected and ok.tags == prev


# ---- D4-5：plan、hint、LoopTrigger 都帶不出比表更大的集合 ----

def test_plan_hint_and_trigger_cannot_exceed_table_exhaustive():
    """從 plan 取 hint -> 純函式 -> LoopTrigger：任何 hint 組合，trigger 標籤都 ⊆ 表 ∩ 快照。"""
    for route, hint, snap in itertools.product(("direct_chat", "agent_loop"), HINTS, [KNOWN, frozenset({"memory.read"})]):
        plan = DecisionPlan(route=route, capability=DecisionCapability(capability_hint=hint))
        r = narrow_capability(route_base(route), plan.capability.capability_hint, snap, KNOWN, route=route)
        table = ROUTE_DEFAULT_CAPABILITIES[route]
        assert r.tags <= table and r.tags <= snap
        if r.tags:
            trigger = LoopTrigger(
                kind="user_turn", origin_ref="o", goal_code="g", capability_tags=r.tags, scope_ref="s",
                owner_actor_id="a", policy_ref="p", snapshot_ref="n",
            )
            assert trigger.capability_tags <= table


def test_agent_loop_default_is_minimal_set_d4_4():
    r = narrow_capability(route_base("agent_loop"), None, KNOWN, KNOWN, route="agent_loop")
    assert r.tags == frozenset({"memory.read", CONSULT_TAG})
    for forbidden in ("memory.write", "desktop.file_read"):
        assert forbidden not in r.tags


def test_narrow_signature_has_no_grant_path():
    """純函式的授權輸入只有 base、snapshot、known；hint 只經交集使用（不存在 grant／expand 類參數）。"""
    import inspect

    params = set(inspect.signature(narrow_capability).parameters)
    assert params == {"base", "hint", "snapshot_tags", "known", "route", "hint_source", "previous_tags"}


def test_result_is_immutable_and_pure():
    base, snap = set(route_base("agent_loop")), set(KNOWN)
    r1 = narrow_capability(base, ("memory.read",), snap, KNOWN, route="agent_loop")
    r2 = narrow_capability(base, ("memory.read",), snap, KNOWN, route="agent_loop")
    assert r1 == r2 and base == set(route_base("agent_loop")) and snap == set(KNOWN)
    with pytest.raises(AttributeError):
        r1.tags = frozenset()  # type: ignore[misc]
    assert isinstance(r1, NarrowResult)


def test_record_narrow_logs_reason_code_without_hint_content(caplog):
    r = narrow_capability(route_base("agent_loop"), (BOGUS,), KNOWN, KNOWN, route="agent_loop")
    with caplog.at_level(logging.INFO, logger="uep.decision.capability"):
        record_narrow(r, Counter(), ref="req1")
    assert any("reason_code=capability_empty" in rec.getMessage() for rec in caplog.records)
    assert not any(BOGUS in rec.getMessage() for rec in caplog.records)


# ---- 邊界與無消費者鎖 ----

def _imports(path: Path) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module)
        elif isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
    return out


def test_capability_module_is_pure_decision_layer():
    """決策層不 import 工具層：capability.py 只依賴標準庫（詞彙 known 由呼叫端傳入）。"""
    mods = _imports(_UEP / "decision" / "capability.py")
    assert not {m for m in mods if m.startswith("uep")}, sorted(mods)
    # contracts 仍是葉節點
    assert not {m for m in _imports(_UEP / "decision" / "contracts.py") if m.startswith("uep")}


def test_decision_layer_does_not_import_tools_layer():
    for path in sorted((_UEP / "decision").glob("*.py")):
        bad = {m for m in _imports(path) if m.startswith(("uep.tools", "uep.adapters"))}
        assert not bad, f"{path.name} 不得 import 工具層：{sorted(bad)}"


# 收窄相關符號：PR4 起只有 agent 工具路徑的 wiring（echo/agent_wiring.py）可以引用（capability.py 本體除外）
_NARROW_SYMBOLS = {"narrow_capability", "route_base", "record_narrow", "NarrowResult"}
# route 表：PR3 起只有組裝驗證與 Host 可以引用
_ROUTE_TABLE_SYMBOLS = {"ROUTE_DEFAULT_CAPABILITIES"}
_ROUTE_TABLE_CONSUMERS = {"assembly.py", "echo/host.py"}


def _symbol_hits(skip: set[str], symbols: set[str]) -> list[str]:
    problems: list[str] = []
    for path in sorted(_UEP.rglob("*.py")):
        rel = path.relative_to(_UEP).as_posix()
        if rel in skip:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            name = node.id if isinstance(node, ast.Name) else node.attr if isinstance(node, ast.Attribute) else None
            if name in symbols or (
                isinstance(node, ast.ImportFrom) and node.module == "uep.decision.capability"
                and {a.name for a in node.names} & symbols
            ):
                problems.append(f"{rel}:{getattr(node, 'lineno', 0)}")
    return problems


_NARROW_CONSUMERS = {"echo/agent_wiring.py"}


def test_narrow_production_consumer_is_only_agent_wiring():
    """PR4：narrow_capability 等收窄符號的 production 消費者只有 ``echo/agent_wiring.py``（capability_tags_for）；
    route 表的消費不算在內（見下一個測試）。"""
    assert _symbol_hits({"decision/capability.py", *_NARROW_CONSUMERS}, _NARROW_SYMBOLS) == []
    every = {p.relative_to(_UEP).as_posix() for p in _UEP.rglob("*.py")}
    for rel in _NARROW_CONSUMERS:
        assert _symbol_hits(every - {rel}, _NARROW_SYMBOLS), f"{rel} 應引用收窄符號"


def test_route_table_consumers_are_only_assembly_and_host():
    """PR3：route 表只有組裝驗證（assembly.py）與 Host（echo/host.py）可以引用，其他地方維持禁止；兩個合法消費者確實存在。"""
    assert _symbol_hits({"decision/capability.py", *_ROUTE_TABLE_CONSUMERS}, _ROUTE_TABLE_SYMBOLS) == []
    every = {p.relative_to(_UEP).as_posix() for p in _UEP.rglob("*.py")}
    for rel in _ROUTE_TABLE_CONSUMERS:
        assert _symbol_hits(every - {rel}, _ROUTE_TABLE_SYMBOLS), f"{rel} 應引用 route 表"


_CAPABILITY_NAMES = {"capability_hint", "DecisionCapability", "capability"}


def test_proactive_does_not_consume_capability_hint_d4_2():
    """D4-2：Proactive 不吃 hint；proactive.py 不引用 hint／capability 區段／narrow 符號（AST 無消費者鎖）。"""
    tree = ast.parse((_UEP / "decision" / "proactive.py").read_text(encoding="utf-8"))
    hits: list[str] = []
    for node in ast.walk(tree):
        name = node.id if isinstance(node, ast.Name) else node.attr if isinstance(node, ast.Attribute) else None
        if name in (_CAPABILITY_NAMES - {"capability"}) | _NARROW_SYMBOLS:
            hits.append(f"{node.lineno}:{name}")
        if isinstance(node, ast.Attribute) and node.attr == "capability":
            hits.append(f"{node.lineno}:.capability")
        if isinstance(node, ast.keyword) and node.arg in ("capability", "capability_hint"):
            hits.append(f"{node.value.lineno}:kw {node.arg}")
        if isinstance(node, ast.ImportFrom) and node.module == "uep.decision.capability":
            hits.append(f"{node.lineno}:import capability")
    assert hits == [], hits
    from uep.decision.proactive import proactive_initiate_spec

    assert "capability" not in proactive_initiate_spec().plan_sections
