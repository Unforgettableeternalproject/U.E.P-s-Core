# user/access_widget.py
"""
User Access Widget

可拖拽擴展的使用者存取小工具
提供快速操作和功能存取介面
"""

import os
import sys
from typing import Dict, Any, Optional

try:
    from PyQt5.QtWidgets import (QWidget, QLabel, QPushButton, QVBoxLayout, 
                                QHBoxLayout, QFrame, QScrollArea, QGroupBox)
    from PyQt5.QtCore import Qt, QPoint, QTimer, pyqtSignal, QSize
    from PyQt5.QtGui import QPixmap, QPainter, QColor, QFont
except ImportError:
    QWidget = object
    QLabel = object
    QPushButton = object
    QVBoxLayout = object
    QHBoxLayout = object
    QFrame = object
    QScrollArea = object
    QGroupBox = object
    Qt = None
    QPoint = None
    QTimer = None
    pyqtSignal = None
    QSize = None
    QPixmap = None
    QPainter = None
    QColor = None
    QFont = None

# 添加項目根目錄到 Python 路徑
script_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.abspath(os.path.join(script_dir, '..', '..', '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from utils.debug_helper import debug_log, info_log, error_log


class UserAccessWidget(QWidget):
    """
    使用者存取小工具
    
    特性：
    - 可拖拽移動
    - 可摺疊/展開
    - 提供快速功能按鈕
    - 支持模組狀態顯示
    """
    
    # 信號定義
    function_requested = pyqtSignal(str) if pyqtSignal else None
    position_changed = pyqtSignal(int, int) if pyqtSignal else None
    expanded_changed = pyqtSignal(bool) if pyqtSignal else None
    
    def __init__(self, ui_module=None):
        super().__init__()
        self.ui_module = ui_module
        self.is_expanded = False
        self.is_dragging = False
        self.drag_position = QPoint() if QPoint else None
        
        self.init_ui()
        info_log("[UserAccessWidget] 使用者存取小工具初始化完成")
    
    def init_ui(self):
        """初始化 UI"""
        if not QWidget:
            error_log("[UserAccessWidget] PyQt5 未安裝，無法初始化 UI")
            return
            
        # 設定窗口屬性
        self.setWindowFlags(
            Qt.FramelessWindowHint |
            Qt.WindowStaysOnTopHint |
            Qt.Tool
        )
        
        self.setFixedSize(60, 120)  # 摺疊狀態大小
        self.setup_layout()
        
        # 設置初始位置（螢幕右上角）
        if hasattr(self, 'screen'):
            screen = self.screen().geometry()
            self.move(screen.width() - self.width() - 20, 50)
        
        info_log("[UserAccessWidget] UI 初始化完成")
    
    def setup_layout(self):
        """設置布局"""
        if not QVBoxLayout:
            return
            
        # 主布局
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(5, 5, 5, 5)
        main_layout.setSpacing(3)
        
        # 標題區域
        self.title_label = QLabel("UEP")
        if QFont:
            self.title_label.setFont(QFont("Arial", 10, QFont.Bold))
        self.title_label.setAlignment(Qt.AlignCenter)
        self.title_label.setStyleSheet("""
            QLabel {
                background-color: #2d3142;
                color: white;
                border-radius: 3px;
                padding: 2px;
            }
        """)
        main_layout.addWidget(self.title_label)
        
        # 切換按鈕
        self.toggle_button = QPushButton("▼")
        self.toggle_button.setFixedSize(50, 25)
        self.toggle_button.clicked.connect(self.toggle_expanded)
        self.toggle_button.setStyleSheet("""
            QPushButton {
                background-color: #4f5d75;
                color: white;
                border: none;
                border-radius: 3px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #6c7b95;
            }
        """)
        main_layout.addWidget(self.toggle_button)
        
        # 功能按鈕容器
        self.function_container = QFrame()
        self.function_container.setVisible(False)
        self.function_layout = QVBoxLayout(self.function_container)
        self.function_layout.setContentsMargins(0, 0, 0, 0)
        self.function_layout.setSpacing(2)
        
        # 新增功能按鈕
        self.create_function_buttons()
        
        main_layout.addWidget(self.function_container)
        main_layout.addStretch()
        
        # 設置整體樣式
        self.setStyleSheet("""
            QWidget {
                background-color: rgba(255, 255, 255, 240);
                border: 1px solid #ccc;
                border-radius: 5px;
            }
        """)
    
    def create_function_buttons(self):
        """建立功能按鈕"""
        if not QPushButton:
            return
            
        functions = [
            ("📝", "show_note", "筆記"),
            ("🎭", "ani_control", "動畫"),
            ("🎬", "mov_control", "影片"),
            ("⚙️", "settings", "設定"),
            ("🔧", "debug", "除錯")
        ]
        
        for icon, func_id, tooltip in functions:
            btn = QPushButton(icon)
            btn.setFixedSize(50, 30)
            btn.setToolTip(tooltip)
            btn.clicked.connect(lambda checked, f=func_id: self.request_function(f))
            btn.setStyleSheet("""
                QPushButton {
                    background-color: #f8f9fa;
                    border: 1px solid #dee2e6;
                    border-radius: 3px;
                    font-size: 14px;
                }
                QPushButton:hover {
                    background-color: #e9ecef;
                }
                QPushButton:pressed {
                    background-color: #dee2e6;
                }
            """)
            self.function_layout.addWidget(btn)
    
    def toggle_expanded(self):
        """切換展開/摺疊狀態"""
        self.is_expanded = not self.is_expanded
        
        if self.is_expanded:
            # 展開
            self.setFixedSize(60, 220)
            self.function_container.setVisible(True)
            self.toggle_button.setText("▲")
        else:
            # 摺疊
            self.setFixedSize(60, 120)
            self.function_container.setVisible(False)
            self.toggle_button.setText("▼")
        
        if self.expanded_changed:
            self.expanded_changed.emit(self.is_expanded)
        
        debug_log(f"[UserAccessWidget] 小工具{'展開' if self.is_expanded else '摺疊'}")
    
    def request_function(self, function_id: str):
        """請求執行功能"""
        debug_log(f"[UserAccessWidget] 請求功能: {function_id}")
        
        if self.function_requested:
            self.function_requested.emit(function_id)
        
        # 透過 UI 模組轉發請求
        if self.ui_module and hasattr(self.ui_module, 'handle_user_request'):
            self.ui_module.handle_user_request({
                'command': 'function_request',
                'function': function_id,
                'source': 'access_widget'
            })
    
    def mousePressEvent(self, event):
        """鼠標按下事件"""
        if event.button() == Qt.LeftButton:
            self.is_dragging = True
            if QPoint:
                self.drag_position = event.globalPos() - self.frameGeometry().topLeft()
    
    def mouseMoveEvent(self, event):
        """鼠標移動事件"""
        if self.is_dragging and event.buttons() == Qt.LeftButton:
            if QPoint:
                new_pos = event.globalPos() - self.drag_position
                self.move(new_pos)
                
                if self.position_changed:
                    self.position_changed.emit(new_pos.x(), new_pos.y())
    
    def mouseReleaseEvent(self, event):
        """鼠標釋放事件"""
        if event.button() == Qt.LeftButton:
            self.is_dragging = False
    
    def update_module_status(self, module_id: str, status: str):
        """更新模組狀態顯示"""
        # 在標題欄或其他地方顯示模組狀態
        if status == "active":
            self.title_label.setStyleSheet("""
                QLabel {
                    background-color: #28a745;
                    color: white;
                    border-radius: 3px;
                    padding: 2px;
                }
            """)
        elif status == "error":
            self.title_label.setStyleSheet("""
                QLabel {
                    background-color: #dc3545;
                    color: white;
                    border-radius: 3px;
                    padding: 2px;
                }
            """)
        else:
            self.title_label.setStyleSheet("""
                QLabel {
                    background-color: #2d3142;
                    color: white;
                    border-radius: 3px;
                    padding: 2px;
                }
            """)
    
    def handle_request(self, data: dict) -> dict:
        """處理來自 UI 模組的請求"""
        try:
            command = data.get('command')
            
            if command == 'show_widget':
                self.show()
                return {"success": True, "message": "存取小工具已顯示"}
            
            elif command == 'hide_widget':
                self.hide()
                return {"success": True, "message": "存取小工具已隱藏"}
            
            elif command == 'set_expanded':
                expanded = data.get('expanded', True)
                if expanded != self.is_expanded:
                    self.toggle_expanded()
                return {"success": True, "expanded": self.is_expanded}
            
            elif command == 'move_widget':
                x = data.get('x', self.x())
                y = data.get('y', self.y())
                self.move(x, y)
                return {"success": True, "position": {"x": x, "y": y}}
            
            elif command == 'update_status':
                module_id = data.get('module_id')
                status = data.get('status')
                if module_id and status:
                    self.update_module_status(module_id, status)
                    return {"success": True, "updated": module_id}
                return {"error": "需要提供 module_id 和 status 參數"}
            
            elif command == 'get_widget_info':
                return {
                    "position": {"x": self.x(), "y": self.y()},
                    "size": {"width": self.width(), "height": self.height()},
                    "visible": self.isVisible(),
                    "expanded": self.is_expanded
                }
            
            else:
                return {"error": f"未知命令: {command}"}
                
        except Exception as e:
            error_log(f"[UserAccessWidget] 處理請求異常: {e}")
            return {"error": str(e)}
    
    def closeEvent(self, event):
        """窗口關閉事件"""
        info_log("[UserAccessWidget] 使用者存取小工具正在關閉")
        event.accept()
