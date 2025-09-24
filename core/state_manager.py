# core/state_manager.py
from enum import Enum, auto
from typing import Dict, Any, Optional, List, Callable
import time
import time

from utils.debug_helper import debug_log
from core.working_context import ContextType

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
    """

    def __init__(self):
        self._state = UEPState.IDLE
        self._current_session_id: Optional[str] = None
        self._state_change_callbacks: List[Callable[[UEPState, UEPState], None]] = []
        
    def get_state(self) -> UEPState:
        return self._state
    
    def get_current_state(self) -> UEPState:
        """獲取當前狀態（與 get_state 相同，提供兼容性）"""
        return self._state

    def set_state(self, new_state: UEPState, context: Optional[Dict[str, Any]] = None):
        """
        設置新狀態，並觸發狀態變化處理
        
        Args:
            new_state: 新狀態
            context: 狀態變化上下文 (包含創建會話所需的資訊)
        """
        old_state = self._state
        if old_state == new_state:
            return  # 狀態沒有變化
            
        self._state = new_state
        debug_log(2, f"[StateManager] 狀態變更: {old_state.name} -> {new_state.name}")
        
        # 觸發狀態變化回調
        self._on_state_changed(old_state, new_state, context)
        
        # 通知所有回調
        for callback in self._state_change_callbacks:
            try:
                callback(old_state, new_state)
            except Exception as e:
                debug_log(1, f"[StateManager] 狀態變化回調執行失敗: {e}")
    
    def add_state_change_callback(self, callback: Callable[[UEPState, UEPState], None]):
        """添加狀態變化回調"""
        self._state_change_callbacks.append(callback)
        
    def get_current_session_id(self) -> Optional[str]:
        """獲取當前會話ID"""
        return self._current_session_id
        
    def _on_state_changed(self, old_state: UEPState, new_state: UEPState, context: Optional[Dict[str, Any]] = None):
        """
        處理狀態變化，創建對應的會話
        
        Args:
            old_state: 舊狀態
            new_state: 新狀態
            context: 狀態變化上下文
        """
        try:
            # 根據新狀態創建對應的會話
            if new_state == UEPState.CHAT:
                self._create_chat_session(context)
            elif new_state == UEPState.WORK:
                self._create_work_session(context)
            elif new_state == UEPState.IDLE:
                self._cleanup_sessions()
                
        except Exception as e:
            debug_log(1, f"[StateManager] 狀態變化處理失敗: {e}")
    
    def _create_chat_session(self, context: Optional[Dict[str, Any]] = None):
        """創建聊天會話"""
        try:
            from core.chatting_session import chatting_session_manager
            from core.working_context import working_context_manager
            
            # 從 Working Context 獲取身份信息
            identity_context = working_context_manager.get_data(ContextType.IDENTITY_MANAGEMENT, "identity_context", {})
            if not identity_context:
                # 如果沒有身份信息，使用默認值
                identity_context = {
                    "user_id": "default_user",
                    "personality": "default",
                    "preferences": {}
                }
            
            # 創建 Chatting Session
            gs_session_id = f"gs_{int(time.time())}"
            cs = chatting_session_manager.create_session(
                gs_session_id=gs_session_id,
                identity_context=identity_context
            )
            
            if cs:
                self._current_session_id = cs.session_id
                debug_log(2, f"[StateManager] 創建聊天會話成功: {cs.session_id}")
                
                # 如果有初始輸入，處理它
                if context and context.get("initial_input"):
                    response = cs.process_input(context["initial_input"])
                    debug_log(3, f"[StateManager] 處理初始輸入完成: {cs.session_id}")
            else:
                debug_log(1, "[StateManager] 創建聊天會話失敗")
                
        except Exception as e:
            debug_log(1, f"[StateManager] 創建聊天會話時發生錯誤: {e}")
    
    def _create_work_session(self, context: Optional[Dict[str, Any]] = None):
        """創建工作會話"""
        try:
            from core.session_manager import session_manager
            
            # 從上下文獲取工作流程信息
            workflow_type = "single_command"
            command = "unknown command"
            
            if context:
                workflow_type = context.get("workflow_type", workflow_type)
                command = context.get("command", command)
            
            # 創建 Workflow Session
            ws = session_manager.create_session(
                workflow_type=workflow_type,
                command=command,
                initial_data=context
            )
            
            if ws:
                self._current_session_id = ws.session_id
                debug_log(2, f"[StateManager] 創建工作會話成功: {ws.session_id} (類型: {workflow_type})")
            else:
                debug_log(1, "[StateManager] 創建工作會話失敗")
                
        except Exception as e:
            debug_log(1, f"[StateManager] 創建工作會話時發生錯誤: {e}")
    
    def _cleanup_sessions(self):
        """清理會話 (當回到IDLE狀態時)"""
        # 這裡可以添加會話清理邏輯
        # 目前只是清除當前會話ID引用
        self._current_session_id = None
        debug_log(3, "[StateManager] 清理會話引用")
        
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
            from core.session_manager import session_manager
            from core.chatting_session import chatting_session_manager
            
            # 檢查工作會話
            active_work_sessions = session_manager.get_active_sessions() if session_manager else []
            
            # 檢查聊天會話
            active_chat_sessions = chatting_session_manager.get_active_sessions() if chatting_session_manager else []
            
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
