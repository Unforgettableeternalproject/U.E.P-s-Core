import os
import sys
import logging
from datetime import datetime
from configs.config_loader import load_config
import traceback  # 添加追蹤堆疊的功能

_config = load_config()
conf = _config.get("logging", {})
enabled = conf.get("enabled", True)

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

# 確保日誌根目錄存在
try:
    os.makedirs(LOG_DIR, exist_ok=True)
    # print(f"日誌目錄: {LOG_DIR} 已確保存在")
except Exception as e:
    print(f"創建日誌目錄錯誤: {str(e)}")

# 日誌文件名格式函數
def log_file(name):
    """根據類型和月份返回日誌文件路徑"""
    # 每次調用時獲取當前時間
    now = datetime.now()
    timestamp = now.strftime("%Y-%m-%d_%H-%M-%S")
    month_folder = now.strftime("%Y-%m")  # 年月格式，例如：2025-07
    
    try:
        if SPLIT_LOGS:
            # 首先確保主類型目錄存在 (debug/runtime/error)
            log_type_path = os.path.join(LOG_DIR, name)
            try:
                if not os.path.exists(log_type_path):
                    os.makedirs(log_type_path, exist_ok=True)
                    # print(f"創建日誌類型目錄: {log_type_path}")
            except Exception as e:
                # print(f"創建目錄失敗 {log_type_path}: {str(e)}")
                # print(f"堆疊追蹤:\n{traceback.format_exc()}")
                # 回退到主日誌目錄
                return os.path.join(LOG_DIR, f"{name}-{timestamp}.log")
            
            # 在主類型目錄下創建月份子目錄
            month_path = os.path.join(log_type_path, month_folder)
            try:
                if not os.path.exists(month_path):
                    os.makedirs(month_path, exist_ok=True)
                    # print(f"創建月份子目錄: {month_path}")
            except Exception as e:
                # print(f"創建月份目錄失敗 {month_path}: {str(e)}")
                # print(f"堆疊追蹤:\n{traceback.format_exc()}")
                # 回退到主類型目錄
                return os.path.join(log_type_path, f"{name}-{timestamp}.log")
                
            # 返回完整路徑：logs/類型/年-月/類型-時間戳.log
            return os.path.join(month_path, f"{name}-{timestamp}.log")
        else:
            # 如果未拆分日誌，則使用根目錄下的月份目錄
            month_path = os.path.join(LOG_DIR, month_folder)
            try:
                if not os.path.exists(month_path):
                    os.makedirs(month_path, exist_ok=True)
                    # print(f"創建月份目錄: {month_path}")
            except Exception as e:
                # print(f"創建月份目錄失敗 {month_path}: {str(e)}")
                # 回退到主日誌目錄
                return os.path.join(LOG_DIR, f"{name}-{timestamp}.log")
                
            return os.path.join(month_path, f"{name}-{timestamp}.log")
    except Exception as e:
        # print(f"生成日誌路徑出錯: {str(e)}")
        # print(f"堆疊追蹤:\n{traceback.format_exc()}")
        # 安全回退
        return os.path.join(LOG_DIR, f"fallback-{timestamp}.log")

# 建立 logger
logger = logging.getLogger("UEP")
logger.setLevel(getattr(logging, LOG_LEVEL))
logger.propagate = False

formatter = logging.Formatter("[%(asctime)s] %(levelname)s - %(message)s")
console_formatter = ColorFormatter(
    "\n[%(asctime)s] %(levelname)s - %(message)s\n", datefmt="%H:%M:%S")

# 初始化日誌處理器變數
logger = logging.getLogger("UEP")
logger.setLevel(getattr(logging, LOG_LEVEL))
logger.propagate = False

# 基本格式化程序
formatter = logging.Formatter("[%(asctime)s] %(levelname)s - %(message)s")
console_formatter = ColorFormatter(
    "\n[%(asctime)s] %(levelname)s - %(message)s\n", datefmt="%H:%M:%S")

# 清空現有處理程序 (確保不會重複添加)
for handler in list(logger.handlers):
    logger.removeHandler(handler)

# 添加控制台處理程序
stream_handler = logging.StreamHandler()
stream_handler.setFormatter(console_formatter)
logger.addHandler(stream_handler)
# print("已添加控制台日誌處理程序")

# 提前創建所有必要的日誌目錄
if SPLIT_LOGS:
    # 當前月份
    current_month = datetime.now().strftime("%Y-%m")
    
    # 建立主類型目錄和月份子目錄
    for log_type in ["debug", "runtime", "error"]:
        # 主類型目錄
        type_path = os.path.join(LOG_DIR, log_type)
        try:
            if not os.path.exists(type_path):
                os.makedirs(type_path, exist_ok=True)
                # print(f"創建日誌類型目錄: {type_path}")
        except Exception as e:
            pass
            # print(f"無法創建日誌目錄 {type_path}: {str(e)}")
            
        # 月份子目錄
        month_path = os.path.join(type_path, current_month)
        try:
            if not os.path.exists(month_path):
                os.makedirs(month_path, exist_ok=True)
                # print(f"創建月份子目錄: {month_path}")
        except Exception as e:
            pass
            # print(f"無法創建月份子目錄 {month_path}: {str(e)}")

# 添加文件處理程序
try:
    if SPLIT_LOGS:
        # Debug 日誌
        try:
            debug_path = log_file("debug")
            debug_file = logging.FileHandler(debug_path, encoding='utf-8')
            debug_file.setFormatter(formatter)
            debug_file.setLevel(logging.DEBUG)
            debug_file.addFilter(LogLevelFilter(logging.DEBUG, logging.DEBUG))
            logger.addHandler(debug_file)
            # print(f"已添加 DEBUG 日誌處理程序: {debug_path}")
        except Exception as e:
            # print(f"無法創建 debug 日誌檔案: {str(e)}")
            print(traceback.format_exc())
        
        # Runtime 日誌
        try:
            runtime_path = log_file("runtime")
            info_file = logging.FileHandler(runtime_path, encoding='utf-8')
            info_file.setFormatter(formatter)
            info_file.setLevel(logging.INFO)
            info_file.addFilter(LogLevelFilter(logging.INFO, logging.WARNING))
            logger.addHandler(info_file)
            # print(f"已添加 RUNTIME 日誌處理程序: {runtime_path}")
        except Exception as e:
            print(f"無法創建 runtime 日誌檔案: {str(e)}")
            print(traceback.format_exc())
        
        # Error 日誌
        try:
            error_path = log_file("error")
            error_file = logging.FileHandler(error_path, encoding='utf-8')
            error_file.setFormatter(formatter)
            error_file.setLevel(logging.ERROR)
            error_file.addFilter(LogLevelFilter(logging.ERROR, logging.CRITICAL))
            logger.addHandler(error_file)
            # print(f"已添加 ERROR 日誌處理程序: {error_path}")
        except Exception as e:
            print(f"無法創建 error 日誌檔案: {str(e)}")
            print(traceback.format_exc())
    else:
        # 合併日誌
        try:
            combined_path = log_file("combined")
            combined_file = logging.FileHandler(combined_path, encoding='utf-8')
            combined_file.setFormatter(formatter)
            logger.addHandler(combined_file)
            # print(f"已添加合併日誌處理程序: {combined_path}")
        except Exception as e:
            print(f"無法創建 combined 日誌檔案: {str(e)}")
            print(traceback.format_exc())
except Exception as e:
    print(f"設置日誌檔案處理器時出錯: {str(e)}")
    print(traceback.format_exc())

# 測試結束後刪除 0 Bytes 的檔案
def cleanup_empty_log_files():
    """清理空的日誌文件和文件夾，但保留關鍵目錄結構"""
    try:
        # 關閉所有日誌處理程序
        for handler in logger.handlers:
            if isinstance(handler, logging.FileHandler):
                try:
                    handler.close()
                except Exception as e:
                    print(f"關閉日誌處理程序時出錯: {str(e)}")
        
        # 檢查日誌目錄是否存在
        if not os.path.exists(LOG_DIR):
            return
            
        # 掃描並刪除空文件
        for root, dirs, files in os.walk(LOG_DIR):
            for file in files:
                try:
                    file_path = os.path.join(root, file)
                    if os.path.exists(file_path) and os.path.getsize(file_path) == 0:
                        os.remove(file_path)
                        # print(f"已刪除空日誌文件: {file_path}")
                except Exception as e:
                    print(f"刪除文件出錯 {file_path}: {str(e)}")
        
        # 獲取主要類型目錄列表和當前月份
        essential_types = ["debug", "runtime", "error"]
        essential_dirs = [os.path.join(LOG_DIR, d) for d in essential_types]
        current_month = datetime.now().strftime("%Y-%m")
        
        # 計算必須保留的目錄（主類型目錄和當月目錄）
        protected_dirs = essential_dirs.copy()
        for type_dir in essential_dirs:
            month_dir = os.path.join(type_dir, current_month)
            protected_dirs.append(month_dir)
        
        # 第二次掃描，刪除空文件夾 (從底層開始)，但保留關鍵目錄
        for root, dirs, files in os.walk(LOG_DIR, topdown=False):
            if not files and not dirs:
                # 不刪除根目錄、主類型目錄和當月目錄
                if (root != LOG_DIR and root not in protected_dirs):
                    try:
                        os.rmdir(root)
                        # print(f"已刪除空文件夾: {root}")
                    except Exception as e:
                        print(f"刪除文件夾出錯 {root}: {str(e)}")
    except Exception as e:
        print(f"清理空日誌文件時出錯: {str(e)}")
        print(traceback.format_exc())

# 組織日誌到月份文件夾的功能
def organize_logs_by_month():
    """將舊日誌文件按月份整理到子文件夾中（處理舊的日誌文件結構）"""
    try:
        if not os.path.exists(LOG_DIR):
            return
            
        # 處理常規日誌類型
        for log_type in ["debug", "runtime", "error"]:
            type_dir = os.path.join(LOG_DIR, log_type)
            if os.path.exists(type_dir) and os.path.isdir(type_dir):
                # 掃描該類型目錄下的所有日誌文件（只處理直接在類型目錄下的文件）
                for file in os.listdir(type_dir):
                    file_path = os.path.join(type_dir, file)
                    # 只處理文件，不處理目錄
                    if os.path.isfile(file_path) and file.endswith('.log'):
                        try:
                            # 從文件名中提取日期 (格式: type-YYYY-MM-DD_HH-MM-SS.log)
                            date_part = file.split('-', 1)[1] if '-' in file else None
                            if date_part and len(date_part) >= 10:
                                year_month = date_part[:7]  # YYYY-MM
                                
                                # 確保類型目錄下的月份子文件夾存在
                                month_dir = os.path.join(type_dir, year_month)
                                os.makedirs(month_dir, exist_ok=True)
                                
                                # 移動文件
                                target_path = os.path.join(month_dir, file)
                                
                                # 如果目標不存在，移動文件
                                if not os.path.exists(target_path):
                                    import shutil
                                    shutil.move(file_path, target_path)
                                    print(f"已移動日誌到月份子文件夾: {file} -> {month_dir}")
                        except Exception as e:
                            print(f"移動日誌文件時出錯 {file}: {str(e)}")
        
        # 處理根目錄下的合併日誌
        for file in os.listdir(LOG_DIR):
            file_path = os.path.join(LOG_DIR, file)
            if os.path.isfile(file_path) and file.endswith('.log'):
                try:
                    # 從文件名中提取日期
                    date_part = file.split('-', 1)[1] if '-' in file else None
                    if date_part and len(date_part) >= 10:
                        year_month = date_part[:7]  # YYYY-MM
                        
                        # 確保月份文件夾存在
                        month_dir = os.path.join(LOG_DIR, year_month)
                        os.makedirs(month_dir, exist_ok=True)
                        
                        # 移動文件
                        target_path = os.path.join(month_dir, file)
                        
                        # 如果目標不存在，移動文件
                        if not os.path.exists(target_path):
                            import shutil
                            shutil.move(file_path, target_path)
                            print(f"已移動日誌到月份文件夾: {file} -> {month_dir}")
                except Exception as e:
                    print(f"移動日誌文件時出錯 {file}: {str(e)}")
    except Exception as e:
        print(f"組織日誌文件夾時出錯: {str(e)}")
        print(traceback.format_exc())

# 定期清理舊日誌文件（默認保留3個月）- 暫時禁用
def cleanup_old_logs(retention_months=3):
    """清理超過保留期的舊日誌文件夾 - 此功能已暫時禁用，日誌將永久保存"""
    # 功能已禁用 - 保留函數定義以備將來啟用
    pass
    
    # 原始代碼保留但註釋掉，以便將來需要時可以恢復
    """
    try:
        if not os.path.exists(LOG_DIR):
            return
            
        # 獲取當前年月
        current_month = datetime.now().strftime("%Y-%m")
        try:
            year, month = map(int, current_month.split('-'))
        except ValueError:
            print(f"無法解析當前月份: {current_month}")
            return
        
        # 掃描主要日誌類型目錄
        for log_type in ["debug", "runtime", "error"]:
            type_dir = os.path.join(LOG_DIR, log_type)
            if os.path.exists(type_dir) and os.path.isdir(type_dir):
                # 掃描類型目錄下的月份子目錄
                for folder_name in os.listdir(type_dir):
                    folder_path = os.path.join(type_dir, folder_name)
                    
                    # 檢查是否是月份文件夾格式 (YYYY-MM)
                    if os.path.isdir(folder_path) and len(folder_name) == 7 and folder_name[4] == '-':
                        try:
                            folder_year, folder_month = map(int, folder_name.split('-'))
                            
                            # 計算月份差距
                            months_diff = (year - folder_year) * 12 + (month - folder_month)
                            
                            # 刪除超過保留期限的文件夾
                            if months_diff > retention_months:
                                import shutil
                                shutil.rmtree(folder_path)
                                print(f"已刪除舊日誌文件夾: {log_type}/{folder_name}（超過{retention_months}個月）")
                        except Exception as e:
                            print(f"處理月份文件夾時出錯 {folder_path}: {str(e)}")
        
        # 同時處理根目錄下的舊月份文件夾(合併日誌的情況)
        for folder_name in os.listdir(LOG_DIR):
            folder_path = os.path.join(LOG_DIR, folder_name)
            
            # 檢查是否是直接在根目錄下的月份文件夾格式 (YYYY-MM)
            if (os.path.isdir(folder_path) and 
                folder_name not in ["debug", "runtime", "error"] and 
                len(folder_name) == 7 and folder_name[4] == '-'):
                try:
                    folder_year, folder_month = map(int, folder_name.split('-'))
                    
                    # 計算月份差距
                    months_diff = (year - folder_year) * 12 + (month - folder_month)
                    
                    # 刪除超過保留期限的文件夾
                    if months_diff > retention_months:
                        import shutil
                        shutil.rmtree(folder_path)
                        print(f"已刪除舊日誌文件夾: {folder_name}（超過{retention_months}個月）")
                except Exception as e:
                    print(f"處理月份文件夾時出錯 {folder_path}: {str(e)}")
    except Exception as e:
        print(f"清理舊日誌時出錯: {str(e)}")
        print(traceback.format_exc())
    """

# 根據配置啟用或禁用日誌
if not enabled: 
    # 禁用日誌，清理臨時文件
    cleanup_empty_log_files()
    
    # 關閉和刪除處理程序
    for handler in list(logger.handlers):
        try:
            logger.removeHandler(handler)
            if hasattr(handler, 'close'):
                handler.close()
        except Exception as e:
            print(f"關閉日誌處理程序時出錯: {str(e)}")
else:
    # 啟用日誌
    cleanup_empty_log_files()
    
    # 組織可能存在的舊日誌到月份子文件夾
    organize_logs_by_month()
    
    # 暫時禁用自動清理舊日誌
    # try:
    #     # 清理超過保留期的舊日誌
    #     retention_months = conf.get("log_retention_months", 3)
    #     cleanup_old_logs(retention_months)
    # except Exception as e:
    #     print(f"維護日誌時出錯: {str(e)}")


