# modules/mov_module/core/tease_tracker.py
"""
互動追蹤器 - 追蹤使用者與 UEP 的互動頻率
用於決定何時觸發 tease 動畫（捉弄動畫）
"""

from __future__ import annotations
import time
from typing import Optional
from collections import deque


class TeaseTracker:
    """追蹤使用者互動以決定是否觸發捉弄動畫"""
    
    def __init__(
        self,
        time_window: float = 10.0,
        interaction_threshold: int = 3
    ):
        """
        Args:
            time_window: 時間窗口（秒），用於計算互動頻率
            interaction_threshold: 觸發 tease 動畫所需的互動次數（拖曳+投擲）
        """
        self.time_window = time_window
        self.interaction_threshold = interaction_threshold
        
        # 互動歷史記錄 (timestamp)
        self.interactions: deque = deque()
        
        # 當前正在播放 tease 動畫
        self._is_teasing = False
        self._tease_start_time: Optional[float] = None
        
    def record_interaction(self) -> None:
        """記錄一次互動（拖曳或投擲）"""
        now = time.time()
        self.interactions.append(now)
        self._cleanup_old_interactions(now)
    
    def _cleanup_old_interactions(self, current_time: float) -> None:
        """移除超出時間窗口的舊互動記錄"""
        cutoff_time = current_time - self.time_window
        while self.interactions and self.interactions[0] < cutoff_time:
            self.interactions.popleft()
    
    def should_trigger_tease(self) -> bool:
        """
        檢查是否應該觸發 tease 動畫
        
        Returns:
            True: 應該觸發 tease
            False: 不觸發
        """
        if self._is_teasing:
            return False
        
        now = time.time()
        self._cleanup_old_interactions(now)
        
        # 檢查互動次數
        interaction_count = len(self.interactions)
        return interaction_count >= self.interaction_threshold
    
    def start_tease(self) -> None:
        """標記開始播放 tease 動畫"""
        self._is_teasing = True
        self._tease_start_time = time.time()
    
    def end_tease(self) -> None:
        """結束 tease 動畫並清空互動記錄"""
        self._is_teasing = False
        self._tease_start_time = None
        self.interactions.clear()
    
    def is_teasing(self) -> bool:
        """當前是否正在播放 tease 動畫"""
        return self._is_teasing
    
    def get_interaction_count(self) -> int:
        """獲取時間窗口內的互動次數"""
        now = time.time()
        self._cleanup_old_interactions(now)
        return len(self.interactions)
    
    def reset(self) -> None:
        """重置所有追蹤狀態"""
        self.interactions.clear()
        self._is_teasing = False
        self._tease_start_time = None
