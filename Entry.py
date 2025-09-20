import sys
from datetime import datetime
from configs.config_loader import load_config
from utils.debug_helper import debug_log, info_log, error_log

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
                
def log_test():
    # 測試日誌功能
    debug_log(1, "這是一條關鍵等級的除錯日誌")
    debug_log(2, "這是一條操作等級的除錯日誌")
    debug_log(3, "這是一條系統等級的除錯日誌")
    debug_log(4, "這是一條詳盡等級的除錯日誌")
    info_log("這是一條資訊日誌")
    info_log("這是一條警告日誌", type="WARNING")
    error_log("這是一條錯誤日誌")
    error_log("這是一條嚴重錯誤日誌", type="CRITICAL")

if __name__ == "__main__":
    print("\n=========================\n")
    print(f"U.E.P <v.0.3.1> - 開發中版本 - {datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}\n")

    # 處理命令行參數
    import argparse
    parser = argparse.ArgumentParser(description='U.E.P 系統')
    parser.add_argument('--reset-speaker-models', action='store_true', help='重置說話人模型')
    parser.add_argument('--log-test', action='store_true', help='測試日誌功能')
    parser.add_argument('--debug', action='store_true', help='強制啟用除錯模式')
    parser.add_argument('--debug-gui', action='store_true', help='啟動圖形除錯介面')
    parser.add_argument('--production', action='store_true', help='強制啟用生產模式')
    args = parser.parse_args()
    
    if args.log_test:
        log_test()
        sys.exit(0)
    
    # 命令行參數可以覆蓋配置文件設定
    if args.debug:
        debug_mode = True
        print("🔧 通過命令行參數強制啟用除錯模式")
    elif args.production:
        debug_mode = False
        print("🚀 通過命令行參數強制啟用生產模式")
    
    # 處理特殊命令
    if args.reset_speaker_models:
        from modules.stt_module.speaker_identification import SpeakerIdentifier
        speaker_id = SpeakerIdentifier(config.get("modules", {}).get("stt_module", {}))
        if speaker_id.reset_speaker_models():
            print("已重置說話人模型")
        else:
            print("重置說話人模型失敗")
        sys.exit(0)
    
    # 處理圖形除錯介面啟動
    if args.debug_gui:
        print("🖥️ 啟動圖形除錯介面...")
        try:
            # 設定為按需載入模式（GUI模式）
            import devtools.debug_api as debug_api
            debug_api.set_loading_mode(preload=False)
            print("✅ 已設定為按需載入模式")
            
            # 不預先載入任何模組，直接啟動除錯介面
            # 讓使用者在除錯介面中手動決定載入哪些模組
            from modules.ui_module.debug import launch_debug_interface
            launch_debug_interface(prefer_gui=True, blocking=True)
        except Exception as e:
            print(f"❌ 圖形除錯介面啟動失敗: {e}")
            sys.exit(1)
        sys.exit(0)

    if debug_mode:
        debug_log(1, "🔧 除錯模式啟用，正在準備各項模組...")
        # 設定為預先載入模式（舊版終端模式）
        import devtools.debug_api as debug_api
        debug_api.set_loading_mode(preload=True)
        print("✅ 已設定為預先載入模式")
        
        from devtools.debugger import debug_interactive
        debug_interactive()  # 啟動互動式命令行介面
    else:
        print("🚀 正式模式啟用，啟動 UEP 生產環境...")
        print("💡 這將使用 UnifiedController 運行已重構的模組")
        print("🔄 如果您想要進入除錯模式，請在配置文件中將 debug.enabled 設置為 true")
        print()
        
        # 啟動生產環境
        try:
            from core.production_runner import run_production_mode
            run_production_mode()
        except KeyboardInterrupt:
            print("⌨️ 用戶中斷程序")
        except Exception as e:
            print(f"❌ 啟動生產環境時發生錯誤: {e}")
            sys.exit(1)

    clear_empty_logs()

    sys.exit(0)