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


# ---- 審查修正：硬取消在 turn 結束後仍生效、turn 結束後的通知經 Host 入口、TOCTOU、Task 狀態、budget_deferred ----

def _real_orchestrator():
    from uep.turn.decision_config import DecisionConfig
    from uep.turn.generations import TurnGenerations
    from uep.turn.orchestrator import TurnOrchestrator, _Live

    orch = TurnOrchestrator(None, TurnGenerations(), auth_provider=lambda: None, decision=DecisionConfig())  # type: ignore[arg-type]
    orch.generations.begin("turn_u")
    orch._emit = lambda *a, **k: None
    orch._phase = lambda *a, **k: None
    orch._live["turn_u"] = _Live(ticket=SimpleNamespace(turn_id="turn_u", speaker_ref="s", reply_mode="respond"), trace_id=None, utterance_event_id=None)
    return orch


def test_hard_cancel_survives_the_real_orchestrator_forgetting_the_reason_after_turn_end():
    from uep.turn.models import TurnEndFacts

    orch = _real_orchestrator()
    host = {"stopped": False, "lease": True}
    launcher = _stub_launcher(resume_cancel_check=lambda: host["stopped"] or not host["lease"])
    launcher._orchestrator = orch
    assert launcher._cancelled("turn_u", "task_request") is False
    orch.cancel_turn("turn_u", "global_stop")  # turn 還在：orchestrator 記得原因
    assert launcher._cancelled("turn_u", "task_request") is True
    orch.on_turn_ended("turn_u", TurnEndFacts(status="cancelled", reason_code="global_stop"))
    assert orch.cancel_reason_of("turn_u") is None  # turn 結束後原因查不到
    assert launcher._cancelled("turn_u", "task_request") is False  # 沒有 Host 回報時 Run 繼續（插話讓位不取消）
    host["stopped"] = True  # Host 在 global_stop 時已設 _stopped_reason
    assert launcher._cancelled("turn_u", "task_request") is True
    host["stopped"], host["lease"] = False, False
    assert launcher._cancelled("turn_u", "task_request") is True  # Lease 遺失
    assert launcher._cancelled("turn_u", "agent_loop") is True  # agent_loop：turn 結束即取消（現狀）


def _late_launcher(*, ended=True):
    seen: list[ResumedRunResult] = []
    launcher, state = _launcher(ended=ended, on_resumed=lambda r: seen.append(r) or ResumeDisposition.DELIVERED)
    launcher._delivery = SimpleNamespace(
        prepare=lambda i: None, dispatch=lambda i: pytest.fail("turn 結束後不應再走 turn 綁定的 Gate 路徑"),
    )
    return launcher, state, seen


def test_budget_notices_after_turn_end_go_through_the_host_entry_with_distinct_keys():
    from uep.echo.agent_wiring import BUDGET_ABORT_TEXT, BUDGET_NOTICE_TEXT

    launcher, _, seen = _late_launcher()
    ctx = _ctx("task_request")
    launcher._on_budget(ctx, "notice")
    launcher._on_budget(ctx, "abort")
    assert [(r.outcome, r.text) for r in seen] == [(ResumeOutcome.FAILED, BUDGET_NOTICE_TEXT), (ResumeOutcome.FAILED, BUDGET_ABORT_TEXT)]
    assert len({r.key for r in seen}) == 2 and ctx.abort_notice_ok  # 各自獨立的冪等鍵，Host 不會把第二則當重複


def test_failure_notice_after_turn_end_goes_through_the_host_entry_once():
    from uep.echo.agent_wiring import LOOP_FAILURE_TEXT

    launcher, _, seen = _late_launcher()
    ctx = _ctx("task_request")
    launcher._notify_failure(ctx)
    launcher._notify_failure(ctx)
    assert [(r.outcome, r.text) for r in seen] == [(ResumeOutcome.FAILED, LOOP_FAILURE_TEXT)]


def test_partial_after_turn_end_is_sent_as_the_failure_notice_not_as_a_completed_reply():
    from uep.echo.agent_wiring import BUDGET_ABORT_TEXT, LOOP_FAILURE_TEXT

    launcher, _, seen = _late_launcher()
    ctx = _ctx("task_request")
    launcher._on_budget(ctx, "abort")
    assert launcher._deliver(ctx, "已取得的部分結果") is False and not ctx.delivered
    launcher._notify_failure(ctx)  # _conclude 的補送：已送過就不重複
    assert [(r.outcome, r.text) for r in seen] == [(ResumeOutcome.FAILED, BUDGET_ABORT_TEXT), (ResumeOutcome.FAILED, LOOP_FAILURE_TEXT)]
    assert all(r.outcome is not ResumeOutcome.COMPLETED for r in seen)  # FAILED 不寫記憶（Host 只對 COMPLETED＋APPROVED 寫）


def test_gate_refusal_after_the_turn_check_falls_back_to_the_host_entry():
    from uep.output.delivery import DeliveryStatus

    seen: list[ResumedRunResult] = []
    launcher, state = _launcher(ended=False, on_resumed=lambda r: seen.append(r) or ResumeDisposition.DELIVERED)

    def dispatch(intent):
        state["ended"] = True  # 檢查之後 turn 才被插話結束：Gate 以 turn 世代拒絕
        return SimpleNamespace(status=DeliveryStatus.REJECTED)

    launcher._delivery = SimpleNamespace(prepare=lambda i: None, dispatch=dispatch)
    ctx = _ctx("task_request")
    assert launcher._deliver(ctx, "做好了") is True and ctx.delivered
    assert [(r.outcome, r.text, r.seq) for r in seen] == [(ResumeOutcome.COMPLETED, "做好了", 0)]
    state["ended"] = False
    seen.clear()
    launcher._notify_failure(_ctx("task_request"))  # 通知也一樣：Gate 拒絕且 turn 已結束 -> Host 入口
    assert [r.outcome for r in seen] == [ResumeOutcome.FAILED]


def test_agent_loop_never_falls_back_to_the_host_entry():
    from uep.output.delivery import DeliveryStatus

    seen: list = []
    launcher, state = _launcher(ended=False, on_resumed=lambda r: seen.append(r))
    launcher._delivery = SimpleNamespace(prepare=lambda i: None, dispatch=lambda i: SimpleNamespace(status=DeliveryStatus.REJECTED))
    state["ended"] = True
    assert launcher._deliver(_ctx("agent_loop"), "x") is False and seen == []


@pytest.mark.parametrize(("route", "state", "raised", "delivered", "expected_ok"), [
    ("task_request", "completed", False, False, True),  # 結果延後（PENDING／UNCERTAIN）不讓 Task 變 FAILED
    ("task_request", "failed", False, True, False),
    ("task_request", "completed", True, False, False),
    ("agent_loop", "completed", False, False, False),  # 首跑 agent_loop 不變：看是否送達
    ("agent_loop", "completed", False, True, True),
])
def test_first_run_settle_follows_the_route_policy(route, state, raised, delivered, expected_ok):
    launcher, _ = _launcher()
    calls: list[bool] = []
    launcher._settle = lambda ctx, ok, reason: calls.append(ok)
    ctx = _ctx(route)
    ctx.delivered = delivered
    launcher._conclude(ctx, SimpleNamespace(state=state, reason_code=None, partial=False), raised)
    assert calls == [expected_ok]


def test_budget_deferred_is_an_optional_checkpoint_counter():
    from uep.agent.budget import BudgetCounters
    from uep.agent.loop import RESUME_REQUIRED_KEYS

    c = BudgetCounters(steps=1, budget_deferred=True)
    assert BudgetCounters.from_checkpoint(c.to_checkpoint()).budget_deferred is True
    legacy = {k: v for k, v in c.to_checkpoint().items() if k != "deferred"}
    assert BudgetCounters.from_checkpoint(legacy).budget_deferred is False  # 舊 checkpoint 缺鍵視為 False
    assert "deferred" not in RESUME_REQUIRED_KEYS and "budget_deferred" not in RESUME_REQUIRED_KEYS


def test_loop_persists_budget_deferred_in_the_checkpoint():
    from tests.uep.loop_support import TAGS, Round
    from tests.uep.test_agent_loop import Rig, call, trigger
    from uep.echo.agent_wiring import TASK_REQUEST_BUDGET

    rig = Rig([Round("", (call("read.lone", "1"), call("read.lone", "2"))), Round("收束")], budget=TASK_REQUEST_BUDGET)
    rig.run(trigger=trigger(tags=TAGS, kind="task_request", route="task_request"))
    assert rig.tasks.checkpoints[-1]["budget_counters"]["deferred"] == 1
