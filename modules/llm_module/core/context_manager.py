# modules/llm_module/core/context_manager.py
"""
Context Manager - 系統上下文管理

負責管理和構建各種上下文信息：
- 系統狀態上下文
- 會話信息上下文
- 身份上下文
- 上下文補充和整合
"""

from typing import Dict, Any, Optional
from utils.debug_helper import debug_log, info_log, error_log
from core.working_context import working_context_manager


class ContextManager:
    """上下文管理器 - 統一管理系統上下文"""
    
    def __init__(self, llm_module):
        """
        初始化上下文管理器
        
        Args:
            llm_module: LLM 模組實例的引用
        """
        self.llm_module = llm_module
        self.status_manager = llm_module.status_manager
        self.state_manager = llm_module.state_manager
        debug_log(2, "[ContextManager] 上下文管理器初始化完成")
    
    def get_current_system_status(self) -> Dict[str, Any]:
        """獲取當前系統狀態"""
        try:
            # 🔧 添加 None 檢查，防止 'NoneType' object is not subscriptable 錯誤
            status_dict = self.status_manager.get_status_dict()
            if status_dict is None:
                debug_log(1, "[ContextManager] status_manager.get_status_dict() 返回 None，使用預設值")
                status_dict = {}
            
            personality_modifiers = self.status_manager.get_personality_modifiers()
            if personality_modifiers is None:
                debug_log(1, "[ContextManager] status_manager.get_personality_modifiers() 返回 None，使用預設值")
                personality_modifiers = {}
            
            return {
                "status_values": status_dict,
                "personality_modifiers": personality_modifiers,
                "system_mode": self.state_manager.get_current_state().value
            }
        except Exception as e:
            error_log(f"[ContextManager] 獲取系統狀態失敗: {e}")
            return {"error": str(e)}
    
    def get_current_gs_id(self) -> str:
        """
        獲取當前 General Session ID
        從 working_context 的全局數據中讀取 (由 SystemLoop 設置)
        
        Returns:
            str: 當前 GS ID,如果無法獲取則返回 'unknown'
        """
        try:
            gs_id = working_context_manager.global_context_data.get('current_gs_id', 'unknown')
            return gs_id
        except Exception as e:
            error_log(f"[ContextManager] 獲取 GS ID 失敗: {e}")
            return 'unknown'
    
    def get_current_cycle_index(self) -> int:
        """
        獲取當前循環計數
        從 working_context 的全局數據中讀取 (由 Controller 在 GS 創建時設置)
        
        Returns:
            int: 當前 cycle_index,如果無法獲取則返回 0（假設為第一個 cycle）
        """
        try:
            cycle_index = working_context_manager.global_context_data.get('current_cycle_index', 0)
            return cycle_index
        except Exception as e:
            error_log(f"[ContextManager] 獲取 cycle_index 失敗: {e}")
            return 0
    
    def get_current_session_info(self, workflow_session_id: Optional[str] = None) -> Dict[str, Any]:
        """獲取當前會話信息 - 優先獲取 CS 或 WS（LLM 作為邏輯中樞的執行會話）
        
        Args:
            workflow_session_id: 可選的指定工作流會話ID，如果提供則優先返回該會話的信息
        """
        try:
            # 從統一會話管理器獲取會話信息
            from core.sessions.session_manager import session_manager
            
            # 如果指定了 workflow_session_id，優先獲取該特定會話
            if workflow_session_id:
                current_ws = session_manager.get_workflow_session(workflow_session_id)
                if current_ws:
                    debug_log(2, f"[ContextManager] 使用指定的工作流會話: {workflow_session_id}")
                    return {
                        "session_id": workflow_session_id,
                        "session_type": "workflow",
                        "start_time": getattr(current_ws, 'start_time', None),
                        "interaction_count": getattr(current_ws, 'step_count', 0),
                        "last_activity": getattr(current_ws, 'last_activity', None),
                        "active_session_type": "WS"
                    }
            
            # LLM 在 CHAT 狀態時應該獲取當前 CS
            active_cs_ids = session_manager.get_active_chatting_session_ids()
            if active_cs_ids:
                # 在架構下，同一時間只會有一個 CS 執行中
                current_cs_id = active_cs_ids[0]
                current_cs = session_manager.get_chatting_session(current_cs_id)
                
                if current_cs:
                    return {
                        "session_id": current_cs_id,
                        "session_type": "chatting",
                        "start_time": getattr(current_cs, 'start_time', None),
                        "interaction_count": getattr(current_cs, 'turn_count', 0),
                        "last_activity": getattr(current_cs, 'last_activity', None),
                        "active_session_type": "CS"
                    }
            
            # LLM 在 WORK 狀態時應該獲取當前 WS
            active_ws_ids = session_manager.get_active_workflow_session_ids()
            if active_ws_ids:
                # 在架構下，同一時間只會有一個 WS 執行中
                current_ws_id = active_ws_ids[0]
                current_ws = session_manager.get_workflow_session(current_ws_id)
                
                if current_ws:
                    return {
                        "session_id": current_ws_id,
                        "session_type": "workflow",
                        "start_time": getattr(current_ws, 'start_time', None),
                        "interaction_count": getattr(current_ws, 'step_count', 0),
                        "last_activity": getattr(current_ws, 'last_activity', None),
                        "active_session_type": "WS"
                    }
            
            # 如果沒有 CS 或 WS，可能系統處於 IDLE 狀態或其他狀態
            return {
                "session_id": "no_active_session", 
                "session_type": "idle",
                "start_time": None,
                "interaction_count": 0,
                "last_activity": None,
                "active_session_type": "NONE"
            }
            
        except Exception as e:
            error_log(f"[ContextManager] 獲取會話信息失敗: {e}")
            return {
                "session_id": "error", 
                "session_type": "error",
                "active_session_type": "ERROR"
            }
    
    def get_identity_context(self) -> Dict[str, Any]:
        """從Working Context獲取Identity信息，對通用身份採用預設處理"""
        try:
            # 使用正確的方法獲取當前身份
            identity_data = working_context_manager.get_current_identity()
            
            if not identity_data:
                debug_log(2, "[ContextManager] 沒有設置身份信息，使用預設值")
                return {
                    "identity": {
                        "name": "default_user",
                        "traits": {}
                    },
                    "preferences": {}
                }
            
            # 檢查是否為通用身份
            identity_status = identity_data.get("status", "unknown")
            if identity_status == "temporary":
                debug_log(2, "[ContextManager] 檢測到通用身份，使用基本設置")
                return {
                    "identity": {
                        "name": "用戶",
                        "traits": {},
                        "status": "temporary"
                    },
                    "preferences": {}  # 通用身份不使用特殊偏好
                }
            
            # 正式身份使用完整資料
            return {
                "identity": {
                    "name": identity_data.get("user_identity", identity_data.get("identity_id", "default_user")),
                    "traits": identity_data.get("traits", {}),
                    "status": identity_status
                },
                "preferences": identity_data.get("conversation_preferences", {})
            }
        except Exception as e:
            error_log(f"[ContextManager] 獲取Identity上下文失敗: {e}")
            return {}
    
    def enrich_with_system_context(self, 
                                   llm_input: Any,  # LLMInput type
                                   current_state: Any,
                                   status: Dict[str, Any],
                                   session_info: Dict[str, Any],
                                   identity_context: Dict[str, Any]) -> Any:
        """補充系統上下文到LLM輸入 - 支援新 Router 整合
        
        Args:
            llm_input: LLMInput 對象
            current_state: 當前系統狀態
            status: 系統狀態字典
            session_info: 會話信息
            identity_context: 身份上下文
            
        Returns:
            補充後的 LLMInput 對象
        """
        try:
            # 導入 LLMInput（避免循環導入）
            from ..schemas import LLMInput
            
            # 創建新的enriched input
            enriched_data = llm_input.dict()
            
            # 補充系統上下文
            if not enriched_data.get("system_context"):
                enriched_data["system_context"] = {}
            
            enriched_data["system_context"].update({
                "current_state": current_state.value if hasattr(current_state, 'value') else str(current_state),
                "status_manager": status,
                "session_info": session_info
            })
            
            # 補充身份上下文 (不覆蓋Router提供的)
            if not enriched_data.get("identity_context"):
                enriched_data["identity_context"] = {}
            # 只在沒有Router數據時補充本地身份上下文
            if not llm_input.source_layer:
                enriched_data["identity_context"].update(identity_context)
            
            # 處理新Router提供的協作上下文
            if llm_input.collaboration_context:
                debug_log(2, f"[ContextManager] 處理協作上下文: {list(llm_input.collaboration_context.keys())}")
                
                # 設置記憶檢索標誌
                if "mem" in llm_input.collaboration_context:
                    enriched_data["enable_memory_retrieval"] = True
                    mem_config = llm_input.collaboration_context["mem"]
                    if mem_config.get("retrieve_relevant"):
                        enriched_data["memory_context"] = "協作模式：需要檢索相關記憶"
                
                # 設置系統動作標誌
                if "sys" in llm_input.collaboration_context:
                    enriched_data["enable_system_actions"] = True
                    sys_config = llm_input.collaboration_context["sys"]
                    if sys_config.get("allow_execution"):
                        enriched_data["workflow_context"] = {"execution_allowed": True}
            
            # 處理Router的會話上下文
            if llm_input.session_context:
                enriched_data["session_id"] = llm_input.session_context.get("session_id")
                enriched_data["system_context"]["router_session"] = llm_input.session_context
            
            # 處理NLP實體信息
            if llm_input.entities:
                enriched_data["system_context"]["nlp_entities"] = llm_input.entities
            
            return LLMInput(**enriched_data)
            
        except Exception as e:
            error_log(f"[ContextManager] 補充系統上下文失敗: {e}")
            return llm_input
