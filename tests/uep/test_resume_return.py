"""續跑結果回 Host：LoopLauncher 端的映射與注入（D107 R1；離線）。Host 端的 Gate／記憶見 test_agent_loop_entry_db.py。"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests.uep.test_agent_loop_entry import _stub_launcher, _ticket
from uep.echo.agent_wiring import _RunCtx
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


@pytest.mark.parametrize(("state", "raised", "expected"), [
    ("failed", False, ResumeOutcome.FAILED), ("completed", True, ResumeOutcome.FAILED),
    ("cancelled", False, ResumeOutcome.CANCELLED), ("stale", False, ResumeOutcome.CANCELLED),
])
def test_other_states_carry_no_text(state, raised, expected):
    seen: list[ResumedRunResult] = []
    launcher = _launcher(lambda r: seen.append(r) or ResumeDisposition.NOT_APPLICABLE)
    launcher._return_to_host(_ctx(), state, "x", raised)
    assert seen[0].outcome is expected and seen[0].text == ""


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
