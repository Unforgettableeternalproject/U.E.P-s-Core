"""
後處理管線測試 — HallucinationFilter + ChineseConverter + PostProcessor
"""

from __future__ import annotations

import pytest

from echo_stt.pipeline.post_process import (
    HallucinationFilter,
    ChineseConverter,
    PostProcessor,
)


class TestHallucinationFilter:

    def test_normal_text_passes(self):
        f = HallucinationFilter()
        assert f.filter("你好世界", confidence=0.9) == "你好世界"

    def test_empty_text_rejected(self):
        f = HallucinationFilter()
        assert f.filter("", confidence=0.9) is None
        assert f.filter("  ", confidence=0.9) is None

    def test_low_confidence_rejected(self):
        f = HallucinationFilter(min_confidence=0.1)
        assert f.filter("some text", confidence=0.03) is None
        assert f.filtered_count == 1

    def test_consecutive_repeats_detected(self):
        """連續重複：「什麼 什麼 什麼 什麼 什麼」"""
        f = HallucinationFilter(max_repeat_count=4)
        assert f.filter("什麼 什麼 什麼 什麼 什麼 什麼", confidence=0.5) is None

    def test_consecutive_repeats_no_space(self):
        """中文無空格重複：「什麼什麼什麼什麼什麼」"""
        f = HallucinationFilter(max_repeat_count=4)
        assert f.filter("什麼什麼什麼什麼什麼什麼", confidence=0.5) is None

    def test_english_repeats(self):
        """英文重複：「the the the the the」"""
        f = HallucinationFilter(max_repeat_count=4)
        assert f.filter("the the the the the the", confidence=0.5) is None

    def test_short_repeats_pass(self):
        """少量重複不觸發"""
        f = HallucinationFilter(max_repeat_count=5)
        assert f.filter("好 好 好", confidence=0.9) == "好 好 好"

    def test_high_repeat_ratio(self):
        """整體重複比例過高"""
        f = HallucinationFilter(max_repeat_ratio=0.4)
        # 10 個 token 中 6 個相同 = 0.6 > 0.4
        text = "你好 你好 你好 你好 你好 你好 世界 嗨 哈 嘿"
        assert f.filter(text, confidence=0.9) is None

    def test_normal_ratio_passes(self):
        """正常重複比例通過"""
        f = HallucinationFilter(max_repeat_ratio=0.4)
        text = "今天天氣很好 我想出去走走 吃點東西 然後回家"
        assert f.filter(text, confidence=0.9) is not None

    def test_massive_hallucination(self):
        """真實場景：Whisper base 的「什麼」×100"""
        f = HallucinationFilter()
        text = " ".join(["什麼"] * 100)
        assert f.filter(text, confidence=0.3) is None


class TestChineseConverter:

    def test_s2twp_conversion(self):
        conv = ChineseConverter("s2twp")
        if not conv.is_available:
            pytest.skip("opencc not installed")
        assert conv.convert("这个很好") == "這個很好"
        # s2twp 至少做字元級轉換
        result = conv.convert("计算机")
        assert "計算" in result  # 字元轉換一定會發生

    def test_empty_string(self):
        conv = ChineseConverter("s2twp")
        assert conv.convert("") == ""

    def test_already_traditional(self):
        conv = ChineseConverter("s2twp")
        if not conv.is_available:
            pytest.skip("opencc not installed")
        # 繁體字不應被改變（或只做用詞轉換）
        result = conv.convert("這個很好")
        assert "這個" in result

    def test_unavailable_fallback(self):
        """OpenCC 不可用時原樣回傳"""
        conv = ChineseConverter("nonexistent_scheme")
        assert conv.convert("hello") == "hello"


class TestPostProcessor:

    def test_full_pipeline(self):
        pp = PostProcessor(
            hallucination_filter=True,
            chinese_convert="s2twp",
        )
        if not pp.chinese_converter_available:
            pytest.skip("opencc not installed")

        # 正常中文（簡→繁）
        result = pp.process("这个很好", confidence=0.9)
        assert result == "這個很好"

    def test_hallucination_blocked(self):
        pp = PostProcessor(hallucination_filter=True)
        result = pp.process(" ".join(["什麼"] * 50), confidence=0.3)
        assert result is None
        assert pp.hallucination_filtered_count == 1

    def test_no_processing(self):
        pp = PostProcessor(hallucination_filter=False, chinese_convert=None)
        assert pp.process("hello", confidence=0.1) == "hello"

    def test_from_config(self):
        config = {
            "post_processing": {
                "hallucination_filter": True,
                "max_repeat_count": 3,
                "chinese_convert": "s2twp",
            }
        }
        pp = PostProcessor.from_config(config)
        assert pp._hallu_filter is not None
        assert pp._hallu_filter.max_repeat_count == 3

    def test_from_empty_config(self):
        pp = PostProcessor.from_config({})
        assert pp._hallu_filter is not None  # 預設開啟
        assert pp._chinese_conv is None  # 預設不轉換
