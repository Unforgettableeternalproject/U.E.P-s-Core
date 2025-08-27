from __future__ import annotations
from typing import Callable, Dict, Optional, List
import time
import os, glob
from utils.debug_helper import debug_log, info_log, error_log
from core.frontend_base import BaseFrontendModule, FrontendModuleType  # type: ignore

try:
    from PyQt5.QtCore import QTimer
    PYQT5 = True
except Exception:
    PYQT5 = False
    class QTimer:  # fallback 型別
        def __init__(self): pass
        def start(self, *a, **k): pass
        def stop(self): pass
        def timeout(self, *a, **k): pass

from .core.animation_manager import AnimationManager
from .core.animation_clip import AnimationClip

class ANIModule(BaseFrontendModule):
    """ANI 前端模組：集中處理動畫，提供 MOV/UI 使用的穩定 API。
    - play(name, loop=None): 播放（含 coalesce/中斷/待播）
    - stop(): 停止
    - register_clips([{name,total_frames,fps,default_loop}]): 註冊素材
    - get_current_animation_status(): 查狀態（供 MOV 等待）
    - add_frame_callback(cb): 每一幀通知（UI 繪製）
    """

    def __init__(self, config: Optional[Dict] = None):
        super().__init__(FrontendModuleType.ANI)
        self.config = config or {}
        self.manager = AnimationManager(request_cooldown=self.config.get("request_cooldown", 0.25))
        # 讀動畫基本設定（tick 與預設幀長）
        anim_cfg = (self.config or {}).get("animation", {})
        self._tick_interval_ms = int(anim_cfg.get("frame_interval", int(1000 / self.config.get("tick_fps", 60))))
        self._default_frame_duration = float(anim_cfg.get("default_frame_duration", 0.1))
        if "request_cooldown" in anim_cfg:
            self.manager.request_cooldown = float(anim_cfg["request_cooldown"])

        # 依 config.resources 自動建立與註冊 clips
        self._apply_config_for_clips(self.config)
        self._frame_callbacks: List[Callable[[str, int], None]] = []
        self._start_callbacks:  List[Callable[[str], None]] = []
        self._finish_callbacks: List[Callable[[str], None]] = []

        # 將 manager 事件轉發給 UI callback
        self.manager.on_frame = self._emit_frame
        self.manager.on_start = self._emit_start
        self.manager.on_finish = self._emit_finish

        self.timer: Optional[QTimer] = None

    # ===== 前端生命週期 =====
    def initialize_frontend(self) -> bool:
        try:
            if PYQT5:
                self.signals.add_timer_callback("ani_update", self._on_tick)
                self.timer = QTimer()
                self.timer.timeout.connect(lambda: self.signals.timer_timeout("ani_update"))
                self.timer.start(self._tick_interval_ms)
            return True
        except Exception as e:
            from utils.debug_helper import error_log
            error_log(f"[ANI] 初始化失敗: {e}")
            return False

    def handle_frontend_request(self, data: Dict) -> Dict:
        cmd = data.get("command")
        if cmd == "play":
            name = data.get("animation_type")
            loop = data.get("loop")
            return self.play(name, loop)
        if cmd == "stop":
            self.stop()
            return {"success": True}
        if cmd == "register_clips":
            clips = data.get("clips", [])
            self.register_clips(clips)
            return {"success": True}
        if cmd == "status":
            return self.get_current_animation_status()
        return {"error": f"unknown command: {cmd}"}

    # ===== 公開 API（給 MOV/UI） =====
    def play(self, name: str, loop: Optional[bool] = None) -> Dict:
        if not name:
            return {"error": "animation name required"}
        return self.manager.play(name, loop=loop)

    def stop(self):
        self.manager.stop()

    def register_clips(self, clips_meta: list[Dict]):
        """clips_meta: [{name,total_frames,fps,default_loop}]"""
        clips = []
        for m in clips_meta:
            if not m or "name" not in m or "total_frames" not in m:
                continue
            clips.append(AnimationClip(
                name=m["name"],
                total_frames=int(m["total_frames"]),
                fps=float(m.get("fps", 30.0)),
                default_loop=bool(m.get("default_loop", True)),
            ))
        if clips:
            self.manager.register_clips(clips)

    def get_current_animation_status(self) -> Dict:
        return self.manager.get_status()

    def add_frame_callback(self, cb: Callable[[str, int], None]):
        if cb not in self._frame_callbacks:
            self._frame_callbacks.append(cb)

    # ===== 註冊回傳方法 =====
    def add_start_callback(self, cb: Callable[[str], None]):
        if cb not in self._start_callbacks:
            self._start_callbacks.append(cb)

    def add_finish_callback(self, cb: Callable[[str], None]):
        if cb not in self._finish_callbacks:
            self._finish_callbacks.append(cb)


    # ===== 計時器更新 =====
    def _on_tick(self):
        self.manager.update()

    # ===== 事件轉發 =====
    def _emit_frame(self, name: str, frame: int):
        for cb in list(self._frame_callbacks):
            try: cb(name, frame)
            except Exception: pass

    def _emit_start(self, name: str):
        debug_log(3, f"[ANI] start: {name}")
        for cb in list(self._start_callbacks):
            try: cb(name)
            except Exception as e: error_log(f"[ANI] start-callback error: {e}")

    def _emit_finish(self, name: str):
        debug_log(3, f"[ANI] finish: {name}")
        for cb in list(self._finish_callbacks):
            try: cb(name)
            except Exception as e: error_log(f"[ANI] finish-callback error: {e}")

    # ===== 其他幫手函數 =====
    def _apply_config_for_clips(self, cfg: dict):
        """
        從 config.resources 註冊 clips。
        支援兩種結構：
        A) 新版：resources.clips.{name:{prefix,total_frames,frame_duration,loop}}
        B) 舊版：resources.{category}.{name:{frame_duration,loop,total_frames}}
        """
        res = (cfg or {}).get("resources", {})
        clips = res.get("clips", None)
        default_fd = float((cfg or {}).get("animation", {}).get("default_frame_duration", getattr(self, "_default_frame_duration", 0.1)))

        def _register(name: str, meta: dict):
            if not isinstance(meta, dict):
                return
            fd = float(meta.get("frame_duration", default_fd))
            fps = 1.0 / max(fd, 1e-6)
            loop = bool(meta.get("loop", True))
            total_frames = int(meta.get("total_frames", 0))
            if total_frames <= 0:
                # 若真的沒提供 frame 數，可回退 30；建議 YAML 填好 total_frames
                total_frames = 30
            try:
                from .core.animation_clip import AnimationClip
                self.manager.register_clip(AnimationClip(
                    name=name,
                    total_frames=total_frames,
                    fps=fps,
                    default_loop=loop,
                ))
                # 可把 prefix/filename_format/index_start 留給 UI 用（ANI 不需）
                from utils.debug_helper import debug_log
                debug_log(2, f"[ANI] 註冊動畫: {name} frames={total_frames} fps={fps:.2f} loop={loop}")
            except Exception as e:
                from utils.debug_helper import error_log
                error_log(f"[ANI] 註冊動畫失敗 {name}: {e}")

        if isinstance(clips, dict):
            # 新版 clips 形式
            for name, meta in clips.items():
                _register(name, meta)
        else:
            # 舊版分類形式（idle/transitions/walking/...）
            for category, entries in res.items():
                if category in ("animations_path", "filename_format", "index_start", "aliases"):
                    continue
                if isinstance(entries, dict):
                    for name, meta in entries.items():
                        _register(name, meta)

        # 處理 aliases（可把 MOV 用名映到現有 clip）
        aliases = res.get("aliases", {})
        for alias, target in aliases.items():
            src = self.manager.clips.get(target)
            if not src:
                continue
            from .core.animation_clip import AnimationClip
            self.manager.register_clip(AnimationClip(
                name=alias,
                total_frames=src.total_frames,
                fps=src.fps,
                default_loop=src.default_loop,
            ))

    def _count_frames_on_disk(self, base_path: str, clip_name: str) -> int:
        """嘗試兩種布局：
        1) {base}/{clip_name}/xxx.png (資料夾)
        2) {base}/{clip_name}_*.png (展平檔名)
        """
        try:
            exts = ("png", "webp", "jpg", "jpeg", "gif")
            # 1) 目錄內檔案
            d = os.path.join(base_path, clip_name)
            cnt = 0
            if os.path.isdir(d):
                for ext in exts:
                    cnt += len(glob.glob(os.path.join(d, f"*.{ext}")))
                if cnt > 0:
                    return cnt
            # 2) 展平檔名
            for ext in exts:
                cnt += len(glob.glob(os.path.join(base_path, f"{clip_name}_*.{ext}")))
            return cnt
        except Exception as e:
            error_log(f"[ANI] 掃描動畫幀失敗 {clip_name}: {e}")
            return 0

    def shutdown(self):
        return super().shutdown()