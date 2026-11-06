"""切片 E1（離線）：核准 park／resume——park_run 順序、帶 approval 續跑、三種重驗失敗、過期再 park、TG-7 反例。

FakeTasks 的核准模型見 ``loop_support``（``grant`` 登記、不合格拋 ``ApprovalInvalid(code)``、單次消耗）；
真實 Manager 的對照 DB 測試在 ``test_agent_loop_approval_db.py``。迴圈只轉交呼叫端提供的 approval_id，從不建立核准。
"""
from __future__ import annotations

import pytest

from tests.uep.loop_support import Round, projector_for
from tests.uep.test_agent_loop import (
    RUN,
    _crash,
    _park_checkpoint,
    _park_rig,
    trigger,
)
from uep.agent.core import stable_action_id
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


def test_manager_version_check_is_the_last_line_of_defense():
    """預檢看不到（投影仍是舊版）、但註冊表在執行時已升版：Manager 回 tool_version_changed ⇒ FAILED，核准不消耗。"""
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
