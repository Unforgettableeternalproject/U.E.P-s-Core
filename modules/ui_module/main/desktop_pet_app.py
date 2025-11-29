# main/desktop_pet_app.py
"""
Desktop Pet Application

UEP 桌面寵物 Overlay 應用程式
提供主要的桌寵顯示和互動功能
"""

import os
import sys
import time
from typing import Dict, Any, Optional
from core.bases.frontend_base import UIEventType

try:
    from PyQt5.QtWidgets import QWidget, QLabel, QApplication
    from PyQt5.QtCore import Qt, QPoint, QTimer, pyqtSignal
    from PyQt5.QtGui import QPixmap, QPainter, QColor
    from PyQt5.QtWidgets import QDesktopWidget
except ImportError:
    # 創建模擬的 Qt 類別
    class MockQWidget:
        def __init__(self):
            self._x = 0
            self._y = 0
            self._width = 200
            self._height = 200
            self._visible = False
            self._opacity = 1.0
        
        def setWindowFlags(self, flags): pass
        def setAttribute(self, attr): pass
        def setFixedSize(self, width, height):
            print(f"MockQWidget.setFixedSize: width={width} (type={type(width)}), height={height} (type={type(height)})")
            self._width = int(width)
            self._height = int(height)
        def move(self, x, y):
            print(f"MockQWidget.move: x={x} (type={type(x)}), y={y} (type={type(y)})")
            self._x = int(x)
            self._y = int(y)
        def show(self):
            self._visible = True
        def hide(self):
            self._visible = False
        def update(self): pass
        def setWindowOpacity(self, opacity):
            print(f"MockQWidget.setWindowOpacity: opacity={opacity} (type={type(opacity)})")
            self._opacity = float(opacity)
        def x(self): return self._x
        def y(self): return self._y
        def width(self): return self._width
        def height(self): return self._height
        def isVisible(self): return self._visible
        def windowOpacity(self): return self._opacity
        def windowFlags(self): return 0
        def frameGeometry(self): return MockQPoint()
        
    class MockQPoint:
        def __init__(self, x=0, y=0):
            self._x = x
            self._y = y
        def x(self): return self._x
        def y(self): return self._y
        def __sub__(self, other):
            return MockQPoint(self._x - other._x, self._y - other._y)
    
    QWidget = MockQWidget
    QLabel = object
    QApplication = None
    Qt = None
    QPoint = MockQPoint
    QTimer = None
    pyqtSignal = None
    QPixmap = None
    QPainter = None
    QColor = None
    QDesktopWidget = None

# 添加項目根目錄到 Python 路徑
script_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.abspath(os.path.join(script_dir, '..', '..', '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from utils.debug_helper import debug_log, info_log, error_log

try:
    from core.bases.frontend_base import UIEventType
except Exception:
    class UIEventType:
        DRAG_START = "DRAG_START"
        DRAG_END = "DRAG_END"
        MOUSE_HOVER = "MOUSE_HOVER"
        FILE_DROP = "FILE_DROP"


class DesktopPetApp(QWidget):
    """
    主要桌面寵物應用程式
    
    特性：
    - 透明背景 Overlay 窗口
    - 支持拖拽移動
    - 支持動畫顯示
    - 鼠標互動響應
    """
    
    # 信號定義
    position_changed = pyqtSignal(int, int) if pyqtSignal else None
    clicked = pyqtSignal() if pyqtSignal else None
    state_changed = pyqtSignal(str) if pyqtSignal else None
    
    def __init__(self, ui_module=None, ani_module=None, mov_module=None):
        super().__init__()
        self.ui_module = ui_module
        
        # 直接接收初始化好的模組
        self.ani_module = ani_module
        self.mov_module = mov_module
        
        self.current_image = None
        self.is_dragging = False
        self.drag_position = QPoint() if QPoint else None
        # 基礎尺寸（zoom=1.0 時的視窗大小）
        self.base_size = (240, 240)
        self.default_size = self.base_size
        # 記錄當前的縮放比例，避免頻繁調整
        self.current_zoom = 1.0
        # 標記是否需要調整視窗大小
        self.pending_resize = None
        
        # 日誌頻率控制
        self._frame_update_log_counter = 0
        self._pause_log_counter = 0
        self._position_log_counter = 0
        self._scale_log_counter = 0
        self.LOG_INTERVAL = 100  # 每100次才輸出一次日誌
        
        # 添加定期檢查模組是否更新的計時器
        if QTimer:
            self.module_check_timer = QTimer(self)
            self.module_check_timer.timeout.connect(self.check_module_references)
            self.module_check_timer.start(5000)  # 每5秒檢查一次
            
            # 添加視窗調整計時器
            self.resize_timer = QTimer(self)
            self.resize_timer.timeout.connect(self._apply_pending_resize)
            self.resize_timer.setSingleShot(True)  # 單次觸發
            
            # 添加滑鼠追蹤計時器（事件驅動架構）
            self.cursor_tracking_timer = QTimer(self)
            self.cursor_tracking_timer.timeout.connect(self._check_cursor_tracking)
            self.cursor_tracking_timer.start(100)  # 降低到 10 FPS，減少性能消耗
        
        # 滑鼠追蹤狀態
        self._cursor_was_near = False  # 上一幀是否在追蹤範圍內
        self._last_cursor_angle = None  # 上一次的角度
        self._last_cursor_pos = None  # 上一次的滑鼠位置（用於檢測滑鼠移動）
        self._cursor_idle_time = 0.0  # 滑鼠靜止時間
        self._cursor_tracking_config = {
            'watch_radius': 300,      # 追蹤半徑
            'watch_radius_out': 330,  # 離開半徑（防抖動）
            'angle_threshold': 10.0,  # 角度變化閾值（度），低於此值不觸發更新
            'cursor_move_threshold': 5.0,  # 滑鼠移動閾值（像素），低於此值視為靜止
            'max_idle_time': 2.0,  # 滑鼠靜止超過此時間後停止追蹤（秒）
        }
        
        # 渲染控制
        self.rendering_paused = False
        self.pause_reason = ""
        self.pause_start_time = None  # 初始化暫停開始時間
        self.rendering_timeout_timer = None  # 超時保護計時器
        self.max_pause_duration = 3.0  # 最長暫停時間 (秒)
        
        # 狀態追蹤
        self.current_movement_mode = None
        self.current_animation_type = None
        
        # 動畫更新計時器
        self.animation_timer = QTimer(self) if QTimer else None
        if self.animation_timer:
            self.animation_timer.timeout.connect(self.update_animation_frame)
            # 從 user_settings 讀取 max_fps
            from configs.user_settings_manager import get_user_setting
            max_fps = get_user_setting("advanced.performance.max_fps", 60)
            interval_ms = int(1000 / max(max_fps, 1))
            self.animation_timer.start(interval_ms)
            debug_log(2, f"[DesktopPetApp] 動畫計時器已啟動: {max_fps} FPS ({interval_ms}ms)")
            
        # 設置超時保護計時器
        if QTimer:
            self.rendering_timeout_timer = QTimer(self)
            self.rendering_timeout_timer.timeout.connect(self.check_rendering_timeout)
            self.rendering_timeout_timer.setSingleShot(True)  # 設為單次觸發
        
        # 建立模組連接
        self.setup_module_connections()
        
        self.init_ui()
        
        info_log("[DesktopPetApp] 桌面寵物應用程式初始化完成")
    
    def setup_module_connections(self):
        """建立與ANI和MOV模組的連接"""
        try:
            # 設置ANI模組連接
            if self.ani_module:
                info_log("[DesktopPetApp] ANI 模組已連接")
                if hasattr(self.ani_module, "add_frame_callback"):
                    try:
                        self.ani_module.add_frame_callback(self.on_animation_frame_update)
                        debug_log(1, "[DesktopPetApp] 已註冊 ANI 幀回呼")
                    except Exception as e:
                        debug_log(2, f"[DesktopPetApp] 註冊 ANI 幀回呼失敗: {e}")
            else:
                info_log("[DesktopPetApp] ANI 模組未提供")
            
            # 設置MOV模組連接
            if self.mov_module:
                info_log("[DesktopPetApp] MOV 模組已連接")
                
                # 註冊位置更新回調
                if hasattr(self.mov_module, 'add_position_callback'):
                    self.mov_module.add_position_callback(self.on_movement_position_change)
                    debug_log(1, "[DesktopPetApp] 位置更新回調已註冊")
                
                # 設置移動模組的動畫回調
                if hasattr(self.mov_module, 'add_animation_callback'):
                    self.mov_module.add_animation_callback(self.on_movement_animation_request)
                    debug_log(1, "[DesktopPetApp] 動畫請求回調已註冊")
            else:
                info_log("[DesktopPetApp] MOV 模組未提供")

            if self.mov_module and self.ani_module:
                try:
                    if hasattr(self.mov_module, "attach_ani"):
                        self.mov_module.attach_ani(self.ani_module)
                    else:
                        self.mov_module.handle_frontend_request({
                            "command": "inject_ani",
                            "ani": self.ani_module
                        })
                    debug_log(1, "[DesktopPetApp] MOV 已注入 ANI")
                except Exception as e:
                    error_log(f"[DesktopPetApp] 注入 ANI 到 MOV 失敗: {e}")
                
        except Exception as e:
            error_log(f"[DesktopPetApp] 模組連接設置失敗: {e}")

    def update_animation_frame(self):
        """更新動畫幀"""
        try:
            if self.rendering_paused:
                # 降低日誌頻率：每100次才輸出一次
                self._pause_log_counter += 1
                if self._pause_log_counter >= self.LOG_INTERVAL:
                    debug_log(3, f"[DesktopPetApp] 渲染已暫停: {self.pause_reason}")
                    self._pause_log_counter = 0
                return

            if not self.ani_module:
                return

            # 只有在 ANI 有這個方法時才調用，避免 AttributeError
            if hasattr(self.ani_module, "get_current_frame"):
                current_frame = self.ani_module.get_current_frame()
                if current_frame:
                    self.current_image = current_frame
                    self.update()
                    # 降低日誌頻率：每100次才輸出一次
                    self._frame_update_log_counter += 1
                    if self._frame_update_log_counter >= self.LOG_INTERVAL:
                        debug_log(3, "[DesktopPetApp] 成功更新動畫幀")
                        self._frame_update_log_counter = 0
            # 若沒有，靠回呼機制推動即可（這裡就不做事）
        except Exception as e:
            debug_log(2, f"[DesktopPetApp] 動畫幀更新異常: {e}")
    
    def pause_rendering(self, reason=""):
        """暫停渲染"""
        # 檢查是否已經暫停，避免重複暫停
        if self.rendering_paused:
            debug_log(2, f"[DesktopPetApp] 已經暫停渲染，忽略暫停請求: {reason}")
            return
            
        self.rendering_paused = True
        self.pause_reason = reason
        self.pause_start_time = time.time()  # 記錄暫停開始時間
        debug_log(2, f"[DesktopPetApp] 暫停渲染: {reason}")
        
        # 啟動超時保護計時器
        if self.rendering_timeout_timer:
            self.rendering_timeout_timer.start(int(self.max_pause_duration * 1000))
    
    def resume_rendering(self):
        """恢復渲染"""
        if not self.rendering_paused:
            debug_log(2, "[DesktopPetApp] 渲染未暫停，忽略恢復請求")
            return
            
        self.rendering_paused = False
        pause_duration = time.time() - getattr(self, 'pause_start_time', time.time())
        self.pause_reason = ""
        debug_log(2, f"[DesktopPetApp] 恢復渲染，暫停持續了 {pause_duration:.2f} 秒")
        
        # 停止超時保護計時器
        if self.rendering_timeout_timer and self.rendering_timeout_timer.isActive():
            self.rendering_timeout_timer.stop()
        
        # 確保MOV模組也解除暫停
        self.ensure_mov_module_resumed()
    
    def check_rendering_timeout(self):
        """檢查渲染暫停是否超時"""
        if self.rendering_paused:
            pause_duration = time.time() - getattr(self, 'pause_start_time', time.time())
            debug_log(2, f"[DesktopPetApp] 渲染暫停超時保護觸發! 已暫停 {pause_duration:.2f} 秒")
            self.resume_rendering()
    
    def ensure_mov_module_resumed(self):
        """確保MOV模組解除暫停"""
        try:
            if self.mov_module:
                # 檢查MOV模組是否有暫停狀態
                if hasattr(self.mov_module, 'movement_paused') and self.mov_module.movement_paused:
                    debug_log(2, "[DesktopPetApp] 檢測到MOV模組仍在暫停狀態，嘗試恢復")
                    # 嘗試呼叫恢復方法
                    if hasattr(self.mov_module, 'resume_movement'):
                        self.mov_module.resume_movement("DesktopPetApp自動恢復")
                        debug_log(2, "[DesktopPetApp] 已強制恢復MOV模組")
                
                # 如果是處於轉換狀態，可能需要額外處理
                if hasattr(self.mov_module, 'is_transitioning') and self.mov_module.is_transitioning:
                    current_time = time.time()
                    if hasattr(self.mov_module, 'transition_start_time') and \
                       hasattr(self.mov_module, '_handle_state_transition'):
                        # 如果轉換開始時間超過3秒，強制完成轉換
                        if current_time - self.mov_module.transition_start_time > 3.0:
                            debug_log(2, "[DesktopPetApp] 檢測到轉換狀態超時，強制處理")
                            self.mov_module._handle_state_transition(current_time + 100)  # 傳入一個未來時間強制完成
        except Exception as e:
            error_log(f"[DesktopPetApp] 確保MOV模組恢復時出錯: {e}")
    
    def handle_mov_state_change(self, event_type, data):
        """處理MOV模組的狀態變更"""
        debug_log(1, f"[DesktopPetApp] 收到MOV狀態變更: {event_type}, 數據: {data}")

        if event_type == "transition_start":
            # 不要 pause_rendering，否則轉場幀出不來
            self.current_transition = f"{data.get('from')} -> {data.get('to')}"
            debug_log(1, f"[DesktopPetApp] 狀態轉換中（保持渲染），{self.current_transition}")
        elif event_type == "transition_complete":
            current_state = data.get('current_state', '')
            debug_log(1, f"[DesktopPetApp] 狀態轉換完成，當前狀態: {current_state}")
            if getattr(self, 'rendering_paused', False):
                self.resume_rendering()
            if hasattr(self, 'current_transition'):
                delattr(self, 'current_transition')

        
    def on_movement_animation_request(self, animation_type: str, params: dict):
        """處理來自 MOV 模組的動畫請求，轉回去"""
        try:
            if not self.mov_module:
                return
            self.mov_module.handle_frontend_request({
                "command": "play_animation",
                "name": animation_type,
                "params": params or {}
            })
        except Exception as e:
            error_log(f"[DesktopPetApp] 轉交 MOV 動畫請求失敗: {animation_type}, 錯誤: {e}")
    
    def init_ui(self):
        """初始化 UI"""
        try:
            # 檢查是否為真正的 Qt 還是模擬版本
            if hasattr(self, 'setWindowFlags') and Qt:
                # 真正的 PyQt5
                from PyQt5.QtCore import Qt as QtCore
                
                self.setWindowFlags(
                    QtCore.FramelessWindowHint |           # 無邊框
                    QtCore.WindowStaysOnTopHint |          # 置頂
                    QtCore.Tool                            # 工具窗口
                )
                
                self.setAttribute(QtCore.WA_TranslucentBackground)
                self.setFixedSize(*self.default_size)
                
                # 啟用拖放功能（必須在設置窗口標誌之後）
                self.setAcceptDrops(True)
                debug_log(2, "[DesktopPetApp] 已啟用檔案拖放功能")
                
                # 注意：不在這裡設置初始位置，由 MOV 模組的入場動畫控制
                # self.center_on_screen()  # 已註解，避免覆蓋 MOV 模組的位置設定
            else:
                # 模擬版本
                self.setFixedSize(*self.default_size)
            
            # 載入默認圖片
            self.load_default_image()
                
            info_log("[DesktopPetApp] UI 初始化完成")
        except Exception as e:
            error_log(f"[DesktopPetApp] UI 初始化異常: {e}")
            # 使用基本設置
            self.setFixedSize(*self.default_size)
    
    def center_on_screen(self):
        """將窗口置中到螢幕"""
        try:
            if QDesktopWidget:
                screen = QDesktopWidget().screenGeometry()
                x = (screen.width() - self.width()) // 2
                y = (screen.height() - self.height()) // 2
                self.move(x, y)
            else:
                # 模擬版本：使用預設位置
                self.move(300, 300)
        except Exception as e:
            error_log(f"[DesktopPetApp] 置中螢幕異常: {e}")
            self.move(300, 300)
    
    def load_default_image(self):
        """載入默認圖片"""
        try:
            # 尋找 default.png 檔案
            default_image_paths = [
                "resources/assets/static/default.png",
                os.path.join(os.path.dirname(__file__), "../../../resources/assets/static/default.png"),
                os.path.join(os.getcwd(), "resources/assets/static/default.png")
            ]
            
            default_image_path = None
            for path in default_image_paths:
                if os.path.exists(path):
                    default_image_path = path
                    break
            
            if default_image_path:
                if self.set_image(default_image_path):
                    info_log(f"[DesktopPetApp] 已載入默認圖片: {default_image_path}")
                    return True
                else:
                    error_log(f"[DesktopPetApp] 載入默認圖片失敗: {default_image_path}")
            else:
                error_log("[DesktopPetApp] 找不到 default.png 檔案")
                
            return False
        except Exception as e:
            error_log(f"[DesktopPetApp] 載入默認圖片異常: {e}")
            return False
    
    def paintEvent(self, event):
        """繪製事件"""
        if not QPainter or not self.current_image:
            return
            
        try:
            painter = QPainter(self)
            painter.setRenderHint(QPainter.Antialiasing)
            
            # 繪製圖片 - 智能視窗大小調整
            if self.current_image:
                # 從 ANI 模組獲取當前動畫的縮放信息
                zoom_factor = 1.0
                if self.ani_module:
                    try:
                        status = self.ani_module.get_current_animation_status()
                        if status and status.get("is_playing"):
                            zoom_factor = status.get("zoom", 1.0)
                    except Exception as e:
                        debug_log(3, f"[DesktopPetApp] 無法獲取縮放信息: {e}")
                
                # 計算基於縮放比例的視窗大小
                target_width = int(self.base_size[0] * zoom_factor)
                target_height = int(self.base_size[1] * zoom_factor)
                
                # 檢查是否需要調整視窗大小
                current_width = self.width()
                current_height = self.height()
                
                if (abs(target_width - current_width) > 5 or 
                    abs(target_height - current_height) > 5 or 
                    abs(zoom_factor - self.current_zoom) > 0.05):
                    
                    # 使用延遲調整避免遞歸繪製
                    self.pending_resize = (target_width, target_height, zoom_factor)
                    if not self.resize_timer.isActive():
                        self.resize_timer.start(10)  # 10ms 延遲
                    debug_log(3, f"[DesktopPetApp] 排程視窗調整: zoom={zoom_factor:.2f}, 尺寸={target_width}x{target_height}")
                
                # 將圖片縮放至視窗大小，保持寬高比
                scaled_image = self.current_image.scaled(
                    self.size(), 
                    Qt.KeepAspectRatio,  # 保持寬高比
                    Qt.SmoothTransformation
                )
                
                # 居中繪製
                x = (self.width() - scaled_image.width()) // 2
                y = (self.height() - scaled_image.height()) // 2
                painter.drawPixmap(x, y, scaled_image)
                
                # 使用計數器減少日誌頻率
                self._scale_log_counter += 1
                if self._scale_log_counter >= self.LOG_INTERVAL:
                    debug_log(3, f"[DesktopPetApp] 比例縮放: zoom={zoom_factor:.2f}, 圖片={scaled_image.width()}x{scaled_image.height()}, 視窗={self.width()}x{self.height()}")
                    self._scale_log_counter = 0
        except Exception as e:
            error_log(f"[DesktopPetApp] 繪製事件異常: {e}")
    
    def _apply_pending_resize(self):
        """延遲執行視窗大小調整，避免在 paintEvent 中直接調整造成遞歸"""
        if not self.pending_resize:
            return
            
        try:
            target_width, target_height, zoom_factor = self.pending_resize
            
            # 計算當前視窗中心位置
            current_center_x = self.x() + self.width() // 2
            current_center_y = self.y() + self.height() // 2
            
            # 調整視窗大小
            self.setFixedSize(target_width, target_height)
            
            # 計算新的左上角位置，使視窗中心保持不變
            new_x = current_center_x - target_width // 2
            new_y = current_center_y - target_height // 2
            
            # 確保視窗不會跑到螢幕外（簡單的邊界檢查）
            new_x = max(0, min(new_x, 1920 - target_width))
            new_y = max(0, min(new_y, 1080 - target_height))
            
            self.move(new_x, new_y)
            self.current_zoom = zoom_factor
            self.pending_resize = None
            
            debug_log(2, f"[DesktopPetApp] 延遲調整視窗: zoom={zoom_factor:.2f}, 尺寸={target_width}x{target_height}, 位置=({new_x},{new_y})")
            
        except Exception as e:
            error_log(f"[DesktopPetApp] 延遲視窗調整失敗: {e}")
            self.pending_resize = None
    
    def mousePressEvent(self, event):
        """鼠標按下事件"""
        try:
            if Qt and hasattr(event, 'button') and event.button() == Qt.LeftButton:
                self.is_dragging = True
                if QPoint and hasattr(event, 'globalPos'):
                    self.drag_position = event.globalPos() - self.frameGeometry().topLeft()
                
                # 拖曳時不暫停渲染，讓struggle動畫能正常播放
                # self.pause_rendering("滑鼠拖拽")  # 註解掉這行
                
                # 通知MOV模組拖拽開始
                if self.mov_module and hasattr(self.mov_module, 'handle_ui_event'):
                    self.mov_module.handle_ui_event(
                        UIEventType.DRAG_START,
                        {
                            "start_position": {
                                "x": event.globalX(),
                                "y": event.globalY(),
                            }
                        }
                    )
                
                # 發射點擊信號
                if self.clicked:
                    self.clicked.emit()
        except Exception as e:
            error_log(f"[DesktopPetApp] 鼠標按下事件異常: {e}")
    
    def mouseMoveEvent(self, event):
        """鼠標移動事件"""
        try:
            if Qt and self.is_dragging and hasattr(event, 'buttons') and event.buttons() == Qt.LeftButton:
                if QPoint and hasattr(event, 'globalPos'):
                    new_pos = event.globalPos() - self.drag_position
                    self.move(new_pos.x(), new_pos.y())
                    if self.position_changed:
                        self.position_changed.emit(new_pos.x(), new_pos.y())

                    # 使用DRAG_MOVE事件通知MOV模組（而不是直接設置位置）
                    if self.mov_module:
                        if hasattr(self.mov_module, 'handle_ui_event'):
                            # 優先使用事件系統
                            self.mov_module.handle_ui_event(
                                UIEventType.DRAG_MOVE,
                                {
                                    "x": new_pos.x(),
                                    "y": new_pos.y(),
                                    "global_pos": (event.globalX(), event.globalY())
                                }
                            )
                        elif hasattr(self.mov_module, 'handle_frontend_request'):
                            # 備用：直接API調用（拖曳時應該被正確處理）
                            self.mov_module.handle_frontend_request({
                                "command": "set_position",
                                "x": new_pos.x(),
                                "y": new_pos.y()
                            })
        except Exception as e:
            error_log(f"[DesktopPetApp] 鼠標移動事件異常: {e}")
        
    def mouseReleaseEvent(self, event):
        """鼠標釋放事件"""
        try:
            if Qt and hasattr(event, 'button') and event.button() == Qt.LeftButton:
                self.is_dragging = False
                
                # 通知MOV模組拖拽結束
                if self.mov_module and hasattr(self.mov_module, 'handle_ui_event'):
                    self.mov_module.handle_ui_event(UIEventType.DRAG_END, {
                        "global_pos": (event.globalX(), event.globalY())
                    })
                
                # 由於拖曳時不再暫停渲染，所以不需要恢復
                # self.resume_rendering()  # 註解掉這行
                
        except Exception as e:
            error_log(f"[DesktopPetApp] 鼠標釋放事件異常: {e}")
    
    def set_image(self, image_path: str):
        """設置顯示圖片"""
        try:
            if os.path.exists(image_path):
                if QPixmap:
                    self.current_image = QPixmap(image_path)
                    self.update()  # 觸發重繪
                    debug_log(2, f"[DesktopPetApp] 已設置圖片: {image_path}")
                    return True
                else:
                    # 模擬版本：僅記錄圖片路徑
                    debug_log(2, f"[DesktopPetApp] 已設置圖片 (模擬): {image_path}")
                    return True
            else:
                error_log(f"[DesktopPetApp] 圖片檔案不存在: {image_path}")
                return False
        except Exception as e:
            error_log(f"[DesktopPetApp] 設置圖片異常: {e}")
            return False
    
    def set_size(self, width: int, height: int):
        """設置窗口大小"""
        try:
            # 記錄原始參數
            debug_log(1, f"[DesktopPetApp] set_size 收到參數: width={width} (型別: {type(width)}), height={height} (型別: {type(height)})")
            
            # 確保 width 和 height 是整數類型
            width_value = int(width)
            height_value = int(height)
            debug_log(1, f"[DesktopPetApp] 轉換後: width={width_value}, height={height_value}")
            
            self.setFixedSize(width_value, height_value)
            self.update()
            debug_log(3, f"[DesktopPetApp] 已設置大小: {width_value}x{height_value}")
        except (ValueError, TypeError) as e:
            error_log(f"[DesktopPetApp] 尺寸值無效: width={width}, height={height}, 錯誤: {e}")
            # 使用預設值
            self.setFixedSize(*self.default_size)
    
    def set_position(self, x: int, y: int):
        """設置窗口位置"""
        try:
            # 確保 x 和 y 是整數類型
            x_value = int(x)
            y_value = int(y)
            self.move(x_value, y_value)
            
            # 使用計數器減少日誌頻率
            self._position_log_counter += 1
            if self._position_log_counter >= self.LOG_INTERVAL:
                debug_log(3, f"[DesktopPetApp] 已設置位置: ({x_value}, {y_value})")
                self._position_log_counter = 0
        except (ValueError, TypeError) as e:
            error_log(f"[DesktopPetApp] 位置值無效: x={x}, y={y}, 錯誤: {e}")
            # 保持當前位置
    
    def set_opacity(self, opacity: float):
        """設置透明度 (0.0-1.0)"""
        try:
            # 記錄原始參數型別和值
            debug_log(1, f"[DesktopPetApp] set_opacity 收到參數: {opacity}, 型別: {type(opacity)}")
            
            # 確保 opacity 是數字類型
            opacity_value = float(opacity)
            debug_log(1, f"[DesktopPetApp] 轉換後 opacity_value: {opacity_value}, 型別: {type(opacity_value)}")
            
            # 限制範圍並設置
            final_opacity = max(0.0, min(1.0, opacity_value))
            debug_log(1, f"[DesktopPetApp] 最終 opacity: {final_opacity}")
            
            self.setWindowOpacity(final_opacity)
            debug_log(1, f"[DesktopPetApp] 已設置透明度: {final_opacity}")
        except (ValueError, TypeError) as e:
            error_log(f"[DesktopPetApp] 透明度值無效: {opacity}, 錯誤: {e}")
            # 使用預設值
            self.setWindowOpacity(1.0)
    
    def set_always_on_top(self, always_on_top: bool):
        """設置視窗置頂狀態"""
        try:
            if Qt and hasattr(self, 'setWindowFlags'):
                if always_on_top:
                    self.setWindowFlags(self.windowFlags() | Qt.WindowStaysOnTopHint)
                else:
                    self.setWindowFlags(self.windowFlags() & ~Qt.WindowStaysOnTopHint)
                self.show()  # 重新顯示視窗以應用新的標誌
            debug_log(1, f"[DesktopPetApp] 已設置置頂狀態: {always_on_top}")
        except Exception as e:
            error_log(f"[DesktopPetApp] 設置置頂狀態異常: {e}")
    
    # === MOV/ANI 模組整合方法 ===
    
    def on_movement_position_change(self, x, y):
        """處理來自 MOV 模組的位置變更"""
        try:
            self.set_position(x, y)
            # 日誌已在 set_position 內部處理
        except Exception as e:
            error_log(f"[DesktopPetApp] 處理位置變更失敗: {e}")
    
    def on_movement_mode_change(self, mode):
        """處理來自 MOV 模組的移動模式變更"""
        try:
            self.current_movement_mode = mode
            debug_log(1, f"[DesktopPetApp] 移動模式變更: {mode}")
        except Exception as e:
            error_log(f"[DesktopPetApp] 處理移動模式變更失敗: {e}")
    
    def on_animation_change(self, animation_type):
        """處理來自 ANI 模組的動畫變更"""
        try:
            self.current_animation_type = animation_type
            debug_log(1, f"[DesktopPetApp] 動畫類型變更: {animation_type}")
        except Exception as e:
            error_log(f"[DesktopPetApp] 處理動畫變更失敗: {e}")
    
    def on_animation_frame_update(self, frame_data):
        """
        ANI 主動推送幀時會回來這裡。
        允許 frame_data 有 'pixmap' 或 'image_path' 兩種格式。
        """
        try:
            if self.rendering_paused:
                return

            pm = None
            if isinstance(frame_data, dict):
                pm = frame_data.get("pixmap")
                if pm is None:
                    image_path = frame_data.get("image_path")
                    if image_path:
                        from PyQt5.QtGui import QPixmap
                        pm = QPixmap(image_path)

            if pm is not None:
                self.current_image = pm
                self.update()
            else:
                debug_log(2, "[DesktopPetApp] 收到 ANI 幀，但缺少 pixmap/image_path")
        except Exception as e:
            debug_log(2, f"[DesktopPetApp] 處理 ANI 幀回呼失敗: {e}")
    
    # === 對外提供的控制方法 ===
    
    def check_module_references(self):
        """定期檢查模組引用是否已更新"""
        try:
            debug_log(1, "[DesktopPetApp] 檢查模組引用是否已更新")
            
            # 檢查渲染是否卡住
            if self.rendering_paused and hasattr(self, 'pause_start_time'):
                pause_duration = time.time() - self.pause_start_time
                if pause_duration > self.max_pause_duration:
                    debug_log(1, f"[DesktopPetApp] 檢測到渲染暫停超過 {self.max_pause_duration} 秒，強制恢復")
                    self.resume_rendering()
            
            # 匯入 debug_api 以檢查當前的模組引用
            try:
                import devtools.debug_api as debug_api
                if not hasattr(debug_api, 'modules'):
                    return
                
                # 檢查 ANI 模組
                current_ani = debug_api.modules.get('ani')
                if current_ani is not None and current_ani is not self.ani_module:
                    debug_log(1, "[DesktopPetApp] 偵測到 ANI 模組已被重新載入，更新引用")
                    self.ani_module = current_ani
                
                # 檢查 MOV 模組
                current_mov = debug_api.modules.get('mov')
                if current_mov is not None and current_mov is not self.mov_module:
                    debug_log(1, "[DesktopPetApp] 偵測到 MOV 模組已被重新載入，更新引用")
                    self.mov_module = current_mov
                    
                    # 重新註冊回調
                    if hasattr(self.mov_module, 'add_position_callback'):
                        self.mov_module.add_position_callback(self.on_movement_position_change)
                        debug_log(1, "[DesktopPetApp] 位置更新回調已重新註冊")
                    
                    if hasattr(self.mov_module, 'add_animation_callback'):
                        self.mov_module.add_animation_callback(self.on_movement_animation_request)
                        debug_log(1, "[DesktopPetApp] 動畫請求回調已重新註冊")
                
            except ImportError:
                debug_log(1, "[DesktopPetApp] 無法匯入 debug_api")
                
        except Exception as e:
            error_log(f"[DesktopPetApp] 檢查模組引用時出錯: {e}")
    
    def set_movement_mode(self, mode):
        """設置移動模式"""
        try:
            if self.mov_module and hasattr(self.mov_module, 'set_movement_mode'):
                self.mov_module.set_movement_mode(mode)
                debug_log(1, f"[DesktopPetApp] 設置移動模式: {mode}")
                return True
            else:
                debug_log(1, f"[DesktopPetApp] MOV 模組不可用，無法設置移動模式")
                return False
        except Exception as e:
            error_log(f"[DesktopPetApp] 設置移動模式失敗: {e}")
            return False
    
    def set_animation_type(self, animation_type):
        """設置動畫類型"""
        try:
            if self.ani_module and hasattr(self.ani_module, 'set_animation'):
                self.ani_module.set_animation(animation_type)
                debug_log(1, f"[DesktopPetApp] 設置動畫類型: {animation_type}")
                return True
            else:
                debug_log(1, f"[DesktopPetApp] ANI 模組不可用，無法設置動畫類型")
                return False
        except Exception as e:
            error_log(f"[DesktopPetApp] 設置動畫類型失敗: {e}")
            return False
    
    def start_automatic_movement(self):
        """啟動自動移動"""
        try:
            if self.mov_module and hasattr(self.mov_module, 'start_auto_movement'):
                self.mov_module.start_auto_movement()
                debug_log(1, f"[DesktopPetApp] 啟動自動移動")
                return True
            else:
                debug_log(1, f"[DesktopPetApp] MOV 模組不可用，無法啟動自動移動")
                return False
        except Exception as e:
            error_log(f"[DesktopPetApp] 啟動自動移動失敗: {e}")
            return False
    
    def stop_automatic_movement(self):
        """停止自動移動"""
        try:
            if self.mov_module and hasattr(self.mov_module, 'stop_auto_movement'):
                self.mov_module.stop_auto_movement()
                debug_log(1, f"[DesktopPetApp] 停止自動移動")
                return True
            else:
                debug_log(1, f"[DesktopPetApp] MOV 模組不可用，無法停止自動移動")
                return False
        except Exception as e:
            error_log(f"[DesktopPetApp] 停止自動移動失敗: {e}")
            return False
    
    def handle_request(self, data: dict) -> dict:
        """處理來自 UI 模組的請求"""
        try:
            command = data.get('command')
            
            if command == 'show_window':
                self.show()
                return {"success": True, "message": "桌面寵物已顯示"}
            
            elif command == 'hide_window':
                self.hide()
                return {"success": True, "message": "桌面寵物已隱藏"}
            
            elif command == 'move_window':
                x = data.get('x', self.x())
                y = data.get('y', self.y())
                self.set_position(x, y)
                return {"success": True, "position": {"x": int(x), "y": int(y)}}
            
            elif command == 'set_image':
                image_path = data.get('image_path')
                if image_path:
                    success = self.set_image(image_path)
                    return {"success": success, "image_path": image_path}
                return {"error": "需要提供 image_path 參數"}
            
            elif command == 'set_window_size':
                width = data.get('width', self.width())
                height = data.get('height', self.height())
                self.set_size(width, height)
                return {"success": True, "size": {"width": int(width), "height": int(height)}}
            
            elif command == 'set_opacity':
                opacity = data.get('opacity', 1.0)
                self.set_opacity(opacity)
                return {"success": True, "opacity": float(opacity)}
            
            elif command == 'set_always_on_top':
                always_on_top = data.get('always_on_top', True)
                self.set_always_on_top(always_on_top)
                return {"success": True, "always_on_top": always_on_top}
            
            elif command == 'get_window_info':
                return {
                    "position": {"x": self.x(), "y": self.y()},
                    "size": {"width": self.width(), "height": self.height()},
                    "visible": self.isVisible(),
                    "opacity": self.windowOpacity() if hasattr(self, 'windowOpacity') else 1.0,
                    "movement_mode": str(self.current_movement_mode) if self.current_movement_mode else None,
                    "animation_type": str(self.current_animation_type) if self.current_animation_type else None
                }
            
            # === MOV/ANI 控制命令 ===
            elif command == 'set_movement_mode':
                mode = data.get('mode')
                if mode:
                    success = self.set_movement_mode(mode)
                    return {"success": success, "movement_mode": mode}
                return {"error": "需要提供 mode 參數"}
            
            elif command == 'set_animation_type':
                animation_type = data.get('animation_type')
                if animation_type:
                    success = self.set_animation_type(animation_type)
                    return {"success": success, "animation_type": animation_type}
                return {"error": "需要提供 animation_type 參數"}
            
            elif command == 'start_auto_movement':
                success = self.start_automatic_movement()
                return {"success": success, "message": "自動移動已啟動" if success else "啟動自動移動失敗"}
            
            elif command == 'stop_auto_movement':
                success = self.stop_automatic_movement()
                return {"success": success, "message": "自動移動已停止" if success else "停止自動移動失敗"}
            
            elif command == 'get_movement_status':
                return {
                    "movement_mode": str(self.current_movement_mode) if self.current_movement_mode else None,
                    "animation_type": str(self.current_animation_type) if self.current_animation_type else None,
                    "mov_module_available": self.mov_module is not None,
                    "ani_module_available": self.ani_module is not None
                }
            
            else:
                return {"error": f"未知命令: {command}"}
                
        except Exception as e:
            error_log(f"[DesktopPetApp] 處理請求異常: {e}")
            return {"error": str(e)}
    
    def close(self):
        """關閉桌面寵物應用程式，停止所有計時器和清理資源"""
        info_log("[DesktopPetApp] 正在關閉桌面寵物應用程式")
        
        # 停止所有計時器
        if hasattr(self, 'module_check_timer') and self.module_check_timer:
            self.module_check_timer.stop()
            info_log("[DesktopPetApp] 模組檢查計時器已停止")
            
        if hasattr(self, 'resize_timer') and self.resize_timer:
            self.resize_timer.stop()
            info_log("[DesktopPetApp] 視窗調整計時器已停止")
            
        if hasattr(self, 'animation_timer') and self.animation_timer:
            self.animation_timer.stop()
            info_log("[DesktopPetApp] 動畫計時器已停止")
            
        if hasattr(self, 'rendering_timeout_timer') and self.rendering_timeout_timer:
            self.rendering_timeout_timer.stop()
            info_log("[DesktopPetApp] 渲染超時計時器已停止")
        
        # 清理模組引用
        self.ui_module = None
        self.ani_module = None
        self.mov_module = None
        
        # 發出狀態變更信號
        if self.state_changed:
            self.state_changed.emit("closed")
        
        # 斷開所有信號連接
        try:
            if hasattr(self, 'position_changed') and self.position_changed:
                self.position_changed.disconnect()
            if hasattr(self, 'clicked') and self.clicked:
                self.clicked.disconnect()
            if hasattr(self, 'state_changed') and self.state_changed:
                self.state_changed.disconnect()
        except Exception as e:
            error_log(f"[DesktopPetApp] 斷開信號連接時發生錯誤: {e}")
        
        # 隱藏並關閉視窗
        if hasattr(self, 'hide'):
            self.hide()
        
        # 刪除所有計時器對象
        try:
            if hasattr(self, 'module_check_timer'):
                self.module_check_timer.deleteLater()
                self.module_check_timer = None
            if hasattr(self, 'resize_timer'):
                self.resize_timer.deleteLater()
                self.resize_timer = None
            if hasattr(self, 'animation_timer'):
                self.animation_timer.deleteLater() 
                self.animation_timer = None
            if hasattr(self, 'rendering_timeout_timer') and self.rendering_timeout_timer:
                self.rendering_timeout_timer.deleteLater()
                self.rendering_timeout_timer = None
            if hasattr(self, 'cursor_tracking_timer'):
                self.cursor_tracking_timer.deleteLater()
                self.cursor_tracking_timer = None
        except Exception as e:
            error_log(f"[DesktopPetApp] 刪除計時器時發生錯誤: {e}")
            
        # 調用父類的close方法
        try:
            if hasattr(super(), 'close'):
                result = super().close()
            else:
                result = True
        except Exception as e:
            error_log(f"[DesktopPetApp] 調用父類close方法失敗: {e}")
            result = True
        
        # 標記自己為已刪除狀態（用於調試）
        self._is_closed = True
        
        info_log("[DesktopPetApp] 桌面寵物應用程式已完全關閉")
        return result
    
    def closeEvent(self, event):
        """窗口關閉事件"""
        info_log("[DesktopPetApp] 收到窗口關閉事件")
        self.close()
        event.accept()
    
    # ========== 滑鼠追蹤（事件驅動架構）==========
    
    def _check_cursor_tracking(self):
        """
        檢查滑鼠追蹤狀態並發送事件給 MOV 模組
        
        使用前端事件總線（FrontendEventBus）減少直接調用開銷
        
        職責：
        1. 計算滑鼠到角色中心的距離
        2. 判斷是否進入/離開追蹤範圍
        3. 透過事件總線發送事件（降低延遲）
        """
        try:
            # 拖曳時不追蹤
            if self.is_dragging:
                return
            
            # MOV 模組必須存在
            if not self.mov_module or not hasattr(self.mov_module, 'handle_cursor_tracking_event'):
                return
            
            # 獲取滑鼠位置（使用 PyQt 的 QCursor）
            try:
                from PyQt5.QtGui import QCursor
                cursor_pos = QCursor.pos()
            except ImportError:
                return
            
            # 檢查滑鼠是否移動
            import math
            cursor_moved = False
            if self._last_cursor_pos is not None:
                cursor_dx = cursor_pos.x() - self._last_cursor_pos.x()
                cursor_dy = cursor_pos.y() - self._last_cursor_pos.y()
                cursor_move_dist = math.hypot(cursor_dx, cursor_dy)
                
                move_threshold = self._cursor_tracking_config['cursor_move_threshold']
                if cursor_move_dist >= move_threshold:
                    cursor_moved = True
                    self._cursor_idle_time = 0.0  # 重置靜止時間
                else:
                    self._cursor_idle_time += 0.1  # 增加靜止時間（timer 間隔為 100ms）
            else:
                # 第一次檢測，記錄位置
                self._cursor_idle_time = 0.0
            
            self._last_cursor_pos = cursor_pos
            
            # 如果滑鼠靜止太久，停止追蹤
            max_idle_time = self._cursor_tracking_config['max_idle_time']
            if self._cursor_idle_time >= max_idle_time and self._cursor_was_near:
                # 滑鼠靜止超時，離開追蹤模式
                self.mov_module.handle_cursor_tracking_event({
                    "type": "cursor_far",
                    "distance": 0  # 距離不重要
                })
                self._cursor_was_near = False
                self._last_cursor_angle = None
                debug_log(2, f"[DesktopPetApp] 滑鼠靜止超時，停止追蹤")
                return
            
            # 計算角色中心
            pet_center_x = self.x() + self.width() // 2
            pet_center_y = self.y() + self.height() // 2
            
            # 計算距離
            dx = cursor_pos.x() - pet_center_x
            dy = cursor_pos.y() - pet_center_y
            distance = math.hypot(dx, dy)
            
            # 計算角度（0° = 右，90° = 上，180° = 左，270° = 下）
            angle_rad = math.atan2(-dy, dx)  # 螢幕 y+ 向下，反轉
            angle_deg = (math.degrees(angle_rad) + 360) % 360
            
            # 判斷是否在追蹤範圍內
            watch_radius = self._cursor_tracking_config['watch_radius']
            watch_radius_out = self._cursor_tracking_config['watch_radius_out']
            
            is_near_now = distance <= watch_radius
            
            # 使用滯後半徑防止抖動
            if self._cursor_was_near:
                is_near_now = distance <= watch_radius_out
            
            # 檢測進入/離開事件（只有在滑鼠移動時才觸發進入事件）
            if is_near_now and not self._cursor_was_near:
                # 進入追蹤範圍 - 但只有在滑鼠有移動時才觸發
                if cursor_moved or self._cursor_idle_time < 0.5:  # 0.5秒內的移動視為有效
                    self.mov_module.handle_cursor_tracking_event({
                        "type": "cursor_near",
                        "distance": distance,
                        "angle": angle_deg
                    })
                    self._last_cursor_angle = angle_deg
                    debug_log(2, f"[DesktopPetApp] 滑鼠進入追蹤範圍，距離={distance:.1f}px，角度={angle_deg:.1f}°")
                else:
                    # 滑鼠沒有移動，是 UEP 自己移動過來的，不觸發追蹤
                    debug_log(3, f"[DesktopPetApp] 滑鼠未移動，跳過追蹤觸發（距離={distance:.1f}px）")
                    return
                
            elif not is_near_now and self._cursor_was_near:
                # 離開追蹤範圍
                self.mov_module.handle_cursor_tracking_event({
                    "type": "cursor_far",
                    "distance": distance
                })
                self._last_cursor_angle = None
                debug_log(2, f"[DesktopPetApp] 滑鼠離開追蹤範圍，距離={distance:.1f}px")
            
            elif is_near_now:
                # 在追蹤範圍內，只有角度變化超過閾值才更新
                angle_threshold = self._cursor_tracking_config['angle_threshold']
                
                if self._last_cursor_angle is None:
                    # 首次更新
                    angle_changed = True
                else:
                    # 計算角度差（處理 0°/360° 邊界）
                    angle_diff = abs(angle_deg - self._last_cursor_angle)
                    if angle_diff > 180:
                        angle_diff = 360 - angle_diff
                    angle_changed = angle_diff >= angle_threshold
                
                if angle_changed:
                    self.mov_module.handle_cursor_tracking_event({
                        "type": "cursor_angle",
                        "angle": angle_deg,
                        "distance": distance
                    })
                    self._last_cursor_angle = angle_deg
            
            # 更新狀態
            self._cursor_was_near = is_near_now
        except Exception as e:
            error_log(f"[DesktopPetApp] 滑鼠追蹤處理失敗: {e}")
    
    # ==================== 檔案拖放事件處理 ====================
    
    def dragEnterEvent(self, event):
        """拖放進入事件"""
        try:
            if event.mimeData().hasUrls():
                # 檢查是否有檔案
                urls = event.mimeData().urls()
                if urls and urls[0].isLocalFile():
                    event.accept()  # 接受事件
                    debug_log(2, "[DesktopPetApp] 接受檔案拖放")
                else:
                    event.ignore()
            else:
                event.ignore()
        except Exception as e:
            error_log(f"[DesktopPetApp] 拖放進入事件處理失敗: {e}")
            event.ignore()
    
    def dragMoveEvent(self, event):
        """拖放移動事件"""
        try:
            if event.mimeData().hasUrls():
                event.accept()  # 接受拖放移動
            else:
                event.ignore()
        except Exception as e:
            error_log(f"[DesktopPetApp] 拖放移動事件處理失敗: {e}")
            event.ignore()
    
    def dropEvent(self, event):
        """拖放釋放事件"""
        try:
            if event.mimeData().hasUrls():
                urls = event.mimeData().urls()
                if urls:
                    file_path = urls[0].toLocalFile()
                    info_log(f"[DesktopPetApp] 收到檔案拖放: {file_path}")
                    
                    # 直接調用 MOV 模組處理檔案拖放
                    if self.mov_module and hasattr(self.mov_module, 'handle_ui_event'):
                        from core.bases.frontend_base import UIEventType
                        self.mov_module.handle_ui_event(UIEventType.FILE_DROP, {
                            "file_path": file_path
                        })
                        event.acceptProposedAction()
                        info_log(f"[DesktopPetApp] 已處理檔案拖放: {file_path}")
                    else:
                        error_log("[DesktopPetApp] MOV 模組未初始化，無法處理檔案拖放")
                        event.ignore()
            else:
                event.ignore()
                
        except Exception as e:
            error_log(f"[DesktopPetApp] 拖放釋放事件處理失敗: {e}")
            event.ignore()
