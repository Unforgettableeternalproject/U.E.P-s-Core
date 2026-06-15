"""
SubconsciousInhibitor — 品質閘門決策器

對應 OpenSoul soul/gating/inhibitor.py。
根據 ResponseVerifier 的分數做三態決策：PASS / REVISE / SUPPRESS。

決策規則（對齊 OpenSoul）：
  score >= threshold        → PASS（直接輸出）
  threshold×0.7 <= score    → REVISE（加免責標記後輸出）
  score < threshold×0.7     → SUPPRESS（觸發重試 + neurochem 懲罰）
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum

from echo_memory.affect.verifier import VerificationResult
from echo_memory.neurochem.state import NeurochemState

logger = logging.getLogger("echo_memory.affect.inhibitor")


class InhibitionAction(Enum):
    """閘門決策動作。"""

    PASS = "pass"          # 通過，直接輸出
    REVISE = "revise"      # 信心不足，加免責標記後輸出
    SUPPRESS = "suppress"  # 嚴重不足，觸發重試


@dataclass
class GateDecision:
    """閘門決策結果。"""

    action: InhibitionAction
    text: str              # 處理後的回覆文本
    reason: str            # 決策原因
    should_retry: bool     # 是否應觸發重試


# REVISE 前綴（對齊 OpenSoul）
_REVISE_PREFIX = "⚠️ *（以下回覆信心略低，請自行判斷）*\n\n"


class SubconsciousInhibitor:
    """
    品質閘門決策器。

    在 ResponseVerifier 打分後，根據分數和門檻做三態決策。
    SUPPRESS 會觸發 neurochem.on_failure() 懲罰多巴胺。
    """

    def __init__(
        self,
        neurochem: NeurochemState,
        max_retries: int = 1,
    ):
        """
        Args:
            neurochem: 神經化學狀態（SUPPRESS 時用於懲罰）
            max_retries: SUPPRESS 最大重試次數
        """
        self._neurochem = neurochem
        self._max_retries = max_retries

    def gate(
        self,
        response_text: str,
        verify_result: VerificationResult,
        attempt: int = 0,
    ) -> GateDecision:
        """
        根據驗證結果做閘門決策。

        Args:
            response_text: LLM 生成的回覆
            verify_result: ResponseVerifier 的驗證結果
            attempt: 當前重試次數（0 = 首次）

        Returns:
            GateDecision
        """
        score = verify_result.score
        threshold = verify_result.threshold
        revise_threshold = threshold * 0.7

        if score >= threshold:
            # PASS — 通過
            logger.debug(f"[Inhibitor] PASS (score={score:.3f} >= {threshold:.3f})")
            return GateDecision(
                action=InhibitionAction.PASS,
                text=response_text,
                reason=f"品質通過 ({score:.2f} >= {threshold:.2f})",
                should_retry=False,
            )

        elif score >= revise_threshold:
            # REVISE — 加免責標記
            logger.info(
                f"[Inhibitor] REVISE (score={score:.3f}, "
                f"threshold={threshold:.3f})"
            )
            return GateDecision(
                action=InhibitionAction.REVISE,
                text=_REVISE_PREFIX + response_text,
                reason=f"信心不足 ({score:.2f} < {threshold:.2f})，加免責標記",
                should_retry=False,
            )

        else:
            # SUPPRESS — 嚴重不足
            should_retry = attempt < self._max_retries

            # 懲罰 neurochem
            penalty = min(0.3, threshold - score)
            self._neurochem.on_failure(penalty=penalty)

            logger.warning(
                f"[Inhibitor] SUPPRESS (score={score:.3f} < "
                f"{revise_threshold:.3f}), retry={should_retry}"
            )

            return GateDecision(
                action=InhibitionAction.SUPPRESS,
                text=response_text,
                reason=(
                    f"品質嚴重不足 ({score:.2f} < {revise_threshold:.2f})，"
                    f"{'觸發重試' if should_retry else '重試次數耗盡，強制輸出'}"
                ),
                should_retry=should_retry,
            )
