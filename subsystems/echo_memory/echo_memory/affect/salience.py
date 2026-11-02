"""
SalienceEvaluator — 三值顯著性評估器

對應 OpenSoul soul/affect/salience.py。
取代 Entry.py 中的 _estimate_salience() keyword matching。

公式（對齊 OpenSoul）：
  salience = complexity×0.4 + novelty×0.4 + |feedback|×0.5 + sentiment×0.1 + error_bonus
  da_weight = clamp(dopamine + feedback_bonus, 0, 1)
  ht_weight = serotonin×0.7 + verification_score×0.3
"""

from __future__ import annotations

import logging
import math

from echo_memory.affect.signals import SalienceSignals
from echo_memory.neurochem.state import NeurochemState

logger = logging.getLogger("echo_memory.affect.salience")


class SalienceEvaluator:
    """
    三值顯著性評估器。

    輸入 SalienceSignals + NeurochemState，
    輸出 (salience_score, da_weight, ht_weight) 三元組。

    所有輸出值 clamp 到 [0.0, 1.0]。
    """

    def __init__(self, neurochem: NeurochemState):
        """
        Args:
            neurochem: 當前神經化學狀態（用於讀取 dopamine/serotonin）
        """
        self._neurochem = neurochem

    def evaluate(
        self,
        signals: SalienceSignals,
        user_input: str = "",
        agent_response: str = "",
    ) -> tuple[float, float, float]:
        """
        計算三值顯著性。

        Args:
            signals: 顯著性信號容器
            user_input: 使用者輸入（用於 text-based 啟發式）
            agent_response: Agent 回覆（用於 sentiment 估算）

        Returns:
            (salience_score, da_weight, ht_weight) 三元組，各值 [0.0, 1.0]
        """
        # ── salience_score ──────────────────────────────────────────
        # OpenSoul 公式：complexity×0.4 + novelty×0.4 + |feedback|×0.5
        #                + text_sentiment×0.1 + error_bonus
        complexity = signals.task_complexity
        novelty = signals.novelty_score
        feedback_abs = abs(signals.user_feedback)
        error_bonus = 0.15 if signals.error_occurred else 0.0

        # text sentiment 粗略估算：回覆/輸入長度比
        # 長回覆通常表示更深入的互動
        text_sentiment = self._estimate_text_sentiment(user_input, agent_response)

        salience = (
            complexity * 0.4
            + novelty * 0.4
            + feedback_abs * 0.5
            + text_sentiment * 0.1
            + error_bonus
        )

        # ── da_weight ───────────────────────────────────────────────
        # OpenSoul 公式：
        #   正向回饋 → dopamine + feedback×0.3
        #   錯誤 → dopamine - 0.2
        #   否則 → dopamine
        da = self._neurochem.dopamine
        if signals.user_feedback > 0.1:
            da_weight = da + signals.user_feedback * 0.3
        elif signals.error_occurred:
            da_weight = da - 0.2
        else:
            da_weight = da

        # ── ht_weight ───────────────────────────────────────────────
        # OpenSoul 公式：serotonin×0.7 + verification_score×0.3
        ht_weight = (
            self._neurochem.serotonin * 0.7
            + signals.verification_score * 0.3
        )

        # 修正懲罰
        if signals.was_corrected:
            ht_weight -= 0.1

        # clamp 所有值
        salience = self._clamp(salience)
        da_weight = self._clamp(da_weight)
        ht_weight = self._clamp(ht_weight)

        logger.debug(
            f"[SalienceEvaluator] 三值輸出: "
            f"salience={salience:.3f}, da={da_weight:.3f}, ht={ht_weight:.3f}"
        )

        return salience, da_weight, ht_weight

    def update_neurochem(self, signals: SalienceSignals) -> None:
        """
        根據顯著性信號更新神經化學狀態。

        對應 OpenSoul SalienceEvaluator.update_neurochem()。
        在 evaluate() 之後呼叫。
        """
        nc = self._neurochem

        if signals.user_feedback > 0.3:
            nc.on_success(reward=signals.user_feedback)
        elif signals.user_feedback < -0.3:
            nc.on_failure(penalty=abs(signals.user_feedback))

        if signals.error_occurred:
            nc.on_failure(penalty=0.15)

        if signals.novelty_score > 0.7:
            nc.on_discovery(novelty=signals.novelty_score)

    @staticmethod
    def _estimate_text_sentiment(user_input: str, agent_response: str) -> float:
        """
        文本情感粗略估算。

        正式版應由 SubconsciousAssessor 的 emotional_tone 取代。
        目前用長度比作為代理（較長的回覆表示更深入的互動）。

        Returns:
            情感分數 [0.0, 1.0]
        """
        if not user_input and not agent_response:
            return 0.5

        len_user = max(1, len(user_input))
        len_resp = max(1, len(agent_response))

        # log 比較，避免極端值
        ratio = math.log1p(len_resp) / math.log1p(len_user + len_resp)
        return min(1.0, max(0.0, ratio))

    @staticmethod
    def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
        return max(low, min(high, value))
