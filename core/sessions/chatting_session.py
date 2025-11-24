# core/sessions/chatting_session.py
"""
Chatting Session (CS) 實現 - 重構版本

根據系統流程文檔，CS 的正確職責：
1. 追蹤會話生命週期和狀態
2. 記錄對話輪次信息
3. 維護會話相關的元數據和配置
4. 提供會話級別的上下文信息

CS 不應該做的事（由模組和 Router 處理）：
- ❌ 直接調用 LLM/MEM 模組
- ❌ 管理模組間數據傳遞
- ❌ 處理記憶載入和存儲
- ❌ 協調模組工作流

正確的流程應該是：
Router → 啟動 CS → CS 提供會話上下文 → Router 調用 MEM/LLM → 
LLM 從 Working Context 獲取數據 → Router 轉送結果 → TTS 輸出 → CS 記錄結果
"""

from typing import Dict, Any, Optional, List
from datetime import datetime
from enum import Enum
import uuid

from utils.debug_helper import debug_log, info_log, error_log


class CSStatus(Enum):
    """CS 狀態"""
    INITIALIZING = "initializing"
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    ERROR = "error"


class ConversationTurn:
    """單輪對話記錄 - 簡化版本，只記錄不處理"""
    
    def __init__(self, turn_id: str):
        self.turn_id = turn_id
        self.timestamp = datetime.now()
        self.user_input: Optional[Dict[str, Any]] = None
        self.system_response: Optional[Dict[str, Any]] = None
        self.processing_time: Optional[float] = None
        self.metadata: Dict[str, Any] = {}
        
    def record_input(self, user_input: Dict[str, Any]):
        """記錄使用者輸入"""
        self.user_input = user_input
        
    def record_response(self, response: Dict[str, Any], processing_time: Optional[float] = None):
        """記錄系統回應"""
        self.system_response = response
        self.processing_time = processing_time
        
    def add_metadata(self, key: str, value: Any):
        """添加元數據"""
        self.metadata[key] = value
        
    def to_dict(self) -> Dict[str, Any]:
        """轉換為字典格式"""
        return {
            "turn_id": self.turn_id,
            "timestamp": self.timestamp.isoformat(),
            "user_input": self.user_input,
            "system_response": self.system_response,
            "processing_time": self.processing_time,
            "metadata": self.metadata
        }


class ChattingSession:
    """
    對話會話 - 重構版本
    
    職責：
    - 會話生命週期管理
    - 對話輪次記錄
    - 會話元數據維護
    - 提供會話上下文信息
    
    不負責：
    - 模組調用（由 Router 處理）
    - 數據處理（由各模組處理）
    - 工作流協調（由 Router 和 Working Context 處理）
    """
    
    def __init__(self, session_id: str, gs_session_id: str, 
                 identity_context: Optional[Dict[str, Any]] = None):
        self.session_id = session_id
        self.gs_session_id = gs_session_id
        self.identity_context = identity_context or {}
        
        self.status = CSStatus.INITIALIZING
        self.created_at = datetime.now()
        self.last_activity = self.created_at
        self.ended_at: Optional[datetime] = None
        
        # 待結束標記 - 符合會話生命週期架構
        # 設置後會在循環完成邊界時終止會話
        self.pending_end = False
        self.pending_end_reason: Optional[str] = None
        
        # 對話記錄
        self.conversation_turns: List[ConversationTurn] = []
        self.turn_counter = 0
        
        # 會話標識和元數據
        self.memory_token = self._generate_memory_token()
        self.session_metadata: Dict[str, Any] = {
            "conversation_mode": "chatting",
            "user_id": self.identity_context.get("user_id", "default_user"),
            "personality": self.identity_context.get("personality", "default"),
            "language": self.identity_context.get("language", "zh-TW")
        }
        
        # 會話配置
        self.config = {
            "max_turns": 50,  # 最大對話輪數
            "context_window": 10,  # 上下文視窗大小（供模組參考）
            "auto_summarize_interval": 20,  # 自動總結間隔（供模組參考）
        }
        
        # 會話統計
        self.stats = {
            "total_turns": 0,
            "total_processing_time": 0.0,
            "avg_processing_time": 0.0,
            "errors": 0
        }
        
        self._initialize()
        
    def _generate_memory_token(self) -> str:
        """生成記憶標識符"""
        return f"cs_{self.session_id}_{int(self.created_at.timestamp())}"
    
    def _initialize(self):
        """初始化會話"""
        try:
            self.status = CSStatus.ACTIVE
            info_log(f"[ChattingSession] CS 初始化完成: {self.session_id}")
            debug_log(2, f"  └─ GS: {self.gs_session_id}")
            debug_log(2, f"  └─ 記憶標識: {self.memory_token}")
            debug_log(2, f"  └─ 使用者ID: {self.session_metadata['user_id']}")
            
        except Exception as e:
            error_log(f"[ChattingSession] CS 初始化失敗: {e}")
            self.status = CSStatus.ERROR
    
    def start_turn(self) -> str:
        """
        開始新的對話輪次
        
        Returns:
            turn_id: 對話輪次ID
        """
        if self.status != CSStatus.ACTIVE:
            error_log(f"[ChattingSession] 無法開始新輪次，會話狀態: {self.status.value}")
            return ""
        
        try:
            self.turn_counter += 1
            turn_id = f"turn_{self.turn_counter}"
            
            # 創建新的對話輪次記錄
            conversation_turn = ConversationTurn(turn_id)
            self.conversation_turns.append(conversation_turn)
            
            self.last_activity = datetime.now()
            
            debug_log(2, f"[ChattingSession] 開始對話輪次: {turn_id}")
            
            return turn_id
            
        except Exception as e:
            error_log(f"[ChattingSession] 開始輪次失敗: {e}")
            return ""
    
    def record_input(self, turn_id: str, user_input: Dict[str, Any]):
        """
        記錄使用者輸入
        
        Args:
            turn_id: 對話輪次ID
            user_input: 使用者輸入數據
        """
        try:
            turn = self._get_turn(turn_id)
            if turn:
                turn.record_input(user_input)
                self.last_activity = datetime.now()
                debug_log(3, f"[ChattingSession] 記錄輸入: {turn_id}")
            else:
                error_log(f"[ChattingSession] 找不到對話輪次: {turn_id}")
                
        except Exception as e:
            error_log(f"[ChattingSession] 記錄輸入失敗: {e}")
    
    def record_response(self, turn_id: str, response: Dict[str, Any], 
                       processing_time: Optional[float] = None):
        """
        記錄系統回應
        
        Args:
            turn_id: 對話輪次ID
            response: 系統回應數據
            processing_time: 處理時間（秒）
        """
        try:
            turn = self._get_turn(turn_id)
            if turn:
                turn.record_response(response, processing_time)
                self.last_activity = datetime.now()
                
                # 更新統計
                self.stats["total_turns"] += 1
                if processing_time:
                    self.stats["total_processing_time"] += processing_time
                    self.stats["avg_processing_time"] = (
                        self.stats["total_processing_time"] / self.stats["total_turns"]
                    )
                
                debug_log(3, f"[ChattingSession] 記錄回應: {turn_id}")
            else:
                error_log(f"[ChattingSession] 找不到對話輪次: {turn_id}")
                
        except Exception as e:
            error_log(f"[ChattingSession] 記錄回應失敗: {e}")
    
    def record_error(self, turn_id: str, error_info: Dict[str, Any]):
        """
        記錄錯誤
        
        Args:
            turn_id: 對話輪次ID（可選）
            error_info: 錯誤信息
        """
        try:
            if turn_id:
                turn = self._get_turn(turn_id)
                if turn:
                    turn.add_metadata("error", error_info)
            
            self.stats["errors"] += 1
            error_log(f"[ChattingSession] 記錄錯誤: {error_info.get('message', 'Unknown error')}")
            
        except Exception as e:
            error_log(f"[ChattingSession] 記錄錯誤失敗: {e}")
    
    def get_session_context(self) -> Dict[str, Any]:
        """
        獲取會話上下文信息（供模組使用）
        
        Returns:
            會話上下文數據
        """
        recent_turns = self.conversation_turns[-self.config["context_window"]:]
        
        return {
            "session_id": self.session_id,
            "gs_session_id": self.gs_session_id,
            "memory_token": self.memory_token,
            "identity_context": self.identity_context,
            "session_metadata": self.session_metadata,
            "turn_counter": self.turn_counter,
            "recent_turns": [turn.to_dict() for turn in recent_turns],
            "config": self.config.copy(),
            "stats": self.stats.copy(),
            "status": self.status.value,
            "created_at": self.created_at.isoformat(),
            "last_activity": self.last_activity.isoformat()
        }
    
    def get_turn(self, turn_id: str) -> Optional[Dict[str, Any]]:
        """
        獲取特定對話輪次數據
        
        Args:
            turn_id: 對話輪次ID
            
        Returns:
            對話輪次數據（字典格式）
        """
        turn = self._get_turn(turn_id)
        return turn.to_dict() if turn else None
    
    def get_recent_turns(self, count: Optional[int] = None) -> List[Dict[str, Any]]:
        """
        獲取最近的對話輪次
        
        Args:
            count: 要獲取的輪次數量，None 則使用配置的 context_window
            
        Returns:
            對話輪次列表
        """
        if count is None:
            count = self.config["context_window"]
        
        recent_turns = self.conversation_turns[-count:]
        return [turn.to_dict() for turn in recent_turns]
    
    def update_metadata(self, key: str, value: Any):
        """
        更新會話元數據
        
        Args:
            key: 元數據鍵
            value: 元數據值
        """
        self.session_metadata[key] = value
        debug_log(3, f"[ChattingSession] 更新元數據: {key} = {value}")
    
    def pause(self):
        """暫停會話"""
        if self.status == CSStatus.ACTIVE:
            self.status = CSStatus.PAUSED
            info_log(f"[ChattingSession] CS 已暫停: {self.session_id}")
    
    def resume(self):
        """恢復會話"""
        if self.status == CSStatus.PAUSED:
            self.status = CSStatus.ACTIVE
            self.last_activity = datetime.now()
            info_log(f"[ChattingSession] CS 已恢復: {self.session_id}")
    
    def end(self, reason: str = "normal") -> Dict[str, Any]:
        """
        結束會話
        
        Args:
            reason: 結束原因
            
        Returns:
            會話總結數據
        """
        try:
            self.status = CSStatus.COMPLETED
            self.ended_at = datetime.now()
            
            duration = (self.ended_at - self.created_at).total_seconds()
            
            summary = {
                "session_id": self.session_id,
                "gs_session_id": self.gs_session_id,
                "memory_token": self.memory_token,
                "duration": duration,
                "total_turns": self.turn_counter,
                "stats": self.stats.copy(),
                "end_reason": reason,
                "created_at": self.created_at.isoformat(),
                "ended_at": self.ended_at.isoformat()
            }
            
            info_log(f"[ChattingSession] CS 已結束: {self.session_id}")
            info_log(f"  └─ 持續時間: {duration:.1f}秒")
            info_log(f"  └─ 對話輪次: {self.turn_counter}")
            info_log(f"  └─ 平均處理時間: {self.stats['avg_processing_time']:.2f}秒")
            
            # 發布會話結束事件 - 通知 StateManager 處理狀態轉換
            try:
                from core.event_bus import event_bus, SystemEvent
                from core.working_context import working_context_manager
                
                # ✅ 讀取當前 cycle_index（會話在循環結束後才真正結束，值已正確更新）
                current_cycle = working_context_manager.global_context_data.get('current_cycle_index', 0)
                debug_log(1, f"[ChattingSession] 📍 發布 SESSION_ENDED: session={self.session_id}, cycle={current_cycle}")
                
                event_bus.publish(
                    event_type=SystemEvent.SESSION_ENDED,
                    data={
                        'session_id': self.session_id,
                        'session_type': 'chatting',
                        'reason': reason,
                        'duration': duration,
                        'total_turns': self.turn_counter,
                        'cycle_index': current_cycle  # ✅ 使用當前 cycle_index
                    },
                    source='chatting_session'
                )
                debug_log(2, f"[ChattingSession] 已發布 SESSION_ENDED 事件: {self.session_id}")
            except Exception as e:
                error_log(f"[ChattingSession] 發布會話結束事件失敗: {e}")
            
            return summary
            
        except Exception as e:
            error_log(f"[ChattingSession] 結束會話失敗: {e}")
            return {}
    
    def _get_turn(self, turn_id: str) -> Optional[ConversationTurn]:
        """獲取對話輪次對象"""
        for turn in self.conversation_turns:
            if turn.turn_id == turn_id:
                return turn
        return None
    
    def get_summary(self) -> Dict[str, Any]:
        """
        獲取會話總結
        
        Returns:
            會話總結數據
        """
        duration = (
            (self.ended_at or datetime.now()) - self.created_at
        ).total_seconds()
        
        return {
            "session_id": self.session_id,
            "gs_session_id": self.gs_session_id,
            "status": self.status.value,
            "duration": duration,
            "total_turns": self.turn_counter,
            "stats": self.stats.copy(),
            "identity_context": self.identity_context,
            "session_metadata": self.session_metadata,
            "created_at": self.created_at.isoformat(),
            "last_activity": self.last_activity.isoformat(),
            "ended_at": self.ended_at.isoformat() if self.ended_at else None
        }


class ChattingSessionManager:
    """
    Chatting Session 管理器
    
    負責創建、追蹤和管理 CS 實例
    """
    
    def __init__(self):
        self.sessions: Dict[str, ChattingSession] = {}
        self.active_session_id: Optional[str] = None
        
        info_log("[ChattingSessionManager] CS 管理器已初始化")
    
    def create_session(self, gs_session_id: str, 
                      identity_context: Optional[Dict[str, Any]] = None) -> str:
        """
        創建新的 CS
        
        Args:
            gs_session_id: 所屬的 GS ID
            identity_context: 身份上下文
            
        Returns:
            session_id: CS ID
        """
        session_id = f"cs_{uuid.uuid4().hex[:8]}"
        
        session = ChattingSession(session_id, gs_session_id, identity_context)
        self.sessions[session_id] = session
        self.active_session_id = session_id
        
        info_log(f"[ChattingSessionManager] 創建 CS: {session_id}")
        
        return session_id
    
    def get_session(self, session_id: str) -> Optional[ChattingSession]:
        """獲取 CS 實例"""
        return self.sessions.get(session_id)
    
    def get_active_session(self) -> Optional[ChattingSession]:
        """獲取當前活躍的 CS"""
        if self.active_session_id:
            return self.sessions.get(self.active_session_id)
        return None
    
    def get_active_sessions(self) -> List[ChattingSession]:
        """獲取所有活躍的 CS（狀態為 ACTIVE）"""
        return [
            session for session in self.sessions.values()
            if session.status == CSStatus.ACTIVE
        ]
    
    def end_session(self, session_id: str, reason: str = "normal") -> Dict[str, Any]:
        """結束 CS"""
        session = self.sessions.get(session_id)
        if session:
            summary = session.end(reason)
            
            if self.active_session_id == session_id:
                self.active_session_id = None
            
            return summary
        
        return {}
    
    def cleanup_old_sessions(self, keep_recent: int = 10):
        """清理舊的已完成會話"""
        completed_sessions = [
            (sid, s) for sid, s in self.sessions.items()
            if s.status == CSStatus.COMPLETED
        ]
        
        # 按結束時間排序
        completed_sessions.sort(key=lambda x: x[1].ended_at or datetime.min)
        
        # 保留最近的會話，刪除其餘的
        if len(completed_sessions) > keep_recent:
            to_remove = completed_sessions[:-keep_recent]
            for session_id, _ in to_remove:
                del self.sessions[session_id]
                debug_log(2, f"[ChattingSessionManager] 清理舊會話: {session_id}")


# 全局 CS 管理器實例
chatting_session_manager = ChattingSessionManager()
