"""
訊息 Schema — 對話訊息的基本資料結構

從 U.E.P Core 的 schemas.py 遷出並改造：
- 移除 U.E.P 特定的系統狀態耦合
- 增加壓縮相關的元資料欄位
- 支援多後端的統一訊息格式
"""

import time
from typing import Any, Literal

from pydantic import BaseModel, Field

# ── 基礎訊息格式 ──────────────────────────────────────────────────────────────


class Message(BaseModel):
    """統一訊息格式 — 後端無關的訊息表示"""

    role: Literal["system", "user", "assistant", "tool"] = Field(
        ..., description="訊息角色"
    )
    content: str = Field(..., description="訊息內容")
    timestamp: float = Field(default_factory=time.time, description="訊息時間戳")
    token_count: int | None = Field(None, description="此訊息的 token 數量（由 counter 填入）")

    # 壓縮相關
    is_compressed: bool = Field(False, description="此訊息是否為壓縮後的摘要")
    original_turn_range: str | None = Field(
        None, description="若為摘要，記錄原始 turn 範圍（如 '1-15'）"
    )

    # 工具呼叫相關
    tool_call_id: str | None = Field(None, description="工具呼叫 ID")
    tool_name: str | None = Field(None, description="工具名稱")
    is_reproducible: bool = Field(
        True, description="工具輸出是否可重現（影響 Layer 1 清理決策）"
    )

    # 元資料
    metadata: dict[str, Any] = Field(default_factory=dict, description="額外元資料")


class ConversationHistory(BaseModel):
    """對話歷史容器"""

    messages: list[Message] = Field(default_factory=list, description="訊息列表")
    total_tokens: int = Field(0, description="總 token 數")
    compression_count: int = Field(0, description="已執行的壓縮次數")

    def add(self, message: Message) -> None:
        """新增訊息"""
        self.messages.append(message)
        if message.token_count:
            self.total_tokens += message.token_count

    def get_recent(self, n: int) -> list[Message]:
        """取得最近 n 則訊息"""
        return self.messages[-n:] if n < len(self.messages) else list(self.messages)

    @property
    def turn_count(self) -> int:
        """使用者訊息數量"""
        return sum(1 for m in self.messages if m.role == "user")
