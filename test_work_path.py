"""
簡單的 WORK 路徑測試腳本
驗證 EventBus 修復後 ModuleCoordinator 能正確收到事件並路由
"""
import sys
sys.path.insert(0, '.')

from devtools.module_tests.integration_tests import SystemLoopIntegrationTest
from utils.debug_helper import info_log, error_log

if __name__ == "__main__":
    print("🚀 開始測試 WORK 路徑...")
    
    # 創建測試實例
    tester = SystemLoopIntegrationTest()
    
    # 設置系統（不提供預初始化模組，讓系統自己初始化）
    if not tester.setup_system():
        error_log("❌ 系統設置失敗")
        sys.exit(1)
    
    info_log("✅ 系統設置完成，開始注入測試輸入...")
    
    # 注入測試輸入
    test_input = "Please help me read the file content"
    if tester.inject_text_input(test_input):
        info_log(f"✅ 已注入測試輸入: {test_input}")
        
        # 等待處理完成
        if tester.wait_for_processing_complete(timeout=30.0):
            info_log("✅ 處理完成")
        else:
            error_log("❌ 處理超時")
    else:
        error_log("❌ 注入測試輸入失敗")
    
    # 清理
    tester.cleanup()
    
    print("\n✅ 測試完成，請檢查日誌確認 WORK 路徑是否正確執行")
