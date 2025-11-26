"""
層級驅動動畫策略

根據系統循環層級（input/processing/output）選擇動畫
"""

from typing import Dict, Any, Optional
from utils.debug_helper import debug_log

from .base_strategy import AnimationStrategy


class LayerAnimationStrategy(AnimationStrategy):
    """
    層級動畫策略
    
    根據當前處理層級選擇對應的動畫
    - input 層：等待輸入動畫
    - processing 層：思考/處理動畫
    - output 層：回應/說話動畫
    """
    
    def __init__(self, coordinator, config: Optional[Dict[str, Any]] = None):
        super().__init__(coordinator, config)
        self.priority = 50  # 中等優先級
        
    def can_select(self, context: Dict[str, Any]) -> bool:
        """只在有明確層級時才選擇"""
        if not super().can_select(context):
            return False
        return context.get('layer') is not None
    
    def select_animation(self, context: Dict[str, Any]) -> Optional[str]:
        """根據層級選擇動畫"""
        try:
            layer = context.get('layer')
            state = context.get('state')
            movement_mode = context.get('movement_mode')
            
            if not layer:
                return None
            
            # 從配置獲取層級動畫映射
            layer_config = self.config.get('LAYERS', {})
            fallbacks = self.config.get('fallbacks', {})
            
            anim_name = None
            
            if layer == "input":
                # 輸入層：等待輸入
                input_config = layer_config.get('input', {})
                anim_name = input_config.get('default', 'thinking_f')
                
            elif layer == "processing":
                # 處理層：思考
                processing_config = layer_config.get('processing', {})
                anim_name = processing_config.get('default', 'data_processing_f')
                
            elif layer == "output":
                # 輸出層：根據情緒回應
                output_config = layer_config.get('output', {})
                mood = context.get('mood', 0)
                
                if mood > 0:
                    anim_name = output_config.get('positive_mood', 'talk_ani_f')
                else:
                    anim_name = output_config.get('negative_mood', 'talk_ani2_f')
                
                # 地面模式例外：只有 talk_ani_g，沒有 talk_ani2_g
                if movement_mode:
                    from ..core.state_machine import MovementMode
                    if movement_mode == MovementMode.GROUND and anim_name == 'talk_ani2_f':
                        anim_name = 'talk_ani_f'  # 地面負面情緒也用 talk_ani
            
            # 使用 fallback 映射
            if anim_name and anim_name in fallbacks:
                anim_name = fallbacks[anim_name]
            
            # 根據移動模式轉換動畫
            if anim_name and movement_mode:
                anim_name = self._convert_for_movement_mode(anim_name, movement_mode)
            
            debug_log(3, f"[LayerStrategy] 層級 '{layer}' 選擇動畫: {anim_name}")
            return anim_name
            
        except Exception as e:
            from utils.debug_helper import error_log
            error_log(f"[LayerStrategy] 選擇動畫失敗: {e}")
            return None
    
    def _convert_for_movement_mode(self, anim_name: str, movement_mode: Any) -> Optional[str]:
        """根據移動模式轉換動畫名稱"""
        if not anim_name:
            return None
        
        try:
            from ..core.state_machine import MovementMode
            
            # 檢查後綴
            if anim_name.endswith('_f'):
                # 浮空動畫
                if movement_mode == MovementMode.GROUND:
                    # 嘗試找地面版本
                    alternative = anim_name[:-2] + '_g'
                    if self._animation_exists(alternative):
                        return alternative
                return anim_name
                
            elif anim_name.endswith('_g'):
                # 地面動畫
                if movement_mode == MovementMode.FLOAT:
                    # 嘗試找浮空版本
                    alternative = anim_name[:-2] + '_f'
                    if self._animation_exists(alternative):
                        return alternative
                return anim_name
            else:
                # 通用動畫
                return anim_name
                
        except Exception:
            return anim_name
    
    def _animation_exists(self, name: str) -> bool:
        """檢查動畫是否存在"""
        if not hasattr(self.coordinator, 'ani_module'):
            return False
        
        ani = self.coordinator.ani_module
        if not ani or not hasattr(ani, 'manager'):
            return False
        
        return name in ani.manager.clips
