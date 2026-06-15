"""
SubconsciousAssessor — LLM 驅動的潛意識評估器

對應 OpenSoul soul/affect/subconscious.py。
評估使用者輸入的 5 個維度：emotional_tone, novelty, complexity, uncertainty, dream_resonance。

設計原則：
  - 非阻塞：呼叫方可在 daemon Thread 中執行
  - 降級安全：LLM 失敗時回傳預設值，不影響主流程
  - llm_fn 為 None 時直接回傳預設值
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from echo_memory.affect.signals import SubconsciousAssessment
from echo_memory.schemas.memory_context import MemoryContext

logger = logging.getLogger("echo_memory.affect.subconscious")

# 潛意識評估 prompt
_SUBCONSCIOUS_PROMPT = """你是一個潛意識評估系統。分析以下對話片段的心理維度。

使用者輸入：
{user_input}

{memory_section}

請以純 JSON 格式回覆（不要包含 markdown 標記），評估以下 5 個維度：
{{
  "emotional_tone": <float, -1.0 到 1.0, 負面到正面>,
  "novelty": <float, 0 到 1, 話題新鮮程度>,
  "complexity": <float, 0 到 1, 問題複雜度>,
  "uncertainty": <float, 0 到 1, 不確定/模糊程度>,
  "dream_resonance": <float, 0 到 1, 與記憶的共鳴程度>
}}

評估規則：
- emotional_tone：正面問候/感謝 → 正值；抱怨/困惑 → 負值
- novelty：全新話題 → 高；重複問題 → 低
- complexity：簡單問答 → 低；多步推理/設計 → 高
- uncertainty：明確指令 → 低；模糊描述 → 高
- dream_resonance：與已知記憶相關 → 高；完全陌生 → 低"""


class SubconsciousAssessor:
    """
    LLM 驅動的潛意識評估器。

    在主流程的 Step 2.5 並行執行（ThreadPoolExecutor），
    結果在 Step 5 收割用於 SalienceEvaluator。

    llm_fn 為 None 時降級回傳預設值。
    """

    def __init__(self, llm_fn: Callable[[str], str] | None = None):
        """
        Args:
            llm_fn: LLM 呼叫函式 (prompt: str) -> str
        """
        self._llm_fn = llm_fn

    def assess(
        self,
        user_input: str,
        memory_ctx: MemoryContext | None = None,
        agent_response: str = "",
    ) -> SubconsciousAssessment:
        """
        評估使用者輸入的 5 個潛意識維度。

        Args:
            user_input: 使用者原始輸入
            memory_ctx: EcphoryRAG 檢索的記憶脈絡（可選）
            agent_response: Agent 回覆（可選，用於後續分析）

        Returns:
            SubconsciousAssessment 包含 5 個維度分數
        """
        if self._llm_fn is None:
            logger.debug("[SubconsciousAssessor] 無 LLM，使用啟發式降級")
            return self._heuristic_assess(user_input, memory_ctx)

        try:
            return self._llm_assess(user_input, memory_ctx)
        except Exception as e:
            logger.warning(f"[SubconsciousAssessor] LLM 評估失敗，降級: {e}")
            return self._heuristic_assess(user_input, memory_ctx)

    def _llm_assess(
        self,
        user_input: str,
        memory_ctx: MemoryContext | None,
    ) -> SubconsciousAssessment:
        """透過 LLM 執行評估。"""
        # 構建記憶脈絡段落
        memory_section = ""
        dreamed_topics: list[str] = []

        if memory_ctx and memory_ctx.episodes:
            summaries = [ep.content_summary[:80] for ep in memory_ctx.episodes[:3]]
            memory_section = "相關記憶：\n" + "\n".join(f"- {s}" for s in summaries)

            # 收集 dreamed topics
            for ep in memory_ctx.episodes:
                if ep.is_dreamed:
                    dreamed_topics.append(ep.content_summary[:30])

        prompt = _SUBCONSCIOUS_PROMPT.format(
            user_input=user_input[:500],  # 限制長度
            memory_section=memory_section or "（無相關記憶）",
        )

        assert self._llm_fn is not None
        raw_response = self._llm_fn(prompt)
        result = self._parse_response(raw_response)
        result.dreamed_topics = dreamed_topics

        logger.debug(
            f"[SubconsciousAssessor] LLM 評估完成: "
            f"tone={result.emotional_tone:.2f}, novelty={result.novelty:.2f}, "
            f"complexity={result.complexity:.2f}"
        )

        return result

    def _heuristic_assess(
        self,
        user_input: str,
        memory_ctx: MemoryContext | None,
    ) -> SubconsciousAssessment:
        """
        啟發式降級評估（無 LLM 時使用）。

        基於文本特徵做粗略估算。
        """
        text = user_input.lower()
        length = len(user_input)

        # emotional_tone：問號/感嘆號啟發式
        question_marks = text.count("?") + text.count("？")
        exclamation = text.count("!") + text.count("！")
        tone = 0.0
        if question_marks > 0:
            tone = -0.1 * min(question_marks, 3)
        if exclamation > 0:
            tone = min(0.3, exclamation * 0.1)

        # complexity：基於長度
        complexity = min(1.0, length / 300)

        # novelty：有相關記憶 → 低新鮮度
        novelty = 0.7
        dream_resonance = 0.0
        dreamed_topics: list[str] = []

        if memory_ctx and memory_ctx.episodes:
            novelty = max(0.2, 0.7 - len(memory_ctx.episodes) * 0.1)
            # dream resonance
            for ep in memory_ctx.episodes:
                if ep.is_dreamed:
                    dream_resonance = min(1.0, dream_resonance + 0.3)
                    dreamed_topics.append(ep.content_summary[:30])

        # uncertainty：問號多 → 高不確定性
        uncertainty = min(1.0, question_marks * 0.2)

        return SubconsciousAssessment(
            emotional_tone=max(-1.0, min(1.0, tone)),
            novelty=novelty,
            complexity=complexity,
            uncertainty=uncertainty,
            dream_resonance=dream_resonance,
            dreamed_topics=dreamed_topics,
        )

    @staticmethod
    def _parse_response(raw: str) -> SubconsciousAssessment:
        """解析 LLM JSON 回覆，有容錯。"""
        # 嘗試直接解析
        try:
            data = json.loads(raw.strip())
            return SubconsciousAssessment(
                emotional_tone=_safe_float(data.get("emotional_tone"), -1.0, 1.0, 0.0),
                novelty=_safe_float(data.get("novelty"), 0.0, 1.0, 0.5),
                complexity=_safe_float(data.get("complexity"), 0.0, 1.0, 0.5),
                uncertainty=_safe_float(data.get("uncertainty"), 0.0, 1.0, 0.0),
                dream_resonance=_safe_float(data.get("dream_resonance"), 0.0, 1.0, 0.0),
            )
        except (json.JSONDecodeError, TypeError, ValueError):
            pass

        # 嘗試從 markdown code block 提取
        import re
        json_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
        if json_match:
            try:
                data = json.loads(json_match.group(1))
                return SubconsciousAssessment(
                    emotional_tone=_safe_float(data.get("emotional_tone"), -1.0, 1.0, 0.0),
                    novelty=_safe_float(data.get("novelty"), 0.0, 1.0, 0.5),
                    complexity=_safe_float(data.get("complexity"), 0.0, 1.0, 0.5),
                    uncertainty=_safe_float(data.get("uncertainty"), 0.0, 1.0, 0.0),
                    dream_resonance=_safe_float(data.get("dream_resonance"), 0.0, 1.0, 0.0),
                )
            except (json.JSONDecodeError, TypeError, ValueError):
                pass

        logger.warning(f"[SubconsciousAssessor] JSON 解析失敗，使用預設值")
        return SubconsciousAssessment()


def _safe_float(
    value: Any,
    low: float,
    high: float,
    default: float,
) -> float:
    """安全地將值轉為 float 並 clamp。"""
    try:
        v = float(value)
        return max(low, min(high, v))
    except (TypeError, ValueError):
        return default
