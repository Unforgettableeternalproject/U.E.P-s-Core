"""以標準 logging 取代 Core 舊 ``utils.debug_helper`` 的簡易日誌介面。

遷入 Core 後，echo_tts 不再靠 ``sys.path`` 把自己的 ``utils`` 變成頂層套件，
所以日誌一律走標準 logging，由 Core 的設定決定輸出。
"""
from __future__ import annotations

import logging

logger = logging.getLogger("echo_tts")

# 舊 debug_log 的等級（數字越大越細）對應到標準 logging 等級
_LEVELS = {1: logging.INFO, 2: logging.DEBUG, 3: logging.DEBUG}


def debug_log(level: int, message: str) -> None:
    logger.log(_LEVELS.get(level, logging.DEBUG), message)


def debug_log_e(level: int, message: str) -> None:
    logger.log(_LEVELS.get(level, logging.DEBUG), message)


def info_log(message: str) -> None:
    logger.info(message)


def error_log(message: str) -> None:
    logger.error(message)
