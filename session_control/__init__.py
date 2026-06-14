"""
Session Control — LLM 會話控制子系統

Prompt / Token / Context 三大指標優化引擎。
四層漸進式壓縮架構，支援多 LLM 後端。

Usage:
    from session_control import ContextManager, CompactionConfig, PersonaContext

    config = CompactionConfig(backend="anthropic", model="claude-sonnet-4-6")
    persona = PersonaContext(name="U.E.P", traits=["friendly", "professional"])
    manager = ContextManager(config=config, persona=persona)

    manager.add_message(role="user", content="Hello!")
    messages = manager.get_messages()
"""

from session_control.context_manager import ContextManager
from session_control.schemas.config import CompactionConfig, PersonaContext
from session_control.schemas.session import SessionState, CompactionResult
from session_control.schemas.message import Message, ConversationEntry

__version__ = "0.1.0"

__all__ = [
    "ContextManager",
    "CompactionConfig",
    "PersonaContext",
    "SessionState",
    "CompactionResult",
    "Message",
    "ConversationEntry",
]
