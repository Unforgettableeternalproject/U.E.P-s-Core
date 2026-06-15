"""
tests/test_concept_verifier.py — ConceptVerifier 單元測試

驗證雙層概念驗證邏輯：embedding 過濾 + LLM 判斷。
"""

from __future__ import annotations

import json

import numpy as np
import pytest

from echo_memory.affect.concept_verifier import (
    ConceptAssessment,
    ConceptVerification,
    ConceptVerifier,
)
from echo_memory.schemas.concept import Concept
from echo_memory.schemas.memory_context import MemoryContext


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _make_concept(
    name: str,
    description: str = "test concept",
    concept_id: str | None = None,
    embedding: list[float] | None = None,
) -> Concept:
    """建立測試用 Concept"""
    c = Concept(name=name, description=description)
    if concept_id:
        c.id = concept_id
    if embedding:
        c.embedding = embedding
    return c


def _make_context(
    concepts: list[Concept],
    concept_similarities: dict[str, float] | None = None,
) -> MemoryContext:
    """建立帶概念的 MemoryContext"""
    ctx = MemoryContext(concepts=concepts)
    if concept_similarities:
        ctx.concept_similarities = concept_similarities
    return ctx


def _mock_llm_valid(_prompt: str) -> str:
    """LLM 回傳全部 valid"""
    return json.dumps([
        {"concept": "貓", "status": "valid", "reason": "無變化", "update": None},
    ])


def _mock_llm_mixed(_prompt: str) -> str:
    """LLM 回傳混合結果"""
    return json.dumps([
        {"concept": "貓", "status": "valid", "reason": "不受影響", "update": None},
        {"concept": "狗", "status": "expand", "reason": "用戶提到新品種", "update": "犬類，包含柴犬"},
        {"concept": "魚", "status": "deprecate", "reason": "用戶表示不再養魚", "update": None},
    ])


def _mock_embed(text: str) -> list[float]:
    """確定性 mock embedding（512 維）"""
    import hashlib
    seed = int.from_bytes(hashlib.sha256(text.encode()).digest()[:4], "little")
    rng = np.random.RandomState(seed)
    vec = rng.randn(512).astype(np.float32)
    vec /= np.linalg.norm(vec)
    return vec.tolist()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ConceptVerification 資料結構
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestConceptVerification:
    """ConceptVerification 資料結構測試"""

    def test_empty_verification(self):
        """空結果不該有變更"""
        v = ConceptVerification()
        assert not v.has_changes
        assert v.changes == []
        assert v.to_context_text() == ""

    def test_all_valid(self):
        """全部 valid 不該有變更"""
        v = ConceptVerification(assessments=[
            ConceptAssessment(concept_id="c1", concept_name="A", status="valid"),
            ConceptAssessment(concept_id="c2", concept_name="B", status="valid"),
        ])
        assert not v.has_changes
        assert len(v.changes) == 0

    def test_has_changes(self):
        """有非 valid 的判定應該有變更"""
        v = ConceptVerification(assessments=[
            ConceptAssessment(concept_id="c1", concept_name="A", status="valid"),
            ConceptAssessment(concept_id="c2", concept_name="B", status="expand",
                              reason="新資訊"),
        ])
        assert v.has_changes
        assert len(v.changes) == 1
        assert v.changes[0].concept_name == "B"

    def test_to_context_text(self):
        """context text 應只列出有變更的概念"""
        v = ConceptVerification(assessments=[
            ConceptAssessment(concept_id="c1", concept_name="貓", status="valid"),
            ConceptAssessment(concept_id="c2", concept_name="狗", status="modify",
                              reason="品種修正", suggested_update="柴犬"),
        ])
        text = v.to_context_text()
        assert "狗" in text
        assert "修正" in text
        assert "柴犬" in text
        assert "貓" not in text  # valid 的不該出現

    def test_summary_no_changes(self):
        """summary 無變更"""
        v = ConceptVerification(assessments=[
            ConceptAssessment(concept_id="c1", concept_name="A", status="valid"),
        ])
        assert "無變更" in v.summary()

    def test_summary_with_changes(self):
        """summary 有變更"""
        v = ConceptVerification(assessments=[
            ConceptAssessment(concept_id="c1", concept_name="A", status="expand"),
            ConceptAssessment(concept_id="c2", concept_name="B", status="deprecate"),
        ])
        s = v.summary()
        assert "2 個變更" in s


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ConceptVerifier — 無 LLM / 無概念
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestVerifierNoLLM:
    """無 LLM 的降級行為"""

    def test_no_concepts(self):
        """無概念時回傳空結果"""
        v = ConceptVerifier()
        result = v.verify("hello", MemoryContext())
        assert not result.has_changes
        assert result.assessments == []

    def test_none_context(self):
        """None context 回傳空結果"""
        v = ConceptVerifier()
        result = v.verify("hello", None)
        assert not result.has_changes

    def test_no_llm_all_valid(self):
        """無 llm_fn 時，所有概念應標記 valid"""
        concepts = [
            _make_concept("貓", "一種動物"),
            _make_concept("狗", "另一種動物"),
        ]
        ctx = _make_context(concepts, {"": 0.5})

        v = ConceptVerifier(llm_fn=None)
        result = v.verify("我的貓很可愛", ctx)

        assert not result.has_changes
        assert not result.used_llm
        # 所有概念都應在結果中
        names = {a.concept_name for a in result.assessments}
        assert "貓" in names
        assert "狗" in names

    def test_no_llm_no_embed(self):
        """完全無 LLM 和 embedding 時，仍然不崩潰"""
        concepts = [_make_concept("測試")]
        ctx = _make_context(concepts)

        v = ConceptVerifier(llm_fn=None, embed_fn=None)
        result = v.verify("hello", ctx)

        assert len(result.assessments) == 1
        assert result.assessments[0].status == "valid"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ConceptVerifier — Embedding 過濾
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestVerifierEmbeddingFilter:
    """Embedding 相關性過濾"""

    def test_uses_context_similarities(self):
        """優先使用 MemoryContext 中已有的 concept_similarities"""
        c1 = _make_concept("貓", concept_id="c1")
        c2 = _make_concept("狗", concept_id="c2")
        ctx = _make_context(
            [c1, c2],
            concept_similarities={"c1": 0.8, "c2": 0.1},
        )

        v = ConceptVerifier(llm_fn=_mock_llm_valid, embed_fn=_mock_embed)
        result = v.verify("貓咪", ctx)

        # c2 (sim=0.1) 低於閾值，應該是 valid（不送 LLM）
        c2_assessment = next(a for a in result.assessments if a.concept_name == "狗")
        assert c2_assessment.status == "valid"
        assert c2_assessment.relevance == 0.1

    def test_low_relevance_skips_llm(self):
        """低相關度的概念不應送 LLM"""
        c1 = _make_concept("量子力學", concept_id="c1")
        ctx = _make_context(
            [c1],
            concept_similarities={"c1": 0.05},  # 非常低
        )

        call_count = 0

        def counting_llm(prompt: str) -> str:
            nonlocal call_count
            call_count += 1
            return "[]"

        v = ConceptVerifier(llm_fn=counting_llm)
        result = v.verify("今天天氣好", ctx)

        assert call_count == 0  # LLM 不應被呼叫
        assert result.assessments[0].status == "valid"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ConceptVerifier — LLM 驗證
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestVerifierWithLLM:
    """LLM 概念狀態判斷"""

    def test_llm_mixed_results(self):
        """LLM 回傳混合結果時正確解析"""
        concepts = [
            _make_concept("貓", concept_id="c1"),
            _make_concept("狗", concept_id="c2"),
            _make_concept("魚", concept_id="c3"),
        ]
        ctx = _make_context(
            concepts,
            concept_similarities={"c1": 0.7, "c2": 0.8, "c3": 0.6},
        )

        v = ConceptVerifier(llm_fn=_mock_llm_mixed)
        result = v.verify("我新養了一隻柴犬，不再養魚了", ctx)

        assert result.used_llm
        assert result.has_changes

        # 檢查各概念狀態
        by_name = {a.concept_name: a for a in result.assessments}
        assert by_name["貓"].status == "valid"
        assert by_name["狗"].status == "expand"
        assert by_name["狗"].suggested_update == "犬類，包含柴犬"
        assert by_name["魚"].status == "deprecate"

    def test_llm_failure_degrades(self):
        """LLM 呼叫失敗時降級為全部 valid"""
        concepts = [_make_concept("測試", concept_id="c1")]
        ctx = _make_context(concepts, concept_similarities={"c1": 0.8})

        def failing_llm(_: str) -> str:
            raise RuntimeError("API 錯誤")

        v = ConceptVerifier(llm_fn=failing_llm)
        result = v.verify("test", ctx)

        assert not result.has_changes
        assert result.assessments[0].status == "valid"

    def test_llm_bad_json_degrades(self):
        """LLM 回傳非 JSON 時降級"""
        concepts = [_make_concept("測試", concept_id="c1")]
        ctx = _make_context(concepts, concept_similarities={"c1": 0.8})

        v = ConceptVerifier(llm_fn=lambda _: "這不是 JSON")
        result = v.verify("test", ctx)

        assert result.assessments[0].status == "valid"

    def test_llm_missing_concepts_default_valid(self):
        """LLM 回應中缺少的概念應預設為 valid"""
        concepts = [
            _make_concept("A", concept_id="c1"),
            _make_concept("B", concept_id="c2"),
        ]
        ctx = _make_context(
            concepts,
            concept_similarities={"c1": 0.7, "c2": 0.7},
        )

        # LLM 只回覆了 A
        def partial_llm(_: str) -> str:
            return json.dumps([
                {"concept": "A", "status": "expand", "reason": "new info", "update": "updated A"},
            ])

        v = ConceptVerifier(llm_fn=partial_llm)
        result = v.verify("test", ctx)

        by_name = {a.concept_name: a for a in result.assessments}
        assert by_name["A"].status == "expand"
        assert by_name["B"].status == "valid"  # 缺少 → valid

    def test_llm_invalid_status_defaults_valid(self):
        """LLM 回傳無效 status 值時降級為 valid"""
        concepts = [_make_concept("X", concept_id="c1")]
        ctx = _make_context(concepts, concept_similarities={"c1": 0.7})

        def bad_status_llm(_: str) -> str:
            return json.dumps([
                {"concept": "X", "status": "unknown_status", "reason": "?", "update": None},
            ])

        v = ConceptVerifier(llm_fn=bad_status_llm)
        result = v.verify("test", ctx)

        assert result.assessments[0].status == "valid"

    def test_max_concepts_limit(self):
        """超過 max_concepts 的概念不應送 LLM"""
        concepts = [
            _make_concept(f"concept_{i}", concept_id=f"c{i}")
            for i in range(10)
        ]
        sims = {f"c{i}": 0.9 - i * 0.05 for i in range(10)}
        ctx = _make_context(concepts, concept_similarities=sims)

        received_concepts = []

        def tracking_llm(prompt: str) -> str:
            # 計算 prompt 中有幾個概念
            count = prompt.count("concept_")
            received_concepts.append(count)
            return "[]"

        v = ConceptVerifier(llm_fn=tracking_llm, max_concepts=3)
        v.verify("test", ctx)

        # LLM 最多收到 3 個概念
        assert received_concepts[0] == 3

    def test_json_with_surrounding_text(self):
        """LLM 回覆 JSON 前後有文字時仍能解析"""
        concepts = [_make_concept("貓", concept_id="c1")]
        ctx = _make_context(concepts, concept_similarities={"c1": 0.8})

        def wrapped_json_llm(_: str) -> str:
            return '以下是分析結果：\n[{"concept": "貓", "status": "expand", "reason": "更多資訊", "update": "家貓"}]\n分析完畢。'

        v = ConceptVerifier(llm_fn=wrapped_json_llm)
        result = v.verify("test", ctx)

        assert result.assessments[0].status == "expand"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ConceptAssessment
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestConceptAssessment:
    """ConceptAssessment 資料結構"""

    def test_defaults(self):
        """預設值"""
        a = ConceptAssessment(concept_id="c1", concept_name="test")
        assert a.status == "valid"
        assert a.reason == ""
        assert a.suggested_update is None
        assert a.relevance == 0.0

    def test_with_update(self):
        """帶更新建議"""
        a = ConceptAssessment(
            concept_id="c1",
            concept_name="test",
            status="modify",
            suggested_update="new desc",
        )
        assert a.suggested_update == "new desc"
