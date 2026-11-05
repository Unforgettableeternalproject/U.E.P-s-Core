"""``decision.consult`` 對真實 TaskManager 的 DB 測試（切片 D2；需 PostgreSQL，沒有 UEP_TEST_DATABASE_URL 時自動 skip）。

這是 ``test_agent_loop_consult`` 離線測試（FakeTasks）的真實對照：
- consult 走完整四階段（internal_read、免核准），receipt 有 act_ 與 result_ref；同 action 重放不重做（executor 只進一次）。
- escalate ⇒ 迴圈把 Run 轉 FAILED(escalated)，不是卡在 RUNNING；候選事件送出一次。
- 中樞失敗 ⇒ ok 結果＋advice=∅，Run 照常 SUCCEEDED（consult 失敗不阻擋完成）。
"""
from __future__ import annotations

import pytest

from tests.uep.acceptance_support import POLICY, SCOPE
from tests.uep.loop_support import Round, projector_for
from tests.uep.test_agent_loop import PARENT, call, trigger
from tests.uep.test_agent_loop_consult import CTAGS, consult
from uep.agent.budget import LoopBudget
from uep.agent.core import stable_action_id
from uep.agent.loop import AgentLoop
from uep.decision.hub import DecisionHub
from uep.decision.loop_consult import (
    MAX_CONSULTS,
    consult_decision_id,
    register_loop_consult,
)
from uep.events.bus import EventBus
from uep.events.publisher import EventPublisher
from uep.fakes.loop_context import InMemoryLoopContextStore
from uep.fakes.tool import FakeToolExecutor
from uep.tasks.manager import TaskManager
from uep.tasks.states import ActionStatus, RunStatus
from uep.tools.catalog.decision import CONSULT_TOOL, DECISION_EXECUTOR, consult_entry
from uep.tools.executors.decision import ConsultToolExecutor
from uep.tools.gateway import ToolGateway
from uep.tools.registry import ToolRegistry

pytestmark = pytest.mark.usefixtures("session_factory")


class _Spy:
    def __init__(self, inner):
        self.inner, self.count = inner, 0
        self.params: list[dict] = []  # 建立 action 時的完整參數（含 Gateway 注入）：重放要用同一份，否則 ParamsHashMismatch

    @property
    def lookup_authoritative(self):
        return False

    def lookup_receipt(self, action_id):
        return self.inner.lookup_receipt(action_id)

    def execute(self, call_):
        self.count += 1
        self.params.append(dict(call_.params))
        return self.inner.execute(call_)


def _rig(session_factory, rounds, *, scope=None):
    from tests.uep.loop_support import ScriptedRuntime, make_registry

    registry: ToolRegistry = make_registry(consult_entry())
    hub = DecisionHub()
    register_loop_consult(hub)
    executor = ConsultToolExecutor(hub)
    spy = _Spy(executor)
    tm = TaskManager(
        session_factory, EventPublisher(session_factory, EventBus()),
        {"fake": FakeToolExecutor(), DECISION_EXECUTOR: spy}, tool_resolver=registry,
    )
    task = tm.create_task(persona_id="uep", goal="查資料", owner_actor_id="owner", scope_ref=SCOPE, policy=POLICY)
    run = tm.start_run(task.task_id, persona_version=1, policy_version=1)
    escalations: list = []
    loop = AgentLoop(
        runtime=ScriptedRuntime(list(rounds)), tasks=tm, gateway=ToolGateway(registry), registry=registry,
        store=InMemoryLoopContextStore(), projector=projector_for(registry), budget=LoopBudget(max_consults=MAX_CONSULTS),
        worker_epoch=run.worker_epoch, consults=executor.results,
        consult_scope=scope or (lambda: {"modalities": ["text"], "platforms": ["desktop"]}), on_escalate=escalations.append,
    )
    return tm, run, loop, spy, escalations


def test_db_consult_full_four_phase_and_replay_does_not_rerun_executor(session_factory):
    tm, run, loop, spy, _ = _rig(session_factory, [Round("", (consult(goal_satisfied=True),)), Round("完成")])
    out = loop.run(trigger(tags=CTAGS), run.run_id, parent=PARENT, system_text="s")
    assert out.state == "completed" and tm.get_run(run.run_id).status == RunStatus.SUCCEEDED.value
    steps = tm.get_run(run.run_id).checkpoint
    assert steps["budget_counters"]["consults"] == 1
    from sqlalchemy import select

    from uep.tasks.models import ActionIntentRow

    with session_factory() as s:
        (action,) = list(s.scalars(select(ActionIntentRow).where(ActionIntentRow.tool == CONSULT_TOOL)))
    assert action.status == ActionStatus.SUCCEEDED.value and action.side_effect_class == "internal_read"
    assert action.action_id == stable_action_id(run.run_id, action.step_id, 0)
    again = tm.execute_action(action.action_id, spy.params[0], worker_epoch=run.worker_epoch)  # 完整 args，不是空 dict
    assert again.executed is False and spy.count == 1
    # CN-9：decision_id 進 receipt meta 與 checkpoint（JSONB 往返後）
    decision_id = consult_decision_id(run.run_id, action.step_id, 0, "loop_end")
    assert tm.get_receipt(action.action_id).meta["decision_id"] == decision_id
    assert steps["last_decision_id"] == decision_id


def test_db_escalate_fails_the_run_with_escalated_and_emits_candidate_once(session_factory):
    tm, run, loop, _, escalations = _rig(
        session_factory,
        [Round("", (consult("output_modality", proposed_modalities=["audio_stream"]), call("read.lone", "x"))), Round("不該到這裡")],
        scope=lambda: {"modalities": ["text"], "platforms": ["desktop"]},
    )
    out = loop.run(trigger(tags=CTAGS), run.run_id, parent=PARENT, system_text="s")
    assert (out.state, out.reason_code) == ("failed", "escalated")
    assert tm.get_run(run.run_id).status == RunStatus.FAILED.value and len(escalations) == 1
    cp = tm.get_run(run.run_id).checkpoint  # 終態 checkpoint 帶 decision_id 與缺口碼（耐久紀錄）
    assert cp["next_step_semantic"] == "end:escalated" and cp["escalate_gap"] == "scope_exceeded"
    assert cp["last_decision_id"] == escalations[0].decision_id
