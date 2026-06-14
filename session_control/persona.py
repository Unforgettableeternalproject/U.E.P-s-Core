"""
Persona & LLM Config Loader — 人設與 LLM 設定載入

仿照 TestSeperateMemorySystem 的最小遷移模式：
- 從 llm_config.yaml 載入 U.E.P 角色人格與 API 設定
- 不搞 PromptManager 那套複雜的多模式 prompt 組裝
- 用 15 行程式碼完成 system_instruction 拼接

角色人格與 U.E.P Core 的 base_personality 逐字相同（去掉動態狀態注入）。
"""

import logging
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

from session_control.schemas.config import PersonaContext

logger = logging.getLogger(__name__)

# 設定檔預設路徑
_DEFAULT_CONFIG_PATH = Path(__file__).parent / "llm_config.yaml"


def load_llm_config(config_path: Optional[Path] = None) -> Dict[str, Any]:
    """載入 LLM 設定檔

    Args:
        config_path: 自訂設定檔路徑（None 使用預設 llm_config.yaml）

    Returns:
        設定 dict，包含 model、temperature、character 等欄位
    """
    path = config_path or _DEFAULT_CONFIG_PATH
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}
        logger.debug("[Persona] 載入設定檔: %s", path)
        return config

    logger.warning("[Persona] 設定檔不存在: %s，使用空設定", path)
    return {}


def build_persona_from_config(config: Optional[Dict[str, Any]] = None) -> PersonaContext:
    """從 LLM config 建立 PersonaContext

    Args:
        config: LLM 設定 dict（None 則自動載入 llm_config.yaml）

    Returns:
        PersonaContext 實例，可直接傳入 ContextManager
    """
    if config is None:
        config = load_llm_config()

    character = config.get("character", "")

    return PersonaContext(
        name="U.E.P",
        traits=["cheerful", "optimistic", "sociable", "curious", "mischievous"],
        speaking_style="casual, friendly, like a human friend — no emojis, prefers kaomojis",
        user_preferences={"language": "en"},
        custom_instructions=character,
    )


def build_system_instruction(
    config: Optional[Dict[str, Any]] = None,
    extra_context: Optional[str] = None,
) -> str:
    """組裝系統指令（仿 TestMemory 的 15 行拼接）

    Args:
        config: LLM 設定 dict（None 則自動載入 llm_config.yaml）
        extra_context: 額外的上下文（如記憶、任務等）

    Returns:
        完整的 system_instruction 字串
    """
    if config is None:
        config = load_llm_config()

    parts = [config.get("character", "")]

    if extra_context:
        parts.append(extra_context)

    compaction_preamble = config.get("compaction_preamble", "")
    if compaction_preamble:
        parts.append(compaction_preamble)

    return "\n\n".join(p for p in parts if p)
