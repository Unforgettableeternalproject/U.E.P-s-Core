"""
Echo Thought Core — LLM 會話控制子系統

U.E.P 回聲系統的思考核心，與 Echo Memory（記憶核心）互補。
Prompt / Token / Context 三大指標優化引擎。
四層漸進式壓縮架構，支援多 LLM 後端（Gemini / Ollama / 自訂）。

Usage (低層級):
    from echo_thought_core import ContextManager, CompactionConfig, PersonaContext

    config = CompactionConfig(backend="gemini", model="gemini-2.5-flash-lite")
    persona = PersonaContext(name="U.E.P", traits=["friendly", "curious"])
    manager = ContextManager(config=config, persona=persona)

Usage (推薦 — 高層級 factory):
    from echo_thought_core import create_context_manager

    manager = create_context_manager(
        backend_type="ollama",
        backend_config={"model": "gemma4:e4b-it-q4_K_M"},
        persona=PersonaContext(name="U.E.P"),
    )
"""

from echo_thought_core.backends.base import AbstractBackend
from echo_thought_core.backends.factory import create_backend, create_context_manager
from echo_thought_core.backends.registry import BackendRegistry

# Cache Provider（可插拔顯性快取）
from echo_thought_core.cache import (
    ExplicitCacheProvider,
    GeminiCacheProvider,
    NullCacheProvider,
)
from echo_thought_core.context_manager import ContextManager
from echo_thought_core.echo_compressor import EchoCompressor
from echo_thought_core.engine_base import ContextEngine
from echo_thought_core.memory_bridge import MemoryBridge, NullMemoryBridge
from echo_thought_core.persona import (
    build_persona_from_config,
    build_system_instruction,
    load_llm_config,
)

# Prompt 管理（介面預留）
from echo_thought_core.prompt import DefaultPromptBuilder, PromptBuilder, PromptContext
from echo_thought_core.schemas.config import CompactionConfig, PersonaContext
from echo_thought_core.schemas.message import Message
from echo_thought_core.schemas.session import CompactionResult, SessionState

__version__ = "0.2.1-dev"

# ── 延遲匯入（可選依賴 — 含 google.genai 等重量級套件）─────────────────────
# LLMClient 和 OllamaBackend 不在頂層 import，
# 避免只用 Ollama 時也載入 google.genai（反之亦然）。

_LAZY_IMPORTS: dict[str, tuple[str, str]] = {
    "LLMClient": ("echo_thought_core.llm_client", "LLMClient"),
    "OllamaBackend": ("echo_thought_core.backends.ollama", "OllamaBackend"),
}


def __getattr__(name: str):
    """模組層級 __getattr__ — 延遲載入 LLMClient / OllamaBackend。

    支援 `from echo_thought_core import LLMClient` 語法，
    但實際 import 只在第一次存取時發生。
    """
    if name in _LAZY_IMPORTS:
        module_path, attr_name = _LAZY_IMPORTS[name]
        import importlib
        mod = importlib.import_module(module_path)
        obj = getattr(mod, attr_name)
        # 快取到模組 globals，下次不再經過 __getattr__
        globals()[name] = obj
        return obj
    raise AttributeError(f"module 'echo_thought_core' has no attribute {name!r}")


__all__ = [
    # 核心
    "ContextManager",
    "ContextEngine",
    "EchoCompressor",
    "CompactionConfig",
    "PersonaContext",
    "SessionState",
    "CompactionResult",
    "Message",
    # 記憶整合
    "MemoryBridge",
    "NullMemoryBridge",
    # 後端（延遲載入）
    "AbstractBackend",
    "BackendRegistry",
    "LLMClient",
    "OllamaBackend",
    # Factory
    "create_backend",
    "create_context_manager",
    # Persona
    "load_llm_config",
    "build_persona_from_config",
    "build_system_instruction",
    # Prompt
    "PromptBuilder",
    "DefaultPromptBuilder",
    "PromptContext",
    # Cache Providers
    "ExplicitCacheProvider",
    "GeminiCacheProvider",
    "NullCacheProvider",
]
