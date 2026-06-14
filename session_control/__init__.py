"""
Session Control — LLM 會話控制子系統

Prompt / Token / Context 三大指標優化引擎。
四層漸進式壓縮架構，支援多 LLM 後端。

Usage (低層級):
    from session_control import ContextManager, CompactionConfig, PersonaContext

    config = CompactionConfig(backend="gemini", model="gemini-2.5-flash-lite")
    persona = PersonaContext(name="U.E.P", traits=["friendly", "curious"])
    manager = ContextManager(config=config, persona=persona)

Usage (推薦 — 高層級 factory):
    from session_control import create_context_manager, load_llm_config

    config = load_llm_config()
    manager = create_context_manager(
        backend_config={"model": config["model"]},
        persona=PersonaContext(name="U.E.P"),
    )
"""

from session_control.context_manager import ContextManager
from session_control.schemas.config import CompactionConfig, PersonaContext
from session_control.schemas.session import SessionState, CompactionResult
from session_control.schemas.message import Message, ConversationEntry
from session_control.backends.factory import create_backend, create_context_manager
from session_control.persona import load_llm_config

__version__ = "0.1.0"

__all__ = [
    # 核心
    "ContextManager",
    "CompactionConfig",
    "PersonaContext",
    "SessionState",
    "CompactionResult",
    "Message",
    "ConversationEntry",
    # Factory
    "create_backend",
    "create_context_manager",
    # Persona
    "load_llm_config",
]
