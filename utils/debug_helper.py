# utils/debug_helper.py

from configs.config_loader import load_config
from utils.logger import logger

_config = load_config()
_debug_conf = _config.get("debug", {})
_debug_enabled = _debug_conf.get("debug", False)
_debug_level = _debug_conf.get("debug_level", 1)

def debug_log(level: int, msg: str, exclusive: bool = False):
    if _debug_enabled:
        if exclusive:
            if level == _debug_level: logger.debug(msg)
        else:
            if level <= _debug_level: logger.debug(msg)

def info_log(msg: str, level: str = "INFO"):
    level = level.upper()
    if level == "INFO":
        logger.info(msg)
    elif level == "WARNING":
        logger.warning(msg)
    else:
        logger.info(f"[Unrecognized level '{level}'] {msg}")

def error_log(msg: str, level: str = "ERROR"):
    level = level.upper()
    if level == "ERROR":
        logger.error(msg)
    elif level == "CRITICAL":
        logger.critical(msg)
    else:
        logger.error(f"[Unrecognized level '{level}'] {msg}")
