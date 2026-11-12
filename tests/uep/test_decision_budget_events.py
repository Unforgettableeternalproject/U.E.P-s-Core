"""預算門檻事件寫入 decision_log（agent.budget；Chain 契約 §2.3.1）。不需資料庫。"""
from __future__ import annotations

from collections import Counter
from types import SimpleNamespace

from uep.decision import DecisionHub, DecisionRequest
from uep.decision.budget_events import KIND_AGENT_BUDGET, register_budget_events
from uep.decision.log import DecisionLogWriter


class _ListSink:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def write(self, rows) -> None:
        self.rows.extend(rows)


def _wired():
    hub = DecisionHub()
    sink = _ListSink()
    writer = DecisionLogWriter(sink, flush_interval=0.01)
    writer.attach(hub)
    return hub, writer, sink, register_budget_events(hub)


def test_each_stage_writes_one_row_with_sub_kind_and_run_id_only():
    hub, writer, sink, recorder = _wired()
    for stage in ("warn", "notice", "abort"):
        recorder("run_1", "task_1", stage)
    assert writer.flush()
    assert [r["sub_kind"] for r in sink.rows] == ["budget_warn", "budget_notice", "task_budget_exceeded"]
    assert all(r["kind"] == KIND_AGENT_BUDGET and r["status"] == "ok" and r["outcome"] == "recorded" for r in sink.rows)
    assert [r["reason_code"] for r in sink.rows] == ["threshold_1_0x", "threshold_1_5x", "threshold_2_0x"]
    assert all(r["request_id"].startswith("run_1:budget:") for r in sink.rows)
    assert all(r["utterance_refs"] is None for r in sink.rows)  # 不含任何文字或其參照


def test_unknown_stage_is_ignored_and_invalid_facts_fall_back():
    hub, writer, sink, recorder = _wired()
    recorder("run_1", None, "bogus")
    hub.publish(KIND_AGENT_BUDGET, DecisionRequest(
        request_id="x", kind=KIND_AGENT_BUDGET, trigger="event", facts={"sub_kind": "budget_warn"}))
    assert writer.flush()
    assert len(sink.rows) == 1 and sink.rows[0]["status"] == "invalid" and sink.rows[0]["fallback"] is True


def test_launcher_on_budget_calls_recorder_and_survives_its_failure():
    from uep.echo.agent_wiring import LoopLauncher, _RunCtx

    launcher = LoopLauncher.__new__(LoopLauncher)
    launcher._orchestrator = SimpleNamespace(metrics=Counter())
    launcher._texts = None
    ctx = _RunCtx(SimpleNamespace(turn_id="t"), "task_9", SimpleNamespace(run_id="run_9", worker_epoch=1))
    launcher._budget_recorder = None
    launcher._on_budget(ctx, "warn")  # 未掛記錄器：行為不變
    calls: list[tuple] = []
    launcher.set_budget_recorder(lambda *a: calls.append(a))
    launcher._on_budget(ctx, "warn")
    assert calls == [("run_9", "task_9", "warn")]

    def boom(*a):
        raise RuntimeError("x")

    launcher.set_budget_recorder(boom)
    launcher._on_budget(ctx, "warn")
    assert launcher._orchestrator.metrics["budget.warn"] == 3
