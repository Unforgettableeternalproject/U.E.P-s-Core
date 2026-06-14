"""
Layer 2: Rolling Summary — 增量摘要

觸發條件：token 使用率 > 80%
策略：
- 對最舊的 N 個 turn 做語義摘要（需要 LLM 呼叫）
- 保留神聖段落
- 觸發寫入 Echo Memory L1（異步）

參考 Anthropic Claude Code 的 Tier 3 Context Collapse 設計。
"""

from typing import List, Optional, Tuple, Protocol
from session_control.schemas.message import Message


class SummaryBackend(Protocol):
    """摘要後端介面"""

    async def summarize(self, messages: List[Message], instructions: Optional[str] = None) -> str:
        """將訊息列表摘要為文本"""
        ...


# 預設摘要 prompt 模板（參考 Anthropic Cookbook）
SUMMARY_PROMPT = """Summarize the following conversation segment concisely.
Preserve:
1. Key decisions and their rationale
2. Important technical details (file names, configurations, error messages)
3. User's explicit instructions and preferences
4. Current task progress and pending items

Do NOT preserve:
- Casual greetings or small talk
- Redundant confirmations
- Tool output details that can be regenerated

Output a structured summary in the conversation's primary language.
"""


class RollingSummarizer:
    """Layer 2 增量摘要器"""

    def __init__(self, backend: Optional[SummaryBackend] = None):
        self.backend = backend

    async def summarize_oldest(
        self,
        messages: List[Message],
        n_turns: int = 10,
        instructions: Optional[str] = None,
    ) -> Tuple[List[Message], str, int]:
        """摘要最舊的 N 個 turn

        Returns:
            (更新後的訊息列表, 摘要文本, 節省的 token 數)
        """
        if not self.backend:
            raise RuntimeError("No summary backend configured for Layer 2")

        if len(messages) <= n_turns:
            return messages, "", 0

        # 分割：舊的（要摘要）和新的（保留）
        old_messages = messages[:n_turns]
        new_messages = messages[n_turns:]

        # 計算舊訊息的 token 數
        old_tokens = sum(m.token_count or 0 for m in old_messages)

        # 呼叫 LLM 做摘要
        prompt = instructions or SUMMARY_PROMPT
        summary_text = await self.backend.summarize(old_messages, prompt)

        # 建立摘要訊息
        summary_message = Message(
            role="system",
            content=f"[Conversation Summary (turns 1-{n_turns})]\n{summary_text}",
            is_compressed=True,
            original_turn_range=f"1-{n_turns}",
            metadata={"compression_tier": 2, "original_token_count": old_tokens},
        )

        # 計算節省
        summary_tokens = len(summary_text) // 4 + 10  # 粗估
        tokens_saved = old_tokens - summary_tokens

        result = [summary_message] + new_messages
        return result, summary_text, max(0, tokens_saved)
