# utils/logger.py

import os
import logging
from datetime import datetime
from configs.config_loader import load_config

_config = load_config()
conf = _config.get("logging", {})
enabled = conf.get("enabled", True)

class LogLevelFilter(logging.Filter):
    def __init__(self, min_level, max_level):
        self.min_level = min_level
        self.max_level = max_level
        super().__init__()

    def filter(self, record):
        return self.min_level <= record.levelno <= self.max_level

class ColorFormatter(logging.Formatter):
    COLORS = {
        'DEBUG': '\033[36m',    # Cyan
        'INFO': '\033[32m',     # Green
        'WARNING': '\033[33m',  # Yellow
        'ERROR': '\033[31m',    # Red
        'CRITICAL': '\033[41m', # Red background
    }
    RESET = '\033[0m'

    def format(self, record):
        color = self.COLORS.get(record.levelname, self.RESET)
        message = super().format(record)
        return f"{color}{message}{self.RESET}"


LOG_LEVEL = conf.get("log_level", "DEBUG").upper()
LOG_DIR = conf.get("log_dir", "logs")
SPLIT_LOGS = conf.get("enable_split_logs", True)

# 時間戳
timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

# 建立 log 資料夾
os.makedirs(LOG_DIR, exist_ok=True)

# 建立子資料夾
if SPLIT_LOGS:
    os.makedirs(os.path.join(LOG_DIR, "debug"), exist_ok=True)
    os.makedirs(os.path.join(LOG_DIR, "runtime"), exist_ok=True)
    os.makedirs(os.path.join(LOG_DIR, "error"), exist_ok=True)

# 檔名格式
def log_file(name): 
    if SPLIT_LOGS: return os.path.join(LOG_DIR, name, f"{name}-{timestamp}.log")
    return os.path.join(LOG_DIR, f"{name}-{timestamp}.log")

# 建立 logger
logger = logging.getLogger("UEP")
logger.setLevel(getattr(logging, LOG_LEVEL))

formatter = logging.Formatter("[%(asctime)s] %(levelname)s - %(message)s")
console_formatter = ColorFormatter(
    "\n[%(asctime)s] %(levelname)s - %(message)s\n", datefmt="%H:%M:%S")

# 主輸出（console）
stream_handler = logging.StreamHandler()
stream_handler.setFormatter(console_formatter)
logger.addHandler(stream_handler)

# 拆分 log 檔案
if SPLIT_LOGS:
    debug_file = logging.FileHandler(log_file("debug"))
    debug_file.setFormatter(formatter)
    debug_file.setLevel(logging.DEBUG)
    debug_file.addFilter(LogLevelFilter(logging.DEBUG, logging.DEBUG))
    logger.addHandler(debug_file)

    info_file = logging.FileHandler(log_file("runtime"))
    info_file.setFormatter(formatter)
    info_file.setLevel(logging.INFO)
    info_file.addFilter(LogLevelFilter(logging.INFO, logging.WARNING))
    logger.addHandler(info_file)

    error_file = logging.FileHandler(log_file("error"))
    error_file.setFormatter(formatter)
    error_file.setLevel(logging.ERROR)
    error_file.addFilter(LogLevelFilter(logging.ERROR, logging.CRITICAL))
    logger.addHandler(error_file)
else:
    combined_file = logging.FileHandler(log_file("combined"))
    combined_file.setFormatter(formatter)
    logger.addHandler(combined_file)


if not enabled: 
    # Remove log files
    for handler in logger.handlers:
        if isinstance(handler, logging.FileHandler):
            handler.close()
            os.remove(handler.baseFilename)

    if stream_handler in logger.handlers:
        logger.removeHandler(stream_handler)
        stream_handler.close()
