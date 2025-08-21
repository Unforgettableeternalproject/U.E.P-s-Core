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

@dataclass
class StateQueueItem:
    """狀態佇列項目"""
    state: SystemState
    trigger_content: str              # 觸發此狀態的原始內容
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
        
        # 載入持久化數據
        self._load_queue()
        
        info_log("[StateQueue] 狀態佇列管理器初始化完成")
    
    def register_state_handler(self, state: SystemState, handler: Callable):
        """註冊狀態處理器"""
        self.state_handlers[state] = handler
        debug_log(2, f"[StateQueue] 註冊狀態處理器: {state.value}")
    
    def register_completion_handler(self, state: SystemState, handler: Callable):
        """註冊狀態完成處理器"""
        self.completion_handlers[state] = handler
        debug_log(2, f"[StateQueue] 註冊完成處理器: {state.value}")
    
    def add_state(self, state: SystemState, trigger_content: str, 
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
        debug_log(3, f"[StateQueue] 觸發內容: {trigger_content[:50]}...")
        
        # 保存佇列
        self._save_queue()
        
        return True
    
    def process_nlp_intents(self, intent_segments: List[Any]) -> List[SystemState]:
        """處理NLP意圖分析結果，添加相應狀態到佇列"""
        added_states = []
        
        for segment in intent_segments:
            # 根據意圖類型決定系統狀態
            if hasattr(segment, 'intent'):
                intent_value = segment.intent.value if hasattr(segment.intent, 'value') else str(segment.intent)
            else:
                intent_value = str(segment.get('intent', 'unknown'))
            
            state_mapping = {
                'command': SystemState.WORK,
                'compound': SystemState.WORK,  # 複合指令也是工作
                'call': SystemState.CHAT,      # 呼叫通常是想聊天
                'chat': SystemState.CHAT,
                'query': SystemState.WORK      # 查詢也算工作
            }
            
            target_state = state_mapping.get(intent_value.lower())
            
            if target_state:
                # 獲取觸發內容
                if hasattr(segment, 'text'):
                    trigger_content = segment.text
                else:
                    trigger_content = segment.get('text', '未知內容')
                
                # 檢查是否已經有相同狀態在佇列中
                if not self._has_pending_state(target_state):
                    success = self.add_state(
                        state=target_state,
                        trigger_content=trigger_content,
                        metadata={
                            'intent_type': intent_value,
                            'confidence': getattr(segment, 'confidence', 0.0),
                            'entities': getattr(segment, 'entities', [])
                        }
                    )
                    
                    if success:
                        added_states.append(target_state)
                else:
                    debug_log(2, f"[StateQueue] 狀態 {target_state.value} 已在佇列中，跳過添加")
        
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
        self.current_state = next_item.state
        self.current_item = next_item
        
        info_log(f"[StateQueue] 開始執行狀態: {next_item.state.value}")
        debug_log(3, f"[StateQueue] 觸發內容: {next_item.trigger_content}")
        
        # 調用狀態處理器
        if next_item.state in self.state_handlers:
            try:
                self.state_handlers[next_item.state](next_item)
            except Exception as e:
                error_log(f"[StateQueue] 狀態處理器執行失敗: {e}")
                self.complete_current_state(success=False)
                return False
        
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
            info_log("[StateQueue] 切換到 IDLE 狀態")
            self.current_state = SystemState.IDLE
            self.current_item = None
            
            # 調用IDLE處理器
            if SystemState.IDLE in self.state_handlers:
                try:
                    self.state_handlers[SystemState.IDLE](None)
                except Exception as e:
                    error_log(f"[StateQueue] IDLE處理器執行失敗: {e}")
    
    def _has_pending_state(self, state: SystemState) -> bool:
        """檢查佇列中是否已有指定狀態"""
        return any(item.state == state for item in self.queue)
    
    def get_queue_status(self) -> Dict[str, Any]:
        """獲取佇列狀態"""
        return {
            "current_state": self.current_state.value,
            "current_item": self.current_item.to_dict() if self.current_item else None,
            "queue_length": len(self.queue),
            "pending_states": [item.state.value for item in self.queue],
            "queue_items": [item.to_dict() for item in self.queue]
        }
    
    def clear_queue(self):
        """清空佇列"""
        info_log("[StateQueue] 清空狀態佇列")
        self.queue.clear()
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
