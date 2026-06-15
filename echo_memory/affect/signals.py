"""
SalienceSignals + SubconsciousAssessment — 資料結構定義

對應 OpenSoul 的：
  - soul/affect/salience.py::SalienceSignals
  - soul/affect/subconscious.py::SubconsciousAssessment

這些 dataclass 是 SalienceEvaluator 和 SubconsciousAssessor 的輸出容器。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SubconsciousAssessment:
    """
    潛意識評估結果 — 5 維度 [0, 1]

    對應 OpenSoul SubconsciousAssessment。
    由 SubconsciousAssessor（LLM 呼叫）生成。
    LLM 不可用時使用預設值。

    Attributes:
        emotional_tone: 情感基調（-1.0 負 ~ 1.0 正）
        novelty: 話題新鮮程度（0~1）
        complexity: 問題複雜度（0~1）
        uncertainty: 不確定程度（0~1）
        dream_resonance: 與夢境記憶的共鳴（0~1）
        dreamed_topics: 觸發共鳴的夢境主題名稱
    """

    emotional_tone: float = 0.0
    novelty: float = 0.5
    complexity: float = 0.5
    uncertainty: float = 0.0
    dream_resonance: float = 0.0
    dreamed_topics: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "emotional_tone": round(self.emotional_tone, 4),
            "novelty": round(self.novelty, 4),
            "complexity": round(self.complexity, 4),
            "uncertainty": round(self.uncertainty, 4),
            "dream_resonance": round(self.dream_resonance, 4),
            "dreamed_topics": self.dreamed_topics,
        }


@dataclass
class SalienceSignals:
    """
    顯著性信號 — SalienceEvaluator 的輸入容器

    對應 OpenSoul SalienceSignals。
    集合了各種來源的信號，由 SalienceEvaluator 統一計算三值輸出。

    Attributes:
        user_feedback: 使用者回饋分數 (-1.0 ~ 1.0)
        task_complexity: 任務複雜度 (0~1)，來自 SubconsciousAssessment
        novelty_score: 新鮮程度 (0~1)，來自 SubconsciousAssessment
        error_occurred: 是否發生錯誤
        was_corrected: 是否被修正
        verification_score: 品質閘門分數 (0~1)，來自 ResponseVerifier
    """

    user_feedback: float = 0.0
    task_complexity: float = 0.5
    novelty_score: float = 0.5
    error_occurred: bool = False
    was_corrected: bool = False
    verification_score: float = 1.0

    @classmethod
    def from_subconscious(
        cls,
        assessment: SubconsciousAssessment,
        verification_score: float = 1.0,
        error_occurred: bool = False,
    ) -> SalienceSignals:
        """從 SubconsciousAssessment 快速建立。"""
        return cls(
            user_feedback=assessment.emotional_tone,
            task_complexity=assessment.complexity,
            novelty_score=assessment.novelty,
            error_occurred=error_occurred,
            verification_score=verification_score,
        )
