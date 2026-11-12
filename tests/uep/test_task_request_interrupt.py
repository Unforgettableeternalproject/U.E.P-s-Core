"""route -> 中斷政策（Agent 提案 4.5）：task_request 被插話時暫停語音、Run 繼續；agent_loop 維持現狀（離線）。

語音輸出本身的停止走既有的 turn 取消（ES／端點）；loop 認領的回合不經 ES 生成，Run 完成前沒有語音可停，
這裡鎖住的是工具層的部分：Run 不被取消，結果在 turn 結束後經 Host 入口延後通知。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests.uep.test_agent_loop_entry import _stub_launcher, _ticket
from uep.echo.agent_wiring import (
    HARD_CANCEL_REASONS,
    ROUTE_INTERRUPT_POLICY,
    _RunCtx,
)
from uep.echo.resume_return import ResumeDisposition, ResumedRunResult, ResumeOutcome


def _launcher(*, ended=False, reason=None, on_resumed=None):
    launcher = _stub_launcher(on_resumed=on_resumed)
    state = {"ended": ended, "reason": reason}
    launcher._orchestrator = SimpleNamespace(
        metrics=launcher._orchestrator.metrics, is_ended=lambda t: state["ended"], cancel_reason_of=lambda t: state["reason"],
        on_turn_ended=lambda *a: None,
    )
    return launcher, state


def _ctx(route):
    return _RunCtx(_ticket(), "task_1", SimpleNamespace(run_id="run_1", worker_epoch=2), route=route, activity_id="actv_1")


def test_policy_table_maps_routes():
    assert ROUTE_INTERRUPT_POLICY == {"agent_loop": "cancel", "task_request": "continue"}


@pytest.mark.parametrize("reason", [None, "barge_in", "superseded"])
def test_agent_loop_is_cancelled_when_the_turn_ends_or_is_cancelled(reason):
    launcher, state = _launcher(ended=True, reason=reason)
    assert launcher._cancelled("turn_u", "agent_loop") is True  # 現狀：turn 結束／被取消 = Run 取消
    state["ended"] = False
    assert launcher._cancelled("turn_u", "agent_loop") is (reason is not None)


@pytest.mark.parametrize("reason", [None, "barge_in", "superseded", "yielded"])
def test_task_request_keeps_running_when_the_turn_is_interrupted(reason):
    launcher, _ = _launcher(ended=True, reason=reason)
    assert launcher._cancelled("turn_u", "task_request") is False


@pytest.mark.parametrize("reason", sorted(HARD_CANCEL_REASONS))
def test_task_request_is_still_cancelled_by_hard_reasons(reason):
    launcher, _ = _launcher(ended=True, reason=reason)
    assert launcher._cancelled("turn_u", "task_request") is True
    assert {"global_stop", "presence_lost", "control_stop", "shutdown"} <= HARD_CANCEL_REASONS


def test_closing_cancels_every_route():
    launcher, _ = _launcher()
    launcher._closing.set()
    assert launcher._cancelled("turn_u", "task_request") is True and launcher._cancelled("turn_u", "agent_loop") is True


def test_the_loops_cancel_callable_follows_the_run_route():
    launcher, state = _launcher(ended=True, reason="barge_in")
    assert launcher._build_loop(_ctx("task_request"))._cancelled() is False
    assert launcher._build_loop(_ctx("agent_loop"))._cancelled() is True
    state["reason"] = "global_stop"
    assert launcher._build_loop(_ctx("task_request"))._cancelled() is True


# ---- 結果延後通知 ----

def test_task_request_result_after_an_interrupted_turn_goes_through_the_host_entry():
    seen: list[ResumedRunResult] = []
    launcher, _ = _launcher(ended=True, reason="barge_in", on_resumed=lambda r: seen.append(r) or ResumeDisposition.DELIVERED)
    ctx = _ctx("task_request")
    assert launcher._deliver(ctx, "做好了") is True
    assert ctx.delivered and ctx.text == "做好了"
    (r,) = seen
    assert (r.run_id, r.worker_epoch, r.activity_id, r.outcome, r.text) == ("run_1", 2, "actv_1", ResumeOutcome.COMPLETED, "做好了")


@pytest.mark.parametrize(("disposition", "uncertain"), [
    (ResumeDisposition.PENDING, "loop_delivery_pending"), (ResumeDisposition.UNCERTAIN, "loop_delivery_unknown"),
    (ResumeDisposition.DROPPED, None),
])
def test_deferred_delivery_that_is_not_delivered_does_not_trigger_a_second_failure_message(disposition, uncertain):
    launcher, _ = _launcher(ended=True, on_resumed=lambda r: disposition)
    ctx = _ctx("task_request")
    assert launcher._deliver(ctx, "做好了") is False and not ctx.delivered and ctx.delivery_uncertain == uncertain


def test_task_request_with_a_live_turn_still_uses_the_normal_gate_path():
    called: list = []
    launcher, _ = _launcher(ended=False, on_resumed=lambda r: called.append(r))
    sent: list = []
    launcher._delivery = SimpleNamespace(
        prepare=lambda i: sent.append(i),
        dispatch=lambda i: SimpleNamespace(status=__import__("uep.output.delivery", fromlist=["x"]).DeliveryStatus.SENT),
    )
    assert launcher._deliver(_ctx("task_request"), "即時回覆") is True and called == [] and len(sent) == 1


def test_agent_loop_result_after_the_turn_ended_is_not_rerouted():
    called: list = []
    launcher, _ = _launcher(ended=True, on_resumed=lambda r: called.append(r))
    assert launcher._deliver(_ctx("agent_loop"), "x") is False and called == []  # 現狀：取消，不送


def test_without_a_host_entry_the_deferred_result_has_nowhere_to_go():
    launcher, _ = _launcher(ended=True, on_resumed=None)
    assert launcher._deliver(_ctx("task_request"), "x") is False
