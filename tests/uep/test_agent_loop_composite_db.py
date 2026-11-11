"""composite 對真實 TaskManager／PostgreSQL 的測試（需 UEP_TEST_DATABASE_URL；未設時 skip）。離線對照在 ``test_agent_loop_composite.py``。

涵蓋：子步驟各自成為 ActionIntent（composite 本身沒有 action 列）、``composite_cursor`` 經 JSONB 往返、park 後換 worker 接手
（record_approval -> resume(approvals)）從 cursor 續跑且已成功的唯讀步驟不重播、前步輸出取自 receipt 的 ``result_ref``，
以及 ``save_checkpoint`` 對 cursor 的值層級白名單。
"""
from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

import pytest

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
