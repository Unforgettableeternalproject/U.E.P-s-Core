# utils/debug_helper.py

from configs.config_loader import load_config
from utils.logger import logger

def debug_log(level: int, message: str):
    config = load_config()
    debug_enabled = config.get("debug", {}).get("debug", False)
    debug_level = config.get("debug", {}).get("debug_level", 1)

    if debug_enabled and level <= debug_level:
        logger.debug(message)