from __future__ import annotations
from typing import Callable, Dict, Optional
from enum import Enum
import time

from .animation_clip import AnimationClip

class AnimPlayState(Enum):
    STOPPED = "STOPPED"
    PLAYING = "PLAYING"
    FINISHED = "FINISHED"

class AnimationManager:
    """動畫管理器（單飛 + 去重 + 可中斷 + 單槽待播）
    - 同名 loop 正在播：coalesce（不重播）
    - loop → 非 loop：允許立即中斷
    - 非 loop → loop：待播，等前者播完
    - 非 loop → 非 loop：直接取代
    - loop → loop：cooldown 節流，避免抖動
    """

    def __init__(self, request_cooldown: float = 0.25):
        self.clips: Dict[str, AnimationClip] = {}
        self.current: Optional[AnimationClip] = None
        self.state: AnimPlayState = AnimPlayState.STOPPED
        self._active_loop: bool = True
        self.last_request_name: Optional[str] = None
        self.last_request_time: float = 0.0
        self.request_cooldown = request_cooldown
        self._pending: Optional[tuple[str, dict]] = None  # 單槽
        self.static_frame_mode: bool = False  # 靜態幀模式：不自動更新幀
        # 事件回呼
        self.on_start: Optional[Callable[[str], None]] = None
        self.on_frame: Optional[Callable[[str, int], None]] = None
        self.on_finish: Optional[Callable[[str], None]] = None

    # 資源
    def register_clip(self, clip: AnimationClip):
        self.clips[clip.name] = clip

    def register_clips(self, clips: list[AnimationClip]):
        for c in clips:
            self.register_clip(c)

    # 播放
    def play(self, name: str, *, loop: Optional[bool] = None) -> Dict:
        now = time.time()
        clip = self.clips.get(name)
        if not clip:
            return {"error": f"clip not found: {name}"}
        desired_loop = clip.default_loop if loop is None else bool(loop)

        if (self.current and self.state == AnimPlayState.PLAYING and
            self.current.name == name and self._active_loop and desired_loop and
            (now - self.last_request_time) < self.request_cooldown):
            return {"success": True, "animation": name, "coalesced": True}

        if self.current and self.state == AnimPlayState.PLAYING:
            curr_loop = self._active_loop
            if not desired_loop:
                self._stop_like()
                self._start_clip(clip, desired_loop, now)
                return {"success": True, "animation": name}
            else:
                if not curr_loop:
                    self._pending = (name, {"loop": True})
                    return {"success": True, "queued": True, "animation": name}
                if (now - self.last_request_time) < self.request_cooldown and self.current.name != name:
                    return {"success": True, "throttled": True, "animation": name}

        self._start_clip(clip, desired_loop, now)
        return {"success": True, "animation": name}

    def stop(self):
        if not self.current:
            return
        finished = self.current.name
        self.state = AnimPlayState.STOPPED
        self.current = None
        if self.on_finish:
            try: self.on_finish(finished)
            except Exception: pass
    
    def set_frame(self, frame_index: int) -> Dict:
        """直接設置當前動畫的幀索引（參考 desktop_pet.py）"""
        if not self.current:
            return {"error": "no active animation"}
        
        # 確保幀索引在有效範圍內（使用模運算自動循環）
        frame_index = frame_index % self.current.total_frames
        
        # 直接設置幀
        self.current.current_frame = frame_index
        
        # 觸發幀回調以更新 UI
        if self.on_frame:
            try:
                self.on_frame(self.current.name, frame_index)
            except Exception:
                pass
        
        return {"success": True, "frame": frame_index}

    # 更新
    def update(self, now: Optional[float] = None):
        if not self.current:
            return
        # 靜態幀模式：不自動更新幀（用於 turn_head 等需要手動控制幀的動畫）
        if self.static_frame_mode:
            return
        now = now or time.time()
        dt = self.current.frame_duration()
        while now - self.current.last_frame_time >= dt:
            self.current.last_frame_time += dt
            self.current.current_frame += 1
            if self.on_frame:
                try: self.on_frame(self.current.name, self.current.current_frame)
                except Exception: pass
            if self.current.current_frame >= self.current.total_frames:
                if self._active_loop:
                    self.current.current_frame = 0
                    if self.on_frame:
                        try: self.on_frame(self.current.name, self.current.current_frame)
                        except Exception: pass
                else:
                    finished = self.current.name
                    self.state = AnimPlayState.FINISHED
                    self.current = None
                    if self.on_finish:
                        try: self.on_finish(finished)
                        except Exception: pass
                    if self._pending:
                        pname, params = self._pending
                        self._pending = None
                        self.play(pname, loop=params.get("loop", True))
                    break

    def enter_static_frame_mode(self, clip_name: str, initial_frame: int = 0) -> Dict:
        """進入靜態幀模式（用於 turn_head 等需要手動控制幀的動畫）"""
        clip = self.clips.get(clip_name)
        if not clip:
            return {"error": f"clip not found: {clip_name}"}
        
        # 停止當前動畫
        self.stop()
        
        # 設置為靜態幀模式
        self.current = clip
        self.current.current_frame = initial_frame % clip.total_frames
        self.current.last_frame_time = time.time()
        self.state = AnimPlayState.PLAYING
        self._active_loop = False
        self.static_frame_mode = True
        
        return {"success": True, "clip": clip_name, "frame": self.current.current_frame}
    
    def exit_static_frame_mode(self):
        """退出靜態幀模式並停止當前動畫"""
        self.static_frame_mode = False
        # 停止當前動畫，避免繼續播放完
        self.stop()
    
    def get_status(self) -> Dict:
        if not self.current:
            return {"name": None, "is_playing": False, "loop": False, "state": self.state.value}
        return {
            "name": self.current.name,
            "frame": self.current.current_frame,
            "total_frames": self.current.total_frames,
            "fps": self.current.fps,
            "is_playing": self.state == AnimPlayState.PLAYING,
            "loop": self._active_loop,
            "state": self.state.value,
            "static_frame_mode": self.static_frame_mode,
            # 新增：變換屬性
            "zoom": self.current.zoom,
            "offset_x": self.current.offset_x,
            "offset_y": self.current.offset_y,
        }

    # 私有
    def _start_clip(self, clip: AnimationClip, active_loop: bool, now: float):
        self.current = clip
        self.current.current_frame = 0
        self.current.last_frame_time = now
        self._active_loop = active_loop
        self.state = AnimPlayState.PLAYING
        self.last_request_name = clip.name
        self.last_request_time = now
        if self.on_start:
            try: self.on_start(clip.name)
            except Exception: pass
        if self.on_frame:
            try: self.on_frame(clip.name, self.current.current_frame)
            except Exception: pass

    def _stop_like(self):
        self.state = AnimPlayState.STOPPED