"""
PoiseLevel — 呼吸段強度層級定義
"""

from enum import IntEnum


class PoiseLevel(IntEnum):
    """
    呼吸段的強度層級。

    由 Potency × 累積量共同決定，越高層級的休息
    對敘事的影響越大、持續時間越長。
    """
    SILENCE = 0       # 純靜音 200-800ms
    BREATH = 1        # TTS 合成嘆氣（"......" + melancholic emotion）
    FILLER_LIGHT = 2  # 輕量銜接詞（「嗯...」「umm...」）
    FILLER_HEAVY = 3  # 有敘事影響的插入語（「等一下喔...」「let me think...」）
    ACTION = 4        # 完整動作描述（「抱歉讓我喝口水」+ 音效 placeholder）

    @property
    def requires_tts(self) -> bool:
        """Whether this level needs TTS synthesis (vs. silence/sfx insertion)."""
        return self >= PoiseLevel.BREATH

    @property
    def requires_sfx(self) -> bool:
        """Whether this level uses sound effect assets."""
        return self == PoiseLevel.ACTION  # Only ACTION may use SFX in the future

    @property
    def label(self) -> str:
        """Human-readable label for logging."""
        return {
            PoiseLevel.SILENCE: "靜音",
            PoiseLevel.BREATH: "呼吸",
            PoiseLevel.FILLER_LIGHT: "輕量銜接",
            PoiseLevel.FILLER_HEAVY: "敘事插入",
            PoiseLevel.ACTION: "動作描述",
        }[self]
