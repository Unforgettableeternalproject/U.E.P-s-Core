# utils/debug_helper.py
"""
除錯輔助模組

提供多等級日誌記錄功能，支援不同的除錯詳細程度
"""

from configs.config_loader import load_config
from utils.logger import logger

# 除錯等級常量定義
KEY_LEVEL = 1  # 關鍵事件、錯誤和重要狀態變更 (極少)
OPERATION_LEVEL = 2  # 警告、一般操作和中等重要性的事件 (適中)
SYSTEM_LEVEL = 3  # 詳細信息、運行狀態、一般流程 (較多)
ELABORATIVE_LEVEL = 4  # 非常詳細的除錯信息 (大量)

# 載入配置
_config = load_config()
_debug_conf = _config.get("debug", {})
_debug_enabled = _debug_conf.get("enabled", False)
_debug_level = _debug_conf.get("debug_level", KEY_LEVEL)  # 默認使用最低等級
_logging_conf = _config.get("logging", {})
_logging_enabled = _logging_conf.get("enabled", True)

if not _logging_enabled: print("[Logging] Logging is disabled in the configuration.")

def debug_log(level: int=1, msg: str="", exclusive: bool = False):
    """
    記錄除錯日誌，根據設定的等級決定是否輸出
    
    Args:
        level: 除錯等級 (1-4)
            1 (KEY_LEVEL): 關鍵事件、和重要狀態變更 (極少)
            2 (OPERATION_LEVEL): 警告、一般操作和中等重要性的事件 (適中)
            3 (SYSTEM_LEVEL): 詳細信息、運行狀態、一般流程 (較多)
            4 (ELABORATIVE_LEVEL): 非常詳細的除錯信息 (大量)
        msg: 日誌訊息
        exclusive: 是否為嚴格等級匹配模式
                  True: 僅當 level 與配置的 _debug_level 完全相同時輸出
                  False: 當 level 小於或等於配置的 _debug_level 時輸出
    """

    if not _logging_enabled:
        return

    if _debug_enabled:
        if exclusive:
            if level == _debug_level: logger.debug(msg)
        else:
            if level <= _debug_level: logger.debug(msg)

def debug_log_e(level: int=1, msg: str=""):
    debug_log(level, msg, True)

def info_log(msg: str, type: str = "INFO"):

    if not _logging_enabled:
        return

    type = type.upper()
    if type == "INFO":
        logger.info(msg)
    elif type == "WARNING":
        logger.warning(msg)
    else:
        logger.info(f"[Unrecognized type '{type}'] {msg}")

def error_log(msg: str, type: str = "ERROR"):

    if not _logging_enabled:
        return

    type = type.upper()
    if type == "ERROR":
        logger.error(msg)
    elif type == "CRITICAL":
        logger.critical(msg)
    else:
        logger.error(f"[Unrecognized type '{type}'] {msg}")
        
def get_debug_level() -> int:
    """獲取當前除錯等級"""
    return _debug_level

def set_debug_level(level: int):
    """
    動態設置除錯等級
    
    Args:
        level: 除錯等級 (1-4)
            1 (KEY_LEVEL): 關鍵事件、和重要狀態變更 (極少)
            2 (OPERATION_LEVEL): 警告、一般操作和中等重要性的事件 (適中)
            3 (SYSTEM_LEVEL): 詳細信息、運行狀態、一般流程 (較多)
            4 (ELABORATIVE_LEVEL): 非常詳細的除錯信息 (大量)
    """
    global _debug_level
    if level < 1 or level > 4:
        logger.warning(f"[DebugHelper] 無效的除錯等級 {level}，應該在 1-4 之間")
        return
    _debug_level = level
    logger.info(f"[DebugHelper] 除錯等級已設置為 {level}")
