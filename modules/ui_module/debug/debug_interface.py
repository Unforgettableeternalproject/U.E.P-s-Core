# debug/debug_interface.py
"""
Debug Interface

開發用除錯介面
提供模組監控、日誌查看、測試執行等開發功能
"""

import os
import sys
from typing import Dict, Any, Optional, List

try:
    from PyQt5.QtWidgets import (QWidget, QLabel, QPushButton, QVBoxLayout, 
                                QHBoxLayout, QTextEdit, QTabWidget, QGroupBox,
                                QScrollArea, QSplitter, QFrame, QTableWidget,
                                QTableWidgetItem, QHeaderView)
    from PyQt5.QtCore import Qt, QTimer, pyqtSignal, QThread
    from PyQt5.QtGui import QFont, QColor, QPalette
except ImportError:
    QWidget = object
    QLabel = object
    QPushButton = object
    QVBoxLayout = object
    QHBoxLayout = object
    QTextEdit = object
    QTabWidget = object
    QGroupBox = object
    QScrollArea = object
    QSplitter = object
    QFrame = object
    QTableWidget = object
    QTableWidgetItem = object
    QHeaderView = object
    Qt = None
    QTimer = None
    pyqtSignal = None
    QThread = None
    QFont = None
    QColor = None
    QPalette = None

# 添加項目根目錄到 Python 路徑
script_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.abspath(os.path.join(script_dir, '..', '..', '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from utils.debug_helper import debug_log, info_log, error_log


class DebugInterface(QWidget):
    """
    除錯介面
    
    特性：
    - 即時日誌顯示
    - 模組狀態監控
    - 測試執行控制
    - 系統資訊顯示
    """
    
    # 信號定義
    test_requested = pyqtSignal(str) if pyqtSignal else None
    command_executed = pyqtSignal(str) if pyqtSignal else None
    
    def __init__(self, ui_module=None):
        super().__init__()
        self.ui_module = ui_module
        self.log_buffer = []
        self.max_log_lines = 1000
        
        self.init_ui()
        self.setup_timer()
        
        info_log("[DebugInterface] 除錯介面初始化完成")
    
    def init_ui(self):
        """初始化 UI"""
        if not QWidget:
            error_log("[DebugInterface] PyQt5 未安裝，無法初始化 UI")
            return
            
        self.setWindowTitle("UEP Debug Interface")
        self.setGeometry(100, 100, 800, 600)
        
        # 設置樣式
        self.setStyleSheet("""
            QWidget {
                background-color: #1e1e1e;
                color: #ffffff;
                font-family: Consolas, monospace;
            }
            QTabWidget::pane {
                border: 1px solid #404040;
                background-color: #2d2d2d;
            }
            QTabBar::tab {
                background-color: #404040;
                padding: 8px 16px;
                margin-right: 2px;
                border-top-left-radius: 4px;
                border-top-right-radius: 4px;
            }
            QTabBar::tab:selected {
                background-color: #2d2d2d;
            }
            QPushButton {
                background-color: #404040;
                border: 1px solid #606060;
                padding: 6px 12px;
                border-radius: 4px;
            }
            QPushButton:hover {
                background-color: #505050;
            }
            QPushButton:pressed {
                background-color: #353535;
            }
            QTextEdit {
                background-color: #2d2d2d;
                border: 1px solid #404040;
                border-radius: 4px;
                padding: 4px;
            }
            QGroupBox {
                font-weight: bold;
                border: 1px solid #404040;
                border-radius: 4px;
                margin-top: 6px;
                padding-top: 4px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 5px 0 5px;
            }
        """)
        
        self.setup_layout()
        info_log("[DebugInterface] UI 初始化完成")
    
    def setup_layout(self):
        """設置布局"""
        if not QVBoxLayout:
            return
            
        main_layout = QVBoxLayout(self)
        
        # 建立標籤頁
        self.tab_widget = QTabWidget()
        
        # 日誌標籤頁
        self.create_log_tab()
        
        # 模組監控標籤頁
        self.create_module_tab()
        
        # 測試標籤頁
        self.create_test_tab()
        
        # 系統標籤頁
        self.create_system_tab()
        
        main_layout.addWidget(self.tab_widget)
    
    def create_log_tab(self):
        """建立日誌標籤頁"""
        log_widget = QWidget()
        layout = QVBoxLayout(log_widget)
        
        # 控制按鈕
        button_layout = QHBoxLayout()
        
        self.clear_log_btn = QPushButton("清空日誌")
        self.clear_log_btn.clicked.connect(self.clear_logs)
        button_layout.addWidget(self.clear_log_btn)
        
        self.pause_log_btn = QPushButton("暫停更新")
        self.pause_log_btn.clicked.connect(self.toggle_log_pause)
        button_layout.addWidget(self.pause_log_btn)
        
        button_layout.addStretch()
        layout.addLayout(button_layout)
        
        # 日誌顯示區域
        self.log_display = QTextEdit()
        self.log_display.setReadOnly(True)
        if QFont:
            self.log_display.setFont(QFont("Consolas", 9))
        layout.addWidget(self.log_display)
        
        self.tab_widget.addTab(log_widget, "📋 日誌")
        self.log_paused = False
    
    def create_module_tab(self):
        """建立模組監控標籤頁"""
        module_widget = QWidget()
        layout = QVBoxLayout(module_widget)
        
        # 模組狀態表格
        self.module_table = QTableWidget()
        self.module_table.setColumnCount(4)
        self.module_table.setHorizontalHeaderLabels(["模組", "狀態", "最後活動", "操作"])
        
        if QHeaderView:
            header = self.module_table.horizontalHeader()
            header.setStretchLastSection(True)
        
        layout.addWidget(self.module_table)
        
        # 刷新按鈕
        refresh_btn = QPushButton("🔄 刷新模組狀態")
        refresh_btn.clicked.connect(self.refresh_module_status)
        layout.addWidget(refresh_btn)
        
        self.tab_widget.addTab(module_widget, "🔧 模組")
    
    def create_test_tab(self):
        """建立測試標籤頁"""
        test_widget = QWidget()
        layout = QVBoxLayout(test_widget)
        
        # 前端模組測試按鈕組
        frontend_group = QGroupBox("前端模組測試")
        frontend_layout = QVBoxLayout(frontend_group)
        
        frontend_tests = [
            ("測試前端模組狀態", "frontend_status"),
            ("測試模組間通訊", "frontend_communication"), 
            ("測試整合功能", "frontend_integration"),
            ("測試全部功能", "frontend_all")
        ]
        
        for test_name, test_id in frontend_tests:
            btn = QPushButton(test_name)
            btn.clicked.connect(lambda checked, tid=test_id: self.run_test(tid))
            frontend_layout.addWidget(btn)
        
        layout.addWidget(frontend_group)
        
        # MEM模組測試按鈕組
        mem_group = QGroupBox("MEM 記憶模組測試")
        mem_layout = QVBoxLayout(mem_group)
        
        mem_tests = [
            ("測試身份Token創建", "mem_identity_token"),
            ("測試對話快照創建", "mem_conversation_snapshot"),
            ("測試記憶查詢", "mem_memory_query"),
            ("測試身份管理統計", "mem_identity_stats"),
            ("測試NLP整合", "mem_nlp_integration"),
            ("測試LLM上下文提取", "mem_llm_context"),
            ("測試完整工作流程", "mem_full_workflow")
        ]
        
        for test_name, test_id in mem_tests:
            btn = QPushButton(test_name)
            btn.clicked.connect(lambda checked, tid=test_id: self.run_test(tid))
            mem_layout.addWidget(btn)
        
        layout.addWidget(mem_group)
        
        # 測試結果顯示
        self.test_result = QTextEdit()
        self.test_result.setReadOnly(True)
        layout.addWidget(self.test_result)
        
        self.tab_widget.addTab(test_widget, "🧪 測試")
    
    def create_system_tab(self):
        """建立系統標籤頁"""
        system_widget = QWidget()
        layout = QVBoxLayout(system_widget)
        
        # 系統資訊顯示
        self.system_info = QTextEdit()
        self.system_info.setReadOnly(True)
        layout.addWidget(self.system_info)
        
        # 更新按鈕
        update_btn = QPushButton("🔄 更新系統資訊")
        update_btn.clicked.connect(self.update_system_info)
        layout.addWidget(update_btn)
        
        self.tab_widget.addTab(system_widget, "💻 系統")
        
        # 初始載入系統資訊
        self.update_system_info()
    
    def setup_timer(self):
        """設置定時器"""
        if not QTimer:
            return
            
        # 日誌更新定時器
        self.log_timer = QTimer()
        self.log_timer.timeout.connect(self.update_log_display)
        self.log_timer.start(3000)  # 每3秒更新（降低頻率以改善性能）
        
        # 模組狀態更新定時器
        self.module_timer = QTimer()
        self.module_timer.timeout.connect(self.refresh_module_status)
        self.module_timer.start(10000)  # 每10秒更新（降低頻率）
    
    def add_log(self, message: str, level: str = "INFO"):
        """新增日誌訊息"""
        import datetime
        timestamp = datetime.datetime.now().strftime("%H:%M:%S")
        log_entry = f"[{timestamp}] [{level}] {message}"
        
        self.log_buffer.append(log_entry)
        
        # 保持緩衝區大小
        if len(self.log_buffer) > self.max_log_lines:
            self.log_buffer = self.log_buffer[-self.max_log_lines:]
    
    def update_log_display(self):
        """更新日誌顯示"""
        if self.log_paused or not hasattr(self, 'log_display'):
            return
            
        # 獲取滾動位置
        cursor = self.log_display.textCursor()
        at_bottom = cursor.atEnd()
        
        # 更新內容
        self.log_display.clear()
        self.log_display.setText('\n'.join(self.log_buffer))
        
        # 恢復滾動位置
        if at_bottom:
            cursor.movePosition(cursor.End)
            self.log_display.setTextCursor(cursor)
    
    def clear_logs(self):
        """清空日誌"""
        self.log_buffer.clear()
        if hasattr(self, 'log_display'):
            self.log_display.clear()
    
    def toggle_log_pause(self):
        """切換日誌暫停狀態"""
        self.log_paused = not self.log_paused
        if hasattr(self, 'pause_log_btn'):
            self.pause_log_btn.setText("繼續更新" if self.log_paused else "暫停更新")
    
    def refresh_module_status(self):
        """刷新模組狀態"""
        if not hasattr(self, 'module_table'):
            return
            
        # 透過 UI 模組獲取系統狀態
        if self.ui_module and hasattr(self.ui_module, 'get_system_status'):
            try:
                status = self.ui_module.get_system_status()
                self.update_module_table(status)
            except Exception as e:
                error_log(f"[DebugInterface] 獲取模組狀態異常: {e}")
    
    def update_module_table(self, status: dict):
        """更新模組表格"""
        if not hasattr(self, 'module_table'):
            return
            
        modules = status.get('modules', {})
        self.module_table.setRowCount(len(modules))
        
        for row, (module_id, module_info) in enumerate(modules.items()):
            # 模組名稱
            self.module_table.setItem(row, 0, QTableWidgetItem(module_id))
            
            # 狀態
            state = module_info.get('state', 'unknown')
            status_item = QTableWidgetItem(state)
            if state == 'active':
                status_item.setBackground(QColor(40, 167, 69))
            elif state == 'error':
                status_item.setBackground(QColor(220, 53, 69))
            else:
                status_item.setBackground(QColor(108, 117, 125))
            self.module_table.setItem(row, 1, status_item)
            
            # 最後活動
            last_activity = module_info.get('last_activity', 'N/A')
            self.module_table.setItem(row, 2, QTableWidgetItem(str(last_activity)))
            
            # 操作按鈕（暫時顯示為文字）
            self.module_table.setItem(row, 3, QTableWidgetItem("重啟"))
    
    def run_test(self, test_id: str):
        """執行測試"""
        debug_log(1, f"[DebugInterface] 執行測試: {test_id}")
        
        if self.test_requested:
            self.test_requested.emit(test_id)
        
        # 透過 UI 模組執行測試
        if self.ui_module and hasattr(self.ui_module, 'run_debug_test'):
            try:
                result = self.ui_module.run_debug_test(test_id)
                self.display_test_result(test_id, result)
            except Exception as e:
                error_result = {"error": str(e), "success": False}
                self.display_test_result(test_id, error_result)
    
    def display_test_result(self, test_id: str, result: dict):
        """顯示測試結果"""
        if not hasattr(self, 'test_result'):
            return
            
        import datetime
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        result_text = f"\n[{timestamp}] 測試: {test_id}\n"
        result_text += "=" * 50 + "\n"
        
        if result.get('success', False):
            result_text += "✅ 測試通過\n"
        else:
            result_text += "❌ 測試失敗\n"
        
        for key, value in result.items():
            result_text += f"{key}: {value}\n"
        
        result_text += "\n"
        
        self.test_result.append(result_text)
    
    def update_system_info(self):
        """更新系統資訊"""
        if not hasattr(self, 'system_info'):
            return
            
        import platform
        import psutil
        
        info = []
        info.append(f"作業系統: {platform.system()} {platform.release()}")
        info.append(f"Python 版本: {platform.python_version()}")
        info.append(f"CPU 使用率: {psutil.cpu_percent()}%")
        info.append(f"記憶體使用率: {psutil.virtual_memory().percent}%")
        
        # 添加 UI 模組資訊
        if self.ui_module:
            info.append("\n=== UI 模組資訊 ===")
            info.append(f"模組 ID: {getattr(self.ui_module, 'module_id', 'N/A')}")
            info.append(f"介面數量: {len(getattr(self.ui_module, 'interfaces', {}))}")
            info.append(f"狀態: {getattr(self.ui_module, 'status', 'N/A')}")
        
        self.system_info.setText('\n'.join(info))
    
    def handle_request(self, data: dict) -> dict:
        """處理來自 UI 模組的請求"""
        try:
            command = data.get('command')
            
            if command == 'show_debug':
                self.show()
                return {"success": True, "message": "除錯介面已顯示"}
            
            elif command == 'hide_debug':
                self.hide()
                return {"success": True, "message": "除錯介面已隱藏"}
            
            elif command == 'add_log':
                message = data.get('message', '')
                level = data.get('level', 'INFO')
                self.add_log(message, level)
                return {"success": True, "added": message}
            
            elif command == 'run_test':
                test_id = data.get('test_id')
                if test_id:
                    self.run_test(test_id)
                    return {"success": True, "test": test_id}
                return {"error": "需要提供 test_id 參數"}
            
            elif command == 'get_debug_info':
                return {
                    "visible": self.isVisible(),
                    "log_count": len(self.log_buffer),
                    "log_paused": getattr(self, 'log_paused', False)
                }
            
            else:
                return {"error": f"未知命令: {command}"}
                
        except Exception as e:
            error_log(f"[DebugInterface] 處理請求異常: {e}")
            return {"error": str(e)}
    
    def closeEvent(self, event):
        """窗口關閉事件"""
        info_log("[DebugInterface] 除錯介面正在關閉")
        event.accept()


# 新版除錯介面整合
def create_enhanced_debug_interface(ui_module=None):
    """
    建立增強版除錯介面
    
    優先使用新版分頁式介面，失敗時回退到舊版
    """
    try:
        from .debug_main_window import launch_debug_interface
        return launch_debug_interface(ui_module)
    except ImportError:
        info_log("[DebugInterface] 新版介面不可用，使用舊版介面")
        return DebugInterface(ui_module)
    except Exception as e:
        error_log(f"[DebugInterface] 新版介面啟動失敗: {e}")
        return DebugInterface(ui_module)
