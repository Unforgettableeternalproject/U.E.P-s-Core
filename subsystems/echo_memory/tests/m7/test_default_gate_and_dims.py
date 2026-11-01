"""
預設 Gate 的並行／明確無 Gate 語意，以及換 embedding 模型後的維度防護（寫入端）。
全部 mock LLM／mock embedding，不連外。
"""

from __future__ import annotations

import threading
import time

import pytest

from echo_memory.affect.concept_inhibitor import ConceptInhibitor
from echo_memory.engine import MemoryEngine
from echo_memory.schemas.evidence import EvidenceInput
from echo_memory.storage.episodic import EpisodicMemory
from echo_memory.storage.semantic import EmbeddingDimensionError, SemanticMemory
from tests.m7.sim import RoutedLLM, user_ev
from tests.m7.test_release_gates import _distill, _fake_embedding


# ━━━ M1：並行冷啟動時，快速路徑不得拿到尚未建成的 Gate ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_default_gate_concurrent_callers_never_see_none(tmp_path, monkeypatch):
    engine = MemoryEngine(storage_path=tmp_path / "d", namespace_id="ns1")
    # 先把元件建好但不建 Gate，再拖慢 ConceptInhibitor.__init__ 放大競爭窗口
    _ = (engine.episodic, engine.semantic, engine.working, engine.procedural,
         engine.entity_store, engine.entity_extractor, engine.evidence,
         engine.idempotency, engine.activity, engine.retrieval)
    assert engine._concept_inhibitor is None

    orig_init = ConceptInhibitor.__init__

    def slow_init(self, *a, **k):
        time.sleep(0.3)
        orig_init(self, *a, **k)

    monkeypatch.setattr(ConceptInhibitor, "__init__", slow_init)

    seen: list = []
    barrier = threading.Barrier(4)

    def worker():
        barrier.wait()
        engine._ensure_default_gate()
        seen.append(engine._concept_inhibitor)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(seen) == 4 and all(g is not None for g in seen), seen
    assert len({id(g) for g in seen}) == 1  # 只建一份


# ━━━ M2：ensure_initialized 之前明確設 None＝刻意無 Gate ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_explicit_none_before_init_is_respected(tmp_path):
    engine = MemoryEngine(storage_path=tmp_path / "d", namespace_id="ns1")
    engine.set_concept_inhibitor(None)
    engine.ensure_initialized()
    assert engine._concept_inhibitor is None
    # 之後宿主仍可注入 Gate
    gate = ConceptInhibitor(semantic=engine.semantic, llm_fn=None)
    engine.set_concept_inhibitor(gate)
    assert engine._concept_inhibitor is gate


def test_unset_gate_still_gets_default(tmp_path):
    engine = MemoryEngine(storage_path=tmp_path / "d", namespace_id="ns1")
    engine.ensure_initialized()
    assert isinstance(engine._concept_inhibitor, ConceptInhibitor)


# ━━━ 換模型後：寫入端拒絕、不留混維資料、重載仍可讀 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _engine_with_concept(tmp_path, monkeypatch, llm):
    monkeypatch.setattr(EpisodicMemory, "_generate_embedding", _fake_embedding(1024))
    e1 = MemoryEngine(storage_path=tmp_path / "d", namespace_id="ns1", llm_fn=llm,
                      embedding_model="bge-m3", embedding_backend="ollama")
    e1.store_episode("我養了一隻貓", "", "s", salience=0.8,
                     evidence=[EvidenceInput(**user_ev("我養了一隻貓"))])
    e1.dream("manual")
    assert e1.semantic.get_by_name("貓") is not None
    return e1


def _snapshot(semantic: SemanticMemory):
    return (
        {n: (c.description, len(c.embedding or []), c.version) for n, c in semantic._concepts.items()},
        None if semantic._embeddings is None else semantic._embeddings.shape,
    )


def test_semantic_write_with_wrong_dim_is_rejected_cleanly(tmp_path, monkeypatch):
    llm = RoutedLLM(distill=_distill(("貓", "使用者養貓", "user", [0])))
    e1 = _engine_with_concept(tmp_path, monkeypatch, llm)
    sem = e1.semantic
    before = _snapshot(sem)
    with pytest.raises(EmbeddingDimensionError):
        sem.upsert_concept("狗", "使用者養狗", embedding=[0.1] * 512)       # 新概念
    with pytest.raises(EmbeddingDimensionError):
        sem.upsert_concept("貓", "改過的描述", embedding=[0.1] * 512)       # 既有概念
    assert sem.get_by_name("狗") is None
    assert _snapshot(sem) == before
    # 重載後仍可讀、維度一致
    reloaded = SemanticMemory(storage_path=sem.storage_path)
    assert reloaded.get_by_name("貓") is not None
    assert reloaded._embeddings.shape[1] == 1024


def test_dream_and_gate_after_model_switch_fail_without_mixed_dims(tmp_path, monkeypatch):
    llm1 = RoutedLLM(distill=_distill(("貓", "使用者養貓", "user", [0])))
    llm = RoutedLLM(distill=_distill(("貓", "使用者養貓", "user", [0]),
                                      ("狗", "使用者也養狗", "user", [0])))
    e1 = _engine_with_concept(tmp_path, monkeypatch, llm1)
    sem_path = e1.semantic.storage_path

    monkeypatch.setattr(EpisodicMemory, "_generate_embedding", _fake_embedding(512))
    e2 = MemoryEngine(storage_path=tmp_path / "d", namespace_id="ns1", llm_fn=llm)
    e2.ensure_initialized()
    e2.semantic._embed_fn = lambda text: [0.1] * 512   # 模擬 Echo Stream 以新模型算概念向量
    before = _snapshot(e2.semantic)

    e2.store_episode("我也養了一隻狗", "", "s2", salience=0.8,
                     evidence=[EvidenceInput(**user_ev("我也養了一隻狗"))])
    e2.dream("manual")                                  # Dream 不得崩潰
    assert e2.semantic.get_by_name("狗") is None         # 新概念被拒，沒有半套寫入
    # Gate 端 modify：embedding 維度不符 -> patch 失敗，描述不變
    cat = e2.semantic.get_by_name("貓")
    gate = e2._concept_inhibitor
    from echo_memory.affect.concept_patch import ConceptPatch, PatchEvidence
    patch = ConceptPatch(transaction_id="tx", concept_id=cat.id, concept_name="貓",
                         action="modify", target_path="description",
                         old_value=cat.description, new_value="全新的描述",
                         patch_confidence=0.8,
                         evidence=PatchEvidence(evidence_kind="explicit_user_claim", evidence_strength=0.8,
                                                verifier_status="valid", verifier_reason="test"))
    assert gate._apply_patch(patch) is False
    assert e2.semantic.get_by_name("貓").description == "使用者養貓"
    assert _snapshot(e2.semantic) == before

    reloaded = SemanticMemory(storage_path=sem_path)
    assert reloaded.get_by_name("貓") is not None
    assert reloaded._embeddings.shape[1] == 1024
