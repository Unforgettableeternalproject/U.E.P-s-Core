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
from uep.fakes.composite_blobs import InMemoryCompositeBlobStore
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


def test_composite_resume_rejection_counts_towards_rejected_limit():
    """composite 待決子步驟被否決（結果 rejected）：resume 時累計 rejected_streak，達 rejected_limit 結束 Run、不喚醒模型。"""
    from uep.agent.core import IntentRecord, IntentResult

    rig, _ = _park()
    cp = dict(rig.tasks.checkpoints[-1])
    cp["budget_counters"] = {**cp["budget_counters"], "rejected": 2}
    loop = _crash(rig, 2, rounds=[Round("不該到這裡")])
    denied = IntentResult(IntentRecord(0, "notes.publish", "act_x", "rejected", "approval_denied"),
                          loop._core._gateway.result_view("rejected", reason_code="approval_denied"))
    loop._execute_composite = lambda *a, **k: denied
    out = loop.resume(RUN, cp, trigger=trigger(rig.tags), parent_id="ctx_parent", approvals={})
    assert (out.state, out.reason_code) == ("failed", "no_progress") and rig.last_runtime.requests == []


# ---- D110：composite 引用 text 的持久 blob ----

_TEXT = "內容:/a.md"


def _blob_ref(rig, index: int = 0) -> dict:
    row = rig.tasks.checkpoints[-1]["composite_cursor"]["rows"][index]
    return {"ref": row["text_blob_ref"], "hash": row["text_blob_hash"], "size": row["text_blob_size"]}


def _park_with_blobs(store, **kwargs):
    rig, out = _park(composite_blobs=store, **kwargs)
    assert out.state == "awaiting_approval"
    rig.tasks.contents.clear()  # 重啟：回合範圍暫存已清
    ask_id = sub_id("step_1", 0, 1)
    rig.tasks.grant("appr_1", ask_id)
    return rig, ask_id


def _assert_blob_fail_closed(rig, out):
    assert (out.state, out.reason_code) == ("failed", "intent_rejected")
    assert rig.last_runtime.requests == []  # 不喚醒模型
    assert rig.tasks.approvals["appr_1"].consumed_by is None  # 核准不消耗
    assert [t for _, t in rig.tasks.executions] == ["fs.read"]


def test_blob_keeps_referenced_text_so_pending_step_resumes_after_restart():
    store = InMemoryCompositeBlobStore()
    rig, out = _park(composite_blobs=store)
    ref = _blob_ref(rig)
    assert ref["size"] == len(_TEXT.encode()) and len(ref["ref"]) <= 64 and len(ref["hash"]) <= 64
    assert _TEXT not in repr(rig.tasks.checkpoints[-1])  # D33：checkpoint 不放原文
    assert store.blobs[ref["ref"]] == _TEXT.encode() and out.state == "awaiting_approval"  # park 不是終態，blob 還在
    rig.tasks.contents.clear()
    ask_id = sub_id("step_1", 0, 1)
    rig.tasks.grant("appr_1", ask_id)
    done = _resume(rig, {ask_id: "appr_1"})
    assert (done.state, done.text) == ("completed", "全部完成")
    assert [t for _, t in rig.tasks.executions] == ["fs.read", "fs.ask"]
    assert rig.tasks.actions[ask_id]["params"]["content"] == _TEXT  # 重啟後仍重建出核准當時的參數
    assert rig.tasks.approvals["appr_1"].consumed_by == ask_id
    assert "cblob" not in "".join(_tool_texts(rig))  # blob 引用不給模型看
    assert store.blobs == {}  # 終態即刪


def test_lost_blob_keeps_failing_closed():
    store = InMemoryCompositeBlobStore()
    rig, ask_id = _park_with_blobs(store)
    store.lose(_blob_ref(rig)["ref"])
    _assert_blob_fail_closed(rig, _resume(rig, {ask_id: "appr_1"}, rounds=[Round("不該到這裡")]))


@pytest.mark.parametrize("tampered", [lambda n: b"X" * n, lambda n: b"X" * (n + 3)], ids=["same_size", "other_size"])
def test_tampered_blob_keeps_failing_closed_and_is_deleted(tampered):
    store = InMemoryCompositeBlobStore()
    rig, ask_id = _park_with_blobs(store)
    ref = _blob_ref(rig)
    store.tamper(ref["ref"], tampered(ref["size"]))
    _assert_blob_fail_closed(rig, _resume(rig, {ask_id: "appr_1"}, rounds=[Round("不該到這裡")]))
    assert store.blobs == {}  # 失敗終態也清掉


def test_blob_over_limit_at_resume_keeps_failing_closed():
    store = InMemoryCompositeBlobStore()
    rig, ask_id = _park_with_blobs(store)
    loop = _crash(rig, 2, rounds=[Round("不該到這裡")], composite_blob_max_bytes=_blob_ref(rig)["size"] - 1)
    out = loop.resume(RUN, dict(rig.tasks.checkpoints[-1]), trigger=trigger(rig.tags), parent_id="ctx_parent", approvals={ask_id: "appr_1"})
    _assert_blob_fail_closed(rig, out)


def test_text_over_limit_at_write_is_not_persisted_and_fails_closed_on_resume():
    store = InMemoryCompositeBlobStore()
    rig, ask_id = _park_with_blobs(store, composite_blob_max_bytes=4)
    assert "text_blob_ref" not in rig.tasks.checkpoints[-1]["composite_cursor"]["rows"][0] and store.blobs == {}
    _assert_blob_fail_closed(rig, _resume(rig, {ask_id: "appr_1"}, rounds=[Round("不該到這裡")]))


def test_put_failure_does_not_break_the_run_but_resume_fails_closed():
    store = InMemoryCompositeBlobStore(fail_put=True)
    rig, ask_id = _park_with_blobs(store)
    assert "text_blob_ref" not in rig.tasks.checkpoints[-1]["composite_cursor"]["rows"][0]
    _assert_blob_fail_closed(rig, _resume(rig, {ask_id: "appr_1"}, rounds=[Round("不該到這裡")]))


def test_without_store_nothing_is_persisted_and_behaviour_is_unchanged():
    rig, _ = _park()
    assert "text_blob_ref" not in rig.tasks.checkpoints[-1]["composite_cursor"]["rows"][0]
    rig.tasks.contents.clear()
    ask_id = sub_id("step_1", 0, 1)
    rig.tasks.grant("appr_1", ask_id)
    _assert_blob_fail_closed(rig, _resume(rig, {ask_id: "appr_1"}, rounds=[Round("不該到這裡")]))


def test_only_referenced_text_is_persisted():
    noref = _notes(
        [READ_STEP, CompositeStep("fs.ask", {"path": "$.input.source", "content": "$.input.title"})],
        name="notes.noref", side="external_effect", cap="chatroom.post", approval="per_action",
    )
    store = InMemoryCompositeBlobStore()
    rig, out = _park([Round("發佈", (("notes.noref", {"source": "/a.md"}),)), Round("完成")], noref, composite_blobs=store)
    assert out.state == "awaiting_approval" and store.blobs == {}
    assert "text_blob_ref" not in rig.tasks.checkpoints[-1]["composite_cursor"]["rows"][0]


def test_terminal_run_deletes_blobs_even_when_not_resumed_to_success():
    store = InMemoryCompositeBlobStore()
    rig, ask_id = _park_with_blobs(store)
    assert len(store.blobs) == 1
    rig.tasks.grant("appr_bad", ask_id, state="consumed")
    out = _resume(rig, {ask_id: "appr_bad"})
    assert (out.state, out.reason_code) == ("failed", "approval_invalid")
    assert store.blobs == {}


def test_blob_ref_of_another_run_is_rejected():
    store = InMemoryCompositeBlobStore()
    rig, ask_id = _park_with_blobs(store)
    foreign_ref, foreign_hash = store.put("run_other", _TEXT.encode())  # 內容與雜湊都對，但不屬於本 Run
    cp = {k: v for k, v in rig.tasks.checkpoints[-1].items()}
    cursor = {**cp["composite_cursor"], "rows": [dict(r) for r in cp["composite_cursor"]["rows"]]}
    cursor["rows"][0].update(text_blob_ref=foreign_ref, text_blob_hash=foreign_hash)
    cp["composite_cursor"] = cursor
    out = _resume(rig, {ask_id: "appr_1"}, checkpoint=cp, rounds=[Round("不該到這裡")])
    _assert_blob_fail_closed(rig, out)
    assert foreign_ref in store.blobs  # 別的 Run 的 blob 不受影響


def test_empty_text_is_persisted_and_resumes():
    store = InMemoryCompositeBlobStore()
    rig, out = _park(composite_blobs=store, behaviors={"fs.read": lambda a: ""})
    assert out.state == "awaiting_approval" and _blob_ref(rig)["size"] == 0
    rig.tasks.contents.clear()
    ask_id = sub_id("step_1", 0, 1)
    rig.tasks.grant("appr_1", ask_id)
    done = _resume(rig, {ask_id: "appr_1"})
    assert done.state == "completed" and rig.tasks.actions[ask_id]["params"]["content"] == ""


def test_run_that_never_wrote_a_blob_skips_delete_run():
    noref = _notes(
        [READ_STEP, CompositeStep("fs.ask", {"path": "$.input.source", "content": "$.input.title"})],
        name="notes.noref", side="external_effect", cap="chatroom.post", approval="per_action",
    )
    store = InMemoryCompositeBlobStore()
    rig, _ = _park([Round("發佈", (("notes.noref", {"source": "/a.md"}),)), Round("完成")], noref, composite_blobs=store)
    ask_id = sub_id("step_1", 0, 1)
    rig.tasks.grant("appr_1", ask_id)
    assert _resume(rig, {ask_id: "appr_1"}).state == "completed"
    assert store.delete_calls == 0


def test_resumed_run_with_blob_refs_in_cursor_still_deletes_at_terminal():
    store = InMemoryCompositeBlobStore()
    rig, ask_id = _park_with_blobs(store)  # 換 worker：新 loop 的 flag 由 cursor 的引用推回
    assert _resume(rig, {ask_id: "appr_1"}).state == "completed"
    assert store.delete_calls >= 1 and store.blobs == {}


def test_settle_deletes_blobs_after_failing_the_run():
    from types import SimpleNamespace

    from uep.echo.agent_wiring import LoopLauncher
    from uep.tasks.states import RunStatus

    store = InMemoryCompositeBlobStore()
    store.put("run_x", b"secret")
    calls = []
    tasks = SimpleNamespace(
        get_run=lambda rid: SimpleNamespace(worker_epoch=3, status=RunStatus.RUNNING.value),
        transition_run=lambda rid, status, worker_epoch: calls.append((rid, status)),
        transition_task=lambda *a, **k: None,
    )
    me = SimpleNamespace(_tasks=tasks, _composite_blobs=store)
    ctx = SimpleNamespace(run=SimpleNamespace(run_id="run_x", worker_epoch=3), task_id="task_x")
    LoopLauncher._settle(me, ctx, ok=False, reason="boom")
    assert calls == [("run_x", RunStatus.FAILED)] and store.blobs == {}
    # 清理失敗不影響終結
    store2 = InMemoryCompositeBlobStore()
    store2.delete_run = lambda rid: (_ for _ in ()).throw(RuntimeError("x"))  # type: ignore[method-assign]
    me2 = SimpleNamespace(_tasks=tasks, _composite_blobs=store2)
    LoopLauncher._settle(me2, ctx, ok=False, reason="boom")
    assert len(calls) == 2


def test_completed_composite_drops_blobs_even_if_run_later_parks_elsewhere():
    store = InMemoryCompositeBlobStore()
    rig = _rig(
        [Round("整理", (("notes.prepare", {"source": "/a.md"}),)), Round("發佈", (("fs.ask", {"path": "/a.md", "content": "x"}),)),
         Round("全部完成")],
        composite_blobs=store,
    )
    out = _run(rig)
    assert out.state == "awaiting_approval"
    assert store.delete_calls == 1 and store.blobs == {}  # composite 完成當下就刪，不等 Run 終態
    ask_id = stable_action_id(RUN, "step_2", 0)
    rig.tasks.grant("appr_1", ask_id)
    done = _resume(rig, {ask_id: "appr_1"})  # 換 worker：新 loop 沒有寫過 blob，終態不需再刪
    assert done.state == "completed" and store.blobs == {}


def test_failed_composite_also_drops_blobs():
    store = InMemoryCompositeBlobStore()
    rig = _rig(
        [Round("整理", (("notes.prepare", {"source": "/a.md"}),)), Round("完成")], composite_blobs=store,
        behaviors={"fs.write": lambda a: (_ for _ in ()).throw(RuntimeError("寫入失敗"))},
    )
    _run(rig)
    assert store.delete_calls == 1 and store.blobs == {}
