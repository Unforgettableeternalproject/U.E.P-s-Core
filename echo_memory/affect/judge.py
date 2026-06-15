"""
JudgeAgent — 工具決策裁判（Hybrid 版）

對應 OpenSoul soul/gating/judge.py。
在 LLM 生成前過濾可用工具集，避免全量工具丟給 LLM。

Hybrid 架構：
  1. 確定性安全規則——過濾明確不適用的工具（不可繞過）
  2. LLM 語意推薦——對剩餘工具排序並推薦最適合的（可選）
  3. llm_fn 為 None 或呼叫失敗時，完全回退到規則結果
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from echo_memory.schemas.memory_context import MemoryContext

logger = logging.getLogger("echo_memory.affect.judge")

# LLM 推薦 prompt
_RECOMMEND_PROMPT = """你是記憶系統的工具決策裁判。根據使用者的意圖，從可用工具中推薦最適合的。

使用者輸入：{user_input}

可用工具：
{tool_descriptions}

記憶脈絡摘要：{context_summary}

請以純 JSON 格式回覆（不要包含 markdown 標記）：
{{"recommended_tool": "工具名稱或 none", "reasoning": "一句話理由", "confidence": 0.0到1.0}}

規則：
- 若沒有工具適合，recommended_tool 設為 "none"
- confidence 低於 0.5 時應設為 "none"
- 優先考慮使用者的實際意圖，而非文字表面"""

# LLM 推薦的最低信心門檻
_MIN_CONFIDENCE = 0.5

# LLM 呼叫超時（秒），由外部呼叫者控制
LLM_TIMEOUT_SECONDS = 10


@dataclass
class JudgeRecommendation:
    """LLM 推薦結果。"""
    recommended_tool: str  # 工具名稱或 "none"
    reasoning: str
    confidence: float
    source: str  # "llm" 或 "rules"


class JudgeAgent:
    """
    工具決策裁判（Hybrid 版）。

    第一層：確定性安全規則（不可繞過）
    第二層：LLM 語意推薦（可選，失敗時降級）
    """

    def __init__(self, llm_fn: Callable[[str], str] | None = None):
        """
        Args:
            llm_fn: LLM 呼叫函式。為 None 時僅使用規則過濾。
        """
        self._llm_fn = llm_fn

    def filter_tools(
        self,
        context: MemoryContext | None,
        all_tools: list[dict[str, Any]],
        total_episodes: int = 0,
    ) -> list[dict[str, Any]]:
        """
        根據上下文過濾工具集（確定性規則層）。

        過濾規則（不可繞過）：
        1. 無記憶脈絡 → 移除 adjust_importance
        2. 記憶太少 → 移除 trigger_dream
        3. 記憶太少 → 移除 deep_recall
        4. 其他工具通過

        Args:
            context: 記憶脈絡（可為 None）
            all_tools: 完整工具定義清單
            total_episodes: 總 episode 數

        Returns:
            過濾後的工具清單
        """
        if not all_tools:
            return []

        filtered = []
        removed = []

        for tool in all_tools:
            name = tool.get("name", "")

            # 規則 1：無脈絡時不能調整重要性
            if name == "adjust_importance":
                if not context or not context.episodes:
                    removed.append(name)
                    continue

            # 規則 2：記憶太少時 Dream 沒意義
            if name == "trigger_dream":
                if total_episodes < 5:
                    removed.append(name)
                    continue

            # 規則 3：記憶太少時深度搜尋無意義
            if name == "deep_recall":
                if total_episodes < 3:
                    removed.append(name)
                    continue

            filtered.append(tool)

        if removed:
            logger.debug("[JudgeAgent] 規則過濾工具: %s", removed)

        return filtered

    def recommend_tool(
        self,
        user_input: str,
        context: MemoryContext | None,
        available_tools: list[dict[str, Any]],
    ) -> JudgeRecommendation:
        """
        對過濾後的工具集做語意推薦。

        流程：
        1. 先執行 filter_tools 取得安全工具集
        2. llm_fn 可用時，送 LLM 推薦
        3. LLM 失敗或信心不足時，回傳 "none"

        Args:
            user_input: 使用者輸入
            context: 記憶脈絡
            available_tools: 已過濾的工具清單

        Returns:
            推薦結果
        """
        # 沒有工具或沒有 LLM → 規則降級
        if not available_tools or self._llm_fn is None:
            return JudgeRecommendation(
                recommended_tool="none",
                reasoning="無可用工具或 LLM 未配置",
                confidence=0.0,
                source="rules",
            )

        # 建立工具描述
        tool_descs = []
        for t in available_tools:
            name = t.get("name", "unknown")
            desc = t.get("description", "無描述")
            tool_descs.append(f"- {name}: {desc}")
        tool_text = "\n".join(tool_descs)

        # 建立脈絡摘要
        ctx_summary = "無記憶脈絡"
        if context and context.episodes:
            ctx_summary = f"已召回 {len(context.episodes)} 個相關記憶"
            if context.concepts:
                concept_names = [c.name for c in context.concepts[:5]]
                ctx_summary += f"，相關概念：{', '.join(concept_names)}"

        prompt = _RECOMMEND_PROMPT.format(
            user_input=user_input[:300],
            tool_descriptions=tool_text,
            context_summary=ctx_summary,
        )

        # 建立合法候選集（用於驗證 LLM 輸出）
        valid_names = {t.get("name", "") for t in available_tools}

        # LLM 呼叫（失敗時降級）
        try:
            raw = self._llm_fn(prompt)
            return self._parse_recommendation(raw, valid_names)
        except Exception as e:
            logger.warning("[JudgeAgent] LLM 推薦失敗，降級為規則結果: %s", e)
            return JudgeRecommendation(
                recommended_tool="none",
                reasoning=f"LLM 呼叫失敗: {e}",
                confidence=0.0,
                source="rules",
            )

    def _parse_recommendation(
        self,
        raw: str,
        valid_names: set[str] | None = None,
    ) -> JudgeRecommendation:
        """解析 LLM 推薦結果。"""
        # 嘗試直接解析
        parsed = None
        try:
            parsed = json.loads(raw.strip())
        except json.JSONDecodeError:
            # 嘗試從 markdown code block 提取
            match = re.search(r"\{.*?\}", raw, re.DOTALL)
            if match:
                try:
                    parsed = json.loads(match.group(0))
                except json.JSONDecodeError:
                    pass

        if not parsed or not isinstance(parsed, dict):
            logger.warning("[JudgeAgent] LLM 回應無法解析: %s", raw[:200])
            return JudgeRecommendation(
                recommended_tool="none",
                reasoning="LLM 回應格式無效",
                confidence=0.0,
                source="rules",
            )

        tool = str(parsed.get("recommended_tool", "none"))
        reasoning = str(parsed.get("reasoning", ""))
        confidence = float(parsed.get("confidence", 0.0))

        # 信心不足時回退
        if confidence < _MIN_CONFIDENCE:
            tool = "none"

        # 驗證推薦工具是否在合法候選集內
        if tool != "none" and valid_names and tool not in valid_names:
            logger.warning(
                "[JudgeAgent] LLM 推薦了不存在的工具 '%s'，回退為 none", tool
            )
            tool = "none"
            reasoning = f"LLM 推薦的 '{tool}' 不在合法候選集中"

        return JudgeRecommendation(
            recommended_tool=tool,
            reasoning=reasoning,
            confidence=confidence,
            source="llm",
        )
