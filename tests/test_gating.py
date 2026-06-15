"""
品質閘門測試 — ResponseVerifier + SubconsciousInhibitor + JudgeAgent

驗收標準：
  1. ResponseVerifier 四維度打分在 [0, 1]
  2. 動態門檻隨 serotonin 變化
  3. SubconsciousInhibitor 三態決策正確
  4. SUPPRESS 觸發 neurochem 懲罰
  5. JudgeAgent 規則過濾正確
"""

from __future__ import annotations

import pytest

from echo_memory.affect.verifier import ResponseVerifier, VerificationResult
from echo_memory.affect.inhibitor import (
    SubconsciousInhibitor,
    InhibitionAction,
)
from echo_memory.affect.judge import JudgeAgent
from echo_memory.neurochem.state import NeurochemState
from echo_memory.schemas.memory_context import MemoryContext


# ─── ResponseVerifier ──────────────────────────────────────────────


class TestResponseVerifier:
    def test_score_in_range(self):
        nc = NeurochemState(serotonin=0.5)
        v = ResponseVerifier(nc)
        result = v.verify("這是一個正常的回覆。")
        assert 0.0 <= result.score <= 1.0

    def test_empty_response_low_quality(self):
        nc = NeurochemState(serotonin=0.5)
        v = ResponseVerifier(nc)
        result = v.verify("")
        assert result.details["quality"] == 0.0
        assert result.passed is False
        assert result.score < result.threshold

    def test_short_response_low_quality(self):
        nc = NeurochemState(serotonin=0.5)
        v = ResponseVerifier(nc)
        result = v.verify("abc")
        assert result.score < v.verify("這是一個正常的完整回覆，包含足夠的資訊。").score

    def test_repeated_chars_low_quality(self):
        nc = NeurochemState(serotonin=0.5)
        v = ResponseVerifier(nc)
        result = v.verify("aaaaaaaaaaaaaaaa")
        # quality 維度對重複字元給低分
        assert result.details["quality"] < 0.5
        normal = v.verify("這是一個正常的完整回覆。")
        assert result.score < normal.score

    def test_fake_tool_marker_penalized(self):
        nc = NeurochemState(serotonin=0.5)
        v = ResponseVerifier(nc)
        clean = v.verify("這是正常的回覆。")
        fake = v.verify("[tool_call: something] 這是正常的回覆。")
        assert fake.score < clean.score

    def test_threshold_rises_with_serotonin(self):
        low_ht = NeurochemState(serotonin=0.2)
        high_ht = NeurochemState(serotonin=0.9)

        v_low = ResponseVerifier(low_ht)
        v_high = ResponseVerifier(high_ht)

        r_low = v_low.verify("正常回覆。")
        r_high = v_high.verify("正常回覆。")

        assert r_high.threshold > r_low.threshold

    def test_no_memory_ctx_neutral_consistency(self):
        """無記憶脈絡時 consistency 不懲罰。"""
        nc = NeurochemState(serotonin=0.5)
        v = ResponseVerifier(nc)
        result = v.verify("正常回覆。", memory_ctx=None)
        assert result.details["consistency"] == 0.7

    def test_passed_flag(self):
        nc = NeurochemState(serotonin=0.1)  # 低門檻
        v = ResponseVerifier(nc)
        result = v.verify("這是一個足夠好的回覆，應該能通過驗證。")
        # 低 serotonin → 低門檻 → 容易通過
        assert result.passed is True

    def test_details_keys(self):
        nc = NeurochemState()
        v = ResponseVerifier(nc)
        result = v.verify("測試回覆。")
        assert "quality" in result.details
        assert "consistency" in result.details
        assert "rules" in result.details
        assert "knowledge" in result.details
        assert "contradiction_penalty" in result.details
        assert "threshold" in result.details

    def test_margin_property(self):
        nc = NeurochemState(serotonin=0.5)
        v = ResponseVerifier(nc)
        result = v.verify("正常回覆。")
        assert result.margin == pytest.approx(result.score - result.threshold)

    # ─── Grounded Verification（向量語意比對）───────────────────

    def test_grounded_no_embed_fn_neutral(self):
        """embed_fn 為 None 時，一致性給中性分數。"""
        nc = NeurochemState(serotonin=0.5)
        v = ResponseVerifier(nc, embed_fn=None)
        from echo_memory.schemas.episode import Episode
        ctx = MemoryContext(episodes=[Episode(
            user_input="量子計算", agent_response="量子計算是一種新型計算範式",
            session_id="s1", content_summary="量子計算",
        )])
        result = v.verify("量子計算利用量子位元進行運算", memory_ctx=ctx)
        assert result.details["consistency"] == 0.7

    def test_grounded_high_similarity(self):
        """回覆與記憶高度相似時，一致性分數接近 1.0。"""
        # 使用固定向量模擬高相似度
        call_count = {"n": 0}
        def mock_embed(text):
            call_count["n"] += 1
            # 所有文本回傳相似向量
            return [1.0, 0.0, 0.0, 0.0]

        nc = NeurochemState(serotonin=0.5)
        v = ResponseVerifier(nc, embed_fn=mock_embed)
        from echo_memory.schemas.episode import Episode
        ctx = MemoryContext(episodes=[Episode(
            user_input="測試", agent_response="相關回覆",
            session_id="s1", content_summary="測試",
        )])
        result = v.verify("相關回覆內容", memory_ctx=ctx)
        # 相同向量 → cosine sim = 1.0 → consistency = 1.0
        assert result.details["consistency"] == 1.0

    def test_grounded_low_similarity(self):
        """回覆與記憶低相似度時，一致性分數較低但不嚴重懲罰。"""
        call_count = {"n": 0}
        def mock_embed(text):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return [1.0, 0.0, 0.0, 0.0]  # 回覆向量
            return [0.0, 1.0, 0.0, 0.0]  # 記憶向量（正交）

        nc = NeurochemState(serotonin=0.5)
        v = ResponseVerifier(nc, embed_fn=mock_embed)
        from echo_memory.schemas.episode import Episode
        ctx = MemoryContext(episodes=[Episode(
            user_input="測試", agent_response="完全不同的內容",
            session_id="s1", content_summary="測試",
        )])
        result = v.verify("毫不相關的回覆", memory_ctx=ctx)
        # 正交向量 → cosine sim ≈ 0 → consistency ≈ 0.5
        assert result.details["consistency"] < 0.7

    def test_grounded_embed_failure_degrades(self):
        """embed_fn 拋異常時降級為中性分數。"""
        def broken_embed(text):
            raise RuntimeError("embedding 服務掛了")

        nc = NeurochemState(serotonin=0.5)
        v = ResponseVerifier(nc, embed_fn=broken_embed)
        from echo_memory.schemas.episode import Episode
        ctx = MemoryContext(episodes=[Episode(
            user_input="測試", agent_response="回覆",
            session_id="s1", content_summary="測試",
        )])
        result = v.verify("某個回覆", memory_ctx=ctx)
        assert result.details["consistency"] == 0.7


# ─── SubconsciousInhibitor ─────────────────────────────────────────


class TestSubconsciousInhibitor:
    def _make_result(
        self, score: float, threshold: float
    ) -> VerificationResult:
        return VerificationResult(
            score=score,
            threshold=threshold,
            passed=score >= threshold,
            details={},
        )

    def test_pass_decision(self):
        nc = NeurochemState()
        inhibitor = SubconsciousInhibitor(nc)
        vr = self._make_result(score=0.9, threshold=0.7)

        decision = inhibitor.gate("好回覆", vr)
        assert decision.action == InhibitionAction.PASS
        assert decision.should_retry is False
        assert decision.text == "好回覆"

    def test_revise_decision(self):
        nc = NeurochemState()
        inhibitor = SubconsciousInhibitor(nc)
        # score < threshold 但 >= threshold×0.7
        vr = self._make_result(score=0.55, threshold=0.7)  # 0.55 >= 0.49

        decision = inhibitor.gate("普通回覆", vr)
        assert decision.action == InhibitionAction.REVISE
        assert decision.should_retry is False
        assert "⚠️" in decision.text  # 有免責標記

    def test_suppress_decision_first_attempt(self):
        nc = NeurochemState(dopamine=0.5)
        inhibitor = SubconsciousInhibitor(nc, max_retries=1)
        # score < threshold×0.7
        vr = self._make_result(score=0.3, threshold=0.7)  # 0.3 < 0.49

        decision = inhibitor.gate("差回覆", vr, attempt=0)
        assert decision.action == InhibitionAction.SUPPRESS
        assert decision.should_retry is True  # 第一次可以重試
        assert nc.dopamine < 0.5  # on_failure 被呼叫，DA 下降

    def test_suppress_no_retry_after_max(self):
        nc = NeurochemState()
        inhibitor = SubconsciousInhibitor(nc, max_retries=1)
        vr = self._make_result(score=0.2, threshold=0.7)

        decision = inhibitor.gate("差回覆", vr, attempt=1)
        assert decision.action == InhibitionAction.SUPPRESS
        assert decision.should_retry is False  # 已達重試上限
        assert decision.text != "差回覆"
        assert "無法產生足夠可靠" in decision.text

    def test_suppress_neurochem_penalty(self):
        nc = NeurochemState(dopamine=0.6, serotonin=0.4)
        inhibitor = SubconsciousInhibitor(nc)
        vr = self._make_result(score=0.1, threshold=0.7)

        da_before = nc.dopamine
        inhibitor.gate("很差的回覆", vr)
        assert nc.dopamine < da_before  # DA 被懲罰


# ─── JudgeAgent ────────────────────────────────────────────────────


class TestJudgeAgent:
    def _make_tools(self) -> list[dict]:
        return [
            {"name": "force_remember", "description": "強制記憶"},
            {"name": "deep_recall", "description": "深度搜尋"},
            {"name": "adjust_importance", "description": "調整重要性"},
            {"name": "trigger_dream", "description": "觸發夢境"},
        ]

    def test_all_tools_pass_with_context(self):
        """有足夠脈絡時，所有工具都通過。"""
        judge = JudgeAgent()
        from echo_memory.schemas.episode import Episode

        ctx = MemoryContext(
            episodes=[Episode(
                user_input="test", agent_response="test",
                session_id="s1", content_summary="test",
            )],
        )
        result = judge.filter_tools(ctx, self._make_tools(), total_episodes=10)
        assert len(result) == 4

    def test_no_context_removes_adjust(self):
        """無脈絡時移除 adjust_importance。"""
        judge = JudgeAgent()
        result = judge.filter_tools(None, self._make_tools(), total_episodes=10)
        names = [t["name"] for t in result]
        assert "adjust_importance" not in names

    def test_few_episodes_removes_dream(self):
        """記憶太少時移除 trigger_dream。"""
        judge = JudgeAgent()
        result = judge.filter_tools(None, self._make_tools(), total_episodes=3)
        names = [t["name"] for t in result]
        assert "trigger_dream" not in names

    def test_few_episodes_removes_deep_recall(self):
        """記憶太少時移除 deep_recall。"""
        judge = JudgeAgent()
        result = judge.filter_tools(None, self._make_tools(), total_episodes=1)
        names = [t["name"] for t in result]
        assert "deep_recall" not in names

    def test_empty_tools_returns_empty(self):
        judge = JudgeAgent()
        result = judge.filter_tools(None, [], total_episodes=10)
        assert result == []

    def test_force_remember_always_available(self):
        """force_remember 不受限制。"""
        judge = JudgeAgent()
        result = judge.filter_tools(None, self._make_tools(), total_episodes=0)
        names = [t["name"] for t in result]
        assert "force_remember" in names

    # ─── Hybrid LLM 推薦測試 ────────────────────────────────────

    def test_recommend_no_llm_returns_rules(self):
        """llm_fn 為 None 時，recommend_tool 回退為規則結果。"""
        judge = JudgeAgent(llm_fn=None)
        rec = judge.recommend_tool("測試", None, self._make_tools())
        assert rec.source == "rules"
        assert rec.recommended_tool == "none"
        assert rec.confidence == 0.0

    def test_recommend_llm_success(self):
        """LLM 回傳有效 JSON 時，正確解析推薦。"""
        def mock_llm(prompt):
            return '{"recommended_tool": "deep_recall", "reasoning": "需要搜尋", "confidence": 0.8}'

        judge = JudgeAgent(llm_fn=mock_llm)
        rec = judge.recommend_tool("幫我找之前的對話", None, self._make_tools())
        assert rec.source == "llm"
        assert rec.recommended_tool == "deep_recall"
        assert rec.confidence == 0.8

    def test_recommend_llm_low_confidence(self):
        """LLM 信心不足時，推薦結果為 none。"""
        def mock_llm(prompt):
            return '{"recommended_tool": "deep_recall", "reasoning": "不太確定", "confidence": 0.3}'

        judge = JudgeAgent(llm_fn=mock_llm)
        rec = judge.recommend_tool("隨便聊聊", None, self._make_tools())
        assert rec.recommended_tool == "none"
        assert rec.confidence == 0.3

    def test_recommend_llm_failure_degrades(self):
        """LLM 呼叫拋異常時，降級為規則結果。"""
        def mock_llm(prompt):
            raise RuntimeError("LLM 掛了")

        judge = JudgeAgent(llm_fn=mock_llm)
        rec = judge.recommend_tool("測試", None, self._make_tools())
        assert rec.source == "rules"
        assert rec.recommended_tool == "none"

    def test_recommend_llm_bad_json_degrades(self):
        """LLM 回傳無效格式時，降級為規則結果。"""
        def mock_llm(prompt):
            return "我不知道該怎麼回答"

        judge = JudgeAgent(llm_fn=mock_llm)
        rec = judge.recommend_tool("測試", None, self._make_tools())
        assert rec.source == "rules"
        assert rec.recommended_tool == "none"

    def test_recommend_empty_tools(self):
        """無可用工具時回傳 none。"""
        judge = JudgeAgent(llm_fn=lambda p: "不應該被呼叫")
        rec = judge.recommend_tool("測試", None, [])
        assert rec.recommended_tool == "none"
        assert rec.source == "rules"

    def test_recommend_rejects_invalid_tool(self):
        """LLM 推薦不存在的工具時，回退為 none。"""
        def mock_llm(prompt):
            return '{"recommended_tool": "delete_everything", "reasoning": "刪掉", "confidence": 0.99}'

        judge = JudgeAgent(llm_fn=mock_llm)
        rec = judge.recommend_tool("刪掉所有記憶", None, self._make_tools())
        assert rec.recommended_tool == "none"
