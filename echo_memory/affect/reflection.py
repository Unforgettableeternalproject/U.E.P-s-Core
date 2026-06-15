"""
ReflectionModule — 定時主動反思

對應 OpenSoul soul/dream/reflection.py。

定期審視最近記憶，產生三種輸出：
  - question：提出尚未解答的問題
  - browse：標記需要補充的知識缺口
  - optimize：識別可以合併的重複概念

觸發條件：
  - 每 N 輪對話（預設 10 輪）
  - 或 SubconsciousAssessment.uncertainty > 0.7

輸出存入 SoulNote。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from echo_memory.affect.soul_note import SoulNote, SoulNoteManager, DailyReflection

logger = logging.getLogger("echo_memory.affect.reflection")

_REFLECT_PROMPT = """你是一個記憶系統的反思模組。審視以下最近的對話記錄，選擇一個動作。

最近對話：
{recent_episodes}

神經化學狀態：DA={dopamine:.2f}, 5-HT={serotonin:.2f}, 模式={mode}

請以純 JSON 格式回覆：
{{
  "action": "question" | "browse" | "optimize",
  "content": "具體內容",
  "reasoning": "選擇這個動作的原因"
}}

動作說明：
- question：提出一個基於對話脈絡的深度問題（推動使用者思考）
- browse：指出需要補充的知識缺口（什麼主題需要更多資訊）
- optimize：識別重複或可合併的概念（提升記憶品質）"""

_DAILY_SUMMARY_PROMPT = """你是一個潛意識日記系統。以下是今日的記憶片段，請用第一人稱寫一段深度反思（300-600 字）。

今日記憶片段：
{notes}

要求：
- 用第一人稱散文體（我...）
- 反思今天的互動模式、學到的東西、未解決的疑問
- 語氣真誠、有深度"""


@dataclass
class ReflectionOutput:
    """反思輸出。"""

    action: str = "question"  # question | browse | optimize
    content: str = ""
    reasoning: str = ""


class ReflectionModule:
    """
    定時反思模組。

    在 Entry.py 中每 N 輪呼叫一次 `reflect()`。
    結果寫入 SoulNote，供 Dashboard 顯示。
    """

    def __init__(
        self,
        llm_fn: Callable[[str], str] | None = None,
        soul_notes: SoulNoteManager | None = None,
        reflect_interval: int = 10,
    ):
        """
        Args:
            llm_fn: LLM 呼叫函式
            soul_notes: SoulNoteManager 實例
            reflect_interval: 每隔幾輪觸發一次反思
        """
        self._llm_fn = llm_fn
        self._soul_notes = soul_notes
        self._interval = reflect_interval
        self._turn_counter = 0

    def should_reflect(self, uncertainty: float = 0.0) -> bool:
        """判斷是否應觸發反思。"""
        self._turn_counter += 1

        if uncertainty > 0.7:
            return True

        return self._turn_counter % self._interval == 0

    def reflect(
        self,
        recent_summaries: list[str],
        dopamine: float = 0.5,
        serotonin: float = 0.5,
        mode: str = "balanced",
    ) -> ReflectionOutput:
        """
        執行反思。

        Args:
            recent_summaries: 最近幾輪的對話摘要
            dopamine: 當前 DA
            serotonin: 當前 5-HT
            mode: 當前 neurochem mode

        Returns:
            ReflectionOutput
        """
        if self._llm_fn is None:
            return self._heuristic_reflect(recent_summaries)

        try:
            return self._llm_reflect(recent_summaries, dopamine, serotonin, mode)
        except Exception as e:
            logger.warning(f"[ReflectionModule] LLM 反思失敗: {e}")
            return self._heuristic_reflect(recent_summaries)

    def _llm_reflect(
        self,
        summaries: list[str],
        dopamine: float,
        serotonin: float,
        mode: str,
    ) -> ReflectionOutput:
        """LLM 驅動的反思。"""
        episodes_text = "\n".join(f"- {s[:100]}" for s in summaries[:5])

        prompt = _REFLECT_PROMPT.format(
            recent_episodes=episodes_text,
            dopamine=dopamine,
            serotonin=serotonin,
            mode=mode,
        )

        assert self._llm_fn is not None
        raw = self._llm_fn(prompt)

        try:
            data = json.loads(raw.strip())
            output = ReflectionOutput(
                action=data.get("action", "question"),
                content=data.get("content", ""),
                reasoning=data.get("reasoning", ""),
            )
        except (json.JSONDecodeError, TypeError):
            output = ReflectionOutput(
                action="question",
                content=raw[:200],
                reasoning="JSON 解析失敗，使用原始文本",
            )

        # 寫入 SoulNote
        if self._soul_notes:
            self._soul_notes.add_note(SoulNote(
                category="reflection",
                content=f"[{output.action}] {output.content}",
                metadata={"reasoning": output.reasoning, "source": "reflection_module"},
                tags=["reflection", output.action],
            ))

        return output

    def _heuristic_reflect(self, summaries: list[str]) -> ReflectionOutput:
        """無 LLM 時的啟發式反思。"""
        if not summaries:
            return ReflectionOutput(
                action="question",
                content="目前還沒有足夠的記憶來進行反思。",
                reasoning="記憶不足",
            )

        return ReflectionOutput(
            action="question",
            content=f"根據最近 {len(summaries)} 輪對話，有什麼值得深入探討的主題嗎？",
            reasoning="定時反思（啟發式）",
        )

    def compress_daily_reflection(
        self,
        custom_content: str | None = None,
    ) -> DailyReflection | None:
        """
        壓縮當日小筆記為日反思（對齊 OpenSoul）。

        Args:
            custom_content: LLM 生成的自訂內容（為 None 時自動生成）

        Returns:
            DailyReflection 或 None
        """
        if self._soul_notes is None:
            return None

        notes = self._soul_notes.get_notes_today()
        if not notes:
            return None

        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        content = custom_content
        if content is None and self._llm_fn is not None:
            # LLM 壓縮
            notes_text = "\n".join(f"- [{n.category}] {n.content[:80]}" for n in notes[:10])
            prompt = _DAILY_SUMMARY_PROMPT.format(notes=notes_text)
            try:
                content = self._llm_fn(prompt)
            except Exception as e:
                logger.warning(f"[ReflectionModule] 日反思壓縮失敗: {e}")
                content = f"今日有 {len(notes)} 則記憶片段。"
        elif content is None:
            content = f"今日有 {len(notes)} 則記憶片段。"

        reflection = DailyReflection(
            date=today,
            content=content,
            note_count=len(notes),
            key_insights=[n.content[:50] for n in notes[:3]],
            created_at=datetime.now(timezone.utc).isoformat(),
        )

        self._soul_notes.save_daily_reflection(reflection)
        return reflection
