import sys
from datetime import datetime
from configs.config_loader import load_config
from devtools.debugger import debug_interactive
from utils.debug_helper import debug_log

config = load_config()

debug_mode = config.get("debug", {}).get("enabled", False)

def clear_empty_logs():
    # 清除空的日誌檔案
    import os
    log_dir = config.get("logging", {}).get("log_dir", "logs")
    for root, dirs, files in os.walk(log_dir):
        for file in files:
            file_path = os.path.join(root, file)
            if os.path.getsize(file_path) == 0:
                os.remove(file_path)

if __name__ == "__main__":
    print("\n=========================\n")
    print(f"U.E.P <v.0.0.2> - 開發中版本 - {datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}\n")

    if debug_mode:
        debug_log(1, "除錯模式啟用")
        debug_interactive()  # 啟動互動式命令行介面
    else:
        print("\n除錯模式未啟用，請檢查配置文件")
        print("如果您想要進入除錯模式，請在配置文件中將 debug 設置為 True")
        print("退出程式...")

    clear_empty_logs()

    sys.exit(0)