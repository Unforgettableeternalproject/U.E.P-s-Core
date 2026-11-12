"""續跑結果回 Host：LoopLauncher 端的映射與注入（D107 R1；離線）。Host 端的 Gate／記憶見 test_agent_loop_entry_db.py。"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests.uep.test_agent_loop_entry import _stub_launcher, _ticket
from uep.echo.agent_wiring import LOOP_FAILURE_TEXT, _RunCtx
from uep.echo.resume_return import (
    ResumeCause,
    ResumeDisposition,
    ResumedRunResult,
    ResumeOutcome,
)


def _launcher(callback, *, epoch=3):
    launcher = _stub_launcher(on_resumed=callback)
    launcher._tasks = SimpleNamespace(get_run=lambda run_id: SimpleNamespace(worker_epoch=epoch))
    return launcher


def _ctx(**over):
    ctx = _RunCtx(_ticket(), "task_1", SimpleNamespace(run_id="run_1", worker_epoch=3), resumed=True, activity_id="actv_1")
    ctx.text = "完成了"
    for k, v in over.items():
        setattr(ctx, k, v)
    return ctx


def test_completed_state_maps_to_completed_result_with_identity_and_text():
    seen: list[ResumedRunResult] = []
    launcher = _launcher(lambda r: seen.append(r) or ResumeDisposition.DELIVERED)
    assert launcher._return_to_host(_ctx(), "completed", None, False) is ResumeDisposition.DELIVERED
    (r,) = seen
    assert (r.run_id, r.worker_epoch, r.task_id, r.activity_id) == ("run_1", 3, "task_1", "actv_1")
    assert r.outcome is ResumeOutcome.COMPLETED and r.cause is ResumeCause.APPROVED and r.text == "完成了"


@pytest.mark.parametrize(("state", "raised", "expected", "text"), [
    ("failed", False, ResumeOutcome.FAILED, LOOP_FAILURE_TEXT), ("completed", True, ResumeOutcome.FAILED, LOOP_FAILURE_TEXT),
    ("cancelled", False, ResumeOutcome.CANCELLED, ""), ("stale", False, ResumeOutcome.CANCELLED, ""),
])
def test_failed_carries_the_fixed_failure_text_and_cancelled_carries_nothing(state, raised, expected, text):
    seen: list[ResumedRunResult] = []
    launcher = _launcher(lambda r: seen.append(r) or ResumeDisposition.NOT_APPLICABLE)
    launcher._return_to_host(_ctx(), state, "x", raised)
    assert seen[0].outcome is expected and seen[0].text == text


def test_budget_abort_partial_in_a_resumed_run_sends_only_the_fixed_failure_text():
    """續跑中預算 abort（state=failed、code=budget_exhausted、ctx.text 為部分結果）：固定失敗訊息，部分結果文字不送。"""
    seen: list[ResumedRunResult] = []
    launcher = _launcher(lambda r: seen.append(r) or ResumeDisposition.DELIVERED)
    ctx = _ctx()
    ctx.text = "已取得的部分結果"
    launcher._return_to_host(ctx, "failed", "budget_exhausted", False)
    assert seen[0].outcome is ResumeOutcome.FAILED and seen[0].text == LOOP_FAILURE_TEXT and seen[0].reason_code == "budget_exhausted"


def test_cause_is_carried_to_host():
    seen: list[ResumedRunResult] = []
    launcher = _launcher(lambda r: seen.append(r) or ResumeDisposition.DELIVERED)
    launcher._return_to_host(_ctx(cause=ResumeCause.REJECTED), "completed", None, False)
    assert seen[0].cause is ResumeCause.REJECTED


def test_no_callback_keeps_old_behavior():
    assert _launcher(None)._return_to_host(_ctx(), "completed", None, False) is None


def test_superseded_epoch_worker_does_not_call_host():
    called: list = []
    launcher = _launcher(lambda r: called.append(r), epoch=9)  # Run 已被別的 worker 接手
    assert launcher._return_to_host(_ctx(), "completed", None, False) is None and called == []


def test_host_exception_becomes_uncertain_not_a_crash():
    def boom(r):
        raise RuntimeError("x")

    assert _launcher(boom)._return_to_host(_ctx(), "completed", None, False) is ResumeDisposition.UNCERTAIN


# ---- Console：/reject 傳 cause、已渲染的文字不重複印 ----

def _console_with(launcher):
    from contextlib import contextmanager

    from uep.console.commands import Console

    @contextmanager
    def session():
        yield SimpleNamespace(get=lambda *a: None, scalars=lambda *a: [])

    rt = SimpleNamespace(core=SimpleNamespace(loop_launcher=launcher), session_factory=session)
    lines: list[str] = []
    return Console(rt, out=lines.append, wait_s=1), lines


def _fake_launcher(disposition, text="結果"):
    from concurrent.futures import Future

    from uep.echo.agent_wiring import LoopResumeResult

    seen: dict = {}

    class _L:
        def resume(self, run_id, approvals=None, *, cause=ResumeCause.APPROVED):
            seen["cause"] = cause
            f: Future = Future()
            f.set_result(LoopResumeResult(run_id, "completed", text, None, disposition))
            return f

    return _L(), seen


def test_console_reject_passes_rejected_cause_and_approve_passes_approved():
    launcher, seen = _fake_launcher(None)
    console, _ = _console_with(launcher)
    console._resume("run_1", {}, cause=ResumeCause.REJECTED)
    assert seen["cause"] is ResumeCause.REJECTED
    console._resume("run_1", {})
    assert seen["cause"] is ResumeCause.APPROVED


def test_console_does_not_print_text_the_endpoint_already_rendered():
    launcher, _ = _fake_launcher(ResumeDisposition.DELIVERED)
    console, lines = _console_with(launcher)
    console._resume("run_1", {})
    assert not [line for line in lines if line.startswith("回應（續跑）")] and any("delivered" in line for line in lines)
    launcher2, _ = _fake_launcher(ResumeDisposition.DROPPED)
    console2, lines2 = _console_with(launcher2)
    console2._resume("run_1", {})
    assert [line for line in lines2 if line.startswith("回應（續跑）")]  # 沒送出的才由 Console 補印


# ---- R2：ResumePending 佇列（純記憶體，離線）----

def _res(run_id, epoch=1):
    return ResumedRunResult(
        run_id=run_id, worker_epoch=epoch, task_id="t", activity_id="actv_1", ticket=object(),
        outcome=ResumeOutcome.COMPLETED, cause=ResumeCause.APPROVED, text="x",
    )


def test_pending_queue_orders_oldest_first_dedupes_and_overflows_oldest():
    from uep.echo.resume_return import ResumePending

    q = ResumePending(ttl_s=60, max_items=2)
    assert q.add(_res("a")) is None and q.add(_res("b")) is None
    assert q.add(_res("b")) is None and len(q) == 2  # 同鍵不重複入列
    evicted = q.add(_res("c"))
    assert evicted is not None and evicted.run_id == "a" and [r.run_id for r in q.snapshot()] == ["b", "c"]
    assert ("b", 1) in q and ("a", 1) not in q


def test_pending_queue_expires_by_ttl_lazily():
    from uep.echo.resume_return import ResumePending

    now = {"t": 0.0}
    q = ResumePending(ttl_s=30, max_items=8, clock=lambda: now["t"])
    q.add(_res("a"))
    now["t"] = 10
    q.add(_res("b"))
    now["t"] = 31
    assert [r.run_id for r in q.expire()] == ["a"] and [r.run_id for r in q.snapshot()] == ["b"]
    now["t"] = 45
    assert [r.run_id for r in q.expire()] == ["b"] and len(q) == 0


def test_pending_queue_rejects_invalid_limits():
    from uep.echo.resume_return import ResumePending

    with pytest.raises(ValueError):
        ResumePending(ttl_s=0, max_items=1)
    with pytest.raises(ValueError):
        ResumePending(ttl_s=1, max_items=0)


def test_settings_defaults_match_the_decision():
    from uep.config import UepSettings

    s = UepSettings()
    assert (s.resume_pending_ttl_s, s.resume_pending_max) == (1800.0, 8)


# ---- PR-C：Task 狀態與送達解耦（Q4）、resumed 取消納入 Lease／global_stop（Q7）----

class _FakeResumeTasks:
    """_work_resume 用的最小 TaskManager：Run 已 SUCCEEDED（迴圈自己轉的），記錄 launcher 對 Task 的收尾。"""

    heartbeat_stale_after = __import__("datetime").timedelta(seconds=60)

    def __init__(self, run_status="SUCCEEDED", epoch=3):
        self.run_status, self.epoch = run_status, epoch
        self.task_calls: list = []
        self.run_calls: list = []

    def get_run(self, run_id):
        return SimpleNamespace(worker_epoch=self.epoch, status=self.run_status)

    def transition_run(self, run_id, to, *, worker_epoch):
        self.run_calls.append(to)

    def transition_task(self, task_id, to, **kw):
        self.task_calls.append((to, kw.get("reason_code")))

    def heartbeat(self, *a, **k):
        return None


def _run_work_resume(outcome_state, *, delivered, run_status, callback):
    from concurrent.futures import Future

    from uep.agent.loop import LoopOutcome
    from uep.echo.agent_wiring import ParkedLoop

    launcher = _stub_launcher(on_resumed=callback)
    tasks = _FakeResumeTasks(run_status=run_status)
    launcher._tasks = tasks
    ctx = _ctx()
    ctx.delivered = delivered
    launcher._active = 1
    launcher._build_loop = lambda c: SimpleNamespace(
        resume=lambda *a, **k: LoopOutcome(outcome_state, "run_1", "budget_exhausted" if outcome_state == "failed" else None, "完成了"),
    )
    entry = ParkedLoop(_ticket(), "task_1", SimpleNamespace(), "parent")
    future: Future = Future()
    launcher._work_resume(entry, ctx, {}, {}, future)
    return future.result(5), tasks


def test_task_succeeds_when_work_completed_even_if_the_reply_was_not_delivered():
    """Q4：SUCCEEDED 只代表工作完成；文字有沒有送達（這裡是 DROPPED）不影響。"""
    from uep.tasks.states import RunStatus

    result, tasks = _run_work_resume("completed", delivered=False, run_status="SUCCEEDED", callback=lambda r: ResumeDisposition.DROPPED)
    assert tasks.task_calls == [(RunStatus.SUCCEEDED, None)] and result.disposition is ResumeDisposition.DROPPED


def test_task_fails_when_the_run_failed_and_the_failure_notice_is_requested():
    from uep.tasks.states import RunStatus

    seen: list[ResumedRunResult] = []
    result, tasks = _run_work_resume(
        "failed", delivered=True, run_status="FAILED", callback=lambda r: seen.append(r) or ResumeDisposition.DELIVERED,
    )
    assert tasks.task_calls[0][0] is RunStatus.FAILED
    assert seen[0].outcome is ResumeOutcome.FAILED and seen[0].text == LOOP_FAILURE_TEXT and result.disposition is ResumeDisposition.DELIVERED


def test_resumed_loop_cancel_includes_lease_loss_and_global_stop_when_host_says_so():
    state = {"cancel": False}
    launcher = _stub_launcher(resume_cancel_check=lambda: state["cancel"])
    ctx = _ctx()
    loop = launcher._build_loop(ctx)
    assert loop._cancelled() is False
    state["cancel"] = True
    assert loop._cancelled() is True  # Lease 遺失／global_stop
    state["cancel"] = False
    ctx.cancel_requested = True
    assert loop._cancelled() is True  # 原本的旗標仍有效


def test_resumed_loop_without_a_host_check_behaves_as_before_and_check_errors_do_not_cancel():
    launcher = _stub_launcher()
    assert launcher._build_loop(_ctx())._cancelled() is False

    def boom():
        raise RuntimeError("x")

    broken = _stub_launcher(resume_cancel_check=boom)
    assert broken._build_loop(_ctx())._cancelled() is False


def test_host_resume_should_cancel_on_lease_loss_and_global_stop():
    from uep.echo.host import EchoPipelineHost

    host = EchoPipelineHost.__new__(EchoPipelineHost)
    host.endpoint = SimpleNamespace(lease_auth=lambda: object())
    host._stopped_reason = None
    assert host.resume_should_cancel() is False
    host._stopped_reason = "global_stop"
    assert host.resume_should_cancel() is True
    host._stopped_reason = None
    host.endpoint = SimpleNamespace(lease_auth=lambda: None)
    assert host.resume_should_cancel() is True
