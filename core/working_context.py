# core/working_context.py
"""
工作上下文管理器 - 管理整個 UEP 程式生命周期中的大型工作階段

這個系統是一個高層級的上下文管理系統，支援多種上下文類型：
- 語者樣本累積和身份管理
- 對話上下文和工作流會話管理
- 跨模組數據共享和決策觸發機制
- 支援可插拔的決策處理器
- 與 state_manager 和 router 協同工作
- 完全獨立於具體模組實現

主要功能：
1. 多類型上下文管理：支援語者累積、身份管理、工作流等多種上下文
2. 全局數據共享：提供跨模組的數據存取機制
3. 決策觸發：當上下文達到條件時自動觸發決策處理
4. 便利方法：提供針對不同上下文類型的專用操作方法
"""

import time
import uuid
import json
import os
from datetime import datetime
from typing import Dict, List, Any, Optional, Callable, Protocol
from enum import Enum, auto
from utils.debug_helper import debug_log, info_log, error_log
import threading


class ContextScope(Enum):
    """工作上下文作用域"""
    SESSION = "session"         # 會話級別（GS 結束時清理）
    GLOBAL = "global"           # 全局級別（系統運行期間保留，重啟清除）
    PERSISTENT = "persistent"   # 持久化級別（存入文件，跨重啟保留）


class ContextType(Enum):
    """工作上下文類型"""
    SPEAKER_ACCUMULATION = "speaker_accumulation"  # 語者樣本累積
    IDENTITY_MANAGEMENT = "identity_management"     # 身份管理
    CONVERSATION = "conversation"                   # 對話上下文
    TASK_EXECUTION = "task_execution"              # 任務執行
    WORKFLOW_SESSION = "workflow_session"          # 工作流會話
    LEARNING = "learning"                          # 學習模式
    CROSS_MODULE_DATA = "cross_module_data"        # 跨模組數據共享
    # Session system specific context types
    MEM_EXTERNAL_ACCESS = "mem_external_access"    # MEM 模組外部存取
    LLM_CONTEXT = "llm_context"                    # LLM 上下文
    SYS_WORKFLOW = "sys_workflow"                  # SYS 模組工作流
    GENERAL_SESSION = "general_session"            # General Session 上下文


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


class WorkingContext:
    """單個工作上下文實例"""
    
    def __init__(self, context_id: str, context_type: ContextType, 
                 threshold: int = 15, timeout: float = 300.0,
                 scope: ContextScope = ContextScope.SESSION):
        self.context_id = context_id
        self.context_type = context_type
        self.scope = scope  # 🆕 作用域
        self.status = ContextStatus.ACTIVE
        self.created_at = time.time()
        self.last_activity = time.time()
        self.timeout = timeout  # 上下文超時時間（秒）
        self._rwlock = threading.RLock()
        
        # 累積數據相關
        self.data: List[Any] = []  # 改名為更通用的 data
        self.metadata: Dict[str, Any] = {}
        self.threshold = threshold
        
        # 決策相關
        self.pending_decision: Optional[Dict[str, Any]] = None
        self.decision_callback: Optional[Callable] = None
        self.warmup()
        
    def warmup(self):
        """初始化工作上下文"""
        with self._rwlock:
            # WorkingContext 不需要清理其他上下文
            pass
        info_log(f"[WorkingContext] {self.context_id} warmup 完成")
        
    def add_data(self, data_item: Any, metadata: Optional[Dict] = None):
        """添加數據到上下文 - 更通用的方法名"""
        self.data.append(data_item)
        self.last_activity = time.time()
        
        if metadata:
            self.metadata.update(metadata)
            
        max_items = self.metadata.get("max_items", 10000)
        if len(self.data) > max_items:
            # 丟最舊的，或在這裡做摘要/壓縮
            self.data = self.data[-max_items:]
                    
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
            "trace_id": f"{self.context_id}:{len(self.data)}",
            "timestamp": time.time(),
            "context_id": self.context_id,
            "context_type": self.context_type,
            "data": self.data.copy(),
            "metadata": self.metadata.copy(),
            "threshold": self.threshold,
            "data_count": len(self.data)
        }
    
    # === 特定上下文類型的便利方法 ===
    
    def is_speaker_accumulation(self) -> bool:
        """檢查是否為語者樣本累積上下文"""
        return self.context_type == ContextType.SPEAKER_ACCUMULATION
    
    def is_identity_management(self) -> bool:
        """檢查是否為身份管理上下文"""
        return self.context_type == ContextType.IDENTITY_MANAGEMENT
    
    def is_workflow_session(self) -> bool:
        """檢查是否為工作流會話上下文"""
        return self.context_type == ContextType.WORKFLOW_SESSION
    
    def get_speaker_id(self) -> Optional[str]:
        """獲取語者ID（如果是語者相關上下文）"""
        return self.metadata.get("speaker_id")
    
    def get_identity_id(self) -> Optional[str]:
        """獲取身份ID（如果是身份相關上下文）"""
        return self.metadata.get("identity_id")
    
    def get_session_id(self) -> Optional[str]:
        """獲取會話ID（如果是會話相關上下文）"""
        return self.metadata.get("session_id")
    
    def add_speaker_sample(self, sample_data: Dict[str, Any]):
        """添加語者樣本（專用於語者累積上下文）"""
        if self.is_speaker_accumulation():
            self.add_data(sample_data)
        else:
            debug_log(1, f"[WorkingContext] 警告：嘗試向非語者累積上下文添加語者樣本")
    
    def get_sample_count(self) -> int:
        """獲取樣本數量"""
        return len(self.data)
    
    def get_latest_sample(self) -> Optional[Any]:
        """獲取最新的樣本"""
        return self.data[-1] if self.data else None


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
        self._rwlock = threading.RLock()  # 添加缺失的讀寫鎖
        
        # 🆕 多層級數據存儲
        self.session_contexts: Dict[str, WorkingContext] = {}      # SESSION scope
        self.global_contexts: Dict[str, WorkingContext] = {}       # GLOBAL scope
        self.persistent_contexts: Dict[str, WorkingContext] = {}   # PERSISTENT scope
        
        # 繼續支持舊的 contexts 屬性（映射到 session_contexts）
        self.contexts = self.session_contexts
        
        self.active_contexts_by_type: Dict[ContextType, str] = {}
        
        # 全局上下文數據 - GLOBAL 層級（系統運行期間保留，重啟後清空）
        self.global_context_data: Dict[str, Any] = {}
        
        # 階段三：層級跳過控制旗標（用於工作流驅動的輸入層跳過）
        self.global_context_data['skip_input_layer'] = False
        self.global_context_data['input_layer_reason'] = None  # 跳過原因
        self.global_context_data['workflow_waiting_input'] = False
        
        # 持久化上下文數據 - PERSISTENT 層級（跨系統重啟保留）
        self.persistent_context_data: Dict[str, Any] = {}
        self.persistent_context_data['gs_history'] = []  # GS 歷史記錄
        
        # 🆕 持久化文件路徑
        self._persistent_file = "memory/persistent_context.json"
        self._load_persistent_data()  # 系統啟動時載入（需要 global_context_data 已初始化）
        
        # 類型默認配置（添加 scope 配置）
        self._type_defaults = {
            # 🔄 Speaker Accumulation 改為 GLOBAL（跟隨當前聲明的 Identity，系統運行期間保留）
            ContextType.SPEAKER_ACCUMULATION: {"threshold": 15, "timeout": 600.0, "scope": ContextScope.GLOBAL},
            ContextType.IDENTITY_MANAGEMENT:  {"threshold": 1,  "timeout": 900.0, "scope": ContextScope.GLOBAL},
            ContextType.CONVERSATION:         {"threshold": 1,  "timeout": 300.0, "scope": ContextScope.SESSION},
            ContextType.WORKFLOW_SESSION:     {"threshold": 1,  "timeout": 900.0, "scope": ContextScope.SESSION},
            ContextType.LEARNING:             {"threshold": 1,  "timeout": 300.0, "scope": ContextScope.GLOBAL},
            ContextType.CROSS_MODULE_DATA:    {"threshold": 1,  "timeout": 300.0, "scope": ContextScope.GLOBAL},
            ContextType.MEM_EXTERNAL_ACCESS:  {"threshold": 1,  "timeout": 300.0, "scope": ContextScope.SESSION},
            ContextType.LLM_CONTEXT:          {"threshold": 1,  "timeout": 300.0, "scope": ContextScope.SESSION},
            ContextType.SYS_WORKFLOW:         {"threshold": 1,  "timeout": 300.0, "scope": ContextScope.SESSION},
            ContextType.GENERAL_SESSION:      {"threshold": 1,  "timeout": 300.0, "scope": ContextScope.SESSION},
        }
        
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
                      threshold: int = 1, timeout: float = 300.0,
                      scope: Optional[ContextScope] = None) -> str:
        """創建新的工作上下文"""
        with self._rwlock:
            # 如果未指定 scope，使用類型默認配置
            effective_scope: ContextScope = scope if scope is not None else self._type_defaults.get(
                context_type, {}
            ).get("scope", ContextScope.SESSION)
            
            context_id = f"{context_type.value}_{int(time.time())}_{uuid.uuid4().hex[:8]}"
            
            context = WorkingContext(context_id, context_type, threshold, timeout, effective_scope)
            
            # 🆕 根據 scope 存儲到不同的字典
            if effective_scope == ContextScope.SESSION:
                self.session_contexts[context_id] = context
            elif effective_scope == ContextScope.GLOBAL:
                self.global_contexts[context_id] = context
            elif effective_scope == ContextScope.PERSISTENT:
                self.persistent_contexts[context_id] = context
            
            # 設定為該類型的活躍上下文
            self.active_contexts_by_type[context_type] = context_id
            
            info_log(f"[WorkingContextManager] 創建新工作上下文: {context_id} "
                    f"(類型: {context_type.value}, 閾值: {threshold}, scope: {effective_scope.value})")
            
            return context_id
    
    def get_active_context(self, context_type: ContextType) -> Optional[WorkingContext]:
        """獲取指定類型的活躍上下文"""
        context_id = self.active_contexts_by_type.get(context_type)
        if not context_id:
            return None
        
        # 🆕 從所有層級查詢
        context = (self.session_contexts.get(context_id) or 
                   self.global_contexts.get(context_id) or 
                   self.persistent_contexts.get(context_id))
        
        if context and context.status == ContextStatus.ACTIVE and not context.is_expired():
            return context
        return None
    
    def add_data_to_context(self, context_type: ContextType, data_item: Any, 
                           metadata: Optional[Dict] = None,
                           scope: Optional[ContextScope] = None) -> Optional[str]:
        """添加數據到指定類型的上下文"""
        with self._rwlock:
            context = self.get_active_context(context_type)
            
            if context is None:
                # 自動創建新上下文
                d = self._type_defaults.get(context_type, {"threshold": 1, "timeout": 300.0, "scope": ContextScope.SESSION})
                # scope 參數優先級：函數參數 > 類型配置
                effective_scope = scope if scope is not None else d.get("scope", ContextScope.SESSION)
                context_id = self.create_context(
                    context_type, 
                    threshold=d["threshold"], 
                    timeout=d["timeout"],
                    scope=effective_scope
                )
                context = self.get_active_context(context_type)  # 🆕 使用多層級查詢
            
            # 類型檢查：確保 context 不為 None
            if context is None:
                error_log(f"[WorkingContextManager] 無法創建或獲取上下文: {context_type}")
                return None
            
            context.add_data(data_item, metadata)
            
            # 🆕 PERSISTENT scope 每次添加數據後都保存
            if context.scope == ContextScope.PERSISTENT:
                self._save_persistent_data()
            
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
                        # ⬇️ 釋放大 payload（保留必要 metadata/統計即可）
                        context.data.clear()
                        context.metadata["completed_at"] = time.time()
                        if self.notification_callback:
                            try:
                                self.notification_callback({
                                    "event": "context.completed",
                                    "context_id": context.context_id,
                                    "context_type": context.context_type.value
                                })
                            except Exception as e:
                                error_log(f"[WorkingContextManager] 通知回調失敗: {e}")
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
        
        if self.notification_callback:
            try:
                self.notification_callback({
                    "event": "context.inquiry",
                    "context_id": context.context_id,
                    "context_type": context.context_type.value,
                    "reason": decision_info.get("reason", "require_confirmation")
                })
            except Exception as e:
                error_log(f"[WorkingContextManager] 通知回調失敗: {e}")
                
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
        context.status = ContextStatus.SUSPENDED
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
        """清理過期的上下文（只清理 SESSION scope，保留 GLOBAL 和 PERSISTENT）"""
        expired_contexts = []
        
        # 🆕 只清理 SESSION scope 的上下文
        for context_id, context in self.session_contexts.items():
            if context.is_expired() or context.status in [ContextStatus.COMPLETED, ContextStatus.EXPIRED]:
                expired_contexts.append(context_id)
        
        for context_id in expired_contexts:
            del self.session_contexts[context_id]
            # 如果是活躍上下文，也要清理
            for ctx_type, active_id in list(self.active_contexts_by_type.items()):
                if active_id == context_id:
                    del self.active_contexts_by_type[ctx_type]
        
        if expired_contexts:
            debug_log(3, f"[WorkingContextManager] 清理 {len(expired_contexts)} 個過期的 SESSION 上下文")
        
        return len(expired_contexts)
    
    def cleanup_incomplete_contexts(self, context_type: ContextType, min_threshold: int = 15) -> int:
        """
        清理未完成的上下文（樣本數不足的上下文）- 只清理 SESSION scope
        
        Args:
            context_type: 要清理的上下文類型
            min_threshold: 最小樣本數閾值，低於此數值的上下文將被清理
            
        Returns:
            清理的上下文數量
        """
        cleanup_contexts = []
        
        # 🆕 只清理 SESSION scope 的上下文
        for context_id, context in self.session_contexts.items():
            # 只清理指定類型且狀態為 ACTIVE 且樣本數不足的上下文
            # 不清理 COMPLETED 狀態的上下文，即使樣本數不足（因為可能已經觸發決策）
            if (context.context_type == context_type and 
                context.status == ContextStatus.ACTIVE and
                len(context.data) < min_threshold):
                cleanup_contexts.append(context_id)
        
        # 執行清理
        for context_id in cleanup_contexts:
            context = self.session_contexts[context_id]
            info_log(f"[WorkingContextManager] 清理未完成上下文 {context_id} "
                    f"(樣本數: {len(context.data)}/{min_threshold})")
            
            # 標記為已清理
            context.status = ContextStatus.EXPIRED
            del self.session_contexts[context_id]
            
            # 清理活躍上下文引用
            if self.active_contexts_by_type.get(context_type) == context_id:
                del self.active_contexts_by_type[context_type]
        
        if cleanup_contexts:
            info_log(f"[WorkingContextManager] 清理 {len(cleanup_contexts)} 個未完成的 SESSION {context_type} 上下文")
        
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
        """獲取所有上下文資訊（所有 scope）"""
        all_contexts = []
        all_contexts.extend(self.session_contexts.values())
        all_contexts.extend(self.global_contexts.values())
        all_contexts.extend(self.persistent_contexts.values())
        return [context.get_context_info() for context in all_contexts]
    
    def set_context_data(self, key: str, data: Any) -> None:
        """
        設定全局上下文數據
        
        這個方法用於在不同模組之間共享數據，例如使用者身份、
        偏好設定、記憶令牌等。這些數據與特定上下文類型無關，
        是全局可訪問的。
        
        Args:
            key: 數據鍵名
            data: 要存儲的數據
        """
        self.global_context_data[key] = data
        debug_log(3, f"[WorkingContextManager] 設定全局上下文數據: {key}")
    
    def get_context_data(self, key: str, default: Any = None) -> Any:
        """
        獲取全局上下文數據
        
        Args:
            key: 數據鍵名
            default: 如果鍵不存在時返回的默認值
            
        Returns:
            存儲的數據或默認值
        """
        value = self.global_context_data.get(key, default)
        debug_log(3, f"[WorkingContextManager] 獲取全局上下文數據: {key}")
        return value
    
    def delete_context_data(self, key: str) -> bool:
        """
        刪除全局上下文數據
        
        Args:
            key: 要刪除的數據鍵名
            
        Returns:
            是否成功刪除
        """
        if key in self.global_context_data:
            del self.global_context_data[key]
            debug_log(3, f"[WorkingContextManager] 刪除全局上下文數據: {key}")
            return True
        return False
    
    def get_all_context_data_keys(self) -> List[str]:
        """
        獲取所有全局上下文數據的鍵名
        
        Returns:
            鍵名列表
        """
        return list(self.global_context_data.keys())
    
    # === 身份管理相關的便利方法 ===
    
    def set_current_identity(self, identity_data: Dict[str, Any]):
        """設置當前用戶身份"""
        self.set_context_data("current_identity", identity_data)
        info_log(f"[WorkingContextManager] 設置當前身份: {identity_data.get('identity_id', 'Unknown')}")
    
    def get_current_identity(self) -> Optional[Dict[str, Any]]:
        """獲取當前用戶身份"""
        identity_data = self.get_context_data("current_identity")
        debug_log(3, f"[WorkingContext] 獲取當前身份: {identity_data}")
        return identity_data
    
    def set_identity(self, identity_data: Dict[str, Any]):
        """設置當前用戶身份（別名方法，與測試代碼兼容）"""
        self.set_current_identity(identity_data)
    
    def set_memory_token(self, token: str):
        """設置記憶庫存取令牌（通過更新當前身份）"""
        current_identity = self.get_current_identity()
        if current_identity:
            current_identity["memory_token"] = token
            self.set_current_identity(current_identity)
        else:
            # 如果沒有身份，創建一個基本身份
            new_identity = {
                "identity_id": "default",
                "user_identity": "default_user",
                "memory_token": token
            }
            self.set_current_identity(new_identity)
    
    def get_memory_token(self) -> Optional[str]:
        """獲取記憶庫存取令牌（從當前身份中）"""
        current_identity = self.get_current_identity()
        return current_identity.get("memory_token") if current_identity else None
    
    def set_voice_preferences(self, preferences: Dict[str, Any]):
        """設置語音偏好"""
        self.set_context_data("voice_preferences", preferences)
    
    def get_voice_preferences(self) -> Optional[Dict[str, Any]]:
        """獲取語音偏好"""
        return self.get_context_data("voice_preferences")
    
    def set_conversation_style(self, style: Dict[str, Any]):
        """設置對話風格"""
        self.set_context_data("conversation_style", style)
    
    def get_conversation_style(self) -> Optional[Dict[str, Any]]:
        """獲取對話風格"""
        return self.get_context_data("conversation_style")
    
    # === 🆕 Identity 主動聲明機制 ===
    
    def set_declared_identity(self, identity_id: str):
        """
        用戶主動聲明身份（優先級高於 Speaker 推斷）
        
        適用場景：
        - 前端用戶選擇/創建身份
        - 文字模式指定身份
        - 需要明確身份時
        
        Args:
            identity_id: 身份ID
        """
        self.set_context_data("declared_identity", identity_id)
        info_log(f"[WorkingContextManager] 用戶聲明身份: {identity_id}")
    
    def get_declared_identity(self) -> Optional[str]:
        """
        獲取已聲明的身份ID
        
        Returns:
            已聲明的身份ID，如果未聲明則返回 None
        """
        return self.get_context_data("declared_identity")
    
    def clear_declared_identity(self):
        """清除聲明的身份（用戶登出時）"""
        if self.delete_context_data("declared_identity"):
            info_log("[WorkingContextManager] 已清除聲明的身份")
    
    def has_declared_identity(self) -> bool:
        """檢查是否有聲明的身份"""
        return self.get_declared_identity() is not None
    
    # === 上下文查找和管理便利方法 ===
    
    def find_context(self, context_type: ContextType, 
                    metadata_filter: Optional[Dict[str, Any]] = None) -> Optional[WorkingContext]:
        """
        根據類型和元數據篩選條件查找上下文（所有 scope）
        
        Args:
            context_type: 上下文類型
            metadata_filter: 元數據篩選條件
            
        Returns:
            匹配的上下文實例或None
        """
        # 🆕 收集所有層級的上下文
        all_contexts = []
        all_contexts.extend(self.session_contexts.values())
        all_contexts.extend(self.global_contexts.values())
        all_contexts.extend(self.persistent_contexts.values())
        
        for context in all_contexts:
            if context.context_type == context_type:
                if metadata_filter:
                    # 檢查所有篩選條件是否匹配
                    matches = all(
                        context.metadata.get(key) == value 
                        for key, value in metadata_filter.items()
                    )
                    if matches:
                        return context
                else:
                    return context
        return None
    
    def get_contexts_by_type(self, context_type: ContextType) -> List[WorkingContext]:
        """獲取指定類型的所有上下文（所有 scope）"""
        all_contexts = []
        # 🆕 收集所有層級的上下文
        all_contexts.extend(self.session_contexts.values())
        all_contexts.extend(self.global_contexts.values())
        all_contexts.extend(self.persistent_contexts.values())
        
        return [
            context for context in all_contexts
            if context.context_type == context_type
        ]
    
    def add_data(self, context_type: ContextType, data_item: Any, 
                metadata: Optional[Dict] = None) -> Optional[str]:
        """
        便利方法：添加數據到指定類型的上下文
        
        這是 add_data_to_context 的別名，用於向下兼容。
        
        Args:
            context_type: 上下文類型
            data_item: 要添加的數據項
            metadata: 可選的元數據
            
        Returns:
            上下文ID或None
        """
        return self.add_data_to_context(context_type, data_item, metadata)
    
    def get_context_summary(self) -> Dict[str, Any]:
        """獲取上下文管理器的摘要信息"""
        # 🆕 收集所有層級的上下文
        all_contexts = []
        all_contexts.extend(self.session_contexts.values())
        all_contexts.extend(self.global_contexts.values())
        all_contexts.extend(self.persistent_contexts.values())
        
        summary = {
            "total_contexts": len(all_contexts),
            "session_contexts": len(self.session_contexts),
            "global_contexts": len(self.global_contexts),
            "persistent_contexts": len(self.persistent_contexts),
            "active_contexts": len([c for c in all_contexts if c.status == ContextStatus.ACTIVE]),
            "contexts_by_type": {},
            "global_data_keys": list(self.global_context_data.keys()),
            "decision_handlers": list(self.decision_handlers.keys())
        }
        
        # 按類型統計上下文
        for context in all_contexts:
            ctx_type = context.context_type.value
            if ctx_type not in summary["contexts_by_type"]:
                summary["contexts_by_type"][ctx_type] = 0
            summary["contexts_by_type"][ctx_type] += 1
        
        return summary
    
    def clear_all_data(self):
        """清理所有上下文數據（所有 scope）"""
        self.session_contexts.clear()
        self.global_contexts.clear()
        self.persistent_contexts.clear()
        self.global_context_data.clear()  # GLOBAL 層級
        self.persistent_context_data.clear()  # PERSISTENT 層級
        info_log("[WorkingContextManager] 清理所有上下文數據")
    
    # === 🆕 持久化方法 ===
    
    def _save_persistent_data(self):
        """持久化 PERSISTENT scope 的數據到文件"""
        try:
            os.makedirs(os.path.dirname(self._persistent_file), exist_ok=True)
            
            # 序列化 persistent_contexts
            persistent_contexts = {}
            for context_id, context in self.persistent_contexts.items():
                # 只保存必要的數據（避免大型 embedding 對象無法序列化）
                context_data = {
                    "context_id": context.context_id,
                    "context_type": context.context_type.value,
                    "threshold": context.threshold,
                    "timeout": context.timeout,
                    "scope": context.scope.value,
                    "data_count": len(context.data),
                    "metadata": context.metadata,
                    "created_at": context.created_at,
                    "last_activity": context.last_activity
                }
                persistent_contexts[context_id] = context_data
            
            # Phase 4: 保存 PERSISTENT 層級數據（如 gs_history）
            persistent_global_data = {
                "gs_history": self.persistent_context_data.get('gs_history', [])
            }
            
            # 組合成完整的持久化數據
            full_persistent_data = {
                "persistent_contexts": persistent_contexts,
                "global_data": persistent_global_data,
                "version": "1.0"
            }
            
            with open(self._persistent_file, 'w', encoding='utf-8') as f:
                json.dump(full_persistent_data, f, ensure_ascii=False, indent=2)
            
            debug_log(3, f"[WorkingContextManager] 持久化數據已保存: {len(persistent_contexts)} 個上下文, gs_history={len(persistent_global_data['gs_history'])} 條記錄")
        
        except Exception as e:
            error_log(f"[WorkingContextManager] 持久化數據保存失敗: {e}")
    
    def _load_persistent_data(self):
        """系統啟動時載入 PERSISTENT scope 的數據"""
        try:
            if not os.path.exists(self._persistent_file):
                info_log("[WorkingContextManager] 無持久化文件，跳過載入")
                return
            
            with open(self._persistent_file, 'r', encoding='utf-8') as f:
                full_persistent_data = json.load(f)
            
            # 兼容舊格式（直接是 contexts 字典）
            if "persistent_contexts" in full_persistent_data:
                persistent_contexts = full_persistent_data["persistent_contexts"]
                global_data = full_persistent_data.get("global_data", {})
            else:
                # 舊格式：整個文件就是 contexts
                persistent_contexts = full_persistent_data
                global_data = {}
            
            # 恢復 persistent_contexts（但不恢復 data，只恢復結構）
            for context_id, context_data in persistent_contexts.items():
                try:
                    context_type = ContextType(context_data["context_type"])
                    context = WorkingContext(
                        context_id=context_data["context_id"],
                        context_type=context_type,
                        threshold=context_data["threshold"],
                        timeout=context_data["timeout"],
                        scope=ContextScope.PERSISTENT
                    )
                    context.metadata = context_data.get("metadata", {})
                    context.created_at = context_data.get("created_at", time.time())
                    context.last_activity = context_data.get("last_activity", time.time())
                    # 註：data 不恢復，需要重新累積（避免反序列化大型對象）
                    
                    self.persistent_contexts[context_id] = context
                    self.active_contexts_by_type[context_type] = context_id
                    
                except Exception as e:
                    error_log(f"[WorkingContextManager] 恢復上下文 {context_id} 失敗: {e}")
            
            # Phase 4: 恢復 PERSISTENT 層級數據
            if global_data:
                gs_history = global_data.get("gs_history", [])
                if gs_history:
                    self.persistent_context_data['gs_history'] = gs_history
                    info_log(f"[WorkingContextManager] 恢復 gs_history: {len(gs_history)} 條記錄")
            
            info_log(f"[WorkingContextManager] 載入持久化數據: {len(self.persistent_contexts)} 個上下文")
        
        except Exception as e:
            error_log(f"[WorkingContextManager] 載入持久化數據失敗: {e}")
    
    # === GS History 管理方法（Phase 4）===
    def get_gs_history(self) -> List[str]:
        """獲取 GS 歷史記錄（session_id 列表）- PERSISTENT 層級"""
        return self.persistent_context_data.get('gs_history', [])
    
    def add_to_gs_history(self, session_id: str, max_history: int = 10):
        """
        添加 GS session_id 到歷史記錄
        
        Args:
            session_id: GS session ID（字符串格式，如 'gs_1234567890_abcd1234'）
            max_history: 最大保留歷史數量
        """
        gs_history = self.get_gs_history()
        
        # 添加新的 session_id
        gs_history.append(session_id)
        
        # 限制歷史記錄數量
        if len(gs_history) > max_history:
            gs_history = gs_history[-max_history:]
        
        # 保存回 persistent_context_data（PERSISTENT 層級）
        self.persistent_context_data['gs_history'] = gs_history
        
        # Phase 4: 持久化到文件（跨系統重啟保留）
        self._save_persistent_data()
        
        debug_log(2, f"[WorkingContextManager] GS 歷史已更新: {session_id}, 總記錄={len(gs_history)}")
    
    def get_current_gs_id(self) -> Optional[str]:
        """獲取當前活躍的 GS session_id（從 global_context_data 讀取）"""
        return self.global_context_data.get('current_gs_id', None)
    
    # === 兼容性方法 ===
    def set_data(self, context_type: ContextType, key: str, data: Any):
        """設定上下文數據 (兼容性方法)"""
        context_key = f"{context_type.value}_{key}"
        self.set_context_data(context_key, data)
    
    def get_data(self, context_type: ContextType, key: str, default: Any = None) -> Any:
        """獲取上下文數據 (兼容性方法)"""
        context_key = f"{context_type.value}_{key}"
        return self.get_context_data(context_key, default)
    
    def clear_data(self, context_type: ContextType, key: str):
        """清除特定上下文數據 (兼容性方法)"""
        context_key = f"{context_type.value}_{key}"
        if context_key in self.global_context_data:
            del self.global_context_data[context_key]
            
    def start_cleanup_worker(self, interval_sec: float = 30.0):
        if self._cleanup_thread and self._cleanup_thread.is_alive():
            return
        self._stop_cleanup = False
        def _loop():
            while not self._stop_cleanup:
                try:
                    with self._rwlock:
                        self.cleanup_expired_contexts()
                except Exception as e:
                    error_log(f"[WorkingContextManager] 清理執行緒錯誤: {e}")
                time.sleep(interval_sec)
        self._cleanup_thread = threading.Thread(target=_loop, daemon=True)
        self._cleanup_thread.start()
        info_log("[WorkingContextManager] 清理執行緒已啟動")

    def stop_cleanup_worker(self):
        self._stop_cleanup = True
    
    # === 階段三：層級跳過控制方法 ===
    
    def set_skip_input_layer(self, skip: bool, reason: Optional[str] = None):
        """
        設置是否跳過輸入層
        
        Args:
            skip: 是否跳過輸入層
            reason: 跳過原因（用於日誌記錄）
        """
        self.global_context_data['skip_input_layer'] = skip
        self.global_context_data['input_layer_reason'] = reason
        if skip:
            debug_log(2, f"[WorkingContextManager] 設置跳過輸入層: {reason}")
        else:
            debug_log(3, "[WorkingContextManager] 重置輸入層跳過旗標")
    
    def should_skip_input_layer(self) -> bool:
        """
        檢查是否應該跳過輸入層
        
        Returns:
            bool: 是否應該跳過輸入層
        """
        return self.global_context_data.get('skip_input_layer', False)
    
    def get_skip_reason(self) -> Optional[str]:
        """
        獲取跳過輸入層的原因
        
        Returns:
            Optional[str]: 跳過原因
        """
        return self.global_context_data.get('input_layer_reason')
    
    def set_workflow_waiting_input(self, waiting: bool):
        """
        設置工作流是否正在等待輸入
        
        Args:
            waiting: 是否等待輸入
        """
        self.global_context_data['workflow_waiting_input'] = waiting
        if waiting:
            debug_log(2, "[WorkingContextManager] 工作流等待使用者輸入")
        else:
            debug_log(3, "[WorkingContextManager] 工作流輸入完成")
    
    def is_workflow_waiting_input(self) -> bool:
        """
        檢查工作流是否正在等待輸入
        
        Returns:
            bool: 是否等待輸入
        """
        return self.global_context_data.get('workflow_waiting_input', False)
    
    # 🆕 Resume Context 管理（用於 BW 恢復 CS）
    def set_resume_context(self, resume_context: Dict[str, Any]):
        """
        設置 resume context（用於 BW 中斷後恢復 CS）
        
        Args:
            resume_context: CS 的上下文信息（從 get_session_context 獲取）
        """
        self.global_context_data['resume_context'] = resume_context
        debug_log(2, f"[WorkingContextManager] 已設置 resume_context: session_id={resume_context.get('session_id')}")
    
    def get_resume_context(self) -> Optional[Dict[str, Any]]:
        """
        獲取 resume context
        
        Returns:
            Optional[Dict[str, Any]]: Resume context 或 None
        """
        return self.global_context_data.get('resume_context')
    
    def clear_resume_context(self):
        """清除 resume context"""
        if 'resume_context' in self.global_context_data:
            del self.global_context_data['resume_context']
            debug_log(3, "[WorkingContextManager] 已清除 resume_context")


# 全局工作上下文管理器實例
working_context_manager = WorkingContextManager()
