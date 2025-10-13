"""
測試文字輸入模式的系統循環

這個腳本會:
1. 設置配置為文字輸入模式
2. 啟動完整系統
3. 等待用戶輸入並處理完整循環

使用方法:
    python test_text_mode.py
    
退出:
    輸入 'exit', 'quit', 'q' 或按 Ctrl+C
"""

import sys
from pathlib import Path

# 確保可以導入模組
sys.path.insert(0, str(Path(__file__).parent))

from utils.debug_helper import info_log, error_log, debug_log
from utils.logger import force_enable_file_logging


def test_text_input_mode():
    """測試文字輸入模式"""
    
    print("\n" + "="*60)
    print("    U.E.P 文字輸入模式測試")
    print("="*60 + "\n")
    
    original_mode = None  # 記錄原始模式以便恢復
    
    # 步驟 1: 修改配置為文字輸入模式
    info_log("📝 步驟 1: 設置文字輸入模式...")
    
    try:
        from configs.config_loader import load_config, save_config
        
        config = load_config()
        
        # 記錄原始模式
        original_mode = config.get("system", {}).get("input_mode", {}).get("mode", "vad")
        debug_log(2, f"   原始輸入模式: {original_mode}")
        
        # 設置為文字模式
        config.setdefault("system", {})
        config["system"].setdefault("input_mode", {})
        config["system"]["input_mode"]["mode"] = "text"
        
        if save_config(config):
            info_log("✅ 配置已更新為文字輸入模式")
        else:
            error_log("❌ 更新配置失敗")
            return
            
    except Exception as e:
        error_log(f"❌ 設置配置失敗: {e}")
        import traceback
        traceback.print_exc()
        return
    
    # 步驟 2: 初始化並運行系統
    info_log("\n步驟 2: 初始化並運行系統...")
    
    # 顯示使用說明
    print("\n" + "-"*60)
    info_log("準備啟動文字輸入模式...")
    print("-"*60)
    print("\n使用說明:")
    print("  - 直接輸入文字與 U.E.P 對話")
    print("  - 輸入 'exit', 'quit' 或 'q' 退出")
    print("  - 按 Ctrl+C 強制退出")
    print("\n" + "-"*60 + "\n")
    
    try:
        # 導入並運行 ProductionRunner
        from core.production_runner import ProductionRunner
        
        runner = ProductionRunner()
        info_log("✅ Production Runner 已創建")
        
        # run() 會阻塞直到系統停止
        # 它會自動處理初始化、運行和清理
        success = runner.run(production_mode=True)
        
        if success:
            info_log("✅ 系統正常退出")
        else:
            error_log("❌ 系統異常退出")
        
    except KeyboardInterrupt:
        info_log("\n收到中斷信號...")
    except Exception as e:
        error_log(f"❌ 系統運行失敗: {e}")
        import traceback
        traceback.print_exc()
    finally:
        # 步驟 3: 清理
        info_log("\n清理配置...")
        
        # 恢復原始配置
        if original_mode:
            try:
                from configs.config_loader import load_config, save_config
                config = load_config()
                config["system"]["input_mode"]["mode"] = original_mode
                save_config(config)
                info_log(f"✅ 配置已恢復為 {original_mode} 模式")
            except Exception as e:
                error_log(f"⚠️ 恢復配置失敗: {e}")
    
    print("\n" + "="*60)
    info_log("✅ 測試完成!")
    print("="*60 + "\n")


if __name__ == "__main__":
    force_enable_file_logging()
    test_text_input_mode()
