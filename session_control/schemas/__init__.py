"""
Schema 定義 — 統一匯出入口

使用方式：
    from session_control.schemas import Message, LLMInput, SessionState, CompactionConfig
"""

# 訊息相關
from .message import (
    LLMMode,
    Message,
    ConversationEntry,
    ConversationHistory,
    LearningSignals,
    LearningData,
    LLMInput,
    LLMOutput,
    StatusUpdate,
)

# 會話狀態相關
from .session import (
    CompressionTier,
    SystemState,
    ContextCacheConfig,
    SystemAction,
    SessionState,
    CompactionResult,
    SacredContent,
)

# 設定相關
from .config import (
    CompactionConfig,
    PersonaContext,
    LLMModuleData,
    create_llm_data,
)

__all__ = [
    # message.py
    "LLMMode",
    "Message",
    "ConversationEntry",
    "ConversationHistory",
    "LearningSignals",
    "LearningData",
    "LLMInput",
    "LLMOutput",
    "StatusUpdate",
    # session.py
    "CompressionTier",
    "SystemState",
    "ContextCacheConfig",
    "SystemAction",
    "SessionState",
    "CompactionResult",
    "SacredContent",
    # config.py
    "CompactionConfig",
    "PersonaContext",
    "LLMModuleData",
    "create_llm_data",
]
