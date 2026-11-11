"""原生工具（native）免 Core 內部核准：對真實 TaskManager／PostgreSQL 的執行期測試（需 UEP_TEST_DATABASE_URL；未設時 skip）。

native 的 external_effect action 不要求 approval，但仍完整走四階段（act_、receipt、action.* 事件）；
非 native 的 external_effect 仍須核准（回歸）。
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from tests.uep.loop_support import _IN, _OUT, Round, make_registry
from tests.uep.test_agent_loop import call
from tests.uep.test_agent_loop_db import DbRig, _actions
from uep.adapters.tool import ToolDescriptor, ToolEntry, ToolSpec
from uep.agent.budget import LoopBudget
from uep.events.models import EventRow
from uep.fakes.tool import FakeToolExecutor
from uep.tasks.models import ApprovalRow, ReceiptRow
from uep.tasks.states import ActionStatus, RunStatus

pytestmark = pytest.mark.usefixtures("session_factory")


def _native_entry() -> ToolEntry:
    spec = ToolSpec(
        executor="fake", side_effect_class="external_effect", idempotency="via_action_id", presence_bound=False,
        tool_version=1, capability="chatroom.post", native=True,
    )
    return ToolEntry(name="native.post", spec=spec, descriptor=ToolDescriptor("native 說明", _IN), output_schema=_OUT)


def _rig(session_factory, rounds, **kw) -> DbRig:
    return DbRig(
        session_factory, rounds, registry=make_registry(_native_entry()), executor=FakeToolExecutor(),
        budget=LoopBudget(repeat_limit=99), **kw,
    )


def test_native_action_executes_without_approval_and_leaves_receipt(session_factory):
    rig = _rig(session_factory, [Round("", (call("native.post", "1"),)), Round("完成")])
    out = rig.go()
    assert out.state == "completed"
    (action,) = [a for a in _actions(rig) if a.tool == "native.post"]
    assert action.action_id.startswith("act_")
    assert (action.status, action.side_effect_class) == (ActionStatus.SUCCEEDED.value, "external_effect")  # 分類照實
    assert action.approval_ref is None
    assert rig.executor.executions[action.action_id] == 1
    with rig.tm._sf() as s:
        assert s.scalar(select(ReceiptRow).where(ReceiptRow.action_id == action.action_id)) is not None
        assert list(s.scalars(select(ApprovalRow))) == []  # 沒有任何核准被建立或消耗
        types = {e.event_type for e in s.scalars(select(EventRow))}
    assert {"action.approved", "action.executed"} <= types  # 仍完整走四階段並發 action.* 事件
    assert rig.tm.get_run(rig.run.run_id).status != RunStatus.WAITING_APPROVAL.value


def test_non_native_external_effect_still_requires_approval(session_factory):
    rig = _rig(session_factory, [Round("", (call("post.ask", "1"),)), Round("完成")])
    out = rig.go()
    assert out.state == "awaiting_approval"
    (action,) = [a for a in _actions(rig) if a.tool == "post.ask"]
    assert action.status == ActionStatus.INTENDED.value
    assert rig.executor.executions.get(action.action_id, 0) == 0
    assert rig.tm.get_run(rig.run.run_id).status == RunStatus.WAITING_APPROVAL.value

