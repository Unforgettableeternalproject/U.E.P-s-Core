# core/chatting_session.py
"""
Chatting Session (CS) 實現

CS 專門負責處理對話型交互，特點包括：
1. 身份隔離：每個 CS 都有獨立的身份上下文
2. 記憶管理：與 MEM 模組緊密整合，自動產生對話快照
3. LLM 調用：透過 Working Context 調用 LLM 模組
4. 對話持續性：支援多輪對話

會話生命週期：
1. 初始化 -> 設定身份上下文
2. 對話處理 -> 接收輸入、調用 LLM、產生輸出
3. 記憶更新 -> 自動將對話存入 MEM
4. 結束 -> 清理上下文、返回 GS
"""

from typing import Dict, Any, Optional, List
from datetime import datetime
from enum import Enum
import uuid

from core.working_context import working_context_manager, ContextType
from utils.debug_helper import debug_log, info_log, error_log


class CSStatus(Enum):
    """CS 狀態"""
    INITIALIZING = "initializing"
    ACTIVE = "active"
    PROCESSING = "processing"
    PAUSED = "paused"
    COMPLETED = "completed"
    ERROR = "error"


class ConversationTurn:
    """單輪對話記錄"""
    
    def __init__(self, turn_id: str, user_input: Dict[str, Any]):
        self.turn_id = turn_id
        self.user_input = user_input
        self.llm_response: Optional[Dict[str, Any]] = None
        self.timestamp = datetime.now()
        self.processing_time: Optional[float] = None
        self.context_used: Dict[str, Any] = {}
        
    def set_response(self, response: Dict[str, Any], processing_time: float = None):
        """設定 LLM 回應"""
        self.llm_response = response
        self.processing_time = processing_time
        
    def to_dict(self) -> Dict[str, Any]:
        """轉換為字典格式"""
        return {
            "turn_id": self.turn_id,
            "timestamp": self.timestamp.isoformat(),
            "user_input": self.user_input,
            "llm_response": self.llm_response,
            "processing_time": self.processing_time,
            "context_used": self.context_used
        }


class ChattingSession:
    """對話會話"""
    
    def __init__(self, session_id: str, gs_session_id: str, 
                 identity_context: Optional[Dict[str, Any]] = None):
        self.session_id = session_id
        self.gs_session_id = gs_session_id
        self.identity_context = identity_context or {}
        
        self.status = CSStatus.INITIALIZING
        self.created_at = datetime.now()
        self.last_activity = self.created_at
        
        # 對話記錄
        self.conversation_turns: List[ConversationTurn] = []
        self.turn_counter = 0
        
        # 身份和記憶上下文
        self.memory_token = self._generate_memory_token()
        self.identity_snapshot: Optional[Dict[str, Any]] = None
        
        # 會話配置
        self.config = {
            "max_turns": 50,  # 最大對話輪數
            "auto_save_interval": 5,  # 每 5 輪自動保存
            "context_window": 10,  # 保留最近 10 輪對話作為上下文
            "memory_threshold": 0.7  # 記憶重要性閾值
        }
        
        # 初始化會話
        self._initialize_session()
        
    def _generate_memory_token(self) -> str:
        """生成記憶標識符"""
        return f"cs_{self.session_id}_{int(self.created_at.timestamp())}"
    
    def _initialize_session(self):
        """初始化會話"""
        try:
            # 1. 設定身份上下文
            self._setup_identity_context()
            
            # 2. 載入相關記憶
            self._load_relevant_memories()
            
            # 3. 設定 Working Context
            self._setup_working_context()
            
            self.status = CSStatus.ACTIVE
            info_log(f"[ChattingSession] CS 初始化完成: {self.session_id}")
            
        except Exception as e:
            error_log(f"[ChattingSession] CS 初始化失敗: {e}")
            self.status = CSStatus.ERROR
    
    def _setup_identity_context(self):
        """設定身份上下文"""
        # 從 identity_context 中獲取或創建身份信息
        if not self.identity_context:
            self.identity_context = {
                "user_id": "default_user",
                "personality": "default",
                "conversation_mode": "casual"
            }
        
        # 設定身份快照
        self.identity_snapshot = {
            "session_id": self.session_id,
            "user_identity": self.identity_context.get("user_id", "default"),
            "personality_profile": self.identity_context.get("personality", "default"),
            "conversation_preferences": self.identity_context.get("preferences", {}),
            "memory_token": self.memory_token
        }
        
        debug_log(2, f"[ChattingSession] 身份上下文設定完成: {self.identity_snapshot}")
    
    def _load_relevant_memories(self):
        """載入相關記憶"""
        try:
            # 透過 Working Context 請求 MEM 模組載入相關記憶
            memory_request = {
                "action": "load_conversation_context",
                "user_id": self.identity_context.get("user_id"),
                "session_token": self.memory_token,
                "context_size": self.config["context_window"]
            }
            
            working_context_manager.set_data(
                ContextType.MEM_EXTERNAL_ACCESS,
                "conversation_context_request",
                memory_request
            )
            
            debug_log(3, "[ChattingSession] 記憶載入請求已發送")
            
        except Exception as e:
            error_log(f"[ChattingSession] 載入記憶時發生錯誤: {e}")
    
    def _setup_working_context(self):
        """設定 Working Context"""
        # 設定對話上下文資料
        conversation_context = {
            "session_id": self.session_id,
            "identity_context": self.identity_snapshot,
            "conversation_mode": "chatting",
            "turn_count": self.turn_counter,
            "memory_token": self.memory_token
        }
        
        working_context_manager.set_data(
            ContextType.LLM_CONTEXT,
            "conversation_session",
            conversation_context
        )
    
    def process_input(self, user_input: Dict[str, Any]) -> Dict[str, Any]:
        """
        處理使用者輸入
        
        Args:
            user_input: 使用者輸入，格式：{
                "type": "text|voice",
                "content": "輸入內容",
                "metadata": {...}
            }
            
        Returns:
            處理結果：{
                "success": bool,
                "response": Dict[str, Any],
                "turn_id": str
            }
        """
        if self.status != CSStatus.ACTIVE:
            return {
                "success": False,
                "error": f"CS 狀態不正確: {self.status}",
                "turn_id": None
            }
        
        try:
            self.status = CSStatus.PROCESSING
            self.last_activity = datetime.now()
            
            # 1. 創建對話輪次
            turn_id = f"turn_{self.turn_counter + 1}"
            conversation_turn = ConversationTurn(turn_id, user_input)
            
            # 2. 準備 LLM 上下文
            llm_context = self._prepare_llm_context(user_input, conversation_turn)
            
            # 3. 調用 LLM
            start_time = datetime.now()
            llm_response = self._call_llm(llm_context)
            processing_time = (datetime.now() - start_time).total_seconds()
            
            # 4. 處理 LLM 回應
            response_data = self._process_llm_response(llm_response)
            conversation_turn.set_response(response_data, processing_time)
            
            # 5. 更新對話記錄
            self.conversation_turns.append(conversation_turn)
            self.turn_counter += 1
            
            # 6. 自動保存記憶 (如果需要)
            if self.turn_counter % self.config["auto_save_interval"] == 0:
                self._auto_save_memory()
            
            self.status = CSStatus.ACTIVE
            
            info_log(f"[ChattingSession] 對話輪次處理完成: {turn_id}")
            
            return {
                "success": True,
                "response": response_data,
                "turn_id": turn_id,
                "processing_time": processing_time
            }
            
        except Exception as e:
            error_log(f"[ChattingSession] 處理輸入時發生錯誤: {e}")
            self.status = CSStatus.ERROR
            return {
                "success": False,
                "error": str(e),
                "turn_id": None
            }
    
    def _prepare_llm_context(self, user_input: Dict[str, Any], 
                           conversation_turn: ConversationTurn) -> Dict[str, Any]:
        """準備 LLM 上下文"""
        
        # 獲取近期對話歷史
        recent_turns = self.conversation_turns[-self.config["context_window"]:]
        conversation_history = [turn.to_dict() for turn in recent_turns]
        
        # 獲取記憶上下文
        memory_context = working_context_manager.get_data(
            ContextType.MEM_EXTERNAL_ACCESS, 
            "conversation_memories"
        ) or {}
        
        # 構建完整上下文
        llm_context = {
            "session_info": {
                "session_id": self.session_id,
                "turn_id": conversation_turn.turn_id,
                "identity": self.identity_snapshot
            },
            "current_input": user_input,
            "conversation_history": conversation_history,
            "memory_context": memory_context,
            "system_instructions": self._get_system_instructions()
        }
        
        # 記錄使用的上下文
        conversation_turn.context_used = {
            "history_turns": len(conversation_history),
            "memory_entries": len(memory_context.get("relevant_memories", [])),
            "context_size": len(str(llm_context))
        }
        
        return llm_context
    
    def _get_system_instructions(self) -> Dict[str, Any]:
        """獲取系統指令"""
        personality = self.identity_context.get("personality", "default")
        
        return {
            "role": "assistant",
            "personality": personality,
            "conversation_mode": "chatting",
            "response_style": "natural",
            "memory_integration": True,
            "context_awareness": True
        }
    
    def _call_llm(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """調用 LLM 模組"""
        try:
            # 準備 LLM 請求
            llm_request = {
                "action": "generate_response",
                "context": context,
                "session_token": self.memory_token,
                "response_format": "conversational"
            }
            
            # 透過 Working Context 發送請求
            working_context_manager.set_data(
                ContextType.LLM_REQUEST,
                "conversation_generation",
                llm_request
            )
            
            # 等待回應 (實際實現中應該有異步機制)
            # 這裡模擬 LLM 回應
            mock_response = {
                "success": True,
                "response": {
                    "text": "這是一個模擬的 LLM 回應",
                    "confidence": 0.95,
                    "tokens_used": 150
                },
                "metadata": {
                    "model": "gpt-4",
                    "temperature": 0.7,
                    "processing_time": 1.2
                }
            }
            
            debug_log(3, "[ChattingSession] LLM 回應已接收")
            return mock_response
            
        except Exception as e:
            error_log(f"[ChattingSession] LLM 調用失敗: {e}")
            return {
                "success": False,
                "error": str(e)
            }
    
    def _process_llm_response(self, llm_response: Dict[str, Any]) -> Dict[str, Any]:
        """處理 LLM 回應"""
        if not llm_response.get("success", False):
            return {
                "type": "error",
                "content": "抱歉，我現在無法回應。請稍後再試。",
                "error": llm_response.get("error")
            }
        
        response_data = llm_response.get("response", {})
        
        return {
            "type": "text",
            "content": response_data.get("text", ""),
            "confidence": response_data.get("confidence", 0.0),
            "metadata": {
                "tokens_used": response_data.get("tokens_used", 0),
                "model_info": llm_response.get("metadata", {})
            }
        }
    
    def _auto_save_memory(self):
        """自動保存記憶"""
        try:
            # 準備對話快照
            conversation_snapshot = {
                "session_id": self.session_id,
                "memory_token": self.memory_token,
                "identity_context": self.identity_snapshot,
                "conversation_summary": self._generate_conversation_summary(),
                "important_turns": self._extract_important_turns(),
                "timestamp": datetime.now().isoformat()
            }
            
            # 透過 Working Context 發送保存請求
            memory_save_request = {
                "action": "save_conversation_snapshot",
                "snapshot": conversation_snapshot,
                "importance_threshold": self.config["memory_threshold"]
            }
            
            working_context_manager.set_data(
                ContextType.MEM_EXTERNAL_ACCESS,
                "conversation_snapshot",
                memory_save_request
            )
            
            debug_log(2, f"[ChattingSession] 自動保存記憶完成，輪次數: {self.turn_counter}")
            
        except Exception as e:
            error_log(f"[ChattingSession] 自動保存記憶失敗: {e}")
    
    def _generate_conversation_summary(self) -> str:
        """生成對話摘要"""
        if not self.conversation_turns:
            return "空對話"
        
        recent_turns = self.conversation_turns[-5:]  # 最近 5 輪
        
        summary_parts = []
        for turn in recent_turns:
            user_content = turn.user_input.get("content", "")
            if turn.llm_response:
                response_content = turn.llm_response.get("content", "")
                summary_parts.append(f"用戶: {user_content[:50]}... | 回應: {response_content[:50]}...")
        
        return " | ".join(summary_parts)
    
    def _extract_important_turns(self) -> List[Dict[str, Any]]:
        """提取重要的對話輪次"""
        important_turns = []
        
        for turn in self.conversation_turns:
            # 簡單的重要性判斷邏輯
            if turn.llm_response and turn.llm_response.get("confidence", 0) > self.config["memory_threshold"]:
                important_turns.append(turn.to_dict())
        
        return important_turns[-10:]  # 保留最近 10 個重要輪次
    
    def pause_session(self):
        """暫停會話"""
        if self.status == CSStatus.ACTIVE:
            self.status = CSStatus.PAUSED
            info_log(f"[ChattingSession] CS 已暫停: {self.session_id}")
    
    def resume_session(self):
        """恢復會話"""
        if self.status == CSStatus.PAUSED:
            self.status = CSStatus.ACTIVE
            self.last_activity = datetime.now()
            info_log(f"[ChattingSession] CS 已恢復: {self.session_id}")
    
    def end_session(self, save_memory: bool = True) -> Dict[str, Any]:
        """
        結束會話
        
        Args:
            save_memory: 是否保存最終記憶
            
        Returns:
            會話總結
        """
        try:
            if save_memory:
                self._auto_save_memory()
            
            # 生成會話總結
            session_summary = {
                "session_id": self.session_id,
                "gs_session_id": self.gs_session_id,
                "total_turns": self.turn_counter,
                "duration": (datetime.now() - self.created_at).total_seconds(),
                "conversation_summary": self._generate_conversation_summary(),
                "memory_token": self.memory_token,
                "final_status": "completed"
            }
            
            self.status = CSStatus.COMPLETED
            
            # 清理 Working Context
            working_context_manager.clear_data(ContextType.LLM_CONTEXT, "conversation_session")
            
            info_log(f"[ChattingSession] CS 結束: {self.session_id}, 總輪次: {self.turn_counter}")
            
            return session_summary
            
        except Exception as e:
            error_log(f"[ChattingSession] 結束會話時發生錯誤: {e}")
            self.status = CSStatus.ERROR
            return {
                "session_id": self.session_id,
                "error": str(e),
                "final_status": "error"
            }
    
    def get_session_info(self) -> Dict[str, Any]:
        """獲取會話信息"""
        return {
            "session_id": self.session_id,
            "gs_session_id": self.gs_session_id,
            "status": self.status.value,
            "created_at": self.created_at.isoformat(),
            "last_activity": self.last_activity.isoformat(),
            "turn_counter": self.turn_counter,
            "memory_token": self.memory_token,
            "identity_context": self.identity_snapshot,
            "config": self.config
        }


class ChattingSessionManager:
    """CS 管理器"""
    
    def __init__(self):
        self.active_sessions: Dict[str, ChattingSession] = {}
        self.session_history: List[Dict[str, Any]] = []
        
        info_log("[ChattingSessionManager] CS 管理器初始化完成")
    
    def create_session(self, gs_session_id: str, 
                      identity_context: Optional[Dict[str, Any]] = None) -> Optional[ChattingSession]:
        """創建新的 CS"""
        try:
            session_id = f"cs_{gs_session_id}_{len(self.active_sessions) + 1}"
            
            new_session = ChattingSession(session_id, gs_session_id, identity_context)
            
            if new_session.status == CSStatus.ACTIVE:
                self.active_sessions[session_id] = new_session
                info_log(f"[ChattingSessionManager] 創建 CS: {session_id}")
                return new_session
            else:
                error_log(f"[ChattingSessionManager] CS 創建失敗: {session_id}")
                return None
                
        except Exception as e:
            error_log(f"[ChattingSessionManager] 創建 CS 時發生錯誤: {e}")
            return None
    
    def get_session(self, session_id: str) -> Optional[ChattingSession]:
        """獲取 CS"""
        return self.active_sessions.get(session_id)
    
    def end_session(self, session_id: str, save_memory: bool = True) -> bool:
        """結束 CS"""
        session = self.active_sessions.get(session_id)
        if session:
            session_summary = session.end_session(save_memory)
            self.session_history.append(session_summary)
            del self.active_sessions[session_id]
            return True
        return False
    
    def get_active_sessions(self) -> List[ChattingSession]:
        """獲取所有活躍 CS"""
        return list(self.active_sessions.values())
    
    def cleanup_inactive_sessions(self, max_idle_minutes: int = 30):
        """清理非活躍會話"""
        current_time = datetime.now()
        inactive_sessions = []
        
        for session_id, session in self.active_sessions.items():
            idle_time = (current_time - session.last_activity).total_seconds() / 60
            if idle_time > max_idle_minutes:
                inactive_sessions.append(session_id)
        
        for session_id in inactive_sessions:
            self.end_session(session_id, save_memory=True)
            info_log(f"[ChattingSessionManager] 清理非活躍 CS: {session_id}")


# 全域 CS 管理器實例
chatting_session_manager = ChattingSessionManager()