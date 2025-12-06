"""
滑鼠追蹤處理器（事件驅動版本）

處理來自 UI 層的滑鼠追蹤事件
"""

import time
from typing import Any
from utils.debug_helper import debug_log, info_log, error_log

from .base_handler import BaseHandler

try:
    from ..core.state_machine import MovementMode
except ImportError:
    MovementMode = None  # type: ignore


class CursorTrackingHandler(BaseHandler):
    """
    滑鼠追蹤處理器（事件驅動版本）
    
    職責：
    1. 接收來自 UI 的滑鼠追蹤事件
    2. 處理滑鼠靠近/遠離邏輯
    3. 更新轉頭動畫幀（根據滑鼠角度）
    4. 控制移動暫停/恢復
    
    注意：滑鼠位置檢測由 UI 層負責，這裡只處理邏輯
    """
    
    CURSOR_TRACKING_REASON = "cursor_tracking"
    
    def __init__(self, coordinator):
        super().__init__(coordinator)
        
        # 追蹤狀態
        self._is_turning_head = False
        self._turn_head_start_time: float = 0.0
        self._current_turn_anim: str = ""
        self._current_turn_frame: int = 0
        # （已簡化）不再使用檔案互動暫停旗標，直接依賴 FileDropHandler.is_in_file_interaction 判斷
        
        # 配置（用於轉頭動畫幀計算）
        if hasattr(coordinator, 'config'):
            cfg = coordinator.config
            self._head_turn_sensitivity = int(cfg.get("cursor_head_turn_sensitivity", 9))
        else:
            self._head_turn_sensitivity = 9
        
        debug_log(2, "[CursorTrackingHandler] 已初始化（事件驅動模式）")
    
    def can_handle(self, event: Any) -> bool:
        """不透過 HandlerChain 處理，由 MOV 模組直接調用"""
        return False
    
    def handle(self, event: Any) -> bool:
        """不透過 HandlerChain 處理"""
        return False
    
    def update(self):
        """定期更新（目前事件驅動版本不需要）"""
        pass
    
    def on_cursor_near(self, event_data: dict):
        """
        處理滑鼠靠近事件（由 UI 層觸發）
        
        Args:
            event_data: 事件數據（包含初始角度）
        
        Note:
            只有在角色處於 IDLE 狀態時才會開始追蹤，避免移動中的干擾
        """
        # 🔧 出入場期間禁止追蹤
        if hasattr(self.coordinator, '_is_entering') and self.coordinator._is_entering:
            return
        if hasattr(self.coordinator, '_is_leaving') and self.coordinator._is_leaving:
            return
        
        # 檔案互動期間完全禁止追蹤（包含已在追蹤的情況，交由 suspend 方法處理）
        # 檔案互動期間禁止開始追蹤（由 FileDropHandler 狀態提供）
        if hasattr(self.coordinator, '_file_drop_handler') and self.coordinator._file_drop_handler.is_in_file_interaction:
            return
        if self._is_turning_head:
            return  # 已經在轉頭狀態
        
        try:
            # 檢查使用者設定是否啟用滑鼠追蹤
            if hasattr(self.coordinator, '_cursor_tracking_enabled') and not self.coordinator._cursor_tracking_enabled:
                return  # 使用者已禁用滑鼠追蹤
            
            # 檢查是否處於 IDLE 狀態（只有閒置時才追蹤）
            if not self._is_stationary():
                # debug_log 已在 _is_stationary() 內部處理
                return
            
            self._is_turning_head = True
            self._turn_head_start_time = time.time()
            
            # 暫停移動
            if hasattr(self.coordinator, 'pause_movement'):
                self.coordinator.pause_movement(self.CURSOR_TRACKING_REASON)
            
            # 立即播放初始轉頭動畫（使用事件中的角度）
            initial_angle = event_data.get('angle', 0.0)
            self._start_turn_head_animation(initial_angle)
            
            debug_log(2, f"[CursorTrackingHandler] 滑鼠靠近，開始轉頭追蹤（初始角度={initial_angle:.1f}°）")
            
        except Exception as e:
            error_log(f"[CursorTrackingHandler] 處理滑鼠靠近事件失敗: {e}")
    
    def on_cursor_far(self, event_data: dict):
        """
        處理滑鼠遠離事件（由 UI 層觸發）
        
        Args:
            event_data: 事件數據
        """
        if not self._is_turning_head:
            return  # 不在轉頭狀態
        
        try:
            self._stop_tracking()
            debug_log(2, "[CursorTrackingHandler] 滑鼠遠離，停止轉頭追蹤")
            
        except Exception as e:
            error_log(f"[CursorTrackingHandler] 處理滑鼠遠離事件失敗: {e}")
    
    def _stop_tracking(self, restore_idle: bool = True):
        """
        停止追蹤並恢復狀態（內部方法，避免重複代碼）
        
        Args:
            restore_idle: 是否恢復 idle 動畫（預設 True）
                         在拖動開始時應設為 False，避免動畫閃現
        """
        if not self._is_turning_head:
            return
        
        self._is_turning_head = False
        
        # 退出靜態幀模式
        ani_module = self.coordinator.ani_module if hasattr(self.coordinator, 'ani_module') else None
        if ani_module and hasattr(ani_module, 'manager'):
            ani_module.manager.exit_static_frame_mode()
            debug_log(3, "[CursorTrackingHandler] 已退出靜態幀模式")
        
        # 清除追蹤動畫的優先度鎖定
        if hasattr(self.coordinator, '_animation_priority'):
            pm = self.coordinator._animation_priority
            if pm.current_request and pm.current_request.source == "cursor_tracking":
                # 通知動畫完成，清除優先度
                if pm.current_request.name:
                    pm.on_animation_finished(pm.current_request.name)
                    debug_log(3, f"[CursorTrackingHandler] 已清除追蹤動畫優先度: {pm.current_request.name}")
        
        # 恢復移動
        if hasattr(self.coordinator, 'resume_movement'):
            self.coordinator.resume_movement(self.CURSOR_TRACKING_REASON)
        
        # 恢復閒置動畫（檔案互動暫停期間不恢復）
        if restore_idle and hasattr(self.coordinator, 'current_behavior_state'):
            from ..core.state_machine import BehaviorState, MovementMode
            if self.coordinator.current_behavior_state == BehaviorState.IDLE:
                # 只在 IDLE 狀態下恢復閒置動畫
                is_ground = (self.coordinator.movement_mode == MovementMode.GROUND)
                idle_anim = self.coordinator.anim_query.get_idle_animation_for_mode(is_ground)
                if hasattr(self.coordinator, '_trigger_anim'):
                    self.coordinator._trigger_anim(idle_anim, {
                        "loop": True,
                        "force_restart": False
                    }, source="cursor_tracking")
                    debug_log(2, f"[CursorTrackingHandler] 已恢復閒置動畫: {idle_anim}")

    # 已移除暫停/恢復方法，直接依賴 FileDropHandler 狀態
    
    def update_turn_head_angle(self, angle: float):
        """
        更新轉頭動畫幀（根據滑鼠角度）
        
        Args:
            angle: 滑鼠相對於角色的角度（0-360度）
                  0° = 右，90° = 上，180° = 左，270° = 下
        
        Note:
            - turn_head_g（地面）：幀0 = 左90°（180°），順時針旋轉
            - turn_head_f（空中）：幀序列反轉，需要不同的角度映射
        """
        if not self._is_turning_head:
            return
        
        try:
            # 判斷當前是否為地面模式
            is_ground = (MovementMode and 
                        hasattr(self.coordinator, 'movement_mode') and 
                        self.coordinator.movement_mode == MovementMode.GROUND)
            
            # 根據模式選擇不同的角度映射
            if is_ground:
                # 地面動畫：正常映射（反轉方向 + 偏移）
                adjusted_angle = (360 - angle + 180) % 360
            else:
                # 空中動畫：幀序列反轉，直接映射（不反轉方向，只加偏移）
                adjusted_angle = (angle + 180) % 360
            
            frame_index = int(adjusted_angle / self._head_turn_sensitivity)
            
            # 檢查是否需要更新（避免重複設置相同幀）
            if hasattr(self, '_current_turn_frame') and self._current_turn_frame == frame_index:
                return
            
            self._current_turn_frame = frame_index
            
            # 直接設置幀索引（不重新播放動畫）
            self._set_turn_head_frame(frame_index)
            
        except Exception as e:
            error_log(f"[CursorTrackingHandler] 更新轉頭角度失敗: {e}")
    
    def _start_turn_head_animation(self, angle: float):
        """
        開始轉頭追蹤（初始進入追蹤範圍時）
        
        Note:
            不播放動畫，只設置動畫名稱和初始幀
            透過 set_frame() 實現平滑的角度切換，不播放動畫
        """
        try:
            # 根據當前移動模式選擇動畫
            is_ground = (MovementMode and 
                        hasattr(self.coordinator, 'movement_mode') and 
                        self.coordinator.movement_mode == MovementMode.GROUND)
            # 選擇標準轉頭動畫（檔案互動期間不追蹤，無需覆蓋）
            turn_anim = self.coordinator.anim_query.get_turn_head_animation(is_ground=bool(is_ground))
            
            if not turn_anim:
                return
            
            # 計算初始幀（根據模式選擇角度映射）
            if is_ground:
                # 地面動畫：正常映射（反轉方向 + 偏移）
                adjusted_angle = (360 - angle + 180) % 360
            else:
                # 空中動畫：幀序列反轉，直接映射（不反轉方向，只加偏移）
                adjusted_angle = (angle + 180) % 360
            
            frame_index = int(adjusted_angle / self._head_turn_sensitivity)
            
            # 關鍵修復：使用靜態幀模式進入 turn_head 動畫
            # 這樣可以避免 AnimationManager 的自動更新，實現純手動幀切換
            ani_module = self.coordinator.ani_module if hasattr(self.coordinator, 'ani_module') else None
            if ani_module and hasattr(ani_module, 'manager'):
                # 使用靜態幀模式 API
                result = ani_module.manager.enter_static_frame_mode(turn_anim, frame_index)
                
                if result.get("success"):
                    # 記錄當前轉頭動畫
                    self._current_turn_anim = turn_anim
                    self._current_turn_frame = frame_index
                    
                    debug_log(2, f"[CursorTrackingHandler] 設置轉頭動畫（靜態幀模式）: {turn_anim}, 初始角度={angle:.1f}°, 幀={frame_index}")
                    return
                else:
                    debug_log(1, f"[CursorTrackingHandler] 進入靜態幀模式失敗: {turn_anim}, 錯誤: {result.get('error')}")
            
            # 降級方案：使用 _trigger_anim
            debug_log(1, "[CursorTrackingHandler] 警告：無法設置轉頭動畫，使用降級方案")
            self.coordinator._trigger_anim(turn_anim, {"loop": False}, source="cursor_tracking")
            self._current_turn_anim = turn_anim
            self._current_turn_frame = frame_index
            self._set_turn_head_frame(frame_index)
            
        except Exception as e:
            error_log(f"[CursorTrackingHandler] 開始轉頭追蹤失敗: {e}")
    
    def _set_turn_head_frame(self, frame_index: int):
        """
        直接設置轉頭動畫幀（參考 desktop_pet.py）
        
        使用 ANI 模組的 set_current_frame 直接修改幀索引，
        避免重新播放動畫造成的閃爍和性能問題
        """
        try:
            # 獲取 ANI 模組
            ani_module = self.coordinator.ani_module if hasattr(self.coordinator, 'ani_module') else None
            
            # 不進行 notice 覆蓋；檔案互動期間已被暫停

            if ani_module and hasattr(ani_module, 'set_current_frame'):
                # 使用 ANI 的直接幀設置（最優方案）
                result = ani_module.set_current_frame(frame_index)
                # 只在失敗時記錄日誌（減少洗屏）
                if not result.get("success"):
                    debug_log(2, f"[CursorTrackingHandler] 設置幀失敗: {result.get('error')}")
                
        except Exception as e:
            error_log(f"[CursorTrackingHandler] 設置轉頭幀失敗: {e}")
    
    def _is_stationary(self) -> bool:
        """
        檢查角色是否處於 IDLE 狀態且不在特殊模式（正確判斷靜止條件）
        
        Returns:
            True 如果角色處於 BehaviorState.IDLE 且不在 DRAGGING/THROWN 模式
        
        Note:
            不能只用速度判斷，因為 NORMAL_MOVE 初期速度也可能很小
            必須檢查 BehaviorState.IDLE 才能確保角色真正靜止
        """
        try:
            # 最高優先級：入場期間完全禁止追蹤
            if hasattr(self.coordinator, '_is_entering') and self.coordinator._is_entering:
                debug_log(3, "[CursorTrackingHandler] 入場動畫播放中，禁止追蹤")
                return False
            
            # 🔧 最高優先級：離場期間完全禁止追蹤
            if hasattr(self.coordinator, '_is_leaving') and self.coordinator._is_leaving:
                debug_log(3, "[CursorTrackingHandler] 離場動畫播放中，禁止追蹤")
                return False
            
            # 🌙 睡眠狀態下完全禁止追蹤（包括睡眠轉換期間）
            if hasattr(self.coordinator, 'current_behavior_state'):
                from modules.mov_module.core.state_machine import BehaviorState
                if self.coordinator.current_behavior_state == BehaviorState.SLEEPING:
                    debug_log(3, "[CursorTrackingHandler] 睡眠狀態，禁止追蹤")
                    return False
            
            # 🌙 等待睡眠轉換期間也禁止追蹤（避免 f_to_g 播放時被中斷）
            if hasattr(self.coordinator, '_pending_sleep_transition') and self.coordinator._pending_sleep_transition:
                debug_log(3, "[CursorTrackingHandler] 睡眠轉換中，禁止追蹤")
                return False
            
            # 優先檢查：禁止在 THROWN 或 DRAGGING 模式下追蹤
            if hasattr(self.coordinator, 'movement_mode'):
                from modules.mov_module.core.state_machine import MovementMode
                mode = self.coordinator.movement_mode
                if mode in (MovementMode.THROWN, MovementMode.DRAGGING):
                    debug_log(3, f"[CursorTrackingHandler] 特殊模式({mode.value})，禁止追蹤")
                    return False
            
            # 檢查是否處於 IDLE 行為狀態（最可靠的判斷）
            if hasattr(self.coordinator, 'current_behavior_state'):
                from modules.mov_module.core.state_machine import BehaviorState
                is_idle = self.coordinator.current_behavior_state == BehaviorState.IDLE
                
                if not is_idle:
                    # 明確記錄為何不追蹤（避免誤判）
                    # 加強空值保護
                    current_state = self.coordinator.current_behavior_state.value if self.coordinator.current_behavior_state else "None"
                    debug_log(3, f"[CursorTrackingHandler] 非閒置狀態({current_state})，跳過追蹤")
                    return False
            
            # 禁止在投擲動畫序列期間追蹤（避免打斷落地動畫）
            if hasattr(self.coordinator, '_throw_handler'):
                if self.coordinator._throw_handler.is_in_throw_animation:
                    debug_log(3, "[CursorTrackingHandler] 投擲動畫序列進行中，禁止追蹤")
                    return False
            
            # 禁止在 Tease 動畫播放期間追蹤（Tease 動畫優先級最高）
            if hasattr(self.coordinator, '_tease_tracker'):
                if self.coordinator._tease_tracker.is_teasing():
                    debug_log(3, "[CursorTrackingHandler] Tease 動畫播放中，禁止追蹤")
                    return False
            
            # 檔案互動期間完全禁止追蹤（包括 hover）
            if hasattr(self.coordinator, '_file_drop_handler'):
                fdh = self.coordinator._file_drop_handler
                if fdh.is_in_file_interaction:
                    debug_log(3, "[CursorTrackingHandler] 檔案互動中，禁止追蹤")
                    return False
            
            # 檢查是否正在播放閒置動畫（只有閒置動畫時才允許追蹤）
            if hasattr(self.coordinator, 'ani_module'):
                ani = self.coordinator.ani_module
                if hasattr(ani, 'manager') and hasattr(ani.manager, 'current_clip'):
                    current_anim = ani.manager.current_clip
                    if current_anim:
                        # 只有播放閒置動畫時才允許追蹤
                        idle_keywords = ['idle', 'smile', 'stand']
                        is_idle_anim = any(keyword in current_anim.lower() for keyword in idle_keywords)
                        if not is_idle_anim:
                            debug_log(3, f"[CursorTrackingHandler] 正在播放非閒置動畫({current_anim})，禁止追蹤")
                            return False
            
            # 通過所有檢查，允許追蹤
            return True
            
        except Exception as e:
            error_log(f"[CursorTrackingHandler] 檢查靜止狀態失敗: {e}")
            return False
    
    def _restore_idle_animation(self):
        """恢復閒置動畫（僅在真正需要時）"""
        try:
            from ..core.animation_priority import AnimationPriority
            
            # 不鎖定移動，讓行為系統自然接管
            # 不強制播放閒置動畫，讓當前行為決定動畫
            # 只需確保靜態幀模式已退出即可
            
            # 如果角色正在移動或有其他行為，不要強制閒置動畫
            if hasattr(self.coordinator, 'current_behavior'):
                current = self.coordinator.current_behavior
                if current and current not in ['idle', None]:
                    debug_log(2, f"[CursorTrackingHandler] 角色正在執行行為 {current}，跳過閒置動畫恢復")
                    return
            
            # 只在真正閒置時才恢復閒置動畫（使用正常優先度）
            is_ground = (MovementMode and 
                        hasattr(self.coordinator, 'movement_mode') and 
                        self.coordinator.movement_mode == MovementMode.GROUND)
            idle_anim = self.coordinator.anim_query.get_idle_animation_for_mode(is_ground=bool(is_ground))
            self.coordinator._trigger_anim(
                idle_anim, 
                {"loop": True, "force_restart": False},  # 不強制重啟
                source="cursor_tracking",
                priority=AnimationPriority.IDLE_ANIMATION  # 使用正常優先度
            )
            debug_log(2, f"[CursorTrackingHandler] 恢復閒置動畫: {idle_anim}")
            
        except Exception as e:
            error_log(f"[CursorTrackingHandler] 恢復閒置動畫失敗: {e}")
    
    def shutdown(self):
        """關閉處理器"""
        try:
            if self._is_turning_head:
                # 恢復移動
                if hasattr(self.coordinator, 'resume_movement'):
                    self.coordinator.resume_movement(self.CURSOR_TRACKING_REASON)
            
            debug_log(2, "[CursorTrackingHandler] 已關閉")
            
        except Exception as e:
            error_log(f"[CursorTrackingHandler] 關閉失敗: {e}")

    # 已移除檔案 hover 覆蓋模式，改回由 FileDropHandler 直接觸發 notice 動畫
