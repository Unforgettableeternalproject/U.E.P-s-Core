"""
Backend Factory — 根據設定建立對應的後端實例
"""

from typing import Optional, Dict, Any
from session_control.backends.base import AbstractBackend


def create_backend(
    backend_type: str = "anthropic",
    config: Optional[Dict[str, Any]] = None,
) -> AbstractBackend:
    """根據後端類型建立對應的實例

    Args:
        backend_type: "anthropic" | "openai" | "gemini" | "local"
        config: 後端特定設定

    Returns:
        AbstractBackend 實例
    """
    config = config or {}

    if backend_type == "anthropic":
        from session_control.backends.anthropic import AnthropicBackend

        return AnthropicBackend(
            api_key=config.get("api_key"),
            model=config.get("model", "claude-sonnet-4-6"),
            compaction_model=config.get("compaction_model", "claude-haiku-4-5"),
            server_side_compaction=config.get("compaction_enabled", False),
        )

    elif backend_type == "openai":
        from session_control.backends.openai import OpenAIBackend

        return OpenAIBackend(
            api_key=config.get("api_key"),
            model=config.get("model", "gpt-4o"),
            summary_model=config.get("summary_model", "gpt-4o-mini"),
        )

    elif backend_type == "gemini":
        from session_control.backends.gemini import GeminiBackend

        return GeminiBackend(
            api_key=config.get("api_key"),
            project_id=config.get("project_id"),
            location=config.get("location", "us-central1"),
            model=config.get("model", "gemini-2.5-flash-lite"),
        )

    elif backend_type == "local":
        raise NotImplementedError(
            "Local model backend (Qwen2.5) is planned for Phase 3"
        )

    else:
        raise ValueError(f"Unknown backend type: {backend_type}")
