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
            
            # Phase 2: 啟動主循環（在 QThread 中）
            if not self._start_main_loop():
                error_log("❌ 系統主循環啟動失敗")
                return False
            
            # Phase 3: 檢查是否有前端 - 決定使用哪種主循環
            has_frontend = self._check_frontend_enabled()
            
            if has_frontend:
                # 使用 Qt 主循環（阻塞在這裡直到 app.quit()）
                return self._run_with_qt_event_loop()
            else:
                # 使用傳統監控循環
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
            
            # 🌙 檢查是否上次在 SLEEP 狀態
            self._check_previous_sleep_state()
            
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
        """啟動主循環（在 QThread 中，如果前端啟用）"""
        try:
            info_log("🔄 啟動系統主循環...")
            
            # 使用全局單例系統循環 (避免重複訂閱事件)
            from core.system_loop import system_loop
            self.system_loop = system_loop
            
            # 檢查是否有前端
            has_frontend = self._check_frontend_enabled()
            debug_log(4, f"[ProductionRunner] _start_main_loop: has_frontend={has_frontend}")
            
            if has_frontend:
                # 使用 Qt 包裝啟動（在 QThread 中）
                info_log("🎨 前端已啟用，使用 Qt 系統循環包裝...")
                from core.qt_system_loop import QtSystemLoopManager
                from core.registry import get_module
                
                ui_module = get_module("ui_module")
                if not ui_module or not hasattr(ui_module, 'app'):
                    error_log("❌ UI 模組不可用或未初始化")
                    return False
                
                # 創建 Qt 系統循環管理器
                self.qt_loop_manager = QtSystemLoopManager(parent=ui_module.app)
                
                # 啟動系統循環（在 QThread 中）
                success = self.qt_loop_manager.start_system_loop(system_loop)
                if not success:
                    error_log("❌ Qt 系統循環啟動失敗")
                    return False
                
                info_log("✅ Qt 系統循環已在背景線程啟動")
                return True
            else:
                # 傳統方式啟動（在 daemon 線程中）
                info_log("🔄 前端未啟用，使用傳統系統循環...")
                success = self.system_loop.start()
                if not success:
                    error_log("❌ 主循環啟動失敗")
                    return False
                
                info_log("✅ 系統主循環已啟動")
                return True
            
        except Exception as e:
            error_log(f"❌ 主循環啟動過程失敗: {e}")
            return False
    
    def _check_frontend_enabled(self) -> bool:
        """檢查前端是否啟用"""
        try:
            from configs.config_loader import load_config
            config = load_config()
            enable_frontend = config.get("debug", {}).get("enable_frontend", False)
            debug_log(4, f"[ProductionRunner] _check_frontend_enabled: type={type(enable_frontend)}, value={enable_frontend}, bool={bool(enable_frontend)}")
            # 確保是布爾值 True 才啟用
            return enable_frontend is True
        except Exception as e:
            debug_log(1, f"檢查前端狀態失敗: {e}")
            return False
    
    def _run_with_qt_event_loop(self) -> bool:
        """使用 Qt 事件循環作為主循環"""
        try:
            from core.registry import get_module
            from PyQt5.QtCore import QTimer
            
            ui_module = get_module("ui_module")
            if not ui_module or not hasattr(ui_module, 'app') or not ui_module.app:
                error_log("❌ UI 模組或 QApplication 不可用")
                return False
            
            info_log("🎯 UEP 系統正在運行（Qt 主循環模式）...")
            info_log("📋 系統流程: STT → NLP → Router → (CS/WS) → 處理模組 → TTS")
            info_log("⚡ 關閉視窗或按 Ctrl+C 退出系統")
            
            # 設置一個定時器來檢查 Ctrl+C 信號
            self._interrupt_requested = False
            
            def check_interrupt():
                """定期檢查是否應該退出"""
                if not self.is_running or self._interrupt_requested:
                    info_log("⚠️ 檢測到中斷信號，準備退出...")
                    # 停止 STT 持續監聽
                    try:
                        stt_module = get_module("stt_module")
                        if stt_module:
                            stt_module.stop_listening()
                            debug_log(1, "[ProductionRunner] 已通知 STT 停止監聽")
                    except Exception as e:
                        debug_log(1, f"[ProductionRunner] 停止 STT 監聽失敗: {e}")
                    ui_module.app.quit()
            
            interrupt_timer = QTimer()
            interrupt_timer.timeout.connect(check_interrupt)
            interrupt_timer.start(500)  # 每 500ms 檢查一次
            
            # 進入 Qt 事件循環（阻塞直到 app.quit()）
            exit_code = ui_module.app.exec_()
            
            # 停止定時器
            interrupt_timer.stop()
            
            info_log(f"✅ Qt 事件循環已退出 (退出碼: {exit_code})")
            
            # 執行清理
            shutdown_success = self._graceful_shutdown()
            
            # 強制退出 Python 程序，確保終端返回
            info_log("🚪 強制退出 Python 程序...")
            sys.exit(exit_code)
            
            return shutdown_success
            
        except Exception as e:
            error_log(f"❌ Qt 事件循環運行失敗: {e}")
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
            
            # 第一階段: 停止所有執行中任務
            info_log("   📋 第一階段: 停止執行中任務...")
            
            # 1. 停止監控線程池
            try:
                from modules.sys_module.actions.automation_helper import get_monitoring_pool
                monitoring_pool = get_monitoring_pool()
                if monitoring_pool:
                    info_log("   停止監控線程池...")
                    monitoring_pool.shutdown(wait=True, timeout=10)
            except Exception as e:
                debug_log(1, f"   監控線程池關閉警告: {e}")
            
            # 2. 停止 Working Context 清理執行緒
            try:
                from core.working_context import working_context_manager
                if working_context_manager:
                    info_log("   停止 Working Context 清理執行緒...")
                    working_context_manager.stop_cleanup_worker()
            except Exception as e:
                debug_log(1, f"   Working Context 清理執行緒關閉警告: {e}")
            
            # 第二階段: 停止核心服務
            info_log("   📋 第二階段: 停止核心服務...")
            
            # 3. 停止主循環（包含 EventBus）
            if self.system_loop:
                info_log("   停止系統主循環...")
                self.system_loop.stop()
            
            # 4. 停止 Controller 監控線程
            try:
                from core.controller import unified_controller
                info_log("   停止 Controller 監控...")
                unified_controller.shutdown()
            except Exception as e:
                debug_log(1, f"   Controller 關閉警告: {e}")
            
            # 第三階段: 資源清理
            info_log("   📋 第三階段: 清理系統資源...")
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
            
            # 清理 asyncio 事件循環（用於 TTS 的執行器）
            try:
                import asyncio
                try:
                    loop = asyncio.get_event_loop()
                    if loop and not loop.is_closed():
                        # 取消所有待機的任務
                        pending = asyncio.all_tasks(loop)
                        for task in pending:
                            task.cancel()
                        # 簡短等待以允許任務完成
                        loop.run_until_complete(asyncio.sleep(0.1))
                        debug_log(2, f"   已取消 {len(pending)} 個未完成的 asyncio 任務")
                except RuntimeError:
                    # 沒有事件循環，這是正常的
                    pass
            except Exception as e:
                debug_log(1, f"   asyncio 清理警告: {e}")
            
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
    
    def _check_previous_sleep_state(self):
        """檢查系統上次是否在 SLEEP 狀態"""
        try:
            from core.states.wake_api import check_sleep_on_startup
            
            was_sleeping = check_sleep_on_startup()
            
            if was_sleeping:
                info_log("[ProductionRunner] 系統從 SLEEP 狀態恢復，將以正常模式啟動")
            
        except Exception as e:
            debug_log(2, f"[ProductionRunner] 檢查 SLEEP 狀態失敗: {e}")
    
    def _setup_signal_handlers(self):
        """設置信號處理器"""
        def signal_handler(signum, frame):
            info_log(f"⚠️ 接收到信號 {signum}，準備優雅關閉...")
            self.is_running = False
            # 設置中斷標誌，讓 Qt 定時器檢測到
            if hasattr(self, '_interrupt_requested'):
                self._interrupt_requested = True
        
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
    
    # 🆕 將 runner 保存到 __main__ 以供其他模組存取（如 access_widget）
    try:
        import __main__
        __main__.production_runner = runner
    except:
        pass
    
    return runner.run(production_mode=True)


# 保持向後兼容性
if __name__ == "__main__":
    run_production_mode()