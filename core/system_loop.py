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
        self.text_input_prompt = self.config.get("system", {}).get("input_mode", {}).get("text_input_prompt", ">>> ")
        
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
    
    def _on_output_layer_complete(self, event):
        """
        輸出層完成事件處理器
        當 TTS 發布 OUTPUT_LAYER_COMPLETE 事件時觸發
        """
        try:
            debug_log(2, f"[SystemLoop] 收到輸出層完成事件: {event.event_id}")
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
            
            # 清除跳過輸入層旗標，允許輸入層執行
            working_context_manager.set_skip_input_layer(False, reason="workflow_input")
            
            info_log("[SystemLoop] 💬 工作流等待使用者輸入，輸入層已啟用")
            
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
            
            # 重置工作流等待輸入旗標
            working_context_manager.set_workflow_waiting_input(False)
            
            # 設置跳過輸入層旗標（下一循環跳過）
            working_context_manager.set_skip_input_layer(True, reason="workflow_processing")
            
            debug_log(2, "[SystemLoop] 工作流輸入完成，下一循環將跳過輸入層")
            
        except Exception as e:
            error_log(f"[SystemLoop] 處理工作流輸入完成事件失敗: {e}")
    
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
                            # 等待用戶輸入
                            user_input = input(self.text_input_prompt)
                            
                            # 過濾空輸入
                            if not user_input.strip():
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
            
            info_log("✅ 文字輸入循環已啟動")
            return True
            
        except Exception as e:
            error_log(f"❌ 啟動文字輸入失敗: {e}")
            return False
    
    def _start_stt_listening(self):
        """啟動STT持續監聽"""
        try:
            from core.framework import core_framework
            
            # 獲取STT模組
            stt_module = core_framework.get_module('stt')
            if not stt_module:
                error_log("❌ 無法獲取STT模組")
                return False
            
            info_log("🎤 啟動STT持續監聽...")
            
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
            
            info_log("✅ STT持續監聽已啟動")
            return True
            
        except Exception as e:
            error_log(f"❌ 啟動STT監聽失敗: {e}")
            return False
    
    def _restart_stt_listening(self):
        """重新啟動STT監聽"""
        try:
            from core.framework import core_framework
            
            # 獲取STT模組並恢復監聽能力
            stt_module = core_framework.get_module('stt')
            if stt_module:
                stt_module.resume_listening()
                info_log("🔄 重新啟動STT監聽")
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
            
            current_state = state_manager.get_current_state()
            state_queue = get_state_queue_manager()
            queue_size = len(state_queue.queue) if hasattr(state_queue, 'queue') else 0
            
            # 階段三：檢查層級跳過旗標（在循環開始前檢查並重置）
            should_skip = working_context_manager.should_skip_input_layer()
            is_workflow_waiting = working_context_manager.is_workflow_waiting_input()
            
            if should_skip and not is_workflow_waiting:
                skip_reason = working_context_manager.get_skip_reason()
                debug_log(2, f"[SystemLoop] 跳過輸入層 (原因: {skip_reason})")
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
                    # 階段三：循環結束後重置跳過旗標
                    if should_skip:
                        working_context_manager.set_skip_input_layer(False)
                        debug_log(3, "[SystemLoop] 循環結束，重置輸入層跳過旗標")
                    
                    debug_log(2, f"[SystemLoop] 系統回到IDLE狀態，重新啟動STT監聽")
                    self._restart_stt_listening()
                    
                    # 系統循環結束，檢查 GS 結束條件
                    self._check_cycle_end_conditions()
            
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
            # 遞增 cycle_index,開始新循環
            self.cycle_index += 1
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
                    self._complete_cycle()
        
        # 更新最後狀態變化時間
        if hasattr(self, '_last_queue_size'):
            if queue_size != self._last_queue_size:
                self._last_queue_change_time = time.time()
        self._last_queue_size = queue_size
    
    def _complete_cycle(self):
        """完成一次處理循環"""
        if self.current_cycle_start_time:
            cycle_time = time.time() - self.current_cycle_start_time
            
            debug_log(1, f"[SystemLoop] 處理循環 #{self.cycle_index} 完成，耗時 {cycle_time:.2f}秒")
            
            # 發布 CYCLE_COMPLETED 事件用於清理去重鍵
            self._publish_cycle_completed()
            
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

    def handle_output_completion(self, output_data: Dict[str, Any]):
        """
        處理輸出層完成通知，完成整個三層流程
        在 VAD 模式下重新啟動 STT 監聽
        """
        try:
            info_log("[SystemLoop] 接收到輸出層完成通知，三層架構流程結束")
            debug_log(2, f"[SystemLoop] 輸出層結果: {list(output_data.keys())}")
            
            # 記錄完整流程完成
            self._complete_cycle()
            
            # ✅ 在 VAD 模式下重新啟動 STT 監聽
            if self.input_mode == "vad":
                debug_log(2, "[SystemLoop] VAD 模式：重新啟動 STT 語音監聽")
                self._restart_stt_listening()
            
            # 檢查 GS 結束條件
            self._check_cycle_end_conditions()
            
        except Exception as e:
            error_log(f"[SystemLoop] 處理輸出層完成通知失敗: {e}")


# 全局系統循環實例
system_loop = SystemLoop()