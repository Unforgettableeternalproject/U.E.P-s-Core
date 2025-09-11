# core/state_manager.py
from enum import Enum, auto
from typing import Dict, Any, Optional
import time
import time

class UEPState(Enum):
    IDLE      = auto()  # 閒置
    CHAT      = auto()  # 聊天
    WORK      = auto()  # 工作（執行指令，包含單步和多步驟工作流程）
    MISCHIEF  = auto()  # 搗蛋（暫略）
    SLEEP     = auto()  # 睡眠（暫略）
    ERROR     = auto()  # 錯誤

class SessionInfo:
    """工作流程會話資訊"""
    def __init__(self, session_id: str, workflow_type: str):
        self.session_id = session_id
        self.workflow_type = workflow_type
        self.step = 1
        self.started = True
        self.awaiting_input = True
        self.completed = False
        self.error = False
        
    def advance_step(self):
        """進入下一步驟"""
        self.step += 1
        
    def complete(self):
        """標記工作流程完成"""
        self.completed = True
        self.awaiting_input = False
        
    def fail(self):
        """標記工作流程失敗"""
        self.error = True
        self.awaiting_input = False

class StateManager:
    """
    管理 U.E.P 各種狀態。
    接受事件，並在需要時切換 state。
    支援多步驟指令處理工作流程。
    所有指令都被處理為工作流程，讓系統暫停原有STT->TTS管線，優先處理SYS中的工作邏輯。
    """

    def __init__(self):
        self._state = UEPState.IDLE
        self._active_session: Optional[SessionInfo] = None
        
    def get_state(self) -> UEPState:
        return self._state

    def set_state(self, new_state: UEPState):
        self._state = new_state
        
    def get_session(self) -> Optional[SessionInfo]:
        """獲取當前會話資訊"""
        return self._active_session
        
    def create_work_session(self, command: str, sys_action: Dict[str, Any] = None) -> SessionInfo:
        """
        基於LLM識別的指令和系統動作，創建一個工作會話。
        所有指令均通過會話管理，無論是單步還是多步驟工作流程，統一使用WORK狀態。
        
        Args:
            command: 原始指令文本
            sys_action: LLM識別的系統動作，格式為 {action, params}
            
        Returns:
            創建的會話資訊對象
        """
        from utils.debug_helper import debug_log
        
        # 獲取系統動作和功能類型
        action_type = "single_command"  # 默認是單步指令
        session_id = f"cmd-{int(time.time())}"  # 簡單的時間戳ID
        is_multi_step = False
        
        if sys_action and isinstance(sys_action, dict):
            action = sys_action.get("action", "")
            if action == "start_workflow":
                action_type = sys_action.get("params", {}).get("workflow_type", "file_processing")
                is_multi_step = True
                # 如果是啟動工作流程，使用系統生成的session_id
                if "session_id" in sys_action.get("params", {}):
                    session_id = sys_action["params"]["session_id"]
        
        # 創建會話
        session = SessionInfo(session_id, action_type)
        self._active_session = session
          # 統一使用WORK狀態，無論單步還是多步驟工作流程
        self._state = UEPState.WORK
        
        from utils.debug_helper import debug_log
        debug_log(1, f"[StateManager] 創建{'多步驟' if is_multi_step else '單步'} 工作會話: {session_id}, 類型: {action_type}")
        
        return session
        
    def on_event(self, intent: str, result: dict):
        """
        根據意圖與執行結果決定是否切換狀態。
        
        Args:
            intent: 意圖類型 (chat, command 等)
            result: 執行結果
        """
        # 檢查是否有工作流程相關資訊
        session_id = result.get("session_id")
        requires_input = result.get("requires_input", False)
        status = result.get("status", "")
        workflow_type = result.get("data", {}).get("workflow_type") if isinstance(result.get("data"), dict) else None
        
        # 檢查是否包含系統動作（來自LLM的回應）
        sys_action = result.get("sys_action")
        
        # 如果收到LLM識別的指令和系統動作，創建工作會話
        if intent == "command" and sys_action:
            self.create_work_session(result.get("text", ""), sys_action)
            return
        
        # 如果有工作流程session_id，管理工作會話狀態
        if session_id:
            if not self._active_session or self._active_session.session_id != session_id:
                # 新建工作流程
                self._active_session = SessionInfo(session_id, workflow_type or "unknown")
                self._state = UEPState.WORK
            
            if requires_input:
                # 工作流程等待輸入
                self._active_session.awaiting_input = True
                self._state = UEPState.WORK
            elif status in ("completed", "success"):
                # 成功完成
                if self._active_session:
                    self._active_session.complete()
                self._state = UEPState.IDLE
            elif status in ("cancelled", "error"):
                # 失敗或取消
                if self._active_session:
                    self._active_session.fail()
                self._state = UEPState.ERROR
                
            return
                
        # 一般意圖狀態轉換
        if intent == "chat":
            self._state = UEPState.CHAT
            self._active_session = None
        elif intent == "command":
            # 單一指令處理，也視為工作會話的一部分
            if result.get("status") == "success":
                self._state = UEPState.WORK
            else:
                self._state = UEPState.ERROR
            self._active_session = None
        else:
            self._state = UEPState.IDLE
            self._active_session = None


# 全局狀態管理器實例
state_manager = StateManager()
