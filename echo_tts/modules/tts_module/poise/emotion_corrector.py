"""
EmotionCorrector — 情緒向量校正器

在休息段將情緒向量從當前值拉向 neutral，
休息後的「復歸段」則從接近原始情緒重新出發。

設計目標（D2 決策）：
- 主要價值在「感知上的情緒重置」——激烈段後藉呼吸段回到中性
- 不只是修正數學漂移，而是讓每段弧線有起點和終點
"""

from typing import List, Optional

from .level import PoiseLevel


# 8D emotion dimensions (matches emotion_dynamics.py)
NEUTRAL_VECTOR = [0.0] * 8
NUM_EMOTION_DIMS = 8


class EmotionCorrector:
    """
    Computes corrected emotion vectors for poise rest segments
    and resume arc_scale values for post-rest segments.
    """

    # Correction strength per level:
    # How much to lerp toward neutral (0.0 = keep original, 1.0 = fully neutral)
    DEFAULT_CORRECTION_STRENGTH = {
        PoiseLevel.SILENCE: 0.0,       # No TTS, no emotion needed
        PoiseLevel.BREATH: 0.0,        # No TTS, SFX only
        PoiseLevel.FILLER_LIGHT: 0.7,  # Mostly neutral, 30% original tone
        PoiseLevel.FILLER_HEAVY: 0.9,  # Almost neutral
        PoiseLevel.ACTION: 1.0,        # Fully neutral (character is "out of narration")
    }

    # Post-rest arc_scale: slightly emphasize to smooth the re-entry
    DEFAULT_RESUME_ARC_SCALE = {
        PoiseLevel.SILENCE: 1.0,
        PoiseLevel.BREATH: 1.0,
        PoiseLevel.FILLER_LIGHT: 1.03,   # Gentle re-entry
        PoiseLevel.FILLER_HEAVY: 1.05,   # Noticeable re-entry
        PoiseLevel.ACTION: 1.08,          # Strong re-entry after full break
    }

    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self.correction_strength = dict(self.DEFAULT_CORRECTION_STRENGTH)
        self.resume_arc_scale = dict(self.DEFAULT_RESUME_ARC_SCALE)

    def compute_rest_emotion(
        self,
        original_emotion: List[float],
        level: PoiseLevel,
    ) -> Optional[List[float]]:
        """
        Compute the emotion vector for a poise rest segment.

        Returns None if the level doesn't require TTS (SILENCE, BREATH),
        since those don't go through the synthesis pipeline.

        Args:
            original_emotion: The base 8D emotion vector (before any arc offset).
            level: The rest level being synthesized.

        Returns:
            Corrected 8D emotion vector, or None if not applicable.
        """
        if not self.enabled or not level.requires_tts:
            return None

        strength = self.correction_strength.get(level, 0.0)
        if strength == 0.0:
            return list(original_emotion)

        # Linear interpolation toward neutral
        corrected = [
            original_emotion[i] * (1.0 - strength) + NEUTRAL_VECTOR[i] * strength
            for i in range(min(len(original_emotion), NUM_EMOTION_DIMS))
        ]
        return corrected

    def get_resume_arc_scale(self, level: PoiseLevel) -> float:
        """
        Get the arc_scale override for the first segment after a rest.

        This provides a gentle emotional "re-entry" — slightly emphasizing
        the original emotion to smooth the transition from neutral back
        to the narrative tone.

        Args:
            level: The rest level that just completed.

        Returns:
            arc_scale value to use for the next normal segment.
        """
        if not self.enabled:
            return 1.0
        return self.resume_arc_scale.get(level, 1.0)
