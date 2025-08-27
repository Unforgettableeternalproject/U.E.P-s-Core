from __future__ import annotations
from dataclasses import dataclass

@dataclass
class AnimationClip:
    name: str
    total_frames: int
    fps: float = 30.0
    default_loop: bool = True
    
    # 新增：縮放和偏移屬性
    zoom: float = 1.0
    offset_x: int = 0
    offset_y: int = 0

    # runtime（由 AnimationManager 控制）
    current_frame: int = 0
    last_frame_time: float = 0.0

    def frame_duration(self) -> float:
        return 1.0 / max(self.fps, 1e-6)