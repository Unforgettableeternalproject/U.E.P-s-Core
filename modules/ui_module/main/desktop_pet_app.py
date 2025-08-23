# main/desktop_pet_app.py
"""
Desktop Pet Application

UEP 桌面寵物 Overlay 應用程式
提供主要的桌寵顯示和互動功能
"""

import os
import sys
from typing import Dict, Any, Optional

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
    
    def __init__(self, ui_module=None):
        super().__init__()
        self.ui_module = ui_module
        self.current_image = None
        self.is_dragging = False
        self.drag_position = QPoint() if QPoint else None
        self.default_size = (200, 200)
        
        self.init_ui()
        info_log("[DesktopPetApp] 桌面寵物應用程式初始化完成")
    
    def init_ui(self):
        """初始化 UI"""
        try:
            # 檢查是否為真正的 Qt 還是模擬版本
            if hasattr(self, 'setWindowFlags') and Qt:
                # 真正的 PyQt5
                self.setWindowFlags(
                    Qt.FramelessWindowHint |           # 無邊框
                    Qt.WindowStaysOnTopHint |          # 置頂
                    Qt.Tool |                          # 工具窗口
                    Qt.WA_TranslucentBackground        # 透明背景
                )
                
                self.setAttribute(Qt.WA_TranslucentBackground)
                self.setFixedSize(*self.default_size)
                
                # 設置初始位置
                self.center_on_screen()
            else:
                # 模擬版本
                self.setFixedSize(*self.default_size)
                
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
    
    def paintEvent(self, event):
        """繪製事件"""
        if not QPainter or not self.current_image:
            return
            
        try:
            painter = QPainter(self)
            painter.setRenderHint(QPainter.Antialiasing)
            
            # 繪製圖片
            if self.current_image:
                scaled_image = self.current_image.scaled(
                    self.size(), 
                    Qt.KeepAspectRatio, 
                    Qt.SmoothTransformation
                )
                
                # 計算置中位置
                x = (self.width() - scaled_image.width()) // 2
                y = (self.height() - scaled_image.height()) // 2
                painter.drawPixmap(x, y, scaled_image)
        except Exception as e:
            error_log(f"[DesktopPetApp] 繪製事件異常: {e}")
    
    def mousePressEvent(self, event):
        """鼠標按下事件"""
        try:
            if Qt and hasattr(event, 'button') and event.button() == Qt.LeftButton:
                self.is_dragging = True
                if QPoint and hasattr(event, 'globalPos'):
                    self.drag_position = event.globalPos() - self.frameGeometry().topLeft()
                
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
                    
                    # 發射位置變更信號
                    if self.position_changed:
                        self.position_changed.emit(new_pos.x(), new_pos.y())
        except Exception as e:
            error_log(f"[DesktopPetApp] 鼠標移動事件異常: {e}")
    
    def mouseReleaseEvent(self, event):
        """鼠標釋放事件"""
        try:
            if Qt and hasattr(event, 'button') and event.button() == Qt.LeftButton:
                self.is_dragging = False
        except Exception as e:
            error_log(f"[DesktopPetApp] 鼠標釋放事件異常: {e}")
    
    def set_image(self, image_path: str):
        """設置顯示圖片"""
        try:
            if os.path.exists(image_path):
                if QPixmap:
                    self.current_image = QPixmap(image_path)
                    self.update()  # 觸發重繪
                    debug_log(1, f"[DesktopPetApp] 已設置圖片: {image_path}")
                    return True
                else:
                    # 模擬版本：僅記錄圖片路徑
                    debug_log(1, f"[DesktopPetApp] 已設置圖片 (模擬): {image_path}")
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
            debug_log(1, f"[DesktopPetApp] 已設置大小: {width_value}x{height_value}")
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
            debug_log(1, f"[DesktopPetApp] 已設置位置: ({x_value}, {y_value})")
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
                    "opacity": self.windowOpacity() if hasattr(self, 'windowOpacity') else 1.0
                }
            
            else:
                return {"error": f"未知命令: {command}"}
                
        except Exception as e:
            error_log(f"[DesktopPetApp] 處理請求異常: {e}")
            return {"error": str(e)}
    
    def closeEvent(self, event):
        """窗口關閉事件"""
        info_log("[DesktopPetApp] 桌面寵物應用程式正在關閉")
        if self.state_changed:
            self.state_changed.emit("closed")
        event.accept()
