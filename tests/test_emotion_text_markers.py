"""Tests for emotion_text_markers module (no torch dependency)."""

import sys
import random
from pathlib import Path

# Add tts_module to path for direct import (avoids __init__.py torch dependency)
_tts_module = Path(__file__).resolve().parent.parent / "echo_tts" / "modules" / "tts_module"
sys.path.insert(0, str(_tts_module))

from emotion_text_markers import (
    EmotionTextMarkers,
    create_emotion_text_markers,
    EMO_HAPPY, EMO_ANGRY, EMO_SAD, EMO_AFRAID,
    EMO_DISGUSTED, EMO_MELANCHOLIC, EMO_SURPRISED, EMO_CALM,
)


def _make_emo(idx: int, val: float = 0.5) -> list:
    """建立指定情緒維度的向量"""
    v = [0.0] * 8
    v[idx] = val
    return v


class TestEmotionTextMarkersBasic:
    """基本行為測試"""

    def test_neutral_unchanged(self):
        """中性向量不應修改文本"""
        markers = EmotionTextMarkers()
        text = "今天天氣很好"
        result = markers.apply(text, [0.0] * 8, language="zh")
        assert result == text

    def test_disabled_unchanged(self):
        """停用時不修改文本"""
        markers = EmotionTextMarkers(enabled=False)
        text = "今天天氣很好"
        result = markers.apply(text, _make_emo(EMO_ANGRY, 0.5), language="zh")
        assert result == text

    def test_below_threshold_unchanged(self):
        """低於閾值不修改"""
        markers = EmotionTextMarkers(activation_threshold=0.3)
        text = "今天天氣很好"
        result = markers.apply(text, _make_emo(EMO_ANGRY, 0.2), language="zh")
        assert result == text

    def test_empty_text_unchanged(self):
        """空文本不修改"""
        markers = EmotionTextMarkers()
        result = markers.apply("", _make_emo(EMO_ANGRY, 0.5))
        assert result == ""

    def test_wrong_dimension_unchanged(self):
        """維度不對不修改"""
        markers = EmotionTextMarkers()
        result = markers.apply("test", [0.5, 0.5])
        assert result == "test"


class TestEmotionTextMarkersZH:
    """中文標記測試"""

    def test_angry_modifies_text(self):
        """憤怒應修改中文文本"""
        random.seed(42)  # 固定隨機種子
        markers = EmotionTextMarkers(
            prefix_probability=1.0,
            mid_probability=1.0,
            suffix_probability=1.0,
        )
        text = "你怎麼又遲到了"
        result = markers.apply(text, _make_emo(EMO_ANGRY, 0.5), language="zh")
        assert result != text
        # 應該有語氣詞（哼、切）或標點變化
        assert any(p in result for p in ["哼", "切", "！"])

    def test_sad_adds_pauses(self):
        """悲傷應添加停頓"""
        random.seed(42)
        markers = EmotionTextMarkers(
            prefix_probability=1.0,
            suffix_probability=1.0,
        )
        text = "我不知道該怎麼辦"
        result = markers.apply(text, _make_emo(EMO_SAD, 0.5), language="zh")
        assert result != text
        # 應有嘆息或停頓
        assert any(p in result for p in ["唉", "……"])

    def test_surprised_zh(self):
        """驚訝應有驚訝標記"""
        random.seed(42)
        markers = EmotionTextMarkers(
            prefix_probability=1.0,
            suffix_probability=1.0,
        )
        text = "這也太誇張了吧"
        result = markers.apply(text, _make_emo(EMO_SURPRISED, 0.5), language="zh")
        assert result != text

    def test_calm_minimal_change(self):
        """平靜不應大幅修改"""
        markers = EmotionTextMarkers(
            prefix_probability=1.0,
            mid_probability=1.0,
            suffix_probability=1.0,
        )
        text = "今天天氣很好"
        result = markers.apply(text, _make_emo(EMO_CALM, 0.5), language="zh")
        # Calm 規則是空的，不應修改
        assert result == text


class TestEmotionTextMarkersEN:
    """英文標記測試"""

    def test_angry_en(self):
        """憤怒英文"""
        random.seed(42)
        markers = EmotionTextMarkers(
            prefix_probability=1.0,
            suffix_probability=1.0,
        )
        text = "I can't believe you did that"
        result = markers.apply(text, _make_emo(EMO_ANGRY, 0.5), language="en")
        assert result != text
        assert any(p in result for p in ["Tch", "Hmph", "!"])

    def test_sad_en(self):
        """悲傷英文"""
        random.seed(42)
        markers = EmotionTextMarkers(
            prefix_probability=1.0,
            suffix_probability=1.0,
        )
        text = "I don't know what to do anymore"
        result = markers.apply(text, _make_emo(EMO_SAD, 0.5), language="en")
        assert result != text
        assert any(p in result for p in ["sigh", "..."])

    def test_happy_en(self):
        """開心英文"""
        random.seed(42)
        markers = EmotionTextMarkers(
            prefix_probability=1.0,
            suffix_probability=1.0,
        )
        text = "That sounds great"
        result = markers.apply(text, _make_emo(EMO_HAPPY, 0.5), language="en")
        assert result != text


class TestEmotionTextMarkersConfig:
    """Config 建構測試"""

    def test_create_from_config(self):
        """從 dict config 建構"""
        cfg = {
            "emotion_text_markers": {
                "enabled": True,
                "activation_threshold": 0.15,
                "prefix_probability": 0.9,
                "mid_probability": 0.5,
                "suffix_probability": 0.8,
            }
        }
        markers = create_emotion_text_markers(cfg)
        assert markers.enabled is True
        assert markers.activation_threshold == 0.15
        assert markers.prefix_probability == 0.9

    def test_create_default(self):
        """空 config 用預設值"""
        markers = create_emotion_text_markers({})
        assert markers.enabled is True
        assert markers.activation_threshold == 0.10


class TestEmotionTextMarkersDeterminism:
    """確定性測試（固定種子）"""

    def test_same_seed_same_result(self):
        """相同種子應產生相同結果"""
        markers = EmotionTextMarkers(
            prefix_probability=1.0,
            suffix_probability=1.0,
        )
        emo = _make_emo(EMO_ANGRY, 0.5)

        random.seed(123)
        r1 = markers.apply("你怎麼又遲到了", emo, language="zh")

        random.seed(123)
        r2 = markers.apply("你怎麼又遲到了", emo, language="zh")

        assert r1 == r2
