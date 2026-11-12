"""測試用工具組（UEP_TOOL_TESTKIT）對真實 TaskManager／組裝層／Console 的 DB 測試（需 PostgreSQL；沒有
UEP_TEST_DATABASE_URL 時自動 skip）。

- 真實 TaskManager：``test_approval_echo`` 的 park -> 核准 -> resume（外部效果類別才會觸發核准，FakeTasks 看的是 approval_level，
  這裡對照真實 ``APPROVAL_REQUIRED``）、``test_sleep`` 逾時後 executor.cancel 簽回的失敗 receipt 被正確入庫。
- 組裝層＋Console（假 ModelPort）：開關開啟時 /loop 看得到測試工具並能呼叫、核准項目可由 /approve 記錄；
  開關關閉時投影完全沒有測試工具、legacy 路徑下開啟會被拒絕。
"""
from __future__ import annotations

import json
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from tests.uep.acceptance_support import POLICY, SCOPE
from tests.uep.loop_support import Round, ScriptedRuntime, projector_for
from tests.uep.test_agent_loop import PARENT, trigger
from tests.uep.test_agent_loop_entry_db import _agent_pieces, _Capture
from tests.uep.test_agent_tool_path import FakePort, fin, intent, text
from uep.agent.budget import LoopBudget
from uep.agent.loop import AgentLoop
from uep.assembly import AssemblyError
from uep.events.bus import EventBus
from uep.events.publisher import EventPublisher
from uep.fakes.loop_context import InMemoryLoopContextStore
from uep.tasks.manager import AuthenticatedPrincipal, TaskManager
from uep.tasks.models import ActionIntentRow, ApprovalRow, RunRow, TaskRow
from uep.tasks.states import ActionStatus, RunStatus
from uep.tools.catalog.memory import memory_registry
from uep.tools.catalog.testkit import (
    TEST_APPROVAL_TAG,
    TEST_READ_TAG,
    TESTKIT_EXECUTOR,
    TESTKIT_TOOLS,
    TOOL_APPROVAL_ECHO,
    TOOL_CLOCK,
    TOOL_CLOCK_ECHO,
    TOOL_ECHO,
    TOOL_ECHO_THEN_APPROVE,
    TOOL_SLEEP,
    with_testkit,
)
from uep.tools.catalog.testkit import testkit_entries as kit_entries
from uep.tools.executors.testkit import APPROVAL_NOTE, TestkitToolExecutor
from uep.tools.gateway import ToolGateway
from uep.tools.registry import ToolRegistry

pytestmark = pytest.mark.usefixtures("session_factory")
OWNER = AuthenticatedPrincipal(actor_id="owner", kind="human", verified=True)


class KitDb:
    """真實 TaskManager＋真實 ``TestkitToolExecutor`` 的最小裝置（結果文字走 executor 的回合暫存）。"""

    def __init__(self, session_factory, rounds, *, registry: ToolRegistry, budget: LoopBudget | None = None) -> None:
        self.registry = registry
        self.kit = TestkitToolExecutor()
        self.tm = TaskManager(
            session_factory, EventPublisher(session_factory, EventBus()), {TESTKIT_EXECUTOR: self.kit},
            tool_resolver=registry,
        )
        task = self.tm.create_task(persona_id="uep", goal="測試工具", owner_actor_id="owner", scope_ref=SCOPE, policy=POLICY)
        self.run = self.tm.start_run(task.task_id, persona_version=1, policy_version=1)
        self.runtime = ScriptedRuntime(list(rounds))
        self.store = InMemoryLoopContextStore()
        self.budget = budget or LoopBudget(max_parallel=2, repeat_limit=99)

    def loop(self, *, runtime=None, epoch: int | None = None) -> AgentLoop:
        return AgentLoop(
            runtime=runtime or self.runtime, tasks=self.tm, gateway=ToolGateway(self.registry), registry=self.registry,
            store=self.store, projector=projector_for(self.registry), budget=self.budget,
            worker_epoch=self.run.worker_epoch if epoch is None else epoch, content_lookup=self.kit.cache.text,
            executors={TESTKIT_EXECUTOR: self.kit}, cancel_grace_s=1.0,
        )

    def go(self, tags=frozenset({TEST_READ_TAG, TEST_APPROVAL_TAG})):
        return self.loop().run(trigger(tags=tags), self.run.run_id, parent=PARENT, system_text="你是助理", trigger_text="測試")

    def actions(self) -> list[ActionIntentRow]:
        with self.tm._sf() as s:
            return list(s.scalars(select(ActionIntentRow).where(ActionIntentRow.run_id == self.run.run_id)))


REGISTRY = with_testkit(memory_registry())


# ───────────────────────── 真實 TaskManager ─────────────────────────

def test_approval_echo_requires_real_approval_then_resumes_and_executes_once(session_factory):
    rig = KitDb(session_factory, [Round("要核准", ((TOOL_APPROVAL_ECHO, {"message": "放行"}),)), Round("核准後完成")], registry=REGISTRY)
    out = rig.go()
    assert out.state == "awaiting_approval"
    (action,) = rig.actions()
    assert (action.tool, action.status, action.side_effect_class) == (TOOL_APPROVAL_ECHO, ActionStatus.INTENDED.value, "external_effect")
    assert rig.kit.executions == []  # 核准前沒有執行
    assert rig.tm.get_run(rig.run.run_id).status == RunStatus.WAITING_APPROVAL.value
    approval = rig.tm.record_approval(
        principal=OWNER, action_hash=action.params_hash, target=action.tool, scope=SCOPE,
        expiry=datetime.now(UTC) + timedelta(hours=1),
    )
    plan = rig.tm.resume_run(rig.run.run_id)
    resumed = rig.loop(runtime=ScriptedRuntime([Round("核准後完成")]), epoch=plan.run.worker_epoch).resume(
        rig.run.run_id, plan.checkpoint or {}, trigger=trigger(tags={TEST_READ_TAG, TEST_APPROVAL_TAG}),
        parent_id="ctx_parent", approvals={action.action_id: approval.approval_id},
    )
    assert (resumed.state, resumed.text) == ("completed", "核准後完成")
    assert rig.kit.executions == [action.action_id]
    assert rig.tm.get_action(action.action_id).status == ActionStatus.SUCCEEDED.value
    with rig.tm._sf() as s:
        assert s.get(ApprovalRow, approval.approval_id).consumed_by_action_id == action.action_id
    assert json.loads(rig.kit.cache.text(action.action_id)) == {"echo": "放行", "note": APPROVAL_NOTE}


def test_read_tools_do_not_need_approval_with_the_real_manager(session_factory):
    rig = KitDb(session_factory, [
        Round("", ((TOOL_ECHO, {"text": "a"}), (TOOL_CLOCK, {}))), Round("", ((TOOL_SLEEP, {"seconds": 0.05}),)), Round("好"),
    ], registry=REGISTRY)
    out = rig.go()
    assert (out.state, out.tool_calls) == ("completed", 3)
    assert sorted(a.tool for a in rig.actions()) == sorted([TOOL_ECHO, TOOL_CLOCK, TOOL_SLEEP])
    assert {a.status for a in rig.actions()} == {ActionStatus.SUCCEEDED.value}
    assert {a.concurrency for a in rig.actions() if a.tool == TOOL_SLEEP} == {"parallel_one_shot"}


def test_sleep_timeout_cancels_the_executor_and_the_failed_receipt_is_recorded(session_factory):
    entries = [replace(e, timeout_s=0.3) if e.name == TOOL_SLEEP else e for e in kit_entries()]
    rig = KitDb(
        session_factory, [Round("", ((TOOL_SLEEP, {"seconds": 5}),)), Round("逾時後仍完成")], registry=ToolRegistry.build(entries),
    )
    out = rig.go()
    assert out.state == "completed"
    (action,) = rig.actions()
    assert action.status == ActionStatus.FAILED.value  # executor.cancel -> 簽回失敗 receipt -> FAILED（不是 UNKNOWN）
    assert rig.tm.get_receipt(action.action_id).error_code == "cancelled"
    assert rig.tm.get_run(rig.run.run_id).status == RunStatus.SUCCEEDED.value


# ───────────────────────── 組裝層＋Console ─────────────────────────

def _console(session_factory, tmp_path, port, *, testkit: bool | None, **extra):
    from uep.console.commands import Console
    from uep.console.rig import build_fake_runtime

    think, _es_port, host_kw = _agent_pieces("agent_loop")
    assemble_kwargs = {
        "tool_path": "agent", "model_port": port, "agent_loop_entry": True, "executors": {},
        "tool_resolver": memory_registry(), "decision_log": False, "supervisor": False,
    }
    if testkit is not None:
        assemble_kwargs["tool_testkit"] = testkit
    assemble_kwargs.update(extra)
    rt = build_fake_runtime(
        session_factory, tmp_path / "console", think_factory=lambda tracer: think, host_kwargs=host_kw,
        assemble_kwargs=assemble_kwargs,
    )
    out = _Capture()
    return Console(rt, out=out, wait_s=10.0), rt, out


def _offered(port: FakePort) -> list[str]:
    return [t["tool"] for t in port.requests[0].capability_set["tools"]]


def test_console_loop_with_testkit_offers_and_runs_the_test_tools(session_factory, tmp_path):
    port = FakePort([
        [intent(TOOL_ECHO, {"text": "端到端"}, call_id="c1", index=0), intent(TOOL_CLOCK, {}, call_id="c2", index=1), fin("tool_calls")],
        [text("測試工具都好了。"), fin()],
    ])
    console, rt, out = _console(session_factory, tmp_path, port, testkit=True)
    try:
        console.execute("/say 用測試工具")
        assert [TOOL_ECHO, TOOL_CLOCK, TOOL_SLEEP, TOOL_APPROVAL_ECHO, TOOL_CLOCK_ECHO, TOOL_ECHO_THEN_APPROVE] == [n for n in _offered(port) if n.startswith("test_")]
        assert "deep_recall" in _offered(port)  # 記憶工具仍在
        assert "回應：測試工具都好了。" in out.lines
        assert any(line.startswith("工具呼叫：") and f"{TOOL_ECHO}[SUCCEEDED]" in line and f"{TOOL_CLOCK}[SUCCEEDED]" in line
                   for line in out.lines)
        tool_msgs = [m.content for m in port.requests[1].messages if m.role == "tool"]
        assert json.loads(tool_msgs[0]) == {"echo": "端到端", "length": 3}
    finally:
        rt.core.shutdown()


def test_console_loop_without_the_switch_has_no_test_tools_anywhere(session_factory, tmp_path):
    port = FakePort([[text("沒有測試工具。"), fin()]])
    console, rt, _out = _console(session_factory, tmp_path, port, testkit=None)  # 預設關閉
    try:
        console.execute("/say 隨便")
        offered = _offered(port)
        assert not [n for n in offered if n.startswith("test_")] and "deep_recall" in offered
        assert not set(TESTKIT_TOOLS) & set(rt.core.tasks.tool_resolver.entries())
        assert TESTKIT_EXECUTOR not in rt.core.tasks._executors
    finally:
        rt.core.shutdown()


def _parked_console(session_factory, tmp_path, after_rounds):
    port = FakePort([[intent(TOOL_APPROVAL_ECHO, {"message": "放行"}, call_id="c1"), fin("tool_calls")], *after_rounds])
    console, rt, out = _console(session_factory, tmp_path, port, testkit=True)
    console.execute("/say 請核准")
    with rt.session_factory() as s:
        (action,) = list(s.scalars(select(ActionIntentRow)))
    return console, rt, out, action, port


def _kit(rt) -> TestkitToolExecutor:
    return rt.core.tasks._executors[TESTKIT_EXECUTOR]


def _scope_of(rt, action) -> str:
    with rt.session_factory() as s:
        return s.get(TaskRow, s.get(RunRow, action.run_id).task_id).scope_ref


def test_console_park_then_approve_resumes_and_executes_the_side_effect_once(session_factory, tmp_path):
    console, rt, out, action, _port = _parked_console(session_factory, tmp_path, [[text("核准後完成。"), fin()]])
    try:
        assert (action.tool, action.status) == (TOOL_APPROVAL_ECHO, ActionStatus.INTENDED.value)
        assert any(f"等待核准：{action.action_id}" in line for line in out.lines)
        assert any("WAITING_APPROVAL" in line for line in out.lines)
        assert rt.core.loop_launcher.parked_runs() == (action.run_id,)  # park 後保留可 resume 的狀態
        assert _kit(rt).executions == []
        out.lines.clear()
        console.execute("/approve")  # 不帶參數：列出
        assert any(action.action_id in line for line in out.lines)
        out.lines.clear()
        console.execute("/approve act_missing")
        assert out.lines == ["找不到 action：act_missing"]
        out.lines.clear()
        console.execute(f"/approve {action.action_id}")
        assert out.lines[0].startswith("已記錄核准 appr_")
        assert f"續跑結果：Run {action.run_id}：completed" in out.lines
        # 結果經 Host 入口與 Gate 送出（端點渲染），Console 只印處置、不再重印「回應（續跑）」
        assert "回覆處置：delivered" in out.lines and "回應（續跑）：核准後完成。" not in out.lines
        assert any(
            line.startswith(f"Run {action.run_id}：SUCCEEDED") and f"{TOOL_APPROVAL_ECHO}[SUCCEEDED]" in line
            for line in out.lines
        )
        assert _kit(rt).executions == [action.action_id]  # 副作用只發生一次
        with rt.session_factory() as s:
            (approval,) = list(s.scalars(select(ApprovalRow)))
        assert approval.consumed_by_action_id == action.action_id and approval.target == TOOL_APPROVAL_ECHO
        assert rt.core.loop_launcher.parked_runs() == ()  # 終態：暫存已清
        assert not rt.core.loop_launcher._store._store.list_sessions()  # 持久 store 的 child 條目已清
        out.lines.clear()
        console.execute(f"/approve {action.action_id}")  # 已執行：不再需要核准
        assert "不需要核准" in out.lines[0]
    finally:
        rt.core.shutdown()


def test_console_reject_ends_the_run_without_executing(session_factory, tmp_path):
    console, rt, out, action, _port = _parked_console(session_factory, tmp_path, [[text("好，不執行。"), fin()]])
    try:
        out.lines.clear()
        console.execute(f"/reject {action.action_id}")
        assert f"已否決 {action.action_id}。" in out.lines
        assert f"續跑結果：Run {action.run_id}：completed" in out.lines
        assert _kit(rt).executions == []
        with rt.session_factory() as s:
            assert s.get(ActionIntentRow, action.action_id).status == ActionStatus.FAILED.value
            assert s.get(RunRow, action.run_id).status == RunStatus.SUCCEEDED.value
        assert rt.core.loop_launcher.parked_runs() == ()
    finally:
        rt.core.shutdown()


def test_expired_approval_reparks_and_does_not_execute(session_factory, tmp_path):
    _c, rt, _out, action, _port = _parked_console(session_factory, tmp_path, [])
    try:
        approval = rt.core.tasks.record_approval(
            principal=AuthenticatedPrincipal(actor_id="actor_a", kind="human", verified=True),
            action_hash=action.params_hash, target=action.tool, scope=_scope_of(rt, action),
            expiry=datetime.now(UTC) + timedelta(milliseconds=1),
        )
        time.sleep(0.05)
        result = rt.core.loop_launcher.resume(action.run_id, {action.action_id: approval.approval_id}).result(10)
        assert (result.state, result.reason_code) == ("awaiting_approval", "approval_required")
        assert _kit(rt).executions == [] and rt.core.loop_launcher.parked_runs() == (action.run_id,)
    finally:
        rt.core.shutdown()


def test_resume_is_refused_when_the_tool_version_changed(session_factory, tmp_path):
    console, rt, out, action, _port = _parked_console(session_factory, tmp_path, [])
    try:
        launcher = rt.core.loop_launcher
        bumped = [
            replace(e, spec=replace(e.spec, tool_version=2)) if e.name == TOOL_APPROVAL_ECHO else e
            for e in launcher._registry.entries().values()
        ]
        launcher._registry = ToolRegistry.build(bumped)  # 註冊表升版：凍結的版本與當下投影不同
        out.lines.clear()
        console.execute(f"/approve {action.action_id}")
        assert f"續跑結果：Run {action.run_id}：failed（tool_version_changed）" in out.lines
        assert _kit(rt).executions == []
        with rt.session_factory() as s:
            (approval,) = list(s.scalars(select(ApprovalRow)))
            assert approval.consumed_at is None  # 重驗失敗不消耗核准
            assert s.get(RunRow, action.run_id).status == RunStatus.FAILED.value
        assert launcher.parked_runs() == ()
    finally:
        rt.core.shutdown()


def test_resume_refuses_runs_that_are_not_parked(session_factory, tmp_path):
    from uep.echo.agent_wiring import LoopResumeRefused

    _c, rt, _out, _action, _port = _parked_console(session_factory, tmp_path, [])
    try:
        with pytest.raises(LoopResumeRefused):
            rt.core.loop_launcher.resume("run_nope", {})
    finally:
        rt.core.shutdown()


def test_approve_refuses_actions_that_need_no_approval(session_factory, tmp_path):
    port = FakePort([[intent(TOOL_ECHO, {"text": "x"}, call_id="c1"), fin("tool_calls")], [text("好"), fin()]])
    console, rt, out = _console(session_factory, tmp_path, port, testkit=True)
    try:
        console.execute("/say 回聲")
        with rt.session_factory() as s:
            (action,) = list(s.scalars(select(ActionIntentRow)))
        out.lines.clear()
        console.execute(f"/approve {action.action_id}")
        assert out.lines and "不需要核准" in out.lines[0]
        with rt.session_factory() as s:
            assert list(s.scalars(select(ApprovalRow))) == []
    finally:
        rt.core.shutdown()


def test_testkit_requires_the_agent_tool_path_and_a_tool_registry(session_factory, tmp_path):
    from tests.uep.test_assembly import kwargs as base_kwargs
    from uep.assembly import assemble_core

    bus = EventBus()
    publisher = EventPublisher(session_factory, bus)
    with pytest.raises(AssemblyError, match="UEP_TOOL_TESTKIT"):  # legacy 路徑不曝光測試工具
        assemble_core(session_factory, **{**base_kwargs(session_factory, tmp_path, bus=bus, publisher=publisher), "tool_testkit": True})


# ───────────────────────── composite 測試工具（真實 TaskManager／Console） ─────────────────────────

def _by_tool(rig: KitDb) -> dict[str, ActionIntentRow]:
    return {a.tool: a for a in rig.actions()}


def test_clock_echo_composite_feeds_the_first_receipt_result_ref_into_the_second_step(session_factory):
    from uep.tasks.manager import params_hash

    rig = KitDb(session_factory, [Round("組合", ((TOOL_CLOCK_ECHO, {}),)), Round("完成")], registry=REGISTRY)
    out = rig.go()
    assert (out.state, out.tool_calls) == ("completed", 2)
    rows = _by_tool(rig)
    assert set(rows) == {TOOL_CLOCK, TOOL_ECHO}  # composite 本身沒有 action 列
    assert {a.status for a in rows.values()} == {ActionStatus.SUCCEEDED.value}
    receipt = rig.tm.get_receipt(rows[TOOL_CLOCK].action_id)
    assert receipt is not None and receipt.result_ref
    assert rows[TOOL_ECHO].params_hash == params_hash(TOOL_ECHO, 1, {"text": receipt.result_ref})  # 取自 DB receipt，不是回合暫存
    assert json.loads(rig.kit.cache.text(rows[TOOL_ECHO].action_id))["echo"] == receipt.result_ref


def test_echo_then_approve_composite_parks_then_resumes_without_replaying_the_echo(session_factory):
    rig = KitDb(session_factory, [Round("要核准", ((TOOL_ECHO_THEN_APPROVE, {"text": "放行"}),)), Round("核准後完成")], registry=REGISTRY)
    out = rig.go()
    assert out.state == "awaiting_approval"
    rows = _by_tool(rig)
    assert set(rows) == {TOOL_ECHO, TOOL_APPROVAL_ECHO}
    echo, ask = rows[TOOL_ECHO], rows[TOOL_APPROVAL_ECHO]
    assert (echo.status, ask.status) == (ActionStatus.SUCCEEDED.value, ActionStatus.INTENDED.value)
    assert rig.kit.executions == [echo.action_id] and rig.tm.get_run(rig.run.run_id).status == RunStatus.WAITING_APPROVAL.value
    approval = rig.tm.record_approval(
        principal=OWNER, action_hash=ask.params_hash, target=ask.tool, scope=SCOPE, expiry=datetime.now(UTC) + timedelta(hours=1),
    )
    plan = rig.tm.resume_run(rig.run.run_id)
    resumed = rig.loop(runtime=ScriptedRuntime([Round("核准後完成")]), epoch=plan.run.worker_epoch).resume(
        rig.run.run_id, plan.checkpoint or {}, trigger=trigger(tags={TEST_READ_TAG, TEST_APPROVAL_TAG}),
        parent_id="ctx_parent", approvals={ask.action_id: approval.approval_id},
    )
    assert (resumed.state, resumed.text) == ("completed", "核准後完成")
    assert rig.kit.executions == [echo.action_id, ask.action_id]  # 已成功的 echo 不重播，核准步驟只執行一次
    assert {a.status for a in rig.actions()} == {ActionStatus.SUCCEEDED.value}
    with rig.tm._sf() as s:
        assert s.get(ApprovalRow, approval.approval_id).consumed_by_action_id == ask.action_id


def test_console_composite_park_then_approve_resumes_the_sub_step_once(session_factory, tmp_path):
    port = FakePort([
        [intent(TOOL_ECHO_THEN_APPROVE, {"text": "放行"}, call_id="c1"), fin("tool_calls")],
        [text("composite 核准後完成。"), fin()],
    ])
    console, rt, out = _console(session_factory, tmp_path, port, testkit=True)
    try:
        console.execute("/say 請核准這個 composite")
        with rt.session_factory() as s:
            rows = {a.tool: a for a in s.scalars(select(ActionIntentRow))}
        echo, ask = rows[TOOL_ECHO], rows[TOOL_APPROVAL_ECHO]
        assert (echo.status, ask.status) == (ActionStatus.SUCCEEDED.value, ActionStatus.INTENDED.value)
        assert any(f"等待核准：{ask.action_id}" in line for line in out.lines)
        assert _kit(rt).executions == [echo.action_id]
        out.lines.clear()
        console.execute(f"/approve {ask.action_id}")
        assert out.lines[0].startswith("已記錄核准 appr_")
        assert f"續跑結果：Run {ask.run_id}：completed" in out.lines
        assert "回覆處置：delivered" in out.lines and "回應（續跑）：composite 核准後完成。" not in out.lines
        assert _kit(rt).executions == [echo.action_id, ask.action_id]  # echo 沒有重播
        with rt.session_factory() as s:
            assert s.get(RunRow, ask.run_id).status == RunStatus.SUCCEEDED.value
            (approval,) = list(s.scalars(select(ApprovalRow)))
        assert approval.consumed_by_action_id == ask.action_id
    finally:
        rt.core.shutdown()


def test_console_composite_reject_ends_the_run_without_executing_the_approval_step(session_factory, tmp_path):
    port = FakePort([
        [intent(TOOL_ECHO_THEN_APPROVE, {"text": "放行"}, call_id="c1"), fin("tool_calls")],
        [text("好，不執行。"), fin()],
    ])
    console, rt, out = _console(session_factory, tmp_path, port, testkit=True)
    try:
        console.execute("/say 請核准這個 composite")
        with rt.session_factory() as s:
            rows = {a.tool: a for a in s.scalars(select(ActionIntentRow))}
        ask = rows[TOOL_APPROVAL_ECHO]
        out.lines.clear()
        console.execute(f"/reject {ask.action_id}")
        assert f"已否決 {ask.action_id}。" in out.lines
        assert _kit(rt).executions == [rows[TOOL_ECHO].action_id]  # 核准步驟從未執行
        with rt.session_factory() as s:
            assert s.get(ActionIntentRow, ask.action_id).status == ActionStatus.FAILED.value
    finally:
        rt.core.shutdown()


def test_console_clock_echo_composite_runs_end_to_end(session_factory, tmp_path):
    port = FakePort([[intent(TOOL_CLOCK_ECHO, {}, call_id="c1"), fin("tool_calls")], [text("時間回聲好了。"), fin()]])
    console, rt, out = _console(session_factory, tmp_path, port, testkit=True)
    try:
        console.execute("/say 時間回聲")
        assert "回應：時間回聲好了。" in out.lines
        assert any(line.startswith("工具呼叫：") and f"{TOOL_CLOCK}[SUCCEEDED]" in line and f"{TOOL_ECHO}[SUCCEEDED]" in line
                   for line in out.lines)
    finally:
        rt.core.shutdown()
