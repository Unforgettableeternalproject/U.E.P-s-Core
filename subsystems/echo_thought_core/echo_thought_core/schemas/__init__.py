"""
Schema 定義 — 統一匯出入口

使用方式：
    from echo_thought_core.schemas import Message, SessionState, CompactionConfig
"""

# 訊息相關
# 設定相關
from .config import (
    CompactionConfig,
    PersonaContext,
)
from .message import (
    ConversationHistory,
    Message,
)

# 會話狀態相關
from .session import (
    CompactionResult,
    CompressionTier,
    SacredContent,
    SessionState,
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
