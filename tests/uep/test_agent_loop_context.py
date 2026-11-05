"""平行 context（切片 D1，§4.10、AL-8）：fork／持久化／merge-back 冪等／park 狀態標記；不需 DB。"""
from __future__ import annotations

import pytest

from uep.agent.loop_context import (
    SUMMARY_MAX_CHARS,
    ContextLost,
    ParentSnapshot,
    child_ref,
    fork_child,
    load_verified,
    mark_parked,
    merge_back,
    persist_child,
)
from uep.fakes.loop_context import InMemoryLoopContextStore
from uep.models.port import ChatMessage, history_hash

RUN = "run_ctx1"


def _parent(**over) -> ParentSnapshot:
    recent = (
        ChatMessage("user", "你好"),
        ChatMessage("assistant", "嗨"),
        ChatMessage("assistant", "", tool_calls=({"id": "c", "name": "deep_recall", "args": {}},)),  # 工具呼叫不進 child
        ChatMessage("tool", "秘密結果", tool_call_id="c"),
        ChatMessage("user", "幫我查貓"),
    )
    base = {"parent_id": "ctx_parent", "revision": 7, "summary": "先前聊過寵物", "recent": recent}
    base.update(over)
    return ParentSnapshot(**base)


def _fork(store, **over):
    kwargs = {
        "parent": _parent(), "system_text": "你是助理", "goal_code": "research", "trigger_text": "幫我整理貓的資料",
        "persona_version": 3, "scope_ref": "scope:private",
    }
    kwargs.update(over)
    return fork_child(store, RUN, **kwargs)


def test_fork_takes_only_allowed_parts_and_records_manifest():
    store = InMemoryLoopContextStore()
    child = _fork(store)
    assert child.ref == child_ref(RUN) == "loop:run_ctx1" and child.revision == 1
    roles = [m.role for m in child.messages]
    assert roles == ["system", "system", "user", "assistant", "user", "system", "user"]
    joined = "\n".join(m.content for m in child.messages)
    assert "秘密結果" not in joined and not any(m.tool_calls or m.role == "tool" for m in child.messages)
    assert child.messages[0].content == "你是助理" and "先前聊過寵物" in child.messages[1].content
    assert child.messages[-1] == ChatMessage("user", "幫我整理貓的資料")
    m = child.manifest
    assert (m.parent_context_id, m.parent_revision, m.persona_version, m.scope_ref) == ("ctx_parent", 7, 3, "scope:private")
    assert "recent:3" in m.taken and "trigger_text" in m.taken and "summary" in m.taken
    assert store.manifests[child.ref] == m  # 內容在 child session，manifest 只有引用與計數


def test_fork_recent_window_is_bounded_and_trigger_is_optional():
    store = InMemoryLoopContextStore()
    many = tuple(ChatMessage("user" if i % 2 == 0 else "assistant", f"m{i}") for i in range(20))
    child = _fork(store, parent=_parent(recent=many, summary=""), recent_limit=4, trigger_text="")
    texts = [m.content for m in child.messages]
    assert texts[1:5] == ["m16", "m17", "m18", "m19"]
    assert child.messages[-1].role == "system" and "trigger_text" not in child.manifest.taken
    assert not any("[parent 摘要]" in t for t in texts)


def test_parent_new_messages_after_fork_do_not_enter_child():
    store = InMemoryLoopContextStore()
    parent = _parent()
    child = _fork(store, parent=parent)
    # parent 之後多了訊息：child 的持久化內容不變（迴圈期間不自動合併）
    _ = ParentSnapshot("ctx_parent", 8, "x", (*parent.recent, ChatMessage("user", "閒聊")))
    loaded, rev = store.load_child(child.ref)  # type: ignore[misc]
    assert loaded == list(child.messages) and rev == 1


def test_persist_and_load_verified_roundtrip_bumps_revision():
    store = InMemoryLoopContextStore()
    child = _fork(store)
    grown = [*child.messages, ChatMessage("assistant", "好", tool_calls=({"id": "c1", "name": "t", "args": {"a": 1}},))]
    revision, digest = persist_child(store, child.ref, grown)
    assert revision == 2 and digest == history_hash(grown)
    messages, rev = load_verified(store, child.ref, digest)
    assert messages == grown and rev == 2


def test_lost_or_tampered_child_fails_closed():
    store = InMemoryLoopContextStore()
    child = _fork(store)
    _, digest = persist_child(store, child.ref, list(child.messages))
    store.tamper(child.ref)
    with pytest.raises(ContextLost):
        load_verified(store, child.ref, digest)
    store.lose(child.ref)
    with pytest.raises(ContextLost):
        load_verified(store, child.ref, digest)
    with pytest.raises(ContextLost):
        load_verified(store, "loop:never_existed", digest)


def test_merge_back_completed_is_idempotent_and_only_writes_delivered_result():
    store = InMemoryLoopContextStore()
    kwargs = {
        "run_id": RUN, "parent_id": "ctx_parent", "scope_ref": "scope:private", "state": "completed",
        "summary": "查到三筆資料\n並整理", "result": "最終答案", "trail_ref": "trail:run_ctx1",
    }
    assert merge_back(store, delivered=True, **kwargs) is True
    assert merge_back(store, delivered=True, **kwargs) is False  # 冪等：不重複寫
    entries = store.parent_entries["ctx_parent"]
    assert len(entries) == 1 and entries[0].key == f"loop_return:{RUN}"
    p = entries[0].payload
    assert p.result == "最終答案" and p.trail_ref == "trail:run_ctx1" and p.summary.startswith(f"loop:{RUN} 查到三筆資料 並整理")

    other = InMemoryLoopContextStore()
    merge_back(other, delivered=False, **{**kwargs, "run_id": "run_ctx2"})  # 未交付：只記意圖，不寫 result
    assert other.parent_entries["ctx_parent"][0].payload.result is None


def test_merge_back_summary_is_clipped():
    store = InMemoryLoopContextStore()
    merge_back(store, run_id=RUN, parent_id="p", scope_ref="s", state="completed", summary="長" * 5000)
    summary = store.parent_entries["p"][0].payload.summary
    assert len(summary) <= SUMMARY_MAX_CHARS + len(f"loop:{RUN} ")


@pytest.mark.parametrize("state", ["failed", "cancelled", "budget_exhausted"])
def test_merge_back_non_completed_only_status_note(state):
    store = InMemoryLoopContextStore()
    merge_back(
        store, run_id=RUN, parent_id="p", scope_ref="s", state=state, reason_code="why", summary="部分思緒不應外洩",
        result="半成品", delivered=True, trail_ref="trail:x",
    )
    p = store.parent_entries["p"][0].payload
    assert (p.state, p.reason_code) == (state, "why")
    assert p.summary is None and p.result is None and p.trail_ref is None


def test_park_status_mark_is_idempotent_per_state():
    store = InMemoryLoopContextStore()
    assert mark_parked(store, run_id=RUN, parent_id="p") is True
    assert mark_parked(store, run_id=RUN, parent_id="p") is False
    assert mark_parked(store, run_id=RUN, parent_id="p", state="resumed") is True
    assert [e.key for e in store.parent_entries["p"]] == [f"loop_status:{RUN}:awaiting_approval", f"loop_status:{RUN}:resumed"]
