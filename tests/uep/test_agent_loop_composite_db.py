"""composite 對真實 TaskManager／PostgreSQL 的測試（需 UEP_TEST_DATABASE_URL；未設時 skip）。離線對照在 ``test_agent_loop_composite.py``。

涵蓋：子步驟各自成為 ActionIntent（composite 本身沒有 action 列）、``composite_cursor`` 經 JSONB 往返、park 後換 worker 接手
（record_approval -> resume(approvals)）從 cursor 續跑且已成功的唯讀步驟不重播、前步輸出取自 receipt 的 ``result_ref``，
以及 ``save_checkpoint`` 對 cursor 的值層級白名單。
"""
from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

import pytest

from tests.uep.acceptance_support import POLICY, make_tm
from tests.uep.loop_support import Round, _entry
from tests.uep.test_agent_loop_approval_db import (
    OWNER,
    SCOPE,
    approval_row,
    new_loop,
    resume,
    takeover,
)
from tests.uep.test_agent_loop_db import DbRig, _actions
from uep.adapters.tool import CompositeSpec, CompositeStep
from uep.agent.budget import LoopBudget
from uep.agent.core import stable_action_id
from uep.agent.loop import AgentLoop
from uep.fakes.crash import CrashInjector, SimulatedCrash
from uep.tasks.manager import PolicyViolation, params_hash
from uep.tasks.states import ActionStatus, RunStatus
from uep.tools.registry import ToolRegistry

pytestmark = pytest.mark.usefixtures("session_factory")

_OUT_REF = {"type": "object", "properties": {"result_ref": {"type": "string"}}}
_NOTE_IN = {"type": "object", "additionalProperties": False, "required": ["path"], "properties": {"path": {"type": "string"}}}


def composite_registry() -> ToolRegistry:
    read = dataclasses.replace(_entry("read.lone", timeout_s=None), output_schema=_OUT_REF)
    ask = _entry("post.ask", approval="per_action", side="external_effect", capability="chatroom.post")
    publish = dataclasses.replace(
        ask,
        name="post.pub",
        composite=CompositeSpec(steps=(
            CompositeStep("read.lone", {"path": "$.input.path"}),
            CompositeStep("post.ask", {"path": "$.steps[0].output.result_ref"}),
        )),
        output_schema=_OUT_REF,
    )
    return ToolRegistry.build([read, ask, publish])


def composite_rig(session_factory, **kwargs) -> DbRig:
    rig = DbRig(
        session_factory, [Round("發佈", (("post.pub", {"path": "/a.md"}),)), Round("完成")], budget=LoopBudget(repeat_limit=99),
        registry=composite_registry(), **kwargs,
    )
    return rig


def test_db_composite_parks_with_cursor_and_new_worker_continues_without_replay(session_factory):
    rig = composite_rig(session_factory)
    out = rig.go()
    assert out.state == "awaiting_approval"
    rows = {a.tool: a for a in _actions(rig)}
    assert set(rows) == {"read.lone", "post.ask"}  # composite 本身沒有 action 列
    read, ask = rows["read.lone"], rows["post.ask"]
    assert read.status == ActionStatus.SUCCEEDED.value and ask.status == ActionStatus.INTENDED.value
    step_id = read.step_id
    assert read.action_id == stable_action_id(rig.run.run_id, step_id, 0, 0) and ask.action_id == stable_action_id(rig.run.run_id, step_id, 0, 1)
    stored = rig.tm.get_run(rig.run.run_id).checkpoint  # JSONB 往返後
    assert stored["composite_cursor"] == {
        "ordinal": 0, "tool": "post.pub", "next": 1,
        "rows": [{"step": 0, "tool": "read.lone", "status": "ok", "action_id": read.action_id}],
    }
    assert stored["pending_approvals"] == [ask.action_id]

    approval = rig.tm.record_approval(
        principal=OWNER, action_hash=ask.params_hash, target=ask.tool, scope=SCOPE, expiry=datetime.now(UTC) + timedelta(hours=1),
    )
    plan, checkpoint = takeover(rig)
    loop = new_loop(rig, registry=rig.registry, epoch=plan.run.worker_epoch)
    done = resume(rig, loop, checkpoint, {ask.action_id: approval.approval_id})
    assert (done.state, done.text) == ("completed", "完成")
    assert rig.tm.get_run(rig.run.run_id).status == RunStatus.SUCCEEDED.value
    assert approval_row(rig, approval.approval_id).consumed_by_action_id == ask.action_id
    assert rig.executor.execution_count(read.action_id) == 1 and rig.executor.execution_count(ask.action_id) == 1
    assert rig.tm.get_action(ask.action_id).status == ActionStatus.SUCCEEDED.value
    assert rig.tm.get_run(rig.run.run_id).checkpoint.get("composite_cursor") is None


def test_db_receipt_result_ref_is_what_the_next_step_receives(session_factory):
    rig = composite_rig(session_factory)
    rig.go()
    rows = {a.tool: a for a in _actions(rig)}
    receipt = rig.tm.get_receipt(rows["read.lone"].action_id)
    assert receipt is not None and receipt.result_ref
    assert rows["post.ask"].params_hash == params_hash("post.ask", 1, {"path": receipt.result_ref})  # 前步 receipt 的輸出被代入


def test_db_checkpoint_accepts_cursor_but_rejects_free_text(session_factory):
    rig = composite_rig(session_factory)
    run = rig.run
    good = {"composite_cursor": {"ordinal": 0, "tool": "post.pub", "next": 1, "rows": [
        {"step": 0, "tool": "read.lone", "status": "ok", "action_id": "act_ABC"}]}}
    rig.tm.save_checkpoint(run.run_id, good, worker_epoch=run.worker_epoch)
    assert rig.tm.get_run(run.run_id).checkpoint == good
    bad = {"composite_cursor": {"ordinal": 0, "tool": "post.pub", "next": 1, "rows": [
        {"step": 0, "tool": "read.lone", "status": "ok", "note": "這是私人內容 不應該進 checkpoint"}]}}
    with pytest.raises(PolicyViolation):
        rig.tm.save_checkpoint(run.run_id, bad, worker_epoch=run.worker_epoch)
    deep = {"composite_cursor": {"rows": [{"fields": ["/a"]}]}}
    with pytest.raises(PolicyViolation):  # 巢狀清單超過深度上限：cursor 不得存 fields
        rig.tm.save_checkpoint(run.run_id, deep, worker_epoch=run.worker_epoch)


def test_db_composite_failed_read_runs_nothing_else(session_factory):
    rig = composite_rig(session_factory, lose_tool="read.lone")
    out = rig.go()
    assert (out.state, out.reason_code) == ("failed", "unresolved_actions")
    assert [a.tool for a in _actions(rig)] == ["read.lone"]  # UNKNOWN 後第二步沒有建 action


# ---- 崩潰：對真實 Manager 的 resume_run（EXECUTING -> UNKNOWN）----

def chain_registry() -> ToolRegistry:
    read = dataclasses.replace(_entry("read.lone", timeout_s=None), output_schema=_OUT_REF)
    write = dataclasses.replace(_entry("write.s", side="reversible_local", capability="memory.write", timeout_s=None), output_schema=_OUT_REF)
    chain = dataclasses.replace(
        write,
        name="note.chain",
        composite=CompositeSpec(steps=(
            CompositeStep("read.lone", {"path": "$.input.path"}),
            CompositeStep("write.s", {"path": "$.steps[0].output.result_ref"}),
            CompositeStep("write.s", {"path": "$.input.path"}),
        )),
    )
    return ToolRegistry.build([read, write, chain])


def crashing_rig(session_factory, crash: CrashInjector) -> DbRig:
    rig = DbRig(
        session_factory, [Round("串接", (("note.chain", {"path": "/a.md"}),)), Round("完成")], budget=LoopBudget(repeat_limit=99),
        registry=chain_registry(),
    )
    rig.tm = make_tm(session_factory, rig.executor, crash=crash, tool_resolver=rig.registry)
    task = rig.tm.create_task(persona_id="uep", goal="串接", owner_actor_id="owner", scope_ref=SCOPE, policy=POLICY)
    rig.run = rig.tm.start_run(task.task_id, persona_version=1, policy_version=1)
    return rig


def test_db_crash_mid_execute_of_a_step_ends_run_unresolved_and_never_replays_earlier_steps(session_factory):
    crash = CrashInjector()
    rig = crashing_rig(session_factory, crash)
    crash.arm("action.after_executing_commit", on_hit=2)  # 第二個子步驟進入 EXECUTING 後、executor 呼叫前崩潰
    with pytest.raises(SimulatedCrash):
        rig.go()
    read, write = sorted(_actions(rig), key=lambda a: a.created_at)
    assert read.status == ActionStatus.SUCCEEDED.value and write.status == ActionStatus.EXECUTING.value
    assert rig.tm.get_run(rig.run.run_id).checkpoint["composite_cursor"]["next"] == 1  # 逐步 checkpoint 已含第一步

    plan = rig.tm.resume_run(rig.run.run_id, stale_after=timedelta(0))
    assert plan.unknown_action_ids == [write.action_id]
    out = resume(rig, new_loop(rig, epoch=plan.run.worker_epoch, rounds=[Round("不該到這裡")]), plan.checkpoint or {}, {})
    assert (out.state, out.reason_code) == ("failed", "unresolved_actions")
    assert rig.executor.execution_count(read.action_id) == 1 and rig.executor.execution_count(write.action_id) == 0
    assert rig.tm.get_action(write.action_id).status == ActionStatus.UNKNOWN.value
    assert len(_actions(rig)) == 2  # 第三步沒有建 action


def test_db_crash_between_steps_resumes_from_cursor_and_completes(session_factory, monkeypatch):
    crash = CrashInjector()
    rig = crashing_rig(session_factory, crash)
    original = AgentLoop._composite_progress
    seen = {"n": 0}

    def crash_after_second_progress(self, st, m, entry, rows):
        original(self, st, m, entry, rows)  # 第二步的 checkpoint 已寫入
        seen["n"] += 1
        if seen["n"] == 2:
            raise SimulatedCrash("between_steps")

    monkeypatch.setattr(AgentLoop, "_composite_progress", crash_after_second_progress)
    with pytest.raises(SimulatedCrash):
        rig.go()
    first_two = sorted(_actions(rig), key=lambda a: a.created_at)
    assert [a.status for a in first_two] == [ActionStatus.SUCCEEDED.value] * 2
    stored = rig.tm.get_run(rig.run.run_id).checkpoint
    assert stored["composite_cursor"]["next"] == 2 and stored["pending_approvals"] == []

    plan = rig.tm.resume_run(rig.run.run_id, stale_after=timedelta(0))
    assert plan.unknown_action_ids == []
    out = resume(rig, new_loop(rig, epoch=plan.run.worker_epoch, rounds=[Round("完成")]), plan.checkpoint or {}, {})
    assert (out.state, out.text) == ("completed", "完成")
    all_actions = sorted(_actions(rig), key=lambda a: a.created_at)
    assert len(all_actions) == 3 and all(a.status == ActionStatus.SUCCEEDED.value for a in all_actions)
    assert [rig.executor.execution_count(a.action_id) for a in all_actions] == [1, 1, 1]  # 前兩步沒有重播
    assert rig.tm.get_run(rig.run.run_id).status == RunStatus.SUCCEEDED.value
