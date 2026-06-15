"""
JudgeAgent — 工具決策裁判

對應 OpenSoul soul/gating/judge.py。
在 LLM 生成前過濾可用工具集，避免全量工具丟給 LLM。

Echo Memory 版本使用規則過濾（非 LLM 呼叫），保持輕量。
未來可升級為 LLM 驅動。
"""

from __future__ import annotations

import logging
from typing import Any

from echo_memory.schemas.memory_context import MemoryContext

logger = logging.getLogger("echo_memory.affect.judge")


class JudgeAgent:
    """
    工具決策裁判（規則版）。

    根據上下文過濾不適合的工具，減少 LLM 的選擇負擔。
    對齊 OpenSoul 的 JudgeAgent 概念，但用規則代替 LLM 判斷。

    OpenSoul 原版用 utility LLM 判斷 recommended_tool + confidence，
    我們先用規則邏輯做，確保功能通電。
    """

    def filter_tools(
        self,
        context: MemoryContext | None,
        all_tools: list[dict[str, Any]],
        total_episodes: int = 0,
    ) -> list[dict[str, Any]]:
        """
        根據上下文過濾工具集。

        過濾規則（對齊 OpenSoul 四原則精神）：
        1. 無記憶脈絡 → 移除 adjust_importance（沒有脈絡不能調整）
        2. 記憶太少 → 移除 trigger_dream（Dream 沒意義）
        3. 記憶太少 → 移除 deep_recall（沒東西可以深度搜尋）
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
            logger.debug(f"[JudgeAgent] 過濾工具: {removed}")

        return filtered
