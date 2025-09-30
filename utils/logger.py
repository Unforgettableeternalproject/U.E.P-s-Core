import os
import sys
import logging
from datetime import datetime
from configs.config_loader import load_config
import traceback
import inspect

_config = load_config()
conf = _config.get("logging", {})
enabled = conf.get("enabled", True)

def should_write_file_logs():
    """檢查當前調用堆疊，只有在真正的系統運行時才寫入文件日誌"""
    try:
        stack = inspect.stack()
        for frame_info in stack:
            filename = frame_info.filename.lower()
            
            # 主要系統運行入口點
            if (('system_initializer' in filename) or 
                ('debug_api' in filename) or
                ('production_runner' in filename) or
                ('entry.py' in filename.lower())):
                return True
            
            # Debug 相關入口點
            if (('debugger.py' in filename) or
                ('debug_helper' in filename) or
                ('devtools' in filename and 'test' not in filename)):
                return True
            
            # 生產環境和系統核心
            if (('core' in filename and ('framework' in filename or 'controller' in filename)) or
                ('production' in filename)):
                return True
                
        # 如果沒有找到系統運行的證據，檢查是否在測試環境
        for frame_info in stack:
            filename = frame_info.filename.lower()
            if (('test_' in filename) or 
                ('pytest' in filename) or
                ('unittest' in filename)):
                return False  # 明確的測試環境
                
        return False
    except Exception:
        # 如果檢查失敗，預設使用文件日誌（保守策略）
        return True

def force_enable_file_logging():
    """強制啟用文件日誌記錄（用於測試或特殊情況）"""
    global _file_handlers_added
    
    if _file_handlers_added:
        return
        
    if not SPLIT_LOGS:
        return
        
    try:
        print("🔍 強制啟用文件日誌記錄")
        cleanup_monthly_logs()
        
        # Debug 日誌
        try:
            debug_path = log_file("debug")
            debug_file = logging.FileHandler(debug_path, encoding='utf-8')
            debug_file.setFormatter(formatter)
            debug_file.setLevel(logging.DEBUG)
            debug_file.addFilter(LogLevelFilter(logging.DEBUG, logging.DEBUG))
            logger.addHandler(debug_file)
        except Exception:
            pass
        
        # Runtime 日誌
        try:
            runtime_path = log_file("runtime")
            info_file = logging.FileHandler(runtime_path, encoding='utf-8')
            info_file.setFormatter(formatter)
            info_file.setLevel(logging.INFO)
            info_file.addFilter(LogLevelFilter(logging.INFO, logging.WARNING))
            logger.addHandler(info_file)
        except Exception:
            pass
        
        # Error 日誌
        try:
            error_path = log_file("error")
            error_file = logging.FileHandler(error_path, encoding='utf-8')
            error_file.setFormatter(formatter)
            error_file.setLevel(logging.ERROR)
            error_file.addFilter(LogLevelFilter(logging.ERROR, logging.CRITICAL))
            logger.addHandler(error_file)
        except Exception:
            pass
        
        _file_handlers_added = True
        print("✅ 文件日誌記錄已強制啟用")
        
    except Exception as e:
        print(f"強制啟用文件日誌時出錯: {str(e)}")

# 設置第三方庫的日誌級別為 ERROR
logging.getLogger("faiss").setLevel(logging.ERROR)
logging.getLogger("fairseq").setLevel(logging.ERROR)
logging.getLogger("torch").setLevel(logging.ERROR)
logging.getLogger("transformers").setLevel(logging.ERROR)
logging.getLogger("datasets").setLevel(logging.ERROR)
logging.getLogger("google_genai").setLevel(logging.ERROR)
logging.getLogger("httpx").setLevel(logging.ERROR)

class LogLevelFilter(logging.Filter):
    def __init__(self, min_level, max_level):
        super().__init__()
        self.min_level = min_level
        self.max_level = max_level

    def filter(self, record):
        return self.min_level <= record.levelno <= self.max_level

class ColorFormatter(logging.Formatter):
    COLORS = {
        'DEBUG': '\033[36m',    # 青色
        'INFO': '\033[32m',     # 綠色
        'WARNING': '\033[33m',  # 黃色
        'ERROR': '\033[31m',    # 紅色
        'CRITICAL': '\033[35m', # 紫色
    }
    RESET = '\033[0m'

    def format(self, record):
        log_color = self.COLORS.get(record.levelname, self.RESET)
        record.levelname = f"{log_color}{record.levelname}{self.RESET}"
        return super().format(record)

if not enabled:
    logging.disable(logging.CRITICAL)
    logger = logging.getLogger("UEP")
    logger.disabled = True
else:
    LOG_LEVEL = conf.get("level", "INFO").upper()
    LOG_DIR = conf.get("log_dir", "logs")
    SPLIT_LOGS = conf.get("split_logs", True)
    MAX_FILES_PER_MONTH = conf.get("max_files_per_month", 15)

    os.makedirs(LOG_DIR, exist_ok=True)

    def cleanup_monthly_logs():
        """限制每月日誌文件數量，保留最新的指定數量文件"""
        try:
            if not SPLIT_LOGS:
                return
                
            for log_type in ["debug", "runtime", "error"]:
                type_path = os.path.join(LOG_DIR, log_type)
                if not os.path.exists(type_path):
                    continue
                    
                for month_dir in os.listdir(type_path):
                    month_path = os.path.join(type_path, month_dir)
                    if not os.path.isdir(month_path):
                        continue
                    
                    # 獲取該月份的所有日誌文件
                    log_files = []
                    for file in os.listdir(month_path):
                        if file.endswith('.log'):
                            file_path = os.path.join(month_path, file)
                            log_files.append((file_path, os.path.getmtime(file_path)))
                    
                    # 按修改時間排序，保留最新的文件
                    if len(log_files) > MAX_FILES_PER_MONTH:
                        log_files.sort(key=lambda x: x[1], reverse=True)
                        files_to_keep = log_files[:MAX_FILES_PER_MONTH]
                        files_to_delete = log_files[MAX_FILES_PER_MONTH:]
                        
                        for file_path, _ in files_to_delete:
                            try:
                                os.remove(file_path)
                                print(f"已刪除舊日誌: {os.path.basename(file_path)}")
                            except Exception:
                                pass
                        
                        print(f"已清理 {log_type}/{month_dir}: 保留 {len(files_to_keep)} 個最新文件，刪除 {len(files_to_delete)} 個舊文件")
                        
        except Exception as e:
            pass

    def log_file(name):
        try:
            timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            if SPLIT_LOGS:
                current_month = datetime.now().strftime("%Y-%m")
                type_path = os.path.join(LOG_DIR, name)
                month_path = os.path.join(type_path, current_month)
                os.makedirs(month_path, exist_ok=True)
                return os.path.join(month_path, f"{name}-{timestamp}.log")
            else:
                return os.path.join(LOG_DIR, f"{name}-{timestamp}.log")
        except Exception:
            return os.path.join(LOG_DIR, f"fallback-{timestamp}.log")

    # 建立 logger
    logger = logging.getLogger("UEP")
    logger.setLevel(getattr(logging, LOG_LEVEL))
    logger.propagate = False

    formatter = logging.Formatter("[%(asctime)s] %(levelname)s - %(message)s")
    console_formatter = ColorFormatter(
        "\n[%(asctime)s] %(levelname)s - %(message)s\n", datefmt="%H:%M:%S")

    # 清空現有處理程序
    for handler in list(logger.handlers):
        logger.removeHandler(handler)

    # 添加控制台處理程序
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(console_formatter)
    logger.addHandler(stream_handler)

    # 文件日誌處理器變數
    _file_handlers_added = False

    def enable_file_logging():
        """動態啟用文件日誌記錄"""
        global _file_handlers_added
        
        if _file_handlers_added:
            return
            
        if not should_write_file_logs():
            return
            
        if not SPLIT_LOGS:
            return
            
        try:
            print("🔍 檢測到系統運行，啟用文件日誌記錄")
            cleanup_monthly_logs()
            
            # Debug 日誌
            try:
                debug_path = log_file("debug")
                debug_file = logging.FileHandler(debug_path, encoding='utf-8')
                debug_file.setFormatter(formatter)
                debug_file.setLevel(logging.DEBUG)
                debug_file.addFilter(LogLevelFilter(logging.DEBUG, logging.DEBUG))
                logger.addHandler(debug_file)
            except Exception:
                pass
            
            # Runtime 日誌
            try:
                runtime_path = log_file("runtime")
                info_file = logging.FileHandler(runtime_path, encoding='utf-8')
                info_file.setFormatter(formatter)
                info_file.setLevel(logging.INFO)
                info_file.addFilter(LogLevelFilter(logging.INFO, logging.WARNING))
                logger.addHandler(info_file)
            except Exception:
                pass
            
            # Error 日誌
            try:
                error_path = log_file("error")
                error_file = logging.FileHandler(error_path, encoding='utf-8')
                error_file.setFormatter(formatter)
                error_file.setLevel(logging.ERROR)
                error_file.addFilter(LogLevelFilter(logging.ERROR, logging.CRITICAL))
                logger.addHandler(error_file)
            except Exception:
                pass
            
            _file_handlers_added = True
            print("✅ 文件日誌記錄已啟用")
            
        except Exception as e:
            print(f"啟用文件日誌時出錯: {str(e)}")

    # 初始化時不啟用文件日誌，等待動態啟用
    print("📺 日誌系統已初始化，等待動態啟用文件記錄")

# 公開函數
def cleanup_empty_log_files():
    """清理空的日誌文件"""
    try:
        if not enabled:
            return
            
        for handler in logger.handlers:
            if isinstance(handler, logging.FileHandler):
                handler.close()
        
        for root, dirs, files in os.walk(LOG_DIR):
            for file in files:
                if file.endswith('.log'):
                    file_path = os.path.join(root, file)
                    try:
                        if os.path.getsize(file_path) == 0:
                            os.remove(file_path)
                    except (OSError, FileNotFoundError):
                        pass
    except Exception:
        pass

def get_logger():
    """獲取日誌記錄器"""
    if enabled:
        # 每次獲取時檢查是否需要啟用文件日誌
        enable_file_logging()
        return logger
    else:
        null_logger = logging.getLogger("UEP_NULL")
        null_logger.disabled = True
        return null_logger

# 為了兼容性，直接提供 logger
if enabled:
    # 暴露全局 logger，但建議使用 get_logger()
    logger = logger
else:
    logger = logging.getLogger("UEP_NULL")
    logger.disabled = True