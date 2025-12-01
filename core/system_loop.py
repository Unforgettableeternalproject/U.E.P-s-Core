# core/system_loop.py
"""
系統主循環 - UEP 系統的核心運行邏輯

實現完整的系統處理循環：
1. 等待使用者輸入（STT 持續監聽）
2. 啟動 GS 並等待輸入層輸出（NLP）
3. 根據 NLP 分析決定處理層路徑（CHAT/WORK）
4. 處理層模組從 WC/會話管理器獲取資料並處理
5. 結果轉送給輸出層（TTS）
6. Framework 蒐集效能快照

循環流程：
STT → NLP → Router → (CS/WS) → MEM/LLM/SYS → Router → TTS → 效能監控
"""

import time
import threading
from typing import Dict, Any, Optional, Callable
from enum import Enum

from utils.debug_helper import debug_log, info_log, error_log


class LoopStatus(Enum):
    """循環狀態"""
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    PAUSING = "pausing"
    PAUSED = "paused"
    STOPPING = "stopping"
    ERROR = "error"


class SystemLoop:
    """系統主循環 - 實現完整的 UEP 處理流程"""
    
    def __init__(self):
        """初始化系統循環"""
        # 載入配置
        from configs.config_loader import load_config, get_input_mode
        self.config = load_config()
        self.input_mode = get_input_mode()  # "vad" 或 "text"
        
        # 循環狀態
        self.status = LoopStatus.STOPPED
        self.loop_thread: Optional[threading.Thread] = None
        self.stop_event = threading.Event()
        
        # 文字輸入模式專用
        self.text_input_thread: Optional[threading.Thread] = None
        self.text_input_prompt = ">>> "  # 文字輸入提示符
        
        # 效能監控
        self.loop_count = 0  # 基本循環計數（主循環迭代次數）
        self.cycle_index = 0  # 完整處理週期計數（輸入→輸出）- 用於 flow-based 去重
        self.processing_cycles = 0  # 向後兼容：等同於 cycle_index
        self.current_cycle_start_time = None
        self.cycle_tracking = {
            "input_received": False,
            "processing_started": False, 
            "output_completed": False
        }
        self.start_time = 0
        self.last_snapshot_time = 0
        self.last_status_log_time = 0
        self.snapshot_interval = 5.0  # 5秒間隔蒐集效能快照
        self.status_log_interval = 10.0  # 10秒間隔輸出狀態日誌
        
        # 🔧 Cycle 層級的處理/輸出追蹤（確保所有輸出完成後才發布 CYCLE_COMPLETED）
        # 格式: {"session_id:cycle_index": {"processing_count": int, "output_count": int}}
        self._cycle_layer_tracking = {}  # 追蹤每個 cycle 的處理層和輸出層完成情況
        self._cycle_tracking_lock = threading.Lock()  # 保護 _cycle_layer_tracking
        
        # ✅ 狀態監控相關
        from core.states.state_manager import UEPState
        self._previous_state = UEPState.IDLE  # 初始化為 IDLE，避免首次檢查失敗
        
        # 🔧 工作流輸入相關
        self._pending_stt_restart = False  # 延遲 STT 重啟標記
        
        # 🔧 輸入模式切換相關
        self._stt_listening_active = False  # STT 監聽是否活躍
        self._text_input_active = False  # 文字輸入是否活躍
        
        # P1/P2 設定整合
        from configs.user_settings_manager import get_user_setting
        self.gc_interval = get_user_setting("advanced.performance.gc_interval", 300)
        self.last_gc_time = time.time()
        self.allow_system_initiative = get_user_setting("interaction.proactivity.allow_system_initiative", True)
        self.initiative_cooldown = get_user_setting("interaction.proactivity.initiative_cooldown", 300)
        self.require_user_input = get_user_setting("interaction.proactivity.require_user_input", False)
        debug_log(2, f"[SystemLoop] Proactivity: initiative={self.allow_system_initiative}, cooldown={self.initiative_cooldown}s")
        
        info_log(f"[SystemLoop] 系統循環已創建 (輸入模式: {self.input_mode})")
        
        # ✅ 訂閱事件總線
        self._setup_event_subscriptions()
    
    def _setup_event_subscriptions(self):
        """設置事件訂閱"""
        try:
            from core.event_bus import event_bus, SystemEvent
            
            # 訂閱輸出層完成事件
            event_bus.subscribe(
                SystemEvent.OUTPUT_LAYER_COMPLETE,
                self._on_output_layer_complete,
                handler_name="SystemLoop.output_complete"
            )
            
            # 階段三：訂閱工作流輸入事件
            event_bus.subscribe(
                SystemEvent.WORKFLOW_REQUIRES_INPUT,
                self._on_workflow_requires_input,
                handler_name="SystemLoop.workflow_requires_input"
            )
            
            event_bus.subscribe(
                SystemEvent.WORKFLOW_INPUT_COMPLETED,
                self._on_workflow_input_completed,
                handler_name="SystemLoop.workflow_input_completed"
            )
            
            # 🔧 訂閱檔案輸入事件（拖放檔案到前端）
            event_bus.subscribe(
                SystemEvent.FILE_INPUT_PROVIDED,
                self._on_file_input_provided,
                handler_name="SystemLoop.file_input_provided"
            )
            
            # 訂閱處理層完成事件（追蹤輸出任務啟動）
            event_bus.subscribe(
                SystemEvent.PROCESSING_LAYER_COMPLETE,
                self._on_processing_layer_complete_for_tracking,
                handler_name="SystemLoop.processing_complete_tracking"
            )
            
            info_log("[SystemLoop] ✅ 已訂閱事件總線")
            
        except Exception as e:
            error_log(f"[SystemLoop] 事件訂閱失敗: {e}")
    
    def _start_event_bus(self):
        """啟動事件總線處理線程"""
        try:
            from core.event_bus import event_bus
            event_bus.start()
            info_log("[SystemLoop] ✅ 事件總線已啟動")
        except Exception as e:
            error_log(f"[SystemLoop] 啟動事件總線失敗: {e}")
    
    def _stop_event_bus(self):
        """停止事件總線處理線程"""
        try:
            from core.event_bus import event_bus
            event_bus.stop()
            info_log("[SystemLoop] ✅ 事件總線已停止")
        except Exception as e:
            error_log(f"[SystemLoop] 停止事件總線失敗: {e}")
    
    def _on_processing_layer_complete_for_tracking(self, event):
        """處理處理層完成事件 - 用於追蹤輸出任務啟動"""
        try:
            # 從處理層數據提取響應，如果有文字內容則表示會啟動 TTS
            response = event.data.get('response', '')
            if response and response.strip():
                # 使用真實的 GS ID 而不是事件中的 session_id（可能是測試用的假 ID）
                gs_id = self._get_current_gs_id()
                cycle_index = self.cycle_index  # 使用 SystemLoop 自己的 cycle_index
                cycle_key = f"{gs_id}:{cycle_index}"
                
                with self._cycle_tracking_lock:
                    if cycle_key not in self._cycle_layer_tracking:
                        self._cycle_layer_tracking[cycle_key] = {"processing_count": 0, "output_count": 0}
                    
                    self._cycle_layer_tracking[cycle_key]["processing_count"] += 1
                    counts = self._cycle_layer_tracking[cycle_key]
                    debug_log(2, f"[SystemLoop] 📝 PROCESSING 完成 (cycle={cycle_key}, P={counts['processing_count']}, O={counts['output_count']})")
        except Exception as e:
            error_log(f"[SystemLoop] 追蹤 PROCESSING 完成失敗: {e}")
    
    def _on_output_layer_complete(self, event):
        """
        輸出層完成事件處理器
        當 TTS 發布 OUTPUT_LAYER_COMPLETE 事件時觸發
        """
        try:
            debug_log(2, f"[SystemLoop] 收到輸出層完成事件: {event.event_id}")
            
            # 使用真實的 GS ID 而不是事件中的 session_id
            gs_id = self._get_current_gs_id()
            cycle_index = self.cycle_index  # 使用 SystemLoop 自己的 cycle_index
            cycle_key = f"{gs_id}:{cycle_index}"
            
            with self._cycle_tracking_lock:
                if cycle_key in self._cycle_layer_tracking:
                    self._cycle_layer_tracking[cycle_key]["output_count"] += 1
                    counts = self._cycle_layer_tracking[cycle_key]
                    debug_log(2, f"[SystemLoop] ✅ OUTPUT 完成 (cycle={cycle_key}, P={counts['processing_count']}, O={counts['output_count']})")
                else:
                    # 沒有對應的 PROCESSING 記錄（可能是直接調用 TTS）
                    debug_log(3, f"[SystemLoop] OUTPUT 完成但無對應的 PROCESSING 記錄 (cycle={cycle_key})")
            
            self.handle_output_completion(event.data)
        except Exception as e:
            error_log(f"[SystemLoop] 處理輸出層完成事件失敗: {e}")
    
    def _on_workflow_requires_input(self, event):
        """
        工作流需要輸入事件處理器（階段三）
        當工作流 Interactive 步驟觸發時
        """
        try:
            from core.working_context import working_context_manager
            
            debug_log(2, f"[SystemLoop] 工作流需要使用者輸入: {event.data}")
            
            # 設置工作流等待輸入旗標
            working_context_manager.set_workflow_waiting_input(True)
            
            # 🆕 保存工作流輸入上下文信息供 LLM 使用
            workflow_input_context = {
                'workflow_type': event.data.get('workflow_type', 'unknown'),
                'workflow_session_id': event.data.get('session_id'),
                'step_id': event.data.get('step_id', 'input_step'),
                'step_type': event.data.get('step_type', 'interactive'),
                'prompt': event.data.get('prompt', '請提供輸入'),
                'optional': event.data.get('optional', False)
            }
            working_context_manager.set_context_data('workflow_input_context', workflow_input_context)
            debug_log(2, f"[SystemLoop] 保存工作流輸入上下文: {workflow_input_context}")
            
            # 清除跳過輸入層旗標，允許輸入層執行
            working_context_manager.set_skip_input_layer(False, reason="workflow_input")
            
            # ✅ 更新 global_context_data 使用原始 GS ID（cycle_index 不變，等待循環完成後統一遞增）
            # 注入的輸入應該關聯到觸發工作流的原始 GS，而不是工作流自己的 session
            workflow_session_id = event.data.get('session_id')
            if workflow_session_id:
                from core.sessions.session_manager import session_manager
                ws = session_manager.get_workflow_session(workflow_session_id)
                if ws and hasattr(ws, 'gs_session_id'):
                    # 使用觸發工作流的原始 GS ID（stored in ws.gs_session_id）
                    working_context_manager.set_context_data('current_gs_id', ws.gs_session_id)
                    # cycle_index 使用遞增後的循環索引
                    working_context_manager.set_context_data('current_cycle_index', self.cycle_index)
                    debug_log(2, f"[SystemLoop] 更新 global_context 為原始 GS: gs_id={ws.gs_session_id}, cycle={self.cycle_index}")
            
            # 🆕 保存工作流 prompt，供 text_input_loop 使用
            workflow_prompt = event.data.get('prompt', '請輸入: ')
            self.current_workflow_prompt = workflow_prompt
            
            # 在 text mode 下顯示工作流 prompt
            if self.input_mode == "text":
                info_log(f"[SystemLoop] 💬 {workflow_prompt}")
            
            info_log("[SystemLoop] 💬 工作流等待使用者輸入，輸入層已啟用")
            
            # 🔧 VAD 模式下，設置延遲啟動標記，等待當前循環完全結束後再啟動 STT
            # 原因：避免在 TTS 播放提示或輸出層未完成時過早啟動 VAD
            if self.input_mode == "vad":
                self._pending_stt_restart = True
                debug_log(2, "[SystemLoop] 工作流等待輸入，設置延遲 STT 重啟標記 (等待循環結束)")
            
        except Exception as e:
            error_log(f"[SystemLoop] 處理工作流輸入請求失敗: {e}")
    
    def _on_workflow_input_completed(self, event):
        """
        工作流輸入完成事件處理器（階段三）
        當使用者提供輸入後由工作流引擎發布
        """
        try:
            from core.working_context import working_context_manager
            
            debug_log(2, f"[SystemLoop] 工作流輸入完成: {event.data}")
            
            # 清除工作流 prompt
            self.current_workflow_prompt = None
            
            # 重置工作流等待輸入旗標
            working_context_manager.set_workflow_waiting_input(False)
            
            # 設置跳過輸入層旗標（下一循環跳過）
            working_context_manager.set_skip_input_layer(True, reason="workflow_processing")
            
            debug_log(2, "[SystemLoop] 工作流輸入完成，下一循環將跳過輸入層")
            
        except Exception as e:
            error_log(f"[SystemLoop] 處理工作流輸入完成事件失敗: {e}")
    
    def _on_file_input_provided(self, event):
        """
        檔案輸入事件處理器（透過拖放提供檔案）
        
        當使用者透過前端拖放檔案時：
        1. 檢查是否有工作流正在等待輸入
        2. 提交檔案路徑到工作流
        3. 啟動新的循環來繼續工作流執行
        """
        try:
            from core.working_context import working_context_manager
            from core.framework import core_framework
            
            file_path = event.data.get('file_path', '')
            workflow_session_id = event.data.get('workflow_session_id', '')
            step_id = event.data.get('step_id', '')
            
            if not file_path or not workflow_session_id:
                error_log("[SystemLoop] 檔案輸入事件缺少必要參數")
                return
            
            info_log(f"[SystemLoop] 📁 收到檔案輸入: {file_path} (workflow={workflow_session_id}, step={step_id})")
            
            # 獲取 SYS 模組並提交輸入到工作流
            sys_module = core_framework.get_module('sys')
            if not sys_module:
                error_log("[SystemLoop] 無法獲取 SYS 模組")
                return
            
            # 調用 provide_workflow_input 提交檔案路徑
            result = sys_module.provide_workflow_input(
                session_id=workflow_session_id,
                user_input=file_path,
                use_fallback=False
            )
            
            if result.get('status') == 'success':
                info_log(f"[SystemLoop] ✅ 檔案路徑已提交到工作流: {file_path}")
                
                # 重置工作流等待輸入旗標
                working_context_manager.set_workflow_waiting_input(False)
                
                # 設置跳過輸入層旗標（下一循環跳過）
                working_context_manager.set_skip_input_layer(True, reason="file_input_processing")
                
                # 🚀 啟動新的循環來繼續工作流執行
                # 透過狀態佇列加入 WORK 狀態，觸發處理層和輸出層
                from core.states.state_queue import get_state_queue_manager
                from core.states.state_manager import UEPState
                
                state_queue = get_state_queue_manager()
                state_queue.add_state(
                    state=UEPState.WORK,
                    trigger_content=f"File input: {file_path}",
                    priority=10,
                    source="file_input"
                )
                
                debug_log(2, "[SystemLoop] 已加入 WORK 狀態到佇列，啟動工作流繼續執行")
            else:
                error_message = result.get('error', 'Unknown error')
                error_log(f"[SystemLoop] 提交檔案路徑到工作流失敗: {error_message}")
            
        except Exception as e:
            error_log(f"[SystemLoop] 處理檔案輸入事件失敗: {e}")
    
    def _get_current_gs_id(self) -> str:
        """
        獲取當前 General Session ID
        
        Returns:
            str: 當前 GS ID,如果無法獲取則返回 'unknown'
        """
        try:
            from core.sessions.session_manager import session_manager
            
            # 從 UnifiedSessionManager 獲取當前 GS
            current_gs = session_manager.get_current_general_session()
            if current_gs and hasattr(current_gs, 'session_id'):
                return current_gs.session_id
            
            debug_log(3, "[SystemLoop] 無法獲取 GS ID,使用預設值 'unknown'")
            return 'unknown'
            
        except Exception as e:
            error_log(f"[SystemLoop] 獲取 GS ID 失敗: {e}")
            return 'unknown'
    
    def _publish_cycle_completed(self):
        """
        發布 CYCLE_COMPLETED 事件
        用於通知 ModuleCoordinator 清理去重鍵
        """
        try:
            from core.event_bus import event_bus, SystemEvent
            
            session_id = self._get_current_gs_id()
            event_data = {
                'session_id': session_id,
                'cycle_index': self.cycle_index,
                'timestamp': time.time()
            }
            
            event_bus.publish(SystemEvent.CYCLE_COMPLETED, event_data)
            debug_log(2, f"[SystemLoop] 🔄 已發布 CYCLE_COMPLETED (session={session_id}, cycle={self.cycle_index})")
            
        except Exception as e:
            error_log(f"[SystemLoop] 發布 CYCLE_COMPLETED 事件失敗: {e}")
    
    def _update_global_cycle_info(self):
        """
        更新 working_context 全局數據中的循環資訊
        供所有模組訪問當前 cycle_index 和 session_id
        """
        try:
            from core.working_context import working_context_manager
            
            session_id = self._get_current_gs_id()
            working_context_manager.global_context_data['current_cycle_index'] = self.cycle_index
            working_context_manager.global_context_data['current_gs_id'] = session_id
            
            debug_log(3, f"[SystemLoop] 已更新全局循環資訊: session={session_id}, cycle={self.cycle_index}")
            
        except Exception as e:
            error_log(f"[SystemLoop] 更新全局循環資訊失敗: {e}")
    
    def start(self) -> bool:
        """啟動系統主循環"""
        try:
            if self.status != LoopStatus.STOPPED:
                info_log(f"[SystemLoop] 循環已在運行中: {self.status.value}")
                return True
            
            info_log("🔄 啟動系統主循環...")
            self.status = LoopStatus.STARTING
            
            # ✅ 啟動事件總線
            self._start_event_bus()
            
            # 註冊 user_settings 熱重載回調
            from configs.user_settings_manager import user_settings_manager
            user_settings_manager.register_reload_callback("system_loop", self._reload_from_user_settings)
            
            # 🔧 初始化 global_context 的 cycle_index，讓模組能讀到正確的初始值
            self._update_global_cycle_info()
            info_log(f"[SystemLoop] 已初始化全局循環資訊: cycle_index={self.cycle_index}")
            
            # 驗證系統組件就緒
            if not self._verify_system_ready():
                error_log("❌ 系統組件未就緒，無法啟動循環")
                self.status = LoopStatus.ERROR
                return False
            
            # 重置狀態
            self.stop_event.clear()
            self.loop_count = 0
            self.processing_cycles = 0
            self.current_cycle_start_time = None
            self.cycle_tracking = {
                "input_received": False,
                "processing_started": False, 
                "output_completed": False
            }
            self.start_time = time.time()
            self.last_snapshot_time = time.time()
            self.last_status_log_time = time.time()
            
            # 啟動循環線程
            self.loop_thread = threading.Thread(target=self._main_loop, daemon=True)
            self.loop_thread.start()
            
            # 根據輸入模式啟動對應的輸入方式
            if self.input_mode == "text":
                info_log("📝 啟動文字輸入模式...")
                self._start_text_input()
                info_log("✅ 系統主循環已啟動")
                info_log("⌨️  等待使用者文字輸入...")
            else:  # vad 模式
                # 啟動STT持續監聽
                self._start_stt_listening()
                info_log("✅ 系統主循環已啟動")
                info_log("🎧 等待使用者語音輸入...")
            
            self.status = LoopStatus.RUNNING
            return True
            
        except Exception as e:
            error_log(f"❌ 啟動系統循環失敗: {e}")
            self.status = LoopStatus.ERROR
            return False
    
    def stop(self) -> bool:
        """停止系統主循環"""
        try:
            if self.status == LoopStatus.STOPPED:
                info_log("[SystemLoop] 循環已停止")
                return True
            
            info_log("🛑 停止系統主循環...")
            self.status = LoopStatus.STOPPING
            
            # 設置停止事件
            self.stop_event.set()
            
            # 等待循環線程結束
            if self.loop_thread and self.loop_thread.is_alive():
                self.loop_thread.join(timeout=5.0)
                if self.loop_thread.is_alive():
                    error_log("⚠️ 循環線程未能正常結束")
            
            # ✅ 停止事件總線
            self._stop_event_bus()
            
            self.status = LoopStatus.STOPPED
            runtime = time.time() - self.start_time
            info_log(f"✅ 系統循環已停止，運行 {runtime:.1f}秒，處理 {self.processing_cycles} 次完整週期（基本循環 {self.loop_count} 次）")
            
            return True
            
        except Exception as e:
            error_log(f"❌ 停止系統循環失敗: {e}")
            return False
    
    def _verify_system_ready(self) -> bool:
        """驗證系統組件就緒"""
        try:
            # 檢查 Framework
            from core.framework import core_framework
            if not core_framework.is_initialized:
                error_log("   ❌ Framework 未初始化")
                return False
            
            # 檢查 Controller
            from core.controller import unified_controller
            if hasattr(unified_controller, 'is_initialized') and not unified_controller.is_initialized:
                error_log("   ❌ Controller 未初始化")
                return False
            
            # 檢查 State Manager
            from core.states.state_manager import state_manager, UEPState
            current_state = state_manager.get_current_state()
            if current_state != UEPState.IDLE:
                error_log(f"   ❌ 系統狀態不正確: {current_state}")
                return False
            
            # 檢查關鍵模組
            required_modules = ['stt', 'nlp']
            available_modules = list(core_framework.modules.keys())
            missing_modules = [m for m in required_modules if m not in available_modules]
            if missing_modules:
                error_log(f"   ❌ 缺少關鍵模組: {missing_modules}")
                return False
            
            info_log("   ✅ 系統組件驗證通過")
            return True
            
        except Exception as e:
            error_log(f"   ❌ 系統組件驗證失敗: {e}")
            return False
    
    def _start_text_input(self):
        """啟動文字輸入模式"""
        try:
            from core.framework import core_framework
            
            # 獲取STT模組
            stt_module = core_framework.get_module('stt')
            if not stt_module:
                error_log("❌ 無法獲取STT模組")
                return False
            
            info_log("⌨️  啟動文字輸入循環...")
            
            # 在背景線程中運行文字輸入循環
            def text_input_loop():
                try:
                    while not self.stop_event.is_set():
                        try:
                            # 🆕 檢查是否有工作流 prompt
                            current_prompt = getattr(self, 'current_workflow_prompt', None)
                            if current_prompt:
                                # 使用工作流 prompt
                                prompt_to_use = f"\n{current_prompt}\n>>> "
                                # 清除工作流 prompt（只使用一次）
                                self.current_workflow_prompt = None
                            else:
                                # 使用默認 prompt
                                prompt_to_use = self.text_input_prompt
                            
                            # 等待用戶輸入
                            user_input = input(prompt_to_use)
                            
                            # 過濾空輸入（但工作流輸入允許空字串）
                            if not user_input.strip() and not current_prompt:
                                continue
                            
                            # 處理特殊命令
                            if user_input.lower() in ['exit', 'quit', 'q']:
                                info_log("📝 收到退出命令，停止系統...")
                                self.stop()
                                break
                            
                            # 將文字輸入傳遞給 STT 模組處理
                            debug_log(2, f"[SystemLoop] 收到文字輸入: {user_input}")
                            result = stt_module.handle_text_input(user_input)
                            
                            if result:
                                debug_log(2, f"[SystemLoop] 文字輸入處理成功")
                            else:
                                error_log(f"[SystemLoop] 文字輸入處理失敗")
                                
                        except EOFError:
                            # 處理 Ctrl+D (Unix) 或 Ctrl+Z (Windows)
                            info_log("📝 收到 EOF，停止文字輸入...")
                            break
                        except KeyboardInterrupt:
                            # 處理 Ctrl+C
                            info_log("📝 收到中斷信號，停止系統...")
                            self.stop()
                            break
                            
                except Exception as e:
                    error_log(f"[SystemLoop] 文字輸入循環錯誤: {e}")
            
            self.text_input_thread = threading.Thread(target=text_input_loop, daemon=True)
            self.text_input_thread.start()
            
            self._text_input_active = True
            info_log("✅ 文字輸入循環已啟動")
            return True
            
        except Exception as e:
            error_log(f"❌ 啟動文字輸入失敗: {e}")
            return False
    
    def _start_stt_listening(self):
        """啟動STT持續監聽（僅在 VAD 模式）"""
        try:
            # 🔧 檢查輸入模式，只在 VAD 模式才啟動 STT 監聽
            if self.input_mode != "vad":
                debug_log(2, f"[SystemLoop] 非 VAD 模式 ({self.input_mode})，跳過 STT 監聽啟動")
                return True  # 返回 True 表示「不需要啟動」這個狀態正常
            
            from core.framework import core_framework
            
            # 獲取STT模組
            stt_module = core_framework.get_module('stt')
            if not stt_module:
                error_log("❌ 無法獲取STT模組")
                return False
            
            info_log("🎤 啟動STT持續監聽 (VAD 模式)...")
            
            # 創建持續監聽的輸入
            from modules.stt_module.schemas import STTInput, ActivationMode
            stt_input = STTInput(
                mode=ActivationMode.CONTINUOUS,
                context="system_loop_continuous_listening"
            )
            
            # 在背景線程中啟動監聽
            def continuous_listening():
                try:
                    result = stt_module.handle(stt_input.dict())
                    debug_log(2, f"[SystemLoop] STT持續監聽結果: {result}")
                except Exception as e:
                    error_log(f"[SystemLoop] STT持續監聽錯誤: {e}")
            
            listening_thread = threading.Thread(target=continuous_listening, daemon=True)
            listening_thread.start()
            
            self._stt_listening_active = True
            info_log("✅ STT持續監聽已啟動")
            return True
            
        except Exception as e:
            error_log(f"❌ 啟動STT監聽失敗: {e}")
            return False
    
    def _restart_stt_listening(self):
        """重新啟動STT監聽（僅在 VAD 模式）"""
        try:
            # 🔧 檢查輸入模式，只在 VAD 模式才重啟 STT 監聽
            if self.input_mode != "vad":
                debug_log(2, f"[SystemLoop] 非 VAD 模式 ({self.input_mode})，跳過 STT 監聽重啟")
                return True  # 返回 True 表示「不需要重啟」這個狀態正常
            
            from core.framework import core_framework
            
            # 獲取STT模組並恢復監聽能力
            stt_module = core_framework.get_module('stt')
            if stt_module:
                stt_module.resume_listening()
                info_log("🔄 重新啟動STT監聽 (VAD 模式)")
                return self._start_stt_listening()
            else:
                error_log("❌ 無法獲取STT模組進行重啟")
                return False
                
        except Exception as e:
            error_log(f"❌ 重新啟動STT監聽失敗: {e}")
            return False
    
    def _main_loop(self):
        """主循環執行緒"""
        info_log("🔄 主循環線程已啟動")
        
        try:
            while not self.stop_event.is_set():
                current_time = time.time()
                
                # 檢查是否需要蒐集效能快照
                if current_time - self.last_snapshot_time >= self.snapshot_interval:
                    self._collect_performance_snapshot()
                    self.last_snapshot_time = current_time
                
                # 檢查是否需要輸出狀態日誌
                if current_time - self.last_status_log_time >= self.status_log_interval:
                    self._log_system_status()
                    self.last_status_log_time = current_time
                
                # P1: 定期觸發 GC
                if self.gc_interval > 0 and current_time - self.last_gc_time >= self.gc_interval:
                    import gc
                    collected = gc.collect()
                    debug_log(3, f"[SystemLoop] GC 觸發，回收 {collected} 個物件")
                    self.last_gc_time = current_time
                
                # 檢查系統狀態變化
                self._monitor_system_state()
                
                # 短暫休眠避免占用過多 CPU
                time.sleep(0.1)
                
        except Exception as e:
            error_log(f"❌ 主循環執行錯誤: {e}")
            self.status = LoopStatus.ERROR
        
        info_log("🔄 主循環線程已結束")
    
    def _monitor_system_state(self):
        """監控系統狀態變化和處理週期"""
        try:
            from core.states.state_manager import state_manager, UEPState
            from core.states.state_queue import get_state_queue_manager
            from core.working_context import working_context_manager
            
            state_queue = get_state_queue_manager()
            
            # 🆕 循環開始時：檢查並推進狀態佇列
            # 如果上一個狀態已完成且佇列有待處理項目，推進到下一個狀態
            state_advanced = state_queue.check_and_advance_state()
            if state_advanced:
                debug_log(2, "[SystemLoop] ✅ 循環開始時成功推進狀態")
            
            current_state = state_manager.get_current_state()
            queue_size = len(state_queue.queue) if hasattr(state_queue, 'queue') else 0
            
            # 階段三：檢查層級跳過旗標（在循環開始前檢查並重置）
            should_skip = working_context_manager.should_skip_input_layer()
            is_workflow_waiting = working_context_manager.is_workflow_waiting_input()
            
            if should_skip and not is_workflow_waiting:
                skip_reason = working_context_manager.get_skip_reason()
                # debug_log(2, f"[SystemLoop] 跳過輸入層 (原因: {skip_reason})")  # 註解：減少日誌噪音
                # 注意：實際的輸入層跳過邏輯由各輸入模組（STT/NLP）實現
                # 這裡只記錄日誌，循環結束後會重置旗標
            
            # 基本循環計數（每次監控迭代）
            self.loop_count += 1
            
            # 追蹤完整處理週期
            self._track_processing_cycle(current_state, queue_size)
            
            # 如果正在等待輸出層，不進行其他處理
            if hasattr(self, '_waiting_for_output') and self._waiting_for_output:
                return
            
            # 檢查狀態佇列是否有新項目
            if queue_size > 0:
                debug_log(3, f"[SystemLoop] 檢測到狀態佇列活動: {queue_size} 項目")
                        
                # 在非IDLE狀態添加等待機制，避免CPU過度使用
                if current_state in [UEPState.CHAT, UEPState.WORK]:
                    # 檢查是否有活躍的模組處理
                    active_modules = self._check_active_modules()
                    if not active_modules:
                        # 沒有活躍模組，增加等待時間
                        time.sleep(0.5)
                    else:
                        # 有活躍模組，短暫等待
                        time.sleep(0.2)
            
            # 檢查是否回到IDLE狀態，如果是則重新啟動STT監聽
            elif current_state == UEPState.IDLE and hasattr(self, '_previous_state'):
                if self._previous_state != UEPState.IDLE:
                    # ✅ 階段三：層級跳過邏輯 - 檢查是否應該跳過輸入層
                    from core.working_context import working_context_manager
                    should_skip = working_context_manager.should_skip_input_layer()
                    workflow_waiting = working_context_manager.is_workflow_waiting_input()
                    
                    # ✅ 檢查是否有活躍會話
                    from core.sessions.session_manager import unified_session_manager
                    active_ws = unified_session_manager.get_active_workflow_session_ids()
                    active_cs = unified_session_manager.get_active_chatting_session_ids()
                    has_active_session = bool(active_ws or active_cs)
                    
                    # 🔧 NEW: 檢查活躍工作流的下一步是否為處理步驟
                    next_step_is_processing = False
                    if active_ws:
                        next_step_is_processing = self._check_next_workflow_step_is_processing(active_ws)
                    
                    if should_skip and not workflow_waiting:
                        # 工作流自動推進中，跳過輸入層（不重啟 STT VAD）
                        skip_reason = working_context_manager.get_skip_reason() or "工作流自動推進"
                        debug_log(2, f"[SystemLoop] ⏭️ 跳過輸入層（不啟動 VAD）: {skip_reason}")
                        # 重置旗標，準備下次可能的輸入
                        working_context_manager.set_skip_input_layer(False)
                    elif next_step_is_processing:
                        # 🔧 下一步是處理步驟，跳過輸入層
                        # 步驟會在 WorkflowEngine 批准後自動執行，不需要手動觸發
                        debug_log(2, f"[SystemLoop] ⏭️ 下一步是處理步驟，跳過輸入層（等待自動執行）")
                    elif self.input_mode == "vad":
                        # ✅ VAD 模式下，無論是否有活躍會話都重啟 STT
                        # 理由：即使沒有會話，也需要持續監聽新的使用者輸入
                        debug_log(2, f"[SystemLoop] 系統回到IDLE狀態，重新啟動STT監聽 (VAD模式, 會話: {has_active_session})")
                        self._restart_stt_listening()
                    elif self.input_mode == "text":
                        # 文字模式：不重啟 VAD，等待手動輸入
                        if has_active_session:
                            debug_log(2, f"[SystemLoop] 系統回到IDLE狀態 (文字模式)，等待手動輸入")
                        else:
                            debug_log(2, f"[SystemLoop] 系統回到IDLE狀態 (文字模式)，無活躍會話，等待新輸入")
                    
                    # 系統循環結束，檢查 GS 結束條件
                    self._check_cycle_end_conditions()
                    
                    # 🔧 檢查是否有延遲的 STT 重啟請求
                    if hasattr(self, '_pending_stt_restart') and self._pending_stt_restart:
                        self._pending_stt_restart = False
                        debug_log(2, "[SystemLoop] 循環已結束，現在執行延遲的 STT 重啟")
                        self._restart_stt_listening()
            
            # 記錄前一個狀態
            self._previous_state = current_state
            
        except Exception as e:
            debug_log(1, f"[SystemLoop] 狀態監控錯誤: {e}")
    
    def _track_processing_cycle(self, current_state, queue_size):
        """追蹤完整處理週期：STT → NLP → Router → LLM/MEM → TTS"""
        from core.states.state_manager import UEPState
        
        # 檢測循環開始（STT接收到語音，狀態開始變化）
        if not self.cycle_tracking["input_received"] and queue_size > 0:
            self.cycle_tracking["input_received"] = True
            self.current_cycle_start_time = time.time()
            # 🔧 不再遞增 cycle_index - 改為在循環完成後遞增
            # cycle_index 從 0 開始，每完成一個完整循環後 +1
            self.processing_cycles = self.cycle_index  # 向後兼容
            # 更新全局循環資訊供模組使用
            self._update_global_cycle_info()
            debug_log(2, f"[SystemLoop] 處理循環 #{self.cycle_index} 開始：STT輸入層")
        
        # 檢測處理層活動（狀態轉換到CHAT或WORK）
        elif self.cycle_tracking["input_received"] and not self.cycle_tracking["processing_started"]:
            if current_state in [UEPState.CHAT, UEPState.WORK]:
                self.cycle_tracking["processing_started"] = True
                debug_log(2, f"[SystemLoop] 處理層活動：{current_state.value}")
        
        # 檢測是否需要等待輸出層（LLM處理完成後）
        elif (self.cycle_tracking["input_received"] and 
              self.cycle_tracking["processing_started"] and 
              not self.cycle_tracking["output_completed"]):
            
            # 檢查是否有TTS模組可用
            from core.framework import core_framework
            has_tts = 'tts' in core_framework.modules if hasattr(core_framework, 'modules') else False
            
            if not has_tts:
                # 沒有TTS模組，循環卡在等待輸出層
                if not hasattr(self, '_waiting_for_output'):
                    self._waiting_for_output = True
                    self._output_wait_start = time.time()
                    debug_log(1, f"[SystemLoop] 處理循環 #{self.processing_cycles + 1} 等待輸出層（TTS模組未載入）")
                
                # 定期報告等待狀態
                wait_time = time.time() - self._output_wait_start
                if wait_time > 0 and int(wait_time) % 5 == 0 and wait_time - int(wait_time) < 0.2:
                    debug_log(2, f"[SystemLoop] 循環 #{self.processing_cycles + 1} 等待輸出層已 {wait_time:.1f}秒")
            else:
                # 有TTS模組，檢查是否回到IDLE狀態（輸出完成）
                if current_state == UEPState.IDLE and queue_size == 0:
                    # 主循環檢測到輸出完成，發布事件並完成循環
                    self._complete_cycle(publish_event=True)
        
        # 更新最後狀態變化時間
        if hasattr(self, '_last_queue_size'):
            if queue_size != self._last_queue_size:
                self._last_queue_change_time = time.time()
        self._last_queue_size = queue_size
    
    def _complete_cycle(self, publish_event: bool = False):
        """
        完成一次處理循環
        
        Args:
            publish_event: 是否發布 CYCLE_COMPLETED 事件（由調用方決定，避免重複發布）
        """
        if self.current_cycle_start_time:
            cycle_time = time.time() - self.current_cycle_start_time
            debug_log(1, f"[SystemLoop] 處理循環 #{self.cycle_index} 完成，耗時 {cycle_time:.2f}秒")
        else:
            # 🔧 測試環境：沒有 cycle_start_time，但仍然需要遞增 cycle_index
            debug_log(1, f"[SystemLoop] 處理循環 #{self.cycle_index} 完成（測試環境）")
        
        # 🔧 由調用方決定是否發布事件，避免重複發布
        if publish_event:
            self._publish_cycle_completed()
        
        # ✅ 統一循環索引管理：發布 CYCLE_COMPLETED 後立即遞增
        # 所有層級使用同一個 cycle_index，不再有其他遞增邏輯
        debug_log(2, f"[SystemLoop] 循環 #{self.cycle_index} 完成，遞增 cycle_index")
        self.cycle_index += 1
        self.processing_cycles = self.cycle_index  # 向後兼容
        # ✅ 立即更新 global_context，讓下一個 cycle 的模組能讀到新值
        self._update_global_cycle_info()
        debug_log(2, f"[SystemLoop] 下一個循環索引: {self.cycle_index}")
        
        # ✅ 循環完成後，調用 Controller 檢查 CS/WS pending_end 和 GS 結束條件
        debug_log(2, "[SystemLoop] 調用 Controller 檢查會話結束條件")
        self._check_cycle_end_conditions()
        
        # 重置週期追蹤
        self.cycle_tracking = {
            "input_received": False,
            "processing_started": False,
            "output_completed": False
        }
        self.current_cycle_start_time = None
    
    def _check_active_modules(self) -> bool:
        """檢查是否有活躍的模組在處理"""
        try:
            from core.framework import core_framework
            
            # 簡單的啟發式方法：檢查是否有模組正在處理
            # 目前 framework 沒有 get_active_modules 方法，使用簡單邏輯
            # 如果有已初始化的模組，就認為可能有活躍處理
            if hasattr(core_framework, 'modules') and core_framework.modules:
                return len(core_framework.modules) > 0
            else:
                # 如果無法檢查，預設為有活躍模組
                return True
                
        except Exception as e:
            debug_log(1, f"[SystemLoop] 檢查活躍模組時發生錯誤: {e}")
            return True  # 出錯時保守處理
    
    def _log_system_status(self):
        """定期輸出系統運行狀態"""
        try:
            from core.framework import core_framework
            from core.states.state_manager import state_manager
            from core.states.state_queue import get_state_queue_manager
            
            # 運行時間統計
            uptime = time.time() - self.start_time
            uptime_str = f"{uptime:.1f}秒"
            if uptime > 60:
                uptime_str = f"{uptime/60:.1f}分鐘"
            if uptime > 3600:
                uptime_str = f"{uptime/3600:.1f}小時"
            
            # 基本狀態信息
            current_state = state_manager.get_current_state()
            state_queue = get_state_queue_manager()
            queue_size = len(state_queue.queue) if hasattr(state_queue, 'queue') else 0
            
            # 模組狀態
            active_modules = list(core_framework.modules.keys())
            module_count = len(active_modules)
            
            # 效能指標
            loops_per_min = (self.loop_count / uptime * 60) if uptime > 0 else 0
            cycles_per_min = (self.processing_cycles / uptime * 60) if uptime > 0 else 0
            
            # 輸出狀態報告
            info_log("=" * 60)
            info_log("📊 系統運行狀態報告")
            info_log(f"⏰ 運行時間: {uptime_str}")
            info_log(f"🔄 基本循環: {self.loop_count} 次 ({loops_per_min:.1f}/分鐘)")
            info_log(f"🎯 處理週期: {self.processing_cycles} 次 ({cycles_per_min:.1f}/分鐘)")
            info_log(f"🎯 當前狀態: {current_state.value}")
            info_log(f"📝 狀態佇列: {queue_size} 項目")
            info_log(f"🔧 活躍模組: {module_count} 個 {active_modules}")
            
            # 詳細模組狀態（framework 目前沒有此方法，略過）
            # if hasattr(core_framework, 'get_detailed_module_status'):
            #     module_details = core_framework.get_detailed_module_status()
            #     for module_name, status in module_details.items():
            #         status_emoji = "✅" if status.get('healthy', True) else "⚠️"
            #         info_log(f"   {status_emoji} {module_name}: {status.get('status', 'unknown')}")
            
            # Working Context身份狀態檢查
            try:
                from core.working_context import working_context_manager
                current_identity = working_context_manager.get_current_identity()
                if current_identity:
                    identity_id = current_identity.get('identity_id', 'unknown')
                    memory_token = current_identity.get('memory_token', 'none')
                    info_log(f"👤 當前身份: {identity_id}, 記憶令牌: {memory_token}")
                else:
                    info_log("👤 當前身份: 無")
            except Exception as e:
                debug_log(1, f"[SystemLoop] 身份狀態檢查錯誤: {e}")
            
            info_log("=" * 60)
            
        except Exception as e:
            debug_log(1, f"[SystemLoop] 狀態日誌輸出錯誤: {e}")
    
    def _collect_performance_snapshot(self):
        """蒐集系統效能快照"""
        try:
            from core.framework import core_framework
            
            # 調用 Framework 的效能快照功能
            snapshot = core_framework.collect_system_performance_snapshot()
            
            if snapshot:
                debug_log(2, f"[SystemLoop] 效能快照: {snapshot.active_modules} 活躍模組, "
                          f"成功率: {snapshot.system_success_rate:.2%}")
                
                # 記錄關鍵指標
                if snapshot.system_average_response_time > 2.0:  # 超過2秒警告
                    debug_log(1, f"[SystemLoop] ⚠️ 系統響應時間較慢: {snapshot.system_average_response_time:.2f}秒")
                
                if snapshot.system_success_rate < 0.95:  # 成功率低於95%警告
                    debug_log(1, f"[SystemLoop] ⚠️ 系統成功率較低: {snapshot.system_success_rate:.2%}")
            
        except Exception as e:
            debug_log(1, f"[SystemLoop] 效能快照蒐集錯誤: {e}")
    
    def get_status(self) -> Dict[str, Any]:
        """獲取循環狀態"""
        uptime = time.time() - self.start_time if self.start_time > 0 else 0
        
        return {
            "status": self.status.value,
            "loop_count": self.loop_count,
            "processing_cycles": self.processing_cycles,
            "uptime": uptime,
            "is_running": self.status == LoopStatus.RUNNING,
            "thread_alive": self.loop_thread.is_alive() if self.loop_thread else False
        }
    
    def pause(self) -> bool:
        """暫停系統循環"""
        if self.status == LoopStatus.RUNNING:
            self.status = LoopStatus.PAUSED
            info_log("⏸️ 系統循環已暫停")
            return True
        return False
    
    def resume(self) -> bool:
        """恢復系統循環"""
        if self.status == LoopStatus.PAUSED:
            self.status = LoopStatus.RUNNING
            info_log("▶️ 系統循環已恢復")
            return True
        return False

    def _check_cycle_end_conditions(self):
        """系統循環結束時檢查 GS 結束條件"""
        try:
            from core.controller import unified_controller
            
            # 調用 GS 結束條件檢查
            if hasattr(unified_controller, 'check_gs_end_conditions'):
                unified_controller.check_gs_end_conditions()
                
        except Exception as e:
            debug_log(2, f"[SystemLoop] 循環結束條件檢查失敗: {e}")
    
    def _check_workflow_needs_input(self, active_workflow_session_ids: list) -> bool:
        """
        🔧 檢查活躍工作流是否有互動步驟需要使用者輸入
        
        Args:
            active_workflow_session_ids: 活躍的工作流會話 ID 列表
            
        Returns:
            bool: 如果需要使用者輸入則返回 True
        """
        try:
            from core.framework import core_framework
            
            if 'sys' not in core_framework.modules:
                return False
            
            sys_module = core_framework.modules['sys'].module_instance
            
            for session_id in active_workflow_session_ids:
                if not hasattr(sys_module, 'workflow_engines'):
                    continue
                    
                engine = sys_module.workflow_engines.get(session_id)
                if not engine:
                    continue
                
                current_step = engine.get_current_step()
                if not current_step:
                    continue
                
                # 檢查步驟類型
                from modules.sys_module.workflows import WorkflowStep
                if current_step.step_type == WorkflowStep.STEP_TYPE_INTERACTIVE:
                    debug_log(2, f"[SystemLoop] 💬 工作流 {session_id} 的當前步驟需要輸入: {current_step.id}")
                    return True
                
            return False
            
        except Exception as e:
            debug_log(1, f"[SystemLoop] 檢查工作流輸入需求時出錯: {e}")
            return False
    
    def _display_workflow_prompt(self, active_workflow_session_ids: list):
        """
        🔧 在 text mode 下顯示工作流的輸入提示
        
        Args:
            active_workflow_session_ids: 活躍的工作流會話 ID 列表
        """
        try:
            from core.framework import core_framework
            
            if 'sys' not in core_framework.modules:
                return
            
            sys_module = core_framework.modules['sys'].module_instance
            
            for session_id in active_workflow_session_ids:
                if not hasattr(sys_module, 'workflow_engines'):
                    continue
                    
                engine = sys_module.workflow_engines.get(session_id)
                if not engine:
                    continue
                
                current_step = engine.get_current_step()
                if not current_step:
                    continue
                
                # 檢查步驟類型
                from modules.sys_module.workflows import WorkflowStep
                if current_step.step_type == WorkflowStep.STEP_TYPE_INTERACTIVE:
                    # 取得 prompt
                    prompt = current_step.get_prompt()
                    
                    # 顯示 prompt
                    info_log(f"[SystemLoop] 📝 工作流輸入提示: {prompt}")
                    print(f"\n💬 {prompt}")
                    print("⌨️  請輸入回覆（或說「你決定」讓 AI 自行選擇）：")
                    
                    # 設定上下文標記，讓下一次輸入會被路由到工作流
                    # （這個標記已經在 _on_workflow_requires_input 中設定了）
                    return
                    
        except Exception as e:
            debug_log(1, f"[SystemLoop] 顯示工作流提示時出錯: {e}")
    
    def _check_next_workflow_step_is_processing(self, active_workflow_session_ids: list) -> bool:
        """
        🔧 檢查活躍工作流的當前步驟是否為處理步驟（不需要用戶輸入）
        
        Args:
            active_workflow_session_ids: 活躍的工作流會話 ID 列表
            
        Returns:
            bool: 如果當前步驟是處理步驟則返回 True
        """
        try:
            from core.framework import core_framework
            
            # 獲取 SYS 模組
            if 'sys' not in core_framework.modules:
                return False
            
            sys_module = core_framework.modules['sys'].module_instance
            
            # 檢查每個活躍的工作流會話
            for session_id in active_workflow_session_ids:
                # 獲取工作流引擎
                if not hasattr(sys_module, 'workflow_engines'):
                    error_log(f"[SystemLoop] SYS 模組沒有 workflow_engines")
                    continue
                    
                engine = sys_module.workflow_engines.get(session_id)
                if not engine:
                    error_log(f"[SystemLoop] 找不到工作流引擎: {session_id}")
                    continue
                
                # 調試：打印當前步驟 ID 和所有可用步驟
                current_step_id = engine.session.get_data("current_step")
                all_steps = list(engine.definition.steps.keys()) if engine.definition else []
                info_log(f"[SystemLoop] 工作流 {session_id}: current_step_id='{current_step_id}', available_steps={all_steps}")
                
                # 獲取當前步驟
                current_step = engine.get_current_step()
                if not current_step:
                    error_log(f"[SystemLoop] 工作流 {session_id} 無當前步驟（current_step_id='{current_step_id}' 不在 steps 中）")
                    continue
                
                # 檢查步驟類型 - PROCESSING 或 SYSTEM 步驟都需要自動執行
                from modules.sys_module.workflows import WorkflowStep
                info_log(f"[SystemLoop] 檢查步驟 {current_step.id}, 類型: {current_step.step_type}")
                
                if current_step.step_type == WorkflowStep.STEP_TYPE_PROCESSING:
                    info_log(f"[SystemLoop] ✅ 工作流 {session_id} 的當前步驟是處理步驟: {current_step.id}")
                    return True
                elif current_step.step_type == WorkflowStep.STEP_TYPE_SYSTEM:
                    info_log(f"[SystemLoop] ✅ 工作流 {session_id} 的當前步驟是系統步驟: {current_step.id}")
                    return True
                elif current_step.step_type == WorkflowStep.STEP_TYPE_INTERACTIVE:
                    # 🔧 Interactive 步驟：檢查是否已經在等待輸入
                    # 如果已經在等待，不要重複調用 process_input(None)，避免發布重複事件
                    if engine.waiting_for_input:
                        debug_log(2, f"[SystemLoop] 💬 工作流 {session_id} Interactive 步驟 {current_step.id} 已在等待輸入")
                        return False  # Interactive 步驟不算 processing step

                    # 如果還沒有等待標記，觸發輸入請求
                    info_log(f"[SystemLoop] 💬 工作流 {session_id} 遇到 Interactive 步驟: {current_step.id}，觸發輸入請求")
                    try:
                        result = engine.process_input(None)
                        if result and result.data and result.data.get('requires_input'):
                            # 輸入請求已發布，WorkflowEngine 會通過事件通知我們
                            info_log(f"[SystemLoop] ✅ Interactive 步驟輸入請求已發布")
                        else:
                            error_log(f"[SystemLoop] ❌ Interactive 步驟未正確返回輸入請求")
                    except Exception as e:
                        error_log(f"[SystemLoop] ❌ 觸發 Interactive 步驟輸入請求失敗: {e}")
                    return False  # Interactive 步驟不算 processing step
                else:
                    debug_log(2, f"[SystemLoop] 步驟 {current_step.id} 是 {current_step.step_type} 類型，不自動執行")
                
            return False
            
        except Exception as e:
            debug_log(1, f"[SystemLoop] 檢查下一步驟類型時出錯: {e}")
            return False

    def handle_output_completion(self, output_data: Dict[str, Any]):
        """
        處理輸出層完成通知，完成整個三層流程
        在 VAD 模式下重新啟動 STT 監聽
        """
        try:
            info_log("[SystemLoop] 接收到輸出層完成通知，三層架構流程結束")
            debug_log(2, f"[SystemLoop] 輸出層結果: {list(output_data.keys())}")
            
            # 🔧 檢查當前 cycle 的所有 PROCESSING 是否都有對應的 OUTPUT
            # 使用真實的 GS ID 而不是事件中的 session_id（可能是測試用的假 ID）
            gs_id = self._get_current_gs_id()
            cycle_index = self.cycle_index  # 使用 SystemLoop 自己的 cycle_index
            cycle_key = f"{gs_id}:{cycle_index}"
            
            should_complete_cycle = False
            with self._cycle_tracking_lock:
                if cycle_key in self._cycle_layer_tracking:
                    counts = self._cycle_layer_tracking[cycle_key]
                    processing_count = counts["processing_count"]
                    output_count = counts["output_count"]
                    
                    # 🔧 CYCLE_COMPLETED 語義：該循環的所有處理和輸出都已完成
                    # 條件：processing_count > 0（有處理任務）且 output_count >= processing_count（所有處理都有輸出）
                    if processing_count > 0 and output_count >= processing_count:
                        # 所有 PROCESSING 都有對應的 OUTPUT 完成，循環結束
                        info_log(f"[SystemLoop] ✅ Cycle 所有輸出完成 (P={processing_count}, O={output_count})")
                        should_complete_cycle = True
                        # 清理追蹤記錄
                        del self._cycle_layer_tracking[cycle_key]
                    else:
                        # 還有 PROCESSING 沒有對應的 OUTPUT
                        info_log(f"[SystemLoop] ⏳ 還有輸出待完成 (P={processing_count}, O={output_count})")
                else:
                    # 沒有追蹤記錄，可能是：
                    # 1. 直接調用 TTS（沒有經過 PROCESSING_LAYER_COMPLETE）
                    # 2. 無響應的處理（PROCESSING 沒有 response，不啟動 TTS）
                    # 這種情況下不應該發布 CYCLE_COMPLETED，讓主循環的 _track_processing_cycle 處理
                    debug_log(2, f"[SystemLoop] 無追蹤記錄，不發布 CYCLE_COMPLETED (cycle={cycle_key})")
                    should_complete_cycle = False
            
            if should_complete_cycle:
                # 🔧 所有輸出任務完成，發布 CYCLE_COMPLETED 事件並完成循環
                # 這確保即使主循環未運行（如測試環境），也能觸發去重清理和會話結束檢查
                self._complete_cycle(publish_event=True)
            else:
                # 🔧 還有輸出待完成，不遞增 cycle_index，不發布事件
                # 只有當所有 PROCESSING 都有對應的 OUTPUT 時才真正完成循環
                pass
            
            # 🔧 在 WORK 狀態中，預設跳過輸入層，除非有互動步驟需要輸入
            from core.sessions.session_manager import unified_session_manager
            from core.states.state_manager import state_manager, UEPState
            
            active_ws = unified_session_manager.get_active_workflow_session_ids()
            current_state = state_manager.get_current_state()
            
            # 檢查工作流是否有互動步驟需要輸入
            needs_user_input = False
            
            if active_ws:
                debug_log(2, f"[SystemLoop] 檢查活躍工作流: {active_ws}")
                needs_user_input = self._check_workflow_needs_input(active_ws)
                debug_log(2, f"[SystemLoop] 檢查結果: needs_input={needs_user_input}")
            
            # ✅ 決策是否啟動輸入層（檢查是否已經在等待輸入，避免重複啟動）
            from core.working_context import working_context_manager
            already_waiting = working_context_manager.is_workflow_waiting_input()
            
            if needs_user_input:
                if already_waiting:
                    # 已經在等待輸入，不要重複啟動
                    debug_log(2, "[SystemLoop] 工作流已在等待輸入，跳過重複啟動")
                else:
                    # 需要使用者輸入，啟動輸入層
                    info_log(f"[SystemLoop] 💬 工作流需要使用者輸入，啟動輸入層")
                    if self.input_mode == "vad":
                        self._restart_stt_listening()
                    else:
                        # Text mode: 顯示工作流的 prompt
                        self._display_workflow_prompt(active_ws)
            elif current_state == UEPState.WORK:
                # WORK 狀態且沒有互動步驟，跳過輸入層
                debug_log(2, "[SystemLoop] WORK 狀態，無互動步驟，跳過輸入層")
            elif self.input_mode == "vad":
                # 非 WORK 狀態（CHAT/IDLE）且為 VAD 模式，啟動輸入層
                debug_log(2, "[SystemLoop] VAD 模式：重新啟動 STT 語音監聽")
                self._restart_stt_listening()
            
            # 檢查 GS 結束條件
            self._check_cycle_end_conditions()
            
        except Exception as e:
            error_log(f"[SystemLoop] 處理輸出層完成通知失敗: {e}")
    
    def _reload_from_user_settings(self, key_path: str, value: Any):
        """處理 user_settings 熱重載"""
        try:
            if key_path == "advanced.performance.gc_interval":
                self.gc_interval = value
                info_log(f"[SystemLoop] GC 間隔已更新: {value}秒")
            elif key_path == "interaction.proactivity.allow_system_initiative":
                self.allow_system_initiative = value
                info_log(f"[SystemLoop] 系統主動性已更新: {value}")
            elif key_path == "interaction.proactivity.initiative_cooldown":
                self.initiative_cooldown = value
                info_log(f"[SystemLoop] 主動觸發冷卻時間已更新: {value}秒")
            elif key_path == "interaction.proactivity.require_user_input":
                self.require_user_input = value
                info_log(f"[SystemLoop] 需要使用者輸入設定已更新: {value}")
            elif key_path == "interaction.speech_input.enabled":
                # 更新輸入模式: True=VAD, False=文字輸入
                old_mode = self.input_mode
                new_mode = "vad" if value else "text"
                
                if old_mode != new_mode:
                    info_log(f"[SystemLoop] 輸入模式切換: {old_mode} → {new_mode}")
                    
                    # 立即切換輸入處理（不需要重啟整個循環）
                    if self._is_running:
                        info_log("[SystemLoop] 正在切換輸入處理...")
                        
                        # 1. 標記舊模式為非活躍（停止接收新輸入）
                        if old_mode == "vad":
                            self._stt_listening_active = False
                            debug_log(2, "[SystemLoop] 已停止 STT 監聽")
                        else:
                            self._text_input_active = False
                            debug_log(2, "[SystemLoop] 已停止文字輸入")
                        
                        # 2. 切換模式
                        self.input_mode = new_mode
                        
                        # 3. 啟動新模式的輸入處理
                        if new_mode == "vad":
                            self._start_stt_listening()
                        else:
                            self._start_text_input()
                        
                        info_log(f"✅ [SystemLoop] 輸入處理已切換至 {new_mode} 模式")
                    else:
                        # 系統未運行，只更新模式
                        self.input_mode = new_mode
                        info_log(f"[SystemLoop] 輸入模式已更新（將在下次啟動時生效）")
        except Exception as e:
            error_log(f"[SystemLoop] 熱重載設定失敗: {e}")


# 全局系統循環實例
system_loop = SystemLoop()