# core/controller.py
"""
統一控制器 - 系統級監督者和觸發器

這個控制器負責：
1. 系統啟動和初始化
2. 系統監控和狀態追蹤  
3. GS (General Session) 生命週期管理
4. 突發狀況應對和系統恢復
5. 系統級事件處理

Controller 是系統級的監督者，不直接參與模組層級的處理流程。
"""

import time
import threading
from typing import Dict, Any, Optional
from enum import Enum

from core.framework import core_framework
from core.sessions.session_manager import session_manager
from core.states.state_manager import state_manager, UEPState
from configs.config_loader import load_config
from utils.debug_helper import debug_log, info_log, error_log


class SystemStatus(Enum):
    """系統狀態"""
    STOPPED = "stopped"
    INITIALIZING = "initializing" 
    RUNNING = "running"
    MONITORING = "monitoring"
    ERROR = "error"
    RECOVERING = "recovering"


class UnifiedController:
    """
    統一控制器 - 系統級監督者
    
    職責：
    1. 系統啟動和初始化
    2. 系統監控和狀態追蹤
    3. GS 生命週期管理  
    4. 突發狀況應對和系統恢復
    """
    
    def __init__(self):
        self.config = load_config()
        self.system_status = SystemStatus.STOPPED
        self.is_initialized = False
        self.monitoring_thread = None
        self.should_stop_monitoring = threading.Event()
        
        # 系統組件引用
        self.session_manager = session_manager
        self.state_manager = state_manager
        self.core_framework = core_framework
        
        # 系統統計
        self.startup_time = None
        self.total_gs_sessions = 0
        self.system_errors = []
        
        info_log("[UnifiedController] 系統級控制器初始化")
    
    # ========== 系統啟動和初始化 ==========
    
    def initialize(self) -> bool:
        """系統初始化"""
        try:
            if self.is_initialized:
                info_log("[UnifiedController] 系統已初始化")
                return True
                
            self.system_status = SystemStatus.INITIALIZING
            info_log("[UnifiedController] 開始系統初始化...")
            
            # 初始化核心框架
            if not self._initialize_framework():
                return False
                
            # 設置事件處理器
            self._setup_event_handlers()
            
            # 啟動監控
            self._start_monitoring()
            
            self.is_initialized = True
            self.system_status = SystemStatus.RUNNING
            self.startup_time = time.time()
            
            info_log("[UnifiedController] 系統初始化完成")
            return True
            
        except Exception as e:
            self.system_status = SystemStatus.ERROR
            error_log(f"[UnifiedController] 系統初始化失敗: {e}")
            return False
    
    def _initialize_framework(self) -> bool:
        """初始化核心框架"""
        try:
            # 讓框架自行初始化所有模組
            success = self.core_framework.initialize()
            if success:
                info_log("[UnifiedController] 核心框架初始化成功")
                return True
            else:
                error_log("[UnifiedController] 核心框架初始化失敗")
                return False
        except Exception as e:
            error_log(f"[UnifiedController] 框架初始化異常: {e}")
            return False
    
    # ========== 系統監控 ==========
    
    def _start_monitoring(self):
        """啟動系統監控"""
        if self.monitoring_thread and self.monitoring_thread.is_alive():
            return
            
        self.should_stop_monitoring.clear()
        self.monitoring_thread = threading.Thread(target=self._monitoring_loop, daemon=True)
        self.monitoring_thread.start()
        info_log("[UnifiedController] 系統監控已啟動")
    
    def _monitoring_loop(self):
        """監控循環"""
        while not self.should_stop_monitoring.is_set():
            try:
                self._check_system_health()
                time.sleep(1.0)  # 每秒檢查一次
            except Exception as e:
                error_log(f"[UnifiedController] 監控循環錯誤: {e}")
                time.sleep(5.0)  # 錯誤時等待更久
    
    def _check_system_health(self):
        """檢查系統健康狀態"""
        try:
            # 檢查核心組件狀態
            current_state = self.state_manager.get_current_state()
            
            # 檢查會話狀態
            current_gs = self.session_manager.get_current_general_session()
            
            # 記錄系統狀態（簡化版）
            debug_log(3, f"[Monitor] 系統狀態: {current_state.value}, "
                        f"當前GS: {current_gs.session_id if current_gs else 'None'}")
            
        except Exception as e:
            debug_log(2, f"[Monitor] 健康檢查失敗: {e}")
    
    # ========== GS 生命週期管理 ==========
    
    def trigger_user_input(self, user_input: str, input_type: str = "text") -> Dict[str, Any]:
        """
        觸發用戶輸入處理 - 僅負責 GS 生命週期
        
        這是系統的入口點，只負責：
        1. 創建新的 GS 
        2. 觸發系統自主處理
        3. 監控 GS 完成
        4. 返回基本結果
        """
        try:
            info_log(f"[UnifiedController] 觸發用戶輸入處理...")
            
            # 創建新的 General Session
            gs_trigger_event = {
                "user_input": user_input,
                "input_type": input_type,
                "timestamp": time.time()
            }
            
            # 啟動 GS（由 session_manager 自動處理後續流程）
            current_gs_id = self.session_manager.start_general_session(
                input_type + "_input", gs_trigger_event
            )
            
            if current_gs_id:
                self.total_gs_sessions += 1
                info_log(f"[UnifiedController] GS 已創建: {current_gs_id}")
                
                return {
                    "status": "triggered",
                    "session_id": current_gs_id,
                    "message": "輸入處理已觸發，系統將自主處理"
                }
            else:
                return {
                    "status": "error", 
                    "message": "無法創建 General Session"
                }
                
        except Exception as e:
            error_log(f"[UnifiedController] 輸入觸發失敗: {e}")
            return {
                "status": "error",
                "message": str(e)
            }
    
    # ========== 事件處理器 ==========
    
    def _setup_event_handlers(self):
        """設置系統級事件處理器"""
        try:
            # 監聽 GS 生命週期事件
            # TODO: 根據具體的事件系統實現來設置
            info_log("[UnifiedController] 事件處理器設置完成")
        except Exception as e:
            error_log(f"[UnifiedController] 事件處理器設置失敗: {e}")
    
    # ========== 系統狀態報告 ==========
    
    def get_system_status(self) -> Dict[str, Any]:
        """獲取系統狀態報告"""
        try:
            current_state = self.state_manager.get_current_state()
            current_gs = self.session_manager.get_current_general_session()
            
            uptime = time.time() - self.startup_time if self.startup_time else 0
            
            # 基本系統信息
            status_report = {
                "system_status": self.system_status.value,
                "is_initialized": self.is_initialized,
                "uptime_seconds": uptime,
                "current_state": current_state.value,
                "current_gs": current_gs.session_id if current_gs else None,
                "total_gs_sessions": self.total_gs_sessions,
                "error_count": len(self.system_errors)
            }
            
            # 添加詳細的運行統計
            status_report.update(self._get_detailed_system_metrics())
            
            return status_report
            
        except Exception as e:
            return {
                "system_status": "error",
                "error": str(e)
            }
    
    def _get_detailed_system_metrics(self) -> Dict[str, Any]:
        """獲取詳細的系統指標"""
        try:
            from core.states.state_queue import get_state_queue_manager
            from core.working_context import working_context_manager
            
            metrics = {}
            
            # Framework 狀態
            if hasattr(self.core_framework, 'modules'):
                metrics["framework"] = {
                    "modules_count": len(self.core_framework.modules),
                    "modules_list": list(self.core_framework.modules.keys()),
                    "is_initialized": self.core_framework.is_initialized
                }
            
            # 效能監控狀態
            if hasattr(self.core_framework, 'performance_monitoring_enabled'):
                metrics["performance"] = {
                    "monitoring_enabled": self.core_framework.performance_monitoring_enabled,
                    "snapshot_available": hasattr(self.core_framework, 'collect_system_performance_snapshot')
                }
                
                # 嘗試獲取最新效能快照
                try:
                    snapshot = self.core_framework.collect_system_performance_snapshot()
                    if snapshot:
                        metrics["performance"]["latest_snapshot"] = {
                            "active_modules": snapshot.active_modules,
                            "success_rate": snapshot.system_success_rate,
                            "avg_response_time": snapshot.system_average_response_time,
                            "timestamp": snapshot.timestamp
                        }
                except Exception:
                    metrics["performance"]["latest_snapshot"] = "unavailable"
            
            # 狀態佇列資訊
            try:
                state_queue = get_state_queue_manager()
                if hasattr(state_queue, 'queue'):
                    metrics["state_queue"] = {
                        "queue_length": len(state_queue.queue),
                        "current_state": state_queue.current_state.value if hasattr(state_queue, 'current_state') else "unknown"
                    }
            except Exception:
                metrics["state_queue"] = {"status": "unavailable"}
            
            # Working Context 資訊
            try:
                if hasattr(working_context_manager, 'contexts'):
                    active_contexts = [ctx for ctx in working_context_manager.contexts.values() 
                                     if hasattr(ctx, 'status') and ctx.status.name == 'ACTIVE']
                    metrics["working_context"] = {
                        "total_contexts": len(working_context_manager.contexts),
                        "active_contexts": len(active_contexts),
                        "decision_handlers": len(working_context_manager.decision_handlers) if hasattr(working_context_manager, 'decision_handlers') else 0
                    }
            except Exception:
                metrics["working_context"] = {"status": "unavailable"}
            
            # Session 管理資訊
            try:
                current_gs = self.session_manager.get_current_general_session()
                metrics["sessions"] = {
                    "general_session_active": current_gs is not None,
                    "total_sessions_created": self.total_gs_sessions
                }
                
                # 獲取其他會話類型統計
                if hasattr(self.session_manager, 'get_session_statistics'):
                    session_stats = self.session_manager.get_session_statistics()
                    metrics["sessions"].update(session_stats)
                    
            except Exception:
                metrics["sessions"] = {"status": "unavailable"}
            
            return metrics
            
        except Exception as e:
            return {"metrics_error": str(e)}
    
    def get_formatted_system_status(self) -> str:
        """獲取格式化的系統狀態報告"""
        try:
            status = self.get_system_status()
            
            # 格式化運行時間
            uptime = status.get("uptime_seconds", 0)
            if uptime > 3600:
                uptime_str = f"{uptime/3600:.1f}小時"
            elif uptime > 60:
                uptime_str = f"{uptime/60:.1f}分鐘"
            else:
                uptime_str = f"{uptime:.1f}秒"
            
            report_lines = [
                "🖥️ UEP 系統狀態監控報告",
                "=" * 50,
                f"🔧 系統狀態: {status.get('system_status', 'unknown')}",
                f"⏰ 運行時間: {uptime_str}",
                f"🎯 當前狀態: {status.get('current_state', 'unknown')}",
                f"👤 當前會話: {status.get('current_gs', 'None')}",
                f"📊 總會話數: {status.get('total_gs_sessions', 0)}",
                f"❌ 錯誤計數: {status.get('error_count', 0)}"
            ]
            
            # 添加模組信息
            if 'framework' in status:
                fw_info = status['framework']
                report_lines.extend([
                    "",
                    "📦 Framework 狀態:",
                    f"   模組數量: {fw_info.get('modules_count', 0)}",
                    f"   活躍模組: {', '.join(fw_info.get('modules_list', []))}"
                ])
            
            # 添加效能信息
            if 'performance' in status and 'latest_snapshot' in status['performance']:
                perf_info = status['performance']['latest_snapshot']
                if isinstance(perf_info, dict):
                    report_lines.extend([
                        "",
                        "📊 效能指標:",
                        f"   活躍模組: {perf_info.get('active_modules', 0)}",
                        f"   成功率: {perf_info.get('success_rate', 0):.2%}",
                        f"   平均響應: {perf_info.get('avg_response_time', 0):.2f}秒"
                    ])
            
            # 添加狀態佇列信息
            if 'state_queue' in status:
                sq_info = status['state_queue']
                report_lines.extend([
                    "",
                    "📝 狀態佇列:",
                    f"   佇列長度: {sq_info.get('queue_length', 0)}",
                    f"   當前狀態: {sq_info.get('current_state', 'unknown')}"
                ])
            
            report_lines.append("=" * 50)
            return "\n".join(report_lines)
            
        except Exception as e:
            return f"❌ 狀態報告生成錯誤: {e}"
    
    # ========== 突發狀況應對 ==========
    
    def handle_system_error(self, error_info: Dict[str, Any]):
        """處理系統錯誤"""
        try:
            self.system_errors.append({
                "timestamp": time.time(),
                "error": error_info
            })
            
            error_log(f"[UnifiedController] 系統錯誤: {error_info}")
            
            # 簡單的錯誤恢復邏輯
            if len(self.system_errors) > 10:  # 錯誤過多時重置
                self._attempt_system_recovery()
                
        except Exception as e:
            error_log(f"[UnifiedController] 錯誤處理失敗: {e}")
    
    def _attempt_system_recovery(self):
        """嘗試系統恢復"""
        try:
            self.system_status = SystemStatus.RECOVERING
            info_log("[UnifiedController] 嘗試系統恢復...")
            
            # 基本恢復操作
            self.system_errors.clear()
            
            # 確保系統回到正常狀態
            self.system_status = SystemStatus.RUNNING
            info_log("[UnifiedController] 系統恢復完成")
            
        except Exception as e:
            error_log(f"[UnifiedController] 系統恢復失敗: {e}")
            self.system_status = SystemStatus.ERROR
    
    # ========== 系統關閉 ==========
    
    def shutdown(self):
        """系統關閉"""
        try:
            info_log("[UnifiedController] 開始系統關閉...")
            
            # 停止監控
            self.should_stop_monitoring.set()
            if self.monitoring_thread:
                self.monitoring_thread.join(timeout=5)
            
            # 結束當前 GS
            current_gs = self.session_manager.get_current_general_session()
            if current_gs:
                self.session_manager.end_general_session({"status": "system_shutdown"})
            
            self.system_status = SystemStatus.STOPPED
            self.is_initialized = False
            
            info_log("[UnifiedController] 系統關閉完成")
            
        except Exception as e:
            error_log(f"[UnifiedController] 系統關閉失敗: {e}")


# 全局控制器實例
unified_controller = UnifiedController()