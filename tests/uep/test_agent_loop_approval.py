"""切片 E1（離線）：核准 park／resume——park_run 順序、帶 approval 續跑、三種重驗失敗、過期再 park、TG-7 反例。

FakeTasks 的核准模型見 ``loop_support``（``grant`` 登記、不合格拋 ``ApprovalInvalid(code)``、單次消耗）；
真實 Manager 的對照 DB 測試在 ``test_agent_loop_approval_db.py``。迴圈只轉交呼叫端提供的 approval_id，從不建立核准。
"""
from __future__ import annotations

import pytest

from tests.uep.loop_support import Round, projector_for
from tests.uep.test_agent_loop import (
    RUN,
    Rig,
    _crash,
    _park_checkpoint,
    _park_rig,
    call,
    trigger,
)
from uep.agent.budget import LoopBudget
from uep.agent.core import IntentRecord, IntentResult, stable_action_id
from uep.tasks.states import RunStatus

ACTION = stable_action_id(RUN, "step_1", 1)  # _park_rig 的 post.ask
OTHER = stable_action_id(RUN, "step_1", 0)  # 同批的 read.lone（另一個 action）


def _parked():
    rig = _park_rig()
    rig.run()
    return rig, _park_checkpoint(rig)


def _resume(rig, checkpoint, approvals, *, rounds=None, epoch=2, **over):
    loop = _crash(rig, epoch, rounds=rounds if rounds is not None else [Round("全部完成")], **over)
    return loop.resume(RUN, checkpoint, trigger=trigger(), parent_id="ctx_parent", approvals=approvals)


def _assert_nothing_consumed(rig, before_ids):
    """TG-7：迴圈沒有建立任何核准、沒有消耗任何核准。"""
    assert set(rig.tasks.approvals) == before_ids
    assert all(a.consumed_by is None for a in rig.tasks.approvals.values())


# ---- park 出口 ----

def test_park_order_is_checkpoint_then_park_run_then_mark_parked():
    rig = _park_rig()
    seen = {}
    original = rig.tasks.park_run

    def spy(run_id, *, worker_epoch):
        seen["parked_marks"] = list(rig.store.parent_entries.get("ctx_parent", []))
        return original(run_id, worker_epoch=worker_epoch)

    rig.tasks.park_run = spy
    out = rig.run()
    assert out.state == "awaiting_approval"
    assert rig.tasks.log[-2:] == ["checkpoint", "park"] and rig.tasks.park_calls == [(RUN, 1)]
    assert seen["parked_marks"] == []  # park_run 時還沒 mark_parked
    assert [e.key for e in rig.store.parent_entries["ctx_parent"]] == [f"loop_status:{RUN}:awaiting_approval"]


def test_park_with_superseded_epoch_stops_without_marking_parked():
    rig = _park_rig()
    original = rig.tasks.park_run

    def taken_over(run_id, *, worker_epoch):
        rig.tasks.epoch = 9  # park 之前 Run 已被別的 worker 接手
        return original(run_id, worker_epoch=worker_epoch)

    rig.tasks.park_run = taken_over
    out = rig.run()
    assert out.state == "stale" and "ctx_parent" not in rig.store.parent_entries


def test_repark_on_resume_also_calls_park_run_after_checkpoint():
    rig, checkpoint = _parked()
    out = _resume(rig, checkpoint, None)
    assert out.state == "awaiting_approval"
    assert [p[1] for p in rig.tasks.park_calls] == [1, 2] and rig.tasks.log[-2:] == ["checkpoint", "park"]


# ---- 核准後執行並續跑 ----

def test_approved_resume_executes_once_and_runs_to_completion():
    rig, checkpoint = _parked()
    rig.tasks.grant("appr_ok", ACTION)
    out = _resume(rig, checkpoint, {ACTION: "appr_ok"})
    assert (out.state, out.text) == ("completed", "全部完成")
    assert [t for _, t in rig.tasks.executions] == ["read.lone", "post.ask"]
    assert rig.tasks.approvals["appr_ok"].consumed_by == ACTION and rig.tasks.run_status == [RunStatus.SUCCEEDED]
    tools = [m for m in rig.child() if m.role == "tool"]
    assert tools[1].content == "post.ask:2" and tools[2].content == "tool blocked: deferred_by_approval_wait"


def test_replay_of_approved_resume_never_repeats_side_effects():
    rig, checkpoint = _parked()
    rig.tasks.grant("appr_ok", ACTION)
    saved_children = dict(rig.store.children)  # 模擬「第一次續跑在寫入結果前崩潰」：child 與 checkpoint 還停在 park 當下
    _resume(rig, checkpoint, {ACTION: "appr_ok"})
    rig.store.children.update(saved_children)
    again = _resume(rig, checkpoint, {ACTION: "appr_ok"}, epoch=3, rounds=[Round("再來一次")])
    assert again.state == "completed"
    assert [t for _, t in rig.tasks.executions].count("post.ask") == 1  # 同 id 已有 receipt，不重做、不再消耗
    assert rig.tasks.approvals["appr_ok"].consumed_by == ACTION


# ---- 過期 ⇒ 再 park；其他無效 ⇒ FAILED ----

def test_expired_approval_parks_again_and_keeps_run_waiting():
    rig, checkpoint = _parked()
    rig.tasks.grant("appr_old", ACTION, state="expired")
    out = _resume(rig, checkpoint, {ACTION: "appr_old"})
    assert (out.state, out.reason_code, out.pending_action_ids) == ("awaiting_approval", "approval_required", (ACTION,))
    assert [t for _, t in rig.tasks.executions] == ["read.lone"] and rig.tasks.run_status == []
    assert rig.tasks.waiting_approval and rig.tasks.checkpoints[-1]["next_step_semantic"] == f"await_approval:{ACTION}"
    assert rig.last_runtime.requests == []


def test_unknown_approval_id_fails_with_approval_invalid():
    rig, checkpoint = _parked()
    out = _resume(rig, checkpoint, {ACTION: "appr_nope"})
    assert (out.state, out.reason_code) == ("failed", "approval_invalid")
    assert rig.tasks.run_status == [RunStatus.FAILED] and rig.last_runtime.requests == []
    assert [t for _, t in rig.tasks.executions] == ["read.lone"]
    assert rig.tasks.checkpoints[-1]["next_step_semantic"] == "end:approval_invalid"


# ---- 重驗失敗：能力撤銷、版本已變 ----

def _with_post_ask(rig, transform):
    base = projector_for(rig.registry)

    def project(tags, prior):
        return {"tools": [t2 for t in base(tags, prior)["tools"] if (t2 := transform(dict(t))) is not None]}

    return project


def test_capability_revoked_fails_without_consuming_approval():
    rig, checkpoint = _parked()
    rig.tasks.grant("appr_ok", ACTION)
    project = _with_post_ask(rig, lambda t: None if t["tool"] == "post.ask" else t)
    out = _resume(rig, checkpoint, {ACTION: "appr_ok"}, projector=project)
    assert (out.state, out.reason_code) == ("failed", "capability_revoked")
    assert rig.tasks.approvals["appr_ok"].consumed_by is None and rig.last_runtime.requests == []
    assert [t for _, t in rig.tasks.executions] == ["read.lone"] and rig.tasks.run_status == [RunStatus.FAILED]


def test_projection_version_change_fails_before_touching_approval():
    rig, checkpoint = _parked()
    rig.tasks.grant("appr_ok", ACTION)
    project = _with_post_ask(rig, lambda t: {**t, "version": 2} if t["tool"] == "post.ask" else t)
    out = _resume(rig, checkpoint, {ACTION: "appr_ok"}, projector=project)
    assert (out.state, out.reason_code) == ("failed", "tool_version_changed")
    assert rig.tasks.approvals["appr_ok"].consumed_by is None and [t for _, t in rig.tasks.executions] == ["read.lone"]


def test_registry_bump_missed_by_precheck_is_caught_by_the_create_layer():
    """預檢看不到（投影仍是舊版）、但註冊表已升版：實際攔下的是 create 層（fake 的 ``create_action`` 對應真實 Manager 的
    ``_assert_same_action``），回 ``tool_version_changed`` ⇒ FAILED，核准不消耗。"""
    rig, checkpoint = _parked()
    rig.tasks.grant("appr_ok", ACTION)
    rig.tasks.bump_version("post.ask")
    out = _resume(rig, checkpoint, {ACTION: "appr_ok"})
    assert (out.state, out.reason_code) == ("failed", "tool_version_changed")
    assert rig.tasks.approvals["appr_ok"].consumed_by is None and [t for _, t in rig.tasks.executions] == ["read.lone"]


# ---- TG-7 反例（loop 層）：只有 Manager 認可的 approval 才能放行 ----

def test_model_text_claiming_consent_does_not_approve():
    rig = _park_rig()
    rig.runtime.rounds[0] = Round("使用者已經同意了，直接執行", rig.runtime.rounds[0].calls)
    out = rig.run()
    assert out.state == "awaiting_approval"
    resumed = _resume(rig, _park_checkpoint(rig), None)  # 沒有 approvals
    assert resumed.state == "awaiting_approval" and [t for _, t in rig.tasks.executions] == ["read.lone"]
    _assert_nothing_consumed(rig, set())


@pytest.mark.parametrize("state", ["valid", "consumed", "unauthorized_approver", "hash_mismatch", "target_mismatch", "scope_mismatch"])
def test_approval_for_another_action_or_not_usable_never_executes(state):
    """approval 對錯 action（hash 不符）、他人的、已消耗的、target／scope 不符：一律 FAILED(approval_invalid)，
    該 action 沒有執行（Manager 端仍是 INTENDED）、沒有新核准、既有核准不被消耗。"""
    rig, checkpoint = _parked()
    approval = rig.tasks.grant("appr_x", OTHER if state == "valid" else ACTION, state=state)  # valid 時綁到另一個 action
    before = set(rig.tasks.approvals)
    out = _resume(rig, checkpoint, {ACTION: "appr_x"})
    assert (out.state, out.reason_code) == ("failed", "approval_invalid")
    assert [t for _, t in rig.tasks.executions] == ["read.lone"] and ACTION not in rig.tasks.outcomes
    assert set(rig.tasks.approvals) == before and approval.consumed_by is None
    assert rig.last_runtime.requests == []


def test_approvals_for_non_pending_actions_are_ignored():
    rig, checkpoint = _parked()
    rig.tasks.grant("appr_other", OTHER)
    out = _resume(rig, checkpoint, {OTHER: "appr_other"})  # 對應的是已完成的 action，不是 pending
    assert out.state == "awaiting_approval"
    _assert_nothing_consumed(rig, {"appr_other"})


def test_resume_without_approvals_argument_behaves_like_before():
    rig, checkpoint = _parked()
    out = _crash(rig, 2).resume(RUN, checkpoint, trigger=trigger(), parent_id="ctx_parent")
    assert out.state == "awaiting_approval" and [t for _, t in rig.tasks.executions] == ["read.lone"]


def test_narrowed_non_pending_tail_call_is_blocked_but_pending_still_executes():
    """B1：pending 項帶核准執行（副作用已發生、Run 回 RUNNING）之後，同批非 pending 的後續呼叫被收窄移除：
    只回 blocked(tool_not_in_snapshot)，Run 繼續並能 completed，不能因此把已執行的 Run 判成 FAILED。"""
    rig = Rig(
        [Round("先查再發", (call("read.lone", "1"), call("post.ask", "2"), call("read.b", "3"))), Round("全部完成")],
        budget=LoopBudget(repeat_limit=99),
    )
    assert rig.run().state == "awaiting_approval"
    checkpoint = _park_checkpoint(rig)
    rig.tasks.grant("appr_ok", ACTION)
    project = _with_post_ask(rig, lambda t: None if t["tool"] == "read.b" else t)  # park 之後 read.b 被收窄移除
    out = _resume(rig, checkpoint, {ACTION: "appr_ok"}, projector=project)
    assert (out.state, out.text) == ("completed", "全部完成")
    assert [t for _, t in rig.tasks.executions] == ["read.lone", "post.ask"]  # pending 照常執行一次
    assert rig.tasks.approvals["appr_ok"].consumed_by == ACTION and rig.tasks.run_status == [RunStatus.SUCCEEDED]
    tools = [m for m in rig.child() if m.role == "tool"]
    assert tools[1].content == "post.ask:2" and tools[2].content == "tool blocked: tool_not_in_snapshot"


# ---- S1：pending 項沒有真的執行 ⇒ 不續跑（不白喚醒模型），原因碼明確 ----

def _assert_ended_without_waking_model(rig, out, reason):
    assert (out.state, out.reason_code) == ("failed", reason)
    assert rig.last_runtime.requests == []  # 沒有白花一次模型呼叫
    assert rig.tasks.run_status == [RunStatus.FAILED] and rig.tasks.checkpoints[-1]["next_step_semantic"] == f"end:{reason}"


def test_pending_unknown_result_ends_run_with_unresolved_actions():
    def lost(args):
        raise ConnectionError("失聯")

    rig = _park_rig(behaviors={"post.ask": lost})
    rig.run()
    checkpoint = _park_checkpoint(rig)
    rig.tasks.grant("appr_ok", ACTION)
    out = _resume(rig, checkpoint, {ACTION: "appr_ok"})
    _assert_ended_without_waking_model(rig, out, "unresolved_actions")
    assert ACTION in rig.tasks.unknown and rig.tasks.approvals["appr_ok"].consumed_by == ACTION  # 副作用可能已發生：交 resolve_unknown


def test_pending_blocked_by_tc3_ends_run_with_unresolved_actions():
    rig, checkpoint = _parked()
    rig.tasks.grant("appr_ok", ACTION)
    rig.tasks.actions["act_elsewhere"] = {"run_id": RUN, "step_id": "s", "tool": "write.s", "params": {}}
    rig.tasks.unknown.add("act_elsewhere")  # Run 內另有非唯讀的 UNKNOWN ⇒ TC-3 擋下
    out = _resume(rig, checkpoint, {ACTION: "appr_ok"})
    _assert_ended_without_waking_model(rig, out, "action_blocked")
    assert rig.tasks.approvals["appr_ok"].consumed_by is None and ACTION not in rig.tasks.outcomes


def test_pending_params_hash_mismatch_ends_run_with_checkpoint_incomplete():
    rig, checkpoint = _parked()
    rig.tasks.grant("appr_ok", ACTION)
    rig.tasks.actions[ACTION]["params"] = {"path": "不同的參數"}  # 已建 action 的參數與 checkpoint 還原的呼叫不一致
    out = _resume(rig, checkpoint, {ACTION: "appr_ok"})
    _assert_ended_without_waking_model(rig, out, "params_mismatch")
    assert rig.tasks.approvals["appr_ok"].consumed_by is None


def test_pending_presence_required_parks_again_without_consuming_approval():
    rig, checkpoint = _parked()
    rig.tasks.grant("appr_ok", ACTION)
    rig.tasks.presence_bound_tools.add("post.ask")
    rig.tasks.presence_missing = True
    out = _resume(rig, checkpoint, {ACTION: "appr_ok"})
    assert (out.state, out.reason_code, out.pending_action_ids) == ("awaiting_approval", "approval_required", (ACTION,))
    assert rig.last_runtime.requests == [] and rig.tasks.approvals["appr_ok"].consumed_by is None
    assert [p[1] for p in rig.tasks.park_calls] == [1, 2]
    rig.tasks.presence_missing = False  # Presence 恢復後可重試同一份核准
    again = _resume(rig, _park_checkpoint(rig), {ACTION: "appr_ok"}, epoch=3)
    assert again.state == "completed" and rig.tasks.approvals["appr_ok"].consumed_by == ACTION


def test_pending_rejected_by_screening_ends_run_with_intent_rejected():
    rig, checkpoint = _parked()
    loop = _crash(rig, 2, rounds=[Round("不該到這裡")])
    core = loop._core
    loop._core.screen_intent = lambda **kw: IntentResult(
        IntentRecord(kw["ordinal"], kw["tool"], None, "rejected", "schema_invalid"), core._gateway.result_view("rejected"),
    )
    out = loop.resume(RUN, checkpoint, trigger=trigger(), parent_id="ctx_parent", approvals={})
    _assert_ended_without_waking_model(rig, out, "intent_rejected")


@pytest.mark.parametrize(("status", "reason"), [
    ("rejected", "intent_rejected"),  # 建立被拒（PolicyViolation）
    ("blocked", "action_blocked"),  # 建立被擋（ActionBlocked，如 task 已終態）
    ("failed", "action_blocked"),  # 建立時的基礎設施錯誤
])
def test_pending_not_created_ends_run(status, reason):
    rig, checkpoint = _parked()
    loop = _crash(rig, 2, rounds=[Round("不該到這裡")])
    core = loop._core
    loop._core.create_screened = lambda item: IntentResult(
        IntentRecord(item.ordinal, item.tool, None, status, "x"), core._gateway.result_view(status),
    )
    out = loop.resume(RUN, checkpoint, trigger=trigger(), parent_id="ctx_parent", approvals={})
    _assert_ended_without_waking_model(rig, out, reason)


def test_pending_executed_but_failed_still_continues():
    """已執行（有 receipt）但工具失敗：不是「沒執行」，照常把失敗結果回饋模型並續跑。"""
    rig = _park_rig()
    rig.run()
    checkpoint = _park_checkpoint(rig)
    rig.tasks.fail_tools.add("post.ask")
    rig.tasks.grant("appr_ok", ACTION)
    out = _resume(rig, checkpoint, {ACTION: "appr_ok"})
    assert out.state == "completed" and rig.last_runtime.requests != []
    assert rig.tasks.approvals["appr_ok"].consumed_by == ACTION


# ---- _pending_stop 對照表：每一條路徑都有斷言 ----

def _res(status, *, reason=None, action_id="act_1", **extra):
    return IntentResult(IntentRecord(0, "post.ask", action_id, status, reason), {"status": status}, **extra)


@pytest.mark.parametrize(("res", "expected"), [
    (_res("blocked", reason="ActionBlocked", block_code="tool_version_changed"), "tool_version_changed"),
    (_res("blocked", reason="ApprovalInvalid", approval_code="not_found"), "approval_invalid"),
    (_res("blocked", reason="ApprovalInvalid", approval_code="consumed"), "approval_invalid"),
    (_res("blocked", reason="ApprovalInvalid", approval_code="expired"), "repark"),
    (_res("blocked", reason="ApprovalRequired", approval_required=True), "repark"),
    (_res("blocked", reason="PresenceRequired"), "repark"),
    (_res("blocked", reason="PresenceRequired", retryable=True), "repark"),
    (_res("ok"), None),
    (_res("failed"), None),  # 已執行但工具失敗
    (_res("unknown", reason="ConnectionError"), "unresolved_actions"),
    (_res("rejected", reason="params_hash_mismatch"), "params_mismatch"),
    (_res("rejected", reason="schema_invalid", action_id=None), "intent_rejected"),
    (_res("rejected", reason="tool_unknown", action_id=None), "intent_rejected"),
    (_res("blocked", reason="ActionBlocked", block_code="action_unresolved"), "unresolved_actions"),
    (_res("blocked", reason="ActionBlocked"), "action_blocked"),
    (_res("blocked", reason="action_blocked", action_id=None), "action_blocked"),
    (_res("failed", reason="OperationalError", action_id=None), "action_blocked"),
])
def test_pending_stop_maps_every_outcome_to_one_code(res, expected):
    from uep.agent.loop import AgentLoop

    assert AgentLoop._pending_stop(res) == expected


def test_non_retryable_presence_loss_is_an_executed_failure_not_a_repark():
    """階段 1.5 的 PresenceRequired(retryable=False)：action 已 FAILED、核准已消耗、Run 已回 RUNNING。
    不能再 park（核准已用掉，永遠等不到下一份）；視為已執行且失敗，回 failed 視圖並續跑，不呼叫第二次 park_run。"""
    rig, checkpoint = _parked()
    rig.tasks.grant("appr_ok", ACTION)
    rig.tasks.presence_bound_tools.add("post.ask")
    rig.tasks.presence_missing, rig.tasks.presence_retryable = True, False
    parks_before = len(rig.tasks.park_calls)
    out = _resume(rig, checkpoint, {ACTION: "appr_ok"})
    assert out.state == "completed" and out.state != "awaiting_approval"
    assert len(rig.tasks.park_calls) == parks_before  # 沒有再 park
    assert rig.tasks.approvals["appr_ok"].consumed_by == ACTION and rig.tasks.run_status == [RunStatus.SUCCEEDED]
    assert [t for _, t in rig.tasks.executions] == ["read.lone"]  # executor 沒被呼叫
    tools = [m for m in rig.child() if m.role == "tool"]
    assert "failed" in tools[1].content


def test_presence_check_only_applies_to_presence_bound_tools():
    """FakeTasks 與真實 Manager 一致：沒有宣告 presence_bound 的工具不受 Presence 不足影響。"""
    rig, checkpoint = _parked()
    rig.tasks.grant("appr_ok", ACTION)
    rig.tasks.presence_missing = True  # post.ask 未登記為 presence_bound
    out = _resume(rig, checkpoint, {ACTION: "appr_ok"})
    assert out.state == "completed" and rig.tasks.approvals["appr_ok"].consumed_by == ACTION


def test_pending_action_already_unknown_ends_run_with_unresolved_actions():
    """崩潰後重放：pending 的 action 本身已是 UNKNOWN（副作用可能已發生）⇒ unresolved_actions，不是 action_blocked。"""
    rig, checkpoint = _parked()
    rig.tasks.grant("appr_ok", ACTION)
    rig.tasks.actions[ACTION]["run_id"] = RUN
    rig.tasks.unknown.add(ACTION)
    out = _resume(rig, checkpoint, {ACTION: "appr_ok"})
    _assert_ended_without_waking_model(rig, out, "unresolved_actions")
    assert rig.tasks.approvals["appr_ok"].consumed_by is None
