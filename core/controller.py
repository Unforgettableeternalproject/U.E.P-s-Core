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
from typing import Dict, Any, Optional, List
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
        
        # 使用全局單例模組協調器 (避免重複訂閱事件)
        from core.module_coordinator import module_coordinator
        self.module_coordinator = module_coordinator
        
        # 狀態佇列管理器引用
        from core.states.state_queue import get_state_queue_manager
        self.state_queue_manager = get_state_queue_manager()
        
        # 系統統計
        self.startup_time = None
        self.total_gs_sessions = 0
        self.system_errors = []
        
        # 階段五：背景任務監控
        self.background_tasks: Dict[str, Dict[str, Any]] = {}  # task_id -> task_info
        self.background_task_history: List[Dict[str, Any]] = []  # Completed tasks
        self.max_task_history = 100
        self.background_tasks_file = "memory/background_tasks.json"  # 持久化文件路徑
        
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
            
            # ✅ 清空狀態佇列（避免舊狀態殘留）
            self.state_queue_manager.clear_queue()
            info_log("[UnifiedController] 已清空狀態佇列")
            
            # 初始化核心框架
            if not self._initialize_framework():
                return False
                
            # 設置事件處理器
            self._setup_event_handlers()
            
            # 載入背景任務歷史
            self._load_background_tasks()
            
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
            
            # 檢查狀態佇列並監控 GS 結束條件
            self._monitor_gs_lifecycle(current_state, current_gs)
            
            # ✅ 檢查會話超時 (CS/WS 超時處理)
            self._check_session_timeouts()
            
            # 記錄系統狀態（簡化版）
            debug_log(3, f"[Monitor] 系統狀態: {current_state.value}, "
                        f"當前GS: {current_gs.session_id if current_gs else 'None'}")
            
        except Exception as e:
            debug_log(2, f"[Monitor] 健康檢查失敗: {e}")
    
    def _check_session_timeouts(self):
        """
        檢查會話超時 (每秒調用一次)
        
        根據文檔要求:
        - CS/WS 結束條件: 1) 外部中斷點被呼叫 2) 所屬循環結束
        - 超時是外部中斷的一種形式
        - 當用戶長時間無互動時,Controller 介入結束會話
        """
        try:
            # 調用 SessionManager 的超時檢查
            timeout_sessions = self.session_manager.check_session_timeouts()
            
            # 如果有會話超時,記錄並處理
            if timeout_sessions:
                for timeout_info in timeout_sessions:
                    session_id = timeout_info['session_id']
                    session_type = timeout_info['session_type']
                    reason = timeout_info['reason']
                    
                    info_log(f"[Controller] 會話超時處理: {session_type} {session_id} - {reason}")
                    
                    # 發布會話中斷事件 (如果需要通知其他組件)
                    if session_type in ['chatting', 'workflow']:
                        debug_log(2, f"[Controller] 會話 {session_id} 因超時被終止")
                        
        except Exception as e:
            error_log(f"[Controller] 檢查會話超時失敗: {e}")
    
    def _monitor_gs_lifecycle(self, current_state, current_gs):
        """監控 GS 生命週期，根據需要創建或結束 GS"""
        try:
            from core.states.state_queue import get_state_queue_manager
            from core.states.state_manager import UEPState
            
            state_queue = get_state_queue_manager()
            queue_status = state_queue.get_queue_status()
            
            # 檢查是否需要創建 GS
            if not current_gs:
                # 如果狀態佇列有項目或系統不在 IDLE 狀態，則需要創建 GS
                if (queue_status.get('queue_length', 0) > 0 or 
                    current_state != UEPState.IDLE):
                    
                    debug_log(2, f"[Controller] 檢測到需要創建 GS：狀態={current_state.value}, 佇列長度={queue_status.get('queue_length', 0)}")
                    self._create_gs_for_processing()
                    
                # 系統啟動時預先創建 GS
                elif not hasattr(self, '_initial_gs_created'):
                    debug_log(2, "[Controller] 系統啟動，預先創建初始 GS")
                    self._create_gs_for_processing()
                    self._initial_gs_created = True
                    
                return
                
            # 如果有活躍 GS，僅做監控不做結束判斷
            # GS 結束檢查移至 check_gs_end_conditions 方法，由 SystemLoop 在循環結束時調用
            if current_gs:
                debug_log(3, f"[Controller] GS {current_gs.session_id} 正在運行中")
                
        except Exception as e:
            error_log(f"[Controller] GS 生命週期監控失敗: {e}")

    def check_gs_end_conditions(self):
        """檢查 GS 結束條件 - 僅在系統循環結束時調用"""
        try:
            from core.states.state_queue import get_state_queue_manager
            from core.states.state_manager import UEPState
            
            # 1. 首先檢查並處理待結束的 WS（符合會話生命週期架構）
            self._check_and_end_pending_workflow_sessions()
            
            # 2. 然後檢查 GS 結束條件
            current_state = self.state_manager.get_current_state()
            current_gs = self.session_manager.get_current_general_session()
            
            if not current_gs:
                return
                
            state_queue = get_state_queue_manager()
            queue_status = state_queue.get_queue_status()
            
            # GS 結束條件：狀態佇列完全清空且當前狀態為 IDLE
            if (current_state == UEPState.IDLE and 
                queue_status.get('queue_length', 0) == 0 and
                queue_status.get('current_state') == 'idle'):
                
                debug_log(2, f"[Controller] 檢測到 GS 結束條件：狀態佇列已清空，準備結束 GS {current_gs.session_id}")
                self._end_current_gs_with_cleanup(current_gs.session_id)
                
        except Exception as e:
            debug_log(2, f"[Controller] GS 結束條件檢查失敗: {e}")
    
    def _check_and_end_pending_workflow_sessions(self):
        """檢查並結束標記待結束的 Workflow Sessions - 在循環完成邊界執行"""
        try:
            active_ws_list = self.session_manager.get_active_workflow_sessions()
            
            for ws in active_ws_list:
                # 檢查是否有 pending_end 標記
                if hasattr(ws, 'pending_end') and ws.pending_end:
                    session_id = ws.session_id
                    reason = getattr(ws, 'pending_end_reason', 'workflow_complete')
                    
                    debug_log(1, f"[Controller] 在循環邊界處理待結束的 WS: {session_id} (原因: {reason})")
                    
                    # 在循環完成邊界真正結束會話
                    success = self.session_manager.end_workflow_session(session_id)
                    
                    if success:
                        info_log(f"[Controller] ✅ 已在循環邊界結束 WS: {session_id}")
                    else:
                        error_log(f"[Controller] ⚠️ 在循環邊界結束 WS 失敗: {session_id}")
                        
        except Exception as e:
            error_log(f"[Controller] 檢查待結束 WS 時出錯: {e}")

    def _create_gs_for_processing(self):
        """創建 GS 以支持處理流程"""
        try:
            info_log("[Controller] 創建新的 GS 以支持系統處理")
            
            # 創建 General Session - 使用正確的方法名
            gs_result = self.session_manager.start_general_session(
                "system_event",
                {
                    "session_type": "general",
                    "created_by": "controller_monitor",
                    "context": {
                        "purpose": "system_processing",
                        "auto_created": True
                    }
                }
            )
            
            if gs_result:
                # 🔧 立即設置到全局上下文，供所有模組訪問
                try:
                    from core.working_context import working_context_manager
                    working_context_manager.global_context_data['current_gs_id'] = gs_result
                    working_context_manager.global_context_data['current_cycle_index'] = 0
                    debug_log(2, f"[Controller] 自動創建的 GS ID 和 cycle_index 已設置到全局上下文: {gs_result}, cycle=0")
                except Exception as e:
                    error_log(f"[Controller] 設置全局 GS ID 失敗: {e}")
                
                info_log(f"[Controller] 已自動創建 GS: {gs_result}")
            else:
                error_log("[Controller] GS 創建失敗")
                
        except Exception as e:
            error_log(f"[Controller] 創建 GS 失敗: {e}")
    
    def _end_current_gs_with_cleanup(self, gs_id: str):
        """結束當前 GS 並執行系統級清理"""
        try:
            info_log(f"[Controller] 系統級 GS 結束流程啟動: {gs_id}")
            
            # 1. 結束會話（由 Session Manager 處理）
            result = self.session_manager.end_general_session({
                "reason": "state_queue_empty",
                "triggered_by": "controller_monitor"
            })
            
            if result:
                # 2. 系統級清理：確保 Working Context 完全重置
                self._perform_system_cleanup_after_gs()
                
                info_log(f"[Controller] GS {gs_id} 已成功結束，系統清理完成")
            else:
                error_log(f"[Controller] GS {gs_id} 結束失敗")
                
        except Exception as e:
            error_log(f"[Controller] 結束 GS {gs_id} 時發生錯誤: {e}")
    
    def _perform_system_cleanup_after_gs(self):
        """GS 結束後的系統級清理"""
        try:
            from core.working_context import working_context_manager
            
            debug_log(3, "[Controller] 執行 GS 結束後的系統級清理...")
            
            # 1. 清理過期的 Working Context
            if hasattr(working_context_manager, 'cleanup_expired_contexts'):
                working_context_manager.cleanup_expired_contexts()
                debug_log(3, "[Controller] Working Context 過期項目已清理")
            
            # 2. 清理全局上下文中的 GS ID 和 cycle_index
            try:
                working_context_manager.global_context_data['current_gs_id'] = 'unknown'
                working_context_manager.global_context_data['current_cycle_index'] = 0
                debug_log(3, "[Controller] 全局 GS ID 和 cycle_index 已重置")
            except Exception as e:
                error_log(f"[Controller] 清理全局 GS ID 失敗: {e}")
            
            # 3. 重置 Speaker_Accumulation（確保新 GS 時清理）
            self._reset_speaker_accumulation()
            
            # 4. 驗證系統狀態一致性
            self._verify_system_state_consistency()
            
            debug_log(3, "[Controller] 系統級清理完成")
            
        except Exception as e:
            error_log(f"[Controller] 系統級清理失敗: {e}")
    
    def _reset_speaker_accumulation(self):
        """重置 Speaker_Accumulation 確保新 GS 時的清理"""
        try:
            from core.working_context import working_context_manager, ContextType
            
            # 檢查是否有 Speaker_Accumulation 需要清理
            speaker_context = working_context_manager.get_data(
                ContextType.CROSS_MODULE_DATA, "Speaker_Accumulation"
            )
            
            if speaker_context:
                debug_log(3, "[Controller] 清理 Speaker_Accumulation 數據")
                # 可以選擇清除或保留給下個 GS
                # 根據需求決定是否完全清除
                info_log("[Controller] Speaker_Accumulation 已處理")
                
        except Exception as e:
            debug_log(3, f"[Controller] Speaker_Accumulation 重置失敗: {e}")
    
    def _verify_system_state_consistency(self):
        """驗證系統狀態一致性"""
        try:
            from core.states.state_manager import UEPState
            
            current_state = self.state_manager.get_current_state()
            current_gs = self.session_manager.get_current_general_session()
            
            # GS 結束後，應該沒有活躍的 GS，系統狀態應該是 IDLE
            if current_gs is None and current_state == UEPState.IDLE:
                debug_log(3, "[Controller] 系統狀態一致性驗證通過")
            else:
                debug_log(2, f"[Controller] 系統狀態不一致：狀態={current_state.value}, GS存在={current_gs is not None}")
                
        except Exception as e:
            error_log(f"[Controller] 系統狀態一致性驗證失敗: {e}")
    
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
                
                # 🔧 立即設置到全局上下文，供所有模組訪問
                # 這確保 NLP/LLM/TTS 等模組在處理時能立即讀取到正確的 GS ID
                try:
                    from core.working_context import working_context_manager
                    working_context_manager.global_context_data['current_gs_id'] = current_gs_id
                    # 初始化 cycle_index 為 0（每個新 GS 從 cycle 0 開始）
                    working_context_manager.global_context_data['current_cycle_index'] = 0
                    debug_log(2, f"[UnifiedController] GS ID 和 cycle_index 已設置到全局上下文: {current_gs_id}, cycle=0")
                except Exception as e:
                    error_log(f"[UnifiedController] 設置全局 GS ID 失敗: {e}")
                
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
            from core.event_bus import event_bus, SystemEvent
            
            # 訂閱背景工作流事件
            event_bus.subscribe(SystemEvent.BACKGROUND_WORKFLOW_COMPLETED, 
                               self._handle_background_workflow_completed)
            event_bus.subscribe(SystemEvent.BACKGROUND_WORKFLOW_FAILED,
                               self._handle_background_workflow_failed)
            event_bus.subscribe(SystemEvent.BACKGROUND_WORKFLOW_CANCELLED,
                               self._handle_background_workflow_cancelled)
            
            info_log("[UnifiedController] 事件處理器設置完成 (包含背景工作流事件)")
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
                    "snapshot_available": hasattr(self.core_framework, 'collect_system_performance_snapshot'),
                    "latest_snapshot": None  # 預先定義為 None,避免類型推斷問題
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
                    session_stats = self.session_manager.get_session_statistics()  # type: ignore
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
            self.system_status = SystemStatus.STOPPED
            info_log("[UnifiedController] 系統已關閉")
            
        except Exception as e:
            error_log(f"[UnifiedController] 系統關閉失敗: {e}")
    
    # ========== 階段五：背景任務監控 ==========
    
    def _handle_background_workflow_completed(self, event):
        """
        處理背景工作流完成事件
        
        Args:
            event: Event 對象，包含 task_id, workflow_type, session_id, result
        """
        try:
            event_data = event.data
            task_id = event_data.get('task_id')
            workflow_type = event_data.get('workflow_type')
            result = event_data.get('result')
            
            info_log(f"[Controller] 背景工作流完成: {workflow_type} (task_id: {task_id})")
            
            # 從活躍任務移至歷史記錄
            if task_id in self.background_tasks:
                task_info = self.background_tasks[task_id]
                task_info['status'] = 'completed'
                task_info['end_time'] = time.time()
                task_info['result'] = result
                
                # 添加到歷史記錄
                self.background_task_history.append(task_info.copy())
                
                # 從活躍列表移除
                del self.background_tasks[task_id]
                
                debug_log(2, f"[Controller] Task {task_id} moved to history")
            
            # 清理舊歷史記錄
            self._cleanup_task_history()
            
            # 持久化到文件
            self._save_background_tasks()
            
            # 可選：通知使用者（透過 TTS 或 UI）
            self._notify_task_completion(task_id, workflow_type, result)
            
        except Exception as e:
            error_log(f"[Controller] 處理背景工作流完成事件失敗: {e}")
    
    def _handle_background_workflow_failed(self, event):
        """
        處理背景工作流失敗事件
        
        Args:
            event: Event 對象，包含 task_id, workflow_type, session_id, error
        """
        try:
            event_data = event.data
            task_id = event_data.get('task_id')
            workflow_type = event_data.get('workflow_type')
            error = event_data.get('error')
            
            error_log(f"[Controller] 背景工作流失敗: {workflow_type} (task_id: {task_id}), 錯誤: {error}")
            
            # 更新任務狀態
            if task_id in self.background_tasks:
                task_info = self.background_tasks[task_id]
                task_info['status'] = 'failed'
                task_info['end_time'] = time.time()
                task_info['error'] = error
                
                # 添加到歷史記錄
                self.background_task_history.append(task_info.copy())
                
                # 從活躍列表移除
                del self.background_tasks[task_id]
            
            # 清理舊歷史記錄
            self._cleanup_task_history()
            
            # 持久化到文件
            self._save_background_tasks()
            
            # 可選：通知使用者失敗
            self._notify_task_failure(task_id, workflow_type, error)
            
        except Exception as e:
            error_log(f"[Controller] 處理背景工作流失敗事件失敗: {e}")
    
    def _handle_background_workflow_cancelled(self, event_data: Dict[str, Any]):
        """
        處理背景工作流取消事件
        
        Args:
            event_data: 事件數據，包含 task_id, workflow_type
        """
        try:
            task_id = event_data.get('task_id')
            workflow_type = event_data.get('workflow_type')
            
            info_log(f"[Controller] 背景工作流取消: {workflow_type} (task_id: {task_id})")
            
            # 更新任務狀態
            if task_id in self.background_tasks:
                task_info = self.background_tasks[task_id]
                task_info['status'] = 'cancelled'
                task_info['end_time'] = time.time()
                
                # 添加到歷史記錄
                self.background_task_history.append(task_info.copy())
                
                # 從活躍列表移除
                del self.background_tasks[task_id]
            
            # 清理舊歷史記錄
            self._cleanup_task_history()
            
            # 持久化到文件
            self._save_background_tasks()
            
        except Exception as e:
            error_log(f"[Controller] 處理背景工作流取消事件失敗: {e}")
    
    def register_background_task(self, task_id: str, task_info: Dict[str, Any]):
        """
        註冊新的背景任務
        
        Args:
            task_id: 任務ID
            task_info: 任務資訊（workflow_type, session_id, metadata等）
        """
        try:
            self.background_tasks[task_id] = {
                'task_id': task_id,
                'start_time': time.time(),
                'status': 'running',
                **task_info
            }
            
            debug_log(2, f"[Controller] Registered background task: {task_id}")
            
        except Exception as e:
            error_log(f"[Controller] 註冊背景任務失敗: {e}")
    
    def get_background_task_status(self, task_id: str) -> Optional[Dict[str, Any]]:
        """
        獲取背景任務狀態
        
        Args:
            task_id: 任務ID
            
        Returns:
            任務資訊或 None
        """
        # 先查活躍任務
        if task_id in self.background_tasks:
            return self.background_tasks[task_id].copy()
        
        # 再查歷史記錄
        for task in self.background_task_history:
            if task['task_id'] == task_id:
                return task.copy()
        
        return None
    
    def get_all_background_tasks(self) -> Dict[str, Any]:
        """
        獲取所有背景任務資訊
        
        Returns:
            包含活躍任務和歷史記錄的字典
        """
        return {
            'active_tasks': list(self.background_tasks.values()),
            'task_history': self.background_task_history.copy(),
            'active_count': len(self.background_tasks),
            'completed_count': sum(1 for t in self.background_task_history if t.get('status') == 'completed'),
            'failed_count': sum(1 for t in self.background_task_history if t.get('status') == 'failed')
        }
    
    def _cleanup_task_history(self):
        """清理舊的任務歷史記錄，保留最近的 max_task_history 個"""
        if len(self.background_task_history) > self.max_task_history:
            # 按結束時間排序，保留最新的
            self.background_task_history.sort(key=lambda t: t.get('end_time', 0))
            self.background_task_history = self.background_task_history[-self.max_task_history:]
            debug_log(3, f"[Controller] Cleaned up task history, keeping {self.max_task_history} recent tasks")
    
    def _notify_task_completion(self, task_id: str, workflow_type: str, result: Any):
        """
        通知使用者任務完成（可選功能）
        
        Args:
            task_id: 任務ID
            workflow_type: 工作流類型
            result: 執行結果
        """
        try:
            # 獲取 TTS 模組進行語音通知
            tts_module = self.core_framework.get_module("tts")
            if tts_module:
                notification_message = f"背景任務已完成：{workflow_type}"
                try:
                    # 異步發送 TTS 通知（不阻塞）
                    tts_module.speak(notification_message, priority="low")
                    debug_log(2, f"[Controller] 已發送 TTS 完成通知: {workflow_type}")
                except Exception as e:
                    debug_log(2, f"[Controller] TTS 通知失敗: {e}")
            
            # TODO: 整合 UI 模組顯示通知
            # ui_module = self.module_registry.get("UI")
            # if ui_module:
            #     ui_module.show_notification(f"任務完成: {workflow_type}", "success")
            
            debug_log(2, f"[Controller] Task completion notification: {workflow_type} completed")
            
        except Exception as e:
            error_log(f"[Controller] 發送任務完成通知失敗: {e}")
    
    def _notify_task_failure(self, task_id: str, workflow_type: str, error: str):
        """
        通知使用者任務失敗（可選功能）
        
        Args:
            task_id: 任務ID
            workflow_type: 工作流類型
            error: 錯誤訊息
        """
        try:
            # 獲取 TTS 模組進行語音通知
            tts_module = self.core_framework.get_module("tts")
            if tts_module:
                notification_message = f"背景任務失敗：{workflow_type}，錯誤：{error}"
                try:
                    # 異步發送 TTS 通知（不阻塞）
                    tts_module.speak(notification_message, priority="high")
                    debug_log(2, f"[Controller] 已發送 TTS 失敗通知: {workflow_type}")
                except Exception as e:
                    debug_log(2, f"[Controller] TTS 通知失敗: {e}")
            
            # TODO: 整合 UI 模組顯示錯誤通知
            # ui_module = self.module_registry.get("UI")
            # if ui_module:
            #     ui_module.show_notification(f"任務失敗: {workflow_type}", "error")
            
            debug_log(2, f"[Controller] Task failure notification: {workflow_type} failed - {error}")
            
        except Exception as e:
            error_log(f"[Controller] 發送任務失敗通知失敗: {e}")
    
    def _save_background_tasks(self):
        """
        持久化背景任務到文件
        儲存當前活躍任務和歷史記錄
        """
        try:
            import json
            import os
            
            # 確保目錄存在
            os.makedirs(os.path.dirname(self.background_tasks_file), exist_ok=True)
            
            # 準備數據
            data = {
                "active_tasks": list(self.background_tasks.values()),
                "task_history": self.background_task_history,
                "last_updated": time.time()
            }
            
            # 寫入文件
            with open(self.background_tasks_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            
            debug_log(3, f"[Controller] 已儲存背景任務到 {self.background_tasks_file}")
            
        except Exception as e:
            error_log(f"[Controller] 儲存背景任務失敗: {e}")
    
    def _load_background_tasks(self):
        """
        從文件載入背景任務歷史
        注意：活躍任務不會恢復，因為執行緒已終止
        """
        try:
            import json
            import os
            
            if not os.path.exists(self.background_tasks_file):
                debug_log(2, "[Controller] 背景任務文件不存在，跳過載入")
                return
            
            # 讀取文件
            with open(self.background_tasks_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            # 載入歷史記錄（不載入活躍任務，因為無法恢復執行狀態）
            self.background_task_history = data.get("task_history", [])
            
            # 檢查是否有未完成的任務（這些任務可能在上次關閉時丟失）
            active_tasks = data.get("active_tasks", [])
            if active_tasks:
                info_log(f"[Controller] 發現 {len(active_tasks)} 個未完成的背景任務（已丟失，無法恢復）")
                # 將這些任務標記為失敗並加入歷史
                for task in active_tasks:
                    task['status'] = 'failed'
                    task['end_time'] = time.time()
                    task['error'] = '系統重啟導致任務中斷'
                    self.background_task_history.append(task)
            
            info_log(f"[Controller] 已載入 {len(self.background_task_history)} 條背景任務歷史記錄")
            
        except Exception as e:
            error_log(f"[Controller] 載入背景任務失敗: {e}")
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