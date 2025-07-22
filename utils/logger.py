import os
import sys
import logging
from datetime import datetime
from configs.config_loader import load_config

_config = load_config()
conf = _config.get("logging", {})
enabled = conf.get("enabled", True)

logging.getLogger("faiss").setLevel(logging.ERROR)
logging.getLogger("fairseq").setLevel(logging.ERROR)
logging.getLogger("torch").setLevel(logging.ERROR)
logging.getLogger("transformers").setLevel(logging.ERROR)
logging.getLogger("datasets").setLevel(logging.ERROR)
logging.getLogger("google_genai").setLevel(logging.ERROR)
logging.getLogger("httpx").setLevel(logging.ERROR)

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
        # 確保訊息是純 ASCII 或能被控制台編碼處理
        message = super().format(record)
        try:
            # 嘗試用控制台編碼來編碼，如果失敗則替換不支援的字符
            message.encode(sys.stdout.encoding, errors='replace').decode(sys.stdout.encoding)
        except (UnicodeError, AttributeError):
            # 替換不支援的 Unicode 字符
            message = ''.join(char if ord(char) < 128 else '?' for char in message)
        return f"{color}{message}{self.RESET}"

LOG_LEVEL = conf.get("log_level", "DEBUG").upper()
LOG_DIR = conf.get("log_dir", "logs")
SPLIT_LOGS = conf.get("enable_split_logs", True)

# 時間戳
now = datetime.now()
timestamp = now.strftime("%Y-%m-%d_%H-%M-%S")
month_folder = now.strftime("%Y-%m")  # 年月格式，例如：2025-07

# 建立 log 資料夾
os.makedirs(LOG_DIR, exist_ok=True)

# 建立月份資料夾
month_path = os.path.join(LOG_DIR, month_folder)
os.makedirs(month_path, exist_ok=True)

# 建立子資料夾
if SPLIT_LOGS:
    os.makedirs(os.path.join(month_path, "debug"), exist_ok=True)
    os.makedirs(os.path.join(month_path, "runtime"), exist_ok=True)
    os.makedirs(os.path.join(month_path, "error"), exist_ok=True)

# 檔名格式
def log_file(name): 
    if SPLIT_LOGS: return os.path.join(month_path, name, f"{name}-{timestamp}.log")
    return os.path.join(month_path, f"{name}-{timestamp}.log")

# 建立 logger
logger = logging.getLogger("UEP")
logger.setLevel(getattr(logging, LOG_LEVEL))
logger.propagate = False

formatter = logging.Formatter("[%(asctime)s] %(levelname)s - %(message)s")
console_formatter = ColorFormatter(
    "\n[%(asctime)s] %(levelname)s - %(message)s\n", datefmt="%H:%M:%S")

# 檢查是否已經添加過 handler
if not logger.handlers:
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

# 測試結束後刪除 0 Bytes 的檔案
def cleanup_empty_log_files():
    for handler in logger.handlers:
        if isinstance(handler, logging.FileHandler):
            handler.close()
            try:
                if os.path.getsize(handler.baseFilename) == 0:
                    os.remove(handler.baseFilename)
                    
                    # 檢查並清理空文件夾
                    dir_path = os.path.dirname(handler.baseFilename)
                    if os.path.exists(dir_path) and not os.listdir(dir_path):
                        os.rmdir(dir_path)  # 如果文件夾為空，則刪除它
                        
                        # 檢查月份文件夾是否為空
                        month_dir = os.path.dirname(dir_path)
                        if os.path.exists(month_dir) and not os.listdir(month_dir):
                            os.rmdir(month_dir)  # 如果月份文件夾為空，則刪除它
                        
            except Exception as e:
                print(f"\n無法移除記錄檔或空文件夾：{str(e)}\n")

# 定期清理舊日誌文件（默認保留3個月）
def cleanup_old_logs(retention_months=3):
    try:
        if not os.path.exists(LOG_DIR):
            return
            
        current_month = datetime.now().strftime("%Y-%m")
        year, month = map(int, current_month.split('-'))
        
        # 掃描日誌根目錄
        for folder_name in os.listdir(LOG_DIR):
            folder_path = os.path.join(LOG_DIR, folder_name)
            
            # 檢查是否是月份文件夾格式 (YYYY-MM)
            if os.path.isdir(folder_path) and len(folder_name) == 7 and folder_name[4] == '-':
                try:
                    folder_year, folder_month = map(int, folder_name.split('-'))
                    
                    # 計算月份差距
                    months_diff = (year - folder_year) * 12 + (month - folder_month)
                    
                    # 如果超過保留期限，則刪除整個文件夾
                    if months_diff > retention_months:
                        import shutil
                        shutil.rmtree(folder_path)
                        print(f"\n已清理舊日誌文件夾：{folder_name}（超過{retention_months}個月）\n")
                except ValueError:
                    # 不是有效的月份格式文件夾，略過
                    continue
    except Exception as e:
        print(f"\n清理舊日誌時出錯：{str(e)}\n")

if not enabled: 
    # Remove log files
    cleanup_empty_log_files()

    if stream_handler in logger.handlers:
        logger.removeHandler(stream_handler)
        stream_handler.close()
else:
    cleanup_empty_log_files()
    
    # 每次啟動時檢查並清理舊日誌
    retention_months = conf.get("log_retention_months", 3)
    cleanup_old_logs(retention_months)


