"""PR-A（Chain 契約 §2）：LoopTrigger 的 route／budget、預算解析、三段門檻（warn／notice／abort）。離線，不需 DB。"""
from __future__ import annotations

from collections import Counter
from types import SimpleNamespace

import pytest

from tests.uep.loop_support import Round
from tests.uep.test_agent_loop import RUN, Rig, call, trigger
from tests.uep.test_agent_loop_consult import CTAGS, CRig, consult
from tests.uep.test_agent_loop_entry import _stub_launcher, _ticket
from uep.adapters.runtime import BUDGET_HINT_CONVERGE
from uep.agent.budget import (
    BudgetCounters,
    BudgetPolicyTable,
    BudgetThresholds,
    LoopBudget,
    budget_stage,
    hard_budget,
    resolve_budget,
    tighten,
)
from uep.agent.core import stable_action_id
from uep.agent.trigger import BudgetCap, BudgetRequest, LoopTrigger

STAGED = BudgetThresholds(1.0, 1.5, 2.0)


# ---- trigger 欄位 ----

def test_nine_positional_fields_still_construct_with_defaults():
    t = LoopTrigger("user_turn", "t", "g", frozenset({"x"}), "s", "o", "p", "snap", None)
    assert t.route == "agent_loop" and t.budget is None


def test_route_tier_cap_validation():
    base = ("proactive", "t", "g", frozenset({"x"}), "s", "o", "p", "snap")
    with pytest.raises(ValueError):
        LoopTrigger(*base, route="direct_chat")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        LoopTrigger("user_turn", *base[1:], route="task_request")
    with pytest.raises(ValueError):
        LoopTrigger("task_request", *base[1:], route="agent_loop")
    with pytest.raises(ValueError):
        BudgetRequest(tier="huge")
    with pytest.raises(ValueError):
        BudgetCap(max_steps=0)
    assert LoopTrigger("task_request", *base[1:], route="task_request", budget=BudgetRequest("lite", BudgetCap(max_steps=3)))


# ---- 解析：只收窄 ----

def _policy(**over):
    return LoopBudget(max_steps=10, max_tool_calls=40, max_tokens=1000, run_deadline_s=100.0, thresholds=STAGED, **over)


def test_unresolved_policy_falls_back_to_launcher_budget_and_counts():
    default = LoopBudget(max_steps=7)
    metrics: Counter[str] = Counter()
    other = BudgetPolicyTable({("p", "task_request", "lite"): _policy()})
    out = resolve_budget(other, default, policy_ref="p", route="agent_loop", tier=None, cap=None, metrics=metrics)
    assert out == default and metrics["budget.policy_unresolved"] == 1
    resolve_budget(BudgetPolicyTable(), default, policy_ref="p", route="agent_loop", tier=None, cap=None, metrics=metrics)
    assert metrics["budget.policy_unresolved"] == 1  # 空表（尚未配置任何列）不計，避免每個 Run 都是噪音


@pytest.mark.parametrize("cap_steps", [1, 3, 7, 20, 25, 100])
def test_cap_never_widens_and_abort_point_stays_within_cap(cap_steps):
    policy = _policy()
    table = BudgetPolicyTable({("p", "agent_loop", "standard"): policy})
    out = resolve_budget(table, LoopBudget(), policy_ref="p", route="agent_loop", tier=None, cap=BudgetCap(max_steps=cap_steps))
    abort_steps = hard_budget(out).max_steps
    assert abort_steps <= cap_steps and abort_steps <= hard_budget(policy).max_steps
    assert out.max_steps <= policy.max_steps


def test_cap_scales_thresholds_and_min_on_other_dimensions():
    policy = _policy()
    table = BudgetPolicyTable({("p", "agent_loop", "lite"): policy})
    out = resolve_budget(
        table, LoopBudget(), policy_ref="p", route="agent_loop", tier="lite",
        cap=BudgetCap(max_steps=8, max_tool_calls=999, run_deadline_s=30.0),
    )
    assert out.max_steps == 4 and out.thresholds == STAGED  # 中斷點 8 = 4 × 2.0，三段門檻等比縮小
    assert out.max_tool_calls == 40 and out.run_deadline_s == 30.0  # 試圖放寬的維度被忽略


def test_unlimited_policy_dimension_is_narrowed_by_cap():
    table = BudgetPolicyTable({("p", "agent_loop", "standard"): LoopBudget(max_steps=10, max_tool_calls=40, thresholds=STAGED)})
    out = resolve_budget(table, LoopBudget(), policy_ref="p", route="agent_loop", tier=None, cap=BudgetCap(max_tokens=600))
    assert out.max_tokens == 300


def test_tighten_takes_min_per_dimension():
    a = LoopBudget(max_steps=10, max_tool_calls=20, max_tokens=None, run_deadline_s=50.0)
    b = LoopBudget(max_steps=5, max_tool_calls=30, max_tokens=100, run_deadline_s=None)
    out = tighten(a, b)
    assert (out.max_steps, out.max_tool_calls, out.max_tokens, out.run_deadline_s) == (5, 20, 100, 50.0)


def test_startup_validation_rejects_tool_fuse_below_step_abort_point():
    with pytest.raises(ValueError):
        LoopBudget(max_steps=5, max_tool_calls=9, thresholds=STAGED)
    assert LoopBudget(max_steps=5, max_tool_calls=10, thresholds=STAGED)
    assert LoopBudget(max_steps=5, max_tool_calls=1)  # 未啟用三段門檻：維持舊行為，不驗證


def test_budget_stage_uses_steps_and_tokens():
    b = LoopBudget(max_steps=10, max_tool_calls=50, max_tokens=100, thresholds=STAGED)
    assert [budget_stage(b, BudgetCounters(steps=s)) for s in (0, 9, 10, 15, 20)] == [0, 0, 1, 2, 3]
    assert budget_stage(b, BudgetCounters(steps=0, tokens=150)) == 2
    assert budget_stage(LoopBudget(max_steps=1), BudgetCounters(steps=5)) == 0  # 未啟用


# ---- 迴圈：三段門檻 ----

def _staged_rig(rounds, *, kinds, delivered=None, **over):
    budget = LoopBudget(max_steps=2, max_tool_calls=20, thresholds=STAGED, repeat_limit=99)
    deliver = None if delivered is None else (lambda text: delivered.append(text) or True)
    return Rig(rounds, budget=budget, on_budget=kinds.append, deliver_result=deliver, **over)


def _tool_rounds(n):
    return [Round("", (call("read.lone", str(i)),)) for i in range(n)]


def test_default_thresholds_never_notify_and_never_hint():
    kinds: list[str] = []
    rig = Rig(_tool_rounds(5) + [Round("收束")], budget=LoopBudget(max_steps=2, repeat_limit=99), on_budget=kinds.append)
    out = rig.run()
    assert kinds == [] and not out.partial
    assert all(r.budget_hint is None for r in rig.runtime.requests)


def test_each_stage_fires_once_and_hint_follows_warn():
    kinds: list[str] = []
    delivered: list[str] = []
    rig = _staged_rig(_tool_rounds(4) + [Round("收束文字")], kinds=kinds, delivered=delivered)
    out = rig.run()
    assert kinds == ["warn", "notice", "abort"]
    hints = [r.budget_hint for r in rig.runtime.requests]
    assert hints[0] is None and BUDGET_HINT_CONVERGE in hints[1:]
    assert (out.state, out.reason_code, out.partial) == ("failed", "budget_exhausted", True)
    assert out.text == "收束文字" and delivered == ["收束文字"] and out.delivered
    assert rig.tasks.run_status[-1].value == "FAILED"


def test_abort_with_failed_finalize_step_aggregates_receipts_deterministically():
    kinds: list[str] = []
    delivered: list[str] = []
    rig = _staged_rig(_tool_rounds(4) + [Round(error="PROVIDER_UNAVAILABLE")], kinds=kinds, delivered=delivered)
    out = rig.run()
    assert (out.state, out.reason_code, out.partial) == ("failed", "budget_exhausted", True)
    assert "read.lone：ok" in out.text and delivered == [out.text]  # 不經模型，確定性彙整
    assert "read.lone:0" not in out.text  # 只放工具名與狀態，不放結果內容


def test_abort_after_failed_tools_lists_status_only_and_still_delivers():
    kinds: list[str] = []
    delivered: list[str] = []
    budget = LoopBudget(max_steps=1, max_tool_calls=20, thresholds=STAGED, repeat_limit=99)
    rig = Rig([Round("", (("write.s", {"path": "x"}),)), Round("", (("write.s", {"path": "y"}),)), Round(error="TIMEOUT")],
              budget=budget, on_budget=kinds.append, deliver_result=lambda t: delivered.append(t) or True)
    rig.tasks.fail_tools.add("write.s")
    out = rig.run()
    assert out.partial and out.state == "failed" and kinds[-1] == "abort"
    assert "write.s：failed" in out.text and delivered == [out.text]


def test_resume_does_not_resend_passed_stages():
    rig = _staged_rig([], kinds=[])
    st = rig.loop._new_state(RUN, trigger(), "p", {"tools": []}, {}, frozenset(), [], "ref", BudgetCounters(steps=3))
    assert st.budget_stage == 2 and st.budget_hint == BUDGET_HINT_CONVERGE
    assert rig.loop._check_stages(st) is False


# ---- 升級候選複製 route／budget ----

def test_escalation_candidate_copies_route_and_budget():
    seen = []
    request = BudgetRequest("lite", BudgetCap(max_steps=3))
    rig = CRig([Round("", (consult("output_modality", proposed_modalities=["audio_stream"]),)), Round("x")])
    rig.loop_kwargs["on_escalate"] = seen.append
    rig.loop = rig.new_loop()
    rig.run(trigger=trigger(tags=CTAGS, budget=request))
    assert seen and seen[0].trigger.budget == request and seen[0].trigger.route == "agent_loop"


# ---- launcher：政策表、notice／abort 訊息、origin_ref ----

class _Delivery:
    def __init__(self, status="SENT"):
        self.sent = []
        self.status = status

    def prepare(self, intent):
        self.sent.append(intent)

    def dispatch(self, intent):
        from uep.output.delivery import DeliveryStatus

        return SimpleNamespace(status=DeliveryStatus[self.status])


def test_launcher_without_table_uses_default_budget_and_with_table_resolves_per_run():
    t = LoopTrigger("user_turn", "t", "g", frozenset({"memory.read"}), "scope_private_owner", "o", "p", "snap_1",
                    budget=BudgetRequest(None, BudgetCap(run_deadline_s=10.0)))
    plain = _stub_launcher()
    assert plain._effective_budget(t).run_deadline_s == 10.0  # cap 一樣只收窄預設預算
    policy = LoopBudget(max_steps=4, max_tool_calls=8, thresholds=STAGED, run_deadline_s=60.0)
    table = BudgetPolicyTable({("p", "agent_loop", "standard"): policy})
    with_table = _stub_launcher(budget_policies=table)
    eff = with_table._effective_budget(t)
    assert eff.max_steps == 4 and eff.run_deadline_s == 10.0


def test_notice_and_abort_use_fixed_system_notice_and_do_not_set_delivered():
    from uep.echo.agent_wiring import BUDGET_ABORT_TEXT, BUDGET_NOTICE_TEXT, _RunCtx

    delivery = _Delivery()
    launcher = _stub_launcher(delivery=delivery)
    ctx = _RunCtx(_ticket(), "task", SimpleNamespace(run_id="run_u", worker_epoch=1))
    launcher._on_budget(ctx, "warn")
    assert delivery.sent == []  # warn 不送使用者
    launcher._on_budget(ctx, "notice")
    launcher._on_budget(ctx, "abort")
    assert [i.kind.value for i in delivery.sent] == ["system_notice"] * 2 and all(i.origin.value == "system" for i in delivery.sent)
    assert launcher._texts.get(delivery.sent[0].content_ref) == BUDGET_NOTICE_TEXT
    assert launcher._texts.get(delivery.sent[1].content_ref) == BUDGET_ABORT_TEXT
    assert not ctx.delivered
    m = launcher._orchestrator.metrics
    assert (m["budget.warn"], m["budget.notice"], m["budget.abort"]) == (1, 1, 1)


def test_unknown_delivery_of_notice_is_not_retried():
    from uep.echo.agent_wiring import _RunCtx

    delivery = _Delivery("UNKNOWN")
    launcher = _stub_launcher(delivery=delivery)
    ctx = _RunCtx(_ticket(), "task", SimpleNamespace(run_id="run_u", worker_epoch=1))
    launcher._on_budget(ctx, "notice")
    assert len(delivery.sent) == 1 and not ctx.delivered


def test_resume_takes_min_of_frozen_and_reresolved_budget():
    frozen = LoopBudget(max_steps=10, max_tool_calls=40, run_deadline_s=100.0)
    tighter = LoopBudget(max_steps=6, max_tool_calls=60, run_deadline_s=None)
    out = tighten(frozen, tighter)
    assert (out.max_steps, out.max_tool_calls, out.run_deadline_s) == (6, 40, 100.0)


def test_create_loop_run_records_route_in_origin_ref():
    from uep.echo.agent_wiring import create_loop_run

    seen = {}
    tasks = SimpleNamespace(
        create_task=lambda **k: seen.update(k) or SimpleNamespace(task_id="t"),
        start_run=lambda *a, **k: SimpleNamespace(run_id="r"),
    )
    create_loop_run(tasks, trigger(), persona_id="uep", persona_version=1, policy_version=1)
    assert seen["origin_ref"]["route"] == "agent_loop"


# ---- 審查修正：cap 混合維度、tighten、tokens 端到端、保險絲、取消 ----

def test_mixed_dimension_cap_never_widens_any_dimension():
    """steps 的 cap 很大、tokens 的 cap 很小：steps 不得因 tokens 觸發放棄分段而被放寬。"""
    policy = _policy()  # steps D=10（硬上限 20）、tokens D=1000（硬上限 2000）、門檻 (1,1.5,2)
    table = BudgetPolicyTable({("p", "agent_loop", "standard"): policy})
    out = resolve_budget(table, LoopBudget(), policy_ref="p", route="agent_loop", tier=None, cap=BudgetCap(max_steps=100, max_tokens=1))
    hard, orig = hard_budget(out), hard_budget(policy)
    assert hard.max_steps <= orig.max_steps == 20 and hard.max_tokens is not None and hard.max_tokens <= 1


def test_tighten_compares_hard_limits_not_d():
    frozen = LoopBudget(max_steps=10, max_tool_calls=40, thresholds=STAGED)  # 硬上限 20
    fresh = LoopBudget(max_steps=12, max_tool_calls=40)  # 不分段，硬上限 12
    assert hard_budget(tighten(frozen, fresh)).max_steps == 12
    assert hard_budget(tighten(fresh, frozen)).max_steps == 12  # 對稱
    looser = LoopBudget(max_steps=30, max_tool_calls=60)
    assert hard_budget(tighten(frozen, looser)).max_steps == 20  # 重新解析更寬：沿用凍結值


def test_budget_stage_and_hard_limit_share_one_algorithm_for_fractional_ratios():
    b = LoopBudget(max_steps=5, max_tool_calls=30, thresholds=BudgetThresholds(1.0, 1.2, 1.5))
    assert hard_budget(b).max_steps == 8  # ceil(5 × 1.5)
    assert [budget_stage(b, BudgetCounters(steps=s)) for s in (4, 5, 6, 7, 8)] == [0, 1, 2, 2, 3]


def test_tokens_dimension_triggers_abort_end_to_end():
    kinds: list[str] = []
    budget = LoopBudget(max_steps=10, max_tool_calls=40, max_tokens=100, thresholds=STAGED, repeat_limit=99)
    rounds = [Round("", (call("read.lone", str(i)),), usage=80) for i in range(4)] + [Round("收束")]
    rig = Rig(rounds, budget=budget, on_budget=kinds.append)
    out = rig.run()
    assert kinds == ["warn", "notice", "abort"] and out.partial and out.reason_code == "budget_exhausted"


def test_tool_calls_exhausted_first_keeps_todays_finalize_even_with_thresholds():
    """tool_calls 與牆鐘是不分段的保險絲（Chain 契約 2.3.1）：先耗盡時沿用今天的 finalize，不標 partial。"""
    kinds: list[str] = []
    budget = LoopBudget(max_steps=3, max_tool_calls=6, thresholds=STAGED, repeat_limit=99)
    first = tuple(call("read.lone", str(i)) for i in range(3))
    second = tuple(call("read.lone", str(i + 3)) for i in range(3))
    rig = Rig([Round("", first), Round("", second), Round("收束")], budget=budget, on_budget=kinds.append)
    out = rig.run()
    assert (out.state, out.reason_code, out.partial) == ("completed", "budget_finalized", False) and "abort" not in kinds


def test_cancel_during_abort_step_ends_cancelled_without_partial():
    flag = {"on": False}
    kinds: list[str] = []

    def on_budget(kind):
        kinds.append(kind)
        flag["on"] = kind == "abort"

    rig = _staged_rig(_tool_rounds(4) + [Round("收束文字")], kinds=[])
    rig.loop = rig.new_loop(on_budget=on_budget, cancelled=lambda: flag["on"], deliver_result=None)
    out = rig.run()
    assert out.state == "cancelled" and not out.partial and kinds[-1] == "abort"


def test_stale_epoch_during_abort_step_stops_the_loop():
    kinds: list[str] = []
    rig = _staged_rig(_tool_rounds(4) + [Round("收束文字")], kinds=kinds)

    def on_budget(kind):
        kinds.append(kind)
        if kind == "abort":
            rig.tasks.stale_on_heartbeat = True

    rig.loop = rig.new_loop(on_budget=on_budget)
    assert rig.run().state == "stale"


def test_budget_stage_log_carries_stable_event_key(caplog):
    import logging

    rig = _staged_rig(_tool_rounds(4) + [Round("收束")], kinds=[])
    with caplog.at_level(logging.WARNING, logger="uep.agent.loop"):
        rig.run()
    events = [(r.event, r.stage) for r in caplog.records if getattr(r, "event", None) == "budget_stage"]
    assert events == [("budget_stage", 1), ("budget_stage", 2)]


def test_active_consult_uses_the_abort_point_as_baseline():
    from tests.uep.test_agent_loop_consult import CRig
    from uep.tools.catalog.decision import CONSULT_TOOL

    budget = LoopBudget(max_steps=5, max_tool_calls=40, max_consults=3, thresholds=STAGED, consult_loop_end_margin=3)
    rig = CRig([], budget=budget)
    st = rig.loop._new_state(RUN, trigger(), "p", {"tools": []}, {}, frozenset({"decision.consult"}), [], "ref", BudgetCounters(steps=7))
    cap = {"tools": [{"tool": CONSULT_TOOL, "version": 1}]}
    assert rig.loop._active_consult_due(st, cap) is True  # 硬上限 10 - 7 = 3 <= margin（以 D=5 算會是負數而不觸發）


# ---- launcher：_conclude 的 partial 分支、abort 通知失敗、續跑 ctx ----

def _partial_launcher(status="SENT", memory=None):
    delivery = _Delivery(status)
    launcher = _stub_launcher(delivery=delivery, memory_store=memory)
    launcher._tasks = SimpleNamespace(get_run=lambda run_id: (_ for _ in ()).throw(RuntimeError("no db")))
    return launcher, delivery


def _outcome():
    return SimpleNamespace(state="failed", reason_code="budget_exhausted", partial=True, text="")


@pytest.mark.parametrize(("delivered", "notice_ok", "expect_failure_text"), [
    (True, True, False), (False, True, True), (True, False, True), (False, False, True),
])
def test_conclude_partial_never_records_completed_and_backfills_failure_text(delivered, notice_ok, expect_failure_text):
    from uep.echo.agent_wiring import LOOP_FAILURE_TEXT, _RunCtx

    launcher, delivery = _partial_launcher()
    ctx = _RunCtx(_ticket(), "task", SimpleNamespace(run_id="run_u", worker_epoch=1))
    ctx.delivered, ctx.abort_notice_ok, ctx.text = delivered, notice_ok, "部分結果" if delivered else ""
    facts = launcher._conclude(ctx, _outcome(), False)
    assert facts.status == "failed" and facts.partial is True and facts.reason_code == "loop_budget_exhausted"
    assert facts.generation_ok is False
    texts = [launcher._texts.get(i.content_ref) for i in delivery.sent]
    assert (LOOP_FAILURE_TEXT in texts) is expect_failure_text


def test_partial_turn_is_not_written_to_memory():
    from uep.echo.agent_wiring import _RunCtx
    from uep.turn.models import TurnEndFacts

    stored: list = []
    launcher, _ = _partial_launcher(memory=lambda ticket, facts: stored.append(facts))
    ctx = _RunCtx(_ticket(), "task", SimpleNamespace(run_id="run_u", worker_epoch=1))
    launcher._store_memory(ctx.ticket, TurnEndFacts(status="failed", partial=True, spoken_text="部分"))
    assert stored == []
    launcher._store_memory(ctx.ticket, TurnEndFacts(status="completed", generation_ok=True, spoken_text="完整"))
    assert len(stored) == 1


@pytest.mark.parametrize(("status", "ok"), [("SENT", True), ("UNKNOWN", True), ("REJECTED", False)])
def test_abort_notice_failure_is_tracked_so_conclude_can_backfill(status, ok):
    from uep.echo.agent_wiring import _RunCtx

    launcher, _ = _partial_launcher(status)
    ctx = _RunCtx(_ticket(), "task", SimpleNamespace(run_id="run_u", worker_epoch=1))
    launcher._on_budget(ctx, "abort")
    assert ctx.abort_notice_ok is ok


def test_resumed_ctx_only_records_metric_for_notice():
    from uep.echo.agent_wiring import _RunCtx

    launcher, delivery = _partial_launcher()
    ctx = _RunCtx(_ticket(), "task", SimpleNamespace(run_id="run_u", worker_epoch=1), resumed=True)
    launcher._on_budget(ctx, "notice")
    assert delivery.sent == [] and launcher._orchestrator.metrics["budget.notice"] == 1


def test_merge_back_carries_the_delivered_partial_result():
    from uep.agent.loop_context import merge_back
    from uep.fakes.loop_context import InMemoryLoopContextStore

    store = InMemoryLoopContextStore()
    merge_back(store, run_id="r1", parent_id="p", scope_ref="s", state="failed", reason_code="budget_exhausted",
               summary="部分", result="部分", delivered=True, partial=True)
    entry = store.parent_entries["p"][0]
    assert entry.payload.state == "failed" and entry.payload.result == "部分"


# ---- 複審 Minor：不確定交付不補失敗訊息、resume 補上的結果進 trail、merge-back 失敗不蓋掉 partial ----

def test_conclude_partial_with_uncertain_delivery_does_not_send_a_second_message():
    from uep.echo.agent_wiring import _RunCtx

    launcher, delivery = _partial_launcher()
    ctx = _RunCtx(_ticket(), "task", SimpleNamespace(run_id="run_u", worker_epoch=1))
    ctx.delivered, ctx.abort_notice_ok, ctx.delivery_uncertain = False, True, "loop_delivery_unknown"
    facts = launcher._conclude(ctx, _outcome(), False)
    assert facts.partial is True and delivery.sent == []  # 可能已送達：不補發


def test_results_filled_in_on_resume_enter_the_trail():
    from tests.uep.test_agent_loop import (
        RUN,
        _crash,
        _park_checkpoint,
        _park_rig,
        trigger,
    )

    rig = _park_rig()
    rig.run()
    checkpoint = _park_checkpoint(rig)
    rig.tasks.grant("appr_ok", stable_action_id(RUN, "step_1", 1))
    loop = _crash(rig, 2)
    seen = []
    original = loop._guarded
    loop._guarded = lambda st, action: seen.append(st) or original(st, action)
    loop.resume(RUN, checkpoint, trigger=trigger(), parent_id="ctx_parent", approvals={stable_action_id(RUN, "step_1", 1): "appr_ok"})
    st = seen[0]
    assert any(item["tool"] == "post.ask" and item["status"] == "ok" for item in st.trail)


def test_abort_survives_a_failing_merge_back():
    kinds: list[str] = []
    delivered: list[str] = []
    rig = _staged_rig(_tool_rounds(4) + [Round("收束文字")], kinds=kinds, delivered=delivered)
    original = rig.store.merge_into_parent

    def broken(key, parent_id, payload):
        if getattr(payload, "state", "") == "failed":
            raise RuntimeError("store down")
        return original(key, parent_id, payload)

    rig.store.merge_into_parent = broken
    out = rig.run()
    assert out.partial and out.state == "failed" and delivered == ["收束文字"]
