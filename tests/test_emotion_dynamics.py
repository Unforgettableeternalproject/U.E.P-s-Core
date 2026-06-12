"""Tests for emotion_dynamics module (no torch dependency)."""

import sys
from pathlib import Path

# Add tts_module to path for direct import (avoids __init__.py torch dependency)
_tts_module = Path(__file__).resolve().parent.parent / "legacy_tts" / "modules" / "tts_module"
sys.path.insert(0, str(_tts_module))

from emotion_dynamics import (
    EmotionDynamics,
    DynamicsOverrides,
    create_emotion_dynamics,
    EMO_HAPPY, EMO_ANGRY, EMO_SAD, EMO_CALM, EMO_SURPRISED, EMO_MELANCHOLIC,
)


class TestEmotionDynamicsBasic:
    """基本行為測試"""

    def test_neutral_returns_no_overrides(self):
        """中性向量不應產生動態覆寫"""
        dyn = EmotionDynamics()
        result = dyn.compute([0.0] * 8)
        assert result.dominant_emotion == "neutral"
        assert result.temperature is None

    def test_disabled_returns_empty(self):
        """停用時回傳空覆寫"""
        dyn = EmotionDynamics(enabled=False)
        result = dyn.compute([0.5, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
        assert result.temperature is None
        assert result.dominant_emotion == "neutral"

    def test_dominant_emotion_detection(self):
        """應正確偵測主導情緒"""
        dyn = EmotionDynamics()
        # Happy dominant
        result = dyn.compute([0.5, 0.1, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
        assert result.dominant_emotion == "happy"
        # Angry dominant
        result = dyn.compute([0.0, 0.6, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
        assert result.dominant_emotion == "angry"


class TestEmotionDynamicsParameters:
    """參數動態調整測試"""

    def test_angry_raises_temperature(self):
        """憤怒應提高 temperature"""
        dyn = EmotionDynamics()
        base_t = 0.8
        result = dyn.compute(
            [0.0, 0.5, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            base_temperature=base_t,
        )
        assert result.temperature is not None
        assert result.temperature > base_t

    def test_sad_lowers_temperature(self):
        """悲傷應降低 temperature"""
        dyn = EmotionDynamics()
        base_t = 0.8
        result = dyn.compute(
            [0.0, 0.0, 0.5, 0.0, 0.0, 0.0, 0.0, 0.0],
            base_temperature=base_t,
        )
        assert result.temperature is not None
        assert result.temperature < base_t

    def test_angry_shortens_silence(self):
        """憤怒應縮短段間靜音"""
        dyn = EmotionDynamics()
        base_s = 200
        result = dyn.compute(
            [0.0, 0.5, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            base_silence_ms=base_s,
        )
        assert result.interval_silence_ms is not None
        assert result.interval_silence_ms < base_s

    def test_sad_lengthens_silence(self):
        """悲傷應延長段間靜音"""
        dyn = EmotionDynamics()
        base_s = 200
        result = dyn.compute(
            [0.0, 0.0, 0.5, 0.0, 0.0, 0.0, 0.0, 0.0],
            base_silence_ms=base_s,
        )
        assert result.interval_silence_ms is not None
        assert result.interval_silence_ms > base_s

    def test_angry_speeds_up(self):
        """憤怒應加快語速（降低 stretch ratio）"""
        dyn = EmotionDynamics()
        base_r = 1.72
        result = dyn.compute(
            [0.0, 0.5, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            base_stretch_ratio=base_r,
        )
        assert result.s2mel_stretch_ratio is not None
        assert result.s2mel_stretch_ratio < base_r

    def test_melancholic_slows_down(self):
        """憂鬱應減慢語速（提高 stretch ratio）"""
        dyn = EmotionDynamics()
        base_r = 1.72
        result = dyn.compute(
            [0.0, 0.0, 0.0, 0.0, 0.0, 0.5, 0.0, 0.0],
            base_stretch_ratio=base_r,
        )
        assert result.s2mel_stretch_ratio is not None
        assert result.s2mel_stretch_ratio > base_r


class TestEmotionDynamicsSafety:
    """安全邊界測試"""

    def test_temperature_clamped(self):
        """temperature 應被 clamp 在安全範圍"""
        dyn = EmotionDynamics(temperature_range=(0.5, 1.2))
        # Extreme surprised → should be clamped at 1.2
        result = dyn.compute(
            [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0],
            base_temperature=1.15,
        )
        assert result.temperature is not None
        assert result.temperature <= 1.2

    def test_silence_clamped_min(self):
        """段間靜音應不低於最小值"""
        dyn = EmotionDynamics(silence_ms_range=(50, 500))
        result = dyn.compute(
            [0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            base_silence_ms=60,
        )
        assert result.interval_silence_ms is not None
        assert result.interval_silence_ms >= 50

    def test_stretch_ratio_clamped(self):
        """stretch ratio 應在安全範圍"""
        dyn = EmotionDynamics(stretch_ratio_range=(1.2, 2.2))
        result = dyn.compute(
            [0.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0],
            base_stretch_ratio=2.1,
        )
        assert result.s2mel_stretch_ratio is not None
        assert result.s2mel_stretch_ratio <= 2.2

    def test_intensity_scale_amplifies(self):
        """intensity_scale > 1 應放大效果"""
        dyn_normal = EmotionDynamics(intensity_scale=1.0)
        dyn_amp = EmotionDynamics(intensity_scale=2.0)
        emo = [0.0, 0.3, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]

        r_normal = dyn_normal.compute(emo, base_temperature=0.8)
        r_amp = dyn_amp.compute(emo, base_temperature=0.8)

        # Amplified should have a larger temperature increase
        assert r_amp.temperature > r_normal.temperature


class TestEmotionDynamicsConfig:
    """Config 建構測試"""

    def test_create_from_config(self):
        """從 dict config 建構"""
        cfg = {
            "emotion_dynamics": {
                "enabled": True,
                "activation_threshold": 0.1,
                "intensity_scale": 1.5,
            }
        }
        dyn = create_emotion_dynamics(cfg)
        assert dyn.enabled is True
        assert dyn.activation_threshold == 0.1
        assert dyn.intensity_scale == 1.5

    def test_create_default(self):
        """空 config 應用預設值"""
        dyn = create_emotion_dynamics({})
        assert dyn.enabled is True
        assert dyn.activation_threshold == 0.05

    def test_to_dict(self):
        """DynamicsOverrides.to_dict 應只包含非 None 值"""
        ov = DynamicsOverrides(temperature=0.85, s2mel_stretch_ratio=1.65)
        d = ov.to_dict()
        assert "temperature" in d
        assert "s2mel_stretch_ratio" in d
        assert "interval_silence_ms" not in d


class TestEmotionDynamicsMixed:
    """混合情緒測試"""

    def test_mixed_emotions_weighted(self):
        """混合情緒應產生加權中間值"""
        dyn = EmotionDynamics()
        # Half angry + half sad → should have moderate effects
        result = dyn.compute(
            [0.0, 0.3, 0.3, 0.0, 0.0, 0.0, 0.0, 0.0],
            base_temperature=0.8,
            base_silence_ms=200,
        )
        assert result.temperature is not None
        # Angry pulls up, sad pulls down → temperature should be close to base
        # but not exactly at base (different magnitudes)
        assert 0.7 <= result.temperature <= 0.9

    def test_activation_threshold_filters(self):
        """低於閾值的情緒不應觸發調控"""
        dyn = EmotionDynamics(activation_threshold=0.1)
        result = dyn.compute([0.02, 0.01, 0.01, 0.0, 0.0, 0.0, 0.0, 0.0])
        assert result.dominant_emotion == "neutral"
        assert result.temperature is None
