"""
Layer 3: Full Compaction — 完整壓縮

觸發條件：token 使用率 > 92%
策略：
- 九+一段式結構化摘要（參考 Anthropic Claude Code Tier 4）
- 強制寫入 Echo Memory L2
- 神聖段落完整保留

壓縮比目標：6:1 ~ 8:1
"""

from typing import List, Optional, Tuple, Protocol
from session_control.schemas.message import Message
from session_control.schemas.session import SacredContent


class CompactionBackend(Protocol):
    """壓縮後端介面"""

    async def compact(
        self,
        messages: List[Message],
        sacred: SacredContent,
        instructions: Optional[str] = None,
    ) -> str:
        """將完整對話壓縮為結構化摘要"""
        ...


# 十段式壓縮 prompt 模板（仿 Anthropic 九段 + U.E.P 人設段）
COMPACTION_PROMPT = """You are compressing a conversation into a structured summary.
Generate the summary using exactly these sections:

<analysis>
(Your reasoning work — this will be discarded after compression)
</analysis>

<summary>
1. PRIMARY REQUEST & INTENT
What is the user's core goal in this conversation?

2. KEY TECHNICAL CONCEPTS
Important technical decisions, architecture choices, configurations.

3. FILES & CODE SECTIONS
Files modified, key code snippets, configuration changes.

4. ERRORS & FIXES
Problems encountered and how they were resolved.

5. PROBLEM-SOLVING APPROACHES
Strategies tried, what worked and what didn't.

6. ALL USER MESSAGES VERBATIM
[SACRED — preserve every user message exactly as written, do NOT summarize]

7. PENDING TASKS
What remains to be done.

8. CURRENT WORK IN PROGRESS
What was being worked on when this compaction triggered.

9. OPTIONAL NEXT STEPS
Suggested next actions.

10. CHARACTER & PERSONA
[SACRED — preserved from sacred content, do NOT modify]
{sacred_content}
</summary>

RULES:
- Section 6 is SACRED: preserve ALL user messages verbatim, never omit.
- Section 10 is SACRED: copy the persona exactly as provided.
- Be concise in sections 1-5, 7-9.
- Use the conversation's primary language.
"""


class FullCompactor:
    """Layer 3 完整壓縮器"""

    def __init__(self, backend: Optional[CompactionBackend] = None):
        self.backend = backend

    async def compact(
        self,
        messages: List[Message],
        sacred: SacredContent,
        instructions: Optional[str] = None,
    ) -> Tuple[List[Message], str, int]:
        """執行完整壓縮

        Returns:
            (壓縮後的訊息列表, 摘要文本, 節省的 token 數)
        """
        if not self.backend:
            raise RuntimeError("No compaction backend configured for Layer 3")

        # 計算壓縮前的 token 數
        total_before = sum(m.token_count or 0 for m in messages)

        # 呼叫 LLM 做完整壓縮
        summary_text = await self.backend.compact(messages, sacred, instructions)

        # 建立壓縮後的訊息
        compacted_message = Message(
            role="system",
            content=f"[Full Session Compaction]\n{summary_text}",
            is_compressed=True,
            original_turn_range=f"1-{len(messages)}",
            metadata={
                "compression_tier": 3,
                "original_token_count": total_before,
                "original_message_count": len(messages),
            },
        )

        # 計算節省
        summary_tokens = len(summary_text) // 4 + 10
        tokens_saved = total_before - summary_tokens

        return [compacted_message], summary_text, max(0, tokens_saved)
