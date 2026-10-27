"""held patch 重審的上限、逾期與重啟後持久化。"""

from __future__ import annotations

import json

from echo_memory.affect.concept_inhibitor import ConceptInhibitor
from echo_memory.affect.concept_verifier import ConceptAssessment, ConceptVerification
from tests.baseline._support import tx_audit


def _modify_verification(engine) -> ConceptVerification:
    c = engine.semantic.get_by_name("小橘")
    return ConceptVerification(assessments=[ConceptAssessment(
        concept_id=c.id, concept_name="小橘", status="modify",
        reason="使用者更正", suggested_update="使用者養的公貓", relevance=0.9,
    )])


def _always_fail(prompt: str) -> str:
    raise RuntimeError("LLM 持續故障")


def _final_status(engine, patch_id: str) -> str:
    latest = ""
    for tx in tx_audit(engine):
        for p in tx["patches"]:
            if p["patch_id"] == patch_id:
                latest = p["status"]
    return latest


def test_held_patch_becomes_final_after_retry_cap(restored_engine):
    e = restored_engine
    inh = ConceptInhibitor(semantic=e.semantic, llm_fn=_always_fail, held_max_retries=2)
    tx = inh.process(_modify_verification(e), "小橘是公的", "了解", "s", "t1")
    pid = tx.patches[0].patch_id
    assert tx.patches[0].status == "held"
    assert inh.held_count() == 1

    inh.review_held_patches()  # 第 1 次：仍 held
    assert inh.held_count() == 1
    assert _final_status(e, pid) == "held"
    inh.review_held_patches()  # 第 2 次：達上限 -> 最終 rejected
    assert inh.held_count() == 0
    assert _final_status(e, pid) == "rejected"
    assert e.semantic.get_by_name("小橘").description == "使用者養的貓"


def test_held_patch_expires_by_age(restored_engine):
    e = restored_engine
    inh = ConceptInhibitor(semantic=e.semantic, llm_fn=_always_fail, held_max_age_days=0)
    tx = inh.process(_modify_verification(e), "小橘是公的", "了解", "s", "t1")
    pid = tx.patches[0].patch_id
    inh.review_held_patches()
    assert inh.held_count() == 0
    assert _final_status(e, pid) == "rejected"


def test_held_registry_survives_restart(restored_engine):
    e = restored_engine
    inh = ConceptInhibitor(semantic=e.semantic, llm_fn=_always_fail)
    tx = inh.process(_modify_verification(e), "小橘是公的", "了解", "s", "t1")
    pid = tx.patches[0].patch_id

    ok = json.dumps({"approve": True, "confidence": 0.9, "reason": "ok"})
    revived = ConceptInhibitor(semantic=e.semantic, llm_fn=lambda _p: ok)
    assert revived.held_count() == 1
    revived.review_held_patches()
    assert _final_status(e, pid) == "applied"
    assert e.semantic.get_by_name("小橘").description == "使用者養的公貓"


def test_stale_held_patch_is_discarded(restored_engine):
    e = restored_engine
    inh = ConceptInhibitor(semantic=e.semantic, llm_fn=_always_fail)
    tx = inh.process(_modify_verification(e), "小橘是公的", "了解", "s", "t1")
    pid = tx.patches[0].patch_id
    e.semantic.get_by_name("小橘").description = "別處已改過的描述"

    ok = json.dumps({"approve": True, "confidence": 0.9, "reason": "ok"})
    revived = ConceptInhibitor(semantic=e.semantic, llm_fn=lambda _p: ok)
    revived.review_held_patches()
    assert _final_status(e, pid) == "rejected"
    assert e.semantic.get_by_name("小橘").description == "別處已改過的描述"
