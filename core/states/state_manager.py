# core/state_manager.py
from enum import Enum, auto
from typing import Dict, Any, Optional, List, Callable
import time
from core.status_manager import status_manager
from utils.debug_helper import debug_log, info_log, error_log
from core.working_context import ContextType
from core.sessions.workflow_session import WSTaskType

class UEPState(Enum):
    IDLE      = "idle"  # 閒置
    CHAT      = "chat"  # 聊天
    WORK      = "work"  # 工作（執行指令，包含單步和多步驟工作流程）
    MISCHIEF  = "mischief"  # 搗蛋（暫略）
    SLEEP     = "sleep"  # 睡眠（暫略）
    ERROR     = "error"  # 錯誤

class StateManager:
    """
    管理 U.E.P 各種狀態。
    接受事件，並在需要時切換 state。
    負責根據狀態變化創建對應的會話。
    
    架構原則：
    - 狀態創建會話（State → Session）
    - 會話結束觸發狀態轉換（Session End → State Transition）
    - 狀態和會話是一體的，生命週期綁定
    """

    def __init__(self):
        self._state = UEPState.IDLE
        self._current_session_id: Optional[str] = None
        self._state_change_callbacks: List[Callable[[UEPState, UEPState], None]] = []
        self.status_manager = status_manager
        # 與 StatusManager 整合
        self._setup_status_integration()
        # 訂閱會話結束事件
        self._subscribe_to_session_events()
        
    def get_state(self) -> UEPState:
        return self._state
    
    def get_current_state(self) -> UEPState:
        """獲取當前狀態（與 get_state 相同，提供兼容性）"""
        return self._state

    def set_state(self, new_state: UEPState, context: Optional[Dict[str, Any]] = None) -> bool:
        """
        設置新狀態，並觸發狀態變化處理
        
        Args:
            new_state: 新狀態
            context: 狀態變化上下文 (包含創建會話所需的資訊)
            
        Returns:
            bool: 狀態轉換是否成功
        """
        old_state = self._state
        
        # ✅ 即使狀態相同，如果有 context 也要觸發狀態處理
        # 這允許在 WORK -> WORK 轉換時創建新的 WS
        if old_state == new_state and context is None:
            return True  # 狀態沒有變化且沒有新上下文，視為成功
            
        try:
            self._state = new_state
            
            if old_state != new_state:
                debug_log(2, f"[StateManager] 狀態變更: {old_state.name} -> {new_state.name}")
            else:
                debug_log(2, f"[StateManager] 重新進入 {new_state.name} 狀態（創建新會話）")
            
            # 觸發狀態變化回調
            self._on_state_changed(old_state, new_state, context)
            
            # 通知所有回調
            for callback in self._state_change_callbacks:
                try:
                    callback(old_state, new_state)
                except Exception as e:
                    debug_log(1, f"[StateManager] 狀態變化回調執行失敗: {e}")
            
            return True  # 狀態轉換成功
            
        except RuntimeError as e:
            # 架構錯誤，狀態轉換失敗
            debug_log(1, f"[StateManager] 狀態轉換失敗，架構錯誤: {e}")
            # 回滾狀態
            self._state = old_state
            return False
            
        except Exception as e:
            # 其他錯誤，不影響狀態轉換的核心成功性
            debug_log(1, f"[StateManager] 狀態轉換期間發生非關鍵錯誤: {e}")
            # 狀態轉換本身是成功的，只是附加操作（如記憶體存取）失敗
            return True
    
    def add_state_change_callback(self, callback: Callable[[UEPState, UEPState], None]):
        """添加狀態變化回調"""
        self._state_change_callbacks.append(callback)
        
    def get_current_session_id(self) -> Optional[str]:
        """獲取當前會話ID"""
        return self._current_session_id
    
    def _record_state_visit(self, state: UEPState):
        """記錄當前 GS 訪問的狀態"""
        try:
            from core.sessions.session_manager import session_manager
            current_gs = session_manager.get_current_general_session()
            if current_gs:
                current_gs.record_state_visit(state.value)
        except Exception as e:
            debug_log(3, f"[StateManager] 記錄狀態訪問失敗: {e}")
        
    def _on_state_changed(self, old_state: UEPState, new_state: UEPState, context: Optional[Dict[str, Any]] = None):
        """
        處理狀態變化，創建對應的會話
        
        Args:
            old_state: 舊狀態
            new_state: 新狀態
            context: 狀態變化上下文
        """
        try:
            # 🔧 GS 生命週期管理 - 標記階段到實際創建階段
            # 設計: 當系統從 IDLE 進入非 IDLE 狀態時，檢查是否有待機的 GS 標記
            # 若有，則在此時創建實際 GS（意味著 NLP 已驗證 CALL 意圖）
            if old_state == UEPState.IDLE and new_state != UEPState.IDLE:
                # 系統正在進入非 IDLE 狀態（意味著 NLP 驗證通過）
                try:
                    from core.controller import unified_controller
                    if unified_controller and hasattr(unified_controller, '_pending_gs') and unified_controller._pending_gs:
                        # 有待機的 GS，現在創建實際 GS
                        pending_data = unified_controller._pending_gs_data or {}
                        gs_trigger_event = {
                            "user_input": pending_data.get("user_input", ""),
                            "input_type": pending_data.get("input_type", "text"),
                            "timestamp": pending_data.get("timestamp", time.time())
                        }
                        
                        # 創建實際 GS
                        from core.sessions.session_manager import session_manager
                        from core.working_context import working_context_manager
                        
                        current_gs_id = session_manager.start_general_session(
                            pending_data.get("input_type", "text") + "_input", 
                            gs_trigger_event
                        )
                        
                        if current_gs_id:
                            unified_controller.total_gs_sessions += 1
                            # 設置到全局上下文
                            working_context_manager.global_context_data['current_gs_id'] = current_gs_id
                            working_context_manager.global_context_data['current_cycle_index'] = 0
                            debug_log(2, f"[StateManager] 🔄 GS 已由待機標記創建: {current_gs_id} (輸入類型: {pending_data.get('input_type')})")
                            info_log(f"[StateManager] GS 從待機標記轉為實際 GS: {current_gs_id}")
                        
                        # 清除待機標記
                        unified_controller._pending_gs = False
                        unified_controller._pending_gs_data = None
                        
                except Exception as e:
                    debug_log(1, f"[StateManager] GS 創建失敗（從待機標記）: {e}")
                    # 清除標記即使創建失敗
                    try:
                        from core.controller import unified_controller
                        if unified_controller:
                            unified_controller._pending_gs = False
                            unified_controller._pending_gs_data = None
                    except:
                        pass
            
            # 發布 STATE_CHANGED 事件給前端模組
            from core.event_bus import event_bus, SystemEvent
            event_bus.publish(
                SystemEvent.STATE_CHANGED,
                data={
                    "old_state": old_state,
                    "new_state": new_state
                },
                source="state_manager"
            )
            debug_log(2, f"[StateManager] 已發布 STATE_CHANGED 事件: {old_state.name} → {new_state.name}")
            
            # 記錄狀態訪問到當前 GS
            self._record_state_visit(new_state)
            
            # 根據新狀態創建對應的會話或執行特殊處理
            if new_state == UEPState.CHAT:
                self._create_chat_session(context)
            elif new_state == UEPState.WORK:
                self._create_work_session(context)
            elif new_state == UEPState.IDLE:
                self._cleanup_sessions()
            elif new_state == UEPState.MISCHIEF:
                self._handle_mischief_state(context)
            elif new_state == UEPState.SLEEP:
                self._handle_sleep_state(context)
                
        except RuntimeError as e:
            # 對於架構錯誤，直接向上拋出，不進行處理
            debug_log(1, f"[StateManager] 會話架構錯誤: {e}")
            raise
        except Exception as e:
            # 其他錯誤（如記憶體存取失敗）不應影響狀態轉換的核心成功性
            debug_log(1, f"[StateManager] 狀態變化處理中的非關鍵錯誤: {e}")
            # 記錄錯誤但不拋出，讓狀態轉換繼續進行
    
    def _create_chat_session(self, context: Optional[Dict[str, Any]] = None):
        """創建聊天會話 - 使用現有的GS，或恢復之前的CS"""
        try:
            from core.sessions.session_manager import session_manager, unified_session_manager
            from core.working_context import working_context_manager
            
            queue_callback = (context or {}).get("state_queue_callback")
            
            # 🆕 檢查是否為 resume 模式
            is_resume = (context or {}).get("is_resume", False)
            resume_context = (context or {}).get("resume_context")
            
            if is_resume and resume_context:
                # 🆕 Resume 模式：使用保存的上下文重新創建 CS
                debug_log(2, f"[StateManager] Resume 模式：恢復對話會話")
                debug_log(3, f"[StateManager] Resume context: session_id={resume_context.get('session_id')}, "
                             f"turns={resume_context.get('turn_counter')}")
                
                # 使用保存的身份上下文
                identity_context = resume_context.get("identity_context", {
                    "user_id": "default_user",
                    "personality": "default",
                    "preferences": {}
                })
            else:
                # 正常模式：從 Working Context 獲取身份信息
                current_identity = working_context_manager.get_current_identity()
                if current_identity:
                    identity_context = {
                        "user_id": current_identity.get("user_identity", current_identity.get("identity_id", "default_user")),
                        "personality": current_identity.get("personality_profile", "default"),
                        "preferences": current_identity.get("conversation_preferences", {})
                    }
                    debug_log(2, f"[StateManager] 使用Working Context身份: {identity_context}")
                else:
                    # 如果沒有身份信息，使用默認值
                    identity_context = {
                        "user_id": "default_user",
                        "personality": "default",
                        "preferences": {}
                    }
                    debug_log(2, f"[StateManager] 使用默認身份: {identity_context}")
            
            # ✅ 確保 GS 存在（由 Controller 管理）
            self._ensure_gs_exists()
            
            # 獲取現有的 General Session - 如果不存在則為架構錯誤
            current_gs = session_manager.get_current_general_session()
            if not current_gs:
                error_msg = "[StateManager] 嚴重錯誤：嘗試創建 CS 但沒有活躍的 GS！這違反了會話架構設計"
                debug_log(1, error_msg)
                raise RuntimeError("會話架構錯誤：CS 必須依附於現有的 GS，不能獨立創建")
            
            gs_id = current_gs.session_id
            debug_log(2, f"[StateManager] 使用現有 GS: {gs_id}")
            
            # 創建 Chatting Session，依附於現有的GS
            cs_id = session_manager.create_chatting_session(
                gs_session_id=gs_id,
                identity_context=identity_context
            )
            
            if cs_id:
                self._current_session_id = cs_id
                
                # 🆕 如果是 resume 模式，將 resume_context 保存到 working_context
                if is_resume and resume_context:
                    working_context_manager.set_resume_context(resume_context)
                    debug_log(2, f"[StateManager] Resume CS 成功: {cs_id}，已保存 resume_context")
                else:
                    debug_log(2, f"[StateManager] 創建聊天會話成功: {cs_id}")
                
                # ✅ 不在創建時呼叫 callback，等待 session_ended 事件
                # StateQueue 會通過 _on_session_ended 收到完成通知
                debug_log(2, "[StateManager] CS 已創建，等待聊天會話完成...")
            else:
                debug_log(1, "[StateManager] 創建聊天會話失敗")
                # ❌ 創建失敗時才呼叫 callback 報告錯誤
                if callable(queue_callback):
                    queue_callback(None, False, {"error": "Failed to create chat session"})
                
        except RuntimeError as e:
            # 對於架構錯誤，直接向上拋出
            debug_log(1, f"[StateManager] 會話架構錯誤: {e}")
            raise
        except Exception as e:
            debug_log(1, f"[StateManager] 創建聊天會話時發生錯誤: {e}")
    
    def _create_work_session(self, context: Optional[Dict[str, Any]] = None):
        """創建工作會話 - 使用現有的GS"""
        try:
            from core.sessions.session_manager import session_manager
            
            queue_callback = (context or {}).get("state_queue_callback")
            
            # 從上下文獲取工作流程信息
            workflow_type = None if context is None else context.get("workflow_type", "workflow_automation")
            command_text = "unknown command"
            is_system_report = (context or {}).get("system_report", False)
            
            if context:
                # ✅ 從 NLP 分段提取的對應狀態文本
                command_text = context.get("text", context.get("command", command_text))
            
            # ✅ 確保 GS 存在（由 Controller 管理）
            self._ensure_gs_exists()
            
            # 檢查是否為系統匯報模式（不需要工作流引擎，但仍需要 WS）
            if is_system_report:
                info_log(f"[StateManager] WORK 狀態（系統匯報模式）：創建 WS 但不啟動工作流引擎")
                debug_log(3, f"[StateManager] 系統匯報內容: {command_text[:100]}...")
                
                # 使用枚舉來標記這是系統通知
                workflow_type = WSTaskType.SYSTEM_NOTIFICATION.value
            
            # 獲取現有的 General Session - 如果不存在則為架構錯誤
            current_gs = session_manager.get_current_general_session()
            if not current_gs:
                error_msg = "[StateManager] 嚴重錯誤：嘗試創建 WS 但沒有活躍的 GS！這違反了會話架構設計"
                debug_log(1, error_msg)
                raise RuntimeError("會話架構錯誤：WS 必須依附於現有的 GS，不能獨立創建")
            
            gs_id = current_gs.session_id
            debug_log(2, f"[StateManager] 使用現有 GS: {gs_id}")
            
            # 創建 Workflow Session，依附於現有的GS
            ws_id = session_manager.create_workflow_session(
                gs_session_id=gs_id,
                task_type=workflow_type,
                task_definition={
                    "command": command_text,  # ✅ 來自 NLP 分段的 WORK 意圖文本
                    "initial_data": context or {}
                }
            )
            
            if ws_id:
                self._current_session_id = ws_id
                debug_log(2, f"[StateManager] 創建工作會話成功: {ws_id} (類型: {workflow_type})")
                
                if workflow_type == WSTaskType.SYSTEM_NOTIFICATION.value:
                    # 系統通知：直接觸發處理層
                    info_log(f"[StateManager] 系統通知 WS 已創建，直接觸發處理層")
                    self._trigger_work_processing(command_text, context, is_system_report=True)
                    # 系統通知的 WS 在處理完成後會自動結束，不需要等待工作流
                else:
                    # 等待 STATE_ADVANCED 事件（佇列推進）或 INPUT_LAYER_COMPLETE 事件（用戶輸入）
                    debug_log(2, "[StateManager] WS 已創建，等待事件觸發處理層...")
            else:
                debug_log(1, "[StateManager] 創建工作會話失敗")
                # ❌ 創建失敗時才呼叫 callback 報告錯誤
                if callable(queue_callback):
                    queue_callback(None, False, {"error": "Failed to create workflow session"})
                
        except RuntimeError as e:
            # 對於架構錯誤，直接向上拋出
            debug_log(1, f"[StateManager] 會話架構錯誤: {e}")
            raise
        except Exception as e:
            debug_log(1, f"[StateManager] 創建工作會話時發生錯誤: {e}")
    
    def _trigger_work_processing(self, content: str, context: Dict[str, Any], is_system_report: bool = True):
        """直接觸發系統報告的處理層處理（跳過輸入層）
        
        系統報告不需要經過輸入層（STT/NLP），直接構建處理層輸入並調用。
        注意：此方法僅用於 system_report 模式，不用於一般 WORK 狀態推進。
        
        Args:
            content: 報告內容文本
            context: 狀態上下文
            is_system_report: 必須為 True，僅支持系統報告模式
        """
        if not is_system_report:
            error_log("[StateManager] ❌ _trigger_work_processing 僅支持 system_report 模式")
            return
            
        try:
            from core.module_coordinator import module_coordinator, ProcessingLayer
            
            info_log("[StateManager] 🚀 系統報告：直接觸發處理層")
            
            # 構建處理層輸入（模擬輸入層完成的格式）
            processing_input = {
                "text": content,
                "system_report": True,  # 標記為系統報告
                "system_initiated": True,
                "notification_type": context.get("notification_type", "unknown"),
                "metadata": context,
                "cycle_index": 0,
                # 模擬 NLP 結果（系統報告不需要意圖分析）
                "nlp_result": {
                    "primary_intent": "work",  # 系統報告視為 WORK 路徑
                    "overall_confidence": 1.0,
                    "segments": []
                }
            }
            
            # 直接調用 ModuleCoordinator 的處理層處理
            # 注意：使用 INPUT 層完成來觸發處理層轉換
            success = module_coordinator.handle_layer_completion(
                layer=ProcessingLayer.INPUT,  # 模擬輸入層完成
                completion_data=processing_input  # 參數名稱是 completion_data
            )
            
            if success:
                info_log("[StateManager] ✅ 系統報告處理層已觸發")
            else:
                error_log("[StateManager] ❌ 系統報告處理層觸發失敗")
                
        except Exception as e:
            error_log(f"[StateManager] 觸發系統報告處理層失敗: {e}")
            import traceback
            error_log(traceback.format_exc())
    
    def _ensure_gs_exists(self):
        """確保 GS 存在，如果不存在則通知 Controller 創建"""
        from utils.debug_helper import debug_log, error_log
        
        try:
            from core.sessions.session_manager import session_manager
            
            current_gs = session_manager.get_current_general_session()
            if not current_gs:
                debug_log(2, "[StateManager] 檢測到沒有活躍的 GS，通知 Controller 創建")
                # 通過 Controller 單例同步創建 GS
                try:
                    from core.controller import unified_controller
                    unified_controller._create_gs_for_processing()
                    debug_log(2, "[StateManager] GS 創建請求已完成")
                except Exception as e:
                    error_log(f"[StateManager] 通知 Controller 創建 GS 失敗: {e}")
        except Exception as e:
            error_log(f"[StateManager] 檢查 GS 存在性失敗: {e}")
    
    def _cleanup_sessions(self):
        """清理會話 (當回到IDLE狀態時)"""
        # 這裡可以添加會話清理邏輯
        # 目前只是清除當前會話ID引用
        self._current_session_id = None
        debug_log(3, "[StateManager] 清理會話引用")
    
    def _handle_mischief_state(self, context: Optional[Dict[str, Any]] = None):
        """
        處理 Mischief 狀態 - 搗蛋狀態
        
        特點：
        - 不創建會話
        - 系統進入自主活動模式
        - 由 Mood 和其他數值觸發
        - 會影響 Helpfulness (設為 -1)
        """
        try:
            debug_log(1, "[StateManager] 進入 Mischief 狀態 - 系統將進行自主活動")
            
            # 取消當前會話（Mischief 不需要會話）
            self._cleanup_sessions()
            
            # 更新系統數值 - Mischief 狀態時 Helpfulness 為負值
            self._update_status_for_mischief()
            
            # 觸發 Mischief 狀態的特殊行為
            self._trigger_mischief_behaviors(context)
            
        except Exception as e:
            debug_log(1, f"[StateManager] 處理 Mischief 狀態失敗: {e}")
    
    def _handle_sleep_state(self, context: Optional[Dict[str, Any]] = None):
        """
        處理 Sleep 狀態 - 休眠狀態
        
        特點：
        - 不創建會話
        - 系統資源釋放
        - 由 Boredom 數值觸發
        - 降低系統活動度
        """
        try:
            debug_log(1, "[StateManager] 進入 Sleep 狀態 - 系統準備休眠")
            
            # 取消當前會話（Sleep 不需要會話）
            self._cleanup_sessions()
            
            # 使用 SleepManager 進入休眠
            from core.states.sleep_manager import sleep_manager
            
            # 準備休眠上下文
            sleep_context = {
                "previous_state": self._state.value,
                "trigger_reason": context.get("trigger_reason", "system_idle") if context else "system_idle",
                "boredom_level": context.get("boredom_level", 0.0) if context else 0.0,
                "inactive_duration": context.get("inactive_duration", 0.0) if context else 0.0
            }
            
            # 進入休眠
            success = sleep_manager.enter_sleep(sleep_context)
            
            if success:
                debug_log(1, "[StateManager] ✅ 系統已成功進入休眠狀態")
            else:
                debug_log(1, "[StateManager] ❌ 進入休眠狀態失敗")
            
        except Exception as e:
            debug_log(1, f"[StateManager] 處理 Sleep 狀態失敗: {e}")
        
    def sync_with_sessions(self):
        """
        與會話管理器同步狀態
        
        檢查活躍的會話並設置對應的系統狀態：
        - 有活躍的工作會話 -> WORK
        - 有活躍的聊天會話 -> CHAT  
        - 沒有活躍會話 -> IDLE
        """
        try:
            # 延遲導入避免循環依賴
            from core.sessions.session_manager import session_manager
            
            # 獲取所有活躍會話並分類
            all_active_sessions = session_manager.get_all_active_sessions()
            
            active_work_sessions = all_active_sessions.get('workflow', [])
            active_chat_sessions = all_active_sessions.get('chatting', [])
            
            if active_work_sessions:
                # 有活躍的工作會話
                if self._state != UEPState.WORK:
                    debug_log(2, f"[StateManager] 同步狀態為 WORK (活躍會話數: {len(active_work_sessions)})")
                    self._state = UEPState.WORK
            elif active_chat_sessions:
                # 有活躍的聊天會話
                if self._state != UEPState.CHAT:
                    debug_log(2, f"[StateManager] 同步狀態為 CHAT (活躍會話數: {len(active_chat_sessions)})")
                    self._state = UEPState.CHAT
            else:
                # 沒有活躍會話
                if self._state != UEPState.IDLE:
                    debug_log(2, "[StateManager] 同步狀態為 IDLE")
                    self._state = UEPState.IDLE
                    
        except ImportError as e:
            debug_log(2, f"[StateManager] 無法同步會話狀態: {e}")
        except Exception as e:
            debug_log(2, f"[StateManager] 同步會話狀態時發生錯誤: {e}")
    
    def check_special_state_conditions(self):
        """
        檢查是否需要切換到特殊狀態 (Mischief/Sleep)
        
        觸發條件：
        - Mischief: 高 Boredom + 負面 Mood 或極端 Pride
        - Sleep: 極高 Boredom + 長時間無互動
        """
        try:
            status_manager = self.status_manager
            status = status_manager.get_status()
            current_time = time.time()
            
            # 檢查 Sleep 狀態條件（優先級較高）
            # TODO: SLEEP 狀態暫時禁用自動觸發，等待前後端整合時正式實作
            # 將在前後端整合階段實現完整的資源釋放和喚醒機制
            sleep_enabled = False  # 設為 True 以啟用 SLEEP 狀態
            
            if sleep_enabled:
                time_since_interaction = current_time - status.last_interaction_time
                sleep_threshold = 1800  # 30分鐘無互動
                
                if (status.boredom >= 0.8 and 
                    time_since_interaction > sleep_threshold and 
                    self._state in [UEPState.IDLE]):
                    
                    debug_log(2, f"[StateManager] Sleep 條件滿足: Boredom={status.boredom:.2f}, "
                             f"無互動時間={time_since_interaction/60:.1f}分鐘")
                    self.set_state(UEPState.SLEEP, {
                        "trigger_reason": "high_boredom_and_inactivity",
                        "boredom_level": status.boredom,
                        "inactive_duration": time_since_interaction
                    })
                    return True
            
            # 檢查 Mischief 狀態條件
            # TODO: MISCHIEF 狀態尚未完全實作，暫時禁用自動觸發
            # 避免干擾正常的 CHAT 和 WORK 流程
            mischief_enabled = False  # 設為 True 以啟用 MISCHIEF 狀態
            
            if mischief_enabled:
                mischief_conditions = [
                    # 條件1: 高無聊 + 負面情緒
                    (status.boredom >= 0.6 and status.mood <= -0.3),
                    # 條件2: 極端自尊（過高或過低）+ 中等無聊
                    (abs(status.pride) >= 0.7 and status.boredom >= 0.4),
                    # 條件3: 低助人意願 + 負面情緒
                    (status.helpfulness <= 0.3 and status.mood <= -0.2)
                ]
                
                if (any(mischief_conditions) and 
                    self._state in [UEPState.IDLE, UEPState.CHAT]):
                    
                    debug_log(2, f"[StateManager] Mischief 條件滿足: Mood={status.mood:.2f}, "
                             f"Pride={status.pride:.2f}, Boredom={status.boredom:.2f}, "
                             f"Helpfulness={status.helpfulness:.2f}")
                    self.set_state(UEPState.MISCHIEF, {
                        "trigger_reason": "negative_system_values",
                        "mood": status.mood,
                        "pride": status.pride,
                        "boredom": status.boredom,
                        "helpfulness": status.helpfulness
                    })
                    return True
                
            return False
            
        except Exception as e:
            debug_log(1, f"[StateManager] 檢查特殊狀態條件時發生錯誤: {e}")
            return False
    
    def _update_status_for_mischief(self):
        """更新 Mischief 狀態的系統數值"""
        try:
            status_manager = self.status_manager
            
            # Mischief 狀態時，Helpfulness 變為負值
            status_manager.suppress_helpfulness("enter_mischief")
            
            debug_log(2, "[StateManager] 已調整 Mischief 狀態的系統數值")
            
        except Exception as e:
            debug_log(1, f"[StateManager] 更新 Mischief 狀態數值失敗: {e}")
    
    def _trigger_mischief_behaviors(self, context: Optional[Dict[str, Any]] = None):
        """
        觸發 Mischief 狀態的特殊行為
        
        流程：
        1. 獲取用戶配置（是否啟用、最大行為數）
        2. 根據當前系統數值決定可用行為
        3. 調用 LLM 生成行為序列
        4. 使用 MischiefExecutor 執行
        5. 根據執行結果調整系統數值
        """
        try:
            from configs.user_settings_manager import user_settings_manager
            
            # 檢查 MISCHIEF 是否啟用
            mischief_enabled = user_settings_manager.get_setting(
                "behavior.mischief.enabled", False
            )
            
            if not mischief_enabled:
                info_log("[StateManager] MISCHIEF 狀態已觸發，但用戶未啟用此功能")
                # 直接退出此狀態
                self.exit_special_state("mischief_disabled")
                return
            
            # 獲取配置
            max_actions = user_settings_manager.get_setting(
                "behavior.mischief.max_actions", 5
            )
            intensity = user_settings_manager.get_setting(
                "behavior.mischief.intensity", "medium"
            )
            
            info_log(f"[StateManager] 開始 MISCHIEF 行為規劃 "
                    f"(max_actions={max_actions}, intensity={intensity})")
            
            # 獲取當前系統數值
            status_dict = self.status_manager.get_status_dict()
            mood = status_dict.get("mood", 0.0)
            boredom = status_dict.get("boredom", 0.0)
            pride = status_dict.get("pride", 0.0)
            
            # 導入 MISCHIEF 執行器
            from modules.sys_module.actions.mischief.loader import (
                mischief_executor,
                mischief_registry
            )
            
            # 獲取可用行為列表
            available_actions_json = mischief_executor.get_available_actions_for_llm(mood)
            
            # 調用 LLM 生成行為規劃
            action_plan = self._call_llm_for_mischief_planning(
                available_actions_json,
                max_actions,
                mood,
                boredom,
                pride,
                intensity,
                context
            )
            
            if not action_plan:
                info_log("[StateManager] LLM 未返回有效的行為規劃")
                self.exit_special_state("no_plan")
                return
            
            # 解析並執行行為
            success, actions_list = mischief_executor.parse_llm_response(action_plan)
            
            if not success or not actions_list:
                info_log("[StateManager] 行為規劃解析失敗")
                self.exit_special_state("parse_failed")
                return
            
            info_log(f"[StateManager] 開始執行 {len(actions_list)} 個 MISCHIEF 行為")
            
            # 執行行為序列
            results = mischief_executor.execute_actions(actions_list)
            
            # 根據執行結果調整系統數值
            self._adjust_status_after_mischief(results, context)
            
            # MISCHIEF 完成，退出狀態
            self.exit_special_state("mischief_completed")
            
        except Exception as e:
            error_log(f"[StateManager] 觸發 Mischief 行為失敗: {e}")
            self.exit_special_state("error")
    
    def _call_llm_for_mischief_planning(
        self,
        available_actions_json: str,
        max_actions: int,
        mood: float,
        boredom: float,
        pride: float,
        intensity: str,
        context: Optional[Dict[str, Any]] = None
    ) -> Optional[str]:
        """
        調用 LLM 生成 MISCHIEF 行為規劃
        
        Returns:
            LLM 返回的 JSON 字串（包含行為序列）
        """
        try:
            from core.framework import core_framework
            
            llm_module = core_framework.registry.get_module("llm")
            
            if not llm_module:
                error_log("[StateManager] LLM 模組未找到")
                return None
            
            # 構建 prompt
            trigger_reason = context.get("trigger_reason", "unknown") if context else "unknown"
            
            system_prompt = (
                "你正處於 MISCHIEF（搗蛋）狀態。\n"
                "這是系統的自主活動模式，你可以主動進行一些趣味性的互動行為。\n"
                f"當前系統數值：Mood={mood:.2f}, Boredom={boredom:.2f}, Pride={pride:.2f}\n"
                f"搗蛋強度：{intensity}\n"
                f"最大行為數：{max_actions}\n"
                f"觸發原因：{trigger_reason}\n\n"
                "請根據當前情緒和可用行為，規劃一個有趣的搗蛋序列。\n"
                "注意：\n"
                "- 行為應符合當前情緒（心情好時較溫和，心情差時可能更調皮）\n"
                "- 搗蛋強度影響行為選擇的激進程度\n"
                "- 避免過度干擾用戶\n"
            )
            
            user_message = (
                f"可用行為列表：\n{available_actions_json}\n\n"
                "請生成行為規劃（JSON 格式）。"
            )
            
            # 調用 LLM
            response = llm_module.generate_response(
                user_message=user_message,
                system_prompt=system_prompt,
                temperature=0.9,  # 高溫度以獲得更有創意的行為
                max_tokens=1000
            )
            
            if response:
                debug_log(2, f"[StateManager] LLM 返回規劃：{response[:200]}...")
                return response
            else:
                error_log("[StateManager] LLM 未返回有效回應")
                return None
            
        except Exception as e:
            error_log(f"[StateManager] 調用 LLM 規劃失敗: {e}")
            return None
    
    def _adjust_status_after_mischief(
        self,
        results: Dict[str, Any],
        context: Optional[Dict[str, Any]] = None
    ):
        """
        根據 MISCHIEF 執行結果調整系統數值
        
        邏輯：
        - 成功的搗蛋 → 提升 mood，降低 boredom
        - 失敗過多 → 輕微降低 mood
        - 執行行為本身 → 小幅提升 pride（嘗試做些事）
        """
        try:
            total = results.get("total", 0)
            success = results.get("success", 0)
            failed = results.get("failed", 0)
            
            if total == 0:
                return
            
            # 計算成功率
            success_rate = success / total if total > 0 else 0
            
            # 根據成功率調整 mood
            if success_rate > 0.7:
                # 大部分成功，心情變好
                mood_delta = 0.15
                self.status_manager.update_mood(mood_delta, "mischief_success")
            elif success_rate > 0.3:
                # 部分成功
                mood_delta = 0.08
                self.status_manager.update_mood(mood_delta, "mischief_partial")
            else:
                # 失敗居多，略微失落
                mood_delta = -0.05
                self.status_manager.update_mood(mood_delta, "mischief_failed")
            
            # 降低無聊感（做了些事情）
            boredom_delta = -0.20
            self.status_manager.update_boredom(boredom_delta, "mischief_activity")
            
            # 小幅提升 pride（完成了自主活動）
            if success > 0:
                pride_delta = 0.10
                self.status_manager.update_pride(pride_delta, "mischief_completion")
            
            info_log(f"[StateManager] MISCHIEF 後數值調整完成 "
                    f"(成功率={success_rate:.2%}, mood_delta={mood_delta:+.2f})")
            
        except Exception as e:
            error_log(f"[StateManager] 調整 MISCHIEF 後數值失敗: {e}")
    
    def _setup_status_integration(self):
        """設置與 StatusManager 的整合"""
        try:
            status_manager = self.status_manager
            
            # 註冊狀態變化回調
            status_manager.register_update_callback(
                "state_manager", 
                self._on_status_update
            )
            
            debug_log(2, "[StateManager] StatusManager 整合設置完成")
            
        except Exception as e:
            debug_log(1, f"[StateManager] StatusManager 整合設置失敗: {e}")
    
    def _subscribe_to_session_events(self):
        """訂閱會話結束事件，實現狀態-會話一體化管理
        
        架構說明：
        - GS (General Session): 系統層級會話，不綁定特定狀態
        - CS (Chatting Session): 綁定 CHAT 狀態
        - WS (Workflow Session): 綁定 WORK 狀態
        - CS/WS 結束時觸發狀態轉換，由 StateQueue 決定下一個狀態
        """
        try:
            from core.event_bus import event_bus, SystemEvent
            
            # 監聽 SESSION_ENDED 事件 - CS/WS 結束觸發狀態轉換
            event_bus.subscribe(SystemEvent.SESSION_ENDED, self._on_session_ended)
            
            debug_log(2, "[StateManager] 已訂閱會話結束事件")
            
        except Exception as e:
            debug_log(1, f"[StateManager] 訂閱會話事件失敗: {e}")
    
    def _on_session_ended(self, event):
        """處理會話結束事件 - 通知 StateQueue 完成當前狀態
        
        這是狀態-會話一體化的核心：
        - 狀態創建會話 (State → Session)
        - 會話結束觸發狀態完成 (Session End → State Complete)
        - StateQueue 決定下一個狀態（可能是下一個任務，也可能是 IDLE）
        - 不硬編碼狀態轉換，由佇列自動管理
        
        Args:
            event: Event 對象，包含 session_id, reason, session_type 等數據
        """
        try:
            # Event 對象的 data 屬性包含事件數據
            session_id = event.data.get('session_id')
            reason = event.data.get('reason', 'session_completed')
            session_type = event.data.get('session_type', 'unknown')
            cycle_index = event.data.get('cycle_index')  # ✅ 讀取會話結束時的循環索引
            
            debug_log(2, f"[StateManager] 收到會話結束事件: {session_id} ({session_type}), 原因: {reason}, cycle: {cycle_index}")
            
            # 只處理 CS 和 WS 結束（GS 是系統層級，不觸發狀態轉換）
            if session_type in ['chatting', 'workflow']:
                # 通知 StateQueue 完成當前狀態
                from core.states.state_queue import get_state_queue_manager
                state_queue = get_state_queue_manager()
                
                success = reason != 'error' and reason != 'failed'
                state_queue.complete_current_state(
                    success=success,
                    result_data={
                        'session_id': session_id,
                        'session_type': session_type,
                        'end_reason': reason
                    },
                    completion_cycle=cycle_index  # ✅ 傳遞實際的完成循環索引
                )
                
                debug_log(1, f"[StateManager] ✅ {session_type.upper()} 會話結束，已通知 StateQueue 完成當前狀態")
                debug_log(2, f"[StateManager] StateQueue 將自動處理下一個狀態（若佇列為空則回到 IDLE）")
            
        except Exception as e:
            debug_log(1, f"[StateManager] 處理會話結束事件失敗: {e}")
    
    def _on_status_update(self, field: str, old_value: Any, new_value: Any, reason: str):
        """
        StatusManager 狀態更新回調
        當系統數值變化時檢查是否需要切換特殊狀態
        """
        try:
            debug_log(3, f"[StateManager] 收到狀態更新: {field} {old_value} -> {new_value} ({reason})")
            
            # 只在非特殊狀態時檢查特殊狀態條件
            if self._state not in [UEPState.MISCHIEF, UEPState.SLEEP]:
                # 延遲檢查，避免頻繁切換
                import threading
                threading.Timer(1.0, self.check_special_state_conditions).start()
                
        except Exception as e:
            debug_log(1, f"[StateManager] 處理狀態更新回調失敗: {e}")
    
    def exit_special_state(self, reason: str = ""):
        """
        退出特殊狀態 (Mischief/Sleep)
        回到 IDLE 狀態，並恢復正常數值
        """
        try:
            if self._state in [UEPState.MISCHIEF, UEPState.SLEEP]:
                old_state = self._state
                
                # 恢復系統數值
                if old_state == UEPState.MISCHIEF:
                    self._restore_helpfulness_after_mischief()
                elif old_state == UEPState.SLEEP:
                    self._wake_from_sleep(reason)
                
                # 回到 IDLE 狀態
                self.set_state(UEPState.IDLE, {"exit_reason": reason})
                
                debug_log(1, f"[StateManager] 退出 {old_state.name} 狀態: {reason}")
                
        except Exception as e:
            debug_log(1, f"[StateManager] 退出特殊狀態失敗: {e}")
    
    def _wake_from_sleep(self, reason: str):
        """從 SLEEP 狀態喚醒"""
        try:
            from core.states.sleep_manager import sleep_manager
            
            if sleep_manager.is_sleeping():
                sleep_manager.wake_up(reason)
                debug_log(2, f"[StateManager] 系統已喚醒: {reason}")
            
        except Exception as e:
            debug_log(1, f"[StateManager] 喚醒失敗: {e}")
    
    def _restore_helpfulness_after_mischief(self):
        """Mischief 狀態結束後恢復 Helpfulness"""
        try:
            status_manager = self.status_manager
            
            # 恢復到正常的助人意願水平
            status_manager.clear_helpfulness_override("leave_mischief")
            # 如要同時恢復自然值到你偏好的水位（例如 0.8），可額外調整：
            current = status_manager.get_status_dict()["helpfulness"]
            delta = 0.8 - current
            if abs(delta) > 1e-6:
                status_manager.update_helpfulness(delta, "restore_after_mischief")
            
            debug_log(2, "[StateManager] 已恢復 Mischief 後的 Helpfulness 數值")
            
        except Exception as e:
            debug_log(1, f"[StateManager] 恢復 Mischief 後數值失敗: {e}")
    


    def on_event(self, intent: str, result: dict):
        """
        根據意圖與執行結果決定是否切換狀態。

        Args:
            intent: 意圖類型 (chat, command 等)
            result: 執行結果
        """
        # 簡單的狀態轉換邏輯
        if intent == "chat":
            self._state = UEPState.CHAT
        elif intent == "command":
            # 指令處理，視為工作狀態
            if result.get("status") == "success":
                self._state = UEPState.WORK
            else:
                self._state = UEPState.ERROR
        else:
            self._state = UEPState.IDLE


# 全局狀態管理器實例
state_manager = StateManager()
