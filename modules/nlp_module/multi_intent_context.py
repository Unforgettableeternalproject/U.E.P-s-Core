#!/usr/bin/env python3
"""
多意圖上下文管理器
解決狀態佇列中同類型狀態的上下文問題
"""

import uuid
from typing import Dict, Any, List, Optional, Tuple, Set
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from core.state_manager import UEPState
from utils.debug_helper import debug_log, info_log, error_log

class ContextType(Enum):
    """上下文類型"""
    COMMAND_CONTEXT = "command"  # 命令執行上下文
    CHAT_CONTEXT = "chat"        # 聊天話題上下文
    CALL_CONTEXT = "call"        # 呼叫喚醒上下文

@dataclass
class IntentContext:
    """意圖上下文"""
    context_id: str
    context_type: ContextType
    primary_intent: str
    
    # 上下文內容
    extracted_entities: Dict[str, Any] = field(default_factory=dict)
    task_description: str = ""
    conversation_topic: str = ""
    related_segments: List[Dict[str, Any]] = field(default_factory=list)
    
    # 執行狀態
    execution_status: str = "pending"  # pending, in_progress, completed, failed
    priority: int = 5  # 1-10, 數字越小優先級越高
    
    # 時間信息
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    
    # 依賴關係
    depends_on: Set[str] = field(default_factory=set)  # 依賴的其他上下文ID
    blocks: Set[str] = field(default_factory=set)      # 阻塞的其他上下文ID

@dataclass
class StateContextEntry:
    """狀態佇列條目（帶上下文）"""
    state: UEPState
    context: IntentContext
    
    # 佇列管理
    queue_priority: int = 5
    retry_count: int = 0
    max_retries: int = 3
    
    def __lt__(self, other):
        """支持優先佇列排序"""
        # 先按上下文優先級，再按佇列優先級
        return (self.context.priority, self.queue_priority) < (other.context.priority, other.queue_priority)

class MultiIntentContextManager:
    """多意圖上下文管理器"""
    
    def __init__(self):
        self.contexts: Dict[str, IntentContext] = {}
        self.state_queue: List[StateContextEntry] = []
        self.active_contexts: Set[str] = set()
        self.completed_contexts: Set[str] = set()
        
        # 上下文依賴圖
        self.dependency_graph: Dict[str, Set[str]] = {}
        
        info_log("[MultiContext] 多意圖上下文管理器初始化")
    
    def create_contexts_from_segments(self, segments: List[Dict[str, Any]], 
                                    user_text: str) -> List[IntentContext]:
        """從分段創建上下文"""
        contexts = []
        
        for i, segment in enumerate(segments):
            context_id = f"ctx_{uuid.uuid4().hex[:8]}"
            intent = segment['intent']
            text = segment['text']
            
            # 確定上下文類型
            if intent == 'command':
                context_type = ContextType.COMMAND_CONTEXT
                task_description = text
                conversation_topic = ""
            elif intent == 'chat':
                context_type = ContextType.CHAT_CONTEXT
                task_description = ""
                conversation_topic = text
            elif intent == 'call':
                context_type = ContextType.CALL_CONTEXT
                task_description = f"系統喚醒: {text}"
                conversation_topic = ""
            else:
                context_type = ContextType.COMMAND_CONTEXT
                task_description = text
                conversation_topic = ""
            
            # 提取實體（簡化版本）
            entities = self._extract_entities_from_segment(segment)
            
            # 計算優先級
            priority = self._calculate_context_priority(context_type, segment, i)
            
            context = IntentContext(
                context_id=context_id,
                context_type=context_type,
                primary_intent=intent,
                extracted_entities=entities,
                task_description=task_description,
                conversation_topic=conversation_topic,
                related_segments=[segment],
                priority=priority
            )
            
            contexts.append(context)
            self.contexts[context_id] = context
            
            debug_log(2, f"[MultiContext] 創建上下文: {context_id} ({context_type.value}) - {text[:30]}...")
        
        # 分析上下文間依賴關係
        self._analyze_context_dependencies(contexts, user_text)
        
        return contexts
    
    def _extract_entities_from_segment(self, segment: Dict[str, Any]) -> Dict[str, Any]:
        """從分段提取實體"""
        entities = {}
        text = segment['text'].lower()
        
        # 時間實體
        time_keywords = ['tomorrow', 'today', 'tonight', 'morning', 'afternoon', 'evening']
        for keyword in time_keywords:
            if keyword in text:
                entities['time'] = keyword
                break
        
        # 動作實體
        action_keywords = {
            'save': 'file_operation',
            'open': 'file_operation', 
            'create': 'file_operation',
            'delete': 'file_operation',
            'search': 'search_operation',
            'find': 'search_operation',
            'remind': 'reminder_operation',
            'schedule': 'calendar_operation',
            'play': 'media_operation',
            'stop': 'media_operation'
        }
        
        for keyword, category in action_keywords.items():
            if keyword in text:
                entities['action'] = keyword
                entities['action_category'] = category
                break
        
        # 對象實體
        if any(word in text for word in ['file', 'document', 'folder']):
            entities['object_type'] = 'file'
        elif any(word in text for word in ['music', 'song', 'audio']):
            entities['object_type'] = 'media'
        elif any(word in text for word in ['meeting', 'appointment', 'event']):
            entities['object_type'] = 'calendar'
        
        return entities
    
    def _calculate_context_priority(self, context_type: ContextType, 
                                  segment: Dict[str, Any], position: int) -> int:
        """計算上下文優先級"""
        base_priority = {
            ContextType.CALL_CONTEXT: 1,      # 最高優先級
            ContextType.COMMAND_CONTEXT: 3,   # 中等優先級
            ContextType.CHAT_CONTEXT: 7       # 較低優先級
        }
        
        priority = base_priority.get(context_type, 5)
        
        # 根據位置調整（前面的分段優先級略高）
        priority += position
        
        # 根據緊急關鍵詞調整
        text = segment['text'].lower()
        if any(word in text for word in ['urgent', 'emergency', 'immediately', 'now']):
            priority -= 2
        elif any(word in text for word in ['later', 'whenever', 'sometime']):
            priority += 2
        
        return max(1, min(10, priority))  # 限制在1-10範圍內
    
    def _analyze_context_dependencies(self, contexts: List[IntentContext], user_text: str):
        """分析上下文間的依賴關係"""
        # 簡單的依賴規則
        for i, context in enumerate(contexts):
            # 如果有CALL在前面，後面的COMMAND可能依賴於它
            if (context.context_type == ContextType.COMMAND_CONTEXT and 
                i > 0 and contexts[i-1].context_type == ContextType.CALL_CONTEXT):
                context.depends_on.add(contexts[i-1].context_id)
                contexts[i-1].blocks.add(context.context_id)
            
            # 如果有多個COMMAND，後面的可能依賴前面的完成
            if (context.context_type == ContextType.COMMAND_CONTEXT and 
                i > 0 and contexts[i-1].context_type == ContextType.COMMAND_CONTEXT):
                # 檢查是否有順序關係
                if any(word in user_text.lower() for word in ['then', 'after', 'next', 'and then']):
                    context.depends_on.add(contexts[i-1].context_id)
    
    def add_contexts_to_queue(self, contexts: List[IntentContext]) -> List[str]:
        """將上下文添加到狀態佇列"""
        added_context_ids = []
        
        for context in contexts:
            # 創建對應的系統狀態
            system_state = self._context_to_system_state(context)
            
            if system_state:
                # 創建佇列條目
                queue_entry = StateContextEntry(
                    state=system_state,
                    context=context,
                    queue_priority=context.priority
                )
                
                self.state_queue.append(queue_entry)
                self.active_contexts.add(context.context_id)
                added_context_ids.append(context.context_id)
                
                info_log(f"[MultiContext] 上下文已加入佇列: {context.context_id} "
                        f"({context.context_type.value}) 優先級={context.priority}")
        
        # 重新排序佇列
        self.state_queue.sort()
        
        return added_context_ids
    
    def _context_to_system_state(self, context: IntentContext) -> Optional[UEPState]:
        """將上下文轉換為系統狀態"""
        context_to_state_map = {
            ContextType.COMMAND_CONTEXT: UEPState.WORK,
            ContextType.CHAT_CONTEXT: UEPState.CHAT,
            ContextType.CALL_CONTEXT: UEPState.IDLE  # 由於UEPState沒有LISTENING，使用IDLE
        }
        
        return context_to_state_map.get(context.context_type)
    
    def get_next_executable_context(self) -> Optional[Tuple[UEPState, IntentContext]]:
        """獲取下一個可執行的上下文"""
        for i, entry in enumerate(self.state_queue):
            context = entry.context
            
            # 檢查依賴是否滿足
            if self._are_dependencies_satisfied(context):
                # 從佇列中移除
                self.state_queue.pop(i)
                self.active_contexts.discard(context.context_id)
                
                # 標記為進行中
                context.execution_status = "in_progress"
                context.updated_at = datetime.now()
                
                info_log(f"[MultiContext] 執行上下文: {context.context_id} "
                        f"({context.context_type.value})")
                
                return entry.state, context
        
        return None
    
    def _are_dependencies_satisfied(self, context: IntentContext) -> bool:
        """檢查上下文依賴是否滿足"""
        for dep_id in context.depends_on:
            if dep_id not in self.completed_contexts:
                debug_log(3, f"[MultiContext] 上下文 {context.context_id} "
                           f"等待依賴 {dep_id} 完成")
                return False
        return True
    
    def mark_context_completed(self, context_id: str, success: bool = True):
        """標記上下文為已完成"""
        if context_id in self.contexts:
            context = self.contexts[context_id]
            context.execution_status = "completed" if success else "failed"
            context.updated_at = datetime.now()
            
            self.completed_contexts.add(context_id)
            self.active_contexts.discard(context_id)
            
            info_log(f"[MultiContext] 上下文完成: {context_id} "
                    f"({context.context_type.value}) 成功={success}")
            
            # 檢查是否有被阻塞的上下文可以執行
            self._check_unblocked_contexts(context_id)
    
    def _check_unblocked_contexts(self, completed_context_id: str):
        """檢查因完成上下文而解除阻塞的其他上下文"""
        for entry in self.state_queue:
            if completed_context_id in entry.context.depends_on:
                debug_log(2, f"[MultiContext] 上下文 {entry.context.context_id} "
                           f"依賴已滿足，可以執行")
    
    def get_context_summary(self) -> Dict[str, Any]:
        """獲取上下文管理摘要"""
        return {
            "total_contexts": len(self.contexts),
            "active_contexts": len(self.active_contexts),
            "completed_contexts": len(self.completed_contexts),
            "queue_length": len(self.state_queue),
            "context_types": {
                ct.value: sum(1 for c in self.contexts.values() 
                             if c.context_type == ct)
                for ct in ContextType
            }
        }
    
    def get_context_details(self, context_id: str) -> Optional[Dict[str, Any]]:
        """獲取特定上下文的詳細信息"""
        if context_id in self.contexts:
            context = self.contexts[context_id]
            return {
                "context_id": context.context_id,
                "type": context.context_type.value,
                "intent": context.primary_intent,
                "description": context.task_description or context.conversation_topic,
                "entities": context.extracted_entities,
                "priority": context.priority,
                "status": context.execution_status,
                "dependencies": list(context.depends_on),
                "blocks": list(context.blocks),
                "created_at": context.created_at.isoformat(),
                "updated_at": context.updated_at.isoformat()
            }
        return None

# 全局單例
_multi_intent_context_manager = None

def get_multi_intent_context_manager() -> MultiIntentContextManager:
    """獲取多意圖上下文管理器單例"""
    global _multi_intent_context_manager
    if _multi_intent_context_manager is None:
        _multi_intent_context_manager = MultiIntentContextManager()
    return _multi_intent_context_manager
