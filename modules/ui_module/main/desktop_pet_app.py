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

# 導入 MOV 和 ANI 模組
try:
    from modules.mov_module.mov_module import MOVModule, MovementMode
    from modules.ani_module.ani_module import ANIModule, AnimationType
    MOV_ANI_AVAILABLE = True
except ImportError as e:
    error_log(f"[DesktopPetApp] 無法導入 MOV/ANI 模組: {e}")
    MOV_ANI_AVAILABLE = False
    # 創建模擬類別
    class MOVModule: pass
    class ANIModule: pass
    class MovementMode: pass
    class AnimationType: pass


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
        
        # MOV 和 ANI 模組整合
        self.mov_module = None
        self.ani_module = None
        self.current_movement_mode = None
        self.current_animation_type = None
        
        # 初始化模組
        self.init_modules()
        self.init_ui()
        
        info_log("[DesktopPetApp] 桌面寵物應用程式初始化完成")
    
    def init_modules(self):
        """初始化 MOV 和 ANI 模組"""
        try:
            if MOV_ANI_AVAILABLE:
                # 初始化 MOV 模組
                self.mov_module = MOVModule()
                info_log("[DesktopPetApp] MOV 模組初始化成功")
                
                # 初始化 ANI 模組
                self.ani_module = ANIModule()
                info_log("[DesktopPetApp] ANI 模組初始化成功")
                
                # 設置默認狀態
                self.current_movement_mode = MovementMode.FLOAT if hasattr(MovementMode, 'FLOAT') else None
                self.current_animation_type = AnimationType.STAND_IDLE if hasattr(AnimationType, 'STAND_IDLE') else None
                
                # 連接模組信號（如果有的話）
                self.connect_module_signals()
            else:
                info_log("[DesktopPetApp] MOV/ANI 模組不可用，使用基本模式")
        except Exception as e:
            error_log(f"[DesktopPetApp] 模組初始化失敗: {e}")
            self.mov_module = None
            self.ani_module = None
    
    def connect_module_signals(self):
        """連接模組信號"""
        try:
            # 連接 MOV 模組信號
            if self.mov_module and hasattr(self.mov_module, 'position_updated'):
                self.mov_module.position_updated.connect(self.on_movement_position_change)
            
            if self.mov_module and hasattr(self.mov_module, 'movement_mode_changed'):
                self.mov_module.movement_mode_changed.connect(self.on_movement_mode_change)
            
            # 連接 ANI 模組信號
            if self.ani_module and hasattr(self.ani_module, 'animation_changed'):
                self.ani_module.animation_changed.connect(self.on_animation_change)
            
            if self.ani_module and hasattr(self.ani_module, 'frame_updated'):
                self.ani_module.frame_updated.connect(self.on_animation_frame_update)
                
            debug_log(1, "[DesktopPetApp] 模組信號連接完成")
        except Exception as e:
            error_log(f"[DesktopPetApp] 模組信號連接失敗: {e}")
    
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
    
    # === MOV/ANI 模組整合方法 ===
    
    def on_movement_position_change(self, x, y):
        """處理來自 MOV 模組的位置變更"""
        try:
            self.set_position(x, y)
            debug_log(1, f"[DesktopPetApp] MOV 模組更新位置: ({x}, {y})")
        except Exception as e:
            error_log(f"[DesktopPetApp] 處理位置變更失敗: {e}")
    
    def on_movement_mode_change(self, mode):
        """處理來自 MOV 模組的移動模式變更"""
        try:
            self.current_movement_mode = mode
            debug_log(1, f"[DesktopPetApp] 移動模式變更: {mode}")
            
            # 根據移動模式變更動畫
            self.update_animation_for_movement(mode)
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
        """處理來自 ANI 模組的動畫幀更新"""
        try:
            if frame_data and 'image_path' in frame_data:
                self.set_image(frame_data['image_path'])
            elif frame_data and 'pixmap' in frame_data:
                self.current_image = frame_data['pixmap']
                self.update()
            debug_log(1, f"[DesktopPetApp] 動畫幀更新")
        except Exception as e:
            error_log(f"[DesktopPetApp] 處理動畫幀更新失敗: {e}")
    
    def update_animation_for_movement(self, movement_mode):
        """根據移動模式更新動畫"""
        try:
            if not self.ani_module:
                return
            
            # 根據移動模式選擇合適的動畫
            animation_mapping = {
                'float': AnimationType.STAND_IDLE,
                'ground': AnimationType.WALK_LEFT,  # 暫時使用 WALK_LEFT，後續會改進
                'idle': AnimationType.SMILE_IDLE,
                'dragging': AnimationType.CURIOUS_IDLE,
            }
            
            # 獲取移動模式字符串
            mode_str = movement_mode.value if hasattr(movement_mode, 'value') else str(movement_mode)
            
            if mode_str in animation_mapping:
                target_animation = animation_mapping[mode_str]
                if hasattr(self.ani_module, 'set_animation'):
                    self.ani_module.set_animation(target_animation)
                    debug_log(1, f"[DesktopPetApp] 為移動模式 {mode_str} 設置動畫 {target_animation}")
                    
        except Exception as e:
            error_log(f"[DesktopPetApp] 更新移動動畫失敗: {e}")
    
    # === 對外提供的控制方法 ===
    
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
    
    def closeEvent(self, event):
        """窗口關閉事件"""
        info_log("[DesktopPetApp] 桌面寵物應用程式正在關閉")
        if self.state_changed:
            self.state_changed.emit("closed")
        event.accept()
