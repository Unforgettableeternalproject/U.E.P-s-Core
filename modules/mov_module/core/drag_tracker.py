# modules/mov_module/core/drag_tracker.py
"""
拖曳追蹤器 - 用於計算投擲速度
參考 TestOverlayApplication/desktop_pet.py 的 DragTracker
"""

from __future__ import annotations
import time
from typing import Tuple
from collections import deque


class DragTracker:
    """追蹤拖曳歷史以計算釋放時的速度"""
    
    def __init__(self, max_history: int = 5):
        """
        Args:
            max_history: 保留的歷史點數量（用於平滑計算）
        """
        self.max_history = max_history
        self.history: deque = deque(maxlen=max_history)
        
    def clear(self) -> None:
        """清除歷史記錄"""
        self.history.clear()
    
    def add_point(self, x: float, y: float) -> None:
        """添加拖曳點
        
        Args:
            x: X 座標
            y: Y 座標
        """
        now = time.time()
        self.history.append((x, y, now))
    
    def calculate_velocity(self, time_window: float = 0.15) -> Tuple[float, float, float]:
        """計算當前拖曳速度（只使用最近時間窗口內的點）
        
        Args:
            time_window: 時間窗口（秒），預設 0.15 秒
        
        Returns:
            (vx, vy, speed): X速度, Y速度, 總速度
        """
        if len(self.history) < 2:
            return (0.0, 0.0, 0.0)
        
        # 只使用最近時間窗口內的點計算速度
        now = time.time()
        recent_points = [p for p in self.history if (now - p[2]) <= time_window]
        
        if len(recent_points) < 2:
            # 如果時間窗口內點數不足，退回使用所有點
            recent_points = list(self.history)
        
        # 計算最近點的平均速度
        velocities = []
        for i in range(1, len(recent_points)):
            x0, y0, t0 = recent_points[i-1]
            x1, y1, t1 = recent_points[i]
            dt = t1 - t0
            if dt > 0.001:
                vx = (x1 - x0) / dt
                vy = (y1 - y0) / dt
                velocities.append((vx, vy))
        
        if not velocities:
            return (0.0, 0.0, 0.0)
        
        # 計算平均
        avg_vx = sum(v[0] for v in velocities) / len(velocities)
        avg_vy = sum(v[1] for v in velocities) / len(velocities)
        speed = (avg_vx * avg_vx + avg_vy * avg_vy) ** 0.5
        
        return (avg_vx, avg_vy, speed)
    
    def get_average_velocity(self) -> Tuple[float, float, float]:
        """獲取平均速度（更平滑）
        
        Returns:
            (vx, vy, speed): 平均 X速度, Y速度, 總速度
        """
        if len(self.history) < 2:
            return (0.0, 0.0, 0.0)
        
        # 計算所有相鄰點之間的速度
        velocities = []
        for i in range(1, len(self.history)):
            x0, y0, t0 = self.history[i-1]
            x1, y1, t1 = self.history[i]
            dt = t1 - t0
            if dt > 0.001:
                vx = (x1 - x0) / dt
                vy = (y1 - y0) / dt
                velocities.append((vx, vy))
        
        if not velocities:
            return (0.0, 0.0, 0.0)
        
        # 計算平均
        avg_vx = sum(v[0] for v in velocities) / len(velocities)
        avg_vy = sum(v[1] for v in velocities) / len(velocities)
        speed = (avg_vx * avg_vx + avg_vy * avg_vy) ** 0.5
        
        return (avg_vx, avg_vy, speed)
