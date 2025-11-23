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
from core.states.state_manager import UEPState

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
    work_mode: Optional[str] = None   # 工作模式: "direct", "background", None (Stage 4)
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
            "work_mode": self.work_mode,
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
            work_mode=data.get("work_mode"),
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
        
        # 🔧 記錄上次完成狀態的 cycle_index，用於計算下次狀態推進的 cycle
        self.last_completion_cycle: Optional[int] = None
        
        # 狀態處理回調
        self.state_handlers: Dict[UEPState, Callable] = {}
        self.completion_handlers: Dict[UEPState, Callable] = {}
        
        # 會話管理 - 延遲導入避免循環依賴
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
    
    def _get_session_manager(self):
        """獲取統一 Session 管理器 (延遲導入)"""
        if self._session_manager is None:
            try:
                from core.sessions.session_manager import session_manager
                self._session_manager = session_manager
            except ImportError as e:
                error_log(f"[StateQueue] 無法導入 Session 管理器: {e}")
        return self._session_manager
    
    def _handle_chat_state(self, queue_item: StateQueueItem):
        """處理 CHAT 狀態 - 通知狀態管理器創建聊天會話並等待完成通知"""
        try:
            from core.states.state_manager import state_manager
            
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

    def _on_chat_session_complete(self, session_id: str, success: bool, result_data: Dict[str, Any] = None):  # type: ignore
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
            session_manager = self._get_session_manager()
            
            cs_id = queue_item.metadata.get("chatting_session_id")
            
            if session_manager and cs_id:
                cs = session_manager.get_session(cs_id)
                if cs and hasattr(cs, 'status') and cs.status.value in ["active", "paused"]:
                    # 結束 Chatting Session
                    session_summary = session_manager.end_chatting_session(cs_id, save_memory=True)
                    
                    info_log(f"[StateQueue] CHAT 狀態完成，CS 已結束: {cs_id}")
                    debug_log(4, f"[StateQueue] CS 總結: {session_summary}")
                    
                    # 注意：end_chatting_session 已經處理了會話清理
            
        except Exception as e:
            error_log(f"[StateQueue] 處理 CHAT 完成時發生錯誤: {e}")
    
    def _handle_work_state(self, queue_item: StateQueueItem):
        """處理 WORK 狀態 - 通知狀態管理器創建工作會話並等待完成通知"""
        try:
            from core.states.state_manager import state_manager
            
            # 檢查是否為系統匯報模式（不需要工作流程）
            workflow_type = queue_item.metadata.get('workflow_type')
            is_system_report = workflow_type == 'system_report' or queue_item.metadata.get('system_report', False)
            
            if is_system_report:
                # 系統匯報模式：簡單對話，不啟動工作流程
                # 保持 WORK 狀態，但 workflow_type 為 None 表示不需要工作流程
                info_log(f"[StateQueue] WORK 狀態啟動（系統匯報模式）: {queue_item.context_content[:50]}...")
                debug_log(3, "[StateQueue] 系統匯報模式：保持 WORK 狀態但不啟動工作流程")
                
                # 準備上下文，明確標記為系統匯報（不啟動工作流程）
                context = {
                    "workflow_type": None,  # 明確標記：不需要工作流程
                    "command": queue_item.context_content,
                    "trigger_content": queue_item.trigger_content,
                    "queue_item_id": f"{queue_item.state.value}_{queue_item.created_at.timestamp()}",
                    "state_queue_callback": self._on_work_session_complete,
                    "system_report": True,  # 標記為系統匯報
                    **queue_item.metadata
                }
                
                # 保持 WORK 狀態，讓 StateManager 處理無工作流的 WORK
                state_manager.set_state(UEPState.WORK, context)
                
            else:
                # 正常工作流程模式
                intent_type = queue_item.metadata.get('intent_type', 'command')
                if workflow_type is None:
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
    
    def _on_work_session_complete(self, session_id: str, success: bool, result_data: Dict[str, Any] = None): # type: ignore
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
        # 所有 WORK 狀態都使用工作流方式執行，不再有 single_command
        mapping = {
            'command': 'workflow_automation',
            'compound': 'workflow_automation',
            'query': 'workflow_automation',
            'file_operation': 'workflow_automation',
            'system_command': 'workflow_automation',
            'direct_work': 'workflow_automation',
            'background_work': 'workflow_automation'
        }
        return mapping.get(intent_type.lower(), 'workflow_automation')
    
    def _handle_work_completion(self, queue_item: StateQueueItem, success: bool):
        """處理 WORK 狀態完成"""
        try:
            debug_log(4, f"[StateQueue] WORK 狀態完成: {'成功' if success else '失敗'}")
            
        except Exception as e:
            error_log(f"[StateQueue] 處理 WORK 完成時發生錯誤: {e}")
    
    def interrupt_chat_for_work(self, command_task: str, 
                               trigger_user: Optional[str] = None,
                               metadata: Optional[Dict[str, Any]] = None) -> bool:
        """
        聊天中斷：當在 CHAT 狀態中檢測到明顯指令時，插入 WORK 狀態
        這會中斷當前的聊天並優先處理工作任務
        """
        try:
            debug_log(1, f"[StateQueue] 聊天中斷轉工作：{command_task[:50]}...")
            
            # 創建高優先級的 WORK 狀態項目
            interrupt_metadata = metadata or {}
            interrupt_metadata.update({
                "chat_interrupt": True,
                "interrupt_timestamp": datetime.now().isoformat(),
                "original_command": command_task
            })
            
            queue_item = StateQueueItem(
                state=UEPState.WORK,
                trigger_content=command_task,
                context_content=command_task,
                trigger_user=trigger_user,
                priority=200,  # 高於普通任務但不是最高緊急
                metadata=interrupt_metadata,
                created_at=datetime.now()
            )
            
            # 插入到佇列前面（優先處理）
            self.queue.insert(0, queue_item)
            
            info_log(f"[StateQueue] 聊天中斷已插入佇列 - 優先級: 200, 位置: 0")
            debug_log(2, f"[StateQueue] 工作任務: {command_task}")
            
            # 標記當前 CHAT 狀態需要中斷（如果有的話）
            if self.current_item and self.current_item.state == UEPState.CHAT:
                debug_log(1, "[StateQueue] 標記當前 CHAT 會話進行工作中斷")
                interrupt_metadata["interrupted_chat_session"] = True
            
            # 保存佇列
            self._save_queue()
            
            return True
            
        except Exception as e:
            error_log(f"[StateQueue] 聊天中斷處理失敗: {e}")
            return False
    
    def add_state(self, state: UEPState, trigger_content: str, 
                  context_content: Optional[str] = None,
                  trigger_user: Optional[str] = None, 
                  metadata: Optional[Dict[str, Any]] = None,
                  work_mode: Optional[str] = None,
                  custom_priority: Optional[int] = None) -> bool:
        """
        添加狀態到佇列
        
        Args:
            state: 目標狀態
            trigger_content: 觸發內容
            context_content: 上下文內容（可選，默認使用 trigger_content）
            trigger_user: 觸發用戶ID
            metadata: 額外元數據
            work_mode: 工作模式（Stage 4）- "direct", "background", None
            custom_priority: 自訂優先權（Stage 4）- 如果提供，覆蓋默認優先權
        
        Returns:
            bool: 是否成功添加
        """
        
        if state == UEPState.IDLE:
            debug_log(2, "[StateQueue] IDLE狀態不能手動添加到佇列")
            return False
        
        # 確定優先權（Stage 4 擴展）
        if custom_priority is not None:
            priority = custom_priority
            debug_log(3, f"[StateQueue] 使用自訂優先權: {priority}")
        else:
            priority = self.STATE_PRIORITIES.get(state, 0)
            # Stage 4: 工作模式調整優先權
            if work_mode == "direct":
                priority = max(priority, 100)  # 直接工作最高優先權
                debug_log(3, f"[StateQueue] 直接工作模式，優先權提升到: {priority}")
            elif work_mode == "background":
                priority = min(priority, 30)  # 背景工作降低優先權
                debug_log(3, f"[StateQueue] 背景工作模式，優先權降低到: {priority}")
        
        # 創建佇列項目
        queue_item = StateQueueItem(
            state=state,
            trigger_content=trigger_content,
            context_content=context_content or trigger_content,
            trigger_user=trigger_user,
            priority=priority,
            metadata=metadata or {},
            work_mode=work_mode,
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
        
        work_mode_str = f" (工作模式: {work_mode})" if work_mode else ""
        info_log(f"[StateQueue] 添加狀態 {state.value} 到佇列 (優先級: {priority}, 位置: {insert_index}){work_mode_str}")
        debug_log(4, f"[StateQueue] 觸發內容: {trigger_content}")
        debug_log(4, f"[StateQueue] 上下文內容: {context_content or trigger_content}")
        
        # 保存佇列
        self._save_queue()
        
        # ✅ 如果當前是 IDLE 狀態，自動處理下一個狀態
        if self.current_state == UEPState.IDLE and not self.current_item:
            debug_log(2, "[StateQueue] 當前 IDLE，自動處理下一個狀態")
            self.process_next_state()
        
        return True
    
    def process_next_state(self):
        """處理佇列中的下一個狀態"""
        try:
            # 檢查是否有待處理狀態
            if not self.queue:
                debug_log(3, "[StateQueue] 佇列為空，無狀態需要處理")
                return
            
            # 檢查當前是否正在處理狀態
            if self.current_item is not None:
                debug_log(3, f"[StateQueue] 正在處理 {self.current_state.value}，等待完成")
                return
            
            # 取出最高優先級的狀態
            next_item = self.queue.pop(0)
            self.current_item = next_item
            self.current_state = next_item.state
            next_item.started_at = datetime.now()
            
            info_log(f"[StateQueue] 開始處理狀態: {next_item.state.value} (優先級: {next_item.priority})")
            
            # 保存狀態
            self._save_queue()
            
            # 調用狀態處理器
            handler = self.state_handlers.get(next_item.state)
            if handler:
                handler(next_item)
            else:
                error_log(f"[StateQueue] 沒有註冊 {next_item.state.value} 的處理器")
                self.complete_current_state(success=False, result_data={"error": "No handler registered"})
            
        except Exception as e:
            error_log(f"[StateQueue] 處理下一個狀態失敗: {e}")
            if self.current_item:
                self.complete_current_state(success=False, result_data={"error": str(e)})
    
    
    def process_nlp_intents(self, intent_segments: List[Any]) -> List[UEPState]:
        """
        處理NLP意圖分析結果，添加相應狀態到佇列
        
        Stage 4: 支援 IntentSegment 類型，使用意圖優先權
        """
        added_states = []
        
        debug_log(4, f"[StateQueue] 處理 {len(intent_segments)} 個意圖分段")
        
        # 嘗試導入 IntentSegment 和 IntentType（Stage 4）
        try:
            from modules.nlp_module.intent_types import IntentSegment, IntentType
            has_stage4 = True
        except ImportError:
            has_stage4 = False
            debug_log(3, "[StateQueue] Stage 4 意圖類型未找到，使用舊版本處理")
        
        for i, segment in enumerate(intent_segments):
            # Stage 4: 支援 IntentSegment 類型
            if has_stage4 and isinstance(segment, IntentSegment):
                # 使用 IntentSegment 的新邏輯
                intent_type = segment.intent_type
                
                # 根據意圖類型決定系統狀態和工作模式
                if intent_type == IntentType.WORK:
                    target_state = UEPState.WORK
                    # work_mode 從 segment.metadata 獲取（NLP 已設定）
                    work_mode = segment.metadata.get('work_mode', 'direct') if segment.metadata else 'direct'
                    debug_log(3, f"[StateQueue] WORK 意圖，work_mode={work_mode}")
                elif intent_type == IntentType.CHAT:
                    target_state = UEPState.CHAT
                    work_mode = None
                elif intent_type == IntentType.RESPONSE:
                    # RESPONSE 意圖用於工作流回應
                    target_state = UEPState.WORK
                    work_mode = "direct"  # 工作流回應應立即處理
                    debug_log(3, f"[StateQueue] RESPONSE 意圖，視為 direct WORK")
                elif intent_type == IntentType.CALL:
                    # CALL 意圖不加入佇列
                    debug_log(4, f"[StateQueue] 分段 {i+1} 是 CALL 意圖，不加入狀態佇列")
                    continue
                else:
                    # UNKNOWN 或其他
                    debug_log(4, f"[StateQueue] 分段 {i+1} 是 {intent_type.value} 意圖，不加入佇列")
                    continue
                
                # 準備狀態 metadata（包括 degradation 標記）
                state_metadata = {
                    'intent_type': intent_type.value,
                    'confidence': segment.confidence,
                    'segment_index': i,
                    'stage4_segment': True
                }
                
                # 從 segment metadata 提取降級標記
                if segment.metadata:
                    if segment.metadata.get('degraded_from_work'):
                        state_metadata['degraded_from_work'] = segment.metadata['degraded_from_work']
                        state_metadata['original_intent'] = segment.metadata.get('original_intent')
                        state_metadata['degradation_reason'] = segment.metadata.get('degradation_reason')
                        debug_log(2, f"[StateQueue] 分段 {i+1} 包含降級標記，已傳遞到狀態 metadata")
                
                # 添加到佇列，使用 IntentSegment 的優先權
                success = self.add_state(
                    state=target_state,
                    trigger_content=f"意圖分段 {i+1}: {segment.segment_text}",
                    context_content=segment.segment_text,
                    work_mode=work_mode,
                    custom_priority=segment.priority,
                    metadata=state_metadata
                )
                
                if success:
                    added_states.append(target_state)
                    debug_log(4, f"[StateQueue] 分段 {i+1} -> {target_state.value} (優先權: {segment.priority}, 模式: {work_mode}): '{segment.segment_text[:50]}...'")
            
            else:
                # 舊版本邏輯（向下相容）
                if hasattr(segment, 'intent'):
                    intent_value = segment.intent.value if hasattr(segment.intent, 'value') else str(segment.intent)
                else:
                    intent_value = str(segment.get('intent', 'unknown'))
                
                state_mapping = {
                    'command': UEPState.WORK,
                    'compound': UEPState.WORK,
                    'chat': UEPState.CHAT,
                    'query': UEPState.WORK
                }
                
                target_state = state_mapping.get(intent_value.lower())
                
                if target_state:
                    if hasattr(segment, 'text'):
                        context_content = segment.text
                    else:
                        context_content = segment.get('text', '未知內容')
                    
                    trigger_content = f"意圖分段 {i+1}: {context_content}"
                    
                    # 準備狀態 metadata（包括 degradation 標記）
                    state_metadata = {
                        'intent_type': intent_value,
                        'confidence': getattr(segment, 'confidence', 0.0),
                        'entities': getattr(segment, 'entities', []),
                        'segment_index': i,
                        'segment_id': getattr(segment, 'segment_id', f'seg_{i}')
                    }
                    
                    # 從 segment metadata 提取降級標記（舊版本）
                    if isinstance(segment, dict):
                        segment_metadata = segment.get('metadata', {})
                    else:
                        segment_metadata = getattr(segment, 'metadata', {}) or {}
                    
                    if segment_metadata.get('degraded_from_work'):
                        state_metadata['degraded_from_work'] = segment_metadata['degraded_from_work']
                        state_metadata['original_intent'] = segment_metadata.get('original_intent')
                        state_metadata['degradation_reason'] = segment_metadata.get('degradation_reason')
                        debug_log(2, f"[StateQueue] 分段 {i+1} 包含降級標記，已傳遞到狀態 metadata")
                    
                    success = self.add_state(
                        state=target_state,
                        trigger_content=trigger_content,
                        context_content=context_content,
                        metadata=state_metadata
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
    
    def check_and_advance_state(self) -> bool:
        """檢查並推進到下一個狀態（由 SystemLoop 在循環開始時調用）
        
        檢查條件：
        1. 當前沒有執行中的狀態項目（current_item == None）
        2. 佇列中有待處理的狀態
        
        如果滿足條件，推進到下一個狀態並設置 skip_input_layer 標記。
        
        Returns:
            bool: 是否成功推進到下一個狀態
        """
        # 如果當前有執行中的狀態，不推進
        if self.current_item is not None:
            return False
        
        # 如果佇列為空，轉換到 IDLE
        if not self.queue:
            if self.current_state != UEPState.IDLE:
                self._transition_to_idle()
            return False
        
        # 有待處理的狀態，執行推進
        info_log(f"[StateQueue] 🔄 循環開始時檢測到待推進狀態，佇列長度: {len(self.queue)}")
        return self.start_next_state()
    
    def start_next_state(self) -> bool:
        """開始執行下一個狀態（內部方法）"""
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
        
        # ✅ 發布 STATE_ADVANCED 事件，通知 MC 跳過輸入層直接啟動處理層
        try:
            from core.event_bus import event_bus, SystemEvent
            from core.working_context import working_context_manager
            
            # ✅ 直接從 working_context 讀取當前 cycle_index（循環已完成，值已更新）
            # 不再使用 last_completion_cycle 計算，統一使用同一來源
            next_cycle = working_context_manager.global_context_data.get('current_cycle_index', 0)
            debug_log(1, f"[StateQueue] 🔢 STATE_ADVANCED: 使用當前 cycle_index={next_cycle}（循環已遞增）")
            
            event_bus.publish(
                event_type=SystemEvent.STATE_ADVANCED,
                data={
                    "old_state": old_state.value,
                    "new_state": next_item.state.value,
                    "content": next_item.context_content,
                    "trigger": next_item.trigger_content,
                    "metadata": next_item.metadata,
                    "cycle_index": next_cycle  # 使用下一個循環的 index
                },
                source="StateQueue"
            )
            debug_log(2, f"[StateQueue] ✅ 已發布 STATE_ADVANCED 事件: {old_state.value} -> {next_item.state.value} (cycle={next_cycle})")
        except Exception as e:
            error_log(f"[StateQueue] 發布 STATE_ADVANCED 事件失敗: {e}")
        
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
    
    def complete_current_state(self, success: bool = True, result_data: Optional[Dict[str, Any]] = None,
                              completion_cycle: Optional[int] = None):
        debug_log(1, f"[StateQueue] complete_current_state 被調用, completion_cycle={completion_cycle}")
        """完成當前狀態
        
        只標記當前狀態完成，不自動推進到下一個狀態。
        狀態推進由 SystemLoop 在循環開始時統一處理。
        
        Args:
            success: 是否成功完成
            result_data: 結果數據
            completion_cycle: 完成時的循環索引（優先使用此參數，避免讀取可能過期的 working_context）
        
        這確保：
        1. 清晰的循環邊界
        2. 可追蹤的狀態推進時機
        3. 避免在事件處理中嵌套過多邏輯
        """
        debug_log(1, f"[StateQueue] 📥 complete_current_state 被調用, completion_cycle={completion_cycle}")
        
        if not self.current_item:
            debug_log(2, "[StateQueue] 沒有正在執行的狀態")
            return
        
        # 🔧 記錄完成時的 cycle_index，供下次狀態推進使用
        try:
            if completion_cycle is not None:
                # ✅ 優先使用傳入的 cycle_index（來自 SESSION_ENDED 事件）
                self.last_completion_cycle = completion_cycle
                debug_log(3, f"[StateQueue] 狀態完成於 Cycle {completion_cycle} (來自會話事件)")
            else:
                # 🔧 回退到讀取 working_context（僅用於向後兼容）
                from core.working_context import working_context_manager
                completion_cycle = working_context_manager.global_context_data.get('current_cycle_index', 0)
                self.last_completion_cycle = completion_cycle
                debug_log(3, f"[StateQueue] 狀態完成於 Cycle {completion_cycle} (來自 working_context)")
        except Exception as e:
            error_log(f"[StateQueue] 記錄完成 cycle 失敗: {e}")
        
        # 標記完成
        self.current_item.completed_at = datetime.now()
        if result_data:
            self.current_item.metadata.update(result_data)
        
        completed_state = self.current_state
        info_log(f"[StateQueue] 完成狀態: {completed_state.value} ({'成功' if success else '失敗'})")
        debug_log(2, "[StateQueue] 等待下一個循環推進狀態...")
        
        # 調用完成處理器
        if completed_state in self.completion_handlers:
            try:
                self.completion_handlers[completed_state](self.current_item, success)
            except Exception as e:
                error_log(f"[StateQueue] 完成處理器執行失敗: {e}")
        
        # 清理當前狀態，但不自動推進
        self.current_item = None
        # current_state 保持原樣，等待 SystemLoop 推進
        
        self._save_queue()
    
    def _transition_to_idle(self):
        """切換到IDLE狀態"""
        if self.current_state != UEPState.IDLE:
            old_state = self.current_state
            info_log(f"[StateQueue] 狀態切換: {old_state.value} -> IDLE")
            debug_log(4, "[StateQueue] 切換到 IDLE 狀態 - 佇列已空")
            self.current_state = UEPState.IDLE
            self.current_item = None
            
            # ✅ 通知 StateManager 狀態已轉換到 IDLE
            try:
                from core.states.state_manager import state_manager
                state_manager.set_state(UEPState.IDLE, context=None)
                debug_log(2, "[StateQueue] 已通知 StateManager 轉換到 IDLE")
            except Exception as e:
                error_log(f"[StateQueue] 通知 StateManager 失敗: {e}")
            
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
