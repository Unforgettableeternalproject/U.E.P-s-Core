# core/event_bus.py
"""
系統事件總線 - 實現事件驅動架構

用於解耦模組間的直接調用，改用發布-訂閱模式：
- 模組完成處理後發布事件
- 協調器和其他組件訂閱事件
- 支持異步處理，避免阻塞

優勢：
1. 解耦：模組不需要知道誰會處理事件
2. 異步：事件發布立即返回
3. 可擴展：多個訂閱者可處理同一事件
4. 可測試：容易 mock 和單元測試
"""

import time
import threading
from typing import Dict, Any, Callable, List, Optional
from enum import Enum
from dataclasses import dataclass, field
from queue import Queue, Empty

from utils.debug_helper import debug_log, info_log, error_log


class SystemEvent(Enum):
    """系統事件類型"""
    # 層級完成事件
    INPUT_LAYER_COMPLETE = "input_layer_complete"          # 輸入層完成 (NLP)
    PROCESSING_LAYER_COMPLETE = "processing_layer_complete"  # 處理層完成 (LLM/MEM/SYS)
    OUTPUT_LAYER_COMPLETE = "output_layer_complete"        # 輸出層完成 (TTS)
    
    # 模組狀態事件
    MODULE_INITIALIZED = "module_initialized"              # 模組初始化完成
    MODULE_READY = "module_ready"                          # 模組就緒
    MODULE_ERROR = "module_error"                          # 模組錯誤
    MODULE_BUSY = "module_busy"                            # 模組忙碌中
    
    # 系統狀態事件
    STATE_CHANGED = "state_changed"                        # 系統狀態改變
    SESSION_STARTED = "session_started"                    # 會話開始
    SESSION_ENDED = "session_ended"                        # 會話結束
    
    # 循環控制事件
    CYCLE_STARTED = "cycle_started"                        # 處理循環開始
    CYCLE_COMPLETED = "cycle_completed"                    # 處理循環完成
    
    # 工作流輸入控制事件
    WORKFLOW_REQUIRES_INPUT = "workflow_requires_input"    # 工作流需要使用者輸入
    WORKFLOW_INPUT_COMPLETED = "workflow_input_completed"  # 工作流輸入完成
    
    # ✅ 工作流步驟事件（用於 LLM-SYS 協作）
    WORKFLOW_STEP_COMPLETED = "workflow_step_completed"    # 工作流步驟完成，等待 LLM 審核
    WORKFLOW_STEP_APPROVED = "workflow_step_approved"      # 🔧 工作流步驟已批准（LLM 審核完成）
    WORKFLOW_FAILED = "workflow_failed"                    # 工作流執行失敗
    
    # 階段五：背景工作流事件
    BACKGROUND_WORKFLOW_SUBMITTED = "background_workflow_submitted"    # 背景工作流已提交
    BACKGROUND_WORKFLOW_STARTED = "background_workflow_started"        # 背景工作流開始執行
    BACKGROUND_WORKFLOW_COMPLETED = "background_workflow_completed"    # 背景工作流完成
    BACKGROUND_WORKFLOW_FAILED = "background_workflow_failed"          # 背景工作流失敗
    BACKGROUND_WORKFLOW_CANCELLED = "background_workflow_cancelled"    # 背景工作流取消


@dataclass
class Event:
    """事件數據結構"""
    event_type: SystemEvent
    data: Dict[str, Any]
    source: str  # 事件來源（模組名稱）
    timestamp: float = field(default_factory=time.time)
    event_id: str = field(default_factory=lambda: f"evt_{int(time.time() * 1000)}")


class EventBus:
    """
    全局事件總線
    
    實現發布-訂閱模式的事件系統：
    - 模組發布事件 (publish)
    - 其他組件訂閱事件 (subscribe)
    - 支持同步和異步處理
    """
    
    def __init__(self):
        """初始化事件總線"""
        self._handlers: Dict[SystemEvent, List[Callable]] = {}
        self._lock = threading.Lock()
        self._event_queue: Queue = Queue()
        self._processing_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._event_history: List[Event] = []
        self._max_history = 100  # 保留最近100個事件
        
        # 事件統計
        self._stats = {
            "total_published": 0,
            "total_processed": 0,
            "processing_errors": 0,
            "by_event_type": {}
        }
        
        info_log("[EventBus] 事件總線初始化")
    
    def start(self):
        """啟動事件處理線程"""
        if self._processing_thread and self._processing_thread.is_alive():
            debug_log(2, "[EventBus] 事件處理線程已在運行")
            return
        
        self._stop_event.clear()
        self._processing_thread = threading.Thread(
            target=self._process_events, 
            daemon=True,
            name="EventBusProcessor"
        )
        self._processing_thread.start()
        info_log("[EventBus] 事件處理線程已啟動")
    
    def stop(self):
        """停止事件處理線程"""
        if not self._processing_thread:
            return
        
        self._stop_event.set()
        if self._processing_thread.is_alive():
            self._processing_thread.join(timeout=2.0)
        info_log("[EventBus] 事件處理線程已停止")
    
    def subscribe(self, event_type: SystemEvent, handler: Callable[[Event], None], 
                  handler_name: Optional[str] = None):
        """
        訂閱事件
        
        Args:
            event_type: 事件類型
            handler: 事件處理器函數 (接收 Event 對象)
            handler_name: 處理器名稱（用於日誌）
        """
        with self._lock:
            if event_type not in self._handlers:
                self._handlers[event_type] = []
            
            self._handlers[event_type].append(handler)
            
            name = handler_name or getattr(handler, '__name__', 'unknown')
            debug_log(2, f"[EventBus] 訂閱事件: {event_type.value} -> {name}")
    
    def unsubscribe(self, event_type: SystemEvent, handler: Callable):
        """
        取消訂閱事件
        
        Args:
            event_type: 事件類型
            handler: 事件處理器函數
        """
        with self._lock:
            if event_type in self._handlers:
                try:
                    self._handlers[event_type].remove(handler)
                    debug_log(2, f"[EventBus] 取消訂閱: {event_type.value}")
                except ValueError:
                    pass
    
    def publish(self, event_type: SystemEvent, data: Dict[str, Any], 
                source: str = "unknown", sync: bool = False):
        """
        發布事件
        
        Args:
            event_type: 事件類型
            data: 事件數據
            source: 事件來源（模組名稱）
            sync: 是否同步處理（默認異步）
        """
        event = Event(
            event_type=event_type,
            data=data,
            source=source
        )
        
        # 更新統計
        self._stats["total_published"] += 1
        if event_type.value not in self._stats["by_event_type"]:
            self._stats["by_event_type"][event_type.value] = 0
        self._stats["by_event_type"][event_type.value] += 1
        
        # 記錄到歷史
        self._add_to_history(event)
        
        debug_log(3, f"[EventBus] 發布事件: {event_type.value} from {source}")
        
        if sync:
            # 同步處理
            self._dispatch_event(event)
        else:
            # 異步處理：加入隊列
            self._event_queue.put(event)
    
    def _process_events(self):
        """事件處理循環（在獨立線程中運行）"""
        debug_log(2, "[EventBus] 事件處理循環開始")
        
        while not self._stop_event.is_set():
            try:
                # 從隊列獲取事件（超時1秒）
                event = self._event_queue.get(timeout=1.0)
                self._dispatch_event(event)
                self._event_queue.task_done()
                
            except Empty:
                # 隊列為空，繼續等待
                continue
            except Exception as e:
                error_log(f"[EventBus] 事件處理循環錯誤: {e}")
                self._stats["processing_errors"] += 1
        
        debug_log(2, "[EventBus] 事件處理循環結束")
    
    def _dispatch_event(self, event: Event):
        """
        分發事件給所有訂閱者
        
        Args:
            event: 事件對象
        """
        with self._lock:
            handlers = self._handlers.get(event.event_type, []).copy()
        
        if not handlers:
            debug_log(3, f"[EventBus] 無訂閱者: {event.event_type.value}")
            return
        
        debug_log(3, f"[EventBus] 分發事件 {event.event_type.value} 給 {len(handlers)} 個處理器")
        
        for handler in handlers:
            try:
                handler(event)
                self._stats["total_processed"] += 1
            except Exception as e:
                error_log(f"[EventBus] 事件處理器錯誤 ({event.event_type.value}): {e}")
                self._stats["processing_errors"] += 1
    
    def _add_to_history(self, event: Event):
        """添加事件到歷史記錄"""
        self._event_history.append(event)
        
        # 保持歷史記錄在最大限制內
        if len(self._event_history) > self._max_history:
            self._event_history = self._event_history[-self._max_history:]
    
    def get_stats(self) -> Dict[str, Any]:
        """獲取事件總線統計信息"""
        with self._lock:
            subscribers_count = {
                event_type.value: len(handlers)
                for event_type, handlers in self._handlers.items()
            }
        
        return {
            **self._stats,
            "queue_size": self._event_queue.qsize(),
            "subscribers": subscribers_count,
            "history_size": len(self._event_history),
            "is_running": self._processing_thread.is_alive() if self._processing_thread else False
        }
    
    def get_recent_events(self, count: int = 10, 
                         event_type: Optional[SystemEvent] = None) -> List[Event]:
        """
        獲取最近的事件
        
        Args:
            count: 返回的事件數量
            event_type: 過濾特定事件類型（可選）
        
        Returns:
            List[Event]: 事件列表
        """
        events = self._event_history.copy()
        
        if event_type:
            events = [e for e in events if e.event_type == event_type]
        
        return events[-count:]
    
    def clear_history(self):
        """清空事件歷史記錄"""
        self._event_history.clear()
        debug_log(2, "[EventBus] 事件歷史已清空")


# 全局事件總線實例
event_bus = EventBus()


def publish_event(event_type: SystemEvent, data: Dict[str, Any], 
                  source: str = "unknown", sync: bool = False):
    """
    發布事件的便捷函數
    
    Args:
        event_type: 事件類型
        data: 事件數據
        source: 事件來源
        sync: 是否同步處理
    """
    event_bus.publish(event_type, data, source, sync)


def subscribe_event(event_type: SystemEvent, handler: Callable, 
                   handler_name: Optional[str] = None):
    """
    訂閱事件的便捷函數
    
    Args:
        event_type: 事件類型
        handler: 事件處理器
        handler_name: 處理器名稱
    """
    event_bus.subscribe(event_type, handler, handler_name)
