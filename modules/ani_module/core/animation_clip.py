from __future__ import annotations
from dataclasses import dataclass

@dataclass
class AnimationClip:
    name: str
    total_frames: int
    fps: float = 30.0
    default_loop: bool = True

    # runtime（由 AnimationManager 控制）
    current_frame: int = 0
    last_frame_time: float = 0.0

    def frame_duration(self) -> float:
        return 1.0 / max(self.fps, 1e-6)