"""
前端事件總線 - 專門處理高頻前端事件

與系統 EventBus 的區別：
1. 同步處理（無線程/隊列開銷）
2. 針對高頻事件優化（10-60 FPS）
3. 簡化的訂閱機制
4. 低延遲（< 1ms）

使用場景：
- 滑鼠追蹤事件
- 拖動事件
- 動畫幀更新
- UI 交互事件
"""

from typing import Dict, Callable, List, Any
from enum import Enum
from dataclasses import dataclass
import time

from utils.debug_helper import debug_log, error_log


class FrontendEvent(Enum):
    """前端事件類型"""
    # 滑鼠追蹤
    CURSOR_NEAR = "cursor_near"          # 滑鼠靠近角色
    CURSOR_FAR = "cursor_far"            # 滑鼠遠離角色
    CURSOR_ANGLE = "cursor_angle"        # 滑鼠角度更新
    
    # 拖動事件
    DRAG_START = "drag_start"            # 開始拖動
    DRAG_MOVE = "drag_move"              # 拖動中
    DRAG_END = "drag_end"                # 拖動結束
    DRAG_THROW = "drag_throw"            # 拋擲
    
    # 動畫事件
    ANIMATION_FRAME = "animation_frame"  # 動畫幀更新
    ANIMATION_START = "animation_start"  # 動畫開始
    ANIMATION_FINISH = "animation_finish" # 動畫結束
    
    # UI 交互
    PET_CLICKED = "pet_clicked"          # 點擊角色
    PET_DOUBLE_CLICKED = "pet_double_clicked"  # 雙擊角色
    WINDOW_SHIFT = "window_shift"        # 視窗移動


@dataclass
class FrontendEventData:
    """前端事件數據"""
    event_type: FrontendEvent
    data: Dict[str, Any]
    timestamp: float


class FrontendEventBus:
    """
    前端事件總線（同步、高性能）
    
    設計特點：
    - 同步處理：立即調用訂閱者，無線程開銷
    - 簡化訂閱：只需提供回調函數
    - 性能優化：針對 10-60 FPS 高頻事件
    - 錯誤隔離：單個訂閱者錯誤不影響其他訂閱者
    """
    
    def __init__(self):
        """初始化前端事件總線"""
        self._handlers: Dict[FrontendEvent, List[Callable]] = {}
        
        # 性能統計（可選，用於調試）
        self._event_count: Dict[FrontendEvent, int] = {}
        self._total_time: Dict[FrontendEvent, float] = {}
        
        debug_log(2, "[FrontendEventBus] 已初始化（同步高性能模式）")
    
    def subscribe(self, event_type: FrontendEvent, handler: Callable[[Dict[str, Any]], None]):
        """
        訂閱前端事件
        
        Args:
            event_type: 事件類型
            handler: 回調函數 (event_data: Dict) -> None
        
        Example:
            def on_cursor_near(data):
                angle = data['angle']
                # 處理邏輯
            
            bus.subscribe(FrontendEvent.CURSOR_NEAR, on_cursor_near)
        """
        if event_type not in self._handlers:
            self._handlers[event_type] = []
        
        if handler not in self._handlers[event_type]:
            self._handlers[event_type].append(handler)
            debug_log(3, f"[FrontendEventBus] 訂閱事件: {event_type.value}")
    
    def unsubscribe(self, event_type: FrontendEvent, handler: Callable):
        """
        取消訂閱
        
        Args:
            event_type: 事件類型
            handler: 要移除的回調函數
        """
        if event_type in self._handlers:
            if handler in self._handlers[event_type]:
                self._handlers[event_type].remove(handler)
                debug_log(3, f"[FrontendEventBus] 取消訂閱: {event_type.value}")
    
    def publish(self, event_type: FrontendEvent, data: Dict[str, Any]):
        """
        發布前端事件（同步處理）
        
        Args:
            event_type: 事件類型
            data: 事件數據
        
        Note:
            同步處理意味著此方法會阻塞直到所有訂閱者處理完成
            適合高頻低延遲場景，訂閱者應該盡快返回
        """
        if event_type not in self._handlers:
            return  # 無訂閱者，直接返回
        
        start_time = time.perf_counter()
        
        # 同步調用所有訂閱者
        for handler in self._handlers[event_type]:
            try:
                handler(data)
            except Exception as e:
                error_log(f"[FrontendEventBus] 處理事件 {event_type.value} 時發生錯誤: {e}")
        
        # 性能統計
        elapsed = time.perf_counter() - start_time
        self._event_count[event_type] = self._event_count.get(event_type, 0) + 1
        self._total_time[event_type] = self._total_time.get(event_type, 0.0) + elapsed
        
        # 如果處理時間過長，發出警告
        if elapsed > 0.005:  # > 5ms
            debug_log(1, f"[FrontendEventBus] ⚠️ 事件處理過慢: {event_type.value} ({elapsed*1000:.2f}ms)")
    
    def get_stats(self) -> Dict[str, Any]:
        """
        獲取性能統計
        
        Returns:
            統計數據字典
        """
        stats = {}
        for event_type in self._event_count:
            count = self._event_count[event_type]
            total_time = self._total_time[event_type]
            avg_time = total_time / count if count > 0 else 0
            
            stats[event_type.value] = {
                "count": count,
                "total_time_ms": total_time * 1000,
                "avg_time_ms": avg_time * 1000,
                "subscribers": len(self._handlers.get(event_type, []))
            }
        
        return stats
    
    def reset_stats(self):
        """重置性能統計"""
        self._event_count.clear()
        self._total_time.clear()
    
    def clear_all(self):
        """清除所有訂閱"""
        self._handlers.clear()
        debug_log(2, "[FrontendEventBus] 已清除所有訂閱")


# 全局前端事件總線實例
_frontend_event_bus = None


def get_frontend_event_bus() -> FrontendEventBus:
    """獲取全局前端事件總線實例"""
    global _frontend_event_bus
    if _frontend_event_bus is None:
        _frontend_event_bus = FrontendEventBus()
    return _frontend_event_bus
