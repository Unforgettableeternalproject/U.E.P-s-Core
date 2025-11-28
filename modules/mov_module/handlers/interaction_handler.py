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
    1. 檢測檔案拖曳到角色上
    2. 觸發接收檔案動畫（take_file）
    3. 將檔案路徑儲存到 WorkingContext
    """
    
    def can_handle(self, event: Any) -> bool:
        """判斷是否為檔案投放事件"""
        if not hasattr(event, 'event_type'):
            return False
        
        if UIEventType is None:
            return False
            
        return event.event_type == UIEventType.FILE_DROP
    
    def handle(self, event: Any) -> bool:
        """處理檔案投放事件
        
        Args:
            event: 可以是事件物件（有 .data 屬性）或字典（直接包含 file_path）
        """
        try:
            # 支持兩種格式：事件物件或字典
            if isinstance(event, dict):
                event_data = event
            else:
                event_data = event.data if hasattr(event, 'data') else {}
            
            file_path = event_data.get('file_path', '')
            
            if not file_path:
                error_log("[FileDropHandler] 檔案路徑為空")
                return False
            
            # 驗證檔案是否存在
            from pathlib import Path
            path_obj = Path(file_path)
            if not path_obj.exists():
                error_log(f"[FileDropHandler] 檔案不存在: {file_path}")
                return False
            
            info_log(f"[FileDropHandler] 接收檔案: {path_obj.name}")
            
            # 🎯 儲存檔案路徑到 WorkingContext（全局可訪問）
            try:
                from core.working_context import working_context_manager
                working_context_manager.set_context_data("current_file_path", str(path_obj))
                debug_log(2, f"[FileDropHandler] 檔案路徑已儲存到 WorkingContext: {path_obj}")
            except Exception as e:
                error_log(f"[FileDropHandler] 儲存檔案路徑到 WorkingContext 失敗: {e}")
                return False
            
            # 🎬 動畫由 LayerStrategy 在輸入層自動處理
            # 當有檔案時，輸入層會使用 take_file 而非 thinking
            debug_log(2, f"[FileDropHandler] 檔案已準備，輸入層將使用 take_file 動畫")
            
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
            
        except Exception as e:
            error_log(f"[FileDropHandler] 處理檔案投放事件失敗: {e}")
            return False
