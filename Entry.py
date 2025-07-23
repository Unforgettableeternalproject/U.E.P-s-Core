import sys
from datetime import datetime
from configs.config_loader import load_config
from devtools.debugger import debug_interactive
from utils.debug_helper import debug_log

config = load_config()

debug_mode = config.get("debug", {}).get("enabled", False)

def clear_empty_logs():
    # 清除空的日誌檔案，作為一個後手
    import os
    import shutil
    log_dir = config.get("logging", {}).get("log_dir", "logs")
    
    # 檢查日誌目錄是否存在
    if not os.path.exists(log_dir):
        print(f"日誌目錄不存在: {log_dir}")
        os.makedirs(log_dir, exist_ok=True)  # 建立日誌目錄
        return
        
    # 清理空文件
    for root, dirs, files in os.walk(log_dir):
        # 處理文件
        for file in files:
            file_path = os.path.join(root, file)
            try:
                if os.path.exists(file_path) and os.path.getsize(file_path) == 0:
                    os.remove(file_path)
                    print(f"已移除空日誌文件: {file_path}")
            except Exception as e:
                print(f"無法移除文件 {file_path}: {str(e)}")
    
    # 第二次掃描，處理空文件夾
    for root, dirs, files in os.walk(log_dir, topdown=False):  # topdown=False 確保從底層開始處理
        # 如果該目錄是空的（沒有文件也沒有子目錄）
        if not files and not dirs and root != log_dir:  # 不要刪除主日誌目錄
            try:
                os.rmdir(root)
                print(f"已移除空文件夾: {root}")
            except Exception as e:
                print(f"無法移除文件夾 {root}: {str(e)}")

if __name__ == "__main__":
    print("\n=========================\n")
    print(f"U.E.P <v.0.1.0> - 開發中版本 - {datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}\n")

    if debug_mode:
        debug_log(1, "除錯模式啟用")
        debug_interactive()  # 啟動互動式命令行介面
    else:
        print("\n除錯模式未啟用，請檢查配置文件")
        print("如果您想要進入除錯模式，請在配置文件中將 debug 設置為 True")
        print("退出程式...")

    clear_empty_logs()

    sys.exit(0)