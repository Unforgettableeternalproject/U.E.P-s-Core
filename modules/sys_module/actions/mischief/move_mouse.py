"""
MISCHIEF 行為實現：移動滑鼠

檢測滑鼠是否長時間靜止，若是則小幅度移動滑鼠。
"""

import pyautogui
import random
import time
from typing import Dict, Any, Tuple

from . import MischiefAction, MoodContext
from utils.debug_helper import debug_log, error_log, SYSTEM_LEVEL


class MoveMouseAction(MischiefAction):
    """移動滑鼠行為"""
    
    def __init__(self):
        super().__init__()
        self.display_name = "Move Mouse"
        self.description = "Slightly move the mouse cursor if idle"
        self.mood_context = MoodContext.ANY
        self.animation_name = None
        self.allowed_intensities = ["low", "medium"]
        self.requires_params = []
        
        # 安全區域設置（避免意外點擊重要區域）
        pyautogui.FAILSAFE = True
    
    def execute(self, params: Dict[str, Any]) -> Tuple[bool, str]:
        """執行滑鼠移動"""
        
        try:
            # 獲取當前滑鼠位置
            current_x, current_y = pyautogui.position()
            
            # 生成隨機偏移（小幅度）
            offset_x = random.randint(-50, 50)
            offset_y = random.randint(-50, 50)
            
            new_x = current_x + offset_x
            new_y = current_y + offset_y
            
            # 獲取螢幕尺寸確保不超出邊界
            screen_width, screen_height = pyautogui.size()
            new_x = max(0, min(new_x, screen_width - 1))
            new_y = max(0, min(new_y, screen_height - 1))
            
            # 平滑移動（0.3 秒）
            pyautogui.moveTo(new_x, new_y, duration=0.3)
            
            debug_log(2, f"[MoveMouse] 移動滑鼠從 ({current_x}, {current_y}) "
                         f"到 ({new_x}, {new_y})")
            
            return True, "成功移動滑鼠"
            
        except Exception as e:
            error_log(f"[MoveMouse] 執行失敗: {e}")
            return False, f"移動滑鼠時發生錯誤: {str(e)}"
