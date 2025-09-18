# core/session_coordinator.py
"""
會話協調器 - 整合 General Session、Chatting Session、Workflow Session

這個模組負責協調三種會話類型的交互，確保它們按照正確的層級關係運作：

會話層級結構：
General Session (GS)
├── 基礎生命週期：輸入 → 處理 → 輸出
├── Working Context 一致性管理
└── 子會話管理：
    ├── Chatting Session (CS) - 對話處理
    └── Workflow Session (WS) - 工作流處理

協調邏輯：
1. 所有交互都從 GS 開始
2. 根據意圖決定是否啟動 CS 或 WS
3. 子會話結束後回到 GS 進行輸出
4. GS 結束時保留必要資訊到下個 GS
"""

from typing import Dict, Any, Optional, Union
from enum import Enum

from core.general_session import (
    general_session_manager, GeneralSession, GSType, GSStatus
)
from core.session_manager import session_manager
from core.chatting_session import chatting_session_manager, ChattingSession
from core.workflow_session import workflow_session_manager, WorkflowSession, WSTaskType
from core.state_manager import state_manager, UEPState
from core.router import router
from core.working_context import working_context_manager

from utils.debug_helper import debug_log, info_log, error_log


class SessionCoordinationResult(Enum):
    """會話協調結果"""
    GS_STARTED = "gs_started"
    CS_STARTED = "cs_started"
    WS_STARTED = "ws_started"
    SESSION_CONTINUED = "session_continued"
    SESSION_ENDED = "session_ended"
    ERROR = "error"


class SessionCoordinator:
    """會話協調器"""
    
    def __init__(self):
        self.active_cs_sessions: Dict[str, Dict[str, Any]] = {}  # CS 會話追蹤
        self.active_ws_sessions: Dict[str, Dict[str, Any]] = {}  # WS 會話追蹤
        
        # 註冊 GS 生命週期處理器
        self._setup_gs_lifecycle_handlers()
        
        info_log("[SessionCoordinator] 會話協調器初始化完成")
    
    def handle_user_input(self, input_data: Dict[str, Any]) -> SessionCoordinationResult:
        """
        處理使用者輸入，決定會話流程
        
        Args:
            input_data: 使用者輸入資料，包含 type, data 等
            
        Returns:
            協調結果
        """
        try:
            input_type = input_data.get("type", "unknown")
            
            # 1. 檢查是否已有活躍的 GS
            current_gs = general_session_manager.get_current_session()
            
            if not current_gs:
                # 沒有活躍的 GS，啟動新的 GS
                return self._start_new_gs(input_data)
            else:
                # 已有 GS，檢查是否需要子會話或繼續處理
                return self._handle_existing_gs(current_gs, input_data)
                
        except Exception as e:
            error_log(f"[SessionCoordinator] 處理使用者輸入時發生錯誤: {e}")
            return SessionCoordinationResult.ERROR
    
    def _start_new_gs(self, input_data: Dict[str, Any]) -> SessionCoordinationResult:
        """啟動新的 General Session"""
        input_type = input_data.get("type", "unknown")
        
        # 決定 GS 類型
        if input_type == "voice_input":
            gs_type = GSType.VOICE_INPUT
        elif input_type == "text_input":
            gs_type = GSType.TEXT_INPUT
        elif input_type == "system_event":
            gs_type = GSType.SYSTEM_EVENT
        else:
            gs_type = GSType.VOICE_INPUT  # 默認
        
        # 啟動新的 GS
        new_gs = general_session_manager.start_session(gs_type, {
            "type": input_type,
            "data": input_data.get("data", {}),
            "timestamp": input_data.get("timestamp")
        })
        
        if new_gs:
            info_log(f"[SessionCoordinator] 啟動新的 GS: {new_gs.session_id}")
            
            # 分析輸入並決定下一步
            return self._analyze_and_route(new_gs, input_data)
        else:
            error_log("[SessionCoordinator] 啟動 GS 失敗")
            return SessionCoordinationResult.ERROR
    
    def _handle_existing_gs(self, current_gs: GeneralSession, 
                          input_data: Dict[str, Any]) -> SessionCoordinationResult:
        """處理現有 GS 的輸入"""
        
        # 如果 GS 正在處理子會話，將輸入轉發給對應的子會話
        if current_gs.status == GSStatus.PROCESSING:
            return self._handle_sub_session_input(current_gs, input_data)
        
        # 如果 GS 是活躍狀態，分析新輸入
        elif current_gs.status == GSStatus.ACTIVE:
            return self._analyze_and_route(current_gs, input_data)
        
        else:
            debug_log(2, f"[SessionCoordinator] GS 狀態不適合處理輸入: {current_gs.status}")
            return SessionCoordinationResult.SESSION_CONTINUED
    
    def _analyze_and_route(self, gs: GeneralSession, 
                          input_data: Dict[str, Any]) -> SessionCoordinationResult:
        """分析輸入並路由到適當的會話類型"""
        
        # 使用 NLP 分析意圖 (模擬，實際應該調用 NLP 模組)
        intent_analysis = self._analyze_intent(input_data)
        
        intent = intent_analysis.get("primary_intent", "unknown")
        confidence = intent_analysis.get("confidence", 0.0)
        
        debug_log(2, f"[SessionCoordinator] 意圖分析結果: {intent} (信心度: {confidence})")
        
        # 根據意圖決定會話類型
        if intent in ["conversation", "chat", "question"]:
            return self._start_chatting_session(gs, intent_analysis)
        
        elif intent in ["command", "task", "workflow"]:
            # 添加原始文本到意圖分析
            intent_analysis["original_text"] = input_data.get("data", {}).get("text", "")
            return self._start_workflow_session(gs, intent_analysis)
        
        elif intent in ["greeting", "status_check"]:
            # 簡單回應，不需要子會話
            return self._handle_simple_response(gs, intent_analysis)
        
        else:
            # 默認為對話
            return self._start_chatting_session(gs, intent_analysis)
    
    def _analyze_intent(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        分析輸入意圖 (模擬實現)
        實際使用時應該調用 NLP 模組
        """
        data = input_data.get("data", {})
        text = data.get("text", "")
        
        # 簡單的關鍵字匹配 (實際應該使用 NLP 模組)
        if any(word in text.lower() for word in ["執行", "處理", "工作流", "任務"]):
            return {
                "primary_intent": "command",
                "confidence": 0.8,
                "entities": {"action_type": "workflow"},
                "original_text": text
            }
        elif any(word in text.lower() for word in ["聊天", "對話", "問題", "?"]):
            return {
                "primary_intent": "conversation", 
                "confidence": 0.9,
                "entities": {"conversation_type": "general"},
                "original_text": text
            }
        elif any(word in text.lower() for word in ["你好", "嗨", "狀態"]):
            return {
                "primary_intent": "greeting",
                "confidence": 0.7,
                "entities": {},
                "original_text": text
            }
        else:
            return {
                "primary_intent": "conversation",
                "confidence": 0.6,
                "entities": {"conversation_type": "general"},
                "original_text": text
            }
    
    def _start_chatting_session(self, gs: GeneralSession, 
                               intent_analysis: Dict[str, Any]) -> SessionCoordinationResult:
        """啟動 Chatting Session"""
        
        # 準備身份上下文
        identity_context = {
            "user_id": working_context_manager.get_memory_token() or "default_user",
            "personality": "default",
            "conversation_mode": "casual",
            "preferences": intent_analysis.get("entities", {})
        }
        
        # 創建 CS
        cs_session = chatting_session_manager.create_session(
            gs.session_id, 
            identity_context
        )
        
        if cs_session:
            # 註冊到 GS
            general_session_manager.register_sub_session(cs_session.session_id, "chatting")
            
            # 更新追蹤記錄
            self.active_cs_sessions[cs_session.session_id] = {
                "gs_session_id": gs.session_id,
                "intent_analysis": intent_analysis,
                "started_at": gs.context.created_at.isoformat(),
                "cs_instance": cs_session
            }
            
            # 設定系統狀態
            state_manager.set_state(UEPState.CHAT)
            
            info_log(f"[SessionCoordinator] 啟動 CS: {cs_session.session_id}")
            return SessionCoordinationResult.CS_STARTED
        else:
            error_log("[SessionCoordinator] CS 創建失敗")
            return SessionCoordinationResult.ERROR
    
    def _start_workflow_session(self, gs: GeneralSession,
                               intent_analysis: Dict[str, Any]) -> SessionCoordinationResult:
        """啟動 Workflow Session"""
        
        # 分析任務類型
        entities = intent_analysis.get("entities", {})
        action_type = entities.get("action_type", "workflow")
        
        # 決定 WS 任務類型
        if action_type == "file":
            task_type = WSTaskType.FILE_OPERATION
        elif action_type == "system":
            task_type = WSTaskType.SYSTEM_COMMAND
        elif action_type == "integration":
            task_type = WSTaskType.MODULE_INTEGRATION
        elif action_type == "automation":
            task_type = WSTaskType.WORKFLOW_AUTOMATION
        else:
            task_type = WSTaskType.CUSTOM_TASK
        
        # 準備任務定義
        task_definition = {
            "command": intent_analysis.get("original_text", ""),
            "parameters": entities,
            "priority": "normal",
            "timeout": 300
        }
        
        # 創建 WS
        ws_session = workflow_session_manager.create_session(
            gs.session_id,
            task_type,
            task_definition
        )
        
        if ws_session:
            # 註冊到 GS
            general_session_manager.register_sub_session(ws_session.session_id, "workflow")
            
            # 更新追蹤記錄
            self.active_ws_sessions[ws_session.session_id] = {
                "gs_session_id": gs.session_id,
                "intent_analysis": intent_analysis,
                "started_at": gs.context.created_at.isoformat(),
                "ws_instance": ws_session
            }
            
            # 設定系統狀態
            state_manager.set_state(UEPState.WORK)
            
            info_log(f"[SessionCoordinator] 啟動 WS: {ws_session.session_id}")
            return SessionCoordinationResult.WS_STARTED
        else:
            error_log("[SessionCoordinator] WS 創建失敗")
            return SessionCoordinationResult.ERROR
    
    def _handle_simple_response(self, gs: GeneralSession,
                               intent_analysis: Dict[str, Any]) -> SessionCoordinationResult:
        """處理簡單回應 (不需要子會話)"""
        
        intent = intent_analysis.get("primary_intent", "")
        
        if intent == "greeting":
            response = {"type": "greeting", "message": "你好！我是 U.E.P，有什麼可以幫助你的嗎？"}
        elif intent == "status_check":
            response = {"type": "status", "message": "系統運行正常"}
        else:
            response = {"type": "generic", "message": "我了解了"}
        
        # 直接添加輸出到 GS
        general_session_manager.add_output_to_current(response)
        
        return SessionCoordinationResult.SESSION_CONTINUED
    
    def _handle_sub_session_input(self, gs: GeneralSession,
                                 input_data: Dict[str, Any]) -> SessionCoordinationResult:
        """處理子會話的輸入"""
        
        # 檢查最新的子會話
        if gs.context.sub_sessions:
            latest_sub_session = gs.context.sub_sessions[-1]
            
            if latest_sub_session.startswith("cs_"):
                return self._handle_cs_input(latest_sub_session, input_data)
            elif latest_sub_session.startswith("ws_"):
                return self._handle_ws_input(latest_sub_session, input_data)
        
        return SessionCoordinationResult.SESSION_CONTINUED
    
    def _handle_cs_input(self, cs_session_id: str, 
                        input_data: Dict[str, Any]) -> SessionCoordinationResult:
        """處理 CS 輸入"""
        if cs_session_id in self.active_cs_sessions:
            cs_instance = self.active_cs_sessions[cs_session_id].get("cs_instance")
            
            if cs_instance:
                # 調用 CS 處理輸入
                result = cs_instance.process_input(input_data.get("data", {}))
                
                if result.get("success", False):
                    # 將 CS 的回應添加到 GS 輸出
                    general_session_manager.add_output_to_current({
                        "type": "cs_response",
                        "cs_session_id": cs_session_id,
                        "turn_id": result.get("turn_id"),
                        "response": result.get("response"),
                        "processing_time": result.get("processing_time")
                    })
                    
                    debug_log(3, f"[SessionCoordinator] CS 輸入處理成功: {cs_session_id}")
                    return SessionCoordinationResult.SESSION_CONTINUED
                else:
                    error_log(f"[SessionCoordinator] CS 處理失敗: {result.get('error')}")
                    return SessionCoordinationResult.ERROR
            else:
                error_log(f"[SessionCoordinator] CS 實例不存在: {cs_session_id}")
                return SessionCoordinationResult.ERROR
        else:
            error_log(f"[SessionCoordinator] CS 會話不存在: {cs_session_id}")
            return SessionCoordinationResult.ERROR
    
    def _handle_ws_input(self, ws_session_id: str,
                        input_data: Dict[str, Any]) -> SessionCoordinationResult:
        """處理 WS 輸入"""
        if ws_session_id in self.active_ws_sessions:
            ws_instance = self.active_ws_sessions[ws_session_id].get("ws_instance")
            
            if ws_instance:
                # WS 通常不需要處理額外輸入，但可以處理控制命令
                input_type = input_data.get("type", "")
                
                if input_type == "control":
                    control_action = input_data.get("data", {}).get("action", "")
                    
                    if control_action == "pause":
                        ws_instance.pause_execution()
                    elif control_action == "resume":
                        ws_instance.resume_execution()
                    elif control_action == "cancel":
                        ws_instance.cancel_execution()
                    elif control_action == "status":
                        progress = ws_instance.get_progress()
                        general_session_manager.add_output_to_current({
                            "type": "ws_status",
                            "ws_session_id": ws_session_id,
                            "progress": progress
                        })
                    
                    return SessionCoordinationResult.SESSION_CONTINUED
                else:
                    # 一般輸入，執行下一步驟
                    result = ws_instance.execute_next_step()
                    
                    if result.get("success", False):
                        # 將 WS 的結果添加到 GS 輸出
                        general_session_manager.add_output_to_current({
                            "type": "ws_step_result",
                            "ws_session_id": ws_session_id,
                            "step_result": result
                        })
                        
                        # 檢查是否完成
                        if result.get("execution_completed", False):
                            self.end_sub_session(ws_session_id, result.get("task_result"))
                        
                        debug_log(3, f"[SessionCoordinator] WS 步驟執行成功: {ws_session_id}")
                        return SessionCoordinationResult.SESSION_CONTINUED
                    else:
                        error_log(f"[SessionCoordinator] WS 步驟執行失敗: {result.get('error')}")
                        self.end_sub_session(ws_session_id, {"error": result.get("error")})
                        return SessionCoordinationResult.ERROR
            else:
                error_log(f"[SessionCoordinator] WS 實例不存在: {ws_session_id}")
                return SessionCoordinationResult.ERROR
        else:
            error_log(f"[SessionCoordinator] WS 會話不存在: {ws_session_id}")
            return SessionCoordinationResult.ERROR
    
    def end_sub_session(self, sub_session_id: str, 
                       final_output: Optional[Dict[str, Any]] = None) -> bool:
        """結束子會話"""
        
        # 從 GS 中移除子會話
        success = general_session_manager.end_sub_session(sub_session_id)
        
        if success:
            # 處理 CS 結束
            if sub_session_id in self.active_cs_sessions:
                cs_instance = self.active_cs_sessions[sub_session_id].get("cs_instance")
                if cs_instance:
                    session_summary = cs_instance.end_session(save_memory=True)
                    final_output = final_output or session_summary
                
                # 結束 CS Manager 中的會話
                chatting_session_manager.end_session(sub_session_id, save_memory=True)
                
                # 清理追蹤記錄
                del self.active_cs_sessions[sub_session_id]
            
            # 處理 WS 結束
            elif sub_session_id in self.active_ws_sessions:
                ws_instance = self.active_ws_sessions[sub_session_id].get("ws_instance")
                if ws_instance:
                    session_info = ws_instance.get_session_info()
                    final_output = final_output or session_info
                
                # 結束 WS Manager 中的會話
                workflow_session_manager.end_session(sub_session_id)
                
                # 清理追蹤記錄
                del self.active_ws_sessions[sub_session_id]
            
            # 添加子會話的最終輸出到 GS
            if final_output:
                general_session_manager.add_output_to_current({
                    "sub_session_id": sub_session_id,
                    "sub_session_output": final_output
                })
            
            # 重置系統狀態
            state_manager.set_state(UEPState.IDLE)
            
            info_log(f"[SessionCoordinator] 結束子會話: {sub_session_id}")
            return True
        
        return False
    
    def end_current_session(self, final_output: Optional[Dict[str, Any]] = None) -> bool:
        """結束當前 General Session"""
        
        # 先結束所有活躍的子會話
        for cs_session_id in list(self.active_cs_sessions.keys()):
            self.end_sub_session(cs_session_id)
        
        for ws_session_id in list(self.active_ws_sessions.keys()):
            self.end_sub_session(ws_session_id)
        
        success = general_session_manager.end_current_session(final_output)
        
        if success:
            # 清理所有活躍的會話
            self.active_cs_sessions.clear()
            self.active_ws_sessions.clear()
            
            # 重置系統狀態
            state_manager.set_state(UEPState.IDLE)
            
            info_log("[SessionCoordinator] 結束當前 GS")
            return True
        
        return False
    
    def _setup_gs_lifecycle_handlers(self):
        """設置 GS 生命週期處理器"""
        # 這裡可以註冊 GS 生命週期事件的處理器
        # 例如：session.register_lifecycle_handler(GSStatus.COMPLETED, self._on_gs_completed)
        pass
    
    def get_system_status(self) -> Dict[str, Any]:
        """獲取系統狀態"""
        gs_status = general_session_manager.get_system_status()
        
        return {
            "general_session": gs_status,
            "active_cs_sessions": len(self.active_cs_sessions),
            "active_ws_sessions": len(self.active_ws_sessions),
            "system_state": state_manager.get_state().name,
            "coordinator_status": "active"
        }


# 全域會話協調器實例
session_coordinator = SessionCoordinator()