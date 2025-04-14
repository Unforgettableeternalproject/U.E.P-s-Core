# utils/logger.py

import logging
from configs.config_loader import load_config

config = load_config()
debug_conf = config.get("debug", {})
LOG_LEVEL = debug_conf.get("log_level", "DEBUG").upper()
LOG_FILE = debug_conf.get("log_file", "debug.log")

logger = logging.getLogger("UEP")
logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))

formatter = logging.Formatter("[%(asctime)s] %(levelname)s - %(message)s")

stream_handler = logging.StreamHandler()
stream_handler.setFormatter(formatter)
logger.addHandler(stream_handler)

if LOG_FILE:
    file_handler = logging.FileHandler(LOG_FILE)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
