"""
ResponseVerifier — 回覆品質驗證器

對應 OpenSoul soul/gating/verifier.py。
四維度加權打分，動態門檻由 neurochem.serotonin 決定。

公式（對齊 OpenSoul）：
  score = quality×0.2 + consistency×0.3 + rules×0.3 + novelty×0.2 - contradiction_penalty
  threshold = 0.5 + serotonin×0.4（verification_threshold 衍生屬性）

一致性評分使用向量語意比對（Grounded Verification），
而非 keyword matching，以符合架構禁令。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

from echo_memory.neurochem.state import NeurochemState
from echo_memory.schemas.memory_context import MemoryContext

logger = logging.getLogger("echo_memory.affect.verifier")


@dataclass
class VerificationResult:
    """品質驗證結果。"""

    score: float           # 綜合品質分數 [0, 1]
    threshold: float       # 動態門檻（由 serotonin 決定）
    passed: bool           # score >= threshold
    details: dict          # 各維度明細

    @property
    def margin(self) -> float:
        """分數與門檻的差距。正值 = 通過，負值 = 未通過。"""
        return self.score - self.threshold


class ResponseVerifier:
    """
    回覆品質驗證器。

    在 LLM 生成回覆後、發送前執行驗證。
    結果交給 ResponseInhibitor 做 PASS/REVISE/SUPPRESS 決策。
    """

    def __init__(
        self,
        neurochem: NeurochemState,
        embed_fn: Callable[[str], list[float]] | None = None,
        llm_fn: Callable[[str], str] | None = None,
    ):
        """
        Args:
            neurochem: 當前神經化學狀態（用於動態門檻計算）
            embed_fn: 文本嵌入函式（用於語意一致性比對）。
                      為 None 時一致性評分使用中性預設值。
            llm_fn: LLM 呼叫函式（用於 Layer 2 語意矛盾偵測）。
                    為 None 時只做 Layer 1 embedding 比對。
        """
        self._neurochem = neurochem
        self._embed_fn = embed_fn
        self._llm_fn = llm_fn

    def verify(
        self,
        response_text: str,
        memory_ctx: MemoryContext | None = None,
    ) -> VerificationResult:
        """
        驗證回覆品質。

        四維度（對齊 OpenSoul）：
          1. 基礎品質 (0.20)：非空、最低長度、結構完整
          2. 概念一致性 (0.30)：回覆與召回記憶的語意相關度
          3. 規則遵守 (0.30)：是否包含偽造工具標記等違規
          4. 新知識保留 (0.20)：固定 1.0（保留彈性空間）
          - 矛盾懲罰：-0.10/個，上限 -0.50

        Args:
            response_text: LLM 生成的回覆文本
            memory_ctx: 記憶脈絡（用於一致性檢查）

        Returns:
            VerificationResult
        """
        # ── 基礎品質 (0.20) ─────────────────────────────────────────
        quality = self._score_quality(response_text)

        # ── 概念一致性 (0.30) ───────────────────────────────────────
        consistency = self._score_consistency(response_text, memory_ctx)

        # ── 規則遵守 (0.30) ─────────────────────────────────────────
        rules = self._score_rules(response_text)

        # ── 新知識保留 (0.20) ───────────────────────────────────────
        knowledge = 1.0  # 保留彈性空間

        # ── 矛盾懲罰 ───────────────────────────────────────────────
        contradiction_penalty = self._contradiction_penalty(memory_ctx)

        # 加權求和
        score = (
            quality * 0.2
            + consistency * 0.3
            + rules * 0.3
            + knowledge * 0.2
            - contradiction_penalty
        )
        score = max(0.0, min(1.0, score))

        # 動態門檻
        threshold = self._neurochem.verification_threshold

        # 空回覆不能靠其他維度的預設分數通過品質閘門。
        if quality == 0.0:
            score = min(score, max(0.0, threshold - 0.01))

        result = VerificationResult(
            score=score,
            threshold=threshold,
            passed=score >= threshold,
            details={
                "quality": round(quality, 3),
                "consistency": round(consistency, 3),
                "rules": round(rules, 3),
                "knowledge": round(knowledge, 3),
                "contradiction_penalty": round(contradiction_penalty, 3),
                "threshold": round(threshold, 3),
            },
        )

        logger.debug(
            "[ResponseVerifier] score=%.3f, threshold=%.3f, passed=%s",
            score,
            threshold,
            result.passed,
        )

        return result

    def _score_quality(self, text: str) -> float:
        """
        基礎品質評分。

        對齊 OpenSoul：非空、長度 ≥ 5、無重複字元崩潰。
        """
        if not text or not text.strip():
            return 0.0

        stripped = text.strip()

        # 最低長度
        if len(stripped) < 5:
            return 0.2

        # 檢查重複字元崩潰（如 "aaaaaaaaa"）
        if len(set(stripped)) < max(2, len(stripped) * 0.1):
            return 0.1

        # 合理長度加分
        if len(stripped) > 20:
            return 1.0
        return 0.7

    def _score_consistency(
        self,
        text: str,
        memory_ctx: MemoryContext | None,
    ) -> float:
        """
        概念一致性評分（Grounded Verification）。

        使用向量語意比對：計算回覆 embedding 與已召回記憶的
        cosine similarity，以判斷回覆是否有記憶支持。

        不使用 keyword matching，符合架構禁令。

        評分邏輯：
          - 無記憶脈絡 → 中性 0.7（不懲罰）
          - 無 embed_fn → 中性 0.7（降級，不懲罰）
          - 有記憶且有 embedding → 取最高 cosine sim 映射到 [0.5, 1.0]
        """
        if not memory_ctx or not memory_ctx.episodes:
            return 0.7  # 中性，不懲罰

        if self._embed_fn is None:
            return 0.7  # 降級：無 embedding 時不做語意比對

        # 嘗試向量比對
        try:
            response_vec = np.array(
                self._embed_fn(text[:500]), dtype=np.float32
            )
            resp_norm = np.linalg.norm(response_vec)
            if resp_norm == 0:
                return 0.7

            # 與已召回 episode 的 agent_response 做語意比對
            best_sim = 0.0
            for ep in memory_ctx.episodes[:10]:  # 限制數量避免過慢
                ref_text = ep.agent_response or ep.user_input or ""
                if not ref_text.strip():
                    continue

                ref_vec = np.array(
                    self._embed_fn(ref_text[:500]), dtype=np.float32
                )
                ref_norm = np.linalg.norm(ref_vec)
                if ref_norm == 0:
                    continue

                sim = float(np.dot(response_vec, ref_vec) / (resp_norm * ref_norm))
                best_sim = max(best_sim, sim)

            # ── Layer 1 結果：映射 cosine sim 到一致性分數 ──
            # sim >= 0.7 → 1.0（高度相關）
            # sim ~= 0.3 → 0.7（中性）
            # sim < 0.1 → 0.5（略低但不嚴重懲罰）
            if best_sim >= 0.7:
                layer1_score = 1.0
            elif best_sim >= 0.3:
                layer1_score = 0.7 + (best_sim - 0.3) * (0.3 / 0.4)
            else:
                layer1_score = 0.5 + best_sim * (0.2 / 0.3)

            # ── Layer 2：LLM 矛盾偵測 ──
            # 只在 Layer 1 通過（高相關 sim >= 0.5）且 LLM 可用時觸發。
            # cosine 高只表示主題相關，不表示沒矛盾：
            # 「蝦過敏」和「推薦吃蝦」的 embedding 非常相似但邏輯矛盾。
            if (
                best_sim >= 0.5
                and self._llm_fn is not None
                and layer1_score >= 0.7
            ):
                contradiction = self._llm_contradiction_check(
                    text, memory_ctx
                )
                if contradiction:
                    logger.info(
                        "[ResponseVerifier] Layer 2 偵測到矛盾，"
                        "一致性分數降至 0.2"
                    )
                    return 0.2  # 嚴重懲罰

            return layer1_score

        except Exception as e:
            logger.warning(
                "[ResponseVerifier] 語意一致性計算失敗，使用中性預設值: %s", e
            )
            return 0.7  # 失敗降級

    def _llm_contradiction_check(
        self,
        response_text: str,
        memory_ctx: MemoryContext,
    ) -> bool:
        """
        Layer 2：LLM 矛盾偵測。

        只在 Layer 1 通過且 LLM 可用時呼叫。
        判斷回覆是否與已知記憶有邏輯矛盾。

        Returns:
            True = 偵測到矛盾，False = 無矛盾或無法判斷
        """
        # 收集記憶摘要（限制長度）
        memory_lines = []
        for ep in memory_ctx.episodes[:5]:
            summary = ep.content_summary or ep.user_input[:100]
            memory_lines.append(f"- {summary}")
        memories_text = "\n".join(memory_lines)

        prompt = (
            "你是記憶一致性驗證器。判斷以下回覆是否與已知記憶有邏輯矛盾。\n\n"
            f"已知記憶：\n{memories_text}\n\n"
            f"待驗證回覆：\n{response_text[:300]}\n\n"
            '請以純 JSON 格式回覆：\n'
            '{"has_contradiction": true/false, "reason": "簡短說明"}\n\n'
            "規則：\n"
            "- 只判斷邏輯矛盾，不判斷風格或語氣差異\n"
            "- 「記憶說 A 過敏，回覆卻推薦 A」= 矛盾\n"
            "- 「記憶沒提到 B，回覆提到 B」= 不算矛盾（新知識）\n"
            "- 不確定時回 false"
        )

        try:
            import json
            raw = self._llm_fn(prompt)  # type: ignore[misc]
            # 嘗試解析
            parsed = None
            try:
                parsed = json.loads(raw.strip())
            except json.JSONDecodeError:
                import re
                match = re.search(r"\{.*?\}", raw, re.DOTALL)
                if match:
                    try:
                        parsed = json.loads(match.group(0))
                    except json.JSONDecodeError:
                        pass

            if parsed and isinstance(parsed, dict):
                result = bool(parsed.get("has_contradiction", False))
                if result:
                    reason = parsed.get("reason", "")
                    logger.info(
                        "[ResponseVerifier] LLM 矛盾偵測: %s", reason
                    )
                return result

        except Exception as e:
            logger.warning(
                "[ResponseVerifier] LLM 矛盾偵測失敗: %s", e
            )

        return False  # 不確定時不懲罰

    def _score_rules(self, text: str) -> float:
        """
        規則遵守評分。

        檢查是否包含偽造工具標記。
        """
        import re

        # 偽造工具標記模式（對齊 OpenSoul 內容消殺）
        fake_patterns = [
            r"\[tool_?call[:\s]",
            r"\[tool_?result[:\s]",
            r"<tool_use>",
            r"</tool_use>",
            r"function_call\s*:",
            r"\[🛠️.*?實際執行",
            r"\[系統已.*?執行",
        ]

        violations = 0
        for pattern in fake_patterns:
            if re.search(pattern, text, re.IGNORECASE):
                violations += 1

        if violations > 0:
            return max(0.0, 1.0 - violations * 0.3)
        return 1.0

    def _contradiction_penalty(
        self,
        memory_ctx: MemoryContext | None,
    ) -> float:
        """
        矛盾懲罰。

        對齊 OpenSoul：每個矛盾 -0.10，上限 -0.50。
        """
        if not memory_ctx or not memory_ctx.concepts:
            return 0.0

        # 檢查 MemoryContext 中是否有標記為矛盾的概念
        contradiction_count = 0
        for concept in memory_ctx.concepts:
            if concept.metadata.get("has_contradiction"):
                contradiction_count += 1

        return min(0.5, contradiction_count * 0.1)
