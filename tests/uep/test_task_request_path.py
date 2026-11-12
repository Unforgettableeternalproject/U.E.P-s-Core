"""route=task_request 的 Run 路徑（Chain 契約 PR-R 的工具層一半；離線）：分流、route 標籤、緊預算組、超出預算的失敗回報。

決策層尚未提供的部分（route 表仍是空集合、degraded="action_unavailable"、loop.reeval 消費端）不在這裡，見回報。
"""
from __future__ import annotations

import threading
from collections import Counter, OrderedDict
from types import SimpleNamespace

from tests.uep.loop_support import TAGS, Round
from tests.uep.test_agent_loop import Rig, call, trigger
from tests.uep.test_agent_loop_entry import _stub_launcher, _ticket
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


def test_using_the_only_tool_call_without_asking_for_more_still_finalizes_normally():
    rig = Rig([Round("", (call("read.lone", "1"),)), Round("收束")], budget=TASK_REQUEST_BUDGET)
    out = rig.run(trigger=_task_trigger())
    assert [t for _, t in rig.tasks.executions] == ["read.lone"]
    assert (out.state, out.reason_code, out.partial) == ("completed", "budget_finalized", False)


def test_asking_for_a_second_tool_call_is_task_budget_exceeded_with_the_real_budget():
    kinds: list[str] = []
    delivered: list[str] = []
    rig = Rig(
        [Round("", (call("read.lone", "1"), call("read.lone", "2"))), Round("收束文字")],
        budget=TASK_REQUEST_BUDGET, on_budget=kinds.append, deliver_result=lambda t: delivered.append(t) or True,
    )
    out = rig.run(trigger=_task_trigger())
    assert [t for _, t in rig.tasks.executions] == ["read.lone"]  # 第二個呼叫被預算延後（deferred_budget）
    assert kinds == ["abort_tool_calls"]  # 工具次數保險絲觸發：recorder 的 kind 與 2.0 倍輪數的 "abort" 分開（決策層對應 tool_calls_fuse）
    assert (out.state, out.reason_code, out.partial) == ("failed", "budget_exhausted", True)
    assert delivered == ["收束文字"] and out.delivered


def test_agent_loop_with_the_same_deferred_call_is_not_affected():
    budget = LoopBudget(max_steps=2, max_tool_calls=1, max_parallel=1, max_consults=0)
    kinds: list[str] = []
    rig = Rig([Round("", (call("read.lone", "1"), call("read.lone", "2"))), Round("收束")], budget=budget, on_budget=kinds.append)
    out = rig.run()
    assert kinds == [] and (out.state, out.reason_code, out.partial) == ("completed", "budget_finalized", False)


def test_abort_of_a_task_request_run_counts_a_route_specific_metric():
    from uep.echo.agent_wiring import _RunCtx

    launcher = _stub_launcher(delivery=SimpleNamespace(prepare=lambda i: None, dispatch=lambda i: SimpleNamespace(status="x")))
    task_ctx = _RunCtx(_ticket(), "t", SimpleNamespace(run_id="r", worker_epoch=1), route="task_request")
    launcher._on_budget(task_ctx, "abort")
    launcher._on_budget(_RunCtx(_ticket(), "t", SimpleNamespace(run_id="r2", worker_epoch=1)), "abort")
    m = launcher._orchestrator.metrics
    assert m["budget.abort"] == 2 and m["budget.abort.task_request"] == 1


def test_missing_task_request_policy_row_fails_closed_to_the_tight_budget():
    launcher = _stub_launcher()  # 沒有政策表
    trig = trigger_from_turn(
        TICKET, _proposal(), capability_tags=frozenset({"lore.write"}), policy_ref=DEFAULT_LOOP_POLICY_REF,
        owner_actor_id="o", route="task_request",
    )
    out = launcher._effective_budget(trig)
    assert (out.max_steps, out.max_tool_calls, out.max_parallel) == (2, 1, 1)
    assert launcher._orchestrator.metrics["budget.task_request_policy_missing"] == 1
    # 有政策列時不觸發；agent_loop 不受影響
    ok = _stub_launcher(budget_policies=task_request_budget_table())
    ok._effective_budget(trig)
    assert ok._orchestrator.metrics["budget.task_request_policy_missing"] == 0
    plain = launcher._effective_budget(trigger_from_turn(
        TICKET, _proposal("agent_loop"), capability_tags=frozenset({"memory.read"}), policy_ref="p", owner_actor_id="o",
    ))
    assert plain.max_steps == launcher._budget.max_steps


def test_empty_task_request_tags_do_not_start_a_run_and_count_a_dedicated_metric(caplog):
    import logging

    metrics: Counter[str] = Counter()
    launched: list = []
    only_memory = ToolRegistry.build([_write_registry().entry("mem.get")])
    entry = LoopEntry(
        "agent", capability_tags_for=make_capability_tags_for(only_memory), launch=lambda trig, t: launched.append(trig),
        policy_ref=DEFAULT_LOOP_POLICY_REF, owner_for=lambda t: "o", metrics=metrics,
    )
    with caplog.at_level(logging.WARNING, logger="uep.echo.agent_wiring"):
        assert entry(TICKET, _proposal("task_request")) is None
    assert launched == [] and metrics["turn.task_request_capability_empty"] == 1
    assert [r.levelno for r in caplog.records if "task_request" in r.getMessage()] == [logging.WARNING]  # 不是 error


def test_task_request_tags_equal_the_decision_layers_write_native_tags():
    from uep.decision.capability import WRITE_NATIVE_TAGS

    assert TASK_REQUEST_CAPABILITIES == WRITE_NATIVE_TAGS  # 決策層改表時這裡會提醒同步


def test_same_step_overrun_on_a_user_turn_keeps_todays_finalize_behavior():
    kinds: list[str] = []
    budget = LoopBudget(max_steps=2, max_tool_calls=10, max_parallel=1, max_consults=0, repeat_limit=99)
    rig = Rig(
        [Round("", (call("read.lone", "1"),)), Round("", (call("read.lone", "2"),)), Round("收束文字")],
        budget=budget, on_budget=kinds.append,
    )
    out = rig.run()  # 預設 trigger：user_turn／agent_loop
    assert kinds == [] and not out.partial and (out.state, out.reason_code) == ("completed", "budget_finalized")


def test_step_overrun_abort_keeps_the_plain_abort_kind():
    kinds: list[str] = []
    budget = LoopBudget(max_steps=2, max_tool_calls=10, max_parallel=1, max_consults=0, repeat_limit=99)
    rig = Rig(
        [Round("", (call("read.lone", "1"),)), Round("", (call("read.lone", "2"),)), Round("收束")],
        budget=budget, on_budget=kinds.append,
    )
    out = rig.run(trigger=_task_trigger())
    assert kinds == ["abort"] and out.partial


def test_both_abort_kinds_share_the_notice_and_count_separately():
    from uep.echo.agent_wiring import BUDGET_ABORT_TEXT, _RunCtx

    sent: list = []
    delivery = SimpleNamespace(prepare=lambda i: sent.append(i), dispatch=lambda i: SimpleNamespace(status=__import__("uep.output.delivery", fromlist=["x"]).DeliveryStatus.SENT))
    launcher = _stub_launcher(delivery=delivery)
    ctx = _RunCtx(_ticket(), "t", SimpleNamespace(run_id="r", worker_epoch=1), route="task_request")
    launcher._on_budget(ctx, "abort")
    launcher._on_budget(ctx, "abort_tool_calls")
    m = launcher._orchestrator.metrics
    assert (m["budget.abort"], m["budget.abort_tool_calls"], m["budget.abort.task_request"]) == (2, 1, 2)
    assert [launcher._texts.get(i.content_ref) for i in sent] == [BUDGET_ABORT_TEXT, BUDGET_ABORT_TEXT] and ctx.abort_notice_ok


def test_lite_row_is_not_blocked_by_the_threshold_startup_validation():
    """max_tool_calls < 輪數中斷點的檢查只在三段門檻啟用時做；lite 用預設門檻（未啟用），所以 (2 步、1 次工具) 合法。"""
    from uep.agent.budget import BudgetThresholds

    assert not TASK_REQUEST_BUDGET.thresholds.enabled and TASK_REQUEST_BUDGET.thresholds == BudgetThresholds()
    LoopBudget(max_steps=2, max_tool_calls=1)  # 可建構
    with __import__("pytest").raises(ValueError):
        LoopBudget(max_steps=2, max_tool_calls=1, thresholds=BudgetThresholds(1.0, 1.5, 2.0))  # 啟用門檻才會被擋
