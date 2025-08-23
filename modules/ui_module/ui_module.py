# modules/ui_module/ui_module.py
"""
UI 模組 - 前端使用者介面中樞控制器

負責協調三個前端介面：
1. Main Desktop Pet - UEP 桌寵 Overlay 應用程式
2. User Access Widget - 可拖拽擴展的使用者介面
3. Debug Interface - 開發用除錯介面

UI 模組作為中樞，協調 ANI 和 MOV 模組，並管理所有前端交互
"""

import os
import sys
import time
import threading
from typing import Dict, Any, Optional, List
from enum import Enum

# 將 TestOverlayApplication 路徑加入以重用 desktop_pet 資源
test_overlay_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), '..', 'TestOverlayApplication')
if os.path.exists(test_overlay_path):
    sys.path.insert(0, test_overlay_path)

try:
    from PyQt5.QtWidgets import QApplication, QWidget, QLabel, QVBoxLayout, QHBoxLayout, QPushButton
    from PyQt5.QtCore import Qt, QPoint, QTimer, pyqtSignal
    from PyQt5.QtGui import QPixmap, QPainter, QColor, QFont
    PYQT5_AVAILABLE = True
except ImportError:
    PYQT5_AVAILABLE = False
    # 定義替代類別以避免錯誤
    class QApplication: pass

from core.frontend_base import BaseFrontendModule, FrontendModuleType, UIEventType
from core.working_context import ContextType
from core.state_manager import UEPState
from utils.debug_helper import debug_log, info_log, error_log


class UIInterfaceType(Enum):
    """UI 介面類型"""
    MAIN_DESKTOP_PET = "main_desktop_pet"       # 主桌寵應用程式
    USER_ACCESS_WIDGET = "user_access_widget"   # 使用者存取介面
    DEBUG_INTERFACE = "debug_interface"         # 除錯介面


class UIModule(BaseFrontendModule):
    """UI 模組 - 前端介面中樞控制器"""
    
    def __init__(self, config: dict = None):
        super().__init__(FrontendModuleType.UI)
        
        self.config = config or {}
        self.is_initialized = False
        
        # Qt 應用程式實例
        self.app = None
        
        # 三個前端介面實例
        self.interfaces = {
            UIInterfaceType.MAIN_DESKTOP_PET: None,
            UIInterfaceType.USER_ACCESS_WIDGET: None,
            UIInterfaceType.DEBUG_INTERFACE: None
        }
        
        # 活躍介面追蹤
        self.active_interfaces = set()
        
        # 與其他前端模組的連接
        self.ani_module = None
        self.mov_module = None
        
        # 全局系統設定
        self.system_settings = {}
        
        info_log(f"[{self.module_id}] UI 中樞模組初始化")
    
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
            
            # 初始化三個介面
            if not self._initialize_interfaces():
                error_log(f"[{self.module_id}] 初始化介面失敗")
                return False
            
            # 註冊事件處理器
            self._register_event_handlers()
            
            # 連接信號
            self._connect_signals()

            self.is_initialized = True
            info_log(f"[{self.module_id}] UI 前端初始化成功")
            return True
            
        except Exception as e:
            error_log(f"[{self.module_id}] UI 前端初始化失敗: {e}")
            return False
    
    def _initialize_interfaces(self) -> bool:
        """初始化所有介面"""
        try:
            # 動態導入介面類別
            from .main.desktop_pet_app import DesktopPetApp
            from .user.access_widget import UserAccessWidget
            from .debug.debug_interface import DebugInterface
            
            # 創建介面實例
            self.interfaces[UIInterfaceType.MAIN_DESKTOP_PET] = DesktopPetApp(self)
            self.interfaces[UIInterfaceType.USER_ACCESS_WIDGET] = UserAccessWidget(self)
            self.interfaces[UIInterfaceType.DEBUG_INTERFACE] = DebugInterface(self)
            
            info_log(f"[{self.module_id}] 所有介面初始化完成")
            return True
            
        except ImportError as e:
            error_log(f"[{self.module_id}] 導入介面類別失敗: {e}")
            return False
        except Exception as e:
            error_log(f"[{self.module_id}] 初始化介面異常: {e}")
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
    
    def _connect_signals(self):
        """連接信號"""
    
    # ========== 介面管理方法 ==========
    
    def show_interface(self, interface_type: UIInterfaceType) -> dict:
        """顯示指定介面"""
        try:
            interface = self.interfaces.get(interface_type)
            if not interface:
                return {"error": f"介面 {interface_type.value} 不存在"}
            
            interface.show()
            self.active_interfaces.add(interface_type)
            
            info_log(f"[{self.module_id}] 顯示介面: {interface_type.value}")
            return {"success": True, "interface": interface_type.value}
        except Exception as e:
            return {"error": str(e)}
    
    def hide_interface(self, interface_type: UIInterfaceType) -> dict:
        """隱藏指定介面"""
        try:
            interface = self.interfaces.get(interface_type)
            if not interface:
                return {"error": f"介面 {interface_type.value} 不存在"}
            
            interface.hide()
            self.active_interfaces.discard(interface_type)
            
            info_log(f"[{self.module_id}] 隱藏介面: {interface_type.value}")
            return {"success": True, "interface": interface_type.value}
        except Exception as e:
            return {"error": str(e)}
    
    def get_interface_status(self) -> dict:
        """獲取所有介面狀態"""
        status = {}
        for interface_type, interface in self.interfaces.items():
            if interface:
                status[interface_type.value] = {
                    "exists": True,
                    "active": interface_type in self.active_interfaces,
                    "visible": hasattr(interface, 'isVisible') and interface.isVisible()
                }
            else:
                status[interface_type.value] = {
                    "exists": False,
                    "active": False,
                    "visible": False
                }
        return status
    
    def broadcast_to_interfaces(self, message_type: str, data: dict):
        """廣播訊息到所有活躍介面"""
        for interface_type in self.active_interfaces:
            interface = self.interfaces.get(interface_type)
            if interface and hasattr(interface, 'receive_broadcast'):
                try:
                    interface.receive_broadcast(message_type, data)
                except Exception as e:
                    error_log(f"[{self.module_id}] 廣播到 {interface_type.value} 失敗: {e}")
    
    def update_system_settings(self, settings: dict):
        """更新全局系統設定"""
        self.system_settings.update(settings)
        
        # 廣播設定變更到所有介面
        self.broadcast_to_interfaces("system_settings_changed", {
            "settings": settings,
            "timestamp": time.time()
        })
        
        info_log(f"[{self.module_id}] 系統設定已更新: {list(settings.keys())}")
    
    # ========== 前端請求處理 ==========
    
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
            
            # 介面管理命令
            if command == 'show_interface':
                interface_type = data.get('interface')
                if interface_type:
                    return self.show_interface(UIInterfaceType(interface_type))
                return {"error": "需要指定 interface 參數"}
            
            elif command == 'hide_interface':
                interface_type = data.get('interface')
                if interface_type:
                    return self.hide_interface(UIInterfaceType(interface_type))
                return {"error": "需要指定 interface 參數"}
            
            elif command == 'get_interface_status':
                return self.get_interface_status()
            
            elif command == 'update_system_settings':
                settings = data.get('settings', {})
                self.update_system_settings(settings)
                return {"success": True, "updated_settings": list(settings.keys())}
            
            # 向後相容的舊命令 (主要針對 main desktop pet)
            elif command in ['show_window', 'hide_window']:
                interface_type = UIInterfaceType.MAIN_DESKTOP_PET
                interface = self.interfaces.get(interface_type)
                if interface and hasattr(interface, 'handle_request'):
                    return interface.handle_request(data)
                return {"error": "主桌寵介面不可用"}
            
            elif command in ['move_window', 'update_image', 'get_window_info', 
                           'set_window_size', 'set_always_on_top', 'set_image', 'set_opacity']:
                # 轉發到主桌寵介面
                interface_type = UIInterfaceType.MAIN_DESKTOP_PET
                interface = self.interfaces.get(interface_type)
                if interface and hasattr(interface, 'handle_request'):
                    return interface.handle_request(data)
                return {"error": "主桌寵介面不可用"}
            
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
    
    def _set_window_size(self, data: dict) -> dict:
        """設定視窗大小"""
        try:
            width = data.get('width')
            height = data.get('height')
            
            if not all([width, height]):
                return {"error": "需要指定 width 和 height"}
            
            if self.window:
                self.window.resize(width, height)
                self.window_size = (width, height)
                self.update_local_state('window_size', {'width': width, 'height': height})
                return {"success": True, "size": {"width": width, "height": height}}
            return {"error": "視窗未初始化"}
        except Exception as e:
            return {"error": str(e)}
    
    def _set_always_on_top(self, data: dict) -> dict:
        """設定視窗置頂"""
        try:
            enabled = data.get('enabled', True)
            
            if self.window:
                if enabled:
                    self.window.setWindowFlags(self.window.windowFlags() | Qt.WindowStaysOnTopHint)
                else:
                    self.window.setWindowFlags(self.window.windowFlags() & ~Qt.WindowStaysOnTopHint)
                
                self.window.show()  # 需要重新顯示以應用標誌
                self.always_on_top = enabled
                self.update_local_state('always_on_top', enabled)
                return {"success": True, "always_on_top": enabled}
            return {"error": "視窗未初始化"}
        except Exception as e:
            return {"error": str(e)}
    
    def _set_image(self, data: dict) -> dict:
        """設定顯示圖像"""
        try:
            image_path = data.get('image_path')
            if not image_path:
                return {"error": "需要指定 image_path"}
            
            if not os.path.exists(image_path):
                return {"error": f"圖像檔案不存在: {image_path}"}
            
            if PYQT5_AVAILABLE:
                self.current_image = QPixmap(image_path)
                if self.current_image.isNull():
                    return {"error": "無法載入圖像"}
                
                if self.window:
                    self.window.update()
                
                self.update_local_state('current_image', image_path)
                return {"success": True, "image": image_path}
            else:
                return {"error": "PyQt5 不可用"}
        except Exception as e:
            return {"error": str(e)}
    
    def _set_opacity(self, data: dict) -> dict:
        """設定視窗透明度"""
        try:
            opacity = data.get('opacity')
            if opacity is None:
                return {"error": "需要指定 opacity (0.0-1.0)"}
            
            opacity = float(opacity)
            if not (0.0 <= opacity <= 1.0):
                return {"error": "透明度必須在 0.0 到 1.0 之間"}
            
            if self.window:
                self.window.setWindowOpacity(opacity)
                self.window_opacity = opacity
                self.update_local_state('window_opacity', opacity)
                return {"success": True, "opacity": opacity}
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
            
            # 使用新的回調機制連接MOV和ANI模組的動畫觸發
            if hasattr(mov_module, 'add_animation_callback') and ani_module:
                # 定義處理動畫請求的回調
                def handle_animation_trigger(animation_type, params):
                    ani_module.play_animation(animation_type, params)
                
                # 註冊回調到MOV模組
                mov_module.add_animation_callback(handle_animation_trigger)
        
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
