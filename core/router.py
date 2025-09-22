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
            "memory_query": ("mem", "query"),  # memory_query → MEM 模組，記憶查詢
            "memory_store": ("mem", "content"), # memory_store → MEM 模組，記憶存儲
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
            # 檢查是否需要記憶檢索支援
            if self._should_retrieve_memory(intent, detail):
                args["enable_memory_retrieval"] = True
                debug_log(2, f"[Router] 為LLM請求啟用記憶檢索")
        
        # 對於記憶模組，添加必要的上下文
        if module_key == "mem":
            args.update(self._prepare_memory_args(intent, detail, state))
        
        debug_log(1, f"[Router] {intent} → {module_key}: {args}")
        return module_key, args
    
    def _should_retrieve_memory(self, intent: str, detail: Any) -> bool:
        """判斷是否需要檢索記憶來輔助回答"""
        # 聊天意圖通常需要記憶支援
        if intent == "chat":
            return True
        
        # 檢查內容中是否包含記憶相關關鍵詞
        if isinstance(detail, str):
            memory_keywords = ["記得", "之前", "上次", "昨天", "前面", "剛才", "想起"]
            return any(keyword in detail for keyword in memory_keywords)
        
        return False
    
    def _prepare_memory_args(self, intent: str, detail: Any, state: UEPState) -> Dict[str, Any]:
        """為記憶模組準備參數"""
        base_args = {
            "operation_type": "query" if intent == "memory_query" else "store",
            "timestamp": None,  # 由MEM模組自動生成
            "max_results": 5
        }
        
        if intent == "memory_query":
            base_args.update({
                "query_text": str(detail),
                "similarity_threshold": 0.7
            })
        elif intent == "memory_store":
            base_args.update({
                "content": str(detail),
                "memory_type": "user_input"
            })
        
        return base_args
        
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
            if isinstance(sys_action, dict):
                # 用LLM識別的系統動作創建工作會話
                from core.session_manager import session_manager
                
                # 確定工作流程類型
                action = sys_action.get("action", "")
                workflow_type = "single_command"
                if action == "start_workflow":
                    workflow_type = sys_action.get("params", {}).get("workflow_type", "file_processing")
                
                # 使用 session_manager 創建會話
                session = session_manager.create_session(
                    workflow_type=workflow_type,
                    command=response.get("text", ""),
                    initial_data={"sys_action": sys_action}
                )
                
                # 設置系統狀態為 WORK
                if state_manager:
                    state_manager.set_state(state_manager.__class__.WORK)
                
                debug_log(1, f"[Router] 基於LLM回應創建了工作會話: {session.session_id} (類型: {workflow_type})")
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


# 全局路由器實例
router = Router()
