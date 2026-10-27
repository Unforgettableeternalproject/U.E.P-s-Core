"""Cycle 2 稽核區域 C：ContextSnapshot、RuntimeEvent、Assembly（T-24，項目 8、9）。

模型測試不需 DB；存取相容測試需 PostgreSQL。對應探針 P11、P12。
"""
from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from tests.uep.factories import make_snapshot
from tests.uep.test_assembly import kwargs
from uep.adapters.runtime import RuntimeEvent
from uep.assembly import AssemblyError, assemble_core
from uep.config import get_settings
from uep.context.models import load_snapshot, save_snapshot
from uep.output.delivery import DeliveryStatus

PRIVATE = "scope_private_owner"
PUBLIC = "scope_public"


def _entry(state: str, **over):
    data = {
        "turn_id": "turn_1", "role": "assistant", "generated_text_ref": "blob_1", "scope": PRIVATE,
        "delivery_state": state, "delivered_range": None,
    }
    data.update(over)
    return data


# ---- scope（X-1） ----

def test_public_snapshot_rejects_private_commitment():
    with pytest.raises(ValidationError, match="scope"):
        make_snapshot(audience_scope=PUBLIC, commitments=[
            {"commitment_id": "c1", "text_ref": "blob_private_note", "scope": PRIVATE}
        ])
    ok = make_snapshot(audience_scope=PUBLIC, commitments=[{"commitment_id": "c1", "scope": PUBLIC}])
    assert ok.commitments[0]["scope"] == PUBLIC


def test_commitment_requires_scope():
    with pytest.raises(ValueError):
        make_snapshot(commitments=[{"commitment_id": "c1", "kind": "reminder"}])


def test_public_snapshot_rejects_private_task_ref():
    with pytest.raises(ValidationError, match="scope"):
        make_snapshot(audience_scope=PUBLIC, task_refs=[{"task_id": "task_PRIVATE", "scope": PRIVATE}])
    ok = make_snapshot(audience_scope=PRIVATE, task_refs=[{"task_id": "task_1", "scope": PRIVATE}])
    assert ok.task_refs[0].task_id == "task_1"


def test_history_summary_needs_scope_and_must_be_admitted():
    with pytest.raises(ValueError):
        make_snapshot(history_window={"summary_ref": "blob_private_summary", "summary_source_revision": 9})
    with pytest.raises(ValidationError, match="scope"):
        make_snapshot(audience_scope=PUBLIC, history_window={
            "summary_ref": "blob_private_summary", "summary_scope": PRIVATE, "summary_source_revision": 9,
        })
    ok = make_snapshot(history_window={"summary_ref": "blob_s", "summary_scope": PRIVATE})
    assert ok.history_window.summary_scope == PRIVATE


def test_excluded_summary_keys_must_be_reason_codes():
    with pytest.raises(ValueError):
        make_snapshot(excluded_summary={"private text leaked here": 3})
    assert make_snapshot(excluded_summary={"scope": 3, "acl_denied": 1}).excluded_summary["scope"] == 3


# ---- 深度凍結 ----

def test_snapshot_containers_are_deeply_frozen():
    snap = make_snapshot(
        capabilities={"tools": ["read"], "nested": {"a": [1, 2]}},
        commitments=[{"commitment_id": "c1", "scope": PRIVATE}],
        history_entries=[_entry("cancelled", delivered_range={"to_chunk": 3})],
    )
    with pytest.raises(AttributeError):
        snap.capabilities["tools"].append("shell")
    with pytest.raises(TypeError):
        snap.capabilities["new"] = 1
    with pytest.raises(TypeError):
        snap.capabilities["nested"]["a"] = 0
    with pytest.raises(TypeError):
        snap.commitments[0]["scope"] = PUBLIC
    with pytest.raises(TypeError):
        snap.data_egress["providers"] = []
    with pytest.raises(TypeError):
        snap.token_budget["max"] = 1
    with pytest.raises(TypeError):
        snap.history_entries[0].delivered_range["complete"] = True
    assert snap.capabilities["tools"] == ("read",)


# ---- delivery_state enum 與 X-4 ----

def test_delivery_state_is_enum_and_rejects_unknown_value():
    with pytest.raises(ValidationError):
        make_snapshot(history_entries=[_entry("whatever")])
    snap = make_snapshot(history_entries=[_entry("rendered_played")])
    assert snap.history_entries[0].delivery_state is DeliveryStatus.RENDERED_PLAYED


@pytest.mark.parametrize("state", ["cancelled", "unknown", "rejected", "suppressed"])
def test_incomplete_states_cannot_claim_complete_speech(state):
    with pytest.raises(ValueError):
        make_snapshot(history_entries=[_entry(state, delivered_range={"complete": True})])
    assert make_snapshot(history_entries=[_entry(state, delivered_range={"to_chunk": 2})])


# ---- DB 存取相容 ----

def test_snapshot_db_roundtrip_with_new_fields(session_factory):
    snap = make_snapshot(
        commitments=[{"commitment_id": "c1", "scope": PRIVATE}],
        task_refs=[{"task_id": "task_1", "scope": PRIVATE}],
        history_window={"summary_ref": "blob_s", "summary_scope": PRIVATE, "summary_source_revision": 2},
        history_entries=[_entry("cancelled", delivered_range={"to_chunk": 2})],
        capabilities={"tools": ["read"]},
    )
    with session_factory.begin() as s:
        save_snapshot(s, snap)
    with session_factory() as s:
        loaded = load_snapshot(s, snap.snapshot_id)
    assert loaded == snap and loaded.capabilities["tools"] == ("read",)


# ---- RuntimeEvent（P11） ----

@pytest.mark.parametrize("kind", ["seq", "attempt_id", "__class__", "nope"])
def test_runtime_event_kind_strict_enum(kind):
    with pytest.raises(ValueError):
        RuntimeEvent(attempt_id="a", seq=1, kind=kind)  # type: ignore[arg-type]


def test_tool_intent_requires_tool_and_args_complete():
    with pytest.raises(ValueError):
        RuntimeEvent(attempt_id="a", seq=1, kind="tool_intent", tool_intent={})
    with pytest.raises(ValueError):
        RuntimeEvent(attempt_id="a", seq=1, kind="tool_intent", tool_intent={"tool": "x"})
    with pytest.raises(ValueError):
        RuntimeEvent(attempt_id="a", seq=1, kind="tool_intent", tool_intent={"args_complete": True})
    with pytest.raises(ValueError):
        RuntimeEvent(attempt_id="a", seq=1, kind="tool_intent", tool_intent={"tool": "x", "args_complete": "yes"})
    assert RuntimeEvent(attempt_id="a", seq=1, kind="tool_intent", tool_intent={"tool": "x", "args_complete": False})


# ---- Assembly strict ----

def test_assembly_strict_refuses_missing_payload_hash_key(session_factory, tmp_path, monkeypatch):
    for value in (None, SecretStr("short")):
        monkeypatch.setattr(get_settings(), "payload_hash_key", value)
        with pytest.raises(AssemblyError, match="UEP_PAYLOAD_HASH_KEY"):
            assemble_core(session_factory, strict=True, **kwargs(session_factory, tmp_path))
        assert assemble_core(session_factory, **kwargs(session_factory, tmp_path))  # 預設仍只告警


def test_assembly_strict_passes_with_valid_key(session_factory, tmp_path):
    assert assemble_core(session_factory, strict=True, **kwargs(session_factory, tmp_path))
