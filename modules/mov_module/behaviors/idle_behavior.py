from __future__ import annotations
from typing import Optional
import random
import time

from .base_behavior import BaseBehavior, BehaviorContext
from ..core.state_machine import BehaviorState
from utils.debug_helper import debug_log


class IdleBehavior(BaseBehavior):
    state = BehaviorState.IDLE

    def __init__(self):
        super().__init__()
        self._has_triggered_idle_anim = False
        # 🎲 彩蛋動畫追蹤
        self._last_easter_egg_time = 0.0
        self._easter_egg_cooldown = 180.0  # 預設冷卻時間（會從 config 讀取）
        self._easter_egg_playing = False
        self._easter_egg_name: Optional[str] = None

    def on_enter(self, ctx: BehaviorContext) -> None:
        # 停止移動
        ctx.velocity.x = 0.0
        ctx.velocity.y = 0.0
        ctx.target_velocity.x = 0.0
        ctx.target_velocity.y = 0.0
        
        self._has_triggered_idle_anim = False
        self._idle_start_time = ctx.now
        
        # 🎯 檢查是否有待觸發的 tease 動畫
        if hasattr(ctx, 'tease_tracker') and ctx.tease_tracker.has_pending():
            ctx.tease_tracker.clear_pending()
            # 觸發 tease 動畫（通過回調到主模組）
            if hasattr(ctx, 'trigger_tease_callback'):
                ctx.trigger_tease_callback()
                return  # 不播放 idle 動畫，等 tease 完成
        
        # 立即觸發閒置動畫（移除不必要的延遲）
        # 動畫切換緩衝已在 _trigger_anim 中處理
        self._trigger_idle_animation(ctx)
        
        # 標記 idle 起點
        ctx.sm.begin_idle(ctx.now)

    def on_tick(self, ctx: BehaviorContext):
        # � 如果彩蛋動畫正在播放，檢查是否已完成
        if self._easter_egg_playing:
            # 檢查動畫是否已完成（透過優先度管理器）
            if hasattr(ctx, 'animation_priority') and self._easter_egg_name:
                # 如果當前動畫不是彩蛋動畫，表示已完成
                current_anim = getattr(ctx.animation_priority, '_current_animation', None)
                if current_anim != self._easter_egg_name:
                    debug_log(2, f"[IdleBehavior] 彩蛋動畫 {self._easter_egg_name} 已完成，恢復 idle 動畫")
                    self._easter_egg_playing = False
                    self._easter_egg_name = None
                    # 恢復正常的 idle 動畫（強制觸發）
                    self._trigger_idle_animation(ctx, force=True)
            return None  # 停留在 IDLE
        
        # �🎲 檢查是否應該觸發彩蛋動畫
        if not self._has_triggered_idle_anim:
            # 還沒播放基礎 idle 動畫，先不嘗試彩蛋
            pass
        elif self._should_trigger_easter_egg(ctx):
            self._trigger_easter_egg_animation(ctx)
            return None  # 停留在 IDLE，讓彩蛋動畫播完
        
        # 檢查是否應該退出 IDLE 狀態
        if ctx.sm.should_exit_idle(ctx.now):
            # 用狀態機的權重決定下一步
            return ctx.sm.pick_next(ctx.movement_mode)
        return None

    def _trigger_idle_animation(self, ctx: BehaviorContext, force: bool = False):
        """觸發閒置動畫（基於情緒值選擇）
        
        Args:
            ctx: 行為上下文
            force: 是否強制觸發（用於彩蛋動畫完成後的恢復）
        """
        if self._has_triggered_idle_anim and not force:
            return
        
        self._has_triggered_idle_anim = True
        
        # 修復：確保 movement_mode 是枚舉類型，不是字符串
        if hasattr(ctx.movement_mode, 'value'):
            mode_value = ctx.movement_mode.value
        else:
            mode_value = str(ctx.movement_mode)
        
        is_ground = (mode_value == "ground")
        
        # 🎭 嘗試根據情緒值選擇特殊閒置動畫
        mood_anim = None
        if hasattr(ctx, 'status_manager') and ctx.anim_query:
            try:
                status = ctx.status_manager.status
                mood = status.get("mood", 0.5)
                pride = status.get("pride", 0.5)
                
                mood_anim = ctx.anim_query.get_mood_based_idle_animation(mood, pride, is_ground)
                
                if mood_anim:
                    debug_log(1, f"[IdleBehavior] 🎭 使用情緒閒置動畫: {mood_anim} (mood={mood:.2f}, pride={pride:.2f})")
            except Exception as e:
                debug_log(2, f"[IdleBehavior] 獲取情緒閒置動畫失敗: {e}")
        
        # 如果沒有情緒動畫，使用預設閒置動畫
        idle_anim = mood_anim if mood_anim else ("stand_idle_g" if is_ground else "smile_idle_f")
        
        # 先停止當前動畫，然後播放閒置動畫
        ctx.trigger_anim(idle_anim, {
            "loop": True,
            "force_restart": True  # 強制重新開始動畫
        })
    
    def _should_trigger_easter_egg(self, ctx: BehaviorContext) -> bool:
        """
        判斷是否應該觸發彩蛋動畫
        
        條件：
        1. 已經播放基礎 idle 動畫
        2. 冷卻時間已過
        3. 隨機機率通過
        """
        now = ctx.now
        
        # 檢查冷卻時間
        if now - self._last_easter_egg_time < self._easter_egg_cooldown:
            return False
        
        # 從 config 讀取觸發機率（預設 0.5%）
        trigger_chance = 0.005
        if hasattr(ctx, 'config'):
            easter_egg_config = ctx.config.get("easter_egg", {})
            trigger_chance = easter_egg_config.get("trigger_chance", 0.005)
            self._easter_egg_cooldown = easter_egg_config.get("cooldown", 180.0)
        
        # 隨機機率判斷
        return random.random() < trigger_chance
    
    def _trigger_easter_egg_animation(self, ctx: BehaviorContext):
        """觸發彩蛋動畫"""
        # 判斷當前模式
        if hasattr(ctx.movement_mode, 'value'):
            mode_value = ctx.movement_mode.value
        else:
            mode_value = str(ctx.movement_mode)
        
        is_ground = (mode_value == "ground")
        
        # 檢查 anim_query 是否存在
        if not ctx.anim_query:
            debug_log(3, "[IdleBehavior] anim_query 未初始化，無法獲取彩蛋動畫")
            return
        
        # 從動畫查詢輔助器獲取彩蛋動畫（傳入 status_manager 以檢查條件）
        status_manager = getattr(ctx, 'status_manager', None)
        easter_egg_anim = ctx.anim_query.get_easter_egg_animation(is_ground, status_manager)
        
        if not easter_egg_anim:
            debug_log(3, "[IdleBehavior] 沒有可用的彩蛋動畫")
            return
        
        debug_log(1, f"[IdleBehavior] 🎲 觸發彩蛋動畫: {easter_egg_anim}")
        
        # 更新最後觸發時間
        self._last_easter_egg_time = ctx.now
        
        # 觸發彩蛋動畫（不循環，MOV 會在完成時自動恢復 idle 動畫）
        ctx.trigger_anim(easter_egg_anim, {
            "loop": False,
            "force_restart": True
        })