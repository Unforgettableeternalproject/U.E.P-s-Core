"""D110：composite 引用 text 的持久 blob（需 UEP_TEST_DATABASE_URL；未設時 skip）。離線對照在 ``test_agent_loop_composite.py``。

涵蓋 ``DbCompositeBlobStore``（沿用 ``payload_blobs``，不新增表）：寫入／讀回／驗證、ref 與 hash 可進 checkpoint 的長度與字元集、
Run 終態刪除只動該 Run 的 composite blob、TTL、不可經它讀 journal 的 blob，以及對真實 TaskManager 的重啟續跑與 fail-closed。
"""
from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import SecretStr
from sqlalchemy import select, update

from tests.uep.loop_support import Round, ScriptedRuntime, _entry, projector_for
from tests.uep.test_agent_loop import trigger
from tests.uep.test_agent_loop_approval_db import OWNER, SCOPE, approval_row
from tests.uep.test_agent_loop_db import DbRig, _actions
from uep.adapters.tool import CompositeSpec, CompositeStep
from uep.agent.budget import LoopBudget
from uep.agent.loop import AgentLoop
from uep.events import journal
from uep.events.composite_blobs import REF_PREFIX, DbCompositeBlobStore
from uep.events.models import PayloadBlob
from uep.events.payload_hash import PayloadHasher
from uep.tasks.manager import check_safe_value, params_hash
from uep.tasks.states import RunStatus
from uep.tools.gateway import ToolGateway
from uep.tools.registry import ToolRegistry

pytestmark = pytest.mark.usefixtures("session_factory")

_KEY = SecretStr("composite-blob-test-key-0123456789")
_TEXT = "內容:/a.md"
_OUT_TEXT = {"type": "object", "properties": {"text": {"type": "string"}}}


def _store(session_factory, **kwargs) -> DbCompositeBlobStore:
    kwargs.setdefault("ttl_s", 3600)
    return DbCompositeBlobStore(session_factory, hasher=PayloadHasher(_KEY), **kwargs)


def _blob_rows(session_factory) -> list[PayloadBlob]:
    with session_factory() as s:
        return list(s.scalars(select(PayloadBlob).order_by(PayloadBlob.payload_ref)))


# ---- store 本身 ----

def test_put_get_verify_roundtrip_and_checkpoint_safe(session_factory):
    store = _store(session_factory)
    ref, digest = store.put("run_ABC", _TEXT.encode())
    assert ref.startswith(f"{REF_PREFIX}run_ABC:")
    check_safe_value({"text_blob_ref": ref, "text_blob_hash": digest}, depth=3)  # 可進 checkpoint（短碼、<= 64 字元）
    assert store.get(ref) == _TEXT.encode()
    assert store.verify(_TEXT.encode(), digest) and not store.verify(b"other", digest)
    (row,) = _blob_rows(session_factory)
    assert row.content_hash.startswith("hmac-sha256:") and row.content_hash.endswith(digest)  # 完整格式留在表內


def test_put_without_hash_key_is_refused_and_leaves_no_row(session_factory, monkeypatch):
    monkeypatch.setattr("uep.events.payload_hash.get_settings", lambda: type("S", (), {"payload_hash_key": None})())
    store = DbCompositeBlobStore(session_factory, ttl_s=3600)
    with pytest.raises(Exception):  # noqa: B017  PayloadHashKeyMissing
        store.put("run_ABC", b"x")
    assert _blob_rows(session_factory) == []


def test_verify_fails_closed_when_key_rotated(session_factory):
    store = _store(session_factory)
    _, digest = store.put("run_ABC", b"abc")
    rotated = DbCompositeBlobStore(session_factory, ttl_s=3600, hasher=PayloadHasher(SecretStr("another-key-0123456789abcdef")))
    assert store.verify(b"abc", digest) and not rotated.verify(b"abc", digest)


def test_delete_run_only_touches_that_runs_composite_blobs(session_factory):
    store = _store(session_factory)
    a1, _ = store.put("run_A", b"1")
    a2, _ = store.put("run_A", b"2")
    b1, _ = store.put("run_B", b"3")
    with session_factory.begin() as s:  # journal 的 blob 不得被誤刪
        journal_ref, _ = journal.put_blob(s, b"journal", PayloadHasher(_KEY))
    assert store.delete_run("run_A") == 2 and store.delete_run("run_A") == 0  # 冪等
    assert store.get(a1) is None and store.get(a2) is None and store.get(b1) == b"3"
    with session_factory() as s:
        assert journal.read_blob(s, journal_ref) == b"journal"
        gone = s.get(PayloadBlob, a1)
        assert gone.content is None and gone.deleted_at is not None and gone.content_hash  # 保留雜湊與刪除時間


def test_run_id_underscore_is_not_a_like_wildcard(session_factory):
    store = _store(session_factory)
    other, _ = store.put("runXA", b"keep")  # 'run_A' 的 '_' 若被當萬用字元會誤刪 'runXA'
    store.put("run_A", b"gone")
    store.delete_run("run_A")
    assert store.get(other) == b"keep"


def test_get_refuses_non_composite_refs(session_factory):
    store = _store(session_factory)
    with session_factory.begin() as s:
        journal_ref, _ = journal.put_blob(s, b"journal", PayloadHasher(_KEY))
    assert store.get(journal_ref) is None


def test_ttl_expired_blob_reads_as_missing_and_is_purged_on_next_put(session_factory):
    now = [datetime.now(UTC)]
    store = _store(session_factory, ttl_s=60, clock=lambda: now[0])
    ref, _ = store.put("run_A", b"old")
    now[0] += timedelta(seconds=61)
    assert store.get(ref) is None  # 讀取時即視同不存在
    store.put("run_B", b"new")  # 寫入順手清除已過期者
    with session_factory() as s:
        assert s.get(PayloadBlob, ref).content is None


def test_ttl_boundary_keeps_fresh_blob(session_factory):
    now = [datetime.now(UTC)]
    store = _store(session_factory, ttl_s=60, clock=lambda: now[0])
    ref, _ = store.put("run_A", b"fresh")
    now[0] += timedelta(seconds=30)
    assert store.get(ref) == b"fresh"


# ---- 對真實 TaskManager 的重啟續跑 ----

def _text_registry() -> ToolRegistry:
    read = dataclasses.replace(_entry("read.lone", timeout_s=None), output_schema=_OUT_TEXT)
    ask = _entry("post.ask", approval="per_action", side="external_effect", capability="chatroom.post")
    publish = dataclasses.replace(
        ask, name="post.pub",
        composite=CompositeSpec(steps=(
            CompositeStep("read.lone", {"path": "$.input.path"}),
            CompositeStep("post.ask", {"path": "$.steps[0].output.text"}),
        )),
        output_schema=_OUT_TEXT,
    )
    return ToolRegistry.build([read, ask, publish])


class _Live:
    """回合範圍結果來源的替身：只回 ``read.lone`` 子步驟的文字；``alive=False`` 模擬重啟後暫存已清。"""

    def __init__(self) -> None:
        self.rig = None
        self.alive = True

    def __call__(self, action_id: str) -> str | None:
        if not self.alive or self.rig is None:
            return None
        return _TEXT if any(a.action_id == action_id and a.tool == "read.lone" for a in _actions(self.rig)) else None


def _parked(session_factory, store):
    rig = DbRig(
        session_factory, [Round("發佈", (("post.pub", {"path": "/a.md"}),)), Round("完成")], budget=LoopBudget(repeat_limit=99),
        registry=_text_registry(), composite_blobs=store,
    )
    live = _Live()
    live.rig = rig
    rig.loop_kwargs["content_lookup"] = live
    out = rig.go()
    assert out.state == "awaiting_approval"
    return rig, live


def _restart_loop(rig, store):
    plan = rig.tm.resume_run(rig.run.run_id)
    loop = AgentLoop(
        runtime=ScriptedRuntime([Round("完成")]), tasks=rig.tm, gateway=ToolGateway(rig.registry), registry=rig.registry,
        store=rig.store, projector=projector_for(rig.registry), budget=rig.budget, worker_epoch=plan.run.worker_epoch,
        content_lookup=lambda _aid: None, composite_blobs=store,  # 重啟後回合範圍暫存是空的
    )
    return loop, plan.checkpoint or {}


def _grant(rig):
    ask = next(a for a in _actions(rig) if a.tool == "post.ask")
    approval = rig.tm.record_approval(
        principal=OWNER, action_hash=ask.params_hash, target=ask.tool, scope=SCOPE, expiry=datetime.now(UTC) + timedelta(hours=1),
    )
    return ask, approval


def _resume(rig, loop, checkpoint, ask, approval):
    return loop.resume(
        rig.run.run_id, checkpoint, trigger=trigger(), parent_id="ctx_parent", approvals={ask.action_id: approval.approval_id},
    )


def test_db_restart_resumes_from_blob_and_deletes_it_at_terminal(session_factory):
    store = _store(session_factory)
    rig, live = _parked(session_factory, store)
    checkpoint_before = rig.tm.get_run(rig.run.run_id).checkpoint
    stored = checkpoint_before["composite_cursor"]["rows"][0]  # JSONB 往返後仍在，且沒有原文
    assert stored["text_blob_ref"].startswith(REF_PREFIX) and stored["text_blob_size"] == len(_TEXT.encode())
    assert _TEXT not in str(checkpoint_before)
    assert len([b for b in _blob_rows(session_factory) if b.content is not None]) == 1  # park 不是終態，blob 仍在
    ask, approval = _grant(rig)
    assert ask.params_hash == params_hash("post.ask", 1, {"path": _TEXT})  # 核准綁的是含原文的參數
    live.alive = False
    loop, checkpoint = _restart_loop(rig, store)
    done = _resume(rig, loop, checkpoint, ask, approval)
    assert (done.state, done.text) == ("completed", "完成")
    assert approval_row(rig, approval.approval_id).consumed_by_action_id == ask.action_id
    assert rig.tm.get_run(rig.run.run_id).status == RunStatus.SUCCEEDED.value
    assert all(b.content is None and b.deleted_at is not None for b in _blob_rows(session_factory))  # 終態即刪


@pytest.mark.parametrize("damage", ["lost", "tampered"])
def test_db_lost_or_tampered_blob_fails_closed_without_consuming_approval(session_factory, damage):
    store = _store(session_factory)
    rig, live = _parked(session_factory, store)
    ref = rig.tm.get_run(rig.run.run_id).checkpoint["composite_cursor"]["rows"][0]["text_blob_ref"]
    with session_factory.begin() as s:
        values = {"content": None, "deleted_at": datetime.now(UTC)} if damage == "lost" else {"content": b"X" * len(_TEXT.encode())}
        s.execute(update(PayloadBlob).where(PayloadBlob.payload_ref == ref).values(**values))
    ask, approval = _grant(rig)
    live.alive = False
    loop, checkpoint = _restart_loop(rig, store)
    done = _resume(rig, loop, checkpoint, ask, approval)
    assert (done.state, done.reason_code) == ("failed", "intent_rejected")
    assert approval_row(rig, approval.approval_id).consumed_by_action_id is None
    assert rig.tm.get_run(rig.run.run_id).status == RunStatus.FAILED.value


def test_db_expired_blob_fails_closed(session_factory):
    now = [datetime.now(UTC)]
    store = _store(session_factory, ttl_s=60, clock=lambda: now[0])
    rig, live = _parked(session_factory, store)
    ask, approval = _grant(rig)
    live.alive = False
    now[0] += timedelta(seconds=61)
    loop, checkpoint = _restart_loop(rig, store)
    done = _resume(rig, loop, checkpoint, ask, approval)
    assert (done.state, done.reason_code) == ("failed", "intent_rejected")
    assert approval_row(rig, approval.approval_id).consumed_by_action_id is None
