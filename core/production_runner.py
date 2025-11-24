# core/production_runner.py
"""
生產環境運行器 - 協調系統初始化和主循環

實現完整的系統啟動流程：
1. 調用 SystemInitializer 進行系統初始化
2. 啟動 SystemLoop 主循環
3. 處理優雅關閉和錯誤恢復

系統運作流程：
Controller 啟動 Framework 初始化 → Router 等待輸入 → 
STT → NLP → Router 路由 → (CS/WS) → 處理模組 → TTS → 效能監控
"""

import signal
import sys
import time
from typing import Optional

from utils.debug_helper import debug_log, info_log, error_log


class ProductionRunner:
    """生產環境運行器"""
    
    def __init__(self):
        """初始化運行器"""
        self.is_running = False
        self.system_initializer = None
        self.system_loop = None
        
        # 設置信號處理
        self._setup_signal_handlers()
        
        info_log("[ProductionRunner] 生產運行器已創建")
    
    def run(self, production_mode: bool = True) -> bool:
        """
        運行生產模式
        
        Args:
            production_mode: 是否為生產模式
            
        Returns:
            bool: 運行是否成功
        """
        try:
            info_log("🚀 啟動 UEP 生產環境...")
            self.is_running = True
            
            # Phase 1: 系統初始化
            if not self._initialize_system(production_mode):
                error_log("❌ 系統初始化失敗")
                return False
            
            # Phase 2: 啟動主循環
            if not self._start_main_loop():
                error_log("❌ 系統主循環啟動失敗")
                return False
            
            # Phase 3: 保持運行並監控
            return self._keep_running()
            
        except KeyboardInterrupt:
            info_log("⚠️ 接收到用戶中斷信號")
            return self._graceful_shutdown()
        except Exception as e:
            error_log(f"❌ 生產環境運行失敗: {e}")
            return False
        finally:
            self.is_running = False
    
    def _initialize_system(self, production_mode: bool) -> bool:
        """初始化系統"""
        try:
            info_log("🔧 開始系統初始化...")
            
            # 導入並創建系統初始化器
            from core.system_initializer import SystemInitializer
            self.system_initializer = SystemInitializer()
            
            # 執行系統初始化
            success = self.system_initializer.initialize_system(production_mode)
            if not success:
                error_log("❌ 系統初始化失敗")
                return False
            
            info_log("✅ 系統初始化完成")
            
            # 顯示初始化狀態
            status = self.system_initializer.get_initialization_status()
            info_log(f"📊 初始化狀態: {status['phase']}")
            info_log(f"📦 已載入模組: {status['initialized_modules']}")
            
            return True
            
        except Exception as e:
            error_log(f"❌ 系統初始化過程失敗: {e}")
            return False
    
    def _start_main_loop(self) -> bool:
        """啟動主循環"""
        try:
            info_log("🔄 啟動系統主循環...")
            
            # 使用全局單例系統循環 (避免重複訂閱事件)
            from core.system_loop import system_loop
            self.system_loop = system_loop
            
            # 啟動循環
            success = self.system_loop.start()
            if not success:
                error_log("❌ 主循環啟動失敗")
                return False
            
            info_log("✅ 系統主循環已啟動")
            return True
            
        except Exception as e:
            error_log(f"❌ 主循環啟動過程失敗: {e}")
            return False
    
    def _keep_running(self) -> bool:
        """保持運行並監控系統"""
        try:
            info_log("🎯 UEP 系統正在運行...")
            info_log("📋 系統流程: STT → NLP → Router → (CS/WS) → 處理模組 → TTS")
            info_log("⚡ 按 Ctrl+C 優雅退出系統")
            
            # 監控循環
            last_status_time = 0
            status_interval = 30.0  # 30秒報告一次狀態
            
            while self.is_running:
                # 檢查系統循環狀態
                if self.system_loop:
                    loop_status = self.system_loop.get_status()
                    
                    # 如果循環停止了，嘗試重啟或退出
                    if not loop_status["is_running"] and self.is_running:
                        error_log("⚠️ 檢測到主循環停止，嘗試重啟...")
                        if not self.system_loop.start():
                            error_log("❌ 主循環重啟失敗，系統將退出")
                            return False
                    
                    # 定期報告狀態
                    current_time = time.time()
                    if current_time - last_status_time >= status_interval:
                        info_log(f"📊 系統狀態: {loop_status['status']}, "
                                f"運行時間: {loop_status['uptime']:.1f}秒, "
                                f"循環次數: {loop_status['loop_count']}")
                        last_status_time = current_time
                
                # 短暫休眠
                time.sleep(1.0)
            
            return True
            
        except Exception as e:
            error_log(f"❌ 系統運行監控失敗: {e}")
            return False
    
    def _graceful_shutdown(self) -> bool:
        """優雅關閉系統"""
        try:
            info_log("🛑 開始優雅關閉系統...")
            self.is_running = False
            
            # 停止主循環
            if self.system_loop:
                info_log("   停止系統主循環...")
                self.system_loop.stop()
            
            # 執行清理工作
            self._cleanup_resources()
            
            info_log("✅ 系統已優雅關閉")
            return True
            
        except Exception as e:
            error_log(f"❌ 優雅關閉失敗: {e}")
            return False
    
    def _cleanup_resources(self):
        """清理系統資源"""
        try:
            info_log("🧹 清理系統資源...")
            
            # 清理 Working Context
            try:
                from core.working_context import working_context_manager
                if hasattr(working_context_manager, 'cleanup_expired_contexts'):
                    cleaned = working_context_manager.cleanup_expired_contexts()
                    if cleaned > 0:
                        info_log(f"   清理了 {cleaned} 個過期上下文")
            except Exception as e:
                debug_log(1, f"   Working Context 清理警告: {e}")
            
            # 清理會話
            try:
                from core.sessions.session_manager import unified_session_manager
                if hasattr(unified_session_manager, 'cleanup_expired_sessions'):
                    unified_session_manager.cleanup_expired_sessions()
                    info_log("   已清理過期會話")
            except Exception as e:
                debug_log(1, f"   會話清理警告: {e}")
            
            # 收集最終效能快照
            try:
                from core.framework import core_framework
                if core_framework.is_initialized:
                    snapshot = core_framework.collect_system_performance_snapshot()
                    if snapshot:
                        info_log(f"   最終效能快照: {snapshot.total_system_requests} 總請求, "
                               f"成功率: {snapshot.system_success_rate:.2%}")
            except Exception as e:
                debug_log(1, f"   效能快照收集警告: {e}")
            
            info_log("✅ 資源清理完成")
            
        except Exception as e:
            debug_log(1, f"⚠️ 資源清理過程中的警告: {e}")
    
    def _setup_signal_handlers(self):
        """設置信號處理器"""
        def signal_handler(signum, frame):
            info_log(f"⚠️ 接收到信號 {signum}，準備優雅關閉...")
            self.is_running = False
        
        # 註冊信號處理器
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)
    
    def get_status(self) -> dict:
        """獲取運行狀態"""
        status = {
            "is_running": self.is_running,
            "initializer_status": None,
            "loop_status": None
        }
        
        if self.system_initializer:
            status["initializer_status"] = self.system_initializer.get_initialization_status()
        
        if self.system_loop:
            status["loop_status"] = self.system_loop.get_status()
        
        return status


def run_production_mode():
    """運行生產模式 - 主要入口點"""
    runner = ProductionRunner()
    return runner.run(production_mode=True)


# 保持向後兼容性
if __name__ == "__main__":
    run_production_mode()