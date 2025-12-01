"""
互動處理器

處理用戶互動事件（拖曳、投擲、檔案投放等）
"""

from typing import Any, Optional
from utils.debug_helper import debug_log, info_log, error_log

from .base_handler import BaseHandler

try:
    from core.bases.frontend_base import UIEventType
    from ..core.state_machine import MovementMode
    from ..core.position import Position, Velocity
except ImportError:
    UIEventType = None  # type: ignore
    MovementMode = None  # type: ignore
    Position = None  # type: ignore
    Velocity = None  # type: ignore


class InteractionHandler(BaseHandler):
    """
    互動處理器基類
    
    為具體的互動處理器（拖曳、投擲、檔案投放等）提供基礎
    """
    
    def __init__(self, coordinator):
        super().__init__(coordinator)


class DragInteractionHandler(InteractionHandler):
    """
    拖曳互動處理器
    
    職責：
    1. 處理拖曳開始/移動/結束事件
    2. 更新角色位置和狀態
    3. 觸發掙扎動畫
    4. 判斷投擲動作
    """
    
    # 投擲速度閾值（像素/秒）
    THROW_VELOCITY_THRESHOLD = 500.0
    
    def __init__(self, coordinator):
        super().__init__(coordinator)
        self.drag_start_position: Optional[Position] = None
        self.drag_start_mode: Optional[Any] = None
        self.drag_start_time: float = 0.0
        
    def can_handle(self, event: Any) -> bool:
        """判斷是否為拖曳事件"""
        if not hasattr(event, 'event_type'):
            return False
            
        if UIEventType is None:
            return False
            
        return event.event_type in [
            UIEventType.DRAG_START,
            UIEventType.DRAG_MOVE,
            UIEventType.DRAG_END
        ]
    
    def handle(self, event: Any) -> bool:
        """處理拖曳事件"""
        try:
            if event.event_type == UIEventType.DRAG_START:
                return self._handle_drag_start(event)
            elif event.event_type == UIEventType.DRAG_MOVE:
                return self._handle_drag_move(event)
            elif event.event_type == UIEventType.DRAG_END:
                return self._handle_drag_end(event)
            return False
            
        except Exception as e:
            error_log(f"[DragHandler] 處理拖曳事件失敗: {e}")
            return False
    
    def _handle_drag_start(self, event: Any) -> bool:
        """處理拖曳開始"""
        import time
        
        # 記錄拖曳前狀態
        if hasattr(self.coordinator, 'position') and Position:
            self.drag_start_position = self.coordinator.position.copy()
        
        if hasattr(self.coordinator, 'movement_mode'):
            self.drag_start_mode = self.coordinator.movement_mode
        
        self.drag_start_time = time.time()
        
        # 設置拖曳狀態
        if hasattr(self.coordinator, 'is_being_dragged'):
            self.coordinator.is_being_dragged = True
        
        if hasattr(self.coordinator, 'movement_mode') and MovementMode:
            self.coordinator.movement_mode = MovementMode.DRAGGING
        
        # 清空速度
        if hasattr(self.coordinator, 'velocity') and Velocity:
            self.coordinator.velocity = Velocity(0.0, 0.0)
            self.coordinator.target_velocity = Velocity(0.0, 0.0)
        
        # 暫停移動
        if hasattr(self.coordinator, 'pause_movement'):
            self.coordinator.pause_movement("拖曳中")
        
        # 觸發掙扎動畫
        if hasattr(self.coordinator, '_trigger_anim'):
            self.coordinator._trigger_anim("struggle", {"loop": True}, source="drag_handler")
        
        info_log(f"[DragHandler] 拖曳開始")
        return True
    
    def _handle_drag_move(self, event: Any) -> bool:
        """處理拖曳移動"""
        if not hasattr(self.coordinator, 'is_being_dragged') or not self.coordinator.is_being_dragged:
            return False
        
        # 更新位置
        event_data = event.data if hasattr(event, 'data') else {}
        
        if 'x' in event_data and 'y' in event_data:
            if hasattr(self.coordinator, 'position'):
                self.coordinator.position.x = float(event_data['x'])
                self.coordinator.position.y = float(event_data['y'])
            
            # 發射位置更新
            if hasattr(self.coordinator, '_emit_position'):
                self.coordinator._emit_position()
        
        return True
    
    def _handle_drag_end(self, event: Any) -> bool:
        """處理拖曳結束"""
        import time
        
        if not hasattr(self.coordinator, 'is_being_dragged'):
            return False
        
        self.coordinator.is_being_dragged = False
        
        # 計算拖曳持續時間和位移
        drag_duration = time.time() - self.drag_start_time
        
        # 判斷是否為投擲動作（快速移動）
        is_throw = False
        velocity = 0.0
        if self.drag_start_position and hasattr(self.coordinator, 'position'):
            import math
            dx = self.coordinator.position.x - self.drag_start_position.x
            dy = self.coordinator.position.y - self.drag_start_position.y
            distance = math.hypot(dx, dy)
            
            # 計算平均速度
            if drag_duration > 0:
                velocity = distance / drag_duration
                is_throw = velocity > self.THROW_VELOCITY_THRESHOLD
        
        # 根據最終位置判斷模式
        if not is_throw and hasattr(self.coordinator, '_ground_y'):
            gy = self.coordinator._ground_y()
            current_height = gy - self.coordinator.position.y
            height_threshold = 100
            
            if current_height > height_threshold and MovementMode:
                self.coordinator.movement_mode = MovementMode.FLOAT
                info_log(f"[DragHandler] 拖曳結束 → 浮空模式 (高度: {current_height:.1f})")
            elif MovementMode:
                self.coordinator.movement_mode = MovementMode.GROUND
                self.coordinator.position.y = gy
                info_log(f"[DragHandler] 拖曳結束 → 地面模式")
        
        # 如果是投擲，設置投擲模式和速度
        if is_throw and MovementMode and Velocity:
            self.coordinator.movement_mode = MovementMode.THROWN
            # 計算投擲速度向量
            if drag_duration > 0 and self.drag_start_position:
                vx = (self.coordinator.position.x - self.drag_start_position.x) / drag_duration
                vy = (self.coordinator.position.y - self.drag_start_position.y) / drag_duration
                self.coordinator.velocity = Velocity(vx, vy)
            info_log(f"[DragHandler] 檢測到投擲動作！速度: {velocity:.1f} px/s")
        
        # 恢復移動
        if hasattr(self.coordinator, 'resume_movement'):
            self.coordinator.resume_movement("拖曳中")
        
        # 切換到閒置行為
        if hasattr(self.coordinator, '_switch_behavior'):
            from ..core.state_machine import BehaviorState
            self.coordinator._switch_behavior(BehaviorState.IDLE)
        
        # 更新位置
        if hasattr(self.coordinator, '_emit_position'):
            self.coordinator._emit_position()
        
        return True


class FileDropHandler(InteractionHandler):
    """
    檔案投放處理器
    
    職責：
    1. 處理檔案 hover 事件 → 播放 notice 動畫（循環）
    2. 處理檔案 drop 事件 → 播放 receive 動畫（單次）
    3. 管理行為機暫停：從 hover 到 receive 結束
    4. 處理 hover 取消：恢復行為機
    
    動畫流程：
    - FILE_HOVER → notice_{float/ground} (loop=True) + 暫停行為機
    - FILE_HOVER_LEAVE → 停止 notice + 恢復行為機
    - FILE_DROP → notice 結束 → receive_{float/ground} (loop=False) → receive 結束後恢復行為機
    """
    
    def __init__(self, coordinator):
        super().__init__(coordinator)
        self._is_hovering = False  # 是否正在 hover
        self._is_receiving = False  # 是否正在播放 receive 動畫
        self._hover_animation_name: Optional[str] = None  # 當前 notice 動畫名稱
        
        info_log("[FileDropHandler] 初始化完成")
    
    @property
    def is_in_file_interaction(self) -> bool:
        """是否正在進行檔案互動（hover 或 receiving）"""
        return self._is_hovering or self._is_receiving
    
    def can_handle(self, event: Any) -> bool:
        """判斷是否為檔案相關事件"""
        # 支援字典格式（從 MOV._on_file_drop 傳入）或事件物件
        if isinstance(event, dict):
            return 'file_path' in event
        
        if not hasattr(event, 'event_type'):
            return False
        
        if UIEventType is None:
            return False
            
        return event.event_type in [
            UIEventType.FILE_HOVER,
            UIEventType.FILE_HOVER_LEAVE,
            UIEventType.FILE_DROP
        ]
    
    def handle(self, event: Any) -> bool:
        """處理檔案相關事件
        
        Args:
            event: 可以是字典 {'file_path': str} 或事件物件（有 event_type 屬性）
        """
        try:
            # 字典格式（直接來自 UI 的 FILE_DROP）
            if isinstance(event, dict):
                return self._handle_file_drop(event)
            
            # 事件物件格式
            if event.event_type == UIEventType.FILE_HOVER:
                return self._handle_file_hover(event)
            elif event.event_type == UIEventType.FILE_HOVER_LEAVE:
                return self._handle_file_hover_leave(event)
            elif event.event_type == UIEventType.FILE_DROP:
                return self._handle_file_drop(event)
            return False
            
        except Exception as e:
            error_log(f"[FileDropHandler] 處理檔案事件失敗: {e}")
            import traceback
            error_log(f"[FileDropHandler] 錯誤追蹤: {traceback.format_exc()}")
            return False
    
    def _handle_file_hover(self, event: Any) -> bool:
        """處理檔案 hover 事件 - 播放 notice 動畫"""
        if self._is_hovering:
            debug_log(2, "[FileDropHandler] 已經在 hover 狀態，忽略重複事件")
            return True
        
        info_log("[FileDropHandler] 📁 檔案懸停在 UEP 上方")
        # 若正在追蹤則停止（避免靜態幀模式殘留）
        if hasattr(self.coordinator, '_cursor_tracking_handler'):
            tracking_handler = self.coordinator._cursor_tracking_handler
            if tracking_handler and getattr(tracking_handler, '_is_turning_head', False) and hasattr(tracking_handler, '_stop_tracking'):
                tracking_handler._stop_tracking(restore_idle=False)
                debug_log(2, "[FileDropHandler] 已停止滑鼠追蹤（檔案 hover）")

        # 設置 hover 狀態（行為機暫停由行為邏輯自行檢測 is_in_file_interaction）
        self._is_hovering = True
        debug_log(2, "[FileDropHandler] 檔案 hover 狀態已設置，行為機將自動暫停")
        
        # 判斷當前模式（float 或 ground）
        if hasattr(self.coordinator, 'movement_mode') and MovementMode:
            is_floating = self.coordinator.movement_mode == MovementMode.FLOAT
        else:
            # 備用：根據高度判斷
            is_floating = False
            if hasattr(self.coordinator, 'position') and hasattr(self.coordinator, '_ground_y'):
                ground_y = self.coordinator._ground_y()
                current_height = ground_y - self.coordinator.position.y
                is_floating = current_height > 50
        
        # 直接觸發 notice 動畫（循環）
        animation_name = "notice_f" if is_floating else "notice_g"
        self._hover_animation_name = animation_name
        if hasattr(self.coordinator, '_trigger_anim'):
            from ..core.animation_priority import AnimationPriority
            self.coordinator._trigger_anim(
                animation_name,
                {"loop": True, "force_restart": True, "immediate_interrupt": True},
                source="file_drop_handler",
                priority=AnimationPriority.USER_INTERACTION
            )
            info_log(f"[FileDropHandler] 🔔 播放 notice 動畫: {animation_name} (循環)")
        
        return True
    
    def _handle_file_hover_leave(self, event: Any) -> bool:
        """處理檔案離開 - 停止 notice 動畫並恢復行為機"""
        if not self._is_hovering:
            debug_log(2, "[FileDropHandler] 不在 hover 狀態，忽略 leave 事件")
            return True
        
        info_log("[FileDropHandler] 📤 檔案離開 UEP 區域")
        
        # 清除 hover 狀態（行為機會在 _tick_behavior 中檢測狀態變化並自動恢復）
        self._is_hovering = False
        self._hover_animation_name = None
        
        # 停止動畫（若之前曾強制播放）
        if hasattr(self.coordinator, 'ani_module') and self.coordinator.ani_module:
            self.coordinator.ani_module.stop()
            debug_log(2, "[FileDropHandler] 已停止 notice 動畫")
        # 不主動恢復追蹤；由滑鼠靠近事件自然重新判斷
        
        debug_log(2, "[FileDropHandler] hover 狀態已清除，行為機將自動恢復")
        
        return True
    
    def _handle_file_drop(self, event: Any) -> bool:
        """處理檔案投放 - 播放 receive 動畫並處理檔案
        
        Args:
            event: 可以是事件物件（有 .data 屬性）或字典（直接包含 file_path）
        """
        # 支持兩種格式：事件物件或字典
        if isinstance(event, dict):
            event_data = event
        else:
            event_data = event.data if hasattr(event, 'data') else {}
        
        file_path = event_data.get('file_path', '')
        
        if not file_path:
            error_log("[FileDropHandler] 檔案路徑為空")
            # 清理狀態
            self._cleanup_file_interaction()
            return False
        
        # 驗證檔案是否存在
        from pathlib import Path
        path_obj = Path(file_path)
        if not path_obj.exists():
            error_log(f"[FileDropHandler] 檔案不存在: {file_path}")
            self._cleanup_file_interaction()
            return False
        
        info_log(f"[FileDropHandler] 📥 接收檔案: {path_obj.name}")
        
        # 接收期間若正在追蹤則停止
        if hasattr(self.coordinator, '_cursor_tracking_handler'):
            tracking_handler = self.coordinator._cursor_tracking_handler
            if tracking_handler and getattr(tracking_handler, '_is_turning_head', False) and hasattr(tracking_handler, '_stop_tracking'):
                tracking_handler._stop_tracking(restore_idle=False)
                debug_log(2, "[FileDropHandler] 已停止滑鼠追蹤（檔案接收）")
            
        # 設置 receiving 狀態
        self._is_hovering = False  # hover 結束
        self._is_receiving = True
        
        # 判斷當前模式（float 或 ground）
        if hasattr(self.coordinator, 'movement_mode') and MovementMode:
            is_floating = self.coordinator.movement_mode == MovementMode.FLOAT
        else:
            is_floating = False
            if hasattr(self.coordinator, 'position') and hasattr(self.coordinator, '_ground_y'):
                ground_y = self.coordinator._ground_y()
                current_height = ground_y - self.coordinator.position.y
                is_floating = current_height > 50
        
        # 停止 notice 動畫並退出靜態幀模式
        if hasattr(self.coordinator, 'ani_module') and self.coordinator.ani_module:
            self.coordinator.ani_module.stop()
            debug_log(2, "[FileDropHandler] 已停止 notice 動畫")
        
        # 選擇對應的 receive 動畫
        animation_name = "receive_f" if is_floating else "receive_g"
        
        # 註冊動畫結束回調
        if hasattr(self.coordinator, 'ani_module') and self.coordinator.ani_module:
            self.coordinator.ani_module.add_finish_callback(self._on_receive_animation_finish)
        
        # 播放 receive 動畫（單次）- 使用 force_restart 和 immediate_interrupt 確保能打斷追蹤
        if hasattr(self.coordinator, '_trigger_anim'):
            from ..core.animation_priority import AnimationPriority
            self.coordinator._trigger_anim(
                animation_name,
                {"loop": False, "force_restart": True, "immediate_interrupt": True},
                source="file_drop_handler",
                priority=AnimationPriority.USER_INTERACTION
            )
            info_log(f"[FileDropHandler] 🎁 播放 receive 動畫: {animation_name} (優先級=USER_INTERACTION)")
        
        # 🎯 儲存檔案路徑到 WorkingContext（全局可訪問）
        try:
            from core.working_context import working_context_manager
            working_context_manager.set_context_data("current_file_path", str(path_obj))
            debug_log(2, f"[FileDropHandler] 檔案路徑已儲存到 WorkingContext: {path_obj}")
        except Exception as e:
            error_log(f"[FileDropHandler] 儲存檔案路徑到 WorkingContext 失敗: {e}")
            self._cleanup_file_interaction()
            return False
            
            # 📢 發送事件通知其他模組
            if hasattr(self.coordinator, 'event_bus'):
                try:
                    self.coordinator.event_bus.publish(
                        "file_received",
                        {
                            "file_path": str(path_obj),
                            "file_name": path_obj.name,
                            "file_size": path_obj.stat().st_size,
                            "file_type": path_obj.suffix
                        }
                    )
                    debug_log(2, "[FileDropHandler] 已發送 file_received 事件")
                except Exception as e:
                    error_log(f"[FileDropHandler] 發送事件失敗: {e}")
            
            # 🔧 檢查是否有活躍的工作流正在等待檔案輸入
            # 如果有，發布 FILE_INPUT_PROVIDED 事件來觸發工作流繼續執行
            try:
                from core.working_context import working_context_manager
                workflow_waiting = working_context_manager.get_context_data('workflow_waiting_input')
                workflow_context = working_context_manager.get_context_data('workflow_input_context')
                
                if workflow_waiting and workflow_context:
                    workflow_session_id = workflow_context.get('workflow_session_id')
                    step_id = workflow_context.get('step_id')
                    
                    debug_log(2, f"[FileDropHandler] 檢測到工作流正在等待輸入: {workflow_session_id}, step={step_id}")
                    
                    # 發布事件觸發 SystemLoop 提交檔案路徑到工作流
                    from core.event_bus import event_bus, SystemEvent
                    event_bus.publish(
                        SystemEvent.FILE_INPUT_PROVIDED,
                        {
                            "file_path": str(path_obj),
                            "workflow_session_id": workflow_session_id,
                            "step_id": step_id,
                            "timestamp": __import__('time').time()
                        },
                        source="file_drop_handler"
                    )
                    debug_log(2, f"[FileDropHandler] 已發布 FILE_INPUT_PROVIDED 事件觸發工作流繼續")
                    info_log(f"[FileDropHandler] 檔案已提交到工作流 {workflow_session_id}")
            except Exception as e:
                error_log(f"[FileDropHandler] 檢查工作流狀態失敗: {e}")
            
        return True
    
    def _on_receive_animation_finish(self, animation_name: str):
        """receive 動畫結束時的回調"""
        # 檢查是否為 receive 動畫
        if not animation_name.startswith('receive_'):
            return
        
        info_log(f"[FileDropHandler] ✅ receive 動畫播放完畢: {animation_name}")
        
        # 清理狀態
        self._cleanup_file_interaction()

        # 退出靜態幀模式（若仍然存在）並恢復 idle 動畫（像 throw 一樣自然回復）
        try:
            if hasattr(self.coordinator, 'ani_module') and self.coordinator.ani_module and hasattr(self.coordinator.ani_module, 'manager'):
                mgr = self.coordinator.ani_module.manager
                if hasattr(mgr, 'static_frame_mode') and mgr.static_frame_mode:
                    mgr.exit_static_frame_mode()
                    debug_log(2, "[FileDropHandler] 已退出靜態幀模式（receive 完成）")
            # 恢復 idle 動畫（僅在真正 IDLE 狀態下）
            if hasattr(self.coordinator, 'current_behavior_state'):
                from ..core.state_machine import BehaviorState, MovementMode
                if self.coordinator.current_behavior_state == BehaviorState.IDLE:
                    is_ground = (MovementMode and hasattr(self.coordinator, 'movement_mode') and self.coordinator.movement_mode == MovementMode.GROUND)
                    if hasattr(self.coordinator, 'anim_query'):
                        idle_anim = self.coordinator.anim_query.get_idle_animation_for_mode(is_ground)
                        if idle_anim and hasattr(self.coordinator, '_trigger_anim'):
                            self.coordinator._trigger_anim(idle_anim, {"loop": True, "force_restart": False}, source="file_drop_handler")
                            debug_log(2, f"[FileDropHandler] 已恢復 idle 動畫: {idle_anim}")
        except Exception as e:
            error_log(f"[FileDropHandler] 恢復 idle 動畫失敗: {e}")
        
        # 移除自己的回調
        if hasattr(self.coordinator, 'ani_module') and self.coordinator.ani_module:
            try:
                if hasattr(self.coordinator.ani_module, '_finish_callbacks'):
                    self.coordinator.ani_module._finish_callbacks.remove(self._on_receive_animation_finish)
            except (ValueError, AttributeError):
                pass
    
    def _cleanup_file_interaction(self):
        """清理檔案互動狀態（行為機會自動恢復）"""
        debug_log(2, "[FileDropHandler] 清理檔案互動狀態")
        
        self._is_hovering = False
        self._is_receiving = False
        self._hover_animation_name = None

        # 不主動恢復追蹤；追蹤僅在後續靠近時自然啟動
        
        debug_log(2, "[FileDropHandler] 狀態已清除，行為機將自動恢復")
