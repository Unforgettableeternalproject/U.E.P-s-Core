"""
ResponseVerifier 矛盾偵測 Regression Test

固化「蝦過敏卻推薦吃蝦」場景，確認：
  1. 有 llm_fn 時 Layer 2 矛盾偵測能攔截
  2. consistency <= 0.2 強制不通過
  3. 無 llm_fn 時退化為 Layer 1（已知限制：無法偵測語意矛盾）
  4. 無 embed_fn 時完全退化（中性分數，不懲罰）

來源：Codex #4 + 米絲媞 Deep Review
"""

from __future__ import annotations

import json

import numpy as np

from echo_memory.affect.verifier import ResponseVerifier
from echo_memory.neurochem.state import NeurochemState
from echo_memory.schemas.episode import Episode
from echo_memory.schemas.memory_context import MemoryContext


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 測試基礎設施
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# 模擬 embedding：蝦過敏記憶和推薦吃蝦的回覆在 embedding 空間中高度相似
# （主題都是「蝦」），但邏輯上是矛盾的。
# 用固定向量模擬 cosine similarity ~= 0.95（高度相似）
_SHRIMP_ALLERGY_VEC = np.array([0.9, 0.3, 0.1, 0.2], dtype=np.float32)
_SHRIMP_ALLERGY_VEC = _SHRIMP_ALLERGY_VEC / np.linalg.norm(_SHRIMP_ALLERGY_VEC)

_RECOMMEND_SHRIMP_VEC = np.array([0.88, 0.35, 0.12, 0.18], dtype=np.float32)
_RECOMMEND_SHRIMP_VEC = _RECOMMEND_SHRIMP_VEC / np.linalg.norm(_RECOMMEND_SHRIMP_VEC)


def _mock_embed_fn(text: str) -> list[float]:
    """模擬 embedding：根據文本內容回傳預設向量。"""
    if "過敏" in text or "不能吃" in text:
        return _SHRIMP_ALLERGY_VEC.tolist()
    if "推薦" in text or "建議" in text or "吃蝦" in text:
        return _RECOMMEND_SHRIMP_VEC.tolist()
    # 預設：回傳與蝦相關的向量（模擬主題相似）
    return _RECOMMEND_SHRIMP_VEC.tolist()


def _make_shrimp_allergy_ctx() -> MemoryContext:
    """建立包含「蝦過敏」記憶的 MemoryContext。"""
    return MemoryContext(
        episodes=[
            Episode(
                user_input="我對蝦嚴重過敏，吃了會休克",
                agent_response="了解，我已記錄你對蝦的嚴重過敏反應，以後推薦食物時會避開蝦類。",
                session_id="test-session",
                content_summary="使用者對蝦嚴重過敏，食用後會休克",
            ),
        ],
    )


_CONTRADICTORY_RESPONSE = "今晚推薦你吃蝦，新鮮的蝦非常好吃又營養豐富！"
_SAFE_RESPONSE = "今晚推薦你吃雞胸肉沙拉，清淡又營養。"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Layer 2: LLM 矛盾偵測有效時
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestContradictionWithLLM:
    """有 llm_fn 時，Layer 2 矛盾偵測應能攔截蝦過敏矛盾。"""

    @staticmethod
    def _llm_detects_contradiction(prompt: str) -> str:
        """模擬 LLM 正確偵測到矛盾。"""
        return json.dumps({
            "has_contradiction": True,
            "reason": "記憶說使用者對蝦嚴重過敏，回覆卻推薦吃蝦",
        })

    @staticmethod
    def _llm_no_contradiction(prompt: str) -> str:
        """模擬 LLM 判定無矛盾。"""
        return json.dumps({
            "has_contradiction": False,
            "reason": "回覆與記憶一致",
        })

    def test_contradiction_detected_fails_verification(self):
        """蝦過敏 + 推薦吃蝦 → LLM 偵測到矛盾 → 驗證不通過。"""
        nc = NeurochemState(serotonin=0.5)
        v = ResponseVerifier(
            nc,
            embed_fn=_mock_embed_fn,
            llm_fn=self._llm_detects_contradiction,
        )
        ctx = _make_shrimp_allergy_ctx()

        result = v.verify(_CONTRADICTORY_RESPONSE, memory_ctx=ctx)

        # consistency 應被強制降至 0.2
        assert result.details["consistency"] <= 0.2
        # 驗證應不通過
        assert result.passed is False

    def test_contradiction_forced_fail_regardless_of_other_scores(self):
        """即使 quality/rules/knowledge 都滿分，consistency <= 0.2 仍強制失敗。"""
        nc = NeurochemState(serotonin=0.3)  # 低門檻，正常情況容易通過
        v = ResponseVerifier(
            nc,
            embed_fn=_mock_embed_fn,
            llm_fn=self._llm_detects_contradiction,
        )
        ctx = _make_shrimp_allergy_ctx()

        result = v.verify(_CONTRADICTORY_RESPONSE, memory_ctx=ctx)

        # 即使門檻很低，矛盾仍強制失敗
        assert result.passed is False
        assert result.score < result.threshold

    def test_safe_response_passes_with_llm(self):
        """蝦過敏 + 推薦雞胸肉 → LLM 判定無矛盾 → 驗證通過。"""
        nc = NeurochemState(serotonin=0.3)
        v = ResponseVerifier(
            nc,
            embed_fn=_mock_embed_fn,
            llm_fn=self._llm_no_contradiction,
        )
        ctx = _make_shrimp_allergy_ctx()

        result = v.verify(_SAFE_RESPONSE, memory_ctx=ctx)

        assert result.details["consistency"] > 0.2
        assert result.passed is True


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# consistency <= 0.2 強制失敗機制
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestConsistencyForcedFail:
    """consistency <= 0.2 強制讓分數低於門檻。"""

    def test_consistency_0_2_forces_below_threshold(self):
        """直接驗證 consistency=0.2 時 score < threshold。"""
        nc = NeurochemState(serotonin=0.5)
        v = ResponseVerifier(
            nc,
            embed_fn=_mock_embed_fn,
            llm_fn=lambda _: json.dumps({"has_contradiction": True, "reason": "test"}),
        )
        ctx = _make_shrimp_allergy_ctx()

        result = v.verify(_CONTRADICTORY_RESPONSE, memory_ctx=ctx)

        # score 應被強制壓到 threshold - 0.01 以下
        assert result.score < result.threshold
        assert result.passed is False

    def test_consistency_0_3_not_forced_fail(self):
        """consistency > 0.2 不會被強制失敗。"""
        nc = NeurochemState(serotonin=0.1)  # 很低的門檻
        # 不提供 llm_fn，Layer 1 會給出 > 0.2 的一致性分數
        v = ResponseVerifier(nc, embed_fn=_mock_embed_fn, llm_fn=None)
        ctx = _make_shrimp_allergy_ctx()

        result = v.verify(_CONTRADICTORY_RESPONSE, memory_ctx=ctx)

        # 沒有 LLM Layer 2，consistency 來自 Layer 1（主題相似 → 高分）
        assert result.details["consistency"] > 0.2


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 降級場景（已知限制）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestContradictionDegradation:
    """無 llm_fn / embed_fn 時的降級行為（已知限制，非 bug）。"""

    def test_no_llm_fn_contradiction_not_caught(self):
        """
        已知限制：無 llm_fn 時 Layer 2 不執行。

        cosine similarity 只判斷主題相關，不判斷 entailment/contradiction。
        「蝦過敏」和「推薦吃蝦」的 embedding 很相似，
        Layer 1 會給出高分，矛盾不會被抓到。
        """
        nc = NeurochemState(serotonin=0.3)
        v = ResponseVerifier(nc, embed_fn=_mock_embed_fn, llm_fn=None)
        ctx = _make_shrimp_allergy_ctx()

        result = v.verify(_CONTRADICTORY_RESPONSE, memory_ctx=ctx)

        # Layer 1 看到高 cosine sim → 給出高一致性分數
        assert result.details["consistency"] > 0.5
        # 矛盾未被偵測，驗證通過（這是已知限制）
        assert result.passed is True

    def test_no_embed_fn_neutral_consistency(self):
        """
        已知限制：無 embed_fn 時一致性完全退化為中性 0.7。
        """
        nc = NeurochemState(serotonin=0.3)
        v = ResponseVerifier(nc, embed_fn=None, llm_fn=None)
        ctx = _make_shrimp_allergy_ctx()

        result = v.verify(_CONTRADICTORY_RESPONSE, memory_ctx=ctx)

        assert result.details["consistency"] == 0.7

    def test_llm_fn_failure_degrades_gracefully(self):
        """LLM 呼叫失敗時不崩潰，降級為 Layer 1 結果。"""
        def broken_llm(prompt: str) -> str:
            raise RuntimeError("LLM 服務不可用")

        nc = NeurochemState(serotonin=0.3)
        v = ResponseVerifier(
            nc, embed_fn=_mock_embed_fn, llm_fn=broken_llm,
        )
        ctx = _make_shrimp_allergy_ctx()

        result = v.verify(_CONTRADICTORY_RESPONSE, memory_ctx=ctx)

        # LLM 失敗 → _llm_contradiction_check 回傳 False → 不懲罰
        assert result.details["consistency"] > 0.2

    def test_llm_returns_garbage_degrades_gracefully(self):
        """LLM 回傳無法解析的內容時不崩潰。"""
        def garbage_llm(prompt: str) -> str:
            return "這不是 JSON，我只是在說話。"

        nc = NeurochemState(serotonin=0.3)
        v = ResponseVerifier(
            nc, embed_fn=_mock_embed_fn, llm_fn=garbage_llm,
        )
        ctx = _make_shrimp_allergy_ctx()

        result = v.verify(_CONTRADICTORY_RESPONSE, memory_ctx=ctx)

        # 無法解析 → 不懲罰
        assert result.details["consistency"] > 0.2
