"""
Schema 定義 — 統一匯出入口

使用方式：
    from echo_thought_core.schemas import Message, SessionState, CompactionConfig
"""

# 訊息相關
from .message import (
    Message,
    ConversationHistory,
)

# 會話狀態相關
from .session import (
    CompressionTier,
    SessionState,
    CompactionResult,
    SacredContent,
)

# 設定相關
from .config import (
    CompactionConfig,
    PersonaContext,
)

__all__ = [
    # message.py
    "Message",
    "ConversationHistory",
    # session.py
    "CompressionTier",
    "SessionState",
    "CompactionResult",
    "SacredContent",
    # config.py
    "CompactionConfig",
    "PersonaContext",
]
