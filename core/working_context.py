# core/working_context.py
"""
工作上下文管理器 - 管理整個 UEP 程式生命周期中的大型工作階段

這個系統不同於 SYS 模組中的工作流會話，它是一個更高層級的上下文管理系統：
- 管理跨模組的工作上下文
- 提供樣本累積和決策觸發機制
- 支援可插拔的決策處理器
- 與 state_manager 和 router 協同工作
- 完全獨立於具體模組實現
"""

import time
import uuid
from typing import Dict, List, Any, Optional, Callable, Protocol
from enum import Enum, auto
from utils.debug_helper import debug_log, info_log, error_log
import threading


class ContextType(Enum):
    """工作上下文類型"""
    SPEAKER_ACCUMULATION = "speaker_accumulation"  # 語者樣本累積
    CONVERSATION = "conversation"                   # 對話上下文
    TASK_EXECUTION = "task_execution"              # 任務執行
    LEARNING = "learning"                          # 學習模式


class ContextStatus(Enum):
    """上下文狀態"""
    ACTIVE = auto()      # 活躍中
    PENDING = auto()     # 等待決策
    SUSPENDED = auto()   # 暫停
    COMPLETED = auto()   # 已完成
    EXPIRED = auto()     # 已過期


class DecisionHandler(Protocol):
    """決策處理器協議 - 定義決策處理器應該實現的介面"""
    
    def can_handle(self, context_type: ContextType) -> bool:
        """檢查是否可以處理指定類型的上下文"""
        ...
    
    def make_decision(self, context_data: Dict[str, Any]) -> Dict[str, Any]:
        """進行決策，返回決策結果"""
        ...
    
    def apply_decision(self, context_data: Dict[str, Any], decision: Dict[str, Any]) -> bool:
        """應用決策結果，返回是否成功"""
        ...


class ContextType(Enum):
    """工作上下文類型"""
    SPEAKER_ACCUMULATION = "speaker_accumulation"  # 語者樣本累積
    CONVERSATION = "conversation"                   # 對話上下文
    TASK_EXECUTION = "task_execution"              # 任務執行
    LEARNING = "learning"                          # 學習模式


class ContextStatus(Enum):
    """上下文狀態"""
    ACTIVE = auto()      # 活躍中
    PENDING = auto()     # 等待決策
    SUSPENDED = auto()   # 暫停
    COMPLETED = auto()   # 已完成
    EXPIRED = auto()     # 已過期


class WorkingContext:
    """單個工作上下文實例"""
    
    def __init__(self, context_id: str, context_type: ContextType, 
                 threshold: int = 15, timeout: float = 300.0):
        self.context_id = context_id
        self.context_type = context_type
        self.status = ContextStatus.ACTIVE
        self.created_at = time.time()
        self.last_activity = time.time()
        self.timeout = timeout  # 上下文超時時間（秒）
        
        # 累積數據相關
        self.data: List[Any] = []  # 改名為更通用的 data
        self.metadata: Dict[str, Any] = {}
        self.threshold = threshold
        
        # 決策相關
        self.pending_decision: Optional[Dict[str, Any]] = None
        self.decision_callback: Optional[Callable] = None
        
    def add_data(self, data_item: Any, metadata: Optional[Dict] = None):
        """添加數據到上下文 - 更通用的方法名"""
        self.data.append(data_item)
        self.last_activity = time.time()
        
        if metadata:
            self.metadata.update(metadata)
            
        debug_log(3, f"[WorkingContext] 數據添加到上下文 {self.context_id} "
                    f"(數據量: {len(self.data)}/{self.threshold})")
    
    def is_ready_for_decision(self) -> bool:
        """檢查是否達到決策條件"""
        return len(self.data) >= self.threshold
    
    def is_expired(self) -> bool:
        """檢查上下文是否已過期"""
        return (time.time() - self.last_activity) > self.timeout
    
    def get_context_info(self) -> Dict[str, Any]:
        """獲取上下文資訊"""
        return {
            "context_id": self.context_id,
            "type": self.context_type.value,
            "status": self.status.name,
            "data_count": len(self.data),
            "threshold": self.threshold,
            "created_at": self.created_at,
            "last_activity": self.last_activity,
            "is_ready": self.is_ready_for_decision(),
            "is_expired": self.is_expired(),
            "metadata": self.metadata.copy()
        }
    
    def get_decision_package(self) -> Dict[str, Any]:
        """獲取決策所需的數據包"""
        return {
            "context_id": self.context_id,
            "context_type": self.context_type,
            "data": self.data.copy(),
            "metadata": self.metadata.copy(),
            "threshold": self.threshold,
            "data_count": len(self.data)
        }


class WorkingContextManager:
    """工作上下文管理器 - 全局單例，完全獨立於具體模組"""
    
    _instance = None
    _lock = threading.Lock()
    
    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(self):
        if hasattr(self, '_initialized'):
            return
            
        self._initialized = True
        self.contexts: Dict[str, WorkingContext] = {}
        self.active_contexts_by_type: Dict[ContextType, str] = {}
        
        # 決策處理器註冊表
        self.decision_handlers: Dict[ContextType, DecisionHandler] = {}
        
        # 通用回調機制
        self.inquiry_callback: Optional[Callable] = None
        self.notification_callback: Optional[Callable] = None
        
        # 清理線程
        self._cleanup_thread = None
        self._stop_cleanup = False
        
        info_log("[WorkingContextManager] 工作上下文管理器初始化完成")
    
    def register_decision_handler(self, context_type: ContextType, handler: DecisionHandler):
        """註冊決策處理器"""
        self.decision_handlers[context_type] = handler
        info_log(f"[WorkingContextManager] 註冊決策處理器: {context_type.value}")
    
    def create_context(self, context_type: ContextType, 
                      threshold: int = 1, timeout: float = 300.0) -> str:
        """創建新的工作上下文"""
        context_id = f"{context_type.value}_{int(time.time())}_{uuid.uuid4().hex[:8]}"
        
        context = WorkingContext(context_id, context_type, threshold, timeout)
        self.contexts[context_id] = context
        
        # 設定為該類型的活躍上下文
        self.active_contexts_by_type[context_type] = context_id
        
        info_log(f"[WorkingContextManager] 創建新工作上下文: {context_id} "
                f"(類型: {context_type.value}, 閾值: {threshold})")
        
        return context_id
    
    def get_active_context(self, context_type: ContextType) -> Optional[WorkingContext]:
        """獲取指定類型的活躍上下文"""
        context_id = self.active_contexts_by_type.get(context_type)
        if context_id and context_id in self.contexts:
            context = self.contexts[context_id]
            if context.status == ContextStatus.ACTIVE and not context.is_expired():
                return context
        return None
    
    def add_data_to_context(self, context_type: ContextType, data_item: Any, 
                           metadata: Optional[Dict] = None) -> Optional[str]:
        """添加數據到指定類型的上下文"""
        context = self.get_active_context(context_type)
        
        if context is None:
            # 自動創建新上下文
            context_id = self.create_context(context_type)
            context = self.contexts[context_id]
        
        context.add_data(data_item, metadata)
        
        # 檢查是否達到決策條件
        if context.is_ready_for_decision():
            return self._trigger_decision(context)
        
        return context.context_id
    
    def _trigger_decision(self, context: WorkingContext) -> Optional[str]:
        """觸發上下文決策 - 使用註冊的決策處理器"""
        context.status = ContextStatus.PENDING
        
        # 查找對應的決策處理器
        handler = self.decision_handlers.get(context.context_type)
        
        if handler:
            try:
                # 獲取決策數據包
                decision_package = context.get_decision_package()
                
                # 讓決策處理器進行決策
                decision_result = handler.make_decision(decision_package)
                
                if decision_result.get('success', False):
                    # 應用決策
                    success = handler.apply_decision(decision_package, decision_result)
                    
                    if success:
                        context.status = ContextStatus.COMPLETED
                        info_log(f"[WorkingContextManager] 上下文決策完成: {context.context_id}")
                        return decision_result.get('result_id', context.context_id)
                    else:
                        # 決策應用失敗，需要進一步處理
                        return self._request_inquiry(context, decision_result)
                else:
                    # 無法自動決策，需要詢問
                    return self._request_inquiry(context, decision_result)
                    
            except Exception as e:
                error_log(f"[WorkingContextManager] 決策處理失敗: {e}")
                context.status = ContextStatus.SUSPENDED
                return None
        else:
            # 沒有註冊的決策處理器，使用通用詢問機制
            return self._request_inquiry(context, {"reason": "no_handler"})
    
    def _request_inquiry(self, context: WorkingContext, decision_info: Dict[str, Any]) -> Optional[str]:
        """請求外部詢問（如 LLM 或用戶界面）"""
        if self.inquiry_callback:
            inquiry_data = {
                "context_id": context.context_id,
                "context_type": context.context_type.value,
                "data_count": len(context.data),
                "decision_info": decision_info,
                "message": decision_info.get("message", "需要進一步確認"),
                "options": decision_info.get("options", [])
            }
            
            context.pending_decision = inquiry_data
            
            info_log(f"[WorkingContextManager] 請求外部詢問: {context.context_id}")
            return self.inquiry_callback(inquiry_data)
        
        # 沒有詢問回調，預設完成
        context.status = ContextStatus.COMPLETED
        return context.context_id
    
    def handle_inquiry_response(self, context_id: str, response: Dict[str, Any]) -> bool:
        """處理外部詢問的回應"""
        if context_id not in self.contexts:
            return False
        
        context = self.contexts[context_id]
        
        # 找到對應的決策處理器
        handler = self.decision_handlers.get(context.context_type)
        
        if handler:
            try:
                # 構建決策數據包
                decision_package = context.get_decision_package()
                decision_package.update({"user_response": response})
                
                # 讓決策處理器處理回應
                success = handler.apply_decision(decision_package, response)
                
                if success:
                    context.status = ContextStatus.COMPLETED
                    info_log(f"[WorkingContextManager] 外部回應處理完成: {context_id}")
                    return True
                    
            except Exception as e:
                error_log(f"[WorkingContextManager] 外部回應處理失敗: {e}")
        
        context.status = ContextStatus.SUSPENDED
        return False
    
    def get_context_status(self, context_id: str) -> Optional[Dict[str, Any]]:
        """獲取上下文狀態"""
        if context_id in self.contexts:
            return self.contexts[context_id].get_context_info()
        return None
    
    def cleanup_expired_contexts(self):
        """清理過期的上下文"""
        expired_contexts = []
        
        for context_id, context in self.contexts.items():
            if context.is_expired() or context.status in [ContextStatus.COMPLETED, ContextStatus.EXPIRED]:
                expired_contexts.append(context_id)
        
        for context_id in expired_contexts:
            del self.contexts[context_id]
            # 如果是活躍上下文，也要清理
            for ctx_type, active_id in list(self.active_contexts_by_type.items()):
                if active_id == context_id:
                    del self.active_contexts_by_type[ctx_type]
        
        if expired_contexts:
            debug_log(3, f"[WorkingContextManager] 清理 {len(expired_contexts)} 個過期上下文")
    
    def cleanup_incomplete_contexts(self, context_type: ContextType, min_threshold: int = 15) -> int:
        """
        清理未完成的上下文（樣本數不足的上下文）
        
        Args:
            context_type: 要清理的上下文類型
            min_threshold: 最小樣本數閾值，低於此數值的上下文將被清理
            
        Returns:
            清理的上下文數量
        """
        cleanup_contexts = []
        
        for context_id, context in self.contexts.items():
            # 只清理指定類型且狀態為 ACTIVE 且樣本數不足的上下文
            # 不清理 COMPLETED 狀態的上下文，即使樣本數不足（因為可能已經觸發決策）
            if (context.context_type == context_type and 
                context.status == ContextStatus.ACTIVE and
                len(context.data) < min_threshold):
                cleanup_contexts.append(context_id)
        
        # 執行清理
        for context_id in cleanup_contexts:
            context = self.contexts[context_id]
            info_log(f"[WorkingContextManager] 清理未完成上下文 {context_id} "
                    f"(樣本數: {len(context.data)}/{min_threshold})")
            
            # 標記為已清理
            context.status = ContextStatus.EXPIRED
            del self.contexts[context_id]
            
            # 清理活躍上下文引用
            if self.active_contexts_by_type.get(context_type) == context_id:
                del self.active_contexts_by_type[context_type]
        
        if cleanup_contexts:
            info_log(f"[WorkingContextManager] 清理 {len(cleanup_contexts)} 個未完成的 {context_type} 上下文")
        
        return len(cleanup_contexts)
    
    def set_inquiry_callback(self, callback: Callable):
        """設定通用詢問回調函數"""
        self.inquiry_callback = callback
        info_log("[WorkingContextManager] 通用詢問回調已設定")
    
    def set_notification_callback(self, callback: Callable):
        """設定通知回調函數"""
        self.notification_callback = callback
        info_log("[WorkingContextManager] 通知回調已設定")
    
    def get_all_contexts_info(self) -> List[Dict[str, Any]]:
        """獲取所有上下文資訊"""
        return [context.get_context_info() for context in self.contexts.values()]


# 全局工作上下文管理器實例
working_context_manager = WorkingContextManager()
