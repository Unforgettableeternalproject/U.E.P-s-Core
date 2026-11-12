"""route=task_request 的 Run 路徑（Chain 契約 PR-R 的工具層一半；離線）：分流、route 標籤、緊預算組、超出預算的失敗回報。

決策層尚未提供的部分（route 表仍是空集合、degraded="action_unavailable"、loop.reeval 消費端）不在這裡，見回報。
"""
from __future__ import annotations

import threading
from collections import Counter, OrderedDict
from types import SimpleNamespace

from tests.uep.loop_support import TAGS, Round
from tests.uep.test_agent_loop import Rig, call, trigger
from tests.uep.test_agent_loop_entry import _stub_launcher
from uep.adapters.tool import CAPABILITY_CEILING
from uep.agent.budget import LoopBudget
from uep.echo.agent_wiring import (
    DEFAULT_LOOP_POLICY_REF,
    TASK_REQUEST_BUDGET,
    TASK_REQUEST_CAPABILITIES,
    LoopEntry,
    make_capability_tags_for,
    task_request_budget_table,
    trigger_from_turn,
)
from uep.tools.registry import ToolRegistry
from uep.turn.orchestrator import TurnOrchestrator

TICKET = SimpleNamespace(turn_id="turn_t", audience_scope="scope_private_owner", snapshot_version=3)


def _proposal(route="task_request"):
    return SimpleNamespace(route=route, capability_hint=None)


def _entry_with(tags_registry=None):
    launched: list = []
    entry = LoopEntry(
        "agent", capability_tags_for=make_capability_tags_for(tags_registry or _write_registry()),
        launch=lambda trig, ticket: launched.append(trig), policy_ref=DEFAULT_LOOP_POLICY_REF, owner_for=lambda t: "owner",
    )
    return entry, launched


def _write_registry():
    from tests.uep.test_tool_registry import make_entry

    return ToolRegistry.build([
        make_entry("lore.put", spec={"capability": "lore.write", "side_effect_class": "external_effect", "approval_level": "per_action"}),
        make_entry("chat.post", spec={"capability": "chatroom.post", "side_effect_class": "external_effect", "approval_level": "per_action"}),
        make_entry("mem.put", spec={"capability": "memory.write", "side_effect_class": "reversible_local"}),
        make_entry("mem.get", spec={"capability": "memory.read"}),
    ])


# ---- 分流：orchestrator 與入口 ----

def test_orchestrator_hands_task_request_to_the_loop_entry_and_still_ignores_other_routes():
    orch = TurnOrchestrator.__new__(TurnOrchestrator)
    orch._lock = threading.Lock()
    orch._loop_claims = OrderedDict()
    orch.metrics = Counter()
    seen: list = []
    orch._loop_entry = lambda t, p: seen.append(p.route) or object()
    orch._enter_loop(SimpleNamespace(turn_id="t1"), _proposal("task_request"))
    orch._enter_loop(SimpleNamespace(turn_id="t2"), _proposal("agent_loop"))
    orch._enter_loop(SimpleNamespace(turn_id="t3"), _proposal("direct_chat"))
    orch._enter_loop(SimpleNamespace(turn_id="t4"), _proposal("ignore"))
    assert seen == ["task_request", "agent_loop"] and orch.loop_claimed("t1") and not orch.loop_claimed("t3")


def test_loop_entry_builds_a_task_request_trigger_with_lite_tier():
    entry, launched = _entry_with()
    trig = entry(TICKET, _proposal("task_request"))
    assert launched == [trig]
    assert (trig.kind, trig.route, trig.goal_code) == ("task_request", "task_request", "task_request")
    assert trig.budget is not None and trig.budget.tier == "lite" and trig.policy_ref == DEFAULT_LOOP_POLICY_REF


def test_user_turn_path_is_unchanged():
    entry, launched = _entry_with()
    trig = entry(TICKET, _proposal("agent_loop"))
    assert (trig.kind, trig.route, trig.budget, trig.goal_code) == ("user_turn", "agent_loop", None, "user_turn_request")
    assert entry(TICKET, _proposal("direct_chat")) is None and len(launched) == 1
    plain = trigger_from_turn(
        TICKET, _proposal("agent_loop"), capability_tags=frozenset({"memory.read"}), policy_ref="p", owner_actor_id="o",
    )
    assert plain.kind == "user_turn" and plain.route == "agent_loop"


# ---- route 標籤：只投影 lore.write／chatroom.post ----

def test_task_request_projects_only_the_route_tags():
    tags = make_capability_tags_for(_write_registry())(TICKET, _proposal("task_request"))
    assert tags == TASK_REQUEST_CAPABILITIES == frozenset({"lore.write", "chatroom.post"})
    assert "memory.write" not in tags and "memory.read" not in tags  # E3：不含 memory.write；唯讀標籤也不在 route 表


def test_task_request_tags_are_bounded_by_registry_and_closed_vocabulary():
    only_memory = ToolRegistry.build([_write_registry().entry("mem.get")])
    assert make_capability_tags_for(only_memory)(TICKET, _proposal("task_request")) == frozenset()  # 快照沒有 → 空 → 不啟動
    assert TASK_REQUEST_CAPABILITIES <= CAPABILITY_CEILING.keys()
    entry, launched = _entry_with(only_memory)
    assert entry(TICKET, _proposal("task_request")) is None and launched == []  # 退回 direct_chat


def test_agent_loop_tags_are_not_affected_by_the_task_request_table():
    tags = make_capability_tags_for(_write_registry())(TICKET, _proposal("agent_loop"))
    assert "memory.read" in tags


# ---- 緊預算組 ----

def test_task_request_budget_row_is_registered_and_resolved_by_the_launcher():
    table = task_request_budget_table()
    assert table.lookup(DEFAULT_LOOP_POLICY_REF, "task_request", "lite") is TASK_REQUEST_BUDGET
    assert table.lookup(DEFAULT_LOOP_POLICY_REF, "agent_loop", "standard") is None
    b = TASK_REQUEST_BUDGET
    assert (b.max_steps, b.max_tool_calls, b.max_parallel, b.max_consults) == (2, 1, 1, 0)
    launcher = _stub_launcher(budget_policies=table, step_timeout_s=45.0)
    task = launcher._effective_budget(trigger_from_turn(
        TICKET, _proposal(), capability_tags=frozenset({"lore.write"}), policy_ref=DEFAULT_LOOP_POLICY_REF,
        owner_actor_id="o", route="task_request",
    ))
    assert (task.max_steps, task.max_tool_calls, task.max_parallel, task.max_consults) == (2, 1, 1, 0)
    assert task.step_timeout_s == 45.0  # 政策列沒設的 Step 逾時沿用 launcher 的
    plain = launcher._effective_budget(trigger_from_turn(
        TICKET, _proposal("agent_loop"), capability_tags=frozenset({"memory.read"}), policy_ref=DEFAULT_LOOP_POLICY_REF,
        owner_actor_id="o",
    ))
    assert plain.max_steps == launcher._budget.max_steps and plain.max_tool_calls == launcher._budget.max_tool_calls


def _task_trigger():
    return trigger(tags=TAGS, kind="task_request", route="task_request")


def test_tool_call_limit_takes_one_tool_then_finalizes_without_a_failure():
    rig = Rig(
        [Round("", (call("read.lone", "1"), call("read.lone", "2"))), Round("收束")], budget=TASK_REQUEST_BUDGET,
    )
    out = rig.run(trigger=_task_trigger())
    assert [t for _, t in rig.tasks.executions] == ["read.lone"]  # 同批第二個呼叫被預算延後，只執行一次工具
    assert (out.state, out.reason_code, out.partial) == ("completed", "budget_finalized", False)


def test_task_request_exceeding_the_step_budget_fails_as_task_budget_exceeded():
    kinds: list[str] = []
    delivered: list[str] = []
    budget = LoopBudget(max_steps=2, max_tool_calls=10, max_parallel=1, max_consults=0, repeat_limit=99)
    rig = Rig(
        [Round("", (call("read.lone", "1"),)), Round("", (call("read.lone", "2"),)), Round("收束文字")],
        budget=budget, on_budget=kinds.append, deliver_result=lambda t: delivered.append(t) or True,
    )
    out = rig.run(trigger=_task_trigger())
    assert kinds == ["abort"]  # launcher 的 abort 通知 -> 固定訊息＋決策日誌的 task_budget_exceeded
    assert (out.state, out.reason_code, out.partial) == ("failed", "budget_exhausted", True)
    assert delivered == ["收束文字"] and out.delivered


def test_same_step_overrun_on_a_user_turn_keeps_todays_finalize_behavior():
    kinds: list[str] = []
    budget = LoopBudget(max_steps=2, max_tool_calls=10, max_parallel=1, max_consults=0, repeat_limit=99)
    rig = Rig(
        [Round("", (call("read.lone", "1"),)), Round("", (call("read.lone", "2"),)), Round("收束文字")],
        budget=budget, on_budget=kinds.append,
    )
    out = rig.run()  # 預設 trigger：user_turn／agent_loop
    assert kinds == [] and not out.partial and (out.state, out.reason_code) == ("completed", "budget_finalized")
