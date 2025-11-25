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

from core.bases.frontend_base import BaseFrontendModule, FrontendModuleType, UIEventType
from core.states.state_manager import UEPState

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

# 拆出核心/行為
try:
    from .core.position import Position, Velocity
    from .core.physics import PhysicsEngine
    from .core.state_machine import MovementStateMachine, MovementMode, BehaviorState
    from .behaviors.base_behavior import BehaviorContext, BehaviorFactory
except Exception:
    from core.position import Position, Velocity  # type: ignore
    from core.physics import PhysicsEngine  # type: ignore
    from core.state_machine import MovementStateMachine, MovementMode, BehaviorState  # type: ignore
    from behaviors.base_behavior import BehaviorContext, BehaviorFactory  # type: ignore

# 日誌
from utils.debug_helper import debug_log, info_log, error_log


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
        self.physics = PhysicsEngine(
            gravity=self.config.get("gravity", 0.8),
            damping=self.config.get("damping", 0.978),
            ground_friction=self.config.get("ground_friction", 0.95),
            air_resistance=self.config.get("air_resistance", 0.99),
        )
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

        # --- 控制旗標 ---
        self.is_being_dragged = False
        self.movement_paused = False
        self.pause_reasons: set[str] = set()
        self.pause_reason = ""
        
        # --- 拖曳追蹤 ---
        self._drag_start_position: Optional[Position] = None
        self._drag_start_mode: Optional[MovementMode] = None  # 記錄拖曳前的模式

        # --- 轉場共享狀態（交給 TransitionBehavior 用） ---
        self.transition_start_time: Optional[float] = None
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


        # --- 停滯保護 ---
        self.last_movement_time = time.time()
        self.max_idle_time = float(self.config.get("max_idle_time", 5.0))

        # --- 計時器 ---
        self.movement_timer: Optional[QTimer] = None
        self.behavior_timer: Optional[QTimer] = None

        # --- 其他設定 ---
        self._approach_k = 0.12                  # 速度趨近係數（預設）
        self.screen_padding = 50                 # 目標夾取安全邊距
        self.keep_on_screen = True
        self.bounce_off_edges = False
        self._apply_config(self.config)
        
        # --- 狀態動畫系統 ---
        self._current_layer: Optional[str] = None  # "input", "processing", "output"
        self._current_system_state: UEPState = UEPState.IDLE
        self._state_animation_config: Optional[Dict] = None

        info_log(f"[{self.module_id}] MOV 初始化完成")

    # ========= 前端生命週期 =========

    def initialize_frontend(self) -> bool:
        """初始化計時器、事件與初始行為"""
        debug_log(1, "前端 - MOV 初始化中")
        try:
            # 計時器 → 交給 BaseFrontendModule.signals 轉發
            if PYQT5:
                self.signals.add_timer_callback("mov_behavior", self._tick_behavior)
                self.signals.add_timer_callback("mov_movement", self._tick_movement)

                self.behavior_timer = QTimer()
                self.behavior_timer.timeout.connect(lambda: self.signals.timer_timeout("mov_behavior"))
                self.behavior_timer.start(int(self.config.get("behavior_interval_ms", 100)))

                self.movement_timer = QTimer()
                self.movement_timer.timeout.connect(lambda: self.signals.timer_timeout("mov_movement"))
                self.movement_timer.start(int(self.config.get("movement_interval_ms", 16)))

            # 事件
            self._register_handlers()

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

            # 初始目標/位置
            self._initialize_position()

            # 進入第一個行為
            debug_log(1, f"[{self.module_id}] 初始行為: {self.current_behavior_state.value}")
            self._enter_behavior(self.current_behavior_state)
            
            # 訂閱層級事件以驅動動畫
            self._subscribe_to_layer_events()
            
            # 載入狀態動畫配置
            self._state_animation_config = self._load_state_animation_config()

            return True
        except Exception as e:
            error_log(f"[{self.module_id}] 前端初始化失敗: {e}")
            return False

    def handle_frontend_request(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """對外 API（必要時可擴充）"""
        try:
            cmd = data.get("command")
            if cmd == "get_status":
                return self._api_get_status()
            if cmd == "set_position":
                return self._api_set_position(data)
            if cmd == "set_velocity":
                return self._api_set_velocity(data)
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
                self._trigger_anim(name, params)
                return {"success": True, "animation": name}
            return {"error": f"未知命令: {cmd}"}
        except Exception as e:
            error_log(f"[{self.module_id}] 請求處理錯誤: {e}")
            return {"error": str(e)}

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
        now = time.time()

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
        if self.movement_paused and not self.is_being_dragged:
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
            now=now,
            transition_start_time=self.transition_start_time,
            movement_locked_until=self.movement_locked_until,
            previous_state=self.previous_behavior_state,
        )

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
        self.movement_locked_until = ctx.movement_locked_until
        self.movement_target = ctx.movement_target
        self.target_reach_threshold = ctx.target_reach_threshold
        self.target_reached = ctx.target_reached

        if next_state is not None and next_state != self.current_behavior_state:
            debug_log(1, f"[{self.module_id}] 行為建議切換: {self.current_behavior_state.value} -> {next_state.value}")
            self._switch_behavior(next_state)
        elif next_state is not None:
            debug_log(3, f"[{self.module_id}] 行為保持: {self.current_behavior_state.value}")
        else:
            debug_log(3, f"[{self.module_id}] 行為無變化: {self.current_behavior_state.value}")

    def _tick_movement(self):
        now = time.time()
        if self.is_being_dragged or self.movement_paused:
            return
        # 轉場期間仍然允許移動，但其他動畫等待期間不允許
        if now < self.movement_locked_until and self.current_behavior_state != BehaviorState.TRANSITION:
            return

        prev_x, prev_y = self.position.x, self.position.y
        gy = self._ground_y()

        # 模式別物理 - 拖曳時不處理物理
        if self.movement_mode == MovementMode.GROUND:
            # 貼地 - 但拖曳時不強制鎖定在地面
            if not self.is_being_dragged:
                self.position.y = gy
            self.velocity = self.physics.step_ground(self.velocity)
        elif self.movement_mode == MovementMode.FLOAT:
            self.velocity = self.physics.step_float(self.velocity)
        elif self.movement_mode == MovementMode.DRAGGING:
            # 拖曳模式：不處理物理，位置由drag_move事件控制
            self.velocity = Velocity(0.0, 0.0)
            self.target_velocity = Velocity(0.0, 0.0)
        elif self.movement_mode == MovementMode.THROWN:
            grounded = abs(self.position.y - gy) < 5
            self.velocity = self.physics.step_thrown(self.velocity, grounded)
            if self.position.y >= gy:
                self.position.y = gy
                if abs(self.velocity.y) > 2:
                    self.velocity.y = -self.velocity.y * 0.4
                else:
                    self.velocity.y = 0.0
                    self.movement_mode = MovementMode.GROUND

        # 速度趨近 target_velocity（拖曳時不處理）
        if not self.is_being_dragged:
            self.velocity.x += (self.target_velocity.x - self.velocity.x) * self._approach_k
            self.velocity.y += (self.target_velocity.y - self.velocity.y) * self._approach_k

        # 位置整合 + 邊界處理（拖曳時不處理）
        if not self.is_being_dragged:
            self.position.x += self.velocity.x
            self.position.y += self.velocity.y
            self._check_boundaries()

        moved = (abs(self.position.x - prev_x) + abs(self.position.y - prev_y)) > 0.05
        if moved:
            self.last_movement_time = now
        self._emit_position()

        # 停滯保護（可視需要）- 但排除轉場狀態
        if (now - self.last_movement_time > self.max_idle_time and 
            self.current_behavior_state != BehaviorState.IDLE and 
            self.current_behavior_state != BehaviorState.TRANSITION):
            debug_log(2, f"[{self.module_id}] 檢測到移動停滯，強制切換狀態")
            self._switch_behavior(BehaviorState.IDLE)

    # ========= 行為切換 =========

    def _enter_behavior(self, state: BehaviorState):
        """呼叫 on_enter 並更新 current_behavior_state"""
        self.previous_behavior_state = self.current_behavior_state  # 記錄前一個狀態
        self.current_behavior_state = state
        self.current_behavior = BehaviorFactory.create(state)

        # 建 ctx 給 on_enter
        now = time.time()
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
            now=now,
            transition_start_time=self.transition_start_time,
            movement_locked_until=self.movement_locked_until,
            previous_state=self.previous_behavior_state,
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
                        now=time.time(),
                        transition_start_time=self.transition_start_time,
                        movement_locked_until=self.movement_locked_until,
                        previous_state=self.previous_behavior_state,
                    )
                )
        except Exception as e:
            error_log(f"[{self.module_id}] 行為 on_exit 例外: {e}")

        self._enter_behavior(next_state)

    # ========= 工具/邊界/目標 =========

    def _ground_y(self) -> float:
        return self.v_bottom - self.SIZE + self.GROUND_OFFSET

    def _initialize_position(self):
        margin = self.screen_padding if hasattr(self, "screen_padding") else 50
        min_x = self.v_left + margin
        max_x = self.v_right - self.SIZE - margin
        min_y = self.v_top + margin
        max_y = self.v_bottom - self.SIZE - margin
        self.position.x = min(max(self.position.x, min_x), max_x)
        self.position.y = min(max(self.position.y, min_y), max_y)
        self._emit_position()


    def _set_target(self, x: float, y: float):
        margin = self.screen_padding
        # 落地時 y 鎖在地面，但拖曳模式除外
        if self.movement_mode == MovementMode.GROUND and not self.is_being_dragged:
            y = self._ground_y()
        max_x = self.v_right  - self.SIZE
        max_y = self.v_bottom - self.SIZE
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
        left  = self.v_left
        right = self.v_right  - self.SIZE
        boundary_hit = False

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

        if boundary_hit and self.current_behavior_state == BehaviorState.NORMAL_MOVE:
            self.velocity = Velocity(0.0, 0.0)
            self.target_velocity = Velocity(0.0, 0.0)
            self.target_reached = True

            if self.movement_mode == MovementMode.GROUND:
                turn_anim = "turn_right_g" if self.facing_direction > 0 else "turn_left_g"
                # 轉向是非 loop：等待完成 → 自動接 stand_idle_g（loop）
                self._trigger_anim(turn_anim, {
                    "loop": False,
                    "await_finish": True,
                    # 不要硬寫 1.0，交給 _trigger_anim 動態算時長 + 裕度
                    "next_anim": "stand_idle_g",
                    "next_params": {"loop": True}
                })
            else:
                # 浮空時沒有轉向動畫，直接播笑臉 idle
                self._trigger_anim("smile_idle_f", {"loop": True})

        if self.movement_mode == MovementMode.FLOAT:
            top = self.v_top
            bot = self.v_bottom - self.SIZE
            if self.position.y <= top:
                self.position.y = top
            elif self.position.y >= bot:
                self.position.y = bot


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

    def _trigger_anim(self, name: str, params: Optional[dict] = None):
        params = params or {}
        loop = params.get("loop", None)
        await_finish = bool(params.get("await_finish", False) or (loop is False))
        max_wait = float(params.get("max_wait", self._default_anim_timeout))
        next_anim = params.get("next_anim")  # 可選：完成後要接的動畫名
        next_params = params.get("next_params", {})  # 其參數
        force_restart = params.get("force_restart", False)  # 強制重新開始

        # 保護機制：如果正在等待動畫完成，避免重複觸發相同動畫（除非強制重新開始）
        if self._awaiting_anim and self._awaiting_anim == name and await_finish and not force_restart:
            debug_log(2, f"[{self.module_id}] 跳過重複動畫觸發: {name}")
            return

        # 如果強制重新開始，先清除等待狀態
        if force_restart and self._awaiting_anim:
            debug_log(2, f"[{self.module_id}] 強制重新開始動畫: {name}")
            self._awaiting_anim = None
            self._await_deadline = 0.0
            self.movement_locked_until = 0.0
            self.resume_movement(self.WAIT_ANIM_REASON)

        # 先送到 ANI
        if self.ani_module and hasattr(self.ani_module, "play"):
            try:
                # 如果需要強制重新開始，先停止當前動畫
                if force_restart and hasattr(self.ani_module, "stop"):
                    self.ani_module.stop()
                
                res = self.ani_module.play(name, loop=loop)
                debug_log(2, f"[{self.module_id}] 觸發動畫: {name} res={res} force_restart={force_restart}")
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
            else:
                debug_log(2, f"[{self.module_id}] 未處理的UI事件: {event_type}")
        except Exception as e:
            error_log(f"[{self.module_id}] 處理UI事件失敗: {event_type}, 錯誤: {e}")

    def _on_drag_start(self, event):
        # 記錄拖曳前的狀態
        self._drag_start_position = self.position.copy()
        self._drag_start_mode = self.movement_mode  # 記錄拖曳前的模式
        
        # 切換到拖曳狀態
        self.is_being_dragged = True
        self.movement_mode = MovementMode.DRAGGING
        self.velocity = Velocity(0.0, 0.0)
        self.target_velocity = Velocity(0.0, 0.0)
        
        self.pause_movement(self.DRAG_PAUSE_REASON)
        
        # 播放掙扎動畫
        self._trigger_anim("struggle", {"loop": True})
        debug_log(1, f"[{self.module_id}] 拖拽開始於 ({self.position.x:.1f}, {self.position.y:.1f})，從{self._drag_start_mode.value}模式，播放掙扎動畫")

    def _on_drag_move(self, event):
        """處理拖曳移動事件，直接更新位置跟隨滑鼠"""
        if not self.is_being_dragged:
            return
            
        # 支持字典格式的事件數據（來自UI）
        if isinstance(event, dict):
            new_x = float(event.get('x', self.position.x))
            new_y = float(event.get('y', self.position.y))
            
            # 只應用螢幕邊界限制，不限制高度範圍
            max_x = self.v_right - self.SIZE
            max_y = self.v_bottom - self.SIZE
            
            # 允許完全自由的拖曳，只要不超出螢幕範圍
            self.position.x = max(self.v_left, min(max_x, new_x))
            self.position.y = max(self.v_top, min(max_y, new_y))
            
            # 發射位置更新
            self._emit_position()
            
            debug_log(3, f"[{self.module_id}] 拖拽移動: ({self.position.x:.1f}, {self.position.y:.1f})")
            return
            
        # 支持原有的事件對象格式
        if hasattr(event, 'x') and hasattr(event, 'y'):
            new_x = float(event.x)
            new_y = float(event.y)
            
            # 只應用螢幕邊界限制，不限制高度範圍
            max_x = self.v_right - self.SIZE
            max_y = self.v_bottom - self.SIZE
            
            # 允許完全自由的拖曳，只要不超出螢幕範圍
            self.position.x = max(self.v_left, min(max_x, new_x))
            self.position.y = max(self.v_top, min(max_y, new_y))
            
            # 發射位置更新
            self._emit_position()
            
            debug_log(3, f"[{self.module_id}] 拖拽移動: ({self.position.x:.1f}, {self.position.y:.1f})")
        elif hasattr(event, 'data') and isinstance(event.data, dict):
            # 如果位置資訊在data字典中
            data = event.data
            if 'x' in data and 'y' in data:
                new_x = float(data['x'])
                new_y = float(data['y'])
                
                max_x = self.v_right - self.SIZE
                max_y = self.v_bottom - self.SIZE
                
                # 允許完全自由的拖曳
                self.position.x = max(self.v_left, min(max_x, new_x))
                self.position.y = max(self.v_top, min(max_y, new_y))
                
                self._emit_position()
                debug_log(3, f"[{self.module_id}] 拖拽移動: ({self.position.x:.1f}, {self.position.y:.1f})")

    def _on_drag_end(self, event):
        self.is_being_dragged = False
        
        # 智能判斷模式切換 - 基於最終位置的高度
        gy = self._ground_y()
        current_height = gy - self.position.y
        
        # 計算實際拖曳距離（可選的debug信息）
        drag_distance = 0
        if self._drag_start_position:
            drag_distance = math.hypot(
                self.position.x - self._drag_start_position.x,
                self.position.y - self._drag_start_position.y
            )
        
        # 判斷模式切換條件：主要基於高度
        height_threshold = 100  # 高度閾值
        
        debug_log(1, f"[{self.module_id}] 拖拽結束: 當前高度={current_height:.1f}, 距離={drag_distance:.1f}, 閾值={height_threshold}")
        
        if current_height > height_threshold:
            # 拖曳到較高位置 -> 浮空模式
            self.movement_mode = MovementMode.FLOAT
            self._trigger_anim("smile_idle_f", {"loop": True})
            debug_log(1, f"[{self.module_id}] 切換到浮空模式 (高度:{current_height:.1f} > {height_threshold})")
        else:
            # 拖曳到較低位置 -> 落地模式
            self.movement_mode = MovementMode.GROUND
            # 確保在地面上
            self.position.y = gy
            self._trigger_anim("stand_idle_g", {"loop": True})
            debug_log(1, f"[{self.module_id}] 切換到落地模式 (高度:{current_height:.1f} <= {height_threshold})")
        
        # 恢復移動並切換到idle狀態
        self.resume_movement(self.DRAG_PAUSE_REASON)
        self._switch_behavior(BehaviorState.IDLE)
        
        # 更新位置發射
        self._emit_position()
        
        debug_log(1, f"[{self.module_id}] 拖拽結束 → {self.movement_mode.value} 模式")

    # ========= API =========

    def _api_get_status(self) -> Dict[str, Any]:
        return {
            "position": {"x": self.position.x, "y": self.position.y},
            "velocity": {"x": self.velocity.x, "y": self.velocity.y},
            "mode": self.movement_mode.value,
            "state": self.current_behavior_state.value,
            "target": None if not self.movement_target else {"x": self.movement_target.x, "y": self.movement_target.y},
        }

    def _api_set_position(self, data: Dict[str, Any]) -> Dict[str, Any]:
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
        debug_log(1, f"[{self.module_id}] 系統狀態變更: {old_state} -> {new_state}")
        self._current_system_state = new_state
        
        # IDLE 狀態時清除層級並播放閒置動畫
        if new_state == UEPState.IDLE:
            self._current_layer = None
            self._update_animation_for_current_state()
    
    # ========= 層級事件訂閱與處理 =========
    
    def _subscribe_to_layer_events(self):
        """訂閱層級完成事件以驅動動畫"""
        try:
            from core.event_bus import event_bus, SystemEvent
            
            event_bus.subscribe(
                SystemEvent.INPUT_LAYER_COMPLETE,
                self._on_input_layer_complete,
                handler_name="mov_input_layer"
            )
            
            event_bus.subscribe(
                SystemEvent.PROCESSING_LAYER_COMPLETE,
                self._on_processing_layer_complete,
                handler_name="mov_processing_layer"
            )
            
            event_bus.subscribe(
                SystemEvent.OUTPUT_LAYER_COMPLETE,
                self._on_output_layer_complete,
                handler_name="mov_output_layer"
            )
            
            debug_log(2, f"[{self.module_id}] 已訂閱層級完成事件")
            
        except Exception as e:
            error_log(f"[{self.module_id}] 訂閱層級事件失敗: {e}")
    
    def _on_input_layer_complete(self, event):
        """輸入層完成 - 進入處理層"""
        try:
            debug_log(2, f"[{self.module_id}] 輸入層完成，進入處理層")
            self._current_layer = "processing"
            self._update_animation_for_current_state()
        except Exception as e:
            error_log(f"[{self.module_id}] 處理輸入層完成事件失敗: {e}")
    
    def _on_processing_layer_complete(self, event):
        """處理層完成 - 進入輸出層"""
        try:
            debug_log(2, f"[{self.module_id}] 處理層完成，進入輸出層")
            self._current_layer = "output"
            self._update_animation_for_current_state()
        except Exception as e:
            error_log(f"[{self.module_id}] 處理處理層完成事件失敗: {e}")
    
    def _on_output_layer_complete(self, event):
        """輸出層完成 - 回到輸入層（或閒置）"""
        try:
            debug_log(2, f"[{self.module_id}] 輸出層完成")
            self._current_layer = None
            self._update_animation_for_current_state()
        except Exception as e:
            error_log(f"[{self.module_id}] 處理輸出層完成事件失敗: {e}")
    
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
            if not self.ani_module:
                debug_log(3, f"[{self.module_id}] ANI 模組未注入，無法更新動畫")
                return
            
            if not self._state_animation_config:
                debug_log(2, f"[{self.module_id}] 無狀態動畫配置")
                return
            
            # IDLE 狀態：播放閒置動畫
            if self._current_system_state == UEPState.IDLE:
                self._handle_idle_animation()
                return
            
            # 有層級時根據層級選擇動畫
            if self._current_layer:
                self._handle_layer_animation()
            else:
                # 無層級時使用預設閒置動畫
                debug_log(3, f"[{self.module_id}] 無當前層級，使用閒置動畫")
                self._handle_idle_animation()
                
        except Exception as e:
            error_log(f"[{self.module_id}] 更新動畫失敗: {e}")
    
    def _handle_idle_animation(self):
        """處理 IDLE 狀態的動畫"""
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
                    self._trigger_anim(anim_name, {"loop": True})
                    debug_log(2, f"[{self.module_id}] IDLE 動畫: {anim_name}")
            else:
                debug_log(2, f"[{self.module_id}] IDLE 狀態: 無可用動畫")
                
        except Exception as e:
            error_log(f"[{self.module_id}] 處理 IDLE 動畫失敗: {e}")
    
    def _handle_layer_animation(self):
        """根據當前層級選擇動畫"""
        try:
            config = self._state_animation_config
            if not config:
                return
            
            layer_config = config.get("LAYERS", {})
            fallbacks = config.get("fallbacks", {})
            
            anim_name = None
            loop = False
            
            if self._current_layer == "input":
                # 輸入層：偵測到使用者互動
                input_config = layer_config.get("input", {})
                anim_name = input_config.get("default", "smile_idle_f")
                loop = True
                
            elif self._current_layer == "processing":
                # 處理層：系統思考/處理
                processing_config = layer_config.get("processing", {})
                anim_name = processing_config.get("default", "thinking_f")
                loop = True
                
            elif self._current_layer == "output":
                # 輸出層：系統回應（根據 mood）
                output_config = layer_config.get("output", {})
                
                # 獲取當前 mood 值
                try:
                    from core.status_manager import status_manager
                    mood = status_manager.status.mood
                    
                    if mood > 0:
                        anim_name = output_config.get("positive_mood", "talk_ani_f")
                    else:
                        anim_name = output_config.get("negative_mood", "talk_ani2_f")
                except Exception:
                    anim_name = output_config.get("positive_mood", "talk_ani_f")
                
                loop = False  # talk 動畫播放一次
            
            # 使用 fallback 映射
            if anim_name and anim_name in fallbacks:
                anim_name = fallbacks[anim_name]
            
            # 根據當前移動模式轉換動畫名稱
            if anim_name:
                anim_name = self._convert_animation_for_movement_mode(anim_name)
                if anim_name:
                    self._trigger_anim(anim_name, {"loop": loop})
                    debug_log(2, f"[{self.module_id}] 層級動畫: {self._current_layer} -> {anim_name} (loop={loop})")
                else:
                    debug_log(2, f"[{self.module_id}] 層級 {self._current_layer}: 無相容動畫")
            else:
                debug_log(2, f"[{self.module_id}] 層級 {self._current_layer}: 無合適動畫")
                
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
        try:
            if hasattr(ani, "add_start_callback"):
                ani.add_start_callback(self._on_ani_start)
            if hasattr(ani, "add_finish_callback"):
                ani.add_finish_callback(self._on_ani_finish)
            debug_log(2, f"[{self.module_id}] 已注入 ANI 並完成事件註冊")
        except Exception as e:
            error_log(f"[{self.module_id}] 注入 ANI 失敗: {e}")

    def _on_ani_start(self, name: str):
        # 目前僅記錄；之後若要精細同步（例如算轉場起點）可在此補
        debug_log(3, f"[{self.module_id}] ANI start: {name}")

    def _on_ani_finish(self, finished_name: str):
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

    def shutdown(self):
        """關閉移動模組，停止所有計時器和清理資源"""
        info_log(f"[{self.module_id}] 開始關閉移動模組")
        
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


