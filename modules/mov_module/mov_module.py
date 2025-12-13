# modules/mov_module/mov_module.py
"""
MOV 協調器（重構版）

- 保留前端模組契約：initialize_frontend / handle_frontend_request / register_event_handler
- 邏輯分流：
  * core/position.py, core/physics.py, core/state_machine.py
  * behaviors/*（Idle / Movement / Transition）
- 動畫：不在 MOV 內處理。優先呼叫 ani_module.play(...)；若未注入，轉交 animation_callbacks。
- 日誌：utils.debug_helper（debug_log/info_log/error_log）
"""

from __future__ import annotations

import math
import os
import random
import time
import yaml
from typing import Callable, Optional, Dict, Any, List
from types import SimpleNamespace

from core.bases.frontend_base import BaseFrontendModule, FrontendModuleType, UIEventType
from core.states.state_manager import UEPState

try:
    from PyQt5.QtCore import QTimer
    from PyQt5.QtGui import QCursor
    PYQT5 = True
except Exception:
    PYQT5 = False
    class QTimer:  # fallback 型別
        def __init__(self): pass
    class QCursor:  # fallback
        @staticmethod
        def pos():
            return type('Point', (), {'x': lambda: 0, 'y': lambda: 0})()
        def start(self, *a, **k): pass
        def stop(self): pass
        def timeout(self, *a, **k): pass

# 拆出核心/行為
try:
    from .core.position import Position, Velocity
    from .core.physics import PhysicsEngine
    from .core.state_machine import MovementStateMachine, MovementMode, BehaviorState
    from .core.drag_tracker import DragTracker
    from .core.tease_tracker import TeaseTracker
    from .core.animation_query import AnimationQueryHelper
    from .core.animation_priority import AnimationPriorityManager, AnimationPriority
    from .behaviors.base_behavior import BehaviorContext, BehaviorFactory
    from .handlers import CursorTrackingHandler, ThrowHandler, FileDropHandler
    # from .idle_manager import IdleManager  # TODO: 睡眠功能尚未實作
except Exception:
    from core.position import Position, Velocity  # type: ignore
    from core.physics import PhysicsEngine  # type: ignore
    from core.state_machine import MovementStateMachine, MovementMode, BehaviorState  # type: ignore
    from core.drag_tracker import DragTracker  # type: ignore
    from core.tease_tracker import TeaseTracker  # type: ignore
    from core.animation_query import AnimationQueryHelper  # type: ignore
    from core.animation_priority import AnimationPriorityManager, AnimationPriority  # type: ignore
    from behaviors.base_behavior import BehaviorContext, BehaviorFactory  # type: ignore
    from handlers import CursorTrackingHandler, ThrowHandler  # type: ignore

# 日誌
from utils.debug_helper import debug_log, info_log, error_log

# 使用者設定管理器
from configs.user_settings_manager import user_settings_manager, get_user_setting


class MOVModule(BaseFrontendModule):
    """移動/行為協調器"""

    DRAG_PAUSE_REASON = "拖拽中"

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(FrontendModuleType.MOV)
        self.config = config or {}

        # --- 位置/速度 ---
        self.position = Position(self.config.get("init_x", 100), self.config.get("init_y", 100))
        self.velocity = Velocity(0.0, 0.0)
        self.target_velocity = Velocity(0.0, 0.0)

        # --- 核心模組 ---
        # 從 config.yaml 的 physics 區段讀取參數
        physics_config = self.config.get("physics", {})
        # ground_friction 優先從 user_settings.yaml 讀取，再從 mov_module.yaml 讀取
        user_ground_friction = get_user_setting("behavior.movement.ground_friction", None)
        ground_friction_value = float(user_ground_friction if user_ground_friction is not None else physics_config.get("ground_friction", 0.95))
        self.physics = PhysicsEngine(
            gravity=float(physics_config.get("gravity", 0.8)),
            damping=float(physics_config.get("damping", 0.978)),  # 已棄用，保留相容性
            ground_friction=ground_friction_value,
            air_resistance=float(physics_config.get("air_resistance", 0.985)),
            bounce_factor=float(physics_config.get("bounce_factor", 0.4)),
        )
        debug_log(2, f"[{self.module_id}] 地面摩擦係數: {ground_friction_value:.3f} (來源: {'user_settings' if user_ground_friction is not None else 'mov_module'}）")
        self.sm = MovementStateMachine()

        # --- 模式/行為 ---
        self.movement_mode = MovementMode.GROUND
        self.current_behavior_state: BehaviorState = self.sm.choose_initial_state()
        self.previous_behavior_state: Optional[BehaviorState] = None  # 追蹤前一個狀態
        self.current_behavior = BehaviorFactory.create(self.current_behavior_state)
        self.facing_direction = 1  # -1 左 / +1 右

        # --- 邊界/尺寸 ---
        self.SIZE = self.config.get("window_size", 250)
        self.GROUND_OFFSET = self.config.get("ground_offset", 48)
        self._current_animation_offset_x = 0  # 🎯 追蹤當前動畫的 X 軸偏移（從 ANI 取得）
        self._current_animation_offset_y = 0  # 🎯 追蹤當前動畫的 Y 軸偏移（從 ANI 取得）
        self.screen_width = self.config.get("screen_width", 1920)
        self.screen_height = self.config.get("screen_height", 1080)
        self.v_left = 0
        self.v_top = 0
        self.v_right = self.screen_width
        self.v_bottom = self.screen_height
        self._detect_virtual_desktop()  # 多螢幕

        # --- 目標 ---
        self.movement_target: Optional[Position] = None
        self.target_reach_threshold = float(self.config.get("target_reach_threshold", 30.0))  # 增加閾值，避免太快到達
        self.target_reached = True

        # --- 速度參數 ---
        self.GROUND_SPEED = float(self.config.get("ground_speed", 2.2))
        self.FLOAT_MIN_SPEED = float(self.config.get("float_min_speed", 1.0))
        self.FLOAT_MAX_SPEED = float(self.config.get("float_max_speed", 3.5))
        
        # --- 邊界處理模式 ---
        # "barrier": 碰到邊界停止（預設）
        # "wrap": 從右邊出去左邊進來（循環模式）
        # 優先從 user_settings.yaml 讀取
        user_boundary_mode = get_user_setting("behavior.movement.boundary_mode", None)
        self.boundary_mode = user_boundary_mode if user_boundary_mode is not None else self.config.get("boundary_mode", "barrier")
        debug_log(2, f"[{self.module_id}] 邊界模式: {self.boundary_mode} (來源: {'user_settings' if user_boundary_mode is not None else 'mov_module'})")

        # 效能指標追蹤
        self.total_distance_moved = 0.0
        self.total_movements = 0
        self.movement_type_stats = {}

        # --- 控制旗標 ---
        self.is_being_dragged = False
        self.movement_paused = False
        self.pause_reasons: set[str] = set()
        self.pause_reason = ""
        self._on_call_active = False
        
        # --- 拖曳追蹤 ---
        self._drag_start_position: Optional[Position] = None
        self._drag_start_mode: Optional[MovementMode] = None  # 記錄拖曳前的模式
        self._drag_tracker = DragTracker(max_history=5)
        
        # --- 互動追蹤（tease 動畫） ---
        tease_config = self.config.get("tease_tracking", {})
        self._tease_tracker = TeaseTracker(
            time_window=float(tease_config.get("time_window", 10.0)),
            interaction_threshold=int(tease_config.get("interaction_threshold", 3))
        )
        
        # --- 處理器 ---
        self._cursor_tracking_handler = CursorTrackingHandler(self)
        self._throw_handler = ThrowHandler(self)
        self._file_drop_handler = FileDropHandler(self)
        
        # 初始化處理器的使用者設定
        self._cursor_tracking_enabled = get_user_setting("behavior.movement.enable_cursor_tracking", True)
        user_throw_enabled = get_user_setting("behavior.movement.enable_throw_behavior", True)
        user_max_throw_speed = get_user_setting("behavior.movement.max_throw_speed", None)
        
        # 套用投擲設定
        if not user_throw_enabled:
            self._throw_handler.throw_threshold_speed = 999999.0
            debug_log(2, f"[{self.module_id}] 投擲行為已禁用（使用者設定）")
        if user_max_throw_speed is not None:
            self._throw_handler.max_throw_speed = float(user_max_throw_speed)
            debug_log(2, f"[{self.module_id}] 最大投擲速度: {user_max_throw_speed} (來源: user_settings)")
        
        # --- 投擲後行為標記（由 ThrowHandler 管理，這裡保留供 _enter_behavior 使用） ---
        self._post_throw_tease_pending = False
        
        # --- 移動平滑化 ---
        # 優先從 user_settings.yaml 讀取
        user_smoothing = get_user_setting("behavior.movement.movement_smoothing", None)
        smoothing_config = self.config.get("movement_smoothing", {})
        self._smoothing_enabled = user_smoothing if user_smoothing is not None else smoothing_config.get("enabled", True)
        self._velocity_lerp_factor = float(smoothing_config.get("velocity_lerp_factor", 0.15))
        self._pause_damping = float(smoothing_config.get("pause_damping", 0.85))
        self._resume_acceleration = float(smoothing_config.get("resume_acceleration", 0.2))
        self._smooth_velocity = Velocity(0.0, 0.0)  # 平滑後的速度
        self._pause_velocity_buffer = Velocity(0.0, 0.0)  # 暫停前的速度緩衝
        debug_log(2, f"[{self.module_id}] 移動平滑化: {self._smoothing_enabled} (來源: {'user_settings' if user_smoothing is not None else 'mov_module'})")
        
        # --- 入場行為 ---
        self._entry_behavior_config = self.config.get("entry_behavior", {})
        self._is_entering = False
        self._entry_complete = False
        self._is_leaving = False
        self._last_hide_position: Optional[tuple] = None  # 記住隱藏前的位置

        # --- 轉場共享狀態（交給 TransitionBehavior 用） ---
        self.transition_start_time: Optional[float] = None
        self._transition_animation_finished = False  # 追蹤轉場動畫是否完成
        self.movement_locked_until: float = 0.0  # 鎖移動（通常用於轉場/轉頭）

        # --- 動畫管道 ---
        self.ani_module = None  # 可注入：ANI 前端模組實例（具 play/stop/get_status）
        self._animation_callbacks: List[Callable[[str, dict], None]] = []  # 舊相容
        self._position_callbacks: List[Callable[[int, int], None]] = []
        self.WAIT_ANIM_REASON = "等待動畫"
        self._awaiting_anim: Optional[str] = None
        self._await_deadline: float = 0.0
        self._await_follow: Optional[Callable[[], None]] = None
        self._default_anim_timeout = float(self.config.get("anim_timeout", 2.0))
        
        # --- Qt 橋接器（線程安全的動畫觸發） ---
        self._qt_bridge = None  # 將在 initialize_frontend 後創建
        
        # --- 動畫查詢輔助器 ---
        self._state_animation_config = self._load_state_animation_config()
        self.anim_query = AnimationQueryHelper(
            ani_module=None,  # 將在 initialize_frontend 後設置
            state_animation_config=self._state_animation_config
        )
        
        # --- 動畫優先度管理器 ---
        self._animation_priority = AnimationPriorityManager(
            module_id=self.module_id,
            config=self.config
        )
        debug_log(2, f"[{self.module_id}] AnimationPriorityManager 初始化完成 "
                     f"(enabled={self._animation_priority.enabled})")
        
        # --- 層級動畫策略 ---
        try:
            from .strategies.layer_strategy import LayerAnimationStrategy
            self._layer_strategy = LayerAnimationStrategy(self, self._state_animation_config)
            debug_log(2, f"[{self.module_id}] LayerAnimationStrategy 初始化完成")
        except Exception as e:
            error_log(f"[{self.module_id}] LayerAnimationStrategy 初始化失敗: {e}")
            self._layer_strategy = None
        
        # --- 層級事件處理器 ---
        try:
            from .handlers.layer_handler import LayerEventHandler
            self._layer_handler = LayerEventHandler(self)
            debug_log(2, f"[{self.module_id}] LayerEventHandler 初始化完成")
        except Exception as e:
            error_log(f"[{self.module_id}] LayerEventHandler 初始化失敗: {e}")
            self._layer_handler = None


        # --- 停滯保護 ---
        self.last_movement_time = time.time()
        self.max_idle_time = float(self.config.get("max_idle_time", 5.0))

        # --- 日誌頻率控制 ---
        self._drag_log_counter = 0
        self._behavior_log_counter = 0
        self.LOG_INTERVAL = 30  # 每30次輸出一次日誌

        # --- 計時器 ---
        self.movement_timer: Optional[QTimer] = None
        self.behavior_timer: Optional[QTimer] = None

        # --- 其他設定 ---
        self._approach_k = 0.12                  # 速度趨近係數（預設）
        self.screen_padding = 50                 # 目標夾取安全邊距
        self.keep_on_screen = True
        self.bounce_off_edges = False
        self._apply_config(self.config)

        # --- MISCHIEF 行為控制 ---
        self.mischief_active: bool = False
        self._mischief_pending_target: Optional[Position] = None
        self._mischief_pending_anim: Optional[str] = None
        self._mischief_end_at: float = 0.0
        self._mischief_anim_timeout: float = 1.5
        self._mischief_info: Dict[str, Any] = {}
        
        # --- 狀態動畫系統 ---
        self._current_layer: Optional[str] = None  # "input", "processing", "output"
        self._current_system_state: UEPState = UEPState.IDLE
        self._current_gs_id: Optional[str] = None  # 當前 General Session ID
        self._state_animation_config: Optional[Dict] = None
        self._current_playing_anim: Optional[str] = None  # 當前播放的動畫名稱（用於避免重複觸發）
        
        # --- SLEEP 狀態管理 ---
        self._is_sleeping: bool = False  # 是否處於睡眠狀態
        self._pending_sleep_transition: bool = False  # 是否等待執行睡眠轉換 (f_to_g 完成後)
        self._pending_wake_transition: bool = False  # 是否等待完成喚醒轉換 (l_to_g 完成後)
        self._wake_ready: bool = False  # 是否收到 WAKE_READY 事件（模組已重載）
        
        # 🔧 閒置管理器（自動睡眠）
        # TODO: 睡眠功能尚未實作，暫時不初始化 IdleManager
        # self.idle_manager = IdleManager()
        # self.idle_manager.set_sleep_callback(self._enter_sleep_mode)
        # self.idle_manager.set_wake_callback(self._exit_sleep_mode)
        
        # 🔧 註冊 user_settings 熱重載回調會在 initialize_frontend() 中進行
        # （確保 QApplication 已建立後才註冊，避免過早觸發）

        info_log(f"[{self.module_id}] MOV 初始化完成")

    # ========= 前端生命週期 =========

    def initialize_frontend(self) -> bool:
        """初始化計時器、事件與初始行為"""
        debug_log(1, "前端 - MOV 初始化中")
        try:
            # 計時器 → 交給 BaseFrontendModule.signals 轉發
            # ✅ 檢查 QApplication 是否已就緒再創建 QTimer
            if PYQT5:
                from PyQt5.QtWidgets import QApplication
                if QApplication.instance() is not None:
                    # QApplication 已就緒，可以安全創建 Qt 對象
                    self._initialize_signals()  # 初始化父類的 signals
                    
                    if self.signals:
                        self.signals.add_timer_callback("mov_behavior", self._tick_behavior)
                        self.signals.add_timer_callback("mov_movement", self._tick_movement)

                    self.behavior_timer = QTimer()
                    self.behavior_timer.timeout.connect(lambda: self.signals.timer_timeout("mov_behavior") if self.signals else self._tick_behavior())
                    self.behavior_timer.start(int(self.config.get("behavior_interval_ms", 100)))

                    self.movement_timer = QTimer()
                    self.movement_timer.timeout.connect(lambda: self.signals.timer_timeout("mov_movement") if self.signals else self._tick_movement())
                    self.movement_timer.start(int(self.config.get("movement_interval_ms", 16)))
                    
                    debug_log(2, f"[{self.module_id}] Qt 計時器已初始化")
                else:
                    # QApplication 尚未就緒，延後 Qt 對象創建
                    debug_log(2, f"[{self.module_id}] QApplication 尚未就緒，延後 Qt 計時器初始化")
                    self.behavior_timer = None
                    self.movement_timer = None

            # 事件
            self._register_handlers()

            # === 初始化滑鼠追蹤處理器 ===
            # pet_app 由 UI 模組在創建後透過 set_pet_app() 設置
            debug_log(2, f"[{self.module_id}] 滑鼠追蹤處理器將在 pet_app 設置後初始化")
            
            # === 自動尋找並注入 ANI（多種途徑擇一）===
            maybe_ani = self.config.get("ani") or getattr(self, "ani_module", None)
            if not maybe_ani and hasattr(self, "dependencies"):
                # 若你的框架有依賴表
                maybe_ani = self.dependencies.get("ANI") or self.dependencies.get(FrontendModuleType.ANI)  # type: ignore
            if not maybe_ani and hasattr(self, "get_dependency"):
                try:
                    maybe_ani = self.get_dependency(FrontendModuleType.ANI)  # 某些基底可能提供
                except Exception:
                    pass
            if maybe_ani:
                self.attach_ani(maybe_ani)
                # 同時將 ANI 模組傳給動畫查詢輔助器
                self.anim_query.ani_module = self.ani_module
                # Qt 橋接器將在 attach_ani() 中創建

            # 入場動畫延遲到 UI 準備好後再播放
            # 標記需要播放入場動畫，由 UI 模組在顯示時觸發
            self._should_play_entry = self._entry_behavior_config.get("enabled", True)
            if self._should_play_entry:
                # 設置起始位置（但不播放動畫）
                start_pos = self._entry_behavior_config.get("start_position", "top_center")
                self._set_entry_start_position(start_pos)
                self._is_entering = True  # 標記為入場狀態
                # 暫停移動直到動畫完成
                self.pause_movement("entry_animation")
                debug_log(1, f"[{self.module_id}] 入場動畫將在 UI 顯示後播放")
            else:
                # 沒有入場動畫時才初始化位置
                self._initialize_position()
                # 直接進入第一個行為
                debug_log(1, f"[{self.module_id}] 初始行為: {self.current_behavior_state.value}")
                self._enter_behavior(self.current_behavior_state)
            
            # 訂閱層級事件以驅動動畫
            self._subscribe_to_layer_events()
            
            # 載入狀態動畫配置
            self._state_animation_config = self._load_state_animation_config()
            
            # 🔗 註冊到 FrontendBridge（如果存在）
            try:
                from core.framework import core_framework
                if hasattr(core_framework, 'frontend_bridge') and core_framework.frontend_bridge:
                    frontend_bridge = core_framework.frontend_bridge
                    frontend_bridge.register_module('mov', self)
                    info_log(f"[{self.module_id}] ✅ MOV 模組已註冊到 FrontendBridge")
                else:
                    debug_log(2, f"[{self.module_id}] FrontendBridge 不存在，跳過註冊")
            except Exception as e:
                debug_log(2, f"[{self.module_id}] 註冊到 FrontendBridge 失敗: {e}")
            
            # 註冊使用者設定熱重載回調
            user_settings_manager.register_reload_callback("mov_module", self._reload_from_user_settings)
            debug_log(2, f"[{self.module_id}] 已註冊使用者設定熱重載回調")

            return True
        except Exception as e:
            error_log(f"[{self.module_id}] 前端初始化失敗: {e}")
            return False

    def handle_frontend_request(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """對外 API（必要時可擴充）"""
        try:
            cmd = data.get("command")
            
            # 更新效能指標
            movement_type = data.get("action_id") or cmd or "unknown"
            self.movement_type_stats[movement_type] = self.movement_type_stats.get(movement_type, 0) + 1
            self.update_custom_metric('movement_type', movement_type)
            
            if cmd in ["set_position", "set_velocity", "mischief_action", "play_animation"]:
                self.total_movements += 1
            
            if cmd == "get_status":
                return self._api_get_status()
            if cmd == "set_position":
                return self._api_set_position(data)
            if cmd == "set_velocity":
                return self._api_set_velocity(data)
            if cmd == "mischief_action":
                # 手動觸發 MISCHIEF 行為（測試/除錯用）
                action_id = data.get("action_id", "unknown")
                target = data.get("target")
                animation = data.get("animation")
                self._start_mischief_action(action_id, target, animation)
                return {"success": True, "action": action_id, "target": target, "animation": animation}
            if cmd == "mischief_event":
                # 從 FrontendBridge/事件直接觸發具體行為（含目標定位）
                return self._handle_mischief_event(data)
            if cmd == "inject_ani":
                ani = data.get("ani")
                if ani is None:
                    return {"error": "ANI模組為必備元件"}
                self.attach_ani(ani)
            if cmd == "play_animation":
                name = data.get("name") or data.get("animation_type")
                params = data.get("params", {}) or {}
                if not name:
                    return {"error": "animation name required"}
                # 走統一入口（內部會自動處理 await_finish / loop / 超時）
                self._trigger_anim(name, params, source="frontend_request")
                return {"success": True, "animation": name}
            return {"error": f"未知命令: {cmd}"}
        except Exception as e:
            error_log(f"[{self.module_id}] 請求處理錯誤: {e}")
            return {"error": str(e)}
    
    def initialize_qt_timers(self):
        """在 QApplication 就緒後初始化 Qt 計時器（由 UI 模組調用）"""
        if not PYQT5:
            return
        
        try:
            from PyQt5.QtWidgets import QApplication
            if QApplication.instance() is None:
                debug_log(2, f"[{self.module_id}] QApplication 尚未就緒，無法初始化計時器")
                return
            
            # 如果計時器已經創建，不重複創建
            if hasattr(self, 'behavior_timer') and self.behavior_timer is not None:
                debug_log(2, f"[{self.module_id}] Qt 計時器已初始化，跳過")
                return
            
            # 初始化父類的 signals
            self._initialize_signals()
            
            # 創建行為計時器
            if self.signals:
                self.signals.add_timer_callback("mov_behavior", self._tick_behavior)
                self.signals.add_timer_callback("mov_movement", self._tick_movement)
            
            self.behavior_timer = QTimer()
            self.behavior_timer.timeout.connect(lambda: self.signals.timer_timeout("mov_behavior") if self.signals else self._tick_behavior())
            self.behavior_timer.start(int(self.config.get("behavior_interval_ms", 100)))
            
            # 創建移動計時器
            self.movement_timer = QTimer()
            self.movement_timer.timeout.connect(lambda: self.signals.timer_timeout("mov_movement") if self.signals else self._tick_movement())
            self.movement_timer.start(int(self.config.get("movement_interval_ms", 16)))
            
            info_log(f"[{self.module_id}] Qt 計時器已初始化")
            
        except Exception as e:
            error_log(f"[{self.module_id}] 初始化 Qt 計時器失敗: {e}")

    # ========= 事件/回調 =========

    def _register_handlers(self):
        self.register_event_handler(UIEventType.DRAG_START, self._on_drag_start)
        self.register_event_handler(UIEventType.DRAG_MOVE, self._on_drag_move)
        self.register_event_handler(UIEventType.DRAG_END, self._on_drag_end)

    def add_animation_callback(self, cb: Callable[[str, dict], None]):
        if cb not in self._animation_callbacks:
            self._animation_callbacks.append(cb)

    def add_position_callback(self, cb: Callable[[int, int], None]):
        if cb not in self._position_callbacks:
            self._position_callbacks.append(cb)

    # ========= Tick：行為 / 物理 =========

    def _tick_behavior(self):
        # 搗蛋模式：暫停行為機（但允許移動與動畫） 
        if self.mischief_active:
            return

        # 🌙 睡眠狀態下跳過行為更新
        if self.current_behavior_state == BehaviorState.SLEEPING:
            return
        
        # 🎤 ON_CALL 狀態下暫停所有移動（參考 system_cycle_behavior 作法）
        if self._on_call_active:
            # 完全停止移動
            self.velocity.x = 0.0
            self.velocity.y = 0.0
            self.target_velocity.x = 0.0
            self.target_velocity.y = 0.0
            
            # 清除移動目標
            self.movement_target = None
            self.target_reached = False
            
            # debug_log(3, f"[{self.module_id}] ON_CALL 期間保持靜止")
            return
        
        now = time.time()
        
        # 更新投擲處理器（檢查是否需要執行投擲後行為）
        self._throw_handler.update(now)

        if self._awaiting_anim:
            if now >= self._await_deadline:
                debug_log(2, f"[{self.module_id}] 動畫等待超時: {self._awaiting_anim}")
                # 超時照樣解除鎖定
                self._awaiting_anim = None
                self._await_deadline = 0.0
                self.movement_locked_until = 0.0
                self.resume_movement(self.WAIT_ANIM_REASON)
                # 仍執行 follow（當作降級方案）
                if self._await_follow:
                    try: self._await_follow()
                    except Exception as e: error_log(f"[{self.module_id}] 超時後續執行失敗: {e}")
                self._await_follow = None
            return

        # 鎖移動期間，仍讓行為跑（交由 TransitionBehavior 控制）
        # 拖曳期間需要允許行為tick執行，以觸發struggle動畫
        # SYSTEM_CYCLE 也需要執行 tick，以便 SystemCycleBehavior 處理層級動畫
        if self.movement_paused and not self.is_being_dragged:
            # 🔧 允許 SYSTEM_CYCLE 狀態繼續執行 tick（需要檢測層級變化並觸發動畫）
            if self.current_behavior_state != BehaviorState.SYSTEM_CYCLE:
                return
        
        # 滑鼠追蹤時也暫停行為更新（防止移動中播放閒置動畫）
        if hasattr(self, '_cursor_tracking_handler') and self._cursor_tracking_handler._is_turning_head:
            return
        
        # 投擲動畫序列進行中時完全暫停行為機（防止任何打斷）
        if hasattr(self, '_throw_handler') and self._throw_handler.is_in_throw_animation:
            debug_log(3, f"[{self.module_id}] 投擲動畫序列中，暫停行為機 tick")
            return
        
        # 檔案互動期間（hover 或 receive）暫停行為機
        if hasattr(self, '_file_drop_handler') and self._file_drop_handler.is_in_file_interaction:
            debug_log(3, f"[{self.module_id}] 檔案互動中，暫停行為機 tick")
            return
        
        # 檢查是否到達目標（提供給 MovementBehavior 判斷）
        self._update_target_reached()

        # 準備 Context
        ctx = BehaviorContext(
            position=self.position,
            velocity=self.velocity,
            target_velocity=self.target_velocity,
            screen_width=self.screen_width,
            screen_height=self.screen_height,
            SIZE=self.SIZE,
            GROUND_OFFSET=self.GROUND_OFFSET,
            v_left=self.v_left,
            v_top=self.v_top,
            v_right=self.v_right,
            v_bottom=self.v_bottom,
            movement_mode=self.movement_mode,
            facing_direction=self.facing_direction,
            movement_target=self.movement_target,
            target_reach_threshold=self.target_reach_threshold,
            target_reached=self.target_reached,
            ground_speed=self.GROUND_SPEED,
            float_min_speed=self.FLOAT_MIN_SPEED,
            float_max_speed=self.FLOAT_MAX_SPEED,
            physics=self.physics,
            sm=self.sm,
            trigger_anim=self._trigger_anim,
            set_target=self._set_target,
            get_cursor_pos=self._get_cursor_pos,
            now=now,
            anim_query=self.anim_query,
            transition_start_time=self.transition_start_time,
            transition_animation_finished=self._transition_animation_finished,
            movement_locked_until=self.movement_locked_until,
            previous_state=self.previous_behavior_state,
            current_layer=self._current_layer,
            layer_strategy=self._layer_strategy,
            tease_tracker=self._tease_tracker,
            trigger_tease_callback=self._trigger_tease_animation,
        )

        # **建立帶有正確 source 的 trigger_anim 包裝器（for on_tick）**
        def trigger_anim_for_tick(name: str, params: dict):
            source_map = {
                BehaviorState.IDLE: "idle_behavior",
                BehaviorState.NORMAL_MOVE: "movement_behavior",
                BehaviorState.SPECIAL_MOVE: "special_move_behavior",
                BehaviorState.TRANSITION: "transition_behavior",
                BehaviorState.SYSTEM_CYCLE: "system_cycle_behavior",
                BehaviorState.SLEEPING: "sleep_behavior",
            }
            source = source_map.get(self.current_behavior_state, "behavior")
            # 從 params 中提取 priority（如果有的話）
            priority = params.get("priority", None)
            self._trigger_anim(name, params, source=source, priority=priority)
        
        ctx.trigger_anim = trigger_anim_for_tick  # 替換為帶 source 的版本
        
        # on_tick 可能建議切換狀態
        try:
            next_state = self.current_behavior.on_tick(ctx)
        except Exception as e:
            error_log(f"[{self.module_id}] 行為 on_tick 例外: {e}")
            next_state = None

        # 同步回 MOV（Context 是引用型）
        self.movement_mode = ctx.movement_mode
        self.facing_direction = ctx.facing_direction
        self.transition_start_time = ctx.transition_start_time
        self._transition_animation_finished = ctx.transition_animation_finished
        self.movement_locked_until = ctx.movement_locked_until
        self.movement_target = ctx.movement_target
        self.target_reach_threshold = ctx.target_reach_threshold
        self.target_reached = ctx.target_reached

        if next_state is not None and next_state != self.current_behavior_state:
            debug_log(1, f"[{self.module_id}] 行為建議切換: {self.current_behavior_state.value} -> {next_state.value}")
            
            # 🌙 特殊處理：如果是從 TRANSITION 切換到 IDLE，且有待執行的睡眠
            if (self.current_behavior_state == BehaviorState.TRANSITION and 
                next_state == BehaviorState.IDLE and 
                hasattr(self, '_pending_sleep_transition') and 
                self._pending_sleep_transition):
                info_log(f"[{self.module_id}] Transition 完成，繼續執行睡眠轉換")
                self._pending_sleep_transition = False
                # 確保已經在地面
                if self.movement_mode != MovementMode.GROUND:
                    info_log(f"[{self.module_id}] 強制切換到 GROUND 模式")
                    self.movement_mode = MovementMode.GROUND
                    ground_y = self._ground_y()
                    self.position.y = ground_y
                self._execute_sleep_transition()
            else:
                self._switch_behavior(next_state)
            self._behavior_log_counter = 0  # 重置計數器
        else:
            # 降低日誌頻率：每50次才輸出一次狀態保持/無變化
            self._behavior_log_counter += 1
            if self._behavior_log_counter >= 50:
                if next_state is not None:
                    debug_log(3, f"[{self.module_id}] 行為保持: {self.current_behavior_state.value}")
                else:
                    debug_log(3, f"[{self.module_id}] 行為無變化: {self.current_behavior_state.value}")
                self._behavior_log_counter = 0

    def _tick_movement(self):
        # 搗蛋模式：僅執行目標定位與單次動畫觸發
        if self.mischief_active:
            # 直接移動到指定目標（若有）
            if self._mischief_pending_target:
                self.position.x = float(self._mischief_pending_target.x)
                self.position.y = float(self._mischief_pending_target.y)
                self._emit_position()
                self._mischief_pending_target = None
            # 觸發一次動畫（若有）
            if self._mischief_pending_anim:
                self._trigger_anim(
                    self._mischief_pending_anim,
                    {"loop": False},
                    source="mischief",
                    priority=AnimationPriority.USER_INTERACTION
                )
                self._mischief_pending_anim = None
                # 預留動畫完成時間（可覆寫）
                self._mischief_end_at = time.time() + self._mischief_anim_timeout
            # 時間到則結束 mischief 模式
            if self._mischief_end_at and time.time() >= self._mischief_end_at:
                self._end_mischief_action()
            return

        # 🌙 睡眠狀態下跳過移動更新（避免 FLOAT/GROUND 邊界檢測干擾睡眠動畫位置）
        # 睡眠動畫有特殊的 offsetY，如果啟用邊界檢測會被誤判為浮空而強制下壓
        if self.current_behavior_state == BehaviorState.SLEEPING:
            return
        
        now = time.time()
        
        # 更新滑鼠追蹤處理器（即使移動暫停也要更新）
        self._cursor_tracking_handler.update()
        
        # 拖曳時完全不處理物理，避免重力影響
        if self.is_being_dragged:
            return
            
        if self.movement_paused:
            return
            
        # 轉場期間仍然允許移動，但其他動畫等待期間不允許
        if now < self.movement_locked_until and self.current_behavior_state != BehaviorState.TRANSITION:
            return

        prev_x, prev_y = self.position.x, self.position.y
        gy = self._ground_y()

        # 模式別物理
        if self.movement_mode == MovementMode.GROUND:
            # 貼地模式
            self.position.y = gy
            self.velocity = self.physics.step_ground(self.velocity)
        elif self.movement_mode == MovementMode.FLOAT:
            # 漂浮模式
            self.velocity = self.physics.step_float(self.velocity)
            
            # **檢測是否接觸地面，自動切換到地面模式**
            # 但在入場動畫播放期間禁止自動轉換，避免瞬移
            # 🌙 睡眠轉換期間也禁止自動切換到 IDLE（避免打斷 f_to_g → g_to_l 流程）
            is_pending_sleep = hasattr(self, '_pending_sleep_transition') and self._pending_sleep_transition
            
            if self.position.y >= gy - 5 and not self._is_entering and not is_pending_sleep:
                debug_log(1, f"[{self.module_id}] 漂浮模式接觸地面，自動切換到地面模式")
                self.position.y = gy
                self.movement_mode = MovementMode.GROUND
                self.velocity.x = 0.0
                self.velocity.y = 0.0
                self.target_velocity.x = 0.0
                self.target_velocity.y = 0.0
                # 先切換到 IDLE 行為狀態，再播放落地動畫
                # 這樣可以避免 idle 動畫帶著 TRANSITION 優先度，導致後續走路動畫被阻擋
                self._switch_behavior(BehaviorState.IDLE)
                idle_anim = self.anim_query.get_idle_animation_for_mode(is_ground=True)
                self._trigger_anim(idle_anim, {"loop": True}, source="throw_handler", priority=AnimationPriority.IDLE_ANIMATION)
            elif self.position.y >= gy - 5 and is_pending_sleep:
                # 🌙 睡眠轉換：只修正位置，不改變行為（讓 TRANSITION 繼續跑）
                debug_log(2, f"[{self.module_id}] 睡眠轉換中接觸地面，修正位置但保持 TRANSITION 狀態")
                self.position.y = gy
                self.movement_mode = MovementMode.GROUND
                self.velocity.x = 0.0
                self.velocity.y = 0.0
                self.target_velocity.x = 0.0
                self.target_velocity.y = 0.0
        elif self.movement_mode == MovementMode.DRAGGING:
            # 拖曳模式：不應該到達這裡（上面已經 return）
            # 但保留以防萬一
            return
        elif self.movement_mode == MovementMode.THROWN:
            # 投擲模式的物理模擬（參考 desktop_pet.py ThrowState）
            grounded = abs(self.position.y - gy) < 5
            self.velocity = self.physics.step_thrown(self.velocity, grounded)
            
            # 地面碰撞檢測和反彈
            if self.position.y >= gy:
                self.position.y = gy
                
                # 使用 PhysicsEngine 的反彈方法
                if abs(self.velocity.y) > 2:
                    # 有足夠的垂直速度 -> 反彈
                    self.velocity = self.physics.apply_bounce(self.velocity)
                    debug_log(1, f"[{self.module_id}] 投擲反彈: vy={self.velocity.y:.1f}")
                else:
                    # 速度太小，停止投擲，轉為地面模式
                    self.velocity.x = 0.0
                    self.velocity.y = 0.0
                    self.target_velocity.x = 0.0
                    self.target_velocity.y = 0.0
                    self.movement_mode = MovementMode.GROUND
                    
                    # 通知 ThrowHandler 處理落地動畫 (swoop_*_end)
                    # 如果有落地動畫，會阻止行為切換直到動畫完成
                    self._throw_handler.handle_throw_landing()
                    
                    # 如果沒有投擲動畫序列，直接切換到 IDLE
                    if not self._throw_handler.is_in_throw_animation:
                        # 先切換行為狀態，再播放 idle 動畫
                        self._switch_behavior(BehaviorState.IDLE)
                        idle_anim = self.anim_query.get_idle_animation_for_mode(is_ground=True)
                        self._trigger_anim(idle_anim, {"loop": True}, source="throw_handler", priority=AnimationPriority.IDLE_ANIMATION)
                    
                    debug_log(1, f"[{self.module_id}] 投擲落地")

        # 速度趨近 target_velocity（拖曳和投擲時不處理）
        # 投擲模式下速度完全由物理引擎控制，不應被 target_velocity 拉回 0
        if not self.is_being_dragged and self.movement_mode != MovementMode.THROWN:
            self.velocity.x += (self.target_velocity.x - self.velocity.x) * self._approach_k
            self.velocity.y += (self.target_velocity.y - self.velocity.y) * self._approach_k
        
        # 應用平滑化（減少閃現）
        if self._smoothing_enabled:
            self._apply_velocity_smoothing()
        else:
            self._smooth_velocity.x = self.velocity.x
            self._smooth_velocity.y = self.velocity.y

        # 位置整合 + 邊界處理（拖曳時不處理）
        if not self.is_being_dragged:
            self.position.x += self._smooth_velocity.x
            self.position.y += self._smooth_velocity.y
            self._check_boundaries()

        moved = (abs(self.position.x - prev_x) + abs(self.position.y - prev_y)) > 0.05
        if moved:
            self.last_movement_time = now
        self._emit_position()

        # 停滯保護（可視需要）- 但排除特殊狀態
        if (now - self.last_movement_time > self.max_idle_time and 
            self.current_behavior_state != BehaviorState.IDLE and 
            self.current_behavior_state != BehaviorState.TRANSITION and
            self.current_behavior_state != BehaviorState.SYSTEM_CYCLE):  # 系統循環期間應保持狀態
            debug_log(2, f"[{self.module_id}] 檢測到移動停滯，強制切換狀態")
            self._switch_behavior(BehaviorState.IDLE)

    # ========= 行為切換 =========

    def _enter_behavior(self, state: BehaviorState):
        """呼叫 on_enter 並更新 current_behavior_state"""
        
        # 如果正在投擲動畫序列中，不要觸發 idle 動畫（避免 zoom 被重置）
        if state == BehaviorState.IDLE and hasattr(self, '_throw_handler'):
            if self._throw_handler.is_in_throw_animation:
                debug_log(1, f"[{self.module_id}] ⏸️ 投擲動畫序列進行中，延後進入 IDLE")
                return
        
        self.previous_behavior_state = self.current_behavior_state  # 記錄前一個狀態
        self.current_behavior_state = state
        self.current_behavior = BehaviorFactory.create(state)
        
        # **建立帶有正確 source 的 trigger_anim 包裝器**
        def trigger_anim_with_source(name: str, params: dict):
            # 根據當前 behavior 推斷 source
            source_map = {
                BehaviorState.IDLE: "idle_behavior",
                BehaviorState.NORMAL_MOVE: "movement_behavior",
                BehaviorState.SPECIAL_MOVE: "special_move_behavior",
                BehaviorState.TRANSITION: "transition_behavior",
                BehaviorState.SYSTEM_CYCLE: "system_cycle_behavior",
                BehaviorState.SLEEPING: "sleep_behavior",
            }
            source = source_map.get(state, "behavior")
            self._trigger_anim(name, params, source=source)
        
        # **重置移動計時器，避免進入移動行為時立即觸發停滯檢測**
        if state in (BehaviorState.NORMAL_MOVE, BehaviorState.SPECIAL_MOVE):
            self.last_movement_time = time.time()
            debug_log(3, f"[{self.module_id}] 進入移動行為，重置移動計時器")
        
        # **檢查投擲後調皮行為**
        if self._post_throw_tease_pending and state == BehaviorState.NORMAL_MOVE:
            debug_log(1, f"[{self.module_id}] 投擲後調皮：播放 tease 動畫")
            tease_anim = self.anim_query.get_tease_animation(variant=1)
            idle_anim = self.anim_query.get_idle_animation_for_mode(is_ground=False)
            self._trigger_anim(tease_anim, {"loop": False, "next_anim": idle_anim, "next_params": {"loop": True}}, source="tease_system")
            self._post_throw_tease_pending = False  # 清除標記

        # 建 ctx 給 on_enter
        now = time.time()
        
        # **建立帶有正確 source 的 trigger_anim 包裝器（for on_enter）**
        def trigger_anim_for_enter(name: str, params: dict):
            source_map = {
                BehaviorState.IDLE: "idle_behavior",
                BehaviorState.NORMAL_MOVE: "movement_behavior",
                BehaviorState.SPECIAL_MOVE: "special_move_behavior",
                BehaviorState.TRANSITION: "transition_behavior",
                BehaviorState.SYSTEM_CYCLE: "system_cycle_behavior",
            }
            source = source_map.get(state, "behavior")
            # 從 params 中提取 priority（如果有的話）
            priority = params.get("priority", None)
            self._trigger_anim(name, params, source=source, priority=priority)
        
        ctx = BehaviorContext(
            position=self.position,
            velocity=self.velocity,
            target_velocity=self.target_velocity,
            screen_width=self.screen_width,
            screen_height=self.screen_height,
            SIZE=self.SIZE,
            GROUND_OFFSET=self.GROUND_OFFSET,
            v_left=self.v_left,
            v_top=self.v_top,
            v_right=self.v_right,
            v_bottom=self.v_bottom,
            movement_mode=self.movement_mode,
            facing_direction=self.facing_direction,
            movement_target=self.movement_target,
            target_reach_threshold=self.target_reach_threshold,
            target_reached=self.target_reached,
            ground_speed=self.GROUND_SPEED,
            float_min_speed=self.FLOAT_MIN_SPEED,
            float_max_speed=self.FLOAT_MAX_SPEED,
            physics=self.physics,
            sm=self.sm,
            trigger_anim=trigger_anim_for_enter,
            set_target=self._set_target,
            get_cursor_pos=self._get_cursor_pos,
            now=now,
            anim_query=self.anim_query,
            transition_start_time=self.transition_start_time,
            movement_locked_until=self.movement_locked_until,
            previous_state=self.previous_behavior_state,
            current_layer=self._current_layer,
            layer_strategy=self._layer_strategy,
            tease_tracker=self._tease_tracker,
            trigger_tease_callback=self._trigger_tease_animation,
        )

        try:
            self.current_behavior.on_enter(ctx)
        except Exception as e:
            error_log(f"[{self.module_id}] 行為 on_enter 例外: {e}")

        # 同步回 MOV
        self.movement_mode = ctx.movement_mode
        self.facing_direction = ctx.facing_direction
        self.transition_start_time = ctx.transition_start_time
        self.movement_locked_until = ctx.movement_locked_until
        self.movement_target = ctx.movement_target
        self.target_reach_threshold = ctx.target_reach_threshold
        self.target_reached = ctx.target_reached

        debug_log(1, f"[{self.module_id}] 進入行為: {state.value}（模式: {self.movement_mode.value}）")

    def _switch_behavior(self, next_state: BehaviorState):
        if self.mischief_active:
            return
        old = self.current_behavior_state
        debug_log(1, f"[{self.module_id}] 行為狀態轉換: {old.value} -> {next_state.value}（{self.movement_mode.value}）")
        # 若需要 on_exit，可在 BaseBehavior 加入，這裡預留呼叫點
        try:
            if hasattr(self.current_behavior, "on_exit"):
                self.current_behavior.on_exit(  # type: ignore
                    BehaviorContext(
                        position=self.position,
                        velocity=self.velocity,
                        target_velocity=self.target_velocity,
                        screen_width=self.screen_width,
                        screen_height=self.screen_height,
                        SIZE=self.SIZE,
                        GROUND_OFFSET=self.GROUND_OFFSET,
                        v_left=self.v_left,
                        v_top=self.v_right,
                        v_right=self.v_right,
                        v_bottom=self.v_bottom,
                        movement_mode=self.movement_mode,
                        facing_direction=self.facing_direction,
                        movement_target=self.movement_target,
                        target_reach_threshold=self.target_reach_threshold,
                        target_reached=self.target_reached,
                        ground_speed=self.GROUND_SPEED,
                        float_min_speed=self.FLOAT_MIN_SPEED,
                        float_max_speed=self.FLOAT_MAX_SPEED,
                        physics=self.physics,
                        sm=self.sm,
                        trigger_anim=self._trigger_anim,
                        set_target=self._set_target,
                        get_cursor_pos=self._get_cursor_pos,
                        now=time.time(),
                        transition_start_time=self.transition_start_time,
                        transition_animation_finished=self._transition_animation_finished,
                        movement_locked_until=self.movement_locked_until,
                        previous_state=self.previous_behavior_state,
                        current_layer=self._current_layer,
                        layer_strategy=self._layer_strategy,
                        tease_tracker=self._tease_tracker,
                        trigger_tease_callback=self._trigger_tease_animation,
                    )
                )
        except Exception as e:
            error_log(f"[{self.module_id}] 行為 on_exit 例外: {e}")

        self._enter_behavior(next_state)

    # ========= 工具/邊界/目標 =========

    def _ground_y(self) -> float:
        """計算地面 Y 座標
        
        🌙 睡眠狀態：不補償 offset（睡眠動畫的 offset 是視覺調整，不影響物理位置）
        🚶 其他狀態：補償 offset_y（讓角色腳底始終對齊地面線）
        """
        base_ground = self.v_bottom - self.SIZE + self.GROUND_OFFSET
        
        # 🌙 睡眠狀態下不補償動畫偏移（避免位置跳動）
        if self.current_behavior_state == BehaviorState.SLEEPING:
            return base_ground
        
        # 🚶 其他狀態補償動畫偏移（讓角色腳底對齊地面）
        return base_ground - self._current_animation_offset_y

    def _play_entry_animation(self):
        """播放入場動畫（從 ANI 模組獲取動畫名稱）"""
        try:
            self._is_entering = True
            
            # 🔧 強制清除靜態幀模式（入場是最高級動畫，不能被追蹤模式阻擋）
            if self.ani_module and hasattr(self.ani_module, 'manager'):
                if getattr(self.ani_module.manager, 'static_frame_mode', False):
                    self.ani_module.manager.exit_static_frame_mode()
                    debug_log(2, f"[{self.module_id}] 入場時強制退出靜態幀模式")
            
            # 🔧 清除低優先度的優先度鎖定（特別是滑鼠追蹤）
            if hasattr(self, '_animation_priority') and self._animation_priority:
                current_priority = self._animation_priority.get_current_priority()
                if current_priority and current_priority <= AnimationPriority.CURSOR_TRACKING:
                    self._animation_priority.reset()
                    debug_log(2, f"[{self.module_id}] 入場時清除低優先度鎖定")
            
            # 只在第一次顯示時設置起始位置，後續顯示恢復到隱藏前的位置
            if self._last_hide_position is None:
                # 第一次顯示：設置入場起始位置
                start_pos = self._entry_behavior_config.get("start_position", "top_center")
                self._set_entry_start_position(start_pos)
            else:
                # 再次顯示：恢復到隱藏前的位置
                self.position.x, self.position.y = self._last_hide_position
                self._emit_position()
                debug_log(1, f"[{self.module_id}] 恢復到隱藏前位置: ({self.position.x:.0f}, {self.position.y:.0f})")
            
            # 從 ANI 模組的 state_animations.yaml 獲取入場動畫名稱
            anim_name = self._get_entry_animation_name()
            if not anim_name:
                # 如果 ANI 未配置，直接進入
                debug_log(1, f"[{self.module_id}] 未找到入場動畫配置，直接進入")
                self._on_entry_complete()
                return
            
            # 獲取動畫持續時間（從 ANI config 讀取）
            duration = self._get_animation_duration(anim_name)
            # 增加額外緩衝時間以確保動畫完整播放（增加到 1.0 秒）
            timeout = duration + 1.0
            
            debug_log(1, f"[{self.module_id}] 入場動畫 {anim_name}: 持續時間={duration:.2f}s, 超時={timeout:.2f}s")
            
            # 暫停移動直到動畫完成
            self.pause_movement("entry_animation")
            
            # 🔧 使用 ENTRY_EXIT 優先度（最高級，不能被任何其他動畫打斷）
            self._trigger_anim(
                anim_name, 
                {"loop": False, "allow_interrupt": False},  # 不允許被打斷
                source="entry_animation",
                priority=AnimationPriority.ENTRY_EXIT
            )
            self._await_animation(anim_name, timeout, self._on_entry_complete)
            
            info_log(f"[{self.module_id}] 播放入場動畫: {anim_name} (持續 {duration:.2f}秒)")
        except Exception as e:
            error_log(f"[{self.module_id}] 入場動畫失敗: {e}")
            self._on_entry_complete()
    
    def _get_entry_animation_name(self) -> Optional[str]:
        """從 ANI 模組獲取入場動畫名稱（使用動畫查詢輔助器）"""
        return self.anim_query.get_entry_animation()
    
    def _get_animation_duration(self, anim_name: str) -> float:
        """從 ANI 模組獲取動畫持續時間（使用動畫查詢輔助器）"""
        return self.anim_query.get_animation_duration(anim_name)
    
    def _set_entry_start_position(self, start_pos: str):
        """設置入場起始位置"""
        screen_center_x = (self.v_left + self.v_right) / 2
        screen_center_y = (self.v_top + self.v_bottom) / 2
        
        if start_pos == "top_center":
            self.position.x = screen_center_x
            self.position.y = self.v_top - self.SIZE  # 螢幕上方外
        elif start_pos == "top_left":
            self.position.x = self.v_left
            self.position.y = self.v_top - self.SIZE  # 螢幕左上角外
        elif start_pos == "top_right":
            self.position.x = self.v_right - self.SIZE
            self.position.y = self.v_top - self.SIZE  # 螢幕右上角外
        elif start_pos == "bottom_center":
            self.position.x = screen_center_x
            self.position.y = self.v_bottom  # 螢幕下方外
        elif start_pos == "bottom_left":
            self.position.x = self.v_left
            self.position.y = self.v_bottom  # 螢幕左下角外
        elif start_pos == "bottom_right":
            self.position.x = self.v_right - self.SIZE
            self.position.y = self.v_bottom  # 螢幕右下角外
        elif start_pos == "left":
            self.position.x = self.v_left - self.SIZE
            self.position.y = screen_center_y
        elif start_pos == "right":
            self.position.x = self.v_right
            self.position.y = screen_center_y
        else:  # center
            self.position.x = screen_center_x
            self.position.y = screen_center_y
        
        self._emit_position()
        debug_log(2, f"[{self.module_id}] 入場起始位置: {start_pos} → ({self.position.x:.0f}, {self.position.y:.0f})")
    
    def _on_entry_complete(self):
        """入場動畫完成回調"""
        # 注意：不在這裡清除 _is_entering 和恢復移動，而是在 _switch_to_idle 中
        # 這樣可以確保在延遲期間仍然暫停移動和阻止地面轉換
        self._entry_complete = True
        # 不要在這裡 resume_movement，等待延遲完成後再恢復
        
        # 設置入場後的模式
        entry_mode = self._entry_behavior_config.get("mode", "FLOAT")
        
        # 發送位置更新（確保 UI 同步）
        self._emit_position()
        
        debug_log(1, f"[{self.module_id}] 入場完成，位置: ({self.position.x:.0f}, {self.position.y:.0f})，模式: {entry_mode}，保持暫停直到延遲完成")
        
        # 延遲 0.5 秒後再切換到閒置動畫，讓最後一幀停留
        def _switch_to_idle():
            # 現在才清除入場標誌和恢復移動
            self._is_entering = False
            self.resume_movement("entry_animation")
            
            # 入場動畫結束後始終保持浮空模式，避免瞬移到地面
            # 系統會在後續 update 中自動判斷是否需要切換到地面模式
            self.movement_mode = MovementMode.FLOAT
            
            # 保留入場動畫結束時的位置，不強制修改
            # 使用動畫查詢輔助器獲取浮空閒置動畫
            idle_anim = self.anim_query.get_idle_animation_for_mode(is_ground=False)
            self._trigger_anim(idle_anim, {"loop": True}, source="entry_animation")
            
            # 進入第一個行為
            self._enter_behavior(self.current_behavior_state)
        
        # 通知優先度管理器入場動畫已完成，清理優先度鎖定
        anim_name = self._get_entry_animation_name()
        if anim_name and hasattr(self, '_animation_priority') and self._animation_priority:
            self._animation_priority.on_animation_finished(anim_name)
            debug_log(2, f"[{self.module_id}] 入場動畫 {anim_name} 優先度已清理")
        
        # 使用 QTimer.singleShot 延遲執行
        from PyQt5.QtCore import QTimer
        QTimer.singleShot(500, _switch_to_idle)  # 500ms = 0.5秒
    
    def _play_leave_animation(self, on_complete_callback=None):
        """播放離場動畫
        
        Args:
            on_complete_callback: 動畫完成後的回調函數
        """
        try:
            self._is_leaving = True
            
            # 🔧 強制清除靜態幀模式（離場是最高級動畫，不能被追蹤模式阻擋）
            if self.ani_module and hasattr(self.ani_module, 'manager'):
                if getattr(self.ani_module.manager, 'static_frame_mode', False):
                    self.ani_module.manager.exit_static_frame_mode()
                    debug_log(2, f"[{self.module_id}] 離場時強制退出靜態幀模式")
            
            # 🔧 清除低優先度的優先度鎖定（特別是滑鼠追蹤）
            if hasattr(self, '_animation_priority') and self._animation_priority:
                current_priority = self._animation_priority.get_current_priority()
                if current_priority and current_priority <= AnimationPriority.CURSOR_TRACKING:
                    self._animation_priority.reset()
                    debug_log(2, f"[{self.module_id}] 離場時清除低優先度鎖定")
            
            # 從 ANI 模組獲取離場動畫名稱
            anim_name = self._get_leave_animation_name()
            if not anim_name:
                # 如果 ANI 未配置，直接完成
                debug_log(1, f"[{self.module_id}] 未找到離場動畫配置，直接離開")
                self._on_leave_complete(on_complete_callback)
                return
            
            # 獲取動畫持續時間（從 ANI 獲取實際幀數和幀持續時間）
            duration = self._get_animation_duration(anim_name)
            # 增加額外緩衝時間以確保動畫完整播放（增加到 1.0 秒）
            timeout = duration + 1.0
            
            debug_log(1, f"[{self.module_id}] 離場動畫 {anim_name}: 持續時間={duration:.2f}s, 超時={timeout:.2f}s")
            
            # 暫停移動直到動畫完成
            self.pause_movement("leave_animation")
            
            # 🔧 使用 ENTRY_EXIT 優先度（最高級，不能被任何其他動畫打斷）
            # 🎬 使用 immediate_interrupt=True 強制突破優先度鎖定
            self._trigger_anim(
                anim_name, 
                {"loop": False, "allow_interrupt": False, "immediate_interrupt": True},  # 強制覆蓋
                source="exit_animation",
                priority=AnimationPriority.ENTRY_EXIT
            )
            self._await_animation(
                anim_name, 
                timeout, 
                lambda: self._on_leave_complete(on_complete_callback)
            )
            
            info_log(f"[{self.module_id}] 播放離場動畫: {anim_name} (持續 {duration:.2f}秒)")
        except Exception as e:
            error_log(f"[{self.module_id}] 離場動畫失敗: {e}")
            self._on_leave_complete(on_complete_callback)
    
    def _get_leave_animation_name(self) -> Optional[str]:
        """從 ANI 模組獲取離場動畫名稱（使用動畫查詢輔助器）"""
        return self.anim_query.get_exit_animation()
    
    def _on_leave_complete(self, callback=None):
        """離場動畫完成回調"""
        self._is_leaving = False
        self.resume_movement("leave_animation")
        
        # 記住當前位置，以便再次顯示時恢復
        self._last_hide_position = (self.position.x, self.position.y)
        debug_log(1, f"[{self.module_id}] 離場動畫完成，記住位置: ({self.position.x:.0f}, {self.position.y:.0f})")
        info_log(f"[{self.module_id}] 離場動畫完成")
        
        # 通知優先度管理器離場動畫已完成，清理優先度鎖定
        anim_name = self._get_leave_animation_name()
        if anim_name and hasattr(self, '_animation_priority') and self._animation_priority:
            self._animation_priority.on_animation_finished(anim_name)
            debug_log(2, f"[{self.module_id}] 離場動畫 {anim_name} 優先度已清理")
        
        # 停止 ANI 模組動畫，避免隱藏期間繼續播放
        if self.ani_module:
            self.ani_module.stop()
            debug_log(1, f"[{self.module_id}] 已停止 ANI 動畫（隱藏後）")
        
        # 執行回調
        if callback:
            callback()
    
    def _apply_velocity_smoothing(self):
        """應用速度平滑化以減少閃現"""
        # 如果正在暫停，緩慢減速
        if self.movement_paused:
            # 保存暫停前的速度
            if self._pause_velocity_buffer.x == 0 and self._pause_velocity_buffer.y == 0:
                self._pause_velocity_buffer.x = self._smooth_velocity.x
                self._pause_velocity_buffer.y = self._smooth_velocity.y
            
            # 緩慢減速到 0
            self._smooth_velocity.x *= self._pause_damping
            self._smooth_velocity.y *= self._pause_damping
            
            # 接近 0 時完全停止
            if abs(self._smooth_velocity.x) < 0.01:
                self._smooth_velocity.x = 0
            if abs(self._smooth_velocity.y) < 0.01:
                self._smooth_velocity.y = 0
        else:
            # 恢復移動時平滑加速
            if self._pause_velocity_buffer.x != 0 or self._pause_velocity_buffer.y != 0:
                # 從緩衝速度逐漸恢復
                target_x = self.velocity.x
                target_y = self.velocity.y
                
                self._smooth_velocity.x += (target_x - self._smooth_velocity.x) * self._resume_acceleration
                self._smooth_velocity.y += (target_y - self._smooth_velocity.y) * self._resume_acceleration
                
                # 接近目標速度時清除緩衝
                if abs(self._smooth_velocity.x - target_x) < 0.1 and abs(self._smooth_velocity.y - target_y) < 0.1:
                    self._pause_velocity_buffer.x = 0
                    self._pause_velocity_buffer.y = 0
            else:
                # 正常平滑
                self._smooth_velocity.x += (self.velocity.x - self._smooth_velocity.x) * self._velocity_lerp_factor
                self._smooth_velocity.y += (self.velocity.y - self._smooth_velocity.y) * self._velocity_lerp_factor

    def _initialize_position(self):
        margin = self.screen_padding if hasattr(self, "screen_padding") else 50
        min_x = self.v_left + margin
        max_x = self.v_right - self.SIZE - margin
        min_y = self.v_top + margin
        max_y = self.v_bottom - self.SIZE - margin
        
        # Wrap 模式下不限制初始位置，允許在螢幕外的位置
        if self.boundary_mode != "wrap":
            self.position.x = min(max(self.position.x, min_x), max_x)
            self.position.y = min(max(self.position.y, min_y), max_y)
        
        self._emit_position()


    def _get_cursor_pos(self) -> tuple[float, float]:
        """獲取當前滑鼠位置（螢幕座標）- 使用 QCursor"""
        try:
            from PyQt5.QtGui import QCursor
            pos = QCursor.pos()
            return (float(pos.x()), float(pos.y()))
        except Exception:
            return (0.0, 0.0)  # fallback

    def _set_target(self, x: float, y: float):
        margin = self.screen_padding
        # 落地時 y 鎖在地面，但拖曳模式除外
        if self.movement_mode == MovementMode.GROUND and not self.is_being_dragged:
            y = self._ground_y()
        max_x = self.v_right  - self.SIZE
        max_y = self.v_bottom - self.SIZE
        
        # Wrap 模式下不限制目標位置，允許設置螢幕外的目標
        if self.boundary_mode == "wrap":
            cx = float(x)
            cy = float(y)
        else:
            cx = max(self.v_left + margin,  min(max_x - margin, float(x)))
            cy = max(self.v_top  + margin,  min(max_y - margin, float(y)))
        if self.movement_target is None:
            from .core.position import Position  # 避免循環匯入
            self.movement_target = Position(cx, cy)
        else:
            self.movement_target.x, self.movement_target.y = cx, cy
        self.target_reached = False
        debug_log(2, f"[{self.module_id}] 設置新目標: ({cx:.1f},{cy:.1f})")

    def _update_target_reached(self):
        if not self.movement_target:
            self.target_reached = True
            return
        d = math.hypot(self.position.x - self.movement_target.x,
                       self.position.y - self.movement_target.y)
        self.target_reached = d <= self.target_reach_threshold

    def _check_boundaries(self):
        """
        檢查並處理螢幕邊界
        
        支持兩種模式：
        - barrier: 碰到邊界停止（預設）
        - wrap: 從右邊出去左邊進來（循環模式）
        
        注意：拖曳時不檢查邊界，允許使用者自由拖曳
        """
        # 🔧 拖曳時跳過邊界檢查，允許使用者自由拖曳到任何位置
        if self.is_being_dragged or self.movement_mode == MovementMode.DRAGGING:
            return
        
        left  = self.v_left
        right = self.v_right  - self.SIZE
        boundary_hit = False
        wrapped = False  # 標記是否發生了 wrap
        
        # 循環模式（wrap）
        if self.boundary_mode == "wrap":
            # 左邊界：完全離開左邊後，從右邊進來
            if self.position.x < left:
                self.position.x = right  # 從右邊重新出現
                debug_log(2, f"[{self.module_id}] 邊界循環：左邊 -> 右邊 (x={self.position.x:.1f})")
                wrapped = True
            
            # 右邊界：完全離開右邊後，從左邊進來
            elif self.position.x > right:
                self.position.x = left  # 從左邊重新出現
                debug_log(2, f"[{self.module_id}] 邊界循環：右邊 -> 左邊 (x={self.position.x:.1f})")
                wrapped = True
            
            # Wrap 模式下不改變速度，讓角色繼續原方向移動
            if wrapped:
                # 更新移動目標（如果有的話）
                if self.movement_target:
                    # 根據當前位置和方向調整目標
                    if self.facing_direction > 0:  # 向右
                        # 確保目標在右側
                        if self.movement_target.x < self.position.x:
                            self.movement_target.x += (right - left)
                    else:  # 向左
                        # 確保目標在左側
                        if self.movement_target.x > self.position.x:
                            self.movement_target.x -= (right - left)
        
        # 屏障模式（barrier，預設）
        else:
            if self.position.x <= left:
                self.position.x = left
                boundary_hit = True
                if not getattr(self, "bounce_off_edges", False):
                    if self.movement_target and self.movement_target.x < left + 20:
                        self.movement_target.x = left + (self.screen_padding + 30)
                else:
                    self.velocity.x = abs(self.velocity.x); self.target_velocity.x = abs(self.target_velocity.x)
                self.facing_direction = 1

            elif self.position.x >= right:
                self.position.x = right
                boundary_hit = True
                if not getattr(self, "bounce_off_edges", False):
                    if self.movement_target and self.movement_target.x > right - 20:
                        self.movement_target.x = right - (self.screen_padding + 30)
                else:
                    self.velocity.x = -abs(self.velocity.x); self.target_velocity.x = -abs(self.target_velocity.x)
                self.facing_direction = -1

        # Barrier 模式下到達邊界時的處理
        if boundary_hit and self.current_behavior_state == BehaviorState.NORMAL_MOVE:
            self.velocity = Velocity(0.0, 0.0)
            self.target_velocity = Velocity(0.0, 0.0)
            self.target_reached = True

            if self.movement_mode == MovementMode.GROUND:
                direction = "right" if self.facing_direction > 0 else "left"
                turn_anim = self.anim_query.get_turn_animation(direction, is_ground=True)
                idle_anim = self.anim_query.get_idle_animation_for_mode(is_ground=True)
                # 轉向是非 loop：等待完成 → 自動接 idle（loop）
                if turn_anim:
                    self._trigger_anim(turn_anim, {
                        "loop": False,
                        "await_finish": True,
                        # 不要硬寫 1.0，交給 _trigger_anim 動態算時長 + 裕度
                        "next_anim": idle_anim,
                        "next_params": {"loop": True, "allow_interrupt": True}
                    }, source="movement_behavior")
                else:
                    # 如果沒有轉向動畫，直接播閒置動畫
                    self._trigger_anim(idle_anim, {"loop": True, "allow_interrupt": True}, source="movement_behavior")
            else:
                # 浮空時沒有轉向動畫，直接播閒置動畫
                idle_anim = self.anim_query.get_idle_animation_for_mode(is_ground=False)
                self._trigger_anim(idle_anim, {"loop": True, "allow_interrupt": True}, source="movement_behavior")

        # 漂浮模式的高度限制：只限制上方，不限制下方（讓它能落地）
        if self.movement_mode == MovementMode.FLOAT:
            top = self.v_top
            if self.position.y <= top:
                self.position.y = top


    def _detect_virtual_desktop(self):
        """多螢幕：記錄虛擬桌面四邊，或僅鎖定主螢幕"""
        try:
            from PyQt5.QtWidgets import QDesktopWidget
            d = QDesktopWidget()
            if d and d.screenCount() > 0:
                bnd = self.config.get("boundaries", {})
                stay_on_primary = bool(bnd.get("stay_on_primary", False))
                primary_index = int(bnd.get("primary_index", 0))
                if stay_on_primary:
                    g = d.screenGeometry(primary_index)
                    self.v_left, self.v_top = g.x(), g.y()
                    self.v_right = g.x() + g.width()
                    self.v_bottom = g.y() + g.height()
                else:
                    self.v_left  = min(d.screenGeometry(i).x() for i in range(d.screenCount()))
                    self.v_top   = min(d.screenGeometry(i).y() for i in range(d.screenCount()))
                    self.v_right = max(d.screenGeometry(i).x() + d.screenGeometry(i).width()  for i in range(d.screenCount()))
                    self.v_bottom= max(d.screenGeometry(i).y() + d.screenGeometry(i).height() for i in range(d.screenCount()))
                self.screen_width  = self.v_right - self.v_left
                self.screen_height = self.v_bottom - self.v_top
                debug_log(1, f"[{self.module_id}] 虛擬桌面: {self.screen_width}x{self.screen_height} origin=({self.v_left},{self.v_top})")
        except Exception:
            pass


    # ========= 動畫觸發 =========

    def _trigger_anim(self, name: str, params: Optional[dict] = None, source: str = "unknown", priority: Optional[AnimationPriority] = None):
        """
        觸發動畫播放（改進版，帶優先度管理）
        
        閃現問題來源：
        1. 使用者干涉時動作模組仍在演算
        2. 動畫切換時沒有同步狀態
        
        解決方案：
        - 添加動畫鎖機制
        - 檢查是否正在被干涉（拖動、拋擲）
        - 提供 immediate_interrupt 參數強制中斷
        - 使用優先度系統避免動畫衝突
        """
        params = params or {}
        loop = params.get("loop", None)
        await_finish = bool(params.get("await_finish", False) or (loop is False))
        max_wait = float(params.get("max_wait", self._default_anim_timeout))
        next_anim = params.get("next_anim")  # 可選：完成後要接的動畫名
        next_params = params.get("next_params", {})  # 其參數
        force_restart = params.get("force_restart", False)  # 強制重新開始
        immediate_interrupt = params.get("immediate_interrupt", False)  # 立即中斷現有動畫
        
        # === 優先度檢查 ===
        # 如果沒有指定優先度，根據當前狀態推斷
        if priority is None:
            priority = self._infer_animation_priority(params)
        
        # 檢查優先度是否足夠
        lock_duration = 0.0
        if await_finish and not loop:  # 非循環動畫完成前鎖定優先度
            lock_duration = max_wait
        
        if not self._animation_priority.request_animation(
            name=name,
            priority=priority,
            source=source,
            params=params,
            lock_duration=lock_duration,
        ):
            debug_log(3, f"[{self.module_id}] 動畫請求被優先度系統拒絕: {name} (來源: {source})")
            return
        
        # 檢查是否正在被干涉（拖動、拋擲中不應該切換動畫）
        # 例外動畫：struggle、struggle_l、transition 動畫（g_to_f, f_to_g, l_to_g, g_to_l）、idle 動畫
        # SYSTEM_CYCLE 狀態下的層級動畫不檢查 dragging（已在 INTERACTION_STARTED 時清除）
        allowed_during_special = (
            name in ("struggle_f", "struggle_l") or 
            name in ("g_to_f", "f_to_g", "l_to_g", "g_to_l") or 
            "idle" in name.lower()
        )
        
        if not immediate_interrupt and not allowed_during_special:
            if self.current_behavior_state != BehaviorState.SYSTEM_CYCLE:
                if self.is_being_dragged:
                    # 允許 struggle 動畫在拖動時播放
                    if name != "struggle" and "struggle" not in name:
                        debug_log(3, f"[{self.module_id}] 跳過動畫觸發（正在被拖動）: {name}")
                        return
                if self.movement_mode == MovementMode.THROWN:
                    # 允許投擲相關動畫 (swoop_*, struggle) 在投擲時播放
                    if not (name.startswith("swoop_") or name == "struggle"):
                        debug_log(3, f"[{self.module_id}] 跳過動畫觸發（正在拋擲）: {name}")
                        return
            
            # 檢查是否處於靜態幀模式（滑鼠追蹤中）
            # 但 SYSTEM_CYCLE 狀態下的層級動畫應優先於滑鼠追蹤
            if self.current_behavior_state != BehaviorState.SYSTEM_CYCLE:
                if self.ani_module and hasattr(self.ani_module, 'manager'):
                    if getattr(self.ani_module.manager, 'static_frame_mode', False):
                        debug_log(3, f"[{self.module_id}] 跳過動畫觸發（滑鼠追蹤中）: {name}")
                        return

        # 保護機制：如果正在等待動畫完成，避免重複觸發相同動畫（除非強制重新開始）
        if self._awaiting_anim and self._awaiting_anim == name and await_finish and not force_restart:
            debug_log(2, f"[{self.module_id}] 跳過重複動畫觸發: {name}")
            return
        
        # 🔧 檢查動畫超時（防止卡住）
        now = time.time()
        ANIM_TIMEOUT = 5.0  # 30秒超時
        if self._awaiting_anim and self._await_deadline > 0:
            if now > self._await_deadline + ANIM_TIMEOUT:
                debug_log(1, f"[{self.module_id}] ⚠️ 動畫 {self._awaiting_anim} 超時，強制結束")
                # 清除等待狀態
                self._awaiting_anim = None
                self._await_deadline = 0.0
                self.movement_locked_until = 0.0
                self.resume_movement(self.WAIT_ANIM_REASON)
        
        # 動畫切換緩衝：避免頻繁切換導致的閃現
        # 但是高優先度動畫應該能突破防抖限制
        debounce_config = self.config.get("animation_priority", {}).get("debounce", {})
        debounce_enabled = debounce_config.get("enabled", True)
        min_interval = float(debounce_config.get("min_interval", 0.1))
        allow_priority_override = debounce_config.get("allow_priority_override", True)
        
        if debounce_enabled and hasattr(self, '_last_anim_trigger_time'):
            time_since_last = now - self._last_anim_trigger_time
            
            # 檢查是否應該套用防抖
            should_debounce = (
                time_since_last < min_interval and 
                not immediate_interrupt and 
                not force_restart
            )
            
            if should_debounce:
                if allow_priority_override:
                    # 檢查當前請求的優先度是否高於上次的動畫
                    current_priority = self._animation_priority.get_current_priority()
                    if current_priority is not None and priority <= current_priority:
                        debug_log(3, 
                            f"[{self.module_id}] 動畫切換過於頻繁且優先度不足，跳過: {name} "
                            f"(priority={priority.name} <= current={current_priority.name})"
                        )
                        return
                    else:
                        # 高優先度請求，允許突破防抖
                        debug_log(2, 
                            f"[{self.module_id}] 高優先度動畫突破防抖: {name} "
                            f"(priority={priority.name} > current={current_priority.name if current_priority else 'None'})"
                        )
                else:
                    # 不允許優先度覆蓋，直接跳過
                    debug_log(3, f"[{self.module_id}] 動畫切換過於頻繁，跳過: {name}")
                    return
        
        self._last_anim_trigger_time = now

        # 如果強制重新開始或立即中斷，先清除等待狀態
        if (force_restart or immediate_interrupt) and self._awaiting_anim:
            debug_log(2, f"[{self.module_id}] 強制重新開始動畫: {name}")
            self._awaiting_anim = None
            self._await_deadline = 0.0
            self.movement_locked_until = 0.0
            self.resume_movement(self.WAIT_ANIM_REASON)

        # 先送到 ANI（使用 Qt 橋接器確保線程安全）
        if self._qt_bridge:
            try:
                # 如果需要強制重新開始，先停止當前動畫
                if force_restart:
                    self._qt_bridge.stop_animation()
                
                # 使用橋接器觸發動畫（線程安全）
                self._qt_bridge.trigger_animation(name, {"loop": loop})
                debug_log(2, f"[{self.module_id}] 透過 Qt 橋接器觸發動畫: {name} force_restart={force_restart}")
            except Exception as e:
                error_log(f"[{self.module_id}] Qt 橋接器播放動畫失敗: {e}")
        elif self.ani_module and hasattr(self.ani_module, "play"):
            # Fallback：直接調用（不安全，但保持向後兼容）
            try:
                if force_restart and hasattr(self.ani_module, "stop"):
                    self.ani_module.stop()
                
                res = self.ani_module.play(name, loop=loop)
                debug_log(2, f"[{self.module_id}] 直接觸發動畫: {name} res={res} force_restart={force_restart}")
            except Exception as e:
                error_log(f"[{self.module_id}] 向 ANI 播放動畫失敗: {e}")
        else:
            # 回退舊 callbacks
            for cb in list(self._animation_callbacks):
                try: cb(name, {"loop": loop} if loop is not None else {})
                except Exception as e: error_log(f"[{self.module_id}] 動畫回調錯誤: {e}")

        # 需要等待：鎖移動（行為照跑或交由 TransitionBehavior），直到收到 finish 或超時
        if await_finish:
            # 先問 ANI 這個 clip 的實際時長；沒有的話再用預設
            dur = 0.0
            try:
                if self.ani_module and hasattr(self.ani_module, "get_clip_duration"):
                    dur = float(self.ani_module.get_clip_duration(name))
            except Exception:
                pass
            # 加一點裕度（建議 20% 或固定 +0.3s）
            margin = float(self.config.get("anim_timeout_margin", 0.3))
            max_wait = max(dur + margin, float(params.get("max_wait", 0.0)) or self._default_anim_timeout)
        else:
            max_wait = float(params.get("max_wait", self._default_anim_timeout))

    # ========= UI 事件 =========

    def handle_ui_event(self, event_type: UIEventType, data: Dict[str, Any]):
        """處理來自UI的事件"""
        try:
            if event_type == UIEventType.DRAG_START:
                self._on_drag_start(data)
            elif event_type == UIEventType.DRAG_MOVE:
                self._on_drag_move(data)
            elif event_type == UIEventType.DRAG_END:
                self._on_drag_end(data)
            elif event_type == UIEventType.FILE_HOVER:
                if self._file_drop_handler:
                    evt = SimpleNamespace(event_type=event_type, data=data)
                    self._file_drop_handler.handle(evt)
                else:
                    error_log(f"[{self.module_id}] FileDropHandler 未初始化")
            elif event_type == UIEventType.FILE_HOVER_LEAVE:
                if self._file_drop_handler:
                    evt = SimpleNamespace(event_type=event_type, data=data)
                    self._file_drop_handler.handle(evt)
                else:
                    error_log(f"[{self.module_id}] FileDropHandler 未初始化")
            elif event_type == UIEventType.FILE_DROP:
                self._on_file_drop(data)
            else:
                debug_log(2, f"[{self.module_id}] 未處理的UI事件: {event_type}")
        except Exception as e:
            error_log(f"[{self.module_id}] 處理UI事件失敗: {event_type}, 錯誤: {e}")

    def _on_drag_start(self, event):
        if self.mischief_active:
            return
        # 記錄拖曳前的狀態
        self._drag_start_position = self.position.copy()
        self._drag_start_mode = self.movement_mode  # 記錄拖曳前的模式
        
        # 初始化拖曳追蹤器
        self._drag_tracker.clear()
        self._drag_tracker.add_point(self.position.x, self.position.y)
        
        # 強制中斷滑鼠追蹤（不恢復 idle 動畫，直接由 struggle 接管）
        if hasattr(self, '_cursor_tracking_handler'):
            self._cursor_tracking_handler._stop_tracking(restore_idle=False)
            debug_log(2, f"[{self.module_id}] 拖動開始，中斷滑鼠追蹤")
        
        # 取消投擲動畫序列
        if hasattr(self, '_throw_handler'):
            self._throw_handler.cancel_throw()
            debug_log(2, f"[{self.module_id}] 拖動開始，取消投擲動畫")
        
        # ⏸️ 禁止在喚醒期間拖曳（保護 struggle_l 動畫）
        if self._pending_wake_transition:
            debug_log(2, f"[{self.module_id}] 喚醒期間禁止拖曳，保護 struggle_l 動畫")
            return
        
        # 🔧 SYSTEM_CYCLE 狀態下允許拖曳但不改變狀態
        if self.current_behavior_state == BehaviorState.SYSTEM_CYCLE:
            debug_log(2, f"[{self.module_id}] SYSTEM_CYCLE 期間拖曳：允許位置變化但保持狀態")
            self.is_being_dragged = True  # 標記拖曳中（用於位置更新）
            return  # 不改變 movement_mode 和動畫
        
        # 🌙 睡眠狀態下允許拖曳並播放 struggle_l 動畫
        if self.current_behavior_state == BehaviorState.SLEEPING:
            info_log(f"[{self.module_id}] 睡眠期間拖曳：播放 struggle_l 掙扎動畫")
            self.is_being_dragged = True
            # ⚠️ 必須設置 DRAGGING 模式，避免 throw_handler 誤判
            self.movement_mode = MovementMode.DRAGGING
            # 播放睡眠拖曳動畫（struggle_l）
            self._trigger_anim("struggle_l", {"loop": True}, source="drag_sleep")
            return
        
        # 切換到拖曳狀態
        self.is_being_dragged = True
        self.movement_mode = MovementMode.DRAGGING
        self.velocity = Velocity(0.0, 0.0)
        self.target_velocity = Velocity(0.0, 0.0)
        
        self.pause_movement(self.DRAG_PAUSE_REASON)
        
        # 停止當前動畫並重置優先度管理器
        if self.ani_module and hasattr(self.ani_module, 'stop'):
            self.ani_module.stop()
        self._animation_priority.reset()
        
        # 播放掙扎動畫（使用 USER_INTERACTION 優先度）
        struggle_anim = self.anim_query.get_struggle_animation()
        self._trigger_anim(
            struggle_anim, 
            {
                "loop": True,
                "force_restart": True
            }, 
            source="drag_handler",
            priority=AnimationPriority.USER_INTERACTION
        )
        
        mode_desc = "投擲中" if (self._drag_start_mode == MovementMode.THROWN) else (self._drag_start_mode.value if self._drag_start_mode else "未知")
        debug_log(1, f"[{self.module_id}] 拖拽開始於 ({self.position.x:.1f}, {self.position.y:.1f})，從{mode_desc}模式，播放掙扎動畫")

    def _on_drag_move(self, event):
        """處理拖曳移動事件，直接更新位置跟隨滑鼠"""
        if self.mischief_active:
            return
        if not self.is_being_dragged or self._tease_tracker.is_teasing():
            return
        
        # 🌙 檢查是否在睡眠狀態
        is_sleeping = self.current_behavior_state == BehaviorState.SLEEPING
            
        # 支持字典格式的事件數據（來自UI）
        if isinstance(event, dict):
            new_x = float(event.get('x', self.position.x))
            new_y = float(event.get('y', self.position.y))
            
            # 🌙 睡眠時鎖定 Y 座標在地面
            if is_sleeping:
                new_y = self._ground_y()
            
            # Wrap 模式：允許拖曳到任何位置（會在 _check_boundaries 中處理循環）
            # Barrier 模式：限制 X 在螢幕範圍，Y 自由（允許拖到地面判斷模式切換）
            if self.boundary_mode == "wrap":
                self.position.x = new_x
                self.position.y = new_y
            else:
                max_x = self.v_right - self.SIZE
                # 🔧 移除 Y 的上下限制，允許自由拖曳到任何高度
                self.position.x = max(self.v_left, min(max_x, new_x))
                self.position.y = new_y
            
            # **關鍵修復：追蹤拖曳位置以計算速度**
            # 🛑 檢測停止以清除過時速度數據
            if len(self._drag_tracker.history) > 0:
                last_x, last_y, _ = self._drag_tracker.history[-1]
                move_distance = ((self.position.x - last_x) ** 2 + (self.position.y - last_y) ** 2) ** 0.5
                
                # 如果停止（移動距離 < 5px），清除舊點但保留最後一點
                if move_distance < 5.0 and len(self._drag_tracker.history) > 1:
                    last_point = self._drag_tracker.history[-1]
                    self._drag_tracker.history.clear()
                    self._drag_tracker.history.append(last_point)
            
            self._drag_tracker.add_point(self.position.x, self.position.y)
            
            # 發射位置更新
            self._emit_position()
            
            # 降低日誌頻率：每30次才輸出一次
            self._drag_log_counter += 1
            if self._drag_log_counter >= self.LOG_INTERVAL:
                debug_log(3, f"[{self.module_id}] 拖拽移動: ({self.position.x:.1f}, {self.position.y:.1f})")
                self._drag_log_counter = 0
            return
            
        # 支持原有的事件對象格式
        if hasattr(event, 'x') and hasattr(event, 'y'):
            new_x = float(event.x)
            new_y = float(event.y)
            
            # Wrap 模式：允許拖曳到任何位置
            # Barrier 模式：限制在螢幕範圍內
            if self.boundary_mode == "wrap":
                self.position.x = new_x
                self.position.y = new_y
            else:
                max_x = self.v_right - self.SIZE
                max_y = self.v_bottom - self.SIZE
                self.position.x = max(self.v_left, min(max_x, new_x))
                self.position.y = max(self.v_top, min(max_y, new_y))
            
            # 追蹤拖曳位置以計算速度
            # 🛑 檢測停止以清除過時速度數據
            if len(self._drag_tracker.history) > 0:
                last_x, last_y, _ = self._drag_tracker.history[-1]
                move_distance = ((self.position.x - last_x) ** 2 + (self.position.y - last_y) ** 2) ** 0.5
                
                # 如果停止（移動距離 < 5px），清除舊點但保留最後一點
                if move_distance < 5.0 and len(self._drag_tracker.history) > 1:
                    last_point = self._drag_tracker.history[-1]
                    self._drag_tracker.history.clear()
                    self._drag_tracker.history.append(last_point)
            
            self._drag_tracker.add_point(self.position.x, self.position.y)
            
            # 發射位置更新
            self._emit_position()
            
            debug_log(3, f"[{self.module_id}] 拖拽移動: ({self.position.x:.1f}, {self.position.y:.1f})")
        elif hasattr(event, 'data') and isinstance(event.data, dict):
            # 如果位置資訊在data字典中
            data = event.data
            if 'x' in data and 'y' in data:
                new_x = float(data['x'])
                new_y = float(data['y'])
                
                # Wrap 模式：允許拖曳到任何位置
                # Barrier 模式：限制在螢幕範圍內
                if self.boundary_mode == "wrap":
                    self.position.x = new_x
                    self.position.y = new_y
                else:
                    max_x = self.v_right - self.SIZE
                    max_y = self.v_bottom - self.SIZE
                    self.position.x = max(self.v_left, min(max_x, new_x))
                    self.position.y = max(self.v_top, min(max_y, new_y))
                
                # 追蹤拖曳位置以計算速度
                # 🛑 檢測停止以清除過時速度數據
                if len(self._drag_tracker.history) > 0:
                    last_x, last_y, _ = self._drag_tracker.history[-1]
                    move_distance = ((self.position.x - last_x) ** 2 + (self.position.y - last_y) ** 2) ** 0.5
                    
                    # 如果停止（移動距離 < 5px），清除舊點但保留最後一點
                    if move_distance < 5.0 and len(self._drag_tracker.history) > 1:
                        last_point = self._drag_tracker.history[-1]
                        self._drag_tracker.history.clear()
                        self._drag_tracker.history.append(last_point)
                
                self._drag_tracker.add_point(self.position.x, self.position.y)
                
                self._emit_position()
                # 降低日誌頻率：每30次才輸出一次
                self._drag_log_counter += 1
                if self._drag_log_counter >= self.LOG_INTERVAL:
                    debug_log(3, f"[{self.module_id}] 拖拽移動: ({self.position.x:.1f}, {self.position.y:.1f})")
                    self._drag_log_counter = 0

    def _on_drag_end(self, event):
        """
        拖曳結束處理 - 使用 ThrowHandler 檢測投擲
        
        支持空中接住：在 THROWN 模式下也可以重新拖動
        """
        if self.mischief_active:
            return
        # 如果正在播放 tease 動畫，忽略事件
        if self._tease_tracker.is_teasing():
            return
        
        # ⏸️ 禁止在喚醒期間處理拖曳結束事件（保護 struggle_l 動畫）
        if self._pending_wake_transition:
            debug_log(2, f"[{self.module_id}] 喚醒期間忽略拖曳結束事件")
            return
        
        # 🔧 SYSTEM_CYCLE 期間拖曳結束：只清除拖曳標記，不改變狀態
        if self.current_behavior_state == BehaviorState.SYSTEM_CYCLE:
            debug_log(2, f"[{self.module_id}] SYSTEM_CYCLE 期間拖曳結束：保持原狀態")
            self.is_being_dragged = False
            return
        
        # 🌙 睡眠狀態下：拖曳結束後維持睡眠，不進行任何狀態切換或投擲判定
        if self.current_behavior_state == BehaviorState.SLEEPING:
            self.is_being_dragged = False
            # ⚠️ 重置 movement_mode 為 GROUND（睡眠時不應該是 DRAGGING）
            self.movement_mode = MovementMode.GROUND
            # 🔧 停止 struggle 動畫並重置優先度管理器
            if self.ani_module and hasattr(self.ani_module, 'stop'):
                self.ani_module.stop()
            self._animation_priority.reset()
            # 🌙 恢復 sleep_l 動畫（struggle_l → sleep_l）
            self._trigger_anim("sleep_l", {"loop": True}, source="drag_end_sleep", priority=AnimationPriority.SYSTEM_CYCLE)
            # 更新位置（確保前端同步）
            self._emit_position()
            info_log(f"[{self.module_id}] 睡眠狀態下拖曳結束（恢復 sleep_l 動畫）")
            return
        
        self.is_being_dragged = False
        
        # 記錄互動（拖曳或投擲都算）
        self._tease_tracker.record_interaction()
        
        # 使用 ThrowHandler 檢測投擲
        is_throw = self._throw_handler.check_throw(self._drag_tracker, self._drag_start_position)
        
        # 檢查是否達到 tease 閾值（不立即觸發，標記為 pending）
        if not is_throw:
            should_tease = self._tease_tracker.should_trigger_tease()
            
            if should_tease:
                # 標記為待觸發，等回到 IDLE 時才播放
                self._tease_tracker.set_pending()
                debug_log(2, f"[{self.module_id}] Tease 閾值已達到，標記為待觸發")
        
        if not is_throw:
            # 沒有投擲，根據高度判斷模式
            gy = self._ground_y()
            current_height = gy - self.position.y
            height_threshold = 100  # 高度閾值
            
            if current_height > height_threshold:
                # 拖曳到較高位置 -> 浮空模式
                self.movement_mode = MovementMode.FLOAT
                # 🔧 手動停止 struggle 動畫並重置優先度管理器
                if self.ani_module and hasattr(self.ani_module, 'stop'):
                    self.ani_module.stop()
                self._animation_priority.reset()
                # 🔧 如果有 pending tease，不觸發 idle 動畫，讓 tease 優先播放
                if not self._tease_tracker.has_pending():
                    # 以正常的 IDLE_ANIMATION 優先度觸發 idle 動畫
                    idle_anim = self.anim_query.get_idle_animation_for_mode(is_ground=False)
                    self._trigger_anim(idle_anim, {"loop": True, "force_restart": True}, source="idle_behavior", priority=AnimationPriority.IDLE_ANIMATION)
                debug_log(1, f"[{self.module_id}] 切換到浮空模式 (高度:{current_height:.1f} > {height_threshold})")
            else:
                # 拖曳到較低位置 -> 落地模式
                self.movement_mode = MovementMode.GROUND
                # 確保在地面上
                self.position.y = gy
                # 🔧 手動停止 struggle 動畫並重置優先度管理器
                if self.ani_module and hasattr(self.ani_module, 'stop'):
                    self.ani_module.stop()
                self._animation_priority.reset()
                # 🔧 如果有 pending tease，不觸發 idle 動畫，讓 tease 優先播放
                if not self._tease_tracker.has_pending():
                    # 以正常的 IDLE_ANIMATION 優先度觸發 idle 動畫
                    idle_anim = self.anim_query.get_idle_animation_for_mode(is_ground=True)
                    self._trigger_anim(idle_anim, {"loop": True, "force_restart": True}, source="idle_behavior", priority=AnimationPriority.IDLE_ANIMATION)
                debug_log(1, f"[{self.module_id}] 切換到落地模式 (高度:{current_height:.1f} <= {height_threshold})")
        
        # 恢復移動並切換到idle狀態
        self.resume_movement(self.DRAG_PAUSE_REASON)
        if not is_throw:  # 投擲模式由物理引擎自動轉換
            self._switch_behavior(BehaviorState.IDLE)
        
        # 更新位置發射
        self._emit_position()
        
        debug_log(1, f"[{self.module_id}] 拖拽結束 → {self.movement_mode.value} 模式")

    def _on_file_drop(self, data: Dict[str, Any]):
        """處理檔案拖放事件"""
        if self._file_drop_handler:
            self._file_drop_handler.handle(data)
        else:
            error_log(f"[{self.module_id}] FileDropHandler 未初始化")

    # ========= API =========

    def _api_get_status(self) -> Dict[str, Any]:
        return {
            "position": {"x": self.position.x, "y": self.position.y},
            "velocity": {"x": self.velocity.x, "y": self.velocity.y},
            "mode": self.movement_mode.value,
            "state": self.current_behavior_state.value,
            "mischief_active": self.mischief_active,
            "target": None if not self.movement_target else {"x": self.movement_target.x, "y": self.movement_target.y},
        }

    def _api_set_position(self, data: Dict[str, Any]) -> Dict[str, Any]:
        # Tease 動畫期間禁止位置設置
        if self._tease_tracker.is_teasing():
            return {"success": False, "reason": "tease_animation_playing"}
        
        x = float(data.get("x", self.position.x))
        y = float(data.get("y", self.position.y))
        
        # 如果正在拖曳，允許自由設置位置，不受地面鎖定限制
        if self.is_being_dragged:
            # 拖曳時允許完全自由的位置設置
            self.position.x = x
            self.position.y = y
            debug_log(3, f"[{self.module_id}] 拖曳中位置更新: ({x:.1f}, {y:.1f})")
        else:
            # 非拖曳時按照正常邏輯設置位置
            self.position.x = x
            self.position.y = y
            # 如果是地面模式，確保Y在地面上
            if self.movement_mode == MovementMode.GROUND:
                self.position.y = self._ground_y()
        
        self._emit_position()
        return {"success": True}

    def _api_set_velocity(self, data: Dict[str, Any]) -> Dict[str, Any]:
        vx = float(data.get("vx", self.velocity.x))
        vy = float(data.get("vy", self.velocity.y))
        self.velocity.x = vx
        self.velocity.y = vy
        return {"success": True}

    # ========= MISCHIEF 支援 =========
    def _start_mischief_action(self, action_id: str, target: Optional[Dict[str, Any]], animation: Optional[str]):
        """啟動單次 MISCHIEF 前端行為（手動/測試入口）"""
        self.mischief_active = True
        if target and "x" in target and "y" in target:
            self._mischief_pending_target = Position(float(target["x"]), float(target["y"]))
        else:
            self._mischief_pending_target = None
        self._mischief_pending_anim = animation
        # 禁用跟隨/拖曳
        self.is_being_dragged = False
        self._cursor_tracking_enabled = False
        # 使用漂浮模式，避免地面鎖定
        self.movement_mode = MovementMode.FLOAT
        self.movement_paused = False
        self._mischief_info = {"action": action_id, "target": target, "animation": animation}
        info_log(f"[{self.module_id}] 🐾 MISCHIEF action started: {action_id}, anim={animation}, target={target}")

    def _end_mischief_action(self):
        """結束 MISCHIEF 行為，恢復正常行為流程"""
        self.mischief_active = False
        self._mischief_pending_target = None
        self._mischief_pending_anim = None
        self._mischief_end_at = 0.0
        # 重置動畫優先度，避免 USER_INTERACTION 卡住
        if hasattr(self, "_animation_priority"):
            self._animation_priority.reset()
        # 恢復滑鼠追蹤設定
        self._cursor_tracking_enabled = get_user_setting("behavior.movement.enable_cursor_tracking", True)
        # 切回 IDLE 行為
        self._switch_behavior(BehaviorState.IDLE)
        info_log(f"[{self.module_id}] 🐾 MISCHIEF action ended，回到 {self.current_behavior_state.value}")
        debug_log(2, f"[{self.module_id}] MISCHIEF detail: {self._mischief_info}")
        self._mischief_info = {}

    def _handle_mischief_event(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        處理來自 FrontendBridge 的 MISCHIEF 行為事件。
        data:
          - action_id: MoveWindowAction / ClickShortcutAction / CreateTextFileAction / ...
          - animation: 對應的動畫名稱
          - rect: {x, y, width, height} (視窗或捷徑的區域)
          - edge: up/down/left/right（推窗使用）
          - anchor: 用於 click 的錨點（例如 top_right）
        """
        try:
            action_id = data.get("action_id", "unknown")
            animation = data.get("animation")
            rect = data.get("rect") or {}
            edge = data.get("edge")
            anchor = data.get("anchor", "center")
            label = data.get("label")

            target = None
            if rect:
                target = self._calc_mischief_target(rect, edge=edge, anchor=anchor)

            # 可覆寫動畫等待時間
            if "anim_timeout" in data:
                try:
                    self._mischief_anim_timeout = float(data.get("anim_timeout", self._default_anim_timeout))
                except Exception:
                    self._mischief_anim_timeout = self._default_anim_timeout

            # 保存額外信息（例如捷徑名稱/視窗標題）
            if label:
                self._mischief_info = {"action": action_id, "target": target, "animation": animation, "label": label}
            else:
                self._mischief_info = {"action": action_id, "target": target, "animation": animation}

            self._start_mischief_action(action_id, target, animation)
            debug_log(2, f"[{self.module_id}] MISCHIEF event received: action={action_id}, label={label}, rect={rect}, edge={edge}, anchor={anchor}, target={target}")
            return {"success": True, "action": action_id, "target": target, "animation": animation, "label": label}
        except Exception as e:
            error_log(f"[{self.module_id}] 無法處理 MISCHIEF 事件: {e}")
            return {"error": str(e)}

    def _calc_mischief_target(self, rect: Dict[str, Any], edge: Optional[str] = None, anchor: str = "center") -> Dict[str, float]:
        """根據區域和方向計算 MISCHIEF 動畫定位點"""
        x = float(rect.get("x", 0.0))
        y = float(rect.get("y", 0.0))
        w = float(rect.get("width", 0.0))
        h = float(rect.get("height", 0.0))

        center_x = x + w * 0.5
        center_y = y + h * 0.5

        if edge:
            edge = edge.lower()
            offset = 40  # 離開視窗邊一點，避免遮住標題
            if edge == "left":
                return {"x": x - offset, "y": center_y}
            if edge == "right":
                return {"x": x + w + offset, "y": center_y}
            if edge == "up" or edge == "top":
                return {"x": center_x, "y": y - offset}
            if edge == "down" or edge == "bottom":
                return {"x": center_x, "y": y + h + offset}

        # anchor 用於 click 之類的精確定位
        anchor = (anchor or "center").lower()
        if anchor == "top_right":
            return {"x": x + w - 10, "y": y - 20}
        if anchor == "top_left":
            return {"x": x + 10, "y": y - 20}
        if anchor == "bottom_right":
            return {"x": x + w - 10, "y": y + h + 20}
        if anchor == "bottom_left":
            return {"x": x + 10, "y": y + h + 20}

        return {"x": center_x, "y": center_y}

    # ========= 輸出 =========

    def _emit_position(self):
        x, y = int(self.position.x), int(self.position.y)
        for cb in list(self._position_callbacks):
            try:
                cb(x, y)
            except Exception as e:
                error_log(f"[{self.module_id}] 位置回調錯誤: {e}")

    # ========= 系統狀態（框架回調） =========

    def on_system_state_changed(self, old_state: UEPState, new_state: UEPState):
        """系統狀態變化回調
        
        注意：
        - SLEEP_ENTERED → FrontendBridge 調用此方法 → _enter_sleep_state()
        - SLEEP_EXITED → FrontendBridge 直接調用 _exit_sleep_state()（不經過此方法）
        
        因此：只處理進入 SLEEP，不處理退出 SLEEP
        """
        debug_log(1, f"[{self.module_id}] 系統狀態變更: {old_state} -> {new_state}")
        self._current_system_state = new_state
        
        # SLEEP 狀態進入處理
        if new_state == UEPState.SLEEP:
            self._enter_sleep_state()
        # IDLE 狀態時清除層級
        elif new_state == UEPState.IDLE:
            self._current_layer = None
            # SystemCycleBehavior 已結束，切換回 IdleBehavior 會自動處理 IDLE 動畫
    def _enter_sleep_state(self):
        """進入 SLEEP 狀態處理
    
        流程：
        1. 檢查當前是否在 ground 模式
        2. 如果在 float 模式，先執行 f_to_g 轉換
        3. 執行 g_to_l 轉換動畫
        4. 進入 sleep_l 循環動畫
        5. 切換行為狀態為 SLEEPING
        """
        try:
            info_log(f"[{self.module_id}] 🌙 進入 SLEEP 狀態")
            info_log(f"[{self.module_id}] 當前 movement_mode: {self.movement_mode}, 位置: ({self.position.x:.1f}, {self.position.y:.1f})")
        
            # 停止當前移動
            self.movement_locked_until = time.time() + 999999  # 鎖定移動
        
            # 🔧 **先檢查是否需要轉換，再切換行為**（避免 SleepBehavior.on_enter 自動觸發 g_to_l）
            # 如果角色在視覺上還在浮空（即使 movement_mode 已經是 GROUND），也要強制執行 transition
            ground_y = self._ground_y()
            height_from_ground = ground_y - self.position.y
            is_visually_floating = height_from_ground > 50  # 超過50像素視為浮空
            
            needs_transition = (self.movement_mode == MovementMode.FLOAT) or is_visually_floating
            
            if needs_transition:
                info_log(f"[{self.module_id}] 需要轉換到地面 (mode={self.movement_mode}, height={height_from_ground:.1f})")
                # 強制切換到 FLOAT 模式（確保能觸發 f_to_g）
                if self.movement_mode != MovementMode.FLOAT:
                    info_log(f"[{self.module_id}] 強制切換到 FLOAT 模式以執行轉換動畫")
                    self.movement_mode = MovementMode.FLOAT
                
                # 🔧 重置轉場動畫完成標誌
                self._transition_animation_finished = False
                
                # 🔧 使用 TRANSITION 行為來實際移動到地面（不只是播放動畫）
                info_log(f"[{self.module_id}] 切換到 TRANSITION 行為以回到地面")
                self._switch_behavior(BehaviorState.TRANSITION)
                
                # 等待轉換完成後再切換到 SLEEPING 並執行 g_to_l
                self._pending_sleep_transition = True
                info_log(f"[{self.module_id}] 標記等待 transition 完成後繼續睡眠轉換")
                return
        
            # 已經在 ground，切換行為並直接執行躺下動畫
            info_log(f"[{self.module_id}] 已在地面，切換到 SLEEPING 行為並執行睡眠轉換")
            self._switch_behavior(BehaviorState.SLEEPING)
            self._execute_sleep_transition()
        
        except Exception as e:
            error_log(f"[{self.module_id}] 進入 SLEEP 狀態失敗: {e}")
            import traceback
            error_log(traceback.format_exc())

    def _execute_sleep_transition(self):
        """執行睡眠轉換動畫 (g_to_l → sleep_l)
        注意：此方法可能在兩種情況下被調用：
        1. 從 _enter_sleep_state 直接調用（已經在地面）
        2. 從 f_to_g 完成回調調用（_pending_sleep_transition=True）
        在兩種情況下都需要確保切換到 SLEEPING 行為
        """
        try:
            # 如果還沒切換到 SLEEPING 行為，現在切換
            if self.current_behavior_state != BehaviorState.SLEEPING:
                info_log(f"[{self.module_id}] 切換到 SLEEPING 行為")
                self._switch_behavior(BehaviorState.SLEEPING)
            
            info_log(f"[{self.module_id}] 執行睡眠轉換: g_to_l → sleep_l")
        
            # 播放 g_to_l 轉換動畫
            self._trigger_anim("g_to_l", {
                "loop": False,
                "force_restart": True
            }, source="entering_sleep", priority=AnimationPriority.SYSTEM_CYCLE)
        
            # 標記睡眠動畫已開始
            self._is_sleeping = True
        
            # 在 g_to_l 完成後會自動切換到 sleep_l（在動畫完成回調中處理）
        
        except Exception as e:
            error_log(f"[{self.module_id}] 執行睡眠轉換失敗: {e}")

    def _exit_sleep_state(self):
        """退出 SLEEP 狀態處理
    
        流程：
        1. 停止 sleep_l 循環動畫
        2. 播放 struggle_l 作為過渡動畫（後台模組重載時保持 UI 流暢）
        3. 標記 _pending_wake_transition，等待 WAKE_READY 事件
        4. WAKE_READY（模組重載完成）→ 播放 l_to_g → 切換 IDLE
        
        由 FrontendBridge 在收到 SLEEP_EXITED 事件時調用（只會調用一次）
        """
        try:
            info_log(f"[{self.module_id}] ☀️ 退出 SLEEP 狀態，播放過渡動畫...")
        
            # 🔧 重置睡眠相關狀態（確保下次睡眠能正常進入）
            self._is_sleeping = False
            self._pending_sleep_transition = False
            self.transition_start_time = None  # 重置轉場計時器
            
            # 🔧 標記等待喚醒轉換完成（等待 WAKE_READY 事件）
            # WAKE_READY 事件到達時才會播放 l_to_g 動畫
            self._pending_wake_transition = True
            self._wake_ready = False
            
            # 🎬 播放 struggle_l 作為過渡動畫
            # 此動畫會在後台模組重載時持續播放，讓 UI 保持流暢
            # 直到 WAKE_READY 事件到達時被 l_to_g 取代
            self._trigger_anim(
                "struggle_l",
                {
                    "loop": True,
                    "force_restart": True
                },
                source="wake_transition",
                priority=AnimationPriority.SYSTEM_CYCLE
            )
            
            info_log(f"[{self.module_id}] 播放 struggle_l 過渡動畫，等待 WAKE_READY（模組重載中）")
        
        except Exception as e:
            error_log(f"[{self.module_id}] 退出 SLEEP 狀態失敗: {e}")
            import traceback
            error_log(traceback.format_exc())
    
    
    # ========= 層級事件訂閱與處理 =========
    
    def _subscribe_to_layer_events(self):
        """訂閱層級完成事件以驅動動畫
        
        注意：所有 EventBus 事件訂閱已移至 FrontendBridge 統一管理
        MOV 模組不再直接訂閱任何 EventBus 事件，而是通過 FrontendBridge 的方法調用接收事件
        這樣確保了清晰的職責分離和一致的事件流向
        """
        try:
            info_log(f"[{self.module_id}] ✅ MOV 模組已準備接收 FrontendBridge 轉發的事件")
            info_log(f"[{self.module_id}]    所有事件（互動 + 層級 + GS 生命週期 + SLEEP）由 FrontendBridge 統一管理")
            info_log(f"[{self.module_id}]    MOV 提供回調方法供 FrontendBridge 調用")
            
        except Exception as e:
            error_log(f"[{self.module_id}] ❌ 準備事件接收失敗: {e}")
            import traceback
            error_log(traceback.format_exc())
    
    def _on_interaction_started(self, event):
        """使用者互動開始 - STT 檢測到語音輸入"""
        try:
            info_log(f"[{self.module_id}] 🎤 收到 INTERACTION_STARTED 事件")
            info_log(f"[{self.module_id}]    當前行為: {self.current_behavior_state.value}")
            
            # 🎤 如果正在 ON_CALL 中，互動開始表示用戶已說話，應立即結束 ON_CALL
            if self._on_call_active:
                info_log(f"[{self.module_id}] 檢測到 ON_CALL 活躍，互動開始時自動結束 ON_CALL")
                self.end_on_call_animation()
            
            # 如果正在拖動，強制結束拖動並清除 dragging 模式
            if self.is_being_dragged:
                debug_log(2, f"[{self.module_id}] INTERACTION_STARTED 時正在拖動，強制結束拖動")
                self.is_being_dragged = False
                if self.movement_mode == MovementMode.DRAGGING:
                    # 恢復到之前的模式（ground 或 float）
                    if self._drag_start_mode:
                        self.movement_mode = self._drag_start_mode
                    else:
                        self.movement_mode = MovementMode.GROUND
                    debug_log(2, f"[{self.module_id}] 恢復移動模式: {self.movement_mode.value}")
            
            # 進入系統循環狀態，暫停移動
            self._switch_behavior(BehaviorState.SYSTEM_CYCLE)
            self.pause_movement("system_cycle")
            
            # 設置輸入層狀態並觸發動畫
            self._current_layer = "input"
            info_log(f"[{self.module_id}]    切換至: {self.current_behavior_state.value}, 層級: input")
            
            # 使用 LayerEventHandler 處理
            if self._layer_handler:
                info_log(f"[{self.module_id}]    使用 LayerEventHandler 處理")
                self._layer_handler.handle(event)
            
            # 動畫由 SystemCycleBehavior.on_tick() 處理
            
        except Exception as e:
            error_log(f"[{self.module_id}] ❌ 處理互動開始事件失敗: {e}")
            import traceback
            error_log(traceback.format_exc())
    
    def _on_input_layer_complete(self, event):
        """輸入層完成 - 進入處理層"""
        try:
            info_log(f"[{self.module_id}] 📥 收到 INPUT_LAYER_COMPLETE 事件")
            info_log(f"[{self.module_id}]    當前層級: {self._current_layer}")
            
            # 使用 LayerEventHandler 處理
            if self._layer_handler and self._layer_handler.can_handle(event):
                info_log(f"[{self.module_id}]    使用 LayerEventHandler 處理")
                self._layer_handler.handle(event)
            else:
                info_log(f"[{self.module_id}]    使用 Fallback 處理")
                # Fallback：手動更新
                self._current_layer = "processing"
                # 動畫由 SystemCycleBehavior.on_tick() 處理
                
        except Exception as e:
            error_log(f"[{self.module_id}] ❌ 處理輸入層完成事件失敗: {e}")
            import traceback
            error_log(traceback.format_exc())
    
    def _on_processing_layer_complete(self, event):
        """處理層完成 - 進入輸出層"""
        try:
            info_log(f"[{self.module_id}] ⚙️ 收到 PROCESSING_LAYER_COMPLETE 事件")
            info_log(f"[{self.module_id}]    當前層級: {self._current_layer}")
            
            # 使用 LayerEventHandler 處理
            if self._layer_handler and self._layer_handler.can_handle(event):
                info_log(f"[{self.module_id}]    使用 LayerEventHandler 處理")
                self._layer_handler.handle(event)
            else:
                info_log(f"[{self.module_id}]    使用 Fallback 處理")
                # Fallback：手動更新
                self._current_layer = "output"
                # 動畫由 SystemCycleBehavior.on_tick() 處理
                
        except Exception as e:
            error_log(f"[{self.module_id}] ❌ 處理處理層完成事件失敗: {e}")
            import traceback
            error_log(traceback.format_exc())
    
    def _on_output_layer_complete(self, event):
        """輸出層完成 - 觸發輸出層動畫"""
        try:
            info_log(f"[{self.module_id}] 📤 收到 OUTPUT_LAYER_COMPLETE 事件")
            info_log(f"[{self.module_id}]    當前層級: {self._current_layer}")
            
            # 使用 LayerEventHandler 處理（更新層級狀態）
            if self._layer_handler and self._layer_handler.can_handle(event):
                info_log(f"[{self.module_id}]    使用 LayerEventHandler 處理")
                self._layer_handler.handle(event)
            
            # ⚠️ 不要調用 _update_animation_for_current_state()
            # 因為它會檢查 behavior_state 而不是 current_layer
            # SystemCycleBehavior.on_tick() 會自動檢測 current_layer 並觸發動畫
            
            # 注意：_current_layer 會在 CYCLE_COMPLETED 事件時清除並恢復 idle
        except Exception as e:
            error_log(f"[{self.module_id}] 處理輸出層完成事件失敗: {e}")
    
    def _on_session_started(self, event):
        """會話開始 - 記錄當前 GS ID"""
        try:
            session_id = event.data.get('session_id')
            session_type = event.data.get('session_type', 'unknown')
            
            # 只追蹤 General Session
            if session_type == 'general':
                self._current_gs_id = session_id
                debug_log(2, f"[{self.module_id}] 📝 GS 開始: {session_id}")
        except Exception as e:
            error_log(f"[{self.module_id}] 處理會話開始事件失敗: {e}")
    
    def _on_cycle_completed(self, event):
        """循環完成 - 回到 IDLE 狀態"""
        try:
            # 如果當前在 SYSTEM_CYCLE 狀態，循環完成時回到 IDLE
            if self.current_behavior_state == BehaviorState.SYSTEM_CYCLE:
                debug_log(2, f"[{self.module_id}] 🔄 循環完成，回到 IDLE 狀態")
                
                # 🔧 停止當前的系統循環動畫（thinking等）
                if self._current_playing_anim:
                    debug_log(2, f"[{self.module_id}] 停止系統循環動畫: {self._current_playing_anim}")
                    try:
                        if self._qt_bridge:
                            self._qt_bridge.stop_animation()
                    except Exception as e:
                        debug_log(3, f"[{self.module_id}] 停止動畫失敗: {e}")
                
                # 清除層級狀態
                self._current_layer = None
                
                # 清除當前播放的動畫記錄（允許重新觸發 IDLE 動畫）
                self._current_playing_anim = None
                
                # 恢復移動
                self.resume_movement("system_cycle")
                
                # 切換回 IDLE 行為（IdleBehavior.on_enter() 會自動播放 idle 動畫）
                self._switch_behavior(BehaviorState.IDLE)
                
        except Exception as e:
            error_log(f"[{self.module_id}] 處理循環完成事件失敗: {e}")
    
    def _on_gs_advanced(self, event):
        """
GS 推進 - 當前 GS 結束，恢復 idle 狀態和移動"""
        try:
            old_gs_id = event.data.get('old_gs_id')
            new_gs_id = event.data.get('new_gs_id')
            
            debug_log(2, f"[{self.module_id}] 🔄 GS 推進: {old_gs_id} → {new_gs_id}")
            
            # 如果當前在 SYSTEM_CYCLE 狀態，且舊 GS 結束，恢復正常狀態
            if (self.current_behavior_state == BehaviorState.SYSTEM_CYCLE and 
                old_gs_id == self._current_gs_id):
                
                debug_log(2, f"[{self.module_id}] ✅ GS {old_gs_id} 結束，恢復 idle 狀態")
                
                # 清除層級狀態
                self._current_layer = None
                
                # 清除當前播放的動畫記錄（允許重新觸發 IDLE 動畫）
                self._current_playing_anim = None
                
                # 恢復移動
                self.resume_movement("system_cycle")
                
                # 切換回 IDLE 行為（IdleBehavior.on_enter() 會自動播放 idle 動畫）
                self._switch_behavior(BehaviorState.IDLE)
            
            # 更新當前 GS ID
            self._current_gs_id = new_gs_id
            
        except Exception as e:
            error_log(f"[{self.module_id}] 處理 GS 推進事件失敗: {e}")
    
    def _on_wake_ready(self, event):
        """
        收到 WAKE_READY 事件 - 後端模組已重載完成
        
        此時應播放 l_to_g 起身動畫，動畫完成後再切換回 IDLE
        流程：
        1. 播放 l_to_g 動畫（使用系統週期優先度）
        2. 等待動畫完成（由 _on_animation_finished 處理）
        3. 動畫完成後切換到 IDLE，播放對應的 idle 動畫
        """
        try:
            info_log(f"[{self.module_id}] 📨 收到 WAKE_READY 事件，後端模組已重載完成")
            
            self._wake_ready = True
            
            # 如果正在等待喚醒轉換完成，現在可以播放喚醒動畫了
            if self._pending_wake_transition:
                info_log(f"[{self.module_id}] 🎬 播放 l_to_g 起身動畫...")
                
                # 播放起身動畫（使用高優先度，確保優先於其他動畫）
                self._trigger_anim(
                    "l_to_g",
                    {
                        "loop": False,
                        "force_restart": True
                    },
                    source="wake_handler",
                    priority=AnimationPriority.SYSTEM_CYCLE
                )
                
                # 標記動畫完成後應自動切換到 IDLE（由動畫完成回調處理）
                # 不在此處立即切換，讓動畫完成回調負責切換
            
        except Exception as e:
            error_log(f"[{self.module_id}] 處理 WAKE_READY 事件失敗: {e}")
            import traceback
            error_log(traceback.format_exc())
    
    def _load_state_animation_config(self) -> Optional[Dict]:
        """載入狀態-動畫映射配置"""
        try:
            # 從 ANI 模組目錄載入配置
            ani_path = os.path.join(
                os.path.dirname(os.path.dirname(__file__)),
                "ani_module",
                "state_animations.yaml"
            )
            if os.path.exists(ani_path):
                with open(ani_path, 'r', encoding='utf-8') as f:
                    config = yaml.safe_load(f)
                    debug_log(2, f"[{self.module_id}] 已載入狀態動畫配置")
                    return config
            else:
                debug_log(2, f"[{self.module_id}] 未找到狀態動畫配置檔案: {ani_path}")
            return None
        except Exception as e:
            error_log(f"[{self.module_id}] 載入狀態動畫配置失敗: {e}")
            return None
    
    def _update_animation_for_current_state(self):
        """根據當前層級和系統狀態更新動畫"""
        try:
            debug_log(2, f"[{self.module_id}] 🔄 _update_animation_for_current_state 被調用")
            debug_log(2, f"[{self.module_id}]    系統狀態: {self._current_system_state}")
            debug_log(2, f"[{self.module_id}]    當前層級: {self._current_layer}")
            debug_log(2, f"[{self.module_id}]    行為狀態: {self.current_behavior_state}")
            
            if not self.ani_module:
                debug_log(3, f"[{self.module_id}] ANI 模組未注入，無法更新動畫")
                return
            
            if not self._state_animation_config:
                debug_log(2, f"[{self.module_id}] 無狀態動畫配置")
                return
            
            # IDLE 狀態：播放閒置動畫
            if self._current_system_state == UEPState.IDLE:
                debug_log(2, f"[{self.module_id}] → 處理 IDLE 動畫")
                self._handle_idle_animation()
                return
            
            # 有層級時根據層級選擇動畫
            if self._current_layer:
                debug_log(2, f"[{self.module_id}] → 處理層級動畫: {self._current_layer}")
                self._handle_layer_animation()
            else:
                # 無層級時使用預設閒置動畫
                debug_log(3, f"[{self.module_id}] 無當前層級，使用閒置動畫")
                self._handle_idle_animation()
                
        except Exception as e:
            error_log(f"[{self.module_id}] 更新動畫失敗: {e}")
    
    def _handle_idle_animation(self):
        """處理 IDLE 狀態的動畫（只在未播放時觸發）"""
        try:
            config = self._state_animation_config
            if not config:
                return
            
            idle_config = config.get("IDLE", {})
            idle_anims = idle_config.get("idle_animations", [])
            
            if idle_anims:
                # 選擇符合當前移動模式的動畫
                anim_name = self._get_compatible_animation_from_list(idle_anims)
                if anim_name:
                    # 檢查是否已經在播放相同的 loop 動畫
                    if hasattr(self, '_current_playing_anim') and self._current_playing_anim == anim_name:
                        debug_log(3, f"[{self.module_id}] IDLE 動畫已在播放: {anim_name}")
                        return
                    
                    self._current_playing_anim = anim_name
                    self._trigger_anim(anim_name, {"loop": True}, source="idle_behavior")
                    debug_log(2, f"[{self.module_id}] IDLE 動畫: {anim_name}")
            else:
                debug_log(2, f"[{self.module_id}] IDLE 狀態: 無可用動畫")
                
        except Exception as e:
            error_log(f"[{self.module_id}] 處理 IDLE 動畫失敗: {e}")
    
    def _handle_layer_animation(self):
        """根據當前層級選擇動畫（透過 LayerAnimationStrategy）"""
        try:
            if not self._current_layer:
                debug_log(2, f"[{self.module_id}] 層級動畫: 無當前層級")
                return
            
            debug_log(2, f"[{self.module_id}] 🎬 處理層級動畫: {self._current_layer}")
            
            # 準備 context 給 strategy
            context = {
                'layer': self._current_layer,
                'state': self._current_system_state,
                'movement_mode': self.movement_mode,
                'mood': 0  # 預設 mood
            }
            
            # 嘗試從 status_manager 獲取 mood
            try:
                from core.status_manager import status_manager
                context['mood'] = status_manager.status.mood
            except Exception:
                pass
            
            # 使用 LayerAnimationStrategy 選擇動畫
            if hasattr(self, '_layer_strategy') and self._layer_strategy:
                anim_name = self._layer_strategy.select_animation(context)
                if anim_name:
                    debug_log(2, f"[{self.module_id}] 層級動畫選擇: {anim_name}（層級: {self._current_layer}）")
                    
                    # 清除當前播放的動畫記錄（層級動畫優先）
                    if hasattr(self, '_current_playing_anim'):
                        self._current_playing_anim = None
                    
                    # 根據層級決定是否循環
                    loop = self._current_layer in ["input", "processing"]
                    
                    debug_log(1, f"[{self.module_id}] ⚡ 強制觸發層級動畫: {anim_name}（層級: {self._current_layer}, loop={loop}）")
                    
                    # 層級動畫必須立即中斷當前動畫，避免被防抖機制阻擋
                    self._trigger_anim(anim_name, {
                        "loop": loop,
                        "immediate_interrupt": True,  # 強制中斷當前動畫
                        "force_restart": True  # 強制重新開始
                    }, source="system_cycle_behavior")
                    
                    debug_log(1, f"[{self.module_id}] ✅ 層級動畫已觸發: {anim_name}")
                else:
                    debug_log(2, f"[{self.module_id}] 層級 {self._current_layer}: strategy 未返回動畫")
            else:
                debug_log(2, f"[{self.module_id}] LayerAnimationStrategy 未初始化")
                
        except Exception as e:
            error_log(f"[{self.module_id}] 處理層級動畫失敗: {e}")
    
    def _convert_animation_for_movement_mode(self, name: str) -> Optional[str]:
        """
        根據當前移動模式轉換動畫名稱
        
        規則：
        - _f 後綴：浮空動畫（FLOAT 模式）
        - _g 後綴：地面動畫（GROUND 模式）
        - 自動轉換不相容的動畫
        """
        if not name:
            return None
        
        try:
            # 檢查動畫後綴
            if name.endswith('_f'):
                # 浮空動畫
                if self.movement_mode == MovementMode.GROUND:
                    # 當前在地面，嘗試找地面版本
                    alternative = name[:-2] + '_g'
                    if self.ani_module and hasattr(self.ani_module, 'manager'):
                        if alternative in self.ani_module.manager.clips:
                            debug_log(2, f"[{self.module_id}] 動畫轉換: {name} -> {alternative} (地面)")
                            return alternative
                    # 沒有地面版本，使用原動畫
                    return name
                return name
                
            elif name.endswith('_g'):
                # 地面動畫
                if self.movement_mode == MovementMode.FLOAT:
                    # 當前在浮空，嘗試找浮空版本
                    alternative = name[:-2] + '_f'
                    if self.ani_module and hasattr(self.ani_module, 'manager'):
                        if alternative in self.ani_module.manager.clips:
                            debug_log(2, f"[{self.module_id}] 動畫轉換: {name} -> {alternative} (浮空)")
                            return alternative
                    # 沒有浮空版本，使用原動畫
                    return name
                return name
            else:
                # 通用動畫：適用所有模式
                return name
                
        except Exception as e:
            error_log(f"[{self.module_id}] 轉換動畫名稱失敗: {e}")
            return name
    
    def _get_compatible_animation_from_list(self, anim_list: List[str]) -> Optional[str]:
        """從動畫列表中選擇與當前移動模式相容的動畫"""
        if not anim_list:
            return None
        
        # 優先選擇符合當前模式的動畫
        for anim_name in anim_list:
            converted = self._convert_animation_for_movement_mode(anim_name)
            if converted:
                return converted
        
        # 如果沒有相容的，返回第一個並轉換
        return self._convert_animation_for_movement_mode(anim_list[0])

    def _trigger_tease_animation(self) -> None:
        """
        觸發 tease 捉弄動畫
        
        動畫選擇只由 mood 決定：
        - mood > 0: tease2_f (活潑版)
        - mood ≤ 0: tease_f (基本版)
        """
        try:
            # 強制結束拖曳狀態（如果正在拖曳）
            if self.is_being_dragged:
                self.is_being_dragged = False
                self.resume_movement(self.DRAG_PAUSE_REASON)
                debug_log(2, f"[{self.module_id}] Tease 觸發，強制結束拖曳")
            
            # 標記開始播放 tease
            self._tease_tracker.start_tease()
            
            # 獲取 mood 來決定動畫，同時更新 status_manager
            mood = 0
            try:
                from core.status_manager import status_manager
                mood = status_manager.status.mood
                
                # 捉弄互動會降低 mood（被捉弄不開心）但緩解 boredom（有趣的互動）
                status_manager.update_mood(-0.1, "使用者捉弄互動")
                status_manager.update_boredom(-0.2, "捉弄互動緩解無聊")
                debug_log(1, f"[{self.module_id}] Tease 互動影響系統數值: mood-=0.1, boredom-=0.2")
            except Exception as e:
                debug_log(2, f"[{self.module_id}] 無法獲取/更新 status_manager: {e}")
            
            # 決定使用哪個 tease 動畫（只看 mood）
            if mood > 0:
                # 正面情緒 -> tease2_f
                tease_anim = self.anim_query.get_tease_animation(variant=2)
                debug_log(1, f"[{self.module_id}] 觸發 tease2_f (互動次數達標, mood={mood:.2f})")
            else:
                # 負面/中性情緒 -> tease_f
                tease_anim = self.anim_query.get_tease_animation(variant=1)
                debug_log(1, f"[{self.module_id}] 觸發 tease_f (互動次數達標, mood={mood:.2f})")
            
            # 播放 tease 動畫，完成後恢復 idle
            idle_anim = self.anim_query.get_idle_animation_for_mode(
                is_ground=(self.movement_mode == MovementMode.GROUND)
            )
            
            # 播放 tease 並設置回調
            self._trigger_anim(
                tease_anim,
                {
                    "loop": False,
                    "next_anim": idle_anim,
                    "next_params": {"loop": True}
                },
                source="tease_system"
            )
            
            # 暫停移動直到動畫完成
            self.pause_movement("tease_animation")
            
            # 註冊動畫完成回調來清理 tease 狀態
            def on_tease_complete():
                self._tease_tracker.end_tease()
                self.resume_movement("tease_animation")
                # 恢復正常行為
                self._switch_behavior(BehaviorState.IDLE)
                debug_log(2, f"[{self.module_id}] Tease 動畫完成，恢復正常")
            
            # 等待動畫完成（假設 tease 動畫約 2-3 秒）
            self._await_animation(tease_anim, timeout=5.0, follow=on_tease_complete)
            
        except Exception as e:
            error_log(f"[{self.module_id}] 觸發 tease 動畫失敗: {e}")
            # 發生錯誤時清理狀態
            self._tease_tracker.end_tease()
            self.resume_movement("tease_animation")

    # ========= 暫停/恢復 =========

    def pause_movement(self, reason: str = ""):
        self.pause_reasons.add(reason or "")
        self.movement_paused = True
        self.pause_reason = ", ".join(sorted(self.pause_reasons))

    def resume_movement(self, reason: Optional[str] = None):
        if reason:
            self.pause_reasons.discard(reason)
        else:
            self.pause_reasons.clear()
        if self.pause_reasons:
            self.movement_paused = True
            self.pause_reason = ", ".join(sorted(self.pause_reasons))
        else:
            self.movement_paused = False
            self.pause_reason = ""

    # ========= 其他幫手程式 =========

    def _await_animation(self, name: str, timeout: float, follow: Optional[Callable[[], None]] = None):
        now = time.time()
        self._awaiting_anim = name
        self._await_deadline = now + max(timeout, 0.2)
        self._await_follow = follow
        # 同步鎖住移動（物理 tick 會 early return）
        self.movement_locked_until = self._await_deadline
        self.pause_movement(self.WAIT_ANIM_REASON)
        debug_log(2, f"[{self.module_id}] 等待動畫完成: {name} (<= {timeout:.2f}s)")

    def attach_ani(self, ani) -> None:
        """注入 ANI 模組並註冊事件回呼。"""
        self.ani_module = ani
        # 同步更新動畫查詢輔助器的 ANI 模組引用
        self.anim_query.ani_module = ani
        try:
            if hasattr(ani, "add_start_callback"):
                ani.add_start_callback(self._on_ani_start)
            if hasattr(ani, "add_finish_callback"):
                ani.add_finish_callback(self._on_ani_finish)
            debug_log(2, f"[{self.module_id}] 已注入 ANI 並完成事件註冊")
            
            # === 創建 Qt 橋接器（線程安全的動畫觸發） ===
            if PYQT5 and not self._qt_bridge:
                try:
                    from .qt_bridge import MovQtBridge
                    from PyQt5.QtWidgets import QApplication
                    app = QApplication.instance()
                    if app:
                        self._qt_bridge = MovQtBridge(self.ani_module, parent=app)
                        info_log(f"[{self.module_id}] Qt 橋接器已創建（線程安全動畫觸發）")
                    else:
                        debug_log(2, f"[{self.module_id}] QApplication 不可用，跳過 Qt 橋接器創建")
                except Exception as e:
                    error_log(f"[{self.module_id}] 創建 Qt 橋接器失敗: {e}")
                    self._qt_bridge = None
            
        except Exception as e:
            error_log(f"[{self.module_id}] 注入 ANI 失敗: {e}")
    
    def handle_cursor_tracking_event(self, event_data: dict):
        """
        處理滑鼠追蹤事件（由 UI 模組發送）
        
        事件類型：
        - "cursor_near": 滑鼠靠近角色
        - "cursor_far": 滑鼠遠離角色
        - "cursor_angle": 滑鼠角度更新（用於轉頭動畫）
        
        Args:
            event_data: {
                "type": "cursor_near" | "cursor_far" | "cursor_angle",
                "angle": float (僅 cursor_angle),
                "distance": float (可選)
            }
        """
        try:
            # 🔧 出入場期間禁用所有 handler
            if self._is_entering or self._is_leaving:
                debug_log(3, f"[{self.module_id}] 出入場期間禁用滑鼠追蹤")
                return
            
            event_type = event_data.get("type")
            
            if event_type == "cursor_near":
                # 滑鼠靠近，暫停移動並播放轉頭動畫
                self._cursor_tracking_handler.on_cursor_near(event_data)
                
            elif event_type == "cursor_far":
                # 滑鼠遠離，恢復移動並停止轉頭動畫
                self._cursor_tracking_handler.on_cursor_far(event_data)
                
            elif event_type == "cursor_angle":
                # 更新轉頭動畫幀
                angle = event_data.get("angle", 0)
                self._cursor_tracking_handler.update_turn_head_angle(angle)
                
        except Exception as e:
            error_log(f"[{self.module_id}] 處理滑鼠追蹤事件失敗: {e}")

    def _on_ani_start(self, name: str):
        debug_log(3, f"[{self.module_id}] ANI start: {name}")
        
        # 🎯 更新當前動畫的 offset_x 和 offset_y（用於位置補償）
        if self.ani_module and hasattr(self.ani_module, 'get_clip_info'):
            clip_info = self.ani_module.get_clip_info(name)
            if clip_info:
                self._current_animation_offset_x = clip_info.get('offset_x', 0)
                self._current_animation_offset_y = clip_info.get('offset_y', 0)
                if self._current_animation_offset_x != 0 or self._current_animation_offset_y != 0:
                    debug_log(3, f"[{self.module_id}] 動畫 {name} 開始，offset_x={self._current_animation_offset_x}, offset_y={self._current_animation_offset_y}")
    
    def _infer_animation_priority(self, params: Dict[str, Any]) -> AnimationPriority:
        """
        根據當前狀態和參數推斷動畫優先度
        
        優先度推斷規則：
        1. immediate_interrupt=True → FORCE_OVERRIDE
        2. SYSTEM_CYCLE 狀態 → SYSTEM_CYCLE
        3. 拖曳或投擲移動模式 → USER_INTERACTION
        4. Tease 狀態 → TEASE
        5. TRANSITION 行為 → TRANSITION
        6. NORMAL_MOVE 行為 → MOVEMENT
        7. SPECIAL_MOVE 行為 → SPECIAL_MOVE
        8. 滑鼠追蹤靜態幀 → CURSOR_TRACKING
        9. 其他 → IDLE_ANIMATION (預設)
        """
        # 強制覆蓋
        if params.get("immediate_interrupt", False):
            return AnimationPriority.FORCE_OVERRIDE
        
        # 根據行為狀態推斷
        if self.current_behavior_state == BehaviorState.SYSTEM_CYCLE:
            return AnimationPriority.SYSTEM_CYCLE
        elif self.movement_mode == MovementMode.DRAGGING or self.is_being_dragged:
            return AnimationPriority.USER_INTERACTION
        elif self.movement_mode == MovementMode.THROWN:
            return AnimationPriority.USER_INTERACTION
        elif self.current_behavior_state == BehaviorState.TRANSITION:
            return AnimationPriority.TRANSITION
        elif self.current_behavior_state == BehaviorState.NORMAL_MOVE:
            return AnimationPriority.MOVEMENT
        elif self.current_behavior_state == BehaviorState.SPECIAL_MOVE:
            return AnimationPriority.SPECIAL_MOVE
        elif self.current_behavior_state == BehaviorState.IDLE:
            # 檢查是否為 tease
            if self._tease_tracker.is_teasing():
                return AnimationPriority.TEASE
            # 檢查是否為滑鼠追蹤靜態幀
            if self.ani_module and hasattr(self.ani_module, 'manager'):
                if getattr(self.ani_module.manager, 'static_frame_mode', False):
                    return AnimationPriority.CURSOR_TRACKING
            # 預設 IDLE
            return AnimationPriority.IDLE_ANIMATION
        
        # 預設為 IDLE 優先度
        return AnimationPriority.IDLE_ANIMATION

    def _on_ani_finish(self, finished_name: str):
        # 通知優先度管理器動畫完成
        self._animation_priority.on_animation_finished(finished_name)
        
        # 檢查是否是投擲飛行動畫完成 (swoop_left/right，不含 _end)
        if hasattr(self, '_throw_handler') and finished_name in ['swoop_left', 'swoop_right', 'struggle']:
            if self._throw_handler.is_in_throw_animation:
                # 檢查是否已經著地（速度接近零且在地面附近）
                is_landed = False
                if hasattr(self, '_physics_handler'):
                    current_vy = getattr(self._physics_handler, 'velocity_y', 0)
                    current_y = getattr(self, 'current_position_y', 0)
                    ground_level = getattr(self, '_ground_level', 0)
                    # 只有在速度很小且接近地面時才觸發落地動畫
                    is_landed = abs(current_vy) < 2.0 and abs(current_y - ground_level) < 10
                
                if is_landed:
                    debug_log(1, f"[{self.module_id}] 投擲飛行動畫完成且已著地: {finished_name}，觸發落地動畫")
                    # 觸發落地動畫 (swoop_*_end)
                    self._throw_handler.handle_throw_landing()
                else:
                    debug_log(2, f"[{self.module_id}] 投擲飛行動畫完成但仍在空中: {finished_name}，等待著地")
                # 動畫序列繼續，不切換狀態
                return
        
        # 檢查是否是投擲落地動畫完成
        if hasattr(self, '_throw_handler') and finished_name.startswith('swoop_') and finished_name.endswith('_end'):
            debug_log(1, f"[{self.module_id}] 投擲落地動畫完成: {finished_name}")
            self._throw_handler.on_throw_animation_complete()
            # 切換到 IDLE 狀態
            idle_anim = self.anim_query.get_idle_animation_for_mode(is_ground=True)
            self._trigger_anim(idle_anim, {"loop": True}, source="throw_handler")
            self._switch_behavior(BehaviorState.IDLE)
        
        # �🌙 檢查是否是睡眠轉換動畫完成 (g_to_l)
        if finished_name == 'g_to_l':
            debug_log(2, f"[{self.module_id}] 睡眠轉換動畫完成: {finished_name}")
            if self.current_behavior_state == BehaviorState.SLEEPING:
                # 自動播放 sleep_l 循環動畫
                self._trigger_anim('sleep_l', {
                    'loop': True,
                    'force_restart': True
                }, source='sleep_behavior', priority=AnimationPriority.SYSTEM_CYCLE)
                debug_log(2, f"[{self.module_id}] 開始播放睡眠循環動畫: sleep_l")
                return
        
        # ☀️ 檢查是否是喚醒動畫完成 (l_to_g)
        if finished_name == 'l_to_g' and self._pending_wake_transition:
            debug_log(2, f"[{self.module_id}] 喚醒轉換動畫完成: {finished_name}")
            
            # l_to_g 完成後立即切換到 IDLE，不需要再等待 WAKE_READY
            # WAKE_READY 已經在播放 l_to_g 之前就收到了
            info_log(f"[{self.module_id}] ✅ 喚醒動畫完成，切換回 IDLE")
            self._pending_wake_transition = False
            self._wake_ready = False  # 重置標記
            self.movement_locked_until = 0  # 解鎖移動
            
            # 切換回 IDLE 行為
            self._switch_behavior(BehaviorState.IDLE)
            # 播放 ground 模式的 idle 動畫
            idle_anim = self.anim_query.get_idle_animation_for_mode(is_ground=True) if self.anim_query else "stand_idle_g"
            self._trigger_anim(idle_anim, {
                'loop': True,
                'force_restart': True
            }, source='wake_complete', priority=AnimationPriority.IDLE_ANIMATION)
            info_log(f"[{self.module_id}] ☀️ 喚醒完成，恢復正常行為")
            return
        
        # 檢查是否是轉場動畫完成（f_to_g 或 g_to_f）
        if finished_name in ('f_to_g', 'g_to_f'):
            debug_log(2, f"[{self.module_id}] 轉場動畫完成: {finished_name}")
            
            # 設置轉場動畫完成標誌（供 TransitionBehavior 檢查）
            self._transition_animation_finished = True
            
            # 🌙 如果是為了睡眠而執行的 f_to_g，繼續執行睡眠轉換
            if finished_name == 'f_to_g':
                if hasattr(self, '_pending_sleep_transition') and self._pending_sleep_transition:
                    info_log(f"[{self.module_id}] f_to_g 完成，繼續執行睡眠轉換")
                    self._pending_sleep_transition = False
                    # 確保已經在地面
                    if self.movement_mode != MovementMode.GROUND:
                        info_log(f"[{self.module_id}] 強制切換到 GROUND 模式")
                        self.movement_mode = MovementMode.GROUND
                        ground_y = self._ground_y()
                        self.position.y = ground_y
                    self._execute_sleep_transition()
                    return
            
            # 如果當前已經在 IDLE 狀態（由 TransitionBehavior 切換），觸發相應的 idle 動畫
            if self.current_behavior_state == BehaviorState.IDLE:
                is_ground = (self.movement_mode == MovementMode.GROUND)
                idle_anim = self.anim_query.get_idle_animation_for_mode(is_ground) if self.anim_query else (
                    "stand_idle_g" if is_ground else "smile_idle_f"
                )
                # 明確指定 IDLE_ANIMATION 優先度，確保可以播放
                self._trigger_anim(idle_anim, {
                    "loop": True,
                    "force_restart": True
                }, source="transition_complete", priority=AnimationPriority.IDLE_ANIMATION)
                debug_log(2, f"[{self.module_id}] 轉場完成後觸發 idle 動畫: {idle_anim}")
        
        # 若有指定等待且名稱吻合才解除
        if self._awaiting_anim and finished_name == self._awaiting_anim:
            debug_log(2, f"[{self.module_id}] 收到動畫完成: {finished_name}，解除等待")
            self._awaiting_anim = None
            self._await_deadline = 0.0
            self.movement_locked_until = 0.0
            self.resume_movement(self.WAIT_ANIM_REASON)
            follow = self._await_follow
            self._await_follow = None
            if follow:
                try: follow()
                except Exception as e: error_log(f"[{self.module_id}] 等待後續執行失敗: {e}")
        
        # 🎬 如果當前在 IDLE 狀態且動畫完成，自動恢復 idle 動畫
        # （處理彩蛋動畫等非循環動畫完成後的情況）
        if self.current_behavior_state == BehaviorState.IDLE:
            # 檢查是否是彩蛋動畫或其他特殊動畫（通常包含特定關鍵字）
            easter_egg_keywords = ['dance', 'chilling', 'angry', 'yawn']
            is_special_anim = any(keyword in finished_name.lower() for keyword in easter_egg_keywords)
            
            if is_special_anim:
                debug_log(2, f"[{self.module_id}] 彩蛋/特殊動畫 {finished_name} 完成，恢復 idle 動畫")
                # 獲取適當的 idle 動畫
                is_ground = (self.movement_mode == MovementMode.GROUND)
                idle_anim = self.anim_query.get_idle_animation_for_mode(is_ground) if self.anim_query else (
                    "stand_idle_g" if is_ground else "smile_idle_f"
                )
                # 觸發恢復動畫，明確指定 IDLE_ANIMATION 優先度
                self._trigger_anim(idle_anim, {
                    "loop": True,
                    "force_restart": True
                }, source="auto_recovery", priority=AnimationPriority.IDLE_ANIMATION)

    def _apply_config(self, cfg: Dict):
        # physics
        phys = cfg.get("physics", {})
        self.physics.gravity = phys.get("gravity", self.physics.gravity)
        self.physics.damping = phys.get("damping", self.physics.damping)
        self.GROUND_OFFSET = phys.get("ground_offset", self.GROUND_OFFSET)

        # movement
        mov = cfg.get("movement", {})
        self.GROUND_SPEED     = float(mov.get("ground_speed",     self.GROUND_SPEED))
        self.FLOAT_MIN_SPEED  = float(mov.get("float_speed_min",  self.FLOAT_MIN_SPEED))
        self.FLOAT_MAX_SPEED  = float(mov.get("float_speed_max",  self.FLOAT_MAX_SPEED))
        self._approach_k      = float(mov.get("approach_factor",  self._approach_k))
        self.target_reach_threshold = float(mov.get("target_reach_threshold", self.target_reach_threshold))

        # boundaries
        bnd = cfg.get("boundaries", {})
        self.screen_padding   = int(bnd.get("screen_padding", self.screen_padding))
        self.keep_on_screen   = bool(bnd.get("keep_on_screen", self.keep_on_screen))
        self.bounce_off_edges = bool(bnd.get("bounce_off_edges", self.bounce_off_edges))

        # state machine（如果沒有提供，沿用預設）
        sm = cfg.get("state_machine", {})
        idle = sm.get("idle", {})
        # 兼容舊鍵：用 behavior.mode_switch_* 當 idle min/max
        legacy_behavior = cfg.get("behavior", {})
        self.sm.idle_cfg.min_duration = float(idle.get(
            "min_duration",
            legacy_behavior.get("mode_switch_min", self.sm.idle_cfg.min_duration)
        ))
        self.sm.idle_cfg.max_duration = float(idle.get(
            "max_duration",
            legacy_behavior.get("mode_switch_max", self.sm.idle_cfg.max_duration)
        ))
        self.sm.idle_cfg.tick_chance  = float(idle.get("tick_chance", self.sm.idle_cfg.tick_chance))
        if "transition_duration" in sm:
            self.sm.transition_duration = float(sm["transition_duration"])

        # 權重
        wg = sm.get("weights_ground")
        if isinstance(wg, dict):
            # 轉換字符串鍵為 BehaviorState 枚舉
            converted_wg = {}
            for key, value in wg.items():
                if isinstance(key, str):
                    try:
                        enum_key = BehaviorState[key]
                        converted_wg[enum_key] = float(value)
                    except (KeyError, ValueError) as e:
                        error_log(f"[{self.module_id}] 無效的 ground 權重鍵: {key}, 錯誤: {e}")
                else:
                    converted_wg[key] = float(value)
            self.sm.weights_ground.update(converted_wg)
            
        wf = sm.get("weights_float")
        if isinstance(wf, dict):
            # 轉換字符串鍵為 BehaviorState 枚舉
            converted_wf = {}
            for key, value in wf.items():
                if isinstance(key, str):
                    try:
                        enum_key = BehaviorState[key]
                        converted_wf[enum_key] = float(value)
                    except (KeyError, ValueError) as e:
                        error_log(f"[{self.module_id}] 無效的 float 權重鍵: {key}, 錯誤: {e}")
                else:
                    converted_wf[key] = float(value)
            self.sm.weights_float.update(converted_wf)

        # 計時器
        timers = cfg.get("timers", {})
        self.config["behavior_interval_ms"] = int(timers.get("behavior_interval_ms", self.config.get("behavior_interval_ms", 100)))
        self.config["movement_interval_ms"] = int(timers.get("movement_interval_ms", self.config.get("movement_interval_ms", 16)))

    def _reload_from_user_settings(self, key_path: str, value: Any) -> bool:
        """
        從 user_settings.yaml 重載設定
        
        Args:
            key_path: 設定路徑 (如 "behavior.movement.boundary_mode")
            value: 新值
            
        Returns:
            是否成功
        """
        try:
            info_log(f"[{self.module_id}] 🔄 重載使用者設定: {key_path} = {value}")
            
            # 根據設定路徑處理不同的參數
            if key_path == "behavior.movement.boundary_mode":
                # 邊界模式
                old_mode = self.boundary_mode
                self.boundary_mode = value
                info_log(f"[{self.module_id}] 邊界模式已更新: {old_mode} → {value}")
                
            elif key_path == "behavior.movement.enable_throw_behavior":
                # 投擲行為開關
                if hasattr(self, '_throw_handler') and self._throw_handler:
                    # ThrowHandler 沒有 enable/disable，但我們可以透過修改閾值來實現
                    if not value:
                        # 禁用：設置極高的閾值，實際上不會觸發
                        self._throw_handler.throw_threshold_speed = 999999.0
                        info_log(f"[{self.module_id}] 投擲行為已禁用")
                    else:
                        # 啟用：恢復預設閾值
                        config_threshold = float(self.config.get("throw_threshold_speed", 800.0))
                        self._throw_handler.throw_threshold_speed = config_threshold
                        info_log(f"[{self.module_id}] 投擲行為已啟用 (閾值={config_threshold})")
                        
            elif key_path == "behavior.movement.max_throw_speed":
                # 最大投擲速度
                if hasattr(self, '_throw_handler') and self._throw_handler:
                    old_speed = self._throw_handler.max_throw_speed
                    self._throw_handler.max_throw_speed = float(value)
                    info_log(f"[{self.module_id}] 最大投擲速度已更新: {old_speed} → {value}")
                    
            elif key_path == "behavior.movement.enable_cursor_tracking":
                # 滑鼠追蹤開關
                if hasattr(self, '_cursor_tracking_handler') and self._cursor_tracking_handler:
                    # CursorTrackingHandler 透過事件驅動，直接記錄開關狀態
                    self._cursor_tracking_enabled = bool(value)
                    info_log(f"[{self.module_id}] 滑鼠追蹤已{'啟用' if value else '禁用'}")
                    # 如果禁用，停止當前追蹤
                    if not value and hasattr(self._cursor_tracking_handler, '_is_turning_head'):
                        if self._cursor_tracking_handler._is_turning_head:
                            self._cursor_tracking_handler._stop_tracking(restore_idle=True)
                            
            elif key_path == "behavior.movement.movement_smoothing":
                # 移動平滑化
                old_smoothing = self._smoothing_enabled
                self._smoothing_enabled = bool(value)
                info_log(f"[{self.module_id}] 移動平滑化已更新: {old_smoothing} → {value}")
                # 重置平滑速度緩衝
                self._smooth_velocity = Velocity(0.0, 0.0)
                self._pause_velocity_buffer = Velocity(0.0, 0.0)
                
            elif key_path == "behavior.movement.ground_friction":
                # 地面摩擦係數
                if hasattr(self, 'physics') and self.physics:
                    old_friction = self.physics.ground_friction
                    self.physics.ground_friction = float(value)
                    info_log(f"[{self.module_id}] 地面摩擦係數已更新: {old_friction:.3f} → {value:.3f}")
            
            else:
                debug_log(2, f"[{self.module_id}] 未處理的設定路徑: {key_path}")
                return False
            
            return True
            
        except Exception as e:
            error_log(f"[{self.module_id}] 重載使用者設定失敗: {e}")
            import traceback
            error_log(traceback.format_exc())
            return False

    def shutdown(self):
        """關閉移動模組，停止所有計時器和清理資源"""
        info_log(f"[{self.module_id}] 開始關閉移動模組")
        
        # 停止滑鼠追蹤處理器
        try:
            if hasattr(self, '_cursor_tracking_handler') and self._cursor_tracking_handler:
                self._cursor_tracking_handler.shutdown()
                info_log(f"[{self.module_id}] 滑鼠追蹤處理器已停止")
        except Exception as e:
            error_log(f"[{self.module_id}] 停止滑鼠追蹤處理器失敗: {e}")
        
        # 停止行為計時器
        try:
            if hasattr(self, 'behavior_timer') and self.behavior_timer:
                self.behavior_timer.stop()
                self.behavior_timer.deleteLater() if hasattr(self.behavior_timer, 'deleteLater') else None
                self.behavior_timer = None
                info_log(f"[{self.module_id}] 行為計時器已停止並清理")
        except Exception as e:
            error_log(f"[{self.module_id}] 停止行為計時器失敗: {e}")
        
        # 停止移動計時器
        try:
            if hasattr(self, 'movement_timer') and self.movement_timer:
                self.movement_timer.stop()
                self.movement_timer.deleteLater() if hasattr(self.movement_timer, 'deleteLater') else None
                self.movement_timer = None
                info_log(f"[{self.module_id}] 移動計時器已停止並清理")
        except Exception as e:
            error_log(f"[{self.module_id}] 停止移動計時器失敗: {e}")
        
        # 清理信號回調
        try:
            if hasattr(self, 'signals') and self.signals:
                if hasattr(self.signals, 'remove_timer_callback'):
                    self.signals.remove_timer_callback("mov_behavior")
                    self.signals.remove_timer_callback("mov_movement")
                    info_log(f"[{self.module_id}] 信號回調已清理")
                else:
                    info_log(f"[{self.module_id}] 信號系統無remove_timer_callback方法")
        except Exception as e:
            error_log(f"[{self.module_id}] 清理信號回調失敗: {e}")
        
        # 清理ANI模組引用
        try:
            if hasattr(self, 'ani_module'):
                self.ani_module = None
                info_log(f"[{self.module_id}] ANI模組引用已清理")
        except Exception as e:
            error_log(f"[{self.module_id}] 清理ANI模組引用失敗: {e}")
        
        return super().shutdown()
    
    # ========= ON_CALL 動畫 =========
    
    def trigger_on_call_animation(self, mode: str = "vad"):
        """
        觸發 on_call 動畫 - 設置 ON_CALL 標記並播放 notice 動畫
        
        Args:
            mode: on_call 模式 ("vad" 或 "text")
        """
        try:
            from modules.mov_module.core.animation_priority import AnimationPriority
            
            # 設置 ON_CALL 標記（停止移動和滑鼠追蹤）
            self._on_call_active = True
            debug_log(2, f"[{self.module_id}] 已進入 ON_CALL 模式")
            
            # 根據當前浮空/落地狀態選擇適當的 notice 動畫
            # notice_f: 浮空狀態, notice_g: 落地狀態
            is_floating = (self.movement_mode == MovementMode.FLOAT)
            animation_name = "notice_f" if is_floating else "notice_g"
            
            if self.anim_query and self.anim_query.animation_exists(animation_name):
                self._trigger_anim(
                    animation_name,
                    params={
                        "loop": True,  # 循環播放直到 on_call 結束
                        "await_finish": False
                    },
                    source="on_call",
                    priority=AnimationPriority.USER_INTERACTION  # 使用者交互優先度
                )
                debug_log(2, f"[{self.module_id}] ON_CALL notice 動畫已啟動 ({animation_name}, 模式: {mode})")
            else:
                debug_log(1, f"[{self.module_id}] {animation_name} 動畫不存在")
        
        except Exception as e:
            error_log(f"[{self.module_id}] 觸發 ON_CALL 動畫失敗: {e}")
    
    def end_on_call_animation(self, mode: str = "vad"):
        """
        結束 on_call 動畫 - 清除 ON_CALL 標記、停止循環動畫並清除優先度
        
        Args:
            mode: on_call 模式 ("vad" 或 "text")
        """
        try:
            # 清除 ON_CALL 標記
            self._on_call_active = False
            debug_log(2, f"[{self.module_id}] 已離開 ON_CALL 模式")
            
            # 先停止當前動畫（notice_f 是循環播放，需要主動停止）
            if self.ani_module:
                try:
                    self.ani_module.stop()
                    debug_log(2, f"[{self.module_id}] 已停止 notice_f 循環動畫")
                except Exception as stop_err:
                    debug_log(2, f"[{self.module_id}] 停止動畫異常: {stop_err}")
            
            # 清除動畫優先度鎖定（讓行為狀態機自然接管）
            if hasattr(self, '_animation_priority'):
                self._animation_priority.reset()
                debug_log(2, f"[{self.module_id}] 已清除 ON_CALL 動畫優先度鎖定")
            
            # 通知動畫完成（清除 notice_f 的優先度狀態）
            if hasattr(self, '_animation_priority'):
                self._animation_priority.on_animation_finished("notice_f")
            
            debug_log(2, f"[{self.module_id}] ON_CALL 已完全結束，行為狀態機已恢復 (模式: {mode})")
            
        except Exception as e:
            error_log(f"[{self.module_id}] 結束 ON_CALL 動畫失敗: {e}")
    
    def get_performance_window(self) -> dict:
        """獲取效能數據窗口（包含 MOV 特定指標）"""
        window = super().get_performance_window()
        window['total_distance_moved'] = self.total_distance_moved
        window['total_movements'] = self.total_movements
        window['movement_type_distribution'] = self.movement_type_stats.copy()
        window['avg_distance_per_movement'] = (
            self.total_distance_moved / self.total_movements
            if self.total_movements > 0 else 0.0
        )
        return window


