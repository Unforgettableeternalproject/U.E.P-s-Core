import sys, os, time
from configs.config_loader import load_config
from devtools.debugger import debug_interactive
from utils.debug_helper import debug_log

config = load_config()

debug_mode = config.get("debug", {}).get("debug", False)

if __name__ == "__main__":
    print("\n=========================\n")
    print(f"U.E.P 0.0.2 - 開發中版本 - {time.localtime}")
    print("\n=========================\n")

    if debug_mode:
        debug_log(1, "除錯模式啟用")
        debug_interactive()  # 啟動互動式命令行介面
    else:
        print("除錯模式未啟用，請檢查配置文件")
        print("如果您想要進入除錯模式，請在配置文件中將 debug 設置為 True")
        print("退出程式...")

    sys.exit(0)