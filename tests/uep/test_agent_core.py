"""Part S 切片 0：Agent Core 最小單步迴圈與 Gateway 薄層（SA-1～SA-4、TC-2、TG-2、TG-12、D61）。

以 FakeRuntime＋FakeToolExecutor＋FakeToolSpecResolver 跑端到端（需 PostgreSQL）。
"""
from __future__ import annotations

import dataclasses
import logging
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from tests.uep.acceptance_support import POLICY, SCOPE, declare_tool, make_tm
from uep.adapters.runtime import RuntimeRequest
from uep.agent.core import AgentCore, stable_action_id
from uep.fakes import runtime as rt
from uep.fakes.runtime import FakeRuntime
from uep.fakes.tool import FakeToolExecutor
from uep.ids import is_valid_id
from uep.tasks.models import ActionIntentRow
from uep.tools.gateway import ToolGateway

pytestmark = pytest.mark.usefixtures("session_factory")

TOOL = "lore.recall"
CAPS = {"tools": [{"tool": TOOL, "version": 1, "description": "查設定", "params_schema": {}}]}


class Rig:
    def __init__(self, session_factory, *, caps=CAPS, executor=None):
        self.session_factory = session_factory
        self.executor = executor or FakeToolExecutor()
        self.tm = make_tm(session_factory, self.executor)
        declare_tool(self.tm, TOOL, side_effect="internal_read")
        self.runtime = FakeRuntime()
        self.caps = caps
        task = self.tm.create_task(
            persona_id="uep", goal="查設定", owner_actor_id="owner", scope_ref=SCOPE, policy=POLICY,
        )
        self.run = self.tm.start_run(task.task_id, persona_version=1, policy_version=1)
        self.step = self.tm.begin_step(self.run.run_id, "lookup", worker_epoch=self.run.worker_epoch)
        self.attempts = 0

    def core(self, epoch: int | None = None) -> AgentCore:
        return AgentCore(
            self.runtime, self.tm, ToolGateway(),
            worker_epoch=self.run.worker_epoch if epoch is None else epoch,
        )

    def request(self) -> RuntimeRequest:
        self.attempts += 1
        return RuntimeRequest(
            run_id=self.run.run_id, attempt_id=f"att_{self.attempts}", snapshot_ref="snap_1",
            capability_set=self.caps, cancel_scope={"task_run": self.run.run_id},
            deadline=datetime.now(UTC) + timedelta(minutes=5), mode="agent_loop",
        )

    def run_step(self, script, *, epoch: int | None = None):
        request = self.request()
        self.runtime.script_for(request.attempt_id, script)
        outcome = self.core(epoch).run_step(run_id=self.run.run_id, step_id=self.step.step_id, request=request)
        return request, outcome

    def action_rows(self) -> int:
        with self.session_factory() as s:
            return s.scalar(select(func.count()).select_from(ActionIntentRow))


@pytest.fixture
def rig(session_factory):
    return Rig(session_factory)


def test_end_to_end_internal_read(rig):
    request, out = rig.run_step([
        rt.tool_intent(TOOL, {"q": "魔法"}, await_result=True), rt.text("好"), rt.result("ok"),
    ])
    assert out.end == "completed" and out.result["status"] == "ok"
    (record,) = out.intents
    assert record.status == "ok" and is_valid_id(record.action_id, "act")
    ((action_id, result),) = rig.runtime.tool_results[request.attempt_id]
    assert action_id == record.action_id == stable_action_id(rig.run.run_id, rig.step.step_id, 0)
    assert rig.tm.get_action(action_id).status == "SUCCEEDED"
    assert result == {
        "items": [{"scope": "private", "ref": f"blob_fake_{action_id}", "action_id": action_id}],
        "status": "ok", "degraded": False, "partial": False,
    }
    assert "excluded_summary" not in result
    assert rig.executor.execution_count(action_id) == 1


def test_incomplete_args_do_not_create_action(rig, caplog):
    with caplog.at_level(logging.DEBUG, logger="uep.agent.core"):
        request, out = rig.run_step([
            rt.tool_intent(TOOL, {"q": "半"}, args_complete=False, side_effect_hint="high_impact"),
            rt.result("ok"),
        ])
    assert out.end == "completed"
    assert out.intents[0].status == "incomplete" and out.intents[0].action_id is None
    assert rig.action_rows() == 0 and rig.executor.calls == []
    assert rig.runtime.tool_results[request.attempt_id] == []
    # TG-2：side_effect_hint 只出現在 debug log
    assert any("high_impact" in r.getMessage() and r.levelno == logging.DEBUG for r in caplog.records)


def test_tool_outside_capability_set_is_rejected_without_action(rig):
    request, out = rig.run_step([rt.tool_intent("secret.tool", {"x": 1}, await_result=True), rt.result("ok")])
    (record,) = out.intents
    assert record.status == "rejected" and record.action_id is None and record.reason == "capability_missing"
    assert rig.action_rows() == 0 and rig.executor.calls == []
    ((_, result),) = rig.runtime.tool_results[request.attempt_id]
    assert result == {"items": [], "status": "rejected", "degraded": False, "partial": False}


def test_unregistered_tool_in_capability_set_is_rejected(session_factory):
    rig = Rig(session_factory, caps={"tools": [{"tool": "ghost.tool"}]})
    _request, out = rig.run_step([rt.tool_intent("ghost.tool", {}, await_result=True), rt.result("ok")])
    assert out.intents[0].status == "rejected" and out.intents[0].reason == "tool_unknown"
    assert rig.action_rows() == 0


def test_non_canonical_params_are_rejected(rig):
    _request, out = rig.run_step([rt.tool_intent(TOOL, {"q": float("nan")}, await_result=True), rt.result("ok")])
    assert out.intents[0].status == "rejected" and rig.action_rows() == 0


def test_stale_worker_epoch_cancels_attempt(rig):
    request, out = rig.run_step(
        [rt.tool_intent(TOOL, {"q": "x"}, await_result=True), rt.result("ok")], epoch=rig.run.worker_epoch + 7,
    )
    assert out.end == "stale" and out.result is None
    assert rig.runtime.cancel_calls == [(request.attempt_id, "stale_worker_epoch")]
    assert rig.executor.calls == [] and rig.action_rows() == 0
    assert rig.runtime.tool_results[request.attempt_id] == []


def test_executor_failure_maps_to_failed(rig):
    action_id = stable_action_id(rig.run.run_id, rig.step.step_id, 0)
    rig.executor.execute_behavior(action_id, "fail")
    request, out = rig.run_step([rt.tool_intent(TOOL, {"q": "x"}, await_result=True), rt.result("ok")])
    assert out.intents[0].status == "failed"
    ((_, result),) = rig.runtime.tool_results[request.attempt_id]
    assert result == {"items": [], "status": "failed", "degraded": False, "partial": False}
    assert "FAKE_FAILURE" not in repr(result)


def test_lost_response_maps_to_unknown(rig):
    action_id = stable_action_id(rig.run.run_id, rig.step.step_id, 0)
    rig.executor.execute_behavior(action_id, "lose_after_exec")
    request, out = rig.run_step([rt.tool_intent(TOOL, {"q": "x"}, await_result=True), rt.result("ok")])
    assert out.intents[0].status == "unknown"
    ((_, result),) = rig.runtime.tool_results[request.attempt_id]
    assert result["status"] == "unknown" and result["items"] == []
    assert rig.tm.get_action(action_id).status == "UNKNOWN"


def test_unverified_receipt_is_not_ok(rig):
    action_id = stable_action_id(rig.run.run_id, rig.step.step_id, 0)
    rig.executor.unverified_receipt(action_id)
    _request, out = rig.run_step([rt.tool_intent(TOOL, {"q": "x"}, await_result=True), rt.result("ok")])
    # 來源未驗證的 receipt 維持 UNKNOWN（Manager 規則），結果不是 ok
    assert out.intents[0].status == "unknown"


def test_blocked_action_maps_to_blocked_without_leaking_params(rig):
    from uep.tasks.states import PauseReason

    secret = "秘密參數值"
    rig.tm.pause_task(rig.run.task_id, PauseReason.USER)  # Task 暫停：execute_action 拋 ActionBlocked
    request, out = rig.run_step([rt.tool_intent(TOOL, {"q": secret}, await_result=True), rt.result("ok")])
    assert out.intents[0].status == "blocked"
    ((_, result),) = rig.runtime.tool_results[request.attempt_id]
    assert result == {"items": [], "status": "blocked", "degraded": False, "partial": False}
    assert secret not in repr(result) and rig.executor.calls == []


def test_worker_epoch_never_reaches_runtime(rig):
    request, _out = rig.run_step([rt.tool_intent(TOOL, {"q": "x"}, await_result=True), rt.result("ok")])
    sent = rig.runtime.requests[request.attempt_id]
    assert "worker_epoch" not in {f.name for f in dataclasses.fields(sent)}
    assert "worker_epoch" not in repr(sent)
    assert "worker_epoch" not in {f.name for f in dataclasses.fields(rt.RuntimeEvent)}
    for _aid, result in rig.runtime.tool_results[request.attempt_id]:
        assert "worker_epoch" not in repr(result)
    for event in rig.runtime.stream(request.attempt_id):
        assert "worker_epoch" not in repr(event)


def test_action_id_is_stable_and_rerun_is_idempotent(rig):
    script = [rt.tool_intent(TOOL, {"q": "x"}, await_result=True), rt.result("ok")]
    _req1, out1 = rig.run_step(script)
    req2, out2 = rig.run_step(script)  # 同 Run／Step 新 attempt 重跑
    id1, id2 = out1.intents[0].action_id, out2.intents[0].action_id
    assert id1 == id2 == stable_action_id(rig.run.run_id, rig.step.step_id, 0)
    assert rig.action_rows() == 1 and rig.executor.execution_count(id1) == 1
    assert rig.runtime.tool_results[req2.attempt_id][0][1]["status"] == "ok"
    # 不同序號、不同步驟得不同 id
    assert stable_action_id(rig.run.run_id, rig.step.step_id, 1) != id1
    assert stable_action_id(rig.run.run_id, "step_other", 0) != id1


def test_two_intents_get_sequential_ordinals(rig):
    _request, out = rig.run_step([
        rt.tool_intent(TOOL, {"q": "a"}, await_result=True),
        rt.tool_intent(TOOL, {"q": "b"}, await_result=True),
        rt.result("ok"),
    ])
    ids = [r.action_id for r in out.intents]
    assert ids == [stable_action_id(rig.run.run_id, rig.step.step_id, i) for i in (0, 1)]
    assert [r.status for r in out.intents] == ["ok", "ok"]


def test_runtime_error_event_ends_step(rig):
    _request, out = rig.run_step([rt.error("TIMEOUT", retryable=True)])
    assert out.end == "runtime_error" and out.error["code"] == "TIMEOUT"
