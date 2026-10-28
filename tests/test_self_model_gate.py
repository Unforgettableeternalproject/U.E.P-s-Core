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


# ─── 審查修正：m2／m3／m4／m5／m6 ─────────────────────────────────────────────

def _agent_concept_named(engine, gate, name):
    ep = engine.store_episode(
        "", f"{name} 的自述", "s1",
        evidence=[EvidenceInput(source_type="agent", content=f"{name} 的自述", speaker_id="agent")],
    )
    cid, _ = gate.process_extractor_patch(
        noun=name, desc=f"{name} 描述", episode_id=ep, evidence_kind="distillation",
        triggered_by="dream", source_episode_ids=[ep], subject="agent", support_source="agent_only",
    )
    return cid, list(engine.episodic.get(ep).evidence_ids)


def test_is_self_model_concept_requires_refs_and_valid_verification():
    from echo_memory.schemas.concept import Concept

    def mk(mark):
        return Concept(name="n", description="d", subject="agent", metadata={"self_model": mark})

    good = {"evidence_refs": ["u1"], "verification": "unverified"}
    assert is_self_model_concept(mk(good))
    assert not is_self_model_concept(mk({"evidence_refs": [], "verification": "unverified"}))
    assert not is_self_model_concept(mk({"verification": "unverified"}))
    assert not is_self_model_concept(mk({"evidence_refs": ["u1"], "verification": "trusted"}))
    assert not is_self_model_concept(mk({}))
    assert not is_self_model_concept(
        Concept(name="n", description="d", subject="user", metadata={"self_model": good})
    )


def test_upsert_concept_strips_self_model_key(env):
    engine, _ = env
    forged = {"evidence_refs": ["x"], "verification": "verified"}
    cid = engine.semantic.upsert_concept(
        "偽造", "d", metadata={"self_model": forged, "keep": 1},
    )
    c = engine.semantic.get(cid)
    assert "self_model" not in c.metadata and c.metadata["keep"] == 1
    engine.semantic.upsert_concept("偽造", "d2", metadata={"self_model": forged})
    assert "self_model" not in engine.semantic.get(cid).metadata


def test_description_change_drops_verified_to_unverified(env):
    from echo_memory.schemas.self_model import effective_verification

    engine, gate = env
    cid, _ = _agent_concept(engine, gate, support_source="user")
    ok, _ = gate.process_self_model_patch(cid, _user_unit(engine), verification="verified")
    assert ok
    concept = engine.semantic.get(cid)
    assert effective_verification(concept) == "verified"
    concept.description = "被 expand／modify／merge 改過的描述"
    assert effective_verification(concept) == "unverified"


def test_apply_rechecks_state_and_evidence(env):
    from echo_memory.affect.concept_patch import ConceptPatch, PatchEvidence
    from echo_memory.schemas.self_model import description_digest

    engine, gate = env
    cid, units = _agent_concept(engine, gate)
    concept = engine.semantic.get(cid)

    def patch(refs, digest=None):
        return ConceptPatch(
            transaction_id="t", concept_name=concept.name, action="self_model", concept_id=cid,
            new_value={
                "evidence_refs": refs, "verification": "unverified",
                "description_digest": digest or description_digest(concept.description),
            },
            evidence=PatchEvidence("self_model", 0.7, "valid", "x"),
        )

    assert gate._apply_self_model(concept, patch(units)) is True
    concept.metadata.pop("self_model")
    assert gate._apply_self_model(concept, patch(["ghost"])) is False  # 證據不存在
    assert gate._apply_self_model(concept, patch(units, digest="stale")) is False  # 描述已變
    concept.status = "deprecated"
    assert gate._apply_self_model(concept, patch(units)) is False  # 不再 active
    assert "self_model" not in concept.metadata


def test_retrieval_rechecks_evidence(env, monkeypatch):
    monkeypatch.setattr(get_config(), "self_model_enabled", True)
    engine, gate = env
    cid, units = _agent_concept(engine, gate)
    ok, _ = gate.process_self_model_patch(cid, units)
    assert ok
    kept, degraded = engine._retrieve_self_observations()
    assert [c.id for c in kept] == [cid] and degraded == set()

    # 部分證據失效 -> 仍注入但降為未驗證
    extra = _user_unit(engine)
    c2, u2 = _agent_concept_named(engine, gate, "第二習慣")
    ok, _ = gate.process_self_model_patch(c2, u2 + extra)
    assert ok
    engine.evidence.redact(extra)
    kept, degraded = engine._retrieve_self_observations()
    assert {c.id for c in kept} == {cid, c2} and degraded == {c2}

    # 全部失效 -> 不注入
    engine.evidence.redact(units)
    kept, _ = engine._retrieve_self_observations()
    assert cid not in {c.id for c in kept}
