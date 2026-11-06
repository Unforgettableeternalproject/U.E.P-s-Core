"""切片 E1（離線）：核准 park／resume——park_run 順序、帶 approval 續跑、三種重驗失敗、過期再 park、TG-7 反例。

FakeTasks 的核准模型見 ``loop_support``（``grant`` 登記、不合格拋 ``ApprovalInvalid(code)``、單次消耗）；
真實 Manager 的對照 DB 測試在 ``test_agent_loop_approval_db.py``。迴圈只轉交呼叫端提供的 approval_id，從不建立核准。
"""
from __future__ import annotations

import pytest

from tests.uep.loop_support import TAGS, Round, projector_for
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
    (_res("blocked", reason="tool_not_ready", action_id=None), "repark"),
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


def test_pending_tool_not_ready_parks_again_and_a_later_resume_completes():
    """resume 時 pending 的工具被 Gateway 以 tool_not_ready 擋下（模組暖機中）：未建 action、核准未消耗 ⇒ 再 park，
    不讓 Run 失敗；模組轉 ready 後同一份核准可以完成。（用過期的投影模擬「預檢之後才轉非 ready」；
    投影本身已排除非 ready 工具時，預檢會先以 capability_revoked 攔下。）"""
    from tests.uep.test_tool_registry import _POST, make_entry
    from uep.tools.registry import ToolRegistry

    registry = ToolRegistry.build([
        make_entry("read.lone"), make_entry("post.mod", spec=dict(_POST), requires=("mod:b",)),
    ], known_modules=["mod:b"])
    cap = projector_for(registry)(TAGS, None)
    stale = lambda tags, prior: cap
    rig = Rig([Round("先查再發", (call("read.lone", "1"), call("post.mod", "2"))), Round("全部完成")],
              registry=registry, projector=stale, budget=LoopBudget(repeat_limit=99))
    assert rig.run().state == "awaiting_approval"
    action = stable_action_id(RUN, "step_1", 1)
    checkpoint = _park_checkpoint(rig)
    rig.tasks.grant("appr_ok", action)
    assert registry.apply_module_event("module.warming", {"module_id": "mod:b", "revision": 1})
    parks_before = len(rig.tasks.park_calls)

    out = _resume(rig, checkpoint, {action: "appr_ok"}, projector=stale)
    assert (out.state, out.reason_code, out.pending_action_ids) == ("awaiting_approval", "approval_required", (action,))
    assert len(rig.tasks.park_calls) == parks_before + 1 and rig.last_runtime.requests == []
    assert rig.tasks.approvals["appr_ok"].consumed_by is None and action not in rig.tasks.outcomes
    assert rig.tasks.run_status == []  # 沒有失敗

    assert registry.apply_module_event("module.ready", {"module_id": "mod:b", "revision": 2})
    done = _resume(rig, _park_checkpoint(rig), {action: "appr_ok"}, epoch=3, projector=stale)
    assert done.state == "completed" and rig.tasks.approvals["appr_ok"].consumed_by == action
    assert [t for _, t in rig.tasks.executions] == ["read.lone", "post.mod"]


# ---- 預檢：暫時非 ready ⇒ 再 park（真實投影），真的撤銷 ⇒ capability_revoked ----

def _module_rig(known=("mod:b",)):
    from tests.uep.test_tool_registry import _POST, make_entry
    from uep.tools.registry import ToolRegistry

    entries = [make_entry("read.lone"), make_entry("post.mod", spec=dict(_POST), requires=("mod:b",))]
    registry = ToolRegistry.build(entries, known_modules=known)
    rig = Rig([Round("先查再發", (call("read.lone", "1"), call("post.mod", "2"))), Round("全部完成")],
              registry=registry, budget=LoopBudget(repeat_limit=99))  # 真實投影：projector_for(registry)
    assert rig.run().state == "awaiting_approval"
    return rig, registry, stable_action_id(RUN, "step_1", 1)


@pytest.mark.parametrize("state", ["warming", "unavailable"])
def test_precheck_reparks_while_module_is_not_ready_and_completes_after_it_recovers(state):
    rig, registry, action = _module_rig()
    checkpoint = _park_checkpoint(rig)
    rig.tasks.grant("appr_ok", action)
    assert registry.apply_module_event(f"module.{state}", {"module_id": "mod:b", "revision": 1})
    assert "post.mod" not in {t["tool"] for t in projector_for(registry)(TAGS, None)["tools"]}  # 真實投影已排除它
    parks = len(rig.tasks.park_calls)

    out = _resume(rig, checkpoint, {action: "appr_ok"})
    assert (out.state, out.reason_code, out.pending_action_ids) == ("awaiting_approval", "approval_required", (action,))
    assert len(rig.tasks.park_calls) == parks + 1 and rig.last_runtime.requests == []
    assert rig.tasks.approvals["appr_ok"].consumed_by is None and rig.tasks.run_status == []
    # AL-3：再 park 不改寫 checkpoint，被暫時排除的工具仍在 visible_tools，下一次 resume 重新跑完整預檢
    assert _park_checkpoint(rig) == checkpoint

    assert registry.apply_module_event("module.ready", {"module_id": "mod:b", "revision": 2})
    done = _resume(rig, _park_checkpoint(rig), {action: "appr_ok"}, epoch=3)
    assert done.state == "completed" and rig.tasks.approvals["appr_ok"].consumed_by == action
    assert [t for _, t in rig.tasks.executions] == ["read.lone", "post.mod"]


def test_precheck_reparks_again_each_time_it_is_still_not_ready():
    rig, registry, _action = _module_rig()
    checkpoint = _park_checkpoint(rig)
    registry.apply_module_event("module.warming", {"module_id": "mod:b", "revision": 1})
    for epoch in (2, 3, 4):
        assert _resume(rig, checkpoint, None, epoch=epoch).state == "awaiting_approval"
    assert len(rig.tasks.park_calls) == 4 and rig.tasks.run_status == []


def test_precheck_unknown_module_is_readiness_not_revocation():
    """coordinator 不認得該模組（RA-15：readiness 恆為 unavailable）屬 readiness，不是撤銷 ⇒ 再 park。"""
    from tests.uep.test_tool_registry import _POST, make_entry
    from uep.tools.registry import ToolRegistry

    rig, _registry, action = _module_rig()
    checkpoint = _park_checkpoint(rig)
    rig.tasks.grant("appr_ok", action)
    restarted = ToolRegistry.build(
        [make_entry("read.lone"), make_entry("post.mod", spec=dict(_POST), requires=("mod:b",))], known_modules=[],
    )
    assert restarted.readiness("post.mod") == "unavailable"
    out = _resume(rig, checkpoint, {action: "appr_ok"}, registry=restarted, projector=projector_for(restarted))
    assert out.state == "awaiting_approval" and rig.tasks.approvals["appr_ok"].consumed_by is None


def test_precheck_ready_but_not_projected_is_still_capability_revoked():
    rig, registry, _action = _module_rig()
    checkpoint = _park_checkpoint(rig)
    base = projector_for(registry)
    narrowed = lambda tags, prior: {"tools": [t for t in base(tags, prior)["tools"] if t["tool"] != "post.mod"]}
    assert registry.readiness("post.mod") == "ready"  # scope／平台／policy 收窄，不是 readiness
    out = _resume(rig, checkpoint, None, projector=narrowed)
    assert (out.state, out.reason_code) == ("failed", "capability_revoked")


def test_precheck_not_in_frozen_set_is_capability_revoked():
    rig, registry, _action = _module_rig()
    checkpoint = _park_checkpoint(rig)
    checkpoint["frozen_tools"] = {k: v for k, v in checkpoint["frozen_tools"].items() if k != "post.mod"}
    registry.apply_module_event("module.warming", {"module_id": "mod:b", "revision": 1})  # 即使同時非 ready 也是撤銷
    out = _resume(rig, checkpoint, None)
    assert (out.state, out.reason_code) == ("failed", "capability_revoked")


def test_precheck_without_readiness_or_entry_keeps_revocation():
    """registry 沒有 readiness 方法、或沒有該條目 ⇒ 不當成暫時非 ready，行為不變。"""
    rig, registry, _action = _module_rig()
    checkpoint = _park_checkpoint(rig)
    base = projector_for(registry)
    gone = lambda tags, prior: {"tools": [t for t in base(tags, prior)["tools"] if t["tool"] != "post.mod"]}

    class NoReadiness:
        def __getattr__(self, name):
            if name == "readiness":
                raise AttributeError(name)
            return getattr(registry, name)

    out = _resume(rig, checkpoint, None, registry=NoReadiness(), projector=gone)
    assert (out.state, out.reason_code) == ("failed", "capability_revoked")

    rig2, registry2, _a = _module_rig()
    cp2 = _park_checkpoint(rig2)

    class NoEntry:
        def __getattr__(self, name):
            return getattr(registry2, name)

        def readiness(self, tool):
            from uep.tasks.manager import PolicyViolation

            raise PolicyViolation("沒有這個條目")

    base2 = projector_for(registry2)
    out2 = _resume(rig2, cp2, None, registry=NoEntry(), projector=lambda tags, prior: {
        "tools": [t for t in base2(tags, prior)["tools"] if t["tool"] != "post.mod"],
    })
    assert (out2.state, out2.reason_code) == ("failed", "capability_revoked")


def test_precheck_repark_with_superseded_epoch_stops_without_marking_parked():
    """預檢再 park 時 park_run 發現 Run 已被別的 worker 接手 ⇒ stale，不 mark_parked。"""
    rig, registry, _action = _module_rig()
    checkpoint = _park_checkpoint(rig)
    registry.apply_module_event("module.warming", {"module_id": "mod:b", "revision": 1})
    entries_before = list(rig.store.parent_entries["ctx_parent"])
    loop = _crash(rig, 2, rounds=[Round("不該到這裡")])
    original = rig.tasks.park_run

    def taken_over(run_id, *, worker_epoch):
        rig.tasks.epoch = 9  # park 之前 Run 已被取代
        return original(run_id, worker_epoch=worker_epoch)

    rig.tasks.park_run = taken_over
    out = loop.resume(RUN, checkpoint, trigger=trigger(), parent_id="ctx_parent", approvals=None)
    assert out.state == "stale" and rig.last_runtime.requests == []
    assert list(rig.store.parent_entries["ctx_parent"]) == entries_before  # 沒有新的 mark_parked


def test_policy_excluded_and_warming_reparks_but_becomes_revoked_once_ready():
    """釘住：工具同時被 policy 排除、模組又在 warming ⇒ 預檢只看得到 readiness，先再 park；
    等模組 ready，readiness 不再能解釋它為何不在投影 ⇒ 下一次 resume 才是 capability_revoked。"""
    rig, registry, _action = _module_rig()
    checkpoint = _park_checkpoint(rig)
    base = projector_for(registry)
    policy_excluded = lambda tags, prior: {"tools": [t for t in base(tags, prior)["tools"] if t["tool"] != "post.mod"]}
    registry.apply_module_event("module.warming", {"module_id": "mod:b", "revision": 1})
    first = _resume(rig, checkpoint, None, projector=policy_excluded)
    assert first.state == "awaiting_approval" and rig.tasks.run_status == []
    registry.apply_module_event("module.ready", {"module_id": "mod:b", "revision": 2})
    second = _resume(rig, _park_checkpoint(rig), None, epoch=3, projector=policy_excluded)
    assert (second.state, second.reason_code) == ("failed", "capability_revoked")
