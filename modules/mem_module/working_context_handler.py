# modules/mem_module/working_context_handler.py
"""
MEM模組的Working Context決策處理器

負責：
1. 處理記憶相關的上下文決策
2. 從Working Context獲取Identity資訊
3. 管理對話快照的創建時機
4. 處理記憶檢索請求
"""

from typing import Dict, Any, Optional, List
from core.working_context import ContextType, DecisionHandler
from utils.debug_helper import debug_log, info_log, error_log
from .schemas import (
    MemoryQuery, MemoryType, ConversationSnapshot, 
    LLMMemoryInstruction, MemoryOperationResult
)


class MemoryContextHandler(DecisionHandler):
    """記憶模組的Working Context決策處理器"""
    
    def __init__(self, memory_manager=None):
        self.memory_manager = memory_manager
        
    def can_handle(self, context_type: ContextType) -> bool:
        """檢查是否可以處理指定類型的上下文"""
        return context_type in [
            ContextType.CONVERSATION,           # 對話上下文 - 快照管理
            ContextType.IDENTITY_MANAGEMENT,    # 身份管理 - 記憶令牌同步
            ContextType.LEARNING,              # 學習模式 - 長期記憶更新
            ContextType.CROSS_MODULE_DATA      # 跨模組資料 - 記憶檢索
        ]
    
    def make_decision(self, context_data: Dict[str, Any]) -> Dict[str, Any]:
        """進行記憶相關決策"""
        try:
            context_type = context_data.get("context_type")
            
            if context_type == ContextType.CONVERSATION:
                return self._handle_conversation_context(context_data)
            elif context_type == ContextType.IDENTITY_MANAGEMENT:
                return self._handle_identity_context(context_data)
            elif context_type == ContextType.LEARNING:
                return self._handle_learning_context(context_data)
            elif context_type == ContextType.CROSS_MODULE_DATA:
                return self._handle_memory_query_context(context_data)
            
            return {"action": "no_action", "reason": "無法處理的上下文類型"}
            
        except Exception as e:
            error_log(f"[MemoryContextHandler] 決策過程異常: {e}")
            return {"action": "error", "reason": f"決策異常: {str(e)}"}
    
    def apply_decision(self, context_data: Dict[str, Any], decision: Dict[str, Any]) -> bool:
        """應用記憶決策"""
        try:
            action = decision.get("action")
            
            if action == "create_conversation_snapshot":
                return self._create_conversation_snapshot(context_data, decision)
            elif action == "archive_old_snapshots":
                return self._archive_old_snapshots(context_data, decision)
            elif action == "sync_identity_memory":
                return self._sync_identity_memory(context_data, decision)
            elif action == "update_learning_memory":
                return self._update_learning_memory(context_data, decision)
            elif action == "perform_memory_query":
                return self._perform_memory_query(context_data, decision)
            elif action == "no_action" or action == "error":
                return True  # 無動作或錯誤情況，認為已處理
            
            debug_log(2, f"[MemoryContextHandler] 未知動作: {action}")
            return False
            
        except Exception as e:
            error_log(f"[MemoryContextHandler] 決策應用失敗: {e}")
            return False
    
    def _handle_conversation_context(self, context_data: Dict[str, Any]) -> Dict[str, Any]:
        """處理對話上下文 - 決定是否需要創建快照"""
        conversation_data = context_data.get("data", [])
        metadata = context_data.get("metadata", {})
        
        # 檢查對話長度
        total_messages = len(conversation_data)
        if total_messages >= 10:  # 超過10條消息
            return {
                "action": "create_conversation_snapshot",
                "reason": f"對話消息數量達到快照閾值 ({total_messages})",
                "confidence": 0.9,
                "snapshot_data": conversation_data,
                "identity_token": metadata.get("identity_token"),
                "topic": metadata.get("current_topic", "未命名對話")
            }
        
        # 檢查活躍快照數量
        active_snapshots = metadata.get("active_snapshots", 0)
        if active_snapshots > 3:  # 超過3個活躍快照
            return {
                "action": "archive_old_snapshots",
                "reason": f"活躍快照數量過多 ({active_snapshots})",
                "confidence": 0.8,
                "max_active": 2,
                "identity_token": metadata.get("identity_token")
            }
        
        return {"action": "no_action", "reason": "對話上下文正常"}
    
    def _handle_identity_context(self, context_data: Dict[str, Any]) -> Dict[str, Any]:
        """處理身份管理上下文 - 同步身份記憶"""
        identity_data = context_data.get("data")
        metadata = context_data.get("metadata", {})
        
        if identity_data and "identity_token" in metadata:
            return {
                "action": "sync_identity_memory",
                "reason": "身份資訊更新，需要同步記憶系統",
                "confidence": 1.0,
                "identity_data": identity_data,
                "identity_token": metadata["identity_token"]
            }
        
        return {"action": "no_action", "reason": "身份上下文無需處理"}
    
    def _handle_learning_context(self, context_data: Dict[str, Any]) -> Dict[str, Any]:
        """處理學習上下文 - 更新長期記憶"""
        learning_data = context_data.get("data", [])
        metadata = context_data.get("metadata", {})
        
        if learning_data and len(learning_data) >= 5:  # 累積足夠學習資料
            return {
                "action": "update_learning_memory",
                "reason": f"學習資料累積足夠 ({len(learning_data)} 項)",
                "confidence": 0.8,
                "learning_data": learning_data,
                "identity_token": metadata.get("identity_token"),
                "learning_type": metadata.get("learning_type", "general")
            }
        
        return {"action": "no_action", "reason": "學習資料不足"}
    
    def _handle_memory_query_context(self, context_data: Dict[str, Any]) -> Dict[str, Any]:
        """處理記憶查詢上下文"""
        query_data = context_data.get("data")
        metadata = context_data.get("metadata", {})
        
        if query_data and "query_text" in query_data:
            return {
                "action": "perform_memory_query",
                "reason": "接收到記憶查詢請求",
                "confidence": 1.0,
                "query_data": query_data,
                "identity_token": metadata.get("identity_token")
            }
        
        return {"action": "no_action", "reason": "查詢資料不完整"}
    
    def _create_conversation_snapshot(self, context_data: Dict[str, Any], decision: Dict[str, Any]) -> bool:
        """創建對話快照"""
        if not self.memory_manager:
            error_log("[MemoryContextHandler] 記憶管理器未初始化")
            return False
        
        try:
            identity_token = decision.get("identity_token")
            snapshot_data = decision.get("snapshot_data", [])
            topic = decision.get("topic", "未命名對話")
            
            # 組合對話內容
            conversation_text = "\n".join([
                str(msg) for msg in snapshot_data if msg
            ])
            
            snapshot = self.memory_manager.create_conversation_snapshot(
                identity_token=identity_token,
                conversation_text=conversation_text,
                topic=topic
            )
            
            if snapshot:
                info_log(f"[MemoryContextHandler] 成功創建對話快照: {snapshot.memory_id}")
                
                # 更新Working Context，標記快照已創建
                context_data["metadata"]["last_snapshot_id"] = snapshot.memory_id
                context_data["metadata"]["snapshot_created_at"] = snapshot.created_at.isoformat()
                
                return True
            
            return False
            
        except Exception as e:
            error_log(f"[MemoryContextHandler] 快照創建失敗: {e}")
            return False
    
    def _archive_old_snapshots(self, context_data: Dict[str, Any], decision: Dict[str, Any]) -> bool:
        """歸檔舊快照"""
        if not self.memory_manager:
            return False
        
        try:
            identity_token = decision.get("identity_token")
            max_active = decision.get("max_active", 2)
            
            # 這裡應該調用記憶管理器的歸檔方法
            # result = self.memory_manager.archive_old_snapshots(identity_token, max_active)
            
            debug_log(2, f"[MemoryContextHandler] 歸檔舊快照: {identity_token}")
            return True
            
        except Exception as e:
            error_log(f"[MemoryContextHandler] 快照歸檔失敗: {e}")
            return False
    
    def _sync_identity_memory(self, context_data: Dict[str, Any], decision: Dict[str, Any]) -> bool:
        """同步身份記憶"""
        if not self.memory_manager:
            return False
        
        try:
            identity_data = decision.get("identity_data")
            identity_token = decision.get("identity_token")
            
            # 這裡應該調用記憶管理器的身份同步方法
            # result = self.memory_manager.sync_identity_data(identity_token, identity_data)
            
            debug_log(2, f"[MemoryContextHandler] 同步身份記憶: {identity_token}")
            return True
            
        except Exception as e:
            error_log(f"[MemoryContextHandler] 身份記憶同步失敗: {e}")
            return False
    
    def _update_learning_memory(self, context_data: Dict[str, Any], decision: Dict[str, Any]) -> bool:
        """更新學習記憶"""
        if not self.memory_manager:
            return False
        
        try:
            learning_data = decision.get("learning_data", [])
            identity_token = decision.get("identity_token")
            learning_type = decision.get("learning_type", "general")
            
            # 這裡應該調用記憶管理器的學習記憶更新方法
            # result = self.memory_manager.update_learning_memory(identity_token, learning_data, learning_type)
            
            debug_log(2, f"[MemoryContextHandler] 更新學習記憶: {len(learning_data)} 項")
            return True
            
        except Exception as e:
            error_log(f"[MemoryContextHandler] 學習記憶更新失敗: {e}")
            return False
    
    def _perform_memory_query(self, context_data: Dict[str, Any], decision: Dict[str, Any]) -> bool:
        """執行記憶查詢"""
        if not self.memory_manager:
            return False
        
        try:
            query_data = decision.get("query_data", {})
            identity_token = decision.get("identity_token")
            
            # 創建記憶查詢對象
            memory_query = MemoryQuery(
                identity_token=identity_token,
                query_text=query_data.get("query_text", ""),
                memory_types=query_data.get("memory_types"),
                max_results=query_data.get("max_results", 10),
                similarity_threshold=query_data.get("similarity_threshold", 0.7)
            )
            
            # 執行查詢
            results = self.memory_manager.process_memory_query(memory_query)
            
            # 將結果存回Working Context
            context_data["query_results"] = [result.dict() for result in results]
            
            debug_log(2, f"[MemoryContextHandler] 記憶查詢完成: {len(results)} 個結果")
            return True
            
        except Exception as e:
            error_log(f"[MemoryContextHandler] 記憶查詢失敗: {e}")
            return False


def register_memory_context_handler(working_context_manager, memory_manager):
    """註冊記憶上下文處理器到Working Context系統"""
    try:
        handler = MemoryContextHandler(memory_manager)
        
        # 註冊處理器到各種上下文類型
        working_context_manager.register_decision_handler(ContextType.CONVERSATION, handler)
        working_context_manager.register_decision_handler(ContextType.IDENTITY_MANAGEMENT, handler)
        working_context_manager.register_decision_handler(ContextType.LEARNING, handler)
        working_context_manager.register_decision_handler(ContextType.CROSS_MODULE_DATA, handler)
        
        info_log("[MEM] 記憶上下文處理器已註冊到Working Context")
        return handler
        
    except Exception as e:
        error_log(f"[MEM] 記憶上下文處理器註冊失敗: {e}")
        return None
