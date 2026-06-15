"""
ResponseVerifier — 回覆品質驗證器

對應 OpenSoul soul/gating/verifier.py。
四維度加權打分，動態門檻由 neurochem.serotonin 決定。

公式（對齊 OpenSoul）：
  score = quality×0.2 + consistency×0.3 + rules×0.3 + novelty×0.2 - contradiction_penalty
  threshold = 0.5 + serotonin×0.4（verification_threshold 衍生屬性）
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

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
    結果交給 SubconsciousInhibitor 做 PASS/REVISE/SUPPRESS 決策。
    """

    def __init__(self, neurochem: NeurochemState):
        """
        Args:
            neurochem: 當前神經化學狀態（用於動態門檻計算）
        """
        self._neurochem = neurochem

    def verify(
        self,
        response_text: str,
        memory_ctx: MemoryContext | None = None,
    ) -> VerificationResult:
        """
        驗證回覆品質。

        四維度（對齊 OpenSoul）：
          1. 基礎品質 (0.20)：非空、最低長度、結構完整
          2. 概念一致性 (0.30)：回覆是否與記憶脈絡一致
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
            f"[ResponseVerifier] score={score:.3f}, "
            f"threshold={threshold:.3f}, passed={result.passed}"
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
        概念一致性評分。

        對齊 OpenSoul：回覆是否提到已知概念。
        無記憶脈絡時給予中性分數 0.7（不懲罰）。
        """
        if not memory_ctx or not memory_ctx.concepts:
            return 0.7  # 中性，不懲罰

        # 檢查回覆是否提到已知概念
        text_lower = text.lower()
        mentioned = 0
        for concept in memory_ctx.concepts:
            if concept.name.lower() in text_lower:
                mentioned += 1

        if mentioned > 0:
            return min(1.0, 0.7 + mentioned * 0.1)
        return 0.7  # 未提及但不懲罰

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
