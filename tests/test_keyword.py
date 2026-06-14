"""
T-12: Keyword Detector 測試
"""

import pytest
from echo_stt.keyword.detector import SmartKeywordDetector, KeywordMatch


@pytest.fixture
def detector():
    return SmartKeywordDetector()


class TestKeywordDetector:
    def test_uep_mention_activates(self, detector):
        """UEP 提及應觸發啟動"""
        activated, matches = detector.should_activate("hey UEP help me")
        assert activated is True
        assert len(matches) > 0

    def test_help_request_activates(self, detector):
        """幫助請求應觸發啟動"""
        activated, matches = detector.should_activate("can you help me please")
        assert activated is True

    def test_short_text_no_activation(self, detector):
        """過短的文字不應觸發"""
        activated, matches = detector.should_activate("a")
        assert activated is False
        assert len(matches) == 0

    def test_empty_text_no_activation(self, detector):
        """空文字不應觸發"""
        activated, matches = detector.should_activate("")
        assert activated is False

    def test_random_text_no_activation(self, detector):
        """無關文字不應觸發"""
        activated, matches = detector.should_activate("the weather is nice today")
        assert activated is False

    def test_question_pattern(self, detector):
        """問句模式應被偵測"""
        match = detector.check_question_pattern("what is the time?")
        assert match.matched is True

    def test_calling_expression(self, detector):
        """呼叫表達應被偵測"""
        match = detector.check_calling_expression("hey there")
        assert match.matched is True

    def test_uep_fuzzy_match(self, detector):
        """UEP 模糊匹配（u e p 空格分隔觸發 fuzzy regex）"""
        match = detector.check_uep_mention("say you e p now")
        assert match.matched is True
        # "you e p" 在 uep_fuzzy_patterns 的 regex 中命中
        assert match.match_type in ("exact", "fuzzy")

    def test_uep_exact_variant(self, detector):
        """UEP 精確變體匹配"""
        match = detector.check_uep_mention("yep that is correct")
        assert match.matched is True
        assert match.match_type == "exact"  # "yep" 在 uep_variants 列表中

    def test_activation_reason(self, detector):
        """啟動原因應回傳字串"""
        _, matches = detector.should_activate("hey UEP")
        reason = detector.get_activation_reason(matches)
        assert isinstance(reason, str)
        assert len(reason) > 0

    def test_normalize_text(self, detector):
        """文字正規化：移除標點、轉小寫、正規化空白"""
        result = detector.normalize_text("  Hello, World  ")
        assert result == "hello world"

        # 標點符號被替換為空格後，strip 移除首尾
        result2 = detector.normalize_text("Hello!")
        assert "hello" in result2
