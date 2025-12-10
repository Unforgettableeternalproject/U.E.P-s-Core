"""
MISCHIEF 行為實現：移動視窗

隨機移動桌面上的非最大化/全螢幕視窗。
參考 TestOverlayApplication/user_screen/worker_shift.py 的實現。
"""

import random
from typing import Dict, Any, Tuple, List
import pyautogui
import win32gui
import win32con
import time

from . import MischiefAction, MoodContext
from utils.debug_helper import debug_log, error_log, SYSTEM_LEVEL


class MoveWindowAction(MischiefAction):
    """移動視窗行為"""
    
    def __init__(self):
        super().__init__()
        self.display_name = "Move Window"
        self.description = "Randomly select a visible window (not maximized/fullscreen) and move it slightly"
        self.mood_context = MoodContext.ANY
        self.animation_name = "push_left"
        self.allowed_intensities = ["medium", "high"]
        self.requires_params = []
    
    def execute(self, params: Dict[str, Any]) -> Tuple[bool, str]:
        """執行視窗移動"""
        try:
            # 獲取所有可移動的視窗
            movable_windows = self._get_movable_windows()
            
            if not movable_windows:
                return False, "沒有找到可移動的視窗"
            
            # 隨機選擇一個視窗
            hwnd, title = random.choice(movable_windows)
            
            # 獲取視窗當前位置
            rect = win32gui.GetWindowRect(hwnd)
            x, y, right, bottom = rect
            width = right - x
            height = bottom - y
            
            # 獲取螢幕尺寸
            screen_width, screen_height = pyautogui.size()
            
            # 計算新位置（隨機偏移）
            max_offset = 200
            offset_x = random.randint(-max_offset, max_offset)
            offset_y = random.randint(-max_offset, max_offset)
            
            new_x = max(0, min(x + offset_x, screen_width - width))
            new_y = max(0, min(y + offset_y, screen_height - height))
            
            # 移動視窗
            win32gui.SetWindowPos(
                hwnd, 
                win32con.HWND_TOP,
                new_x, new_y,
                width, height,
                win32con.SWP_SHOWWINDOW
            )
            
            debug_log(2, f"[MoveWindow] 移動視窗 '{title}' "
                         f"從 ({x}, {y}) 到 ({new_x}, {new_y})")
            
            return True, f"成功移動視窗：{title}"
            
        except Exception as e:
            error_log(f"[MoveWindow] 執行失敗: {e}")
            return False, f"移動視窗時發生錯誤: {str(e)}"
    
    def _get_movable_windows(self) -> List[Tuple[int, str]]:
        """
        獲取所有可移動的視窗
        
        Returns:
            List of (hwnd, title)
        """
        movable = []
        
        def enum_callback(hwnd, _):
            if not win32gui.IsWindowVisible(hwnd):
                return True
            
            # 檢查是否有標題
            title = win32gui.GetWindowText(hwnd)
            if not title:
                return True
            
            # 排除 UEP 相關視窗
            if "U.E.P" in title or "UEP" in title:
                return True
            
            # 檢查視窗樣式
            style = win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE)
            
            # 排除最大化視窗
            if style & win32con.WS_MAXIMIZE:
                return True
            
            # 檢查是否為全螢幕（透過視窗大小判斷）
            rect = win32gui.GetWindowRect(hwnd)
            x, y, right, bottom = rect
            width = right - x
            height = bottom - y
            
            screen_width, screen_height = pyautogui.size()
            
            # 如果視窗幾乎佔滿螢幕，視為全螢幕
            if width >= screen_width * 0.95 and height >= screen_height * 0.95:
                return True
            
            movable.append((hwnd, title))
            return True
        
        try:
            win32gui.EnumWindows(enum_callback, None)
        except Exception as e:
            error_log(f"[MoveWindow] 枚舉視窗失敗: {e}")
        
        return movable
