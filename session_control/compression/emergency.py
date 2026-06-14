"""
Layer 4: Emergency — 緊急截斷

觸發條件：API 回傳 413 或等效錯誤
策略：
- 只保留最後 N 則訊息（預設 4 則）
- 從 Echo Memory 拉取最近的 session summary 恢復上下文
- 重注入人設上下文

參考 Anthropic Claude Code 的 Tier 5 Reactive Compact。
單次嘗試守衛，防止重試迴圈。
"""

from typing import List, Tuple, Optional
from session_control.schemas.message import Message
from session_control.schemas.session import SacredContent


class EmergencyCompactor:
    """Layer 4 緊急壓縮器"""

    def __init__(self, max_keep: int = 4):
        self.max_keep = max_keep
        self._has_attempted: bool = False

    def compact(
        self,
        messages: List[Message],
        sacred: SacredContent,
        recovery_context: Optional[str] = None,
    ) -> Tuple[List[Message], int]:
        """緊急截斷

        Args:
            messages: 當前訊息列表
            sacred: 神聖段落
            recovery_context: 從 Echo Memory 恢復的上下文（可選）

        Returns:
            (截斷後的訊息列表, 節省的 token 數)
        """
        if self._has_attempted:
            # 斷路器：已經嘗試過一次，不再重試
            raise RuntimeError(
                "Emergency compaction already attempted this session. "
                "Please use /clear to start fresh."
            )

        self._has_attempted = True

        # 計算截斷前的 token 數
        total_before = sum(m.token_count or 0 for m in messages)

        result: List[Message] = []

        # 1. 重注入神聖段落（人設上下文）
        sacred_text = sacred.total_content
        if sacred_text:
            result.append(
                Message(
                    role="system",
                    content=f"[Emergency Recovery — Sacred Context]\n{sacred_text}",
                    metadata={"emergency_recovery": True},
                )
            )

        # 2. 注入恢復上下文（來自 Echo Memory）
        if recovery_context:
            result.append(
                Message(
                    role="system",
                    content=f"[Previous Session Context]\n{recovery_context}",
                    is_compressed=True,
                    metadata={"emergency_recovery": True, "from_echo_memory": True},
                )
            )

        # 3. 保留最後 N 則訊息
        recent = messages[-self.max_keep :] if len(messages) > self.max_keep else messages
        result.extend(recent)

        # 計算節省
        total_after = sum(m.token_count or 0 for m in result)
        tokens_saved = total_before - total_after

        return result, max(0, tokens_saved)

    def reset(self) -> None:
        """重置斷路器（新 session 時呼叫）"""
        self._has_attempted = False
