"""
設定管理 — 從 .env 和環境變數讀取設定
"""

import os
from typing import Dict, Any, Optional
from pathlib import Path
from dotenv import load_dotenv

# 載入 .env
_env_path = Path(__file__).parent.parent / ".env"
if _env_path.exists():
    load_dotenv(_env_path)


def get_config() -> Dict[str, Any]:
    """取得完整設定字典"""
    return {
        "backend": os.getenv("SESSION_CONTROL_BACKEND", "anthropic"),
        "context_window_size": int(os.getenv("CONTEXT_WINDOW_SIZE", "200000")),
        "default_tokenizer": os.getenv("DEFAULT_TOKENIZER", "cl100k_base"),
        "thresholds": {
            "micro_prune": float(os.getenv("COMPRESSION_THRESHOLD_LAYER1", "0.60")),
            "rolling_summary": float(os.getenv("COMPRESSION_THRESHOLD_LAYER2", "0.80")),
            "full_compact": float(os.getenv("COMPRESSION_THRESHOLD_LAYER3", "0.92")),
        },
        "anthropic": {
            "api_key": os.getenv("ANTHROPIC_API_KEY"),
            "model": os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
            "compaction_model": os.getenv("ANTHROPIC_COMPACTION_MODEL", "claude-haiku-4-5"),
            "compaction_enabled": os.getenv("ANTHROPIC_COMPACTION_ENABLED", "false").lower() == "true",
        },
        "openai": {
            "api_key": os.getenv("OPENAI_API_KEY"),
            "model": os.getenv("OPENAI_MODEL", "gpt-4o"),
            "summary_model": os.getenv("OPENAI_SUMMARY_MODEL", "gpt-4o-mini"),
        },
        "gemini": {
            "api_key": os.getenv("GOOGLE_API_KEY"),
            "project_id": os.getenv("GCP_PROJECT_ID"),
            "location": os.getenv("GCP_LOCATION", "us-central1"),
            "model": os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite"),
        },
        "local": {
            "model_path": os.getenv("LOCAL_MODEL_PATH"),
            "backend": os.getenv("LOCAL_MODEL_BACKEND", "llama_cpp"),
        },
        "persona": {
            "context_path": os.getenv("PERSONA_CONTEXT_PATH"),
        },
        "echo_memory": {
            "endpoint": os.getenv("ECHO_MEMORY_ENDPOINT"),
            "namespace_id": os.getenv("ECHO_MEMORY_NAMESPACE_ID", "local-default"),
        },
        "debug": os.getenv("SESSION_CONTROL_DEBUG", "false").lower() == "true",
        "log_level": os.getenv("SESSION_CONTROL_LOG_LEVEL", "INFO"),
    }


def get_backend_config(backend: Optional[str] = None) -> Dict[str, Any]:
    """取得指定後端的設定"""
    config = get_config()
    backend = backend or config["backend"]
    return config.get(backend, {})
