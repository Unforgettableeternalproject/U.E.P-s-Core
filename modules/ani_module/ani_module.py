from __future__ import annotations
from typing import Callable, Dict, Optional, List
import time
import os, glob
import yaml
from utils.debug_helper import debug_log, debug_log_e, info_log, error_log
from core.bases.frontend_base import BaseFrontendModule, FrontendModuleType  # type: ignore
from core.states.state_manager import UEPState

try:
    from PyQt5.QtCore import QTimer # type: ignore
    from PyQt5.QtGui import QPixmap
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
        
        # 從 user_settings 讀取 performance 設定
        from configs.user_settings_manager import get_user_setting
        self.hardware_acceleration = get_user_setting("advanced.performance.enable_hardware_acceleration", True)
        self.reduce_on_battery = get_user_setting("advanced.performance.reduce_animations_on_battery", True)
        debug_log_e(2, f"[ANI] 效能設定: 硬體加速={self.hardware_acceleration}, 電池省電={self.reduce_on_battery}")
        
        # 效能指標追蹤
        self.total_frames_rendered = 0
        self.total_animation_duration = 0.0
        self.animation_type_stats = {}
        self.current_fps = 0.0

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
            # ✅ 先初始化 signals（確保 QApplication 已建立）
            self._initialize_signals()
            
            if PYQT5:
                self.signals.add_timer_callback("ani_update", self._on_tick)
                self.timer = QTimer()
                self.timer.timeout.connect(lambda: self.signals.timer_timeout("ani_update")) # type: ignore
                self.timer.start(self._tick_interval_ms)
            
            # 🔗 註冊到 FrontendBridge（如果存在）
            try:
                from core.framework import core_framework
                if hasattr(core_framework, 'frontend_bridge') and core_framework.frontend_bridge:
                    frontend_bridge = core_framework.frontend_bridge
                    frontend_bridge.register_module('ani', self)
                    from utils.debug_helper import info_log
                    info_log(f"[{self.module_id}] ✅ ANI 模組已註冊到 FrontendBridge")
                else:
                    from utils.debug_helper import debug_log
                    debug_log(2, f"[{self.module_id}] FrontendBridge 不存在，跳過註冊")
            except Exception as e:
                from utils.debug_helper import debug_log
                debug_log(2, f"[{self.module_id}] 註冊到 FrontendBridge 失敗: {e}")
            
            # 註冊 user_settings 熱重載回調
            from configs.user_settings_manager import user_settings_manager
            user_settings_manager.register_reload_callback("ani_module", self._reload_from_user_settings)
            
            return True
        except Exception as e:
            from utils.debug_helper import error_log
            error_log(f"[ANI] 初始化失敗: {e}")
            return False
    
    def handle_frontend_request(self, data: Dict) -> Dict:
        cmd = data.get("command")
        
        # 更新效能指標
        animation_type = data.get("animation_type", "unknown")
        self.animation_type_stats[animation_type] = self.animation_type_stats.get(animation_type, 0) + 1
        self.update_custom_metric('animation_type', animation_type)
        
        if cmd == "play":
            name = data.get("animation_type")
            loop = data.get("loop")
            return self.play(name, loop) # type: ignore
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
        """播放動畫（純播放器，不做相容性檢查）"""
        if not name:
            debug_log(2, "[ANI] play: 動畫名稱為空")
            return {"error": "animation name required"}
        
        # 只在非 coalesced 的情況下記錄（減少日誌洗屏）
        result = self.manager.play(name, loop=loop)
        if not result.get("coalesced"):
            debug_log(2, f"[ANI] 播放動畫: {name}, loop={loop}")
        return result
    
    def stop(self):
        self.manager.stop()
    
    def set_current_frame(self, frame_index: int) -> Dict:
        """
        直接設置當前動畫的幀索引（不重新播放）
        
        用於 turn_head 等需要即時響應的動畫
        
        Args:
            frame_index: 目標幀索引
            
        Returns:
            操作結果
        """
        result = self.manager.set_frame(frame_index)
        if result.get("success"):
            debug_log(3, f"[ANI] 設置幀索引: {frame_index}")
        return result

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

    def get_current_frame(self):
        """
        UI 每個 tick 會來拉目前幀的畫面；回傳 QPixmap 或 None。
        新增縮放和偏移變換支援。
        """
        try:
            st = self.get_current_animation_status()  # 你現有的狀態查詢介面
            if not st or not st.get("name") or not st.get("is_playing"):
                # 移除洗屏日誌 - 這個情況非常常見不需記錄
                return None
                
            anim_name = st["name"]
            idx = st.get("frame")
            if idx is None:
                return None

            # 從狀態中獲取變換屬性
            zoom = st.get("zoom", 1.0)
            offset_x = st.get("offset_x", 0)
            offset_y = st.get("offset_y", 0)

            # 檢查是否有變換的快取（只考慮偏移，不考慮縮放）
            transform_key = (anim_name, idx, 1.0, offset_x, offset_y)
            pm = self._try_get_transformed_cached_pixmap(transform_key)
            if pm is not None:
                return pm

            # 先獲取原始圖片
            original_pm = self._try_get_cached_pixmap(anim_name, idx)
            if original_pm is None:
                # 沒快取就組檔名並載入
                frame_path = self._resolve_frame_path(anim_name, idx)
                if not frame_path or not os.path.exists(frame_path):
                    debug_log(2, f"[ANI] get_current_frame: 檔案不存在 {frame_path}")
                    return None

                if not PYQT5:
                    debug_log(3, f"[ANI] get_current_frame: PyQt5 不可用，無法載入 QPixmap")
                    return None

                # 確保有 QApplication 實例
                try:
                    from PyQt5.QtWidgets import QApplication
                    if not QApplication.instance():
                        debug_log(3, f"[ANI] get_current_frame: 沒有 QApplication，無法載入 QPixmap")
                        return None
                except ImportError:
                    debug_log(3, f"[ANI] get_current_frame: 無法導入 QApplication")
                    return None

                original_pm = QPixmap(frame_path) # type: ignore
                if original_pm.isNull():
                    debug_log(2, f"[ANI] get_current_frame: QPixmap 載入失敗 {frame_path}")
                    return None
                    
                # 放到原始圖片快取
                self._cache_pixmap(anim_name, idx, original_pm)

            # 應用變換（只處理偏移，縮放交給 UI 層處理）
            if offset_x != 0 or offset_y != 0:
                debug_log(3, f"[ANI] 應用 offset 變換: {anim_name} frame={idx}, offset_x={offset_x}, offset_y={offset_y}")
            transformed_pm = self._apply_transform(original_pm, 1.0, offset_x, offset_y)  # zoom 固定為 1.0
            if transformed_pm:
                # 放到變換快取
                self._cache_transformed_pixmap(transform_key, transformed_pm)
                return transformed_pm
            else:
                return original_pm
            
        except Exception as e:
            # 不要讓 UI 噴例外，穩穩地回 None 就好
            # 用你的 debug_helper
            debug_log(2, f"[ANI] get_current_frame 失敗: {e}")
            return None

    def get_clip_info(self, name: str):
        c = self.manager.clips.get(name)
        if not c: 
            return None
        return {
            "frames": c.total_frames, 
            "fps": c.fps, 
            "loop": c.default_loop,
            "zoom": c.zoom,
            "offset_x": c.offset_x,
            "offset_y": c.offset_y
        }

    def get_clip_duration(self, name: str) -> float:
        info = self.get_clip_info(name)
        if not info: 
            return 0.0
        frames = max(1, int(info["frames"]))
        fps = max(1e-6, float(info["fps"]))
        return frames / fps

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
        for cb in list(self._start_callbacks):
            try: cb(name)
            except Exception as e: error_log(f"[ANI] start-callback error: {e}")

    def _emit_finish(self, name: str):
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

        debug_log(1, f"[ANI] 開始註冊動畫 clips，config.resources: {list(res.keys())}")
        if clips:
            debug_log(1, f"[ANI] 找到 {len(clips)} 個 clips: {list(clips.keys())}")

        def _register(name: str, meta: dict):
            if not isinstance(meta, dict):
                debug_log(2, f"[ANI] 跳過無效 meta: {name} -> {meta}")
                return
            fd = float(meta.get("frame_duration", default_fd))
            fps = 1.0 / max(fd, 1e-6)
            loop = bool(meta.get("loop", True))
            total_frames = int(meta.get("total_frames", 0))
            
            # 新增：讀取縮放和偏移屬性
            zoom = float(meta.get("zoom", 1.0))
            offset_x = int(meta.get("offsetX", 0))
            offset_y = int(meta.get("offsetY", 0))
            
            if total_frames <= 0:
                # 若真的沒提供 frame 數，可回退 30；建議 YAML 填好 total_frames
                total_frames = 30
                debug_log_e(2, f"[ANI] {name} 使用預設幀數: {total_frames}")
            try:
                from .core.animation_clip import AnimationClip
                self.manager.register_clip(AnimationClip(
                    name=name,
                    total_frames=total_frames,
                    fps=fps,
                    default_loop=loop,
                    zoom=zoom,
                    offset_x=offset_x,
                    offset_y=offset_y,
                ))
                # 可把 prefix/filename_format/index_start 留給 UI 用（ANI 不需）
                from utils.debug_helper import debug_log
                # 動畫註冊成功（不輸出日誌以減少噪音）
                pass
            except Exception as e:
                from utils.debug_helper import error_log
                error_log(f"[ANI] ✗ 註冊動畫失敗 {name}: {e}")

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
        debug_log(1, f"[ANI] 處理 {len(aliases)} 個 aliases: {list(aliases.keys())}")
        for alias, target in aliases.items():
            src = self.manager.clips.get(target)
            if not src:
                debug_log(2, f"[ANI] alias 目標不存在: {alias} -> {target}")
                continue
            from .core.animation_clip import AnimationClip
            self.manager.register_clip(AnimationClip(
                name=alias,
                total_frames=src.total_frames,
                fps=src.fps,
                default_loop=src.default_loop,
                zoom=src.zoom,
                offset_x=src.offset_x,
                offset_y=src.offset_y,
            ))
            debug_log(1, f"[ANI] ✓ 註冊 alias: {alias} -> {target}")

        total_clips = len(self.manager.clips)
        debug_log(1, f"[ANI] 動畫註冊完成，總計 {total_clips} 個 clips")

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

    def _try_get_cached_pixmap(self, anim_name: str, idx: int):
        cache = getattr(self, "_pixmap_cache", None)
        if not cache:
            return None
        return cache.get((anim_name, idx))

    def _cache_pixmap(self, anim_name: str, idx: int, pm: QPixmap):
        if not hasattr(self, "_pixmap_cache"):
            self._pixmap_cache = {}
        self._pixmap_cache[(anim_name, idx)] = pm

    def _resolve_frame_path(self, anim_name: str, idx: int) -> str:
        """
        根據實際檔案結構解析動畫幀路徑。
        實際結構：resources/animations/{anim_name}/{prefix}{idx:02d}.png
        支援 alias 動畫解析到原始檔案路徑
        自動根據幀數選擇正確的格式 (02d 或 03d)
        """
        try:
            # 取得基礎路徑
            base_animations_path = self.config.get("resources", {}).get("animations_path", "resources/animations")
            
            # 如果是相對路徑，轉為絕對路徑
            if not os.path.isabs(base_animations_path):
                # 假設相對於專案根目錄
                script_dir = os.path.dirname(os.path.abspath(__file__))
                project_root = os.path.abspath(os.path.join(script_dir, '..', '..'))
                base_animations_path = os.path.join(project_root, base_animations_path)
            
            # 查找對應的 clip 配置
            clips_config = self.config.get("resources", {}).get("clips", {})
            
            # 檢查是否為 alias，如果是則解析到原始動畫
            actual_anim_name = anim_name
            if anim_name not in clips_config:
                # 可能是 alias，查找別名映射
                aliases = self.config.get("resources", {}).get("aliases", {})
                if anim_name in aliases:
                    target_name = aliases[anim_name]
                    if target_name in clips_config:
                        actual_anim_name = target_name
                        debug_log(2, f"[ANI] alias 解析: {anim_name} -> {actual_anim_name}")
                    else:
                        debug_log(2, f"[ANI] alias 目標不存在: {anim_name} -> {target_name}")
                        return ""
                else:
                    debug_log(2, f"[ANI] 動畫配置不存在: {anim_name}")
                    return ""
            
            clip_info = clips_config.get(actual_anim_name, {})
            
            # 取得 prefix（保持完整的 prefix，因為實際檔名包含底線）
            prefix = clip_info.get("prefix", f"{actual_anim_name}_")
            # 注意：不要移除底線，因為實際檔案名是 diamond_girl_angry_idle_00.png
            
            # 根據總幀數自動選擇格式
            total_frames = clip_info.get("total_frames", 100)
            if total_frames >= 100:
                # 使用 3 位數格式
                filename = f"{prefix}{idx:03d}.png"
            else:
                # 使用 2 位數格式
                filename = f"{prefix}{idx:02d}.png"
            
            full_path = os.path.join(base_animations_path, actual_anim_name, filename)
            
            # 只在檔案不存在時才記錄錯誤
            if not os.path.exists(full_path):
                debug_log(2, f"[ANI] 檔案不存在: {anim_name}[{idx}] -> {full_path}")
            
            return full_path
            
        except Exception as e:
            error_log(f"[ANI] 路徑解析失敗 {anim_name}[{idx}]: {e}")
            return ""
    
    def _try_get_transformed_cached_pixmap(self, transform_key):
        """獲取變換後的快取圖片"""
        cache = getattr(self, "_transformed_pixmap_cache", None)
        if not cache:
            return None
        return cache.get(transform_key)
    
    def _cache_transformed_pixmap(self, transform_key, pm: QPixmap):
        """快取變換後的圖片"""
        if not hasattr(self, "_transformed_pixmap_cache"):
            self._transformed_pixmap_cache = {}
        self._transformed_pixmap_cache[transform_key] = pm
    
    def _apply_transform(self, original_pm: QPixmap, zoom: float, offset_x: int, offset_y: int) -> QPixmap:
        """應用縮放和偏移變換"""
        try:
            if not PYQT5:
                return original_pm
                
            # 如果沒有變換，直接返回原圖
            if zoom == 1.0 and offset_x == 0 and offset_y == 0:
                return original_pm
                
            from PyQt5.QtCore import Qt
            from PyQt5.QtGui import QPainter
            
            # 計算變換後的尺寸
            orig_width = original_pm.width()
            orig_height = original_pm.height()
            
            if zoom != 1.0:
                # 縮放變換
                scaled_width = int(orig_width * zoom)
                scaled_height = int(orig_height * zoom)
                scaled_pm = original_pm.scaled(scaled_width, scaled_height, Qt.KeepAspectRatio, Qt.SmoothTransformation) # type: ignore
            else:
                scaled_pm = original_pm
                scaled_width = orig_width
                scaled_height = orig_height
            
            # 如果有偏移，需要創建新的畫布
            if offset_x != 0 or offset_y != 0:
                # 計算新畫布大小（確保能容納偏移後的圖片）
                canvas_width = scaled_width + abs(offset_x)
                canvas_height = scaled_height + abs(offset_y)
                
                # 創建新的透明畫布
                result_pm = QPixmap(canvas_width, canvas_height)
                result_pm.fill(Qt.transparent)
                
                # 🎯 計算繪製位置（修正邏輯）
                # offsetY > 0: 圖片向上移動 → 在畫布下方留白 → draw_y = abs(offset_y)
                # offsetY < 0: 圖片向下移動 → 在畫布上方留白 → draw_y = 0
                # offsetX 同理
                draw_x = abs(offset_x) if offset_x < 0 else 0
                draw_y = abs(offset_y) if offset_y < 0 else 0
                
                # 在新畫布上繪製偏移後的圖片
                painter = QPainter(result_pm)
                painter.setRenderHint(QPainter.Antialiasing)
                painter.drawPixmap(draw_x, draw_y, scaled_pm)
                painter.end()
                
                return result_pm
            else:
                return scaled_pm
                
        except Exception as e:
            error_log(f"[ANI] 變換失敗: {e}")
            return original_pm
    
    # ===== 回調註冊（供外部模組使用）=====
    
    def on_module_busy(self, module_name: str):
        """當其他模組忙碌時顯示處理動畫"""
        try:
            debug_log(3, f"[ANI] 模組忙碌: {module_name}")
            # 可以根據不同模組選擇不同的忙碌動畫
            self.play("thinking", loop=True)
        except Exception as e:
            error_log(f"[ANI] 處理模組忙碌狀態失敗: {e}")
    
    # ===== 關閉方法 =====
    
    def _reload_from_user_settings(self, key_path: str, value: Any):
        """處理 user_settings 熱重載"""
        try:
            if key_path == "advanced.performance.enable_hardware_acceleration":
                self.hardware_acceleration = value
                info_log(f"[ANI] 硬體加速設定已更新: {value}")
            elif key_path == "advanced.performance.reduce_animations_on_battery":
                self.reduce_on_battery = value
                info_log(f"[ANI] 電池省電模式已更新: {value}")
            elif key_path == "interface.appearance.animation_quality":
                info_log(f"[ANI] 動畫品質設定已更新: {value} (需重載 ANI 模組生效)")
        except Exception as e:
            error_log(f"[ANI] 熱重載設定失敗: {e}")
    
    def shutdown(self):
        """關閉動畫模組，停止所有計時器和清理資源"""
        info_log(f"[{self.module_id}] 開始關閉動畫模組")
        
        # 停止動畫管理器
        try:
            if hasattr(self, 'manager') and self.manager:
                self.manager.stop()
                info_log(f"[{self.module_id}] 動畫管理器已停止")
        except Exception as e:
            error_log(f"[{self.module_id}] 停止動畫管理器失敗: {e}")
        
        # 停止計時器
        try:
            if hasattr(self, 'timer') and self.timer:
                self.timer.stop()
                self.timer.deleteLater() if hasattr(self.timer, 'deleteLater') else None
                self.timer = None
                info_log(f"[{self.module_id}] 計時器已停止並清理")
        except Exception as e:
            error_log(f"[{self.module_id}] 停止計時器失敗: {e}")
        
        # 清理信號回調
        try:
            if hasattr(self, 'signals') and self.signals:
                if hasattr(self.signals, 'remove_timer_callback'):
                    self.signals.remove_timer_callback("ani_update")
                    info_log(f"[{self.module_id}] 信號回調已清理")
                else:
                    info_log(f"[{self.module_id}] 信號系統無remove_timer_callback方法")
        except Exception as e:
            error_log(f"[{self.module_id}] 清理信號回調失敗: {e}")
        
        return super().shutdown()
    
    def get_performance_window(self) -> dict:
        """獲取效能數據窗口（包含 ANI 特定指標）"""
        window = super().get_performance_window()
        window['total_frames_rendered'] = self.total_frames_rendered
        window['total_animation_duration'] = self.total_animation_duration
        window['animation_type_distribution'] = self.animation_type_stats.copy()
        window['current_fps'] = self.current_fps
        window['avg_frame_time'] = (
            self.total_animation_duration / self.total_frames_rendered
            if self.total_frames_rendered > 0 else 0.0
        )
        return window