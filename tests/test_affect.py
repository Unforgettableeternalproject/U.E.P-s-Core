"""
affect 模組測試 — SalienceEvaluator + SubconsciousAssessor + signals

驗收標準：
  1. SalienceSignals 可正確建立
  2. SubconsciousAssessment 5 維度在合法範圍
  3. SalienceEvaluator 三值輸出在 [0, 1]
  4. SubconsciousAssessor LLM/heuristic 模式都能運作
  5. 事件驅動方法正確更新 neurochem
"""

from __future__ import annotations

import json

import pytest

from echo_memory.affect.signals import SalienceSignals, SubconsciousAssessment
from echo_memory.affect.salience import SalienceEvaluator
from echo_memory.affect.subconscious import SubconsciousAssessor
from echo_memory.neurochem.state import NeurochemState


# ─── SalienceSignals ──────────────────────────────────────────────


class TestSalienceSignals:
    def test_default_values(self):
        s = SalienceSignals()
        assert s.user_feedback == 0.0
        assert s.task_complexity == 0.5
        assert s.novelty_score == 0.5
        assert s.error_occurred is False
        assert s.verification_score == 1.0

    def test_from_subconscious(self):
        assessment = SubconsciousAssessment(
            emotional_tone=0.7,
            novelty=0.8,
            complexity=0.6,
            uncertainty=0.3,
            dream_resonance=0.5,
        )
        signals = SalienceSignals.from_subconscious(
            assessment, verification_score=0.9, error_occurred=True
        )
        assert signals.user_feedback == 0.7
        assert signals.task_complexity == 0.6
        assert signals.novelty_score == 0.8
        assert signals.error_occurred is True
        assert signals.verification_score == 0.9


# ─── SubconsciousAssessment ────────────────────────────────────────


class TestSubconsciousAssessment:
    def test_default_values(self):
        a = SubconsciousAssessment()
        assert a.emotional_tone == 0.0
        assert a.novelty == 0.5
        assert a.complexity == 0.5
        assert a.uncertainty == 0.0
        assert a.dream_resonance == 0.0

    def test_to_dict(self):
        a = SubconsciousAssessment(emotional_tone=0.5, novelty=0.8)
        d = a.to_dict()
        assert d["emotional_tone"] == 0.5
        assert d["novelty"] == 0.8
        assert "dreamed_topics" in d


# ─── SalienceEvaluator ────────────────────────────────────────────


class TestSalienceEvaluator:
    def test_three_values_in_range(self):
        nc = NeurochemState(dopamine=0.6, serotonin=0.4)
        evaluator = SalienceEvaluator(nc)
        signals = SalienceSignals(
            user_feedback=0.3,
            task_complexity=0.7,
            novelty_score=0.8,
        )
        salience, da, ht = evaluator.evaluate(signals, "hello", "world")
        assert 0.0 <= salience <= 1.0
        assert 0.0 <= da <= 1.0
        assert 0.0 <= ht <= 1.0

    def test_high_complexity_high_salience(self):
        nc = NeurochemState()
        evaluator = SalienceEvaluator(nc)

        low = SalienceSignals(task_complexity=0.1, novelty_score=0.1)
        high = SalienceSignals(task_complexity=0.9, novelty_score=0.9)

        s_low, _, _ = evaluator.evaluate(low)
        s_high, _, _ = evaluator.evaluate(high)
        assert s_high > s_low

    def test_error_bonus(self):
        nc = NeurochemState()
        evaluator = SalienceEvaluator(nc)

        no_error = SalienceSignals(error_occurred=False)
        with_error = SalienceSignals(error_occurred=True)

        s1, _, _ = evaluator.evaluate(no_error)
        s2, _, _ = evaluator.evaluate(with_error)
        assert s2 > s1

    def test_positive_feedback_increases_da_weight(self):
        nc = NeurochemState(dopamine=0.5)
        evaluator = SalienceEvaluator(nc)

        signals = SalienceSignals(user_feedback=0.8)
        _, da, _ = evaluator.evaluate(signals)
        assert da > 0.5

    def test_error_decreases_da_weight(self):
        nc = NeurochemState(dopamine=0.5)
        evaluator = SalienceEvaluator(nc)

        signals = SalienceSignals(error_occurred=True)
        _, da, _ = evaluator.evaluate(signals)
        assert da < 0.5

    def test_ht_weight_formula(self):
        """ht_weight = serotonin×0.7 + verification×0.3"""
        nc = NeurochemState(serotonin=0.8)
        evaluator = SalienceEvaluator(nc)

        signals = SalienceSignals(verification_score=1.0)
        _, _, ht = evaluator.evaluate(signals)
        expected = 0.8 * 0.7 + 1.0 * 0.3
        assert ht == pytest.approx(expected, abs=0.01)

    def test_update_neurochem_success(self):
        nc = NeurochemState(dopamine=0.5, serotonin=0.5)
        evaluator = SalienceEvaluator(nc)

        signals = SalienceSignals(user_feedback=0.5)
        evaluator.update_neurochem(signals)
        assert nc.dopamine > 0.5  # on_success 被呼叫

    def test_update_neurochem_failure(self):
        nc = NeurochemState(dopamine=0.5, serotonin=0.5)
        evaluator = SalienceEvaluator(nc)

        signals = SalienceSignals(user_feedback=-0.5, error_occurred=True)
        evaluator.update_neurochem(signals)
        assert nc.dopamine < 0.5  # on_failure 被呼叫

    def test_update_neurochem_discovery(self):
        nc = NeurochemState(dopamine=0.5, serotonin=0.5)
        evaluator = SalienceEvaluator(nc)

        signals = SalienceSignals(novelty_score=0.9)
        evaluator.update_neurochem(signals)
        assert nc.dopamine > 0.5  # on_discovery 被呼叫


# ─── SubconsciousAssessor ──────────────────────────────────────────


class TestSubconsciousAssessor:
    def test_heuristic_mode(self):
        """無 LLM 時使用啟發式評估。"""
        assessor = SubconsciousAssessor(llm_fn=None)
        result = assessor.assess("這是一個很複雜的問題嗎？")

        assert isinstance(result, SubconsciousAssessment)
        assert -1.0 <= result.emotional_tone <= 1.0
        assert 0.0 <= result.novelty <= 1.0
        assert 0.0 <= result.complexity <= 1.0
        assert 0.0 <= result.uncertainty <= 1.0
        assert 0.0 <= result.dream_resonance <= 1.0

    def test_heuristic_question_uncertainty(self):
        """問號多 → 高不確定性。"""
        assessor = SubconsciousAssessor(llm_fn=None)
        result = assessor.assess("為什麼？怎麼做？什麼意思？")
        assert result.uncertainty > 0.0

    def test_heuristic_long_input_complexity(self):
        """長輸入 → 高複雜度。"""
        assessor = SubconsciousAssessor(llm_fn=None)
        short = assessor.assess("你好")
        long_input = "a" * 500
        long_result = assessor.assess(long_input)
        assert long_result.complexity > short.complexity

    def test_llm_mode_success(self):
        """mock LLM 回傳合法 JSON。"""
        mock_response = json.dumps({
            "emotional_tone": 0.7,
            "novelty": 0.8,
            "complexity": 0.6,
            "uncertainty": 0.1,
            "dream_resonance": 0.3,
        })
        assessor = SubconsciousAssessor(llm_fn=lambda _: mock_response)
        result = assessor.assess("測試輸入")

        assert result.emotional_tone == pytest.approx(0.7)
        assert result.novelty == pytest.approx(0.8)
        assert result.complexity == pytest.approx(0.6)

    def test_llm_mode_markdown_json(self):
        """mock LLM 回傳帶 markdown code block 的 JSON。"""
        mock_response = """```json
{
  "emotional_tone": 0.5,
  "novelty": 0.6,
  "complexity": 0.7,
  "uncertainty": 0.2,
  "dream_resonance": 0.1
}
```"""
        assessor = SubconsciousAssessor(llm_fn=lambda _: mock_response)
        result = assessor.assess("測試")
        assert result.emotional_tone == pytest.approx(0.5)

    def test_llm_mode_invalid_json_fallback(self):
        """LLM 回傳無效 JSON 時降級到啟發式。"""
        assessor = SubconsciousAssessor(llm_fn=lambda _: "這不是 JSON")
        result = assessor.assess("測試")
        # 不崩潰，回傳預設值
        assert isinstance(result, SubconsciousAssessment)

    def test_llm_exception_fallback(self):
        """LLM 呼叫拋異常時降級。"""
        def failing_llm(prompt: str) -> str:
            raise RuntimeError("API 失敗")

        assessor = SubconsciousAssessor(llm_fn=failing_llm)
        result = assessor.assess("測試")
        assert isinstance(result, SubconsciousAssessment)

    def test_llm_mode_values_clamped(self):
        """超出範圍的值被 clamp。"""
        mock_response = json.dumps({
            "emotional_tone": 5.0,  # 超出 [-1, 1]
            "novelty": -2.0,        # 超出 [0, 1]
            "complexity": 0.5,
            "uncertainty": 0.3,
            "dream_resonance": 0.0,
        })
        assessor = SubconsciousAssessor(llm_fn=lambda _: mock_response)
        result = assessor.assess("測試")
        assert result.emotional_tone <= 1.0
        assert result.novelty >= 0.0
