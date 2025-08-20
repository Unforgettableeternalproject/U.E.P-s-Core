# core/production_runner.py
"""
生產環境運行器 - 協調系統初始化器和主循環

這是一個簡單的中繼器，負責：
1. 調用系統初始化器進行系統啟動
2. 啟動系統主循環
3. 處理優雅關閉
"""

from utils.debug_helper import debug_log, info_log, error_log
from core.system_initializer import system_initializer
from core.system_loop import system_loop


def run_production_mode():
    """運行生產模式"""
    try:
        info_log("🚀 啟動 UEP 生產環境...")
        
        # 1. 初始化系統
        if not system_initializer.initialize_system(production_mode=True):
            error_log("❌ 系統初始化失敗")
            return False
            
        # 2. 啟動主循環
        info_log("🔄 啟動系統主循環...")
        if not system_loop.start():
            error_log("❌ 系統主循環啟動失敗")
            return False
        
        # 3. 保持主線程運行，等待用戶中斷
        info_log("🎯 UEP 系統正在運行，按 Ctrl+C 退出...")
        try:
            # 等待系統循環線程結束
            while system_loop.loop_thread and system_loop.loop_thread.is_alive():
                system_loop.loop_thread.join(timeout=1.0)
        except KeyboardInterrupt:
            info_log("⏹️ 用戶中斷，正在關閉系統...")
            shutdown_production_mode()
            
        return True
        
    except KeyboardInterrupt:
        info_log("⏹️ 用戶中斷，正在關閉系統...")
        shutdown_production_mode()
        return True
        
    except Exception as e:
        error_log(f"❌ 生產環境運行失敗: {e}")
        shutdown_production_mode()
        return False


def shutdown_production_mode():
    """關閉生產模式"""
    try:
        info_log("🛑 關閉生產環境...")
        
        # 停止系統循環
        if hasattr(system_loop, 'stop'):
            system_loop.stop()
            
        # 關閉系統初始化器
        if hasattr(system_initializer, 'shutdown'):
            system_initializer.shutdown()
            
        info_log("✅ 生產環境已安全關閉")
        
    except Exception as e:
        error_log(f"❌ 關閉過程中發生錯誤: {e}")


if __name__ == "__main__":
    run_production_mode()
