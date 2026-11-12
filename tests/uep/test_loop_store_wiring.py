"""``SessionLoopContextStore``：以 echo_thought_core SessionStore 持久化 child／parent；重建 store 後仍可 resume（§17.2）。不需 DB。"""
from __future__ import annotations

import pytest

from tests.uep.loop_support import Round
from tests.uep.test_agent_loop import RUN, _crash, _park_checkpoint, _park_rig, trigger
from uep.agent.loop_context import (
    ContextLost,
    ForkManifest,
    MergeBack,
    child_ref,
    load_verified,
    mark_parked,
    merge_back,
)
from uep.echo.loop_store_wiring import SessionLoopContextStore
from uep.models.port import ChatMessage, history_hash

MANIFEST = ForkManifest("ctx_parent", 0, 1, "scope:private", ("recent:2",))
MSGS = [ChatMessage("system", "你是助理"), ChatMessage("user", "幫我查貓，中文不跳脫")]


def test_child_roundtrip_survives_new_store_instance(tmp_path):
    ref = child_ref(RUN)  # 含 ":"，檔名需轉換
    store = SessionLoopContextStore(tmp_path)
    assert store.create_child(ref, MSGS, MANIFEST) == 1
    assert store.save_child(ref, [*MSGS, ChatMessage("assistant", "好")]) == 2
    reopened = SessionLoopContextStore(tmp_path)  # 模擬行程重啟
    messages, revision = reopened.load_child(ref)
    assert revision == 2 and messages[-1].content == "好" and messages[1].content == MSGS[1].content
    digest = history_hash(messages)
    assert load_verified(reopened, ref, digest)[1] == 2
    with pytest.raises(ContextLost):
        load_verified(reopened, ref, "bad")


def test_missing_or_corrupt_child_is_lost(tmp_path):
    store = SessionLoopContextStore(tmp_path)
    assert store.load_child("loop:nope") is None
    store.create_child("loop:x", MSGS, MANIFEST)
    (tmp_path / "sessions" / "child__loop_x.json").write_text("{broken", encoding="utf-8")
    assert store.load_child("loop:x") is None
    with pytest.raises(ContextLost):
        load_verified(store, "loop:x", "h")


def test_parent_writes_are_idempotent_and_persistent(tmp_path):
    store = SessionLoopContextStore(tmp_path)
    assert merge_back(store, run_id=RUN, parent_id="ctx_parent", scope_ref="s", state="completed", summary="完成", result="答案", delivered=True)
    assert not merge_back(store, run_id=RUN, parent_id="ctx_parent", scope_ref="s", state="completed", summary="完成")
    assert mark_parked(store, run_id=RUN, parent_id="ctx_parent")
    rows = SessionLoopContextStore(tmp_path).parent_entries("ctx_parent")
    assert [r["key"] for r in rows] == [f"loop_return:{RUN}", f"loop_status:{RUN}:awaiting_approval"]
    assert rows[0]["payload"]["result"] == "答案"
    assert not store.merge_into_parent(f"loop_return:{RUN}", "ctx_parent", MergeBack(RUN, "s", "completed"))


def test_discard_run_removes_child_and_parent_entries_only_for_that_run(tmp_path):
    store = SessionLoopContextStore(tmp_path)
    store.create_child(child_ref(RUN), MSGS, MANIFEST)
    store.create_child(child_ref("run_other"), MSGS, MANIFEST)
    mark_parked(store, run_id=RUN, parent_id="p")
    mark_parked(store, run_id="run_other", parent_id="p")
    store.discard_run(RUN)
    assert store.load_child(child_ref(RUN)) is None and store.load_child(child_ref("run_other")) is not None
    assert [r["key"] for r in store.parent_entries("p")] == ["loop_status:run_other:awaiting_approval"]


def test_park_then_resume_after_store_rebuilt(tmp_path):
    """真迴圈：park 後丟棄 store 物件（行程重啟），以同目錄新 store 續跑到完成。"""
    rig = _park_rig()
    rig.store = SessionLoopContextStore(tmp_path)
    rig.loop = rig.new_loop()
    assert rig.run().state == "awaiting_approval"
    checkpoint = _park_checkpoint(rig)
    rig.store = SessionLoopContextStore(tmp_path)  # 全新實例，只剩磁碟上的檔案
    out = _crash(rig, 2, rounds=[Round("全部完成")]).resume(RUN, checkpoint, trigger=trigger(), parent_id="ctx_parent")
    assert out.state == "awaiting_approval" and out.reason_code == "approval_required"  # child 重建且 hash 通過（未 context_lost）
    (tmp_path / "sessions" / "child__loop_run_loop1.json").unlink()
    out = _crash(rig, 3).resume(RUN, checkpoint, trigger=trigger(), parent_id="ctx_parent")
    assert out.state == "failed" and out.reason_code == "context_lost"
