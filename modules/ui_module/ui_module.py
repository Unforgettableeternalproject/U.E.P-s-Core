# modules/ui_module/ui_module.py
"""
UI 模組 - 主要的前端使用者介面控制器

基於 desktop_pet.py 重構，負責：
- 主視窗管理和渲染
- 使用者輸入事件處理  
- 與 ANI 和 MOV 模組協調
- 系統狀態的視覺回饋
"""

import os
import sys
import time
import threading
from typing import Dict, Any, Optional, List

# 將 TestOverlayApplication 路徑加入以重用 desktop_pet 資源
test_overlay_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), '..', 'TestOverlayApplication')
if os.path.exists(test_overlay_path):
    sys.path.insert(0, test_overlay_path)

try:
    from PyQt5.QtWidgets import QApplication, QWidget
    from PyQt5.QtCore import Qt, QTimer, QPoint, QEvent, pyqtSignal
    from PyQt5.QtGui import QPainter, QPixmap, QCursor
    PYQT5_AVAILABLE = True
except ImportError:
    PYQT5_AVAILABLE = False
    # 定義替代類別以避免錯誤
    class QWidget: pass
    class QApplication: pass
    def pyqtSignal(*args): return None

from core.frontend_base import BaseFrontendModule, FrontendModuleType, UIEventType
from core.working_context import ContextType
from core.state_manager import UEPState
from utils.debug_helper import debug_log, info_log, error_log


class UIModule(BaseFrontendModule):
    """UI 模組 - 桌寵主視窗控制器"""
    
    def __init__(self, config: dict = None):
        super().__init__(FrontendModuleType.UI)
        
        self.config = config or {}
        
        # 視窗設置
        self.SIZE = self.config.get('window_size', 250)
        self.window = None
        self.app = None
        
        # 圖像資源
        self.static_image = None
        self.current_image = None
        
        # 視窗狀態
        self.window_position = QPoint(100, 100)
        self.is_dragging = False
        self.drag_position = QPoint()
        
        # 螢幕資訊
        self.screen_rect = None
        
        # 與其他前端模組的連接
        self.ani_module = None
        self.mov_module = None
        
        info_log(f"[{self.module_id}] UI 模組初始化")
    
    def initialize_frontend(self) -> bool:
        """初始化前端 UI 組件"""
        try:
            if not PYQT5_AVAILABLE:
                error_log(f"[{self.module_id}] PyQt5 不可用，無法初始化 UI")
                return False
            
            # 初始化 Qt 應用程式 (如果尚未存在)
            if not QApplication.instance():
                self.app = QApplication(sys.argv)
            else:
                self.app = QApplication.instance()
            
            # 創建主視窗
            self.window = DesktopPetWindow(self)
            
            # 載入圖像資源
            if not self._load_images():
                error_log(f"[{self.module_id}] 載入圖像資源失敗")
                return False
            
            # 設置螢幕資訊
            self.screen_rect = self.app.primaryScreen().availableGeometry()
            
            # 註冊事件處理器
            self._register_event_handlers()
            
            # 連接信號使用信號包裝器
            if hasattr(self.signals, 'animation_request'):
                self.signals.animation_request.connect(self._handle_animation_request)
            if hasattr(self.signals, 'movement_request'):
                self.signals.movement_request.connect(self._handle_movement_request)
            
            info_log(f"[{self.module_id}] UI 前端初始化成功")
            return True
            
        except Exception as e:
            error_log(f"[{self.module_id}] UI 前端初始化失敗: {e}")
            return False
    
    def _load_images(self) -> bool:
        """載入圖像資源"""
        try:
            # 尋找圖像檔案路徑
            project_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
            base_path = os.path.join(project_root, "resources", "assets")

            image_paths = [
                os.path.join(base_path, "static", "default.png"),
            ]

            static_image_path = None
            for path in image_paths:
                if os.path.exists(path):
                    static_image_path = path
                    break
            
            if not static_image_path:
                error_log(f"[{self.module_id}] 找不到靜態圖像檔案")
                return False
            
            self.static_image = QPixmap(static_image_path)
            self.current_image = self.static_image
            
            info_log(f"[{self.module_id}] 圖像資源載入成功: {static_image_path}")
            return True
            
        except Exception as e:
            error_log(f"[{self.module_id}] 載入圖像資源異常: {e}")
            return False
    
    def _register_event_handlers(self):
        """註冊事件處理器"""
        # 註冊 ANI 模組事件
        self.register_event_handler(UIEventType.ANIMATION_COMPLETE, self._on_animation_complete)
        
        # 註冊 MOV 模組事件  
        self.register_event_handler(UIEventType.WINDOW_MOVE, self._on_window_move)
        
        # 註冊滑鼠事件
        self.register_event_handler(UIEventType.MOUSE_CLICK, self._on_mouse_click)
        self.register_event_handler(UIEventType.MOUSE_HOVER, self._on_mouse_hover)
        self.register_event_handler(UIEventType.DRAG_START, self._on_drag_start)
        self.register_event_handler(UIEventType.DRAG_END, self._on_drag_end)
        
        # 註冊檔案事件
        self.register_event_handler(UIEventType.FILE_DROP, self._on_file_drop)
    
    def handle_frontend_request(self, data: dict) -> dict:
        """處理前端請求"""
        try:
            command = data.get('command')
            
            if command == 'show_window':
                return self._show_window(data)
            elif command == 'hide_window':
                return self._hide_window(data)
            elif command == 'move_window':
                return self._move_window(data)
            elif command == 'update_image':
                return self._update_image(data)
            elif command == 'get_window_info':
                return self._get_window_info()
            else:
                return {"error": f"未知命令: {command}"}
                
        except Exception as e:
            error_log(f"[{self.module_id}] 處理前端請求異常: {e}")
            return {"error": str(e)}
    
    def _show_window(self, data: dict) -> dict:
        """顯示視窗"""
        try:
            if self.window:
                self.window.show()
                self.update_local_state('window_visible', True)
                return {"success": True}
            return {"error": "視窗未初始化"}
        except Exception as e:
            return {"error": str(e)}
    
    def _hide_window(self, data: dict) -> dict:
        """隱藏視窗"""
        try:
            if self.window:
                self.window.hide()
                self.update_local_state('window_visible', False)
                return {"success": True}
            return {"error": "視窗未初始化"}
        except Exception as e:
            return {"error": str(e)}
    
    def _move_window(self, data: dict) -> dict:
        """移動視窗"""
        try:
            x = data.get('x', self.window_position.x())
            y = data.get('y', self.window_position.y())
            
            if self.window:
                self.window.move(x, y)
                self.window_position = QPoint(x, y)
                self.update_local_state('window_position', {'x': x, 'y': y})
                return {"success": True}
            return {"error": "視窗未初始化"}
        except Exception as e:
            return {"error": str(e)}
    
    def _update_image(self, data: dict) -> dict:
        """更新顯示圖像"""
        try:
            image_path = data.get('image_path')
            if image_path and os.path.exists(image_path):
                self.current_image = QPixmap(image_path)
                if self.window:
                    self.window.update()
                return {"success": True}
            return {"error": "圖像路徑無效"}
        except Exception as e:
            return {"error": str(e)}
    
    def _get_window_info(self) -> dict:
        """獲取視窗資訊"""
        try:
            if self.window:
                geo = self.window.geometry()
                return {
                    "x": geo.x(),
                    "y": geo.y(), 
                    "width": geo.width(),
                    "height": geo.height(),
                    "visible": self.window.isVisible()
                }
            return {"error": "視窗未初始化"}
        except Exception as e:
            return {"error": str(e)}
    
    def connect_frontend_modules(self, ani_module, mov_module):
        """連接其他前端模組"""
        self.ani_module = ani_module
        self.mov_module = mov_module
        
        if ani_module:
            # 連接動畫模組
            if hasattr(self.signals, 'animation_request'):
                self.signals.animation_request.connect(ani_module.handle_animation_request)
            if hasattr(ani_module.signals, 'animation_ready'):
                ani_module.signals.animation_ready.connect(self._on_animation_ready)
        
        if mov_module:
            # 連接行為模組
            if hasattr(self.signals, 'movement_request'):
                self.signals.movement_request.connect(mov_module.handle_movement_request)
            if hasattr(mov_module.signals, 'position_changed'):
                mov_module.signals.position_changed.connect(self._on_position_changed)
        
        info_log(f"[{self.module_id}] 前端模組連接完成")
    
    # ========== 事件處理器 ==========
    
    def _on_animation_complete(self, event):
        """動畫完成事件處理"""
        debug_log(2, f"[{self.module_id}] 動畫完成: {event.data}")

    def _on_window_move(self, event):
        """視窗移動事件處理"""
        new_pos = event.data.get('position')
        if new_pos and self.window:
            self.window.move(new_pos['x'], new_pos['y'])
    
    def _on_mouse_click(self, event):
        """滑鼠點擊事件處理"""
        debug_log(2, f"[{self.module_id}] 滑鼠點擊: {event.data}")

    def _on_mouse_hover(self, event):
        """滑鼠懸停事件處理"""
        debug_log(2, f"[{self.module_id}] 滑鼠懸停: {event.data}")

    def _on_drag_start(self, event):
        """拖拽開始事件處理"""
        self.is_dragging = True
        debug_log(2, f"[{self.module_id}] 開始拖拽")

    def _on_drag_end(self, event):
        """拖拽結束事件處理"""
        self.is_dragging = False
        debug_log(2, f"[{self.module_id}] 結束拖拽")

    def _on_file_drop(self, event):
        """檔案拖放事件處理"""
        files = event.data.get('files', [])
        info_log(f"[{self.module_id}] 檔案拖放: {len(files)} 個檔案")
        
        # 更新上下文
        self.update_context(ContextType.CROSS_MODULE_DATA, {
            'event_type': 'file_drop',
            'files': files,
            'timestamp': time.time()
        })
    
    # ========== 信號處理器 ==========
    
    def request_animation(self, animation_type: str, data: dict):
        """請求動畫播放"""
        debug_log(1, f"[{self.module_id}] 動畫請求: {animation_type}")
        # 這裡可以直接調用ANI模組或使用事件系統
        
    def request_movement(self, movement_type: str, data: dict):
        """請求移動操作"""
        debug_log(1, f"[{self.module_id}] 移動請求: {movement_type}")
        # 這裡可以直接調用MOV模組或使用事件系統
    
    def _handle_animation_request(self, animation_type: str, params: dict):
        """處理動畫請求"""
        debug_log(3, f"[{self.module_id}] 動畫請求: {animation_type}")
    
    def _handle_movement_request(self, movement_type: str, params: dict):
        """處理移動請求"""
        debug_log(3, f"[{self.module_id}] 移動請求: {movement_type}")

    def _on_animation_ready(self, image_pixmap):
        """動畫幀準備完成"""
        if image_pixmap and self.window:
            self.current_image = image_pixmap
            self.window.update()
    
    def _on_position_changed(self, new_position):
        """位置變更回調"""
        if self.window:
            self.window.move(new_position['x'], new_position['y'])
            self.window_position = QPoint(new_position['x'], new_position['y'])
    
    # ========== 系統狀態回調 ==========
    
    def on_system_state_changed(self, old_state: UEPState, new_state: UEPState):
        """系統狀態變更回調"""
        debug_log(3, f"[{self.module_id}] 系統狀態變更: {old_state} -> {new_state}")
        
        # 根據系統狀態調整 UI (改為直接調用動畫請求方法)
        if new_state == UEPState.LISTENING:
            self.request_animation("talking", {})
        elif new_state == UEPState.PROCESSING:
            self.request_animation("thinking", {})
        elif new_state == UEPState.RESPONDING:
            self.request_animation("speaking", {})
        elif new_state == UEPState.IDLE:
            self.request_animation("idle", {})
    
    def shutdown(self):
        """關閉 UI 模組"""
        if self.window:
            self.window.close()
            self.window = None
        
        if self.app and self.app != QApplication.instance():
            self.app.quit()
        
        super().shutdown()
        info_log(f"[{self.module_id}] UI 模組已關閉")


class DesktopPetWindow(QWidget):
    """桌寵視窗 - 簡化版的桌寵窗口"""
    
    def __init__(self, ui_module: UIModule):
        super().__init__(None)  # 不設置父對象，避免類型轉換問題
        self.ui_module = ui_module
        self.setup_window()
    
    def setup_window(self):
        """設置視窗"""
        self.setFixedSize(self.ui_module.SIZE, self.ui_module.SIZE)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAcceptDrops(True)
        self.setMouseTracking(True)
        self.setAttribute(Qt.WA_Hover, True)
    
    def paintEvent(self, event):
        """繪製事件"""
        if self.ui_module.current_image and not self.ui_module.current_image.isNull():
            painter = QPainter(self)
            painter.setRenderHint(QPainter.Antialiasing)
            
            # 繪製圖像
            painter.drawPixmap(0, 0, self.ui_module.current_image)
    
    def mousePressEvent(self, event):
        """滑鼠按下事件"""
        if event.button() == Qt.LeftButton:
            self.ui_module.emit_event(UIEventType.MOUSE_CLICK, {
                'button': 'left',
                'position': {'x': event.x(), 'y': event.y()}
            })
            
            self.ui_module.emit_event(UIEventType.DRAG_START, {
                'start_position': {'x': event.globalX(), 'y': event.globalY()}
            })
    
    def mouseMoveEvent(self, event):
        """滑鼠移動事件"""
        if self.ui_module.is_dragging and (event.buttons() & Qt.LeftButton):
            # 發送到 MOV 模組處理 (改為直接調用移動請求方法)
            self.ui_module.request_movement("drag_move", {
                'position': {'x': event.globalX(), 'y': event.globalY()}
            })
    
    def mouseReleaseEvent(self, event):
        """滑鼠釋放事件"""
        if event.button() == Qt.LeftButton:
            self.ui_module.emit_event(UIEventType.DRAG_END, {
                'end_position': {'x': event.globalX(), 'y': event.globalY()}
            })
    
    def enterEvent(self, event):
        """滑鼠進入事件"""
        self.ui_module.emit_event(UIEventType.MOUSE_HOVER, {
            'type': 'enter'
        })
    
    def leaveEvent(self, event):
        """滑鼠離開事件"""
        self.ui_module.emit_event(UIEventType.MOUSE_HOVER, {
            'type': 'leave'
        })
    
    def dragEnterEvent(self, event):
        """拖拽進入事件"""
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
    
    def dropEvent(self, event):
        """檔案拖放事件"""
        urls = event.mimeData().urls()
        files = [url.toLocalFile() for url in urls if url.toLocalFile()]
        
        self.ui_module.emit_event(UIEventType.FILE_DROP, {
            'files': files
        })
