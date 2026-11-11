"""composite 的線性展開執行（離線；提案 §5.7.4、RA-14、§8.2）。

FakeTasks 的 ``contents`` 是子步驟的結果文字（回合範圍結果來源，D33），``$.steps[k].output.text`` 即取自這裡；
真實 Manager 的對照 DB 測試在 ``test_agent_loop_composite_db.py``。
"""
from __future__ import annotations

import pytest

from tests.uep.loop_support import TAGS, Round, projector_for
from tests.uep.test_agent_loop import RUN, Rig, _crash, trigger
from uep.adapters.tool import (
    CompositeSpec,
    CompositeStep,
    ToolDescriptor,
    ToolEntry,
    ToolSpec,
)
from uep.agent.budget import LoopBudget
from uep.agent.core import stable_action_id
from uep.agent.scheduler import SchedItem, plan_waves
from uep.tasks.states import RunStatus
from uep.tools.composite import COMPOSITE_STEP_SKIPPED
from uep.tools.registry import ToolRegistry

_READ_IN = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}
_WRITE_IN = {
    "type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"],
}
_OUT = {"type": "object", "properties": {"text": {"type": "string"}, "extra": {"type": "string"}}}
_NOTE_IN = {
    "type": "object", "additionalProperties": False, "required": ["source"],
    "properties": {"source": {"type": "string"}, "title": {"type": "string", "default": "筆記"}},
}


def _entry(name, *, side="internal_read", cap="memory.read", approval="none", schema=_READ_IN, composite=None, out=_OUT):
    spec = ToolSpec(
        executor="fake", side_effect_class=side, idempotency="native", presence_bound=False, tool_version=1,
        capability=cap, approval_level=approval,
    )
    return ToolEntry(name=name, spec=spec, descriptor=ToolDescriptor(f"{name} 說明", schema), output_schema=out, composite=composite)


def _notes(steps, *, name="notes.prepare", initial=None, schema=_NOTE_IN, side="reversible_local", cap="memory.write", approval="none"):
    return _entry(name, side=side, cap=cap, approval=approval, schema=schema, out=_OUT,
                  composite=CompositeSpec(steps=tuple(steps), initial_params=initial or {}))


READ_STEP = CompositeStep("fs.read", {"path": "$.input.source"})
WRITE_STEP = CompositeStep("fs.write", {"path": "$.input.source", "content": "$.steps[0].output.text"})


def _registry(*extra):
    return ToolRegistry.build([
        _entry("fs.read"),
        _entry("fs.write", side="reversible_local", cap="memory.write", schema=_WRITE_IN),
        _entry("fs.ask", side="external_effect", cap="chatroom.post", approval="per_action", schema=_WRITE_IN),
        _notes([READ_STEP, WRITE_STEP]),
        _notes([READ_STEP, CompositeStep("fs.ask", {"path": "$.input.source", "content": "$.steps[0].output.text"})],
               name="notes.publish", side="external_effect", cap="chatroom.post", approval="per_action"),
        *extra,
    ])


def _rig(rounds, *extra, behaviors=None, tags=TAGS, **kwargs):
    registry = _registry(*extra)
    rig = Rig(rounds, registry=registry, behaviors={"fs.read": lambda a: f"內容:{a['path']}", **(behaviors or {})},
              budget=kwargs.pop("budget", LoopBudget(repeat_limit=99)), **kwargs)
    rig.tags = tags
    return rig


def _run(rig):
    return rig.run(trigger=trigger(rig.tags))


def sub_id(step_id: str, ordinal: int, sub: int) -> str:
    return stable_action_id(RUN, step_id, ordinal, sub)


def _views(rig):
    return [view for results in rig.runtime.submitted.values() for _action, view in results]


def _composite(source="/a.md", **extra):
    return ("notes.prepare", {"source": source, **extra})


def _tool_texts(rig):
    return [m.content for m in rig.child() if m.role == "tool"]


# ---- stable_action_id ----

def test_sub_index_yields_distinct_stable_ids_and_none_keeps_old_id():
    base = stable_action_id(RUN, "step_1", 0)
    assert base == stable_action_id(RUN, "step_1", 0, None)
    ids = {stable_action_id(RUN, "step_1", 0, i) for i in range(3)}
    assert len(ids | {base}) == 4 and sub_id("step_1", 0, 1) == sub_id("step_1", 0, 1)


# ---- 成功路徑 ----

def test_two_steps_pass_previous_output_and_each_gets_its_own_action():
    rig = _rig([Round("組合", (_composite(),)), Round("完成")])
    out = _run(rig)
    assert (out.state, out.text) == ("completed", "完成") and rig.tasks.run_status == [RunStatus.SUCCEEDED]
    read_id, write_id = sub_id("step_1", 0, 0), sub_id("step_1", 0, 1)
    assert rig.tasks.executions == [(read_id, "fs.read"), (write_id, "fs.write")]
    assert rig.tasks.actions[write_id]["params"] == {"path": "/a.md", "content": "內容:/a.md"}  # 前步輸出被代入
    assert set(rig.tasks.actions) == {read_id, write_id}  # composite 本身不建 ActionIntent
    assert (out.steps, out.tool_calls) == (2, 2)  # 子步驟各計一次
    view = _views(rig)[0]
    assert view["status"] == "ok" and [(i["step"], i["tool"], i["status"]) for i in view["items"]] == [
        (0, "fs.read", "ok"), (1, "fs.write", "ok"),
    ]
    assert [i["action_id"] for i in view["items"]] == [read_id, write_id]
    assert view["items"][-1]["content"] == "fs.write:/a.md"  # 對外內容取最後一步
    assert _tool_texts(rig)[0] == "fs.write:/a.md"
    cursors = [cp.get("composite_cursor") for cp in rig.tasks.checkpoints]
    assert [(c["tool"], c["next"]) for c in cursors[:2]] == [("notes.prepare", 1), ("notes.prepare", 2)]
    assert cursors[0]["version"] == 1 and cursors[0]["schema_hash"]
    assert all(cp.get("composite_cursor") is None for cp in rig.tasks.checkpoints[2:])  # 完成後 cursor 清除


def test_schema_default_and_initial_params_fill_inputs():
    registry_extra = _notes(
        [CompositeStep("fs.read", {"path": "$.input.source"}),
         CompositeStep("fs.write", {"path": "$.input.title", "content": "$.steps[0].output.text"})],
        name="notes.titled", initial={"source": "/preset.md"},
    )
    rig = _rig([Round("組合", (("notes.titled", {}),)), Round("完成")], registry_extra)
    assert _run(rig).state == "completed"  # source 為必填，但 initial_params 預填，不必再問
    write_id = sub_id("step_1", 0, 1)
    assert rig.tasks.actions[write_id]["params"]["path"] == "筆記"  # title 未給 ⇒ schema default
    rig2 = _rig([Round("組合", (("notes.titled", {"source": "/mine.md"}),)), Round("完成")], registry_extra)
    _run(rig2)
    assert rig2.tasks.actions[sub_id("step_1", 0, 0)]["params"] == {"path": "/mine.md"}  # 模型給的優先於預填


def test_missing_required_input_is_rejected_with_fields_and_runs_nothing():
    rig = _rig([Round("組合", (("notes.prepare", {}),)), Round("完成")])
    assert _run(rig).state == "completed"
    view = _views(rig)[0]
    assert (view["status"], view["reason_code"]) == ("rejected", "invalid_tool_args") and view["fields"] == ["/source"]
    assert rig.tasks.actions == {} and rig.tasks.executions == []  # 工具本身不進等待輸入、不建任何 action


# ---- 失敗語意 ----

def test_first_step_failure_skips_the_rest_and_reports_per_step_rows():
    rig = _rig([Round("組合", (_composite(),)), Round("沒辦法")])
    rig.tasks.fail_tools.add("fs.read")
    out = _run(rig)
    assert out.state == "completed"
    assert [t for _, t in rig.tasks.executions] == ["fs.read"]  # 只有第一步執行過（失敗）；第二步不執行
    assert set(rig.tasks.actions) == {sub_id("step_1", 0, 0)}
    view = _views(rig)[0]
    assert view["status"] == "failed"
    assert [(i["status"], i.get("reason_code")) for i in view["items"]] == [("failed", None), ("blocked", COMPOSITE_STEP_SKIPPED)]
    assert _tool_texts(rig)[0] == "tool failed"


def test_unknown_step_stops_composite_and_marks_run_unresolved():
    def lose(_args):
        raise ConnectionError("失聯")

    rig = _rig([Round("組合", (_composite(),)), Round("完成")], behaviors={"fs.read": lose})
    out = _run(rig)
    assert (out.state, out.reason_code) == ("failed", "unresolved_actions")
    view = _views(rig)[0]
    assert view["status"] == "unknown" and [i["status"] for i in view["items"]] == ["unknown", "blocked"]
    assert rig.tasks.executions == []


def test_overall_status_is_the_worst_of_the_rows():
    rig = _rig([Round("組合", (_composite(),)), Round("完成")])
    rig.tasks.fail_tools.add("fs.write")
    _run(rig)
    view = _views(rig)[0]
    assert [i["status"] for i in view["items"]] == ["ok", "failed"] and view["status"] == "failed"


def test_path_that_cannot_be_resolved_rejects_that_step_with_invalid_tool_args():
    bad = _notes([READ_STEP, CompositeStep("fs.write", {"path": "$.input.source", "content": "$.steps[0].output.extra"})],
                 name="notes.broken")
    rig = _rig([Round("組合", (("notes.broken", {"source": "/a"}),)), Round("完成")], bad)
    _run(rig)
    view = _views(rig)[0]
    assert (view["status"], view["reason_code"]) == ("rejected", "invalid_tool_args") and view["fields"] == ["/steps/1/params"]
    assert [i["status"] for i in view["items"]] == ["ok", "rejected"]
    assert [t for _, t in rig.tasks.executions] == ["fs.read"]  # 第一步已成功，壞路徑那一步沒有建 action


def test_input_path_without_value_or_default_is_rejected():
    schema = {"type": "object", "additionalProperties": False, "properties": {"a": {"type": "string"}, "b": {"type": "string"}}, "required": ["a"]}
    bad = _notes([CompositeStep("fs.read", {"path": "$.input.b"})], name="notes.opt", schema=schema)
    rig = _rig([Round("組合", (("notes.opt", {"a": "x"}),)), Round("完成")], bad)
    _run(rig)
    assert _views(rig)[0]["status"] == "rejected" and rig.tasks.actions == {}


def test_sub_step_outside_frozen_capability_is_blocked_and_composite_fails():
    # 凍結標籤只有 memory.write：composite 與 fs.write 可見，但第一步 fs.read（memory.read）不在凍結範圍（AL-3）
    rig = _rig([Round("組合", (_composite(),)), Round("完成")], tags={"memory.write"})
    out = _run(rig)
    assert out.state == "completed"
    view = _views(rig)[0]
    assert view["status"] == "blocked" and view["reason_code"] == "tool_not_in_snapshot"
    assert [i["status"] for i in view["items"]] == ["blocked", "blocked"]
    assert rig.tasks.actions == {} and rig.tasks.executions == []


def test_tool_call_budget_stops_composite_between_steps():
    rig = _rig([Round("組合", (_composite(),)), Round("完成")], budget=LoopBudget(repeat_limit=99, max_tool_calls=1))
    _run(rig)
    view = _views(rig)[0]
    assert [i["status"] for i in view["items"]] == ["ok", "blocked"] and view["items"][1]["reason_code"] == "deferred_budget"
    assert [t for _, t in rig.tasks.executions] == ["fs.read"]


# ---- 排程：composite 恆為 serial ----

def test_composite_is_always_a_single_serial_wave():
    registry = _registry()
    waves = plan_waves(
        [SchedItem(0, "fs.read", {"path": "a"}), SchedItem(1, "notes.prepare", {"source": "a"}), SchedItem(2, "fs.read", {"path": "b"})],
        registry, max_parallel=4,
    )
    assert [w.indexes for w in waves] == [(0,), (1,), (2,)] and not waves[1].parallel


# ---- park / resume ----

def _park(rounds=None, *extra, **kwargs):
    rig = _rig(rounds or [Round("發佈", (("notes.publish", {"source": "/a.md"}),)), Round("全部完成")], *extra, **kwargs)
    return rig, _run(rig)


def _resume(rig, approvals, *, checkpoint=None, epoch=2, rounds=None):
    loop = _crash(rig, epoch, rounds=rounds if rounds is not None else [Round("全部完成")])
    return loop.resume(RUN, dict(checkpoint or rig.tasks.checkpoints[-1]), trigger=trigger(rig.tags), parent_id="ctx_parent",
                       approvals=approvals)


def test_sub_step_needing_approval_parks_with_cursor_and_resume_continues_without_replay():
    rig, out = _park()
    ask_id = sub_id("step_1", 0, 1)
    assert out.state == "awaiting_approval" and out.pending_action_ids == (ask_id,)
    cp = rig.tasks.checkpoints[-1]
    assert cp["pending_approvals"] == [ask_id] and cp["next_step_semantic"] == f"await_approval:{ask_id}"
    assert cp["composite_cursor"]["next"] == 1 and [r["status"] for r in cp["composite_cursor"]["rows"]] == ["ok"]
    assert [t for _, t in rig.tasks.executions] == ["fs.read"] and rig.tasks.park_calls
    rig.tasks.grant("appr_1", ask_id)
    done = _resume(rig, {ask_id: "appr_1"})
    assert (done.state, done.text) == ("completed", "全部完成")
    assert [t for _, t in rig.tasks.executions] == ["fs.read", "fs.ask"]  # fs.read 沒有重播
    assert rig.tasks.approvals["appr_1"].consumed_by == ask_id
    assert rig.tasks.actions[ask_id]["params"]["content"] == "內容:/a.md"  # 續跑時前步輸出仍可引用
    assert _tool_texts(rig)[0] == "fs.ask:/a.md"
    assert rig.tasks.checkpoints[-1].get("composite_cursor") is None


def test_resume_without_approval_parks_again_keeping_the_cursor():
    rig, _ = _park()
    ask_id = sub_id("step_1", 0, 1)
    again = _resume(rig, {})
    assert again.state == "awaiting_approval" and again.pending_action_ids == (ask_id,)
    assert rig.tasks.checkpoints[-1]["composite_cursor"]["next"] == 1
    assert [t for _, t in rig.tasks.executions] == ["fs.read"]


def test_second_approval_step_parks_again_with_advanced_cursor():
    two = _notes(
        [READ_STEP, CompositeStep("fs.ask", {"path": "$.input.source", "content": "$.steps[0].output.text"}),
         CompositeStep("fs.ask", {"path": "$.input.title", "content": "$.steps[0].output.text"})],
        name="notes.twice", side="external_effect", cap="chatroom.post", approval="per_action",
    )
    rig, _ = _park([Round("發佈", (("notes.twice", {"source": "/a.md"}),)), Round("全部完成")], two)
    first, second = sub_id("step_1", 0, 1), sub_id("step_1", 0, 2)
    rig.tasks.grant("appr_1", first)
    mid = _resume(rig, {first: "appr_1"}, rounds=[Round("不該到這裡")])
    assert mid.state == "awaiting_approval" and mid.pending_action_ids == (second,)
    cp = rig.tasks.checkpoints[-1]
    assert cp["composite_cursor"]["next"] == 2 and [r["status"] for r in cp["composite_cursor"]["rows"]] == ["ok", "ok"]
    rig.tasks.grant("appr_2", second)
    done = _resume(rig, {second: "appr_2"}, epoch=3)
    assert done.state == "completed"
    assert [t for _, t in rig.tasks.executions] == ["fs.read", "fs.ask", "fs.ask"]


def test_denied_or_invalid_approval_ends_run_like_any_pending_tool():
    rig, _ = _park()
    ask_id = sub_id("step_1", 0, 1)
    rig.tasks.grant("appr_bad", ask_id, state="consumed")
    out = _resume(rig, {ask_id: "appr_bad"})
    assert (out.state, out.reason_code) == ("failed", "approval_invalid")
    assert [t for _, t in rig.tasks.executions] == ["fs.read"]


def test_resume_after_capability_revoked_fails_without_consuming_approval():
    rig, _ = _park()
    ask_id = sub_id("step_1", 0, 1)
    rig.tasks.grant("appr_1", ask_id)
    narrowed = projector_for(ToolRegistry.build([_entry("fs.read"), _entry("fs.write", side="reversible_local", cap="memory.write", schema=_WRITE_IN)]))
    loop = _crash(rig, 2, projector=narrowed)
    out = loop.resume(RUN, dict(rig.tasks.checkpoints[-1]), trigger=trigger(rig.tags), parent_id="ctx_parent", approvals={ask_id: "appr_1"})
    assert (out.state, out.reason_code) == ("failed", "capability_revoked")
    assert rig.tasks.approvals["appr_1"].consumed_by is None


def test_pending_step_that_cannot_rebuild_its_params_ends_run_without_waking_model():
    """park 後回合範圍暫存已清（例如重啟）：待決步驟引用的前步輸出不可得 ⇒ 不回視圖喚醒模型，Run 結束、核准不消耗。"""
    rig, _ = _park()
    ask_id = sub_id("step_1", 0, 1)
    rig.tasks.grant("appr_1", ask_id)
    rig.tasks.contents.clear()
    out = _resume(rig, {ask_id: "appr_1"}, rounds=[Round("不該到這裡")])
    assert (out.state, out.reason_code) == ("failed", "intent_rejected")
    assert rig.last_runtime.requests == []  # 沒有白花一次模型呼叫
    assert rig.tasks.run_status == [RunStatus.FAILED]
    assert rig.tasks.approvals["appr_1"].consumed_by is None and [t for _, t in rig.tasks.executions] == ["fs.read"]


def test_pending_step_blocked_before_execute_ends_run_instead_of_returning_a_blocked_view():
    rig, _ = _park()
    ask_id = sub_id("step_1", 0, 1)
    rig.tasks.grant("appr_1", ask_id)
    rig.tasks.bump_version("fs.ask")  # 建立層擋下（tool_version_changed）：未抵達 execute
    out = _resume(rig, {ask_id: "appr_1"}, rounds=[Round("不該到這裡")])
    assert (out.state, out.reason_code) == ("failed", "tool_version_changed")
    assert rig.last_runtime.requests == [] and rig.tasks.approvals["appr_1"].consumed_by is None


# ---- 崩潰後從 cursor 續跑 ----

class _Crash(BaseException):
    """模擬行程在 executor 執行途中死亡（不是 Exception，不會被轉成 UNKNOWN）。"""


def test_crash_between_steps_resumes_from_cursor_without_replaying_successes():
    three = _notes(
        [READ_STEP, WRITE_STEP, CompositeStep("fs.write", {"path": "$.input.title", "content": "$.steps[0].output.text"})],
        name="notes.three",
    )
    state = {"crash": True}

    def write(args):
        if args["path"] == "筆記" and state["crash"]:
            raise _Crash
        return f"寫入:{args['path']}"

    rig = _rig([Round("組合", (("notes.three", {"source": "/a.md"}),)), Round("不會用到")], three, behaviors={"fs.write": write})
    with pytest.raises(_Crash):
        _run(rig)
    cp = rig.tasks.checkpoints[-1]
    assert cp["next_step_semantic"] == "composite" and cp["pending_approvals"] == []
    assert cp["composite_cursor"]["next"] == 2
    history = [m for m in rig.child() if m.role in ("assistant", "tool")]
    assert history[-1].role == "assistant" and history[-1].tool_calls  # 與 park 同形：呼叫已在 history，結果待補
    state["crash"] = False
    out = _resume(rig, None, checkpoint=cp, rounds=[Round("收尾")])
    assert (out.state, out.text) == ("completed", "收尾")
    done = [(a, t) for a, t in rig.tasks.executions]
    assert [t for _, t in done] == ["fs.read", "fs.write", "fs.write"]
    assert len({a for a, _ in done}) == 3  # 前兩步沒有重播，第三步補跑
    assert _tool_texts(rig)[0] == "寫入:筆記"


def test_malformed_cursor_fails_closed_without_replay():
    rig, _ = _park()
    cp = dict(rig.tasks.checkpoints[-1])
    cp["composite_cursor"] = {"ordinal": 0, "tool": "notes.publish", "next": 5, "rows": []}
    out = _resume(rig, {}, checkpoint=cp)
    assert (out.state, out.reason_code) == ("failed", "checkpoint_incomplete")
    assert [t for _, t in rig.tasks.executions] == ["fs.read"]



# ---- 審查修正：checkpoint 一致性與 cursor 驗證（fail-closed） ----

def _tampered(mutate):
    rig, _ = _park()
    cp = dict(rig.tasks.checkpoints[-1])
    cursor = {**cp["composite_cursor"], "rows": [dict(r) for r in cp["composite_cursor"]["rows"]]}
    cp["composite_cursor"] = cursor
    mutate(cp, cursor)
    ask_id = sub_id("step_1", 0, 1)
    rig.tasks.grant("appr_1", ask_id)
    out = _resume(rig, {ask_id: "appr_1"}, checkpoint=cp, rounds=[Round("不該到這裡")])
    return rig, out


def _assert_failed_closed(rig, out, reason):
    assert (out.state, out.reason_code) == ("failed", reason)
    assert rig.last_runtime.requests == [] and rig.tasks.approvals["appr_1"].consumed_by is None
    assert [t for _, t in rig.tasks.executions] == ["fs.read"]  # 沒有執行任何新步驟、核准不消耗


def test_pending_sub_action_without_cursor_fails_closed_instead_of_deferring():
    def drop_cursor(cp, _cursor):
        del cp["composite_cursor"]

    rig, out = _tampered(drop_cursor)
    _assert_failed_closed(rig, out, "checkpoint_incomplete")
    assert not any("deferred_by_approval_wait" in m.content for m in rig.child() if m.role == "tool")


def test_pending_id_matching_no_call_fails_closed():
    def stray(cp, _cursor):
        cp["pending_approvals"] = [sub_id("step_1", 5, 0)]

    rig, out = _tampered(stray)
    _assert_failed_closed(rig, out, "checkpoint_incomplete")


def test_cursor_tool_must_match_the_call():
    rig, out = _tampered(lambda cp, cursor: cursor.update(tool="notes.prepare"))
    _assert_failed_closed(rig, out, "checkpoint_incomplete")


def test_cursor_rows_must_match_declared_steps():
    rig, out = _tampered(lambda cp, cursor: cursor["rows"][0].update(tool="fs.write"))
    _assert_failed_closed(rig, out, "checkpoint_incomplete")


def test_cursor_longer_than_declared_steps_fails_closed():
    def extend(cp, cursor):
        cursor["rows"] += [{"step": 1, "tool": "fs.ask", "status": "ok"}, {"step": 2, "tool": "fs.ask", "status": "ok"}]
        cursor["next"] = 3

    rig, out = _tampered(extend)
    _assert_failed_closed(rig, out, "checkpoint_incomplete")


@pytest.mark.parametrize("key", ["version", "schema_hash"])
def test_composite_declaration_changed_since_cursor_fails_closed(key):
    rig, out = _tampered(lambda cp, cursor: cursor.update({key: 99 if key == "version" else "0" * 64}))
    _assert_failed_closed(rig, out, "tool_version_changed")


# ---- 審查修正：非待決步驟與全新執行一致、預算 ----

def _crashed_prepare(**loop_over):
    class Crash(BaseException):
        pass

    state = {"crash": True}

    def write(args):
        if state["crash"]:
            raise Crash
        return "寫入"

    rig = _rig([Round("組合", (_composite(),)), Round("不會用到")], behaviors={"fs.write": write})
    with pytest.raises(Crash):
        _run(rig)
    state["crash"] = False
    cp = dict(rig.tasks.checkpoints[-1])
    assert cp["pending_approvals"] == [] and cp["composite_cursor"]["next"] == 1
    return rig, cp


def test_non_pending_step_blocked_on_resume_is_a_blocked_row_not_a_run_ending_halt():
    rig, cp = _crashed_prepare()
    rig.tasks.bump_version("fs.write")  # 建立層擋下：該步不是待核准步驟
    out = _resume(rig, None, checkpoint=cp, rounds=[Round("收尾")])
    assert (out.state, out.text) == ("completed", "收尾")  # 與全新執行一致：列成 blocked，Run 繼續
    view = next(m for m in rig.child() if m.role == "tool").content
    assert view.startswith("tool blocked")


def test_non_pending_step_counts_against_tool_call_budget_on_resume():
    rig, cp = _crashed_prepare()
    loop = _crash(rig, 2, rounds=[Round("收尾")], budget=LoopBudget(repeat_limit=99, max_tool_calls=1))
    out = loop.resume(RUN, cp, trigger=trigger(rig.tags), parent_id="ctx_parent")
    assert out.state == "completed"
    assert [t for _, t in rig.tasks.executions].count("fs.write") == 0  # 預算已滿：第二步 blocked(deferred_budget)，不執行
    assert next(m for m in rig.child() if m.role == "tool").content == "tool blocked: deferred_budget"
