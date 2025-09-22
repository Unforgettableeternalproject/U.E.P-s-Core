#!/usr/bin/env python3
"""
系統狀態佇列管理器
管理UEP系統的狀態切換與任務排程
"""

from enum import Enum
from typing import List, Optional, Dict, Any, Callable
from datetime import datetime
import json
from pathlib import Path
from dataclasses import dataclass
from utils.debug_helper import debug_log, info_log, error_log

class SystemState(Enum):
    """系統狀態定義"""
    IDLE = "idle"          # 待機狀態
    CHAT = "chat"          # 聊天狀態
    WORK = "work"          # 工作狀態
    MISCHIEF = "mischief"  # 惡作劇狀態 (未實現)
    SLEEP = "sleep"        # 睡眠狀態 (未實現)
    ERROR = "error"        # 錯誤狀態

@dataclass
class StateQueueItem:
    """狀態佇列項目"""
    state: SystemState
    trigger_content: str              # 觸發此狀態的原始內容
    context_content: str              # 狀態上下文內容 (該狀態需要處理的具體內容)
    trigger_user: Optional[str]       # 觸發用戶ID
    priority: int                     # 優先級 (數字越大優先級越高)
    metadata: Dict[str, Any]          # 額外元數據
    created_at: datetime
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    
    def to_dict(self) -> Dict[str, Any]:
        """轉換為字典格式"""
        return {
            "state": self.state.value,
            "trigger_content": self.trigger_content,
            "context_content": self.context_content,
            "trigger_user": self.trigger_user,
            "priority": self.priority,
            "metadata": self.metadata,
            "created_at": self.created_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'StateQueueItem':
        """從字典創建實例"""
        return cls(
            state=SystemState(data["state"]),
            trigger_content=data["trigger_content"],
            context_content=data.get("context_content", data["trigger_content"]),  # 向下相容
            trigger_user=data.get("trigger_user"),
            priority=data["priority"],
            metadata=data.get("metadata", {}),
            created_at=datetime.fromisoformat(data["created_at"]),
            started_at=datetime.fromisoformat(data["started_at"]) if data.get("started_at") else None,
            completed_at=datetime.fromisoformat(data["completed_at"]) if data.get("completed_at") else None
        )

class StateQueueManager:
    """系統狀態佇列管理器"""
    
    # 狀態優先級定義 (數字越大優先級越高)
    STATE_PRIORITIES = {
        SystemState.WORK: 100,     # 工作任務最高優先級
        SystemState.CHAT: 50,      # 聊天次之
        SystemState.MISCHIEF: 30,  # 惡作劇
        SystemState.SLEEP: 10,     # 睡眠
        SystemState.ERROR: 5,      # 錯誤狀態
        SystemState.IDLE: 0        # IDLE最低
    }
    
    def __init__(self, storage_path: Optional[Path] = None):
        """初始化狀態佇列管理器"""
        self.storage_path = storage_path or Path("memory/state_queue.json")
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        
        # 狀態佇列 (按優先級排序)
        self.queue: List[StateQueueItem] = []
        
        # 當前執行狀態
        self.current_state = SystemState.IDLE
        self.current_item: Optional[StateQueueItem] = None
        
        # 狀態處理回調
        self.state_handlers: Dict[SystemState, Callable] = {}
        self.completion_handlers: Dict[SystemState, Callable] = {}
        
        # 會話管理 - 延遲導入避免循環依賴
        self._chatting_session_manager = None
        self._session_record_manager = None
        
        # 載入持久化數據
        self._load_queue()
        
        # 註冊默認的狀態處理器
        self._register_default_handlers()
        
        info_log("[StateQueue] 狀態佇列管理器初始化完成")
    
    def register_state_handler(self, state: SystemState, handler: Callable):
        """註冊狀態處理器"""
        self.state_handlers[state] = handler
        debug_log(2, f"[StateQueue] 註冊狀態處理器: {state.value}")
    
    def register_completion_handler(self, state: SystemState, handler: Callable):
        """註冊狀態完成處理器"""
        self.completion_handlers[state] = handler
        debug_log(2, f"[StateQueue] 註冊完成處理器: {state.value}")
    
    def _register_default_handlers(self):
        """註冊默認的狀態處理器"""
        # 註冊 CHAT 狀態處理器
        self.register_state_handler(SystemState.CHAT, self._handle_chat_state)
        self.register_completion_handler(SystemState.CHAT, self._handle_chat_completion)
        
        # 註冊 WORK 狀態處理器
        self.register_state_handler(SystemState.WORK, self._handle_work_state)
        self.register_completion_handler(SystemState.WORK, self._handle_work_completion)
    
    def _get_chatting_session_manager(self):
        """獲取 Chatting Session 管理器 (延遲導入)"""
        if self._chatting_session_manager is None:
            try:
                from core.chatting_session import chatting_session_manager
                self._chatting_session_manager = chatting_session_manager
            except ImportError as e:
                error_log(f"[StateQueue] 無法導入 Chatting Session 管理器: {e}")
        return self._chatting_session_manager
    
    def _get_session_record_manager(self):
        """獲取會話記錄管理器 (延遲導入)"""
        if self._session_record_manager is None:
            try:
                from core.session_record import get_session_record_manager
                self._session_record_manager = get_session_record_manager()
            except ImportError as e:
                debug_log(2, f"[StateQueue] 會話記錄管理器未可用: {e}")
        return self._session_record_manager
    
    def _handle_chat_state(self, queue_item: StateQueueItem):
        """處理 CHAT 狀態 - 自動觸發 Chatting Session"""
        try:
            chatting_manager = self._get_chatting_session_manager()
            session_record_manager = self._get_session_record_manager()
            
            if chatting_manager:
                # 提取身份信息
                identity_context = {
                    "user_id": queue_item.trigger_user or "default_user",
                    "personality": queue_item.metadata.get("personality", "default"),
                    "preferences": queue_item.metadata.get("preferences", {})
                }
                
                # 創建 Chatting Session
                cs = chatting_manager.create_session(
                    gs_session_id=f"gs_{int(datetime.now().timestamp())}",
                    identity_context=identity_context
                )
                
                if cs:
                    # 記錄會話觸發
                    if session_record_manager:
                        session_record_manager.record_session_trigger(
                            session_type="CS",
                            session_id=cs.session_id,
                            trigger_content=queue_item.trigger_content,
                            context_content=queue_item.context_content,
                            metadata={
                                "queue_item_id": f"{queue_item.state.value}_{queue_item.created_at.timestamp()}",
                                "identity_context": identity_context,
                                **queue_item.metadata
                            }
                        )
                    
                    # 處理初始輸入
                    initial_input = {
                        "type": "text",
                        "content": queue_item.context_content,
                        "metadata": queue_item.metadata
                    }
                    
                    response = cs.process_input(initial_input)
                    
                    info_log(f"[StateQueue] CHAT 狀態觸發 CS: {cs.session_id}")
                    debug_log(4, f"[StateQueue] CS 回應: {response.get('response', {}).get('content', '')[:50]}...")
                    
                    # 更新佇列項目元數據
                    queue_item.metadata.update({
                        "chatting_session_id": cs.session_id,
                        "cs_response": response
                    })
                else:
                    error_log("[StateQueue] 無法創建 Chatting Session")
                    self.complete_current_state(success=False, result_data={"error": "無法創建 CS"})
            else:
                debug_log(2, "[StateQueue] Chatting Session 管理器不可用，跳過 CS 創建")
                
        except Exception as e:
            error_log(f"[StateQueue] 處理 CHAT 狀態時發生錯誤: {e}")
            self.complete_current_state(success=False, result_data={"error": str(e)})
    
    def _handle_chat_completion(self, queue_item: StateQueueItem, success: bool):
        """處理 CHAT 狀態完成"""
        try:
            chatting_manager = self._get_chatting_session_manager()
            session_record_manager = self._get_session_record_manager()
            
            cs_id = queue_item.metadata.get("chatting_session_id")
            
            if chatting_manager and cs_id:
                cs = chatting_manager.get_session(cs_id)
                if cs and cs.status.value in ["active", "paused"]:
                    # 結束 Chatting Session
                    session_summary = cs.end_session(save_memory=True)
                    
                    # 記錄會話完成
                    if session_record_manager:
                        session_record_manager.record_session_completion(
                            session_id=cs_id,
                            session_summary=session_summary,
                            success=success
                        )
                    
                    info_log(f"[StateQueue] CHAT 狀態完成，CS 已結束: {cs_id}")
                    debug_log(4, f"[StateQueue] CS 總結: {session_summary}")
                    
                    # 從活動會話中移除
                    chatting_manager.end_session(cs_id, save_memory=False)  # 已在 cs.end_session 中保存
            
        except Exception as e:
            error_log(f"[StateQueue] 處理 CHAT 完成時發生錯誤: {e}")
    
    def _handle_work_state(self, queue_item: StateQueueItem):
        """處理 WORK 狀態 - 觸發 Workflow Session"""
        try:
            session_record_manager = self._get_session_record_manager()
            
            # 記錄工作會話觸發
            if session_record_manager:
                session_record_manager.record_session_trigger(
                    session_type="WS",
                    session_id=f"ws_{int(datetime.now().timestamp())}",
                    trigger_content=queue_item.trigger_content,
                    context_content=queue_item.context_content,
                    metadata={
                        "queue_item_id": f"{queue_item.state.value}_{queue_item.created_at.timestamp()}",
                        **queue_item.metadata
                    }
                )
            
            info_log(f"[StateQueue] WORK 狀態處理: {queue_item.context_content[:50]}...")
            debug_log(4, f"[StateQueue] 工作意圖: {queue_item.metadata.get('intent_type', 'unknown')}")
            
            # 這裡可以添加實際的工作處理邏輯
            # 目前標記為成功完成
            self.complete_current_state(success=True, result_data={"work_processed": True})
            
        except Exception as e:
            error_log(f"[StateQueue] 處理 WORK 狀態時發生錯誤: {e}")
            self.complete_current_state(success=False, result_data={"error": str(e)})
    
    def _handle_work_completion(self, queue_item: StateQueueItem, success: bool):
        """處理 WORK 狀態完成"""
        try:
            session_record_manager = self._get_session_record_manager()
            
            if session_record_manager:
                # 記錄工作會話完成
                ws_id = queue_item.metadata.get("workflow_session_id", f"ws_{queue_item.created_at.timestamp()}")
                session_record_manager.record_session_completion(
                    session_id=ws_id,
                    session_summary={
                        "work_type": queue_item.metadata.get("intent_type", "unknown"),
                        "trigger_content": queue_item.trigger_content,
                        "context_content": queue_item.context_content,
                        "processing_result": queue_item.metadata.get("work_processed", False)
                    },
                    success=success
                )
            
            debug_log(4, f"[StateQueue] WORK 狀態完成: {'成功' if success else '失敗'}")
            
        except Exception as e:
            error_log(f"[StateQueue] 處理 WORK 完成時發生錯誤: {e}")
    
    def add_state(self, state: SystemState, trigger_content: str, 
                  context_content: Optional[str] = None,
                  trigger_user: Optional[str] = None, 
                  metadata: Optional[Dict[str, Any]] = None) -> bool:
        """添加狀態到佇列"""
        
        if state == SystemState.IDLE:
            debug_log(2, "[StateQueue] IDLE狀態不能手動添加到佇列")
            return False
        
        # 創建佇列項目
        priority = self.STATE_PRIORITIES.get(state, 0)
        queue_item = StateQueueItem(
            state=state,
            trigger_content=trigger_content,
            context_content=context_content or trigger_content,  # 如果沒有指定上下文，使用觸發內容
            trigger_user=trigger_user,
            priority=priority,
            metadata=metadata or {},
            created_at=datetime.now()
        )
        
        # 插入到正確位置 (按優先級排序)
        insert_index = 0
        for i, existing_item in enumerate(self.queue):
            if existing_item.priority < priority:
                insert_index = i
                break
            insert_index = i + 1
        
        self.queue.insert(insert_index, queue_item)
        
        info_log(f"[StateQueue] 添加狀態 {state.value} 到佇列 (優先級: {priority}, 位置: {insert_index})")
        debug_log(4, f"[StateQueue] 觸發內容: {trigger_content}")
        debug_log(4, f"[StateQueue] 上下文內容: {context_content or trigger_content}")
        
        # 保存佇列
        self._save_queue()
        
        return True
    
    def process_nlp_intents(self, intent_segments: List[Any]) -> List[SystemState]:
        """處理NLP意圖分析結果，添加相應狀態到佇列"""
        added_states = []
        
        debug_log(4, f"[StateQueue] 處理 {len(intent_segments)} 個意圖分段")
        
        for i, segment in enumerate(intent_segments):
            # 根據意圖類型決定系統狀態
            if hasattr(segment, 'intent'):
                intent_value = segment.intent.value if hasattr(segment.intent, 'value') else str(segment.intent)
            else:
                intent_value = str(segment.get('intent', 'unknown'))
            
            state_mapping = {
                'command': SystemState.WORK,
                'compound': SystemState.WORK,  # 複合指令也是工作
                'chat': SystemState.CHAT,      # 只有真正的chat意圖才需要對話處理
                'query': SystemState.WORK      # 查詢也算工作
                # 注意：'call' 意圖不加入佇列，因為它只是呼叫而不需要狀態處理
            }
            
            target_state = state_mapping.get(intent_value.lower())
            
            if target_state:
                # 獲取觸發內容和上下文內容
                if hasattr(segment, 'text'):
                    context_content = segment.text
                else:
                    context_content = segment.get('text', '未知內容')
                
                # 觸發內容包含分段信息以便追蹤
                trigger_content = f"意圖分段 {i+1}: {context_content}"
                
                # 支援多個相同狀態 - 每個分段都獨立加入佇列
                success = self.add_state(
                    state=target_state,
                    trigger_content=trigger_content,
                    context_content=context_content,  # 這是該狀態實際要處理的內容
                    metadata={
                        'intent_type': intent_value,
                        'confidence': getattr(segment, 'confidence', 0.0),
                        'entities': getattr(segment, 'entities', []),
                        'segment_index': i,
                        'segment_id': getattr(segment, 'segment_id', f'seg_{i}')
                    }
                )
                
                if success:
                    added_states.append(target_state)
                    debug_log(4, f"[StateQueue] 分段 {i+1} -> {target_state.value}: '{context_content}'")
            else:
                if intent_value.lower() == 'call':
                    debug_log(4, f"[StateQueue] 分段 {i+1} 是 call 意圖，不加入狀態佇列: '{segment.get('text', '未知內容') if hasattr(segment, 'get') else getattr(segment, 'text', '未知內容')}'")
                else:
                    debug_log(4, f"[StateQueue] 忽略未知意圖類型: {intent_value}")
        
        debug_log(4, f"[StateQueue] 總共添加 {len(added_states)} 個狀態到佇列")
        return added_states
    
    def get_next_state(self) -> Optional[SystemState]:
        """獲取下一個要執行的狀態"""
        if self.queue:
            next_item = self.queue[0]
            return next_item.state
        return SystemState.IDLE
    
    def start_next_state(self) -> bool:
        """開始執行下一個狀態"""
        if not self.queue:
            # 佇列為空，切換到IDLE
            if self.current_state != SystemState.IDLE:
                self._transition_to_idle()
            return False
        
        # 獲取下一個項目
        next_item = self.queue.pop(0)
        next_item.started_at = datetime.now()
        
        # 切換狀態
        old_state = self.current_state
        self.current_state = next_item.state
        self.current_item = next_item
        
        info_log(f"[StateQueue] 狀態切換: {old_state.value} -> {next_item.state.value}")
        debug_log(4, f"[StateQueue] 開始執行狀態: {next_item.state.value}")
        debug_log(4, f"[StateQueue] 觸發內容: {next_item.trigger_content}")
        debug_log(4, f"[StateQueue] 上下文內容: {next_item.context_content}")
        debug_log(4, f"[StateQueue] 佇列剩餘: {len(self.queue)} 項目")
        
        # 調用狀態處理器
        if next_item.state in self.state_handlers:
            try:
                debug_log(4, f"[StateQueue] 調用狀態處理器: {next_item.state.value}")
                self.state_handlers[next_item.state](next_item)
            except Exception as e:
                error_log(f"[StateQueue] 狀態處理器執行失敗: {e}")
                self.complete_current_state(success=False)
                return False
        else:
            debug_log(4, f"[StateQueue] 狀態 {next_item.state.value} 沒有註冊處理器")
        
        self._save_queue()
        return True
    
    def complete_current_state(self, success: bool = True, result_data: Optional[Dict[str, Any]] = None):
        """完成當前狀態"""
        if not self.current_item:
            debug_log(2, "[StateQueue] 沒有正在執行的狀態")
            return
        
        # 標記完成
        self.current_item.completed_at = datetime.now()
        if result_data:
            self.current_item.metadata.update(result_data)
        
        completed_state = self.current_state
        info_log(f"[StateQueue] 完成狀態: {completed_state.value} ({'成功' if success else '失敗'})")
        
        # 調用完成處理器
        if completed_state in self.completion_handlers:
            try:
                self.completion_handlers[completed_state](self.current_item, success)
            except Exception as e:
                error_log(f"[StateQueue] 完成處理器執行失敗: {e}")
        
        # 清理當前狀態
        self.current_item = None
        
        # 自動開始下一個狀態
        if not self.start_next_state():
            self._transition_to_idle()
        
        self._save_queue()
    
    def _transition_to_idle(self):
        """切換到IDLE狀態"""
        if self.current_state != SystemState.IDLE:
            old_state = self.current_state
            info_log(f"[StateQueue] 狀態切換: {old_state.value} -> IDLE")
            debug_log(4, "[StateQueue] 切換到 IDLE 狀態 - 佇列已空")
            self.current_state = SystemState.IDLE
            self.current_item = None
            
            # 調用IDLE處理器
            if SystemState.IDLE in self.state_handlers:
                try:
                    debug_log(4, "[StateQueue] 調用 IDLE 狀態處理器")
                    self.state_handlers[SystemState.IDLE](None)
                except Exception as e:
                    error_log(f"[StateQueue] IDLE處理器執行失敗: {e}")
    
    def get_queue_status(self) -> Dict[str, Any]:
        """獲取佇列狀態"""
        # 確保如果沒有正在執行的項目，狀態應該是IDLE
        if self.current_item is None and self.current_state != SystemState.IDLE:
            debug_log(4, f"[StateQueue] 修正狀態：沒有執行項目但狀態不是IDLE，從 {self.current_state.value} 修正為 IDLE")
            self.current_state = SystemState.IDLE
        
        status = {
            "current_state": self.current_state.value,
            "current_item": self.current_item.to_dict() if self.current_item else None,
            "queue_length": len(self.queue),
            "pending_states": [item.state.value for item in self.queue],
            "queue_items": [item.to_dict() for item in self.queue]
        }
        
        debug_log(4, f"[StateQueue] 當前狀態: {self.current_state.value}")
        debug_log(4, f"[StateQueue] 佇列長度: {len(self.queue)}")
        if self.queue:
            debug_log(4, f"[StateQueue] 待處理狀態: {[item.state.value for item in self.queue]}")
        
        return status
    
    def clear_queue(self):
        """清空佇列並重置狀態檔案"""
        info_log("[StateQueue] 清空狀態佇列")
        self.queue.clear()
        
        # 確保當前狀態也被重置為IDLE
        self.current_state = SystemState.IDLE
        self.current_item = None
        
        # 保存空狀態到檔案
        self._save_queue()
    
    def _save_queue(self):
        """保存佇列到檔案"""
        try:
            data = {
                "current_state": self.current_state.value,
                "current_item": self.current_item.to_dict() if self.current_item else None,
                "queue": [item.to_dict() for item in self.queue],
                "saved_at": datetime.now().isoformat()
            }
            
            with open(self.storage_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                
        except Exception as e:
            error_log(f"[StateQueue] 保存佇列失敗: {e}")
    
    def _load_queue(self):
        """從檔案載入佇列"""
        try:
            if self.storage_path.exists():
                with open(self.storage_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                
                # 載入當前狀態
                self.current_state = SystemState(data.get("current_state", "idle"))
                
                # 載入當前項目
                if data.get("current_item"):
                    self.current_item = StateQueueItem.from_dict(data["current_item"])
                else:
                    # 如果沒有當前執行項目，確保狀態是IDLE
                    self.current_state = SystemState.IDLE
                
                # 載入佇列
                self.queue = [StateQueueItem.from_dict(item) for item in data.get("queue", [])]
                
                info_log(f"[StateQueue] 載入佇列: {len(self.queue)} 個項目, 當前狀態: {self.current_state.value}")
                
        except Exception as e:
            error_log(f"[StateQueue] 載入佇列失敗: {e}")
            # 使用預設值
            self.current_state = SystemState.IDLE
            self.current_item = None
            self.queue = []

# 全域狀態佇列管理器實例
_state_queue_manager = None

def get_state_queue_manager() -> StateQueueManager:
    """獲取全域狀態佇列管理器實例"""
    global _state_queue_manager
    if _state_queue_manager is None:
        _state_queue_manager = StateQueueManager()
    return _state_queue_manager
