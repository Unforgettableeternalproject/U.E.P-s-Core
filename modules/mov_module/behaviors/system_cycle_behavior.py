"""
系統循環行為

在系統循環期間：
1. 停止所有移動
2. 根據當前層級播放對應動畫（使用 LayerAnimationStrategy）
3. 禁用行為狀態機的自動轉換
4. 等待系統循環結束
"""

from __future__ import annotations
from typing import Optional

from .base_behavior import BaseBehavior, BehaviorContext
from ..core.state_machine import BehaviorState
from utils.debug_helper import debug_log, error_log


class SystemCycleBehavior(BaseBehavior):
    """
    系統循環期間的特殊行為
    
    特點：
    - 完全靜止，不產生任何移動
    - 根據層級自動播放對應動畫（listening/thinking/talking）
    - 使用 LayerAnimationStrategy 選擇動畫
    - 不執行狀態轉換邏輯
    """
    state = BehaviorState.SYSTEM_CYCLE

    def __init__(self):
        super().__init__()
        self._current_layer: Optional[str] = None
        self._last_triggered_anim: Optional[str] = None

    def on_enter(self, ctx: BehaviorContext) -> None:
        """進入系統循環：停止所有移動"""
        # 完全停止移動
        ctx.velocity.x = 0.0
        ctx.velocity.y = 0.0
        ctx.target_velocity.x = 0.0
        ctx.target_velocity.y = 0.0
        
        # 清除移動目標
        ctx.movement_target = None
        ctx.target_reached = False
        
        debug_log(2, "[SystemCycleBehavior] 進入系統循環行為")

    def on_tick(self, ctx: BehaviorContext) -> Optional[BehaviorState]:
        """
        系統循環期間：
        1. 確保保持靜止
        2. 檢查層級變化並觸發對應動畫
        """
        debug_log(3, f"[SystemCycleBehavior] on_tick: current_layer={getattr(ctx, 'current_layer', None)}")
        
        # 確保保持靜止
        ctx.velocity.x = 0.0
        ctx.velocity.y = 0.0
        
        # 檢查並處理層級動畫
        self._handle_layer_animation(ctx)
        
        # 不執行任何狀態轉換
        return None

    def _handle_layer_animation(self, ctx: BehaviorContext) -> None:
        """根據當前層級播放對應動畫"""
        try:
            # 從 context 獲取當前層級（由 MOV 主模組設置）
            current_layer = getattr(ctx, 'current_layer', None)
            
            # 如果層級為 None，表示系統循環已結束，應該退出此行為
            if current_layer is None:
                debug_log(2, "[SystemCycleBehavior] 層級為 None，準備退出系統循環")
                return
            
            # 如果層級沒有變化，不重複觸發
            if current_layer == self._current_layer:
                return
            
            self._current_layer = current_layer
            debug_log(2, f"[SystemCycleBehavior] 層級變更: {current_layer}")
            
            # 使用 LayerAnimationStrategy 選擇動畫
            layer_strategy = getattr(ctx, 'layer_strategy', None)
            if not layer_strategy:
                debug_log(2, "[SystemCycleBehavior] LayerAnimationStrategy 未找到")
                return
            
            # 準備 context 給 strategy
            strategy_context = {
                'layer': current_layer,
                'movement_mode': ctx.movement_mode,
                'mood': 0  # 預設 mood
            }
            
            # 嘗試從 status_manager 獲取 mood
            try:
                from core.status_manager import status_manager
                strategy_context['mood'] = status_manager.status.mood
            except Exception:
                pass
            
            # 選擇動畫
            anim_name = layer_strategy.select_animation(strategy_context)
            
            if anim_name:
                # 避免重複觸發相同動畫
                if anim_name == self._last_triggered_anim:
                    debug_log(3, f"[SystemCycleBehavior] 動畫已在播放: {anim_name}")
                    return
                
                self._last_triggered_anim = anim_name
                
                # 根據層級決定是否循環
                # 所有層級動畫都應該循環播放，直到層級變化或循環結束
                loop = True
                
                debug_log(1, f"[SystemCycleBehavior] ⚡ 觸發層級動畫: {anim_name}（層級: {current_layer}, loop={loop}）")
                
                # 系統循環動畫：最高優先級，強制中斷
                ctx.trigger_anim(anim_name, {
                    "loop": loop,
                    "immediate_interrupt": True,  # 跳過防抖
                    "force_restart": True          # 強制重啟
                })
                
                debug_log(1, f"[SystemCycleBehavior] ✅ 層級動畫已觸發: {anim_name}")
            else:
                debug_log(2, f"[SystemCycleBehavior] 無可用動畫（層級: {current_layer}）")
                
        except Exception as e:
            error_log(f"[SystemCycleBehavior] 處理層級動畫失敗: {e}")

    def on_exit(self, ctx: BehaviorContext) -> None:
        """退出系統循環時的清理"""
        debug_log(2, "[SystemCycleBehavior] 退出系統循環行為")
        self._current_layer = None
        self._last_triggered_anim = None
