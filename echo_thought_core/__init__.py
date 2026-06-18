"""
Echo Thought Core — LLM 會話控制子系統

U.E.P 回聲系統的思考核心，與 Echo Memory（記憶核心）互補。
Prompt / Token / Context 三大指標優化引擎。
四層漸進式壓縮架構，以 Gemini / Vertex AI 為主要後端。

Usage (低層級):
    from echo_thought_core import ContextManager, CompactionConfig, PersonaContext

    config = CompactionConfig(backend="gemini", model="gemini-2.5-flash-lite")
    persona = PersonaContext(name="U.E.P", traits=["friendly", "curious"])
    manager = ContextManager(config=config, persona=persona)

Usage (推薦 — 高層級 factory):
    from echo_thought_core import create_context_manager, load_llm_config

    config = load_llm_config()
    manager = create_context_manager(
        backend_config={"model": config["model"]},
        persona=PersonaContext(name="U.E.P"),
    )
"""

from echo_thought_core.context_manager import ContextManager
from echo_thought_core.schemas.config import CompactionConfig, PersonaContext
from echo_thought_core.schemas.session import SessionState, CompactionResult
from echo_thought_core.schemas.message import Message
from echo_thought_core.backends.factory import create_backend, create_context_manager
from echo_thought_core.llm_client import LLMClient
from echo_thought_core.persona import (
    load_llm_config,
    build_persona_from_config,
    build_system_instruction,
)

__version__ = "0.1.0"

__all__ = [
    # 核心
    "ContextManager",
    "CompactionConfig",
    "PersonaContext",
    "SessionState",
    "CompactionResult",
    "Message",
    # LLM
    "LLMClient",
    # Factory
    "create_backend",
    "create_context_manager",
    # Persona
    "load_llm_config",
    "build_persona_from_config",
    "build_system_instruction",
]
