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

# 導入統一的狀態枚舉
from core.state_manager import UEPState

@dataclass
class StateQueueItem:
    """狀態佇列項目"""
    state: UEPState
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
            state=UEPState(data["state"]),
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
        UEPState.WORK: 100,     # 工作任務最高優先級
        UEPState.CHAT: 50,      # 聊天次之
        UEPState.MISCHIEF: 30,  # 惡作劇
        UEPState.SLEEP: 10,     # 睡眠
        UEPState.ERROR: 5,      # 錯誤狀態
        UEPState.IDLE: 0        # IDLE最低
    }
    
    def __init__(self, storage_path: Optional[Path] = None):
        """初始化狀態佇列管理器"""
        self.storage_path = storage_path or Path("memory/state_queue.json")
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        
        # 狀態佇列 (按優先級排序)
        self.queue: List[StateQueueItem] = []
        
        # 當前執行狀態
        self.current_state = UEPState.IDLE
        self.current_item: Optional[StateQueueItem] = None
        
        # 狀態處理回調
        self.state_handlers: Dict[UEPState, Callable] = {}
        self.completion_handlers: Dict[UEPState, Callable] = {}
        
        # 會話管理 - 延遲導入避免循環依賴
        self._chatting_session_manager = None
        self._session_manager = None
        
        # 載入持久化數據
        self._load_queue()
        
        # 註冊默認的狀態處理器
        self._register_default_handlers()
        
        info_log("[StateQueue] 狀態佇列管理器初始化完成")
    
    def register_state_handler(self, state: UEPState, handler: Callable):
        """註冊狀態處理器"""
        self.state_handlers[state] = handler
        debug_log(2, f"[StateQueue] 註冊狀態處理器: {state.name}")
    
    def register_completion_handler(self, state: UEPState, handler: Callable):
        """註冊狀態完成處理器"""
        self.completion_handlers[state] = handler
        debug_log(2, f"[StateQueue] 註冊完成處理器: {state.name}")
    
    def _register_default_handlers(self):
        """註冊默認的狀態處理器"""
        # 註冊 CHAT 狀態處理器
        self.register_state_handler(UEPState.CHAT, self._handle_chat_state)
        self.register_completion_handler(UEPState.CHAT, self._handle_chat_completion)
        
        # 註冊 WORK 狀態處理器
        self.register_state_handler(UEPState.WORK, self._handle_work_state)
        self.register_completion_handler(UEPState.WORK, self._handle_work_completion)
    
    def _get_chatting_session_manager(self):
        """獲取 Chatting Session 管理器 (延遲導入)"""
        if self._chatting_session_manager is None:
            try:
                from core.chatting_session import chatting_session_manager
                self._chatting_session_manager = chatting_session_manager
            except ImportError as e:
                error_log(f"[StateQueue] 無法導入 Chatting Session 管理器: {e}")
        return self._chatting_session_manager
    
    def _get_session_manager(self):
        """獲取 Session 管理器 (延遲導入)"""
        if self._session_manager is None:
            try:
                from core.session_manager import session_manager
                self._session_manager = session_manager
            except ImportError as e:
                error_log(f"[StateQueue] 無法導入 Session 管理器: {e}")
        return self._session_manager
    
    def _handle_chat_state(self, queue_item: StateQueueItem):
        """處理 CHAT 狀態 - 通知狀態管理器創建聊天會話並等待完成通知"""
        try:
            from core.state_manager import state_manager
            
            # 準備上下文信息
            context = {
                "initial_input": {
                    "type": "text",
                    "content": queue_item.context_content,
                    "metadata": queue_item.metadata
                },
                "trigger_content": queue_item.trigger_content,
                "queue_item_id": f"{queue_item.state.value}_{queue_item.created_at.timestamp()}",
                "state_queue_callback": self._on_chat_session_complete,  # 回調函數
                **queue_item.metadata
            }
            
            # 通知狀態管理器創建聊天會話
            state_manager.set_state(UEPState.CHAT, context)
            
            info_log(f"[StateQueue] CHAT 狀態啟動: {queue_item.context_content[:50]}...")
            debug_log(4, f"[StateQueue] 等待聊天會話完成...")
            
            # 不立即完成狀態，等待會話完成回調
            
        except Exception as e:
            error_log(f"[StateQueue] 處理 CHAT 狀態時發生錯誤: {e}")
            self.complete_current_state(success=False, result_data={"error": str(e)})
    
    def _on_chat_session_complete(self, session_id: str, success: bool, result_data: Dict[str, Any] = None):
        """聊天會話完成回調"""
        try:
            info_log(f"[StateQueue] 聊天會話完成: {session_id} ({'成功' if success else '失敗'})")
            debug_log(4, f"[StateQueue] 會話結果: {result_data}")
            
            # 現在才標記狀態完成
            self.complete_current_state(success=success, result_data=result_data or {})
            
        except Exception as e:
            error_log(f"[StateQueue] 處理聊天會話完成回調時發生錯誤: {e}")
            self.complete_current_state(success=False, result_data={"error": str(e)})
    
    def _handle_chat_completion(self, queue_item: StateQueueItem, success: bool):
        """處理 CHAT 狀態完成"""
        try:
            chatting_manager = self._get_chatting_session_manager()
            
            cs_id = queue_item.metadata.get("chatting_session_id")
            
            if chatting_manager and cs_id:
                cs = chatting_manager.get_session(cs_id)
                if cs and cs.status.value in ["active", "paused"]:
                    # 結束 Chatting Session
                    session_summary = cs.end_session(save_memory=True)
                    
                    info_log(f"[StateQueue] CHAT 狀態完成，CS 已結束: {cs_id}")
                    debug_log(4, f"[StateQueue] CS 總結: {session_summary}")
                    
                    # 從活動會話中移除
                    chatting_manager.end_session(cs_id, save_memory=False)  # 已在 cs.end_session 中保存
            
        except Exception as e:
            error_log(f"[StateQueue] 處理 CHAT 完成時發生錯誤: {e}")
    
    def _handle_work_state(self, queue_item: StateQueueItem):
        """處理 WORK 狀態 - 通知狀態管理器創建工作會話並等待完成通知"""
        try:
            from core.state_manager import state_manager
            
            # 確定工作流程類型
            intent_type = queue_item.metadata.get('intent_type', 'command')
            workflow_type = self._map_intent_to_workflow_type(intent_type)
            
            # 準備上下文信息
            context = {
                "workflow_type": workflow_type,
                "command": queue_item.context_content,
                "intent_type": intent_type,
                "trigger_content": queue_item.trigger_content,
                "queue_item_id": f"{queue_item.state.value}_{queue_item.created_at.timestamp()}",
                "state_queue_callback": self._on_work_session_complete,  # 回調函數
                **queue_item.metadata
            }
            
            # 通知狀態管理器創建工作會話
            state_manager.set_state(UEPState.WORK, context)
            
            info_log(f"[StateQueue] WORK 狀態啟動: {queue_item.context_content[:50]}...")
            debug_log(4, f"[StateQueue] 工作意圖: {intent_type}, 工作流程類型: {workflow_type}")
            debug_log(4, f"[StateQueue] 等待工作會話完成...")
            
            # 不立即完成狀態，等待會話完成回調
            
        except Exception as e:
            error_log(f"[StateQueue] 處理 WORK 狀態時發生錯誤: {e}")
            self.complete_current_state(success=False, result_data={"error": str(e)})
    
    def _on_work_session_complete(self, session_id: str, success: bool, result_data: Dict[str, Any] = None):
        """工作會話完成回調"""
        try:
            info_log(f"[StateQueue] 工作會話完成: {session_id} ({'成功' if success else '失敗'})")
            debug_log(4, f"[StateQueue] 會話結果: {result_data}")
            
            # 現在才標記狀態完成
            self.complete_current_state(success=success, result_data=result_data or {})
            
        except Exception as e:
            error_log(f"[StateQueue] 處理工作會話完成回調時發生錯誤: {e}")
            self.complete_current_state(success=False, result_data={"error": str(e)})
    
    def _map_intent_to_workflow_type(self, intent_type: str) -> str:
        """將意圖類型映射為工作流程類型"""
        mapping = {
            'command': 'single_command',
            'compound': 'multi_step_workflow',
            'query': 'data_query',
            'file_operation': 'file_processing',
            'system_command': 'system_operation'
        }
        return mapping.get(intent_type.lower(), 'single_command')
    
    def _handle_work_completion(self, queue_item: StateQueueItem, success: bool):
        """處理 WORK 狀態完成"""
        try:
            debug_log(4, f"[StateQueue] WORK 狀態完成: {'成功' if success else '失敗'}")
            
        except Exception as e:
            error_log(f"[StateQueue] 處理 WORK 完成時發生錯誤: {e}")
    
    def add_state(self, state: UEPState, trigger_content: str, 
                  context_content: Optional[str] = None,
                  trigger_user: Optional[str] = None, 
                  metadata: Optional[Dict[str, Any]] = None) -> bool:
        """添加狀態到佇列"""
        
        if state == UEPState.IDLE:
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
    
    def process_nlp_intents(self, intent_segments: List[Any]) -> List[UEPState]:
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
                'command': UEPState.WORK,
                'compound': UEPState.WORK,  # 複合指令也是工作
                'chat': UEPState.CHAT,      # 只有真正的chat意圖才需要對話處理
                'query': UEPState.WORK      # 查詢也算工作
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
    
    def get_next_state(self) -> Optional[UEPState]:
        """獲取下一個要執行的狀態"""
        if self.queue:
            next_item = self.queue[0]
            return next_item.state
        return UEPState.IDLE
    
    def start_next_state(self) -> bool:
        """開始執行下一個狀態"""
        if not self.queue:
            # 佇列為空，切換到IDLE
            if self.current_state != UEPState.IDLE:
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
        if self.current_state != UEPState.IDLE:
            old_state = self.current_state
            info_log(f"[StateQueue] 狀態切換: {old_state.value} -> IDLE")
            debug_log(4, "[StateQueue] 切換到 IDLE 狀態 - 佇列已空")
            self.current_state = UEPState.IDLE
            self.current_item = None
            
            # 調用IDLE處理器
            if UEPState.IDLE in self.state_handlers:
                try:
                    debug_log(4, "[StateQueue] 調用 IDLE 狀態處理器")
                    self.state_handlers[UEPState.IDLE](None)
                except Exception as e:
                    error_log(f"[StateQueue] IDLE處理器執行失敗: {e}")
    
    def get_queue_status(self) -> Dict[str, Any]:
        """獲取佇列狀態"""
        # 確保如果沒有正在執行的項目，狀態應該是IDLE
        if self.current_item is None and self.current_state != UEPState.IDLE:
            debug_log(4, f"[StateQueue] 修正狀態：沒有執行項目但狀態不是IDLE，從 {self.current_state.value} 修正為 IDLE")
            self.current_state = UEPState.IDLE
        
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
        self.current_state = UEPState.IDLE
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
                self.current_state = UEPState(data.get("current_state", "idle"))
                
                # 載入當前項目
                if data.get("current_item"):
                    self.current_item = StateQueueItem.from_dict(data["current_item"])
                else:
                    # 如果沒有當前執行項目，確保狀態是IDLE
                    self.current_state = UEPState.IDLE
                
                # 載入佇列
                self.queue = [StateQueueItem.from_dict(item) for item in data.get("queue", [])]
                
                info_log(f"[StateQueue] 載入佇列: {len(self.queue)} 個項目, 當前狀態: {self.current_state.value}")
                
        except Exception as e:
            error_log(f"[StateQueue] 載入佇列失敗: {e}")
            # 使用預設值
            self.current_state = UEPState.IDLE
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
