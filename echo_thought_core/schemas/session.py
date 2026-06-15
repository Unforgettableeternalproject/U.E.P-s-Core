"""Session Schema — 會話狀態與壓縮結果的資料結構"""

import time
from typing import Optional, Dict, Any, List
from enum import Enum
from pydantic import BaseModel, Field


# ── 枚舉 ──────────────────────────────────────────────────────────────────────


class CompressionTier(int, Enum):
    """壓縮層級"""

    TIER_0_NORMAL = 0
    TIER_1_MICRO_PRUNE = 1
    TIER_2_ROLLING_SUMMARY = 2
    TIER_3_FULL_COMPACTION = 3
    TIER_4_EMERGENCY = 4


# ── 會話狀態 ──────────────────────────────────────────────────────────────────


class SessionState(BaseModel):
    """會話狀態"""

    session_id: str = Field(..., description="會話 ID")
    current_tokens: int = Field(0, description="當前 token 使用量")
    max_tokens: int = Field(200000, description="最大 context window 大小")
    current_tier: CompressionTier = Field(
        CompressionTier.TIER_0_NORMAL, description="當前壓縮層級"
    )
    compression_count: int = Field(0, description="累計壓縮次數")
    message_count: int = Field(0, description="總訊息數")
    created_at: float = Field(default_factory=time.time, description="建立時間")
    last_compaction_at: Optional[float] = Field(None, description="最後一次壓縮時間")

    @property
    def usage_percentage(self) -> float:
        """Token 使用率"""
        if self.max_tokens == 0:
            return 0.0
        return self.current_tokens / self.max_tokens

    @property
    def usage_display(self) -> str:
        """顯示用的使用率字串"""
        return f"{self.current_tokens:,} / {self.max_tokens:,} ({self.usage_percentage:.1%})"


class CompactionResult(BaseModel):
    """壓縮結果"""

    tier: CompressionTier = Field(..., description="執行的壓縮層級")
    tokens_before: int = Field(..., description="壓縮前 token 數")
    tokens_after: int = Field(..., description="壓縮後 token 數")
    tokens_saved: int = Field(0, description="節省的 token 數")
    compression_ratio: float = Field(0.0, description="壓縮比")
    summary_text: Optional[str] = Field(None, description="產生的摘要文本")
    sacred_preserved: bool = Field(True, description="神聖段落是否完整保留")
    duration_ms: float = Field(0.0, description="壓縮耗時（毫秒）")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="額外元資料")

    @property
    def savings_percentage(self) -> float:
        """節省百分比"""
        if self.tokens_before == 0:
            return 0.0
        return self.tokens_saved / self.tokens_before


# ── 神聖段落 ──────────────────────────────────────────────────────────────────


class SacredContent(BaseModel):
    """神聖段落 — 任何壓縮層級都不會移除的內容"""

    persona_context: Optional[str] = Field(None, description="人設上下文")
    user_instructions: List[str] = Field(
        default_factory=list, description="使用者明確指令（原文保留）"
    )
    current_task: Optional[str] = Field(None, description="當前任務目標")
    custom_sacred: Dict[str, str] = Field(
        default_factory=dict, description="自定義神聖段落"
    )

    @property
    def total_content(self) -> str:
        """合併所有神聖內容"""
        parts = []
        if self.persona_context:
            parts.append(f"[PERSONA]\n{self.persona_context}")
        if self.user_instructions:
            parts.append(
                "[USER INSTRUCTIONS]\n" + "\n".join(self.user_instructions)
            )
        if self.current_task:
            parts.append(f"[CURRENT TASK]\n{self.current_task}")
        for key, value in self.custom_sacred.items():
            parts.append(f"[{key.upper()}]\n{value}")
        return "\n\n".join(parts)
