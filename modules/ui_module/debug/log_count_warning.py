"""
日誌瀏覽器頁面中的日誌計數警告機制

當日誌數量達到警告閾值時，將在UI上顯示警告信息，
並提供清理日誌的建議。

功能點:
1. 在統計面板顯示警告信息
2. 根據日誌數量設置不同的警告級別
3. 提供一鍵清理日誌的功能
"""
import os
import sys
from typing import Dict, List, Optional

# 從集中管理的 imports.py 導入 PyQt5 相關內容
from .imports import QLabel, QPushButton, QVBoxLayout, QHBoxLayout, QWidget, QTimer, Qt, QFont

# 添加項目根目錄到 Python 路徑
script_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.abspath(os.path.join(script_dir, '..', '..', '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from utils.debug_helper import debug_log, KEY_LEVEL, OPERATION_LEVEL, SYSTEM_LEVEL, ELABORATIVE_LEVEL

class LogCountWarningWidget(QWidget):
    """日誌數量警告小部件"""
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.warning_threshold = 5000
        self.critical_threshold = 10000
        self.setup_ui()
        self.hide()  # 初始時隱藏
        
    def setup_ui(self):
        """設置UI元素"""
        # 主布局
        main_layout = QVBoxLayout()
        main_layout.setContentsMargins(5, 5, 5, 5)
        
        # 警告信息標籤
        self.warning_label = QLabel()
        self.warning_label.setWordWrap(True)
        warning_font = QFont()
        warning_font.setBold(True)
        self.warning_label.setFont(warning_font)
        
        # 清理按鈕
        button_layout = QHBoxLayout()
        self.clear_logs_btn = QPushButton("清理日誌")
        self.clear_logs_btn.setToolTip("清理所有日誌條目")
        self.clear_logs_btn.clicked.connect(self.request_clear_logs)
        
        self.dismiss_btn = QPushButton("忽略")
        self.dismiss_btn.setToolTip("忽略此警告")
        self.dismiss_btn.clicked.connect(self.hide)
        
        button_layout.addWidget(self.clear_logs_btn)
        button_layout.addWidget(self.dismiss_btn)
        button_layout.addStretch()
        
        # 添加到主布局
        main_layout.addWidget(self.warning_label)
        main_layout.addLayout(button_layout)
        
        self.setLayout(main_layout)
        self.setStyleSheet("""
            QWidget { 
                background-color: #FFF3E0; 
                border: 1px solid #FFB74D;
                border-radius: 4px;
            }
            QPushButton {
                background-color: #FFB74D;
                border: none;
                border-radius: 2px;
                padding: 5px 10px;
                color: black;
            }
            QPushButton:hover {
                background-color: #FFA726;
            }
        """)
        
    def update_warning(self, log_count: int):
        """根據日誌數量更新警告信息"""
        if log_count > self.critical_threshold:
            self.warning_label.setText(
                f"<span style='color:#d32f2f;'>警告: 日誌數量過多 ({log_count} > {self.critical_threshold})</span><br>"
                "大量日誌可能導致性能下降，強烈建議清理!"
            )
            self.setStyleSheet("""
                QWidget { 
                    background-color: #FFEBEE; 
                    border: 1px solid #EF5350;
                    border-radius: 4px;
                }
                QPushButton {
                    background-color: #EF5350;
                    border: none;
                    border-radius: 2px;
                    padding: 5px 10px;
                    color: white;
                }
                QPushButton:hover {
                    background-color: #E53935;
                }
            """)
            self.show()
        elif log_count > self.warning_threshold:
            self.warning_label.setText(
                f"<span style='color:#FF8F00;'>提示: 日誌數量較多 ({log_count} > {self.warning_threshold})</span><br>"
                "建議定期清理日誌以保持界面響應速度"
            )
            self.setStyleSheet("""
                QWidget { 
                    background-color: #FFF3E0; 
                    border: 1px solid #FFB74D;
                    border-radius: 4px;
                }
                QPushButton {
                    background-color: #FFB74D;
                    border: none;
                    border-radius: 2px;
                    padding: 5px 10px;
                    color: black;
                }
                QPushButton:hover {
                    background-color: #FFA726;
                }
            """)
            self.show()
        else:
            # 低於警告閾值，隱藏警告
            self.hide()
            
    def request_clear_logs(self):
        """請求清理所有日誌"""
        # 獲取父部件 (LogViewerTab) 並呼叫其清理方法
        parent = self.parent()
        if parent and hasattr(parent, 'clear_logs'):
            parent.clear_logs()
            self.hide()  # 清理後隱藏警告
            debug_log(SYSTEM_LEVEL, "[LogCountWarningWidget] 已清理日誌")
        else:
            debug_log(OPERATION_LEVEL, "[LogCountWarningWidget] 無法找到清理日誌的方法")
