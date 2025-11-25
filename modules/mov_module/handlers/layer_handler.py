"""
層級事件處理器

處理系統循環的層級完成事件（INPUT/PROCESSING/OUTPUT_LAYER_COMPLETE）
根據層級和狀態選擇並觸發動畫
"""

from typing import Any
from utils.debug_helper import debug_log, error_log

from .base_handler import BaseHandler

try:
    from core.event_bus import SystemEvent
except ImportError:
    SystemEvent = None  # type: ignore


class LayerEventHandler(BaseHandler):
    """
    層級事件處理器
    
    職責：
    1. 監聽層級完成事件
    2. 更新當前層級狀態
    3. 根據層級和系統狀態選擇動畫
    4. 觸發動畫播放
    """
    
    def __init__(self, coordinator):
        super().__init__(coordinator)
        self.current_layer: str | None = None
        
    def can_handle(self, event: Any) -> bool:
        """判斷是否為層級事件"""
        if not hasattr(event, 'event_type'):
            return False
            
        if SystemEvent is None:
            return False
            
        return event.event_type in [
            SystemEvent.INPUT_LAYER_COMPLETE,
            SystemEvent.PROCESSING_LAYER_COMPLETE,
            SystemEvent.OUTPUT_LAYER_COMPLETE
        ]
    
    def handle(self, event: Any) -> bool:
        """處理層級完成事件"""
        try:
            # 確定當前層級
            if event.event_type == SystemEvent.INPUT_LAYER_COMPLETE:
                self.current_layer = "processing"
                debug_log(2, f"[LayerHandler] 輸入層完成 → 進入處理層")
                
            elif event.event_type == SystemEvent.PROCESSING_LAYER_COMPLETE:
                self.current_layer = "output"
                debug_log(2, f"[LayerHandler] 處理層完成 → 進入輸出層")
                
            elif event.event_type == SystemEvent.OUTPUT_LAYER_COMPLETE:
                self.current_layer = None
                debug_log(2, f"[LayerHandler] 輸出層完成 → 循環結束")
            
            # 更新協調器的層級狀態
            if hasattr(self.coordinator, '_current_layer'):
                self.coordinator._current_layer = self.current_layer
            
            # 觸發動畫更新
            if hasattr(self.coordinator, '_update_animation_for_current_state'):
                self.coordinator._update_animation_for_current_state()
            
            return True
            
        except Exception as e:
            error_log(f"[LayerHandler] 處理層級事件失敗: {e}")
            return False
