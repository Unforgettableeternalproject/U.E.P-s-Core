# core/router.py
from typing import Tuple, Any, Dict, Optional
from utils.debug_helper import info_log, debug_log
from core.state_manager import UEPState, StateManager
from enum import Enum

class CommandSessionState(Enum):
    """用於追蹤指令工作流程的狀態"""
    NONE = "none"                  # 無工作流程
    AWAITING_INPUT = "awaiting"    # 等待用戶輸入
    PROCESSING = "processing"      # 處理中
    COMPLETED = "completed"        # 已完成
    FAILED = "failed"              # 失敗

class Router:
    """
    將意圖(intent)與當前狀態(state)對應到具體要呼叫的模組與參數。
    支援多步驟指令處理工作流程。
    """

    def __init__(self):
        # intent → (module_key, template_args_key)
        # template_args_key 表示 args 用哪個 payload 欄位
        self._map = {
            "chat": ("llm", "text"),       # chat → LLM 模組，用 text 做輸入
            "command": ("sys", "detail"),  # command → SYS 模組，用 detail 做輸入
            # 其他 intent 可繼續擴
        }
        
        # 工作流程狀態
        self._active_session_id: Optional[str] = None
        self._session_state = CommandSessionState.NONE
        self._session_step = 0
        self._session_workflow_type: Optional[str] = None
        
    def route(self,
              intent: str,
              detail: Any,
              state: UEPState,
              state_manager: Optional[StateManager] = None
             ) -> Tuple[str, Dict[str, Any]]:
        """
        根據 intent、detail，以及當前 state 決定：
          1. module_key: 要呼叫哪個模組
          2. args: 傳給模組的參數 dict

        Args:
            intent: 意圖類型
            detail: 使用者輸入的內容
            state: 當前系統狀態
            state_manager: 系統狀態管理器實例

        Returns:
            module_key, args
        """
        # 檢查當前是否有活躍的工作流程會話
        active_session = None
        if state_manager:
            active_session = state_manager.get_session()
        
        # 處理進行中的工作流程 session
        if active_session and active_session.awaiting_input:
            debug_log(1, f"[Router] 檢測到進行中的工作流程，將輸入傳遞至 session: {active_session.session_id}")
            return "sys", {
                "mode": "continue_workflow", 
                "params": {
                    "session_id": active_session.session_id, 
                    "user_input": detail
                }
            }
        
        # 一般意圖處理
        if intent not in self._map:
            # 預設 fallback
            return "llm", {"text": detail, "intent": "chat"}        # 針對 command 意圖的處理
        if intent == "command":
            debug_log(1, f"[Router] 檢測到指令: {detail}")
              # 第一步：使用LLM分析並識別指令
            if state != UEPState.WORK:
                debug_log(1, f"[Router] 轉發指令到LLM進行分析")
                # 首先轉發到LLM進行分析
                return "llm", {
                    "text": detail, 
                    "intent": "command",
                    "get_sys_functions": True  # 標記需要獲取系統功能列表
                }
            
            # 如果已經在工作狀態，直接轉發到SYS模組
            return "sys", {
                "mode": "execute_command",
                "params": {
                    "command": detail
                }
            }
            
        # 其他意圖保持原樣
        module_key, arg_key = self._map[intent]
        args = {arg_key: detail}
        
        # 對於 llm，加入 intent
        if module_key == "llm":
            args["intent"] = intent
            
        return module_key, args
        
    def handle_response(self, 
                    module_key: str, 
                    response: Dict[str, Any],
                    state_manager: Optional[StateManager] = None
                   ) -> None:
        """
        處理模組回應，更新工作流程狀態
        
        Args:
            module_key: 回應的模組
            response: 模組的回應
            state_manager: 系統狀態管理器實例
        """
        # 檢查是LLM回應中是否包含系統動作
        if module_key == "llm" and "sys_action" in response:
            sys_action = response.get("sys_action")
            if state_manager and isinstance(sys_action, dict):
                # 用LLM識別的系統動作創建工作會話
                state_manager.create_work_session(
                    command=response.get("text", ""),
                    sys_action=sys_action
                )
                debug_log(1, f"[Router] 基於LLM回應創建了工作會話: {sys_action}")
                return
        
        # 只有 sys 模組的回應有可能包含工作流程資訊
        if module_key != "sys":
            return
            
        # 檢查是否包含工作流程相關資訊
        session_id = response.get("session_id")
        requires_input = response.get("requires_input", False)
        status = response.get("status", "")
        
        if not session_id:
            # 不是工作流程回應
            self._session_state = CommandSessionState.NONE
            self._active_session_id = None
            return
            
        # 更新工作流程狀態
        self._active_session_id = session_id
        
        if requires_input:
            # 工作流程需要用戶輸入
            self._session_state = CommandSessionState.AWAITING_INPUT
            self._session_step += 1
            debug_log(1, f"[Router] 工作流程等待輸入，步驟: {self._session_step}")
        elif status in ("completed", "cancelled", "error"):
            # 工作流程結束
            self._session_state = CommandSessionState.COMPLETED if status == "completed" else CommandSessionState.FAILED
            self._active_session_id = None
            debug_log(1, f"[Router] 工作流程結束，狀態: {status}")
        else:
            # 繼續處理中
            self._session_state = CommandSessionState.PROCESSING
            
    def get_session_info(self) -> Dict[str, Any]:
        """獲取當前工作流程狀態資訊"""
        return {
            "active_session": self._active_session_id is not None,
            "session_id": self._active_session_id,
            "state": self._session_state.value if self._session_state else "none",
            "step": self._session_step,
            "workflow_type": self._session_workflow_type
        }
        
    def reset_session(self) -> None:
        """重置工作流程狀態"""
        self._active_session_id = None
        self._session_state = CommandSessionState.NONE
        self._session_step = 0
        self._session_workflow_type = None
