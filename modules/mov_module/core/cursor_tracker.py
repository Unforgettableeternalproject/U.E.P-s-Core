# modules/mov_module/core/cursor_tracker.py
"""
滑鼠追蹤器：檢測滑鼠活動和接近角色的距離
"""

import math
import time
from typing import Tuple, TYPE_CHECKING
from PyQt5.QtCore import QTimer, QObject, pyqtSignal
from PyQt5.QtGui import QCursor
from PyQt5.QtWidgets import QApplication
from PyQt5.QtCore import Qt

if TYPE_CHECKING:
    from ...ui_module.main.desktop_pet_app import DesktopPetApp

try:
    import win32api
    import win32con
    _HAS_WIN32 = True
except ImportError:
    _HAS_WIN32 = False


class CursorConfig:
    """滑鼠追蹤配置"""
    active_speed: float = 120.0          # 判斷滑鼠活躍的速度閾值 (px/s)
    active_window: float = 0.25          # 活躍判定的時間窗口 (秒)
    watch_radius: int = 150              # 開始追蹤的距離 (px)
    watch_radius_out: int = 330          # 停止追蹤的距離 (px) - 有滯後防止抖動
    head_turn_sensitivity: int = 9       # 轉頭靈敏度 (360度 / 靈敏度 = 幀數)


class CursorTracker(QObject):
    """
    滑鼠追蹤器
    
    功能：
    - 追蹤滑鼠位置和移動速度
    - 判斷滑鼠是否在角色附近活躍
    - 發送進入/離開追蹤區域的信號
    """
    
    # 信號：滑鼠進入追蹤範圍且活躍
    cursor_entered = pyqtSignal()
    # 信號：滑鼠離開追蹤範圍
    cursor_left = pyqtSignal()
    
    def __init__(self, pet_app: 'DesktopPetApp', config: CursorConfig = None):
        super().__init__()
        self.pet_app = pet_app
        self.config = config or CursorConfig()
        
        # 滑鼠狀態追蹤
        self.last_pos = QCursor.pos()
        self.last_time = time.time()
        self.speed_px_per_sec = 0.0
        self.active_until = 0.0
        self.is_active_hover = False
        self._was_inside_watch_radius = False  # 上一幀是否在追蹤範圍內
        
        # 定時器：每 33ms 更新一次 (~30fps)
        self.timer = QTimer()
        self.timer.setInterval(33)
        self.timer.timeout.connect(self._tick)
        self.timer.start()
    
    def _tick(self) -> None:
        """定時更新滑鼠活動狀態"""
        self._update_activity()
        
        # 如果正在拖曳，不檢測追蹤
        if hasattr(self.pet_app, 'is_dragging') and self.pet_app.is_dragging:
            return
        
        cursor_pos = QCursor.pos()
        distance = self._distance_to_pet_center(cursor_pos)
        
        # 判斷是否在追蹤範圍內
        is_inside_now = (distance <= self.config.watch_radius and self.is_active_hover)
        
        # 檢測進入/離開事件
        if is_inside_now and not self._was_inside_watch_radius:
            # 進入追蹤範圍
            self.cursor_entered.emit()
        elif not is_inside_now and self._was_inside_watch_radius:
            # 離開追蹤範圍
            self.cursor_left.emit()
        
        self._was_inside_watch_radius = is_inside_now
    
    def _update_activity(self) -> None:
        """更新滑鼠活動狀態（速度計算和活躍判定）"""
        now = time.time()
        pos = QCursor.pos()
        
        # 計算時間差和距離
        dt = max(1e-3, now - self.last_time)
        distance = math.hypot(pos.x() - self.last_pos.x(), pos.y() - self.last_pos.y())
        self.speed_px_per_sec = distance / dt
        
        self.last_pos = pos
        self.last_time = now
        
        # 判斷滑鼠是否活躍（移動速度快 或 按下按鈕）
        if self.speed_px_per_sec >= self.config.active_speed or self._is_mouse_down():
            self.active_until = now + self.config.active_window
        
        # 判斷滑鼠是否在角色窗口內且處於活躍窗口
        is_inside = self.pet_app.frameGeometry().contains(pos)
        self.is_active_hover = is_inside and (now <= self.active_until)
    
    def _is_mouse_down(self) -> bool:
        """檢測滑鼠按鈕是否按下"""
        if _HAS_WIN32:
            try:
                return bool(win32api.GetAsyncKeyState(win32con.VK_LBUTTON) & 0x8000)
            except Exception:
                pass
        return bool(QApplication.mouseButtons() & Qt.LeftButton)
    
    def _distance_to_pet_center(self, cursor_pos) -> float:
        """計算滑鼠到角色中心的距離"""
        pet_center_x = self.pet_app.x() + self.pet_app.width() // 2
        pet_center_y = self.pet_app.y() + self.pet_app.height() // 2
        
        dx = cursor_pos.x() - pet_center_x
        dy = cursor_pos.y() - pet_center_y
        return math.hypot(dx, dy)
    
    def calculate_angle_to_cursor(self) -> float:
        """
        計算角色中心到滑鼠的角度
        
        Returns:
            角度 (度)，0° = 右，90° = 上，180° = 左，270° = 下
        """
        cursor_pos = QCursor.pos()
        pet_center_x = self.pet_app.x() + self.pet_app.width() // 2
        pet_center_y = self.pet_app.y() + self.pet_app.height() // 2
        
        dx = cursor_pos.x() - pet_center_x
        dy = cursor_pos.y() - pet_center_y
        
        # 螢幕座標 y+ 向下，所以反轉 dy 來得到數學角度
        angle_rad = math.atan2(-dy, dx)
        angle_deg = (math.degrees(angle_rad) + 360) % 360
        return angle_deg
    
    def is_cursor_near(self) -> bool:
        """檢查滑鼠是否在追蹤範圍內且活躍"""
        return self._was_inside_watch_radius
    
    def stop(self) -> None:
        """停止追蹤"""
        self.timer.stop()
    
    def start(self) -> None:
        """開始追蹤"""
        if not self.timer.isActive():
            self.timer.start()
