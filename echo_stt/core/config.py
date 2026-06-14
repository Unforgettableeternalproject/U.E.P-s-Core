"""
echo_stt 本地配置載入器

替代 U.E.P Core 的 configs.config_loader 和 configs.user_settings_manager，
從 configs/stt_config.yaml 讀取設定。
"""

import os
import logging
from typing import Any, Optional

import yaml

logger = logging.getLogger(__name__)

# 預設配置檔路徑（相對於專案根目錄）
_DEFAULT_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "configs",
    "stt_config.yaml",
)

# 模組層級快取
_config_cache: Optional[dict] = None


def load_config(path: Optional[str] = None, *, force_reload: bool = False) -> dict:
    """載入 YAML 配置檔。

    Args:
        path: 配置檔絕對路徑。若為 None 則使用預設路徑。
        force_reload: 強制重新讀取（忽略快取）。

    Returns:
        解析後的設定字典；檔案不存在或解析失敗時回傳空 dict。
    """
    global _config_cache

    if _config_cache is not None and not force_reload and path is None:
        return _config_cache

    config_path = path or _DEFAULT_CONFIG_PATH

    if not os.path.isfile(config_path):
        logger.warning("配置檔不存在: %s，使用空設定", config_path)
        _config_cache = {}
        return _config_cache

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        _config_cache = data if isinstance(data, dict) else {}
        logger.info("配置檔載入成功: %s", config_path)
        return _config_cache
    except Exception as e:
        logger.error("配置檔載入失敗: %s — %s", config_path, e)
        _config_cache = {}
        return _config_cache


def get(key_path: str, default: Any = None, *, config: Optional[dict] = None) -> Any:
    """以 dot-notation 取得配置值。

    Examples:
        >>> get("whisper.model_id")
        'openai/whisper-base'
        >>> get("audio.sample_rate", 16000)
        16000

    Args:
        key_path: 以 "." 分隔的鍵路徑，如 ``"whisper.model_id"``。
        default: 鍵不存在時的預設值。
        config: 可選，指定要查詢的 dict（預設用 load_config() 的快取）。

    Returns:
        查詢到的值，或 default。
    """
    data = config if config is not None else load_config()
    keys = key_path.split(".")
    current = data
    for key in keys:
        if isinstance(current, dict) and key in current:
            current = current[key]
        else:
            return default
    return current


def reset_cache() -> None:
    """清除配置快取，下次 load_config() 會重新讀取檔案。"""
    global _config_cache
    _config_cache = None
