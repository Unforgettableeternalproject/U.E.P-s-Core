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
        self._processing_transition_timer = None  # 用於延遲 processing → output 轉換
        
    def can_handle(self, event: Any) -> bool:
        """判斷是否為層級事件"""
        if not hasattr(event, 'event_type'):
            return False
            
        if SystemEvent is None:
            return False
            
        return event.event_type in [
            SystemEvent.INTERACTION_STARTED,
            SystemEvent.INPUT_LAYER_COMPLETE,
            SystemEvent.PROCESSING_LAYER_COMPLETE,
            SystemEvent.OUTPUT_LAYER_COMPLETE,
            SystemEvent.CYCLE_COMPLETED
        ]
    
    def handle(self, event: Any) -> bool:
        """處理層級完成事件"""
        try:
            # 確定當前層級
            if event.event_type == SystemEvent.INTERACTION_STARTED:
                self.current_layer = "input"
                debug_log(2, f"[LayerHandler] 互動開始 → 進入輸入層")
                
            elif event.event_type == SystemEvent.INPUT_LAYER_COMPLETE:
                self.current_layer = "processing"
                debug_log(2, f"[LayerHandler] 輸入層完成 → 進入處理層")
                
            elif event.event_type == SystemEvent.PROCESSING_LAYER_COMPLETE:
                # ⏱️ 延遲 1 秒後才轉換到 output 層，讓 processing 動畫有時間播放
                self.current_layer = "processing"
                debug_log(2, f"[LayerHandler] 處理層完成 → 保持 processing 狀態 1 秒")
                
                # 取消之前的計時器（如果有）
                if self._processing_transition_timer is not None:
                    try:
                        self._processing_transition_timer.cancel()
                    except:
                        pass
                
                # 使用 Timer 延遲轉換
                import threading
                def _transition_to_output():
                    self.current_layer = "output"
                    if hasattr(self.coordinator, '_current_layer'):
                        self.coordinator._current_layer = self.current_layer
                    debug_log(2, f"[LayerHandler] ⏱️ 延遲結束 → 進入輸出層（觸發說話動畫）")
                
                self._processing_transition_timer = threading.Timer(2.5, _transition_to_output)
                self._processing_transition_timer.daemon = True
                self._processing_transition_timer.start()
                
            elif event.event_type == SystemEvent.OUTPUT_LAYER_COMPLETE:
                self.current_layer = "output"
                debug_log(2, f"[LayerHandler] 輸出層完成 → 進入輸出層")
                
                # 立即觸發 output 層動畫（不要等待 on_tick）
                self._trigger_layer_animation("output")
                
            elif event.event_type == SystemEvent.CYCLE_COMPLETED:
                self.current_layer = None
                debug_log(2, f"[LayerHandler] 循環完成 → 清除層級狀態")
            
            # 更新協調器的層級狀態
            # SystemCycleBehavior 會自動從 context 讀取 current_layer 並觸發動畫
            if hasattr(self.coordinator, '_current_layer'):
                self.coordinator._current_layer = self.current_layer
                debug_log(2, f"[LayerHandler] 更新層級狀態: {self.current_layer}")
            
            return True
            
        except Exception as e:
            error_log(f"[LayerHandler] 處理層級事件失敗: {e}")
            return False
    
    def _trigger_layer_animation(self, layer: str) -> None:
        """立即觸發層級動畫"""
        try:
            # 獲取 layer_strategy
            layer_strategy = getattr(self.coordinator, '_layer_strategy', None)
            if not layer_strategy:
                debug_log(2, "[LayerHandler] LayerAnimationStrategy 未找到")
                return
            
            # 準備 context
            from ..core.state_machine import MovementMode
            movement_mode = getattr(self.coordinator, 'movement_mode', MovementMode.GROUND)
            
            # 獲取 mood
            mood = 0
            try:
                from core.status_manager import status_manager
                mood = status_manager.status.mood
            except Exception:
                pass
            
            strategy_context = {
                'layer': layer,
                'movement_mode': movement_mode,
                'mood': mood
            }
            
            # 選擇動畫
            anim_name = layer_strategy.select_animation(strategy_context)
            
            if anim_name:
                debug_log(1, f"[LayerHandler] ⚡ 立即觸發層級動畫: {anim_name}（層級: {layer}）")
                
                # 使用 coordinator 的 _trigger_anim 方法
                self.coordinator._trigger_anim(anim_name, {
                    "loop": True,
                    "immediate_interrupt": True,
                    "force_restart": True
                }, source="system_cycle_behavior")
                
                debug_log(1, f"[LayerHandler] ✅ 層級動畫已觸發: {anim_name}")
            else:
                debug_log(2, f"[LayerHandler] 無可用動畫（層級: {layer}）")
                
        except Exception as e:
            error_log(f"[LayerHandler] 觸發層級動畫失敗: {e}")
