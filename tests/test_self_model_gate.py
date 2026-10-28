"""
M6 Self Model 專用 Gate action（process_self_model_patch）行為測試。

只用確定性 embedding 與假 sink，不載入真模型。
"""

from __future__ import annotations

import pytest

from echo_memory.affect.concept_inhibitor import ConceptInhibitor
from echo_memory.affect.self_model_gate import is_self_model_concept
from echo_memory.config import get_config
from echo_memory.engine import MemoryEngine
from echo_memory.schemas.evidence import EvidenceInput
from tests.mock_core import FakeCore


@pytest.fixture
def env(tmp_path):
    engine = MemoryEngine(storage_path=tmp_path / "data", namespace_id="ns1")
    engine.ensure_initialized()
    gate = ConceptInhibitor(semantic=engine.semantic, llm_fn=None)
    engine.set_concept_inhibitor(gate)
    return engine, gate


def _agent_concept(engine, gate, *, support_source="agent_only", subject="agent"):
    ep = engine.store_episode(
        "", "我回答前會先確認需求", "s1",
        evidence=[EvidenceInput(source_type="agent", content="我回答前會先確認需求", speaker_id="agent")],
    )
    cid, tx = gate.process_extractor_patch(
        noun="回答習慣", desc="回答前先確認需求", episode_id=ep, evidence_kind="distillation",
        triggered_by="dream", source_episode_ids=[ep], subject=subject,
        support_source=support_source,
    )
    assert cid is not None
    return cid, list(engine.episodic.get(ep).evidence_ids)


def _user_unit(engine) -> list[str]:
    ep = engine.store_episode(
        "你回答前常先問我需求", "", "s1",
        evidence=[EvidenceInput(
            source_type="user", content="你回答前常先問我需求", speaker_id="u1",
            attrs={"channel_kind": "dm", "audience_size": 1},
        )],
    )
    return list(engine.episodic.get(ep).evidence_ids)


def test_agent_self_report_is_tagged_unverified(env):
    engine, gate = env
    cid, units = _agent_concept(engine, gate)
    ok, tx = gate.process_self_model_patch(cid, units)
    assert ok and tx.status == "committed"
    concept = engine.semantic.get(cid)
    assert is_self_model_concept(concept)
    mark = concept.metadata["self_model"]
    assert mark["verification"] == "unverified" and mark["evidence_refs"] == units
    assert tx.patches[0].action == "self_model"


def test_agent_only_cannot_be_verified(env):
    engine, gate = env
    cid, units = _agent_concept(engine, gate)
    ok, tx = gate.process_self_model_patch(cid, units, verification="verified")
    assert not ok and "不可為 verified" in (tx.patches[0].rejection_reason or "")
    assert "self_model" not in engine.semantic.get(cid).metadata


def test_user_attested_evidence_can_be_verified(env):
    engine, gate = env
    cid, _ = _agent_concept(engine, gate, support_source="user")
    ok, _ = gate.process_self_model_patch(cid, _user_unit(engine), verification="verified")
    assert ok
    assert engine.semantic.get(cid).metadata["self_model"]["verification"] == "verified"


@pytest.mark.parametrize("refs", [[], ["no_such_unit"]])
def test_requires_real_evidence_refs(env, refs):
    engine, gate = env
    cid, _ = _agent_concept(engine, gate)
    ok, tx = gate.process_self_model_patch(cid, refs)
    assert not ok and tx.status == "failed"
    assert "self_model" not in engine.semantic.get(cid).metadata


def test_only_agent_subject_concepts(env):
    engine, gate = env
    cid, units = _agent_concept(engine, gate, support_source="user", subject="user")
    ok, tx = gate.process_self_model_patch(cid, units)
    assert not ok and "subject=agent" in (tx.patches[0].rejection_reason or "")


def test_rollback_restores_previous_state(env):
    engine, gate = env
    cid, units = _agent_concept(engine, gate)
    ok, tx = gate.process_self_model_patch(cid, units)
    assert ok
    gate._rollback_patch(tx.patches[0])
    assert "self_model" not in engine.semantic.get(cid).metadata


def test_tagging_emits_self_observation_event(env, monkeypatch):
    monkeypatch.setattr(get_config(), "self_model_events_enabled", True)
    engine, gate = env
    core = FakeCore()
    engine.set_candidate_sink(core)
    cid, units = _agent_concept(engine, gate)
    ok, _ = gate.process_self_model_patch(cid, units)
    assert ok
    engine.event_emitter.drain()
    assert [e.event_type for e in core.events] == ["memory.self_observation_proposed"]
    assert set(core.events[0].payload["evidence_refs"]) == set(units)
    assert core.events[0].payload["derived_from"] == [f"concept:{cid}"]
