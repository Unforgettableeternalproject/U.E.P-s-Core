# debug/log_viewer_tab.py
"""
Log Viewer Tab

日誌檢視分頁
提供日誌檢視、過濾、搜尋和管理功能
"""

import os
import sys
from typing import Dict, Any, Optional, List, Tuple
import datetime
import re
import csv
import threading
import time
from collections import deque

# 從集中管理的 imports.py 導入 PyQt5 相關內容
from .imports import (
    PYQT5_AVAILABLE, QWidget, QVBoxLayout, QHBoxLayout, QGroupBox, 
    QPushButton, QTextEdit, QLabel, QComboBox, QLineEdit, QCheckBox, 
    QSplitter, QFrame, QTableWidget, QTableWidgetItem, QHeaderView,
    QFileDialog, QMessageBox, QSpinBox, QTreeView, QListWidget, 
    QListWidgetItem, QDialog, QApplication, Qt, QTimer, pyqtSignal, 
    QThread, QMetaType, QFont, QColor, QTextCharFormat, QTextCursor, 
    QIcon, register_qt_types
)

# 導入日誌計數警告小部件
from .log_count_warning import LogCountWarningWidget

# 添加項目根目錄到 Python 路徑
script_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.abspath(os.path.join(script_dir, '..', '..', '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from utils.debug_helper import debug_log, info_log, error_log, KEY_LEVEL, OPERATION_LEVEL, SYSTEM_LEVEL, ELABORATIVE_LEVEL

# 導入日誌截取器
from .log_interceptor import get_log_interceptor, install_interceptor


class LogViewerTab(QWidget if PYQT5_AVAILABLE else object):
    """
    日誌檢視分頁
    
    特性：
    - 即時日誌顯示
    - 日誌級別過濾
    - 搜尋和高亮
    - 日誌匯出
    - 統計資訊
    """
    
    def __init__(self):
        try:
            if PYQT5_AVAILABLE:
                super().__init__()
            self.log_entries = []
            self.filtered_entries = []
            self.max_entries = 2000  # 降低最大條目數以改善性能
            self.auto_scroll = True
            self.log_filters = {
                'DEBUG': True,
                'INFO': True,
                'WARNING': True,
                'ERROR': True
            }
            self.interceptor_installed = False
            self.last_entry_count = 0  # 記錄上次的條目數，用於判斷是否需要更新顯示
            
            if PYQT5_AVAILABLE:
                self.init_ui()
                self.setup_timer()
                self.setup_log_interceptor()
            
            debug_log(OPERATION_LEVEL, "[LogViewerTab] 日誌檢視分頁初始化完成")
        except Exception as e:
            error_log(f"[LogViewerTab] 初始化失敗: {str(e)}")
        
    def setup_log_interceptor(self):
        """設置日誌截取器"""
        try:
            # 安裝日誌截取器 (如果尚未安裝)
            if not self.interceptor_installed:
                # 安裝截取器
                install_interceptor()
                self.interceptor_installed = True
                
                # 獲取截取器實例
                interceptor = get_log_interceptor()
                
                # 註冊回調
                interceptor.add_callback(self.process_intercepted_logs)
                
                debug_log(OPERATION_LEVEL, "[LogViewerTab] 成功安裝日誌截取器")
        except Exception as e:
            error_log(f"[LogViewerTab] 設置日誌截取器失敗: {e}")
    
    def process_intercepted_logs(self, logs):
        """處理從日誌截取器接收的日誌"""
        if not logs:
            return
            
        # 創建一個帶處理的日誌條目列表
        entries_to_display = []
        
        # 遍歷所有新日誌
        for log in logs:
            try:
                # 獲取日誌資訊
                if 'timestamp_str' in log:
                    timestamp = log['timestamp_str']
                else:
                    timestamp = log['timestamp'].strftime('%Y-%m-%d %H:%M:%S')
                    
                level = log['level']
                message = log['message']
                
                # 檢查是否為重複日誌
                if self._is_duplicate_log(timestamp, level, message):
                    continue
                    
                # 創建新的日誌條目
                log_entry = {
                    'timestamp': log['timestamp'],
                    'timestamp_str': timestamp,
                    'level': level,
                    'message': message,
                    'formatted': log.get('formatted', f'[{timestamp}] {level} - {message}')
                }
                
                # 添加到日誌條目列表
                self.log_entries.append(log_entry)
                
                # 確保日誌條目不超過最大限制
                if len(self.log_entries) > self.max_entries:
                    # 移除最早的條目
                    self.log_entries = self.log_entries[-self.max_entries:]
                
                # 如果符合當前過濾條件，添加到過濾後的列表和顯示列表
                if level in self.log_filters and self.log_filters[level]:
                    self.filtered_entries.append(log_entry)
                    
                    # 確保過濾後的條目不超過最大限制
                    if len(self.filtered_entries) > self.max_entries:
                        self.filtered_entries = self.filtered_entries[-self.max_entries:]
                    
                    # 添加到待顯示列表
                    entries_to_display.append(log_entry)
                    
            except Exception as e:
                print(f"[LogViewerTab] 處理日誌條目時出錯: {e}", file=sys.stderr)
        
        # 在主線程中批量更新 UI
        if PYQT5_AVAILABLE and entries_to_display:
            # 使用一個函數來封裝，避免在 lambda 中捕獲變量
            def update_batch_logs():
                try:
                    for entry in entries_to_display:
                        self.update_log_display(entry)
                    # 更新統計信息
                    self.update_statistics()
                except Exception as e:
                    print(f"批量更新日誌顯示時出錯: {e}", file=sys.stderr)
                    
            # 使用計時器在主線程中安全地執行
            QTimer.singleShot(0, update_batch_logs)
    
    def _is_duplicate_log(self, timestamp, level, message):
        """檢查是否為重複的日誌條目"""
        # 只檢查最近的幾條日誌，提高效率
        recent_logs = self.log_entries[-20:] if len(self.log_entries) > 20 else self.log_entries
        
        for entry in recent_logs:
            # 檢查時間戳、級別和消息是否完全相同
            if (entry['timestamp'] == timestamp and
                entry['level'] == level and
                entry['message'] == message):
                return True
        
        return False
        
    def update_log_display(self, log_entry):
        """更新日誌顯示"""
        if not hasattr(self, 'log_display'):
            return
        
        try:
            # 準備參數
            level = log_entry['level']
            color = self.get_log_level_color(level)
            formatted = log_entry['formatted']
            
            # 處理訊息中的換行符
            formatted = formatted.replace('\n', '<br>')
            
            # 使用 HTML 格式化文本，確保每條日誌後有換行
            html_text = f'<span style="color:{color};">{formatted}</span><br>'
            
            # 在主線程中安全地更新 UI
            if PYQT5_AVAILABLE:
                # 使用 QCoreApplication.instance().postEvent 或 QMetaObject.invokeMethod 在主線程中執行更新
                self._update_display_safe(html_text, level)
            else:
                # 直接調用（非 PyQt5 環境，不需要跨線程）
                self._add_text_to_display(html_text)
                if level in ['ERROR', 'CRITICAL'] and hasattr(self, 'recent_errors'):
                    self._add_text_to_error_display(html_text)
                
        except Exception as e:
            print(f"更新日誌顯示時出錯: {e}", file=sys.stderr)
    
    def _update_display_safe(self, html_text, level):
        """安全地在主線程更新顯示"""
        if not PYQT5_AVAILABLE:
            return
            
        # 將所有 UI 更新封裝為函數，以避免在 lambda 中捕獲引用
        def update_log():
            try:
                self._add_text_to_display_direct(html_text)
            except Exception as e:
                print(f"更新日誌顯示時出錯: {e}", file=sys.stderr)
                
        def update_error():
            try:
                self._add_text_to_error_display_direct(html_text)
            except Exception as e:
                print(f"更新錯誤顯示時出錯: {e}", file=sys.stderr)
        
        # 使用 QMetaObject.invokeMethod 或 QCoreApplication.postEvent 在主線程中執行
        try:
            from PyQt5.QtCore import QCoreApplication, Qt
            
            # 使用 QTimer.singleShot 在主線程中執行更新，但不使用 lambda
            QTimer.singleShot(0, update_log)
            
            # 如果是錯誤日誌，同時更新錯誤顯示區
            if level in ['ERROR', 'CRITICAL'] and hasattr(self, 'recent_errors'):
                QTimer.singleShot(0, update_error)
                
        except Exception as e:
            print(f"無法安全更新 UI: {e}", file=sys.stderr)
    
    def _add_text_to_display_direct(self, html_text):
        """直接將文字添加到日誌顯示區 (必須在主線程中調用)"""
        if not hasattr(self, 'log_display'):
            return
            
        # 追加文本到日誌顯示框
        cursor = self.log_display.textCursor()
        cursor.movePosition(QTextCursor.End)
        self.log_display.setTextCursor(cursor)
        self.log_display.insertHtml(html_text)
        
        # 如果自動滾動啟用，滾動到最新日誌
        if self.auto_scroll:
            self.log_display.ensureCursorVisible()
    
    def _add_text_to_error_display_direct(self, html_text):
        """直接將文字添加到錯誤顯示區 (必須在主線程中調用)"""
        if hasattr(self, 'recent_errors'):
            cursor = self.recent_errors.textCursor()
            cursor.movePosition(QTextCursor.End)
            self.recent_errors.setTextCursor(cursor)
            self.recent_errors.insertHtml(html_text)
            self.recent_errors.ensureCursorVisible()
        
        # 更新統計信息
        self.update_statistics()
        
    # 為了保持向下兼容性，保留原始方法名稱但將它們實現為轉發到新方法
    def _add_text_to_display(self, html_text):
        """向下兼容的方法 (轉發到 _add_text_to_display_direct)"""
        self._add_text_to_display_direct(html_text)
    
    def _add_text_to_error_display(self, html_text):
        """向下兼容的方法 (轉發到 _add_text_to_error_display_direct)"""
        self._add_text_to_error_display_direct(html_text)
        
    def get_log_level_color(self, level):
        """根據日誌級別獲取顏色"""
        colors = {
            'DEBUG': '#888888',    # 灰色
            'INFO': '#00AA00',     # 綠色
            'WARNING': '#CCAA00',  # 黃色
            'ERROR': "#EB3300",    # 橘紅色
            'CRITICAL': '#FF0000'  # 亮紅色
        }
        return colors.get(level, '#000000')
    
    def init_ui(self):
        """初始化介面"""
        layout = QVBoxLayout(self)
        
        # 建立控制區域
        self.create_control_section(layout)
        
        # 建立分割檢視
        splitter = QSplitter(Qt.Horizontal)
        
        # 左側：日誌顯示
        self.create_log_display_section(splitter)
        
        # 右側：統計和工具
        self.create_stats_section(splitter)
        
        layout.addWidget(splitter)
        
        # 設置樣式
        self.setup_styles()
    
    def create_control_section(self, main_layout):
        """建立控制區域"""
        control_group = QGroupBox("日誌控制")
        control_layout = QVBoxLayout(control_group)
        
        # 第一行：過濾器和搜尋
        filter_layout = QHBoxLayout()
        
        # 日誌級別過濾
        filter_layout.addWidget(QLabel("級別:"))
        
        self.debug_checkbox = QCheckBox("DEBUG")
        self.debug_checkbox.setChecked(True)
        self.debug_checkbox.toggled.connect(lambda checked: self.toggle_filter('DEBUG', checked))
        filter_layout.addWidget(self.debug_checkbox)
        
        self.info_checkbox = QCheckBox("INFO")
        self.info_checkbox.setChecked(True)
        self.info_checkbox.toggled.connect(lambda checked: self.toggle_filter('INFO', checked))
        filter_layout.addWidget(self.info_checkbox)
        
        self.warning_checkbox = QCheckBox("WARNING")
        self.warning_checkbox.setChecked(True)
        self.warning_checkbox.toggled.connect(lambda checked: self.toggle_filter('WARNING', checked))
        filter_layout.addWidget(self.warning_checkbox)
        
        self.error_checkbox = QCheckBox("ERROR")
        self.error_checkbox.setChecked(True)
        self.error_checkbox.toggled.connect(lambda checked: self.toggle_filter('ERROR', checked))
        filter_layout.addWidget(self.error_checkbox)
        
        filter_layout.addWidget(QFrame())  # 分隔線
        
        # 搜尋
        filter_layout.addWidget(QLabel("搜尋:"))
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("輸入搜尋關鍵字...")
        self.search_input.textChanged.connect(self.on_search_changed)
        filter_layout.addWidget(self.search_input)
        
        search_btn = QPushButton("🔍")
        search_btn.clicked.connect(self.highlight_search)
        filter_layout.addWidget(search_btn)
        
        control_layout.addLayout(filter_layout)
        
        # 第二行：控制按鈕
        button_layout = QHBoxLayout()
        
        clear_btn = QPushButton("🗑️ 清空日誌")
        clear_btn.clicked.connect(self.clear_logs)
        button_layout.addWidget(clear_btn)
        
        self.pause_btn = QPushButton("⏸️ 暫停")
        self.pause_btn.clicked.connect(self.toggle_pause)
        button_layout.addWidget(self.pause_btn)
        
        self.autoscroll_checkbox = QCheckBox("自動滾動")
        self.autoscroll_checkbox.setChecked(True)
        self.autoscroll_checkbox.toggled.connect(self.toggle_autoscroll)
        button_layout.addWidget(self.autoscroll_checkbox)
        
        button_layout.addWidget(QFrame())  # 分隔線
        
        export_btn = QPushButton("💾 匯出")
        export_btn.clicked.connect(self.export_logs)
        button_layout.addWidget(export_btn)
        
        load_btn = QPushButton("📁 載入")
        load_btn.clicked.connect(self.load_logs)
        button_layout.addWidget(load_btn)
        
        button_layout.addStretch()
        control_layout.addLayout(button_layout)
        
        main_layout.addWidget(control_group)
    
    def create_log_display_section(self, parent):
        """建立日誌顯示區域"""
        display_widget = QWidget()
        display_layout = QVBoxLayout(display_widget)
        
        # 日誌顯示區域
        self.log_display = QTextEdit()
        self.log_display.setReadOnly(True)
        self.log_display.setFont(QFont("Consolas", 9))
        display_layout.addWidget(self.log_display)
        
        # 狀態列
        status_layout = QHBoxLayout()
        
        self.entry_count_label = QLabel("項目: 0")
        status_layout.addWidget(self.entry_count_label)
        
        self.filtered_count_label = QLabel("顯示: 0")
        status_layout.addWidget(self.filtered_count_label)
        
        status_layout.addStretch()
        
        self.update_time_label = QLabel("最後更新: --:--:--")
        status_layout.addWidget(self.update_time_label)
        
        display_layout.addLayout(status_layout)
        
        parent.addWidget(display_widget)
    
    def create_stats_section(self, parent):
        """建立統計區域"""
        stats_widget = QWidget()
        stats_layout = QVBoxLayout(stats_widget)
        stats_widget.setMaximumWidth(300)
        
        # 統計資訊
        stats_group = QGroupBox("日誌統計")
        stats_grid_layout = QVBoxLayout(stats_group)
        
        # 級別統計
        level_layout = QHBoxLayout()
        level_layout.addWidget(QLabel("DEBUG:"))
        self.debug_count_label = QLabel("0")
        self.debug_count_label.setStyleSheet("color: #808080;")
        level_layout.addWidget(self.debug_count_label)
        level_layout.addStretch()
        stats_grid_layout.addLayout(level_layout)
        
        level_layout = QHBoxLayout()
        level_layout.addWidget(QLabel("INFO:"))
        self.info_count_label = QLabel("0")
        self.info_count_label.setStyleSheet("color: #2196f3;")
        level_layout.addWidget(self.info_count_label)
        level_layout.addStretch()
        stats_grid_layout.addLayout(level_layout)
        
        level_layout = QHBoxLayout()
        level_layout.addWidget(QLabel("WARNING:"))
        self.warning_count_label = QLabel("0")
        self.warning_count_label.setStyleSheet("color: #ff9800;")
        level_layout.addWidget(self.warning_count_label)
        level_layout.addStretch()
        stats_grid_layout.addLayout(level_layout)
        
        level_layout = QHBoxLayout()
        level_layout.addWidget(QLabel("ERROR:"))
        self.error_count_label = QLabel("0")
        self.error_count_label.setStyleSheet("color: #f44336;")
        level_layout.addWidget(self.error_count_label)
        level_layout.addStretch()
        stats_grid_layout.addLayout(level_layout)
        
        stats_layout.addWidget(stats_group)
        
        # 最近錯誤
        recent_group = QGroupBox("最近錯誤")
        recent_layout = QVBoxLayout(recent_group)
        
        self.recent_errors = QTextEdit()
        self.recent_errors.setReadOnly(True)
        self.recent_errors.setMaximumHeight(150)
        self.recent_errors.setFont(QFont("Consolas", 8))
        recent_layout.addWidget(self.recent_errors)
        
        stats_layout.addWidget(recent_group)
        
        # 快速動作
        actions_group = QGroupBox("快速動作")
        actions_layout = QVBoxLayout(actions_group)
        
        goto_error_btn = QPushButton("🔴 跳到最新錯誤")
        goto_error_btn.clicked.connect(self.goto_latest_error)
        actions_layout.addWidget(goto_error_btn)
        
        goto_warning_btn = QPushButton("🟡 跳到最新警告")
        goto_warning_btn.clicked.connect(self.goto_latest_warning)
        actions_layout.addWidget(goto_warning_btn)
        
        filter_errors_btn = QPushButton("🚨 只顯示錯誤")
        filter_errors_btn.clicked.connect(self.filter_only_errors)
        actions_layout.addWidget(filter_errors_btn)
        
        reset_filter_btn = QPushButton("🔄 重置過濾器")
        reset_filter_btn.clicked.connect(self.reset_filters)
        actions_layout.addWidget(reset_filter_btn)
        
        stats_layout.addWidget(actions_group)
        
        # 添加日誌計數警告小部件
        self.warning_container = QWidget()
        warning_container_layout = QVBoxLayout(self.warning_container)
        warning_container_layout.setContentsMargins(0, 0, 0, 0)
        
        # 創建並添加警告小部件
        self.log_count_warning = LogCountWarningWidget(self)
        warning_container_layout.addWidget(self.log_count_warning)
        
        stats_layout.addWidget(self.warning_container)
        
        stats_layout.addStretch()
        parent.addWidget(stats_widget)
    
    def setup_styles(self):
        """設置樣式"""
        self.setStyleSheet("""
            QGroupBox {
                font-weight: bold;
                border: 1px solid #404040;
                border-radius: 4px;
                margin-top: 8px;
                padding-top: 8px;
                color: #ffffff;
            }
            
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 8px 0 8px;
                color: #0078d4;
                font-weight: bold;
            }
            
            QTextEdit {
                background-color: #1e1e1e;
                border: 1px solid #404040;
                border-radius: 4px;
                color: #ffffff;
                selection-background-color: #404040;
                font-family: 'Consolas', 'Courier New', monospace;
                line-height: 1.4;
            }
            
            QLineEdit {
                background-color: #2d2d2d;
                border: 1px solid #404040;
                border-radius: 4px;
                padding: 4px;
                color: #ffffff;
            }
            
            QCheckBox {
                color: #ffffff;
                spacing: 5px;
            }
            
            QCheckBox::indicator {
                width: 16px;
                height: 16px;
            }
            
            QCheckBox::indicator:unchecked {
                border: 1px solid #404040;
                background-color: #2d2d2d;
                border-radius: 2px;
            }
            
            QCheckBox::indicator:checked {
                border: 1px solid #0078d4;
                background-color: #0078d4;
                border-radius: 2px;
            }
        """)
    
    def setup_timer(self):
        """設置更新定時器"""
        if not QTimer:
            return
        
        # 日誌更新定時器
        self.update_timer = QTimer()
        self.update_timer.timeout.connect(self.update_display)
        self.update_timer.start(3000)  # 每3秒更新一次（降低頻率以改善性能）
        
        # 統計資訊更新定時器
        self.stats_timer = QTimer()
        self.stats_timer.timeout.connect(self.update_statistics)
        self.stats_timer.start(5000)  # 每5秒更新統計資訊
        
        self.paused = False
    
    def add_log_entry(self, level: str, message: str, timestamp: datetime.datetime = None):
        """新增日誌項目"""
        if timestamp is None:
            timestamp = datetime.datetime.now()
        
        entry = {
            'timestamp': timestamp,
            'level': level,
            'message': message,
            'formatted': f"[{timestamp.strftime('%H:%M:%S')}] [{level}] {message}"
        }
        
        self.log_entries.append(entry)
        
        # 保持最大項目數限制
        if len(self.log_entries) > self.max_entries:
            self.log_entries = self.log_entries[-self.max_entries:]
        
        # 更新統計
        self.update_statistics()
        
        # 如果是錯誤，加入到最近錯誤列表
        if level == 'ERROR':
            self.add_recent_error(entry)
    
    def add_recent_error(self, entry: dict):
        """新增最近錯誤"""
        if hasattr(self, 'recent_errors'):
            formatted = f"[{entry['timestamp'].strftime('%H:%M:%S')}] {entry['message']}\n"
            self.recent_errors.append(formatted)
            
            # 只保留最近10個錯誤
            content = self.recent_errors.toPlainText()
            lines = content.split('\n')
            if len(lines) > 20:  # 每個錯誤可能有多行
                self.recent_errors.setText('\n'.join(lines[-20:]))
    
    def toggle_filter(self, level: str, enabled: bool):
        """切換過濾器"""
        self.log_filters[level] = enabled
        self.apply_filters()
    
    def apply_filters(self):
        """應用過濾器"""
        # 安全地獲取搜索文本
        search_text = ""
        if hasattr(self, 'search_input'):
            if PYQT5_AVAILABLE and QThread.currentThread() != QApplication.instance().thread():
                # 如果在非主線程中，需要使用線程安全的方式獲取
                # 但由於不能跨線程訪問 UI，我們只能使用空字符串
                pass
            else:
                # 在主線程中可以直接訪問
                search_text = self.search_input.text().lower()
        
        # 過濾處理（這部分是數據處理，可在任何線程中進行）
        filtered = []
        for entry in self.log_entries:
            # 級別過濾
            if not self.log_filters.get(entry['level'], True):
                continue
            
            # 搜尋過濾
            if search_text and search_text not in entry['message'].lower():
                continue
            
            filtered.append(entry)
        
        # 更新過濾後的列表
        self.filtered_entries = filtered
        
        # 在主線程中安全地刷新顯示
        self._refresh_display()
    
    def on_search_changed(self):
        """搜尋內容變更"""
        self.apply_filters()
    
    def highlight_search(self):
        """高亮搜尋結果"""
        search_text = self.search_input.text().strip()
        if not search_text:
            return
        
        # 清除之前的高亮
        cursor = self.log_display.textCursor()
        cursor.select(QTextCursor.Document)
        cursor.setCharFormat(QTextCharFormat())
        cursor.clearSelection()
        
        # 高亮新的搜尋結果
        document = self.log_display.document()
        highlight_format = QTextCharFormat()
        highlight_format.setBackground(QColor(255, 255, 0, 100))
        
        cursor = QTextCursor(document)
        while True:
            cursor = document.find(search_text, cursor, Qt.CaseInsensitive)
            if cursor.isNull():
                break
            
            cursor.mergeCharFormat(highlight_format)
    
    def update_display(self):
        """更新顯示（由定時器呼叫）"""
        if self.paused:
            return
        
        # 重新應用過濾器
        self.apply_filters()
        
        # 更新統計信息
        self.update_statistics()
    
    def _refresh_display(self):
        """刷新顯示內容（內部方法，避免遞迴）"""        
        if not PYQT5_AVAILABLE or not hasattr(self, 'log_display'):
            return

        # 在非 UI 線程中準備數據
        content_lines = []
        for entry in self.filtered_entries[-500:]:  # 只顯示最近500條（降低以改善性能）
            formatted_line = self.format_log_entry(entry)
            content_lines.append(formatted_line)
        
        # 複製數據以便在主線程中安全使用
        log_entries_count = len(self.log_entries)
        filtered_entries_count = len(self.filtered_entries)
        html_content = '<div style="white-space:pre-wrap;">' + '<br>'.join(content_lines) + '</div>'
        
        # 使用函數封裝 UI 更新，避免在 lambda 中捕獲變量
        def update_ui():
            try:
                if not hasattr(self, 'log_display'):
                    return
                    
                # 確定當前滾動位置
                cursor = self.log_display.textCursor()
                at_bottom = cursor.atEnd()
                
                # 更新 HTML 內容
                is_empty = self.log_display.toPlainText() == ""
                content_changed = (filtered_entries_count != self.last_entry_count)
                
                if is_empty or (len(content_lines) > 0 and content_changed):
                    self.log_display.setHtml(html_content)
                    self.last_entry_count = filtered_entries_count
                
                # 根據自動滾動設置決定是否滾動到底部
                if self.auto_scroll and at_bottom:
                    cursor = self.log_display.textCursor()
                    cursor.movePosition(QTextCursor.End)
                    self.log_display.setTextCursor(cursor)
                
                # 更新計數標籤
                if hasattr(self, 'entry_count_label'):
                    self.entry_count_label.setText(f"項目: {log_entries_count}")
                
                if hasattr(self, 'filtered_count_label'):
                    self.filtered_count_label.setText(f"顯示: {filtered_entries_count}")
            except Exception as e:
                print(f"刷新顯示時出錯: {e}", file=sys.stderr)
        
        # 在主線程中安全地更新 UI
        QTimer.singleShot(0, update_ui)
        
        if hasattr(self, 'update_time_label'):
            current_time = datetime.datetime.now().strftime("%H:%M:%S")
            self.update_time_label.setText(f"最後更新: {current_time}")
    
    def format_log_entry(self, entry: dict) -> str:
        """格式化日誌項目"""
        level = entry['level']
        
        # 使用timestamp_str如果可用，否則格式化timestamp
        if 'timestamp_str' in entry:
            timestamp = entry['timestamp_str']
        elif isinstance(entry['timestamp'], str):
            timestamp = entry['timestamp']
        else:
            timestamp = entry['timestamp'].strftime('%Y-%m-%d %H:%M:%S')
            
        message = entry['message']
        
        # 處理訊息中的換行符
        message = message.replace('\n', '<br>')
        
        # 獲取顏色
        color = self.get_log_level_color(level)
        
        # 格式化輸出
        return f'<span style="color: {color};">[{timestamp}] [{level}] {message}</span>'
    
    def update_statistics(self):
        """更新統計資訊"""
        # 計算統計資訊
        stats = {'DEBUG': 0, 'INFO': 0, 'WARNING': 0, 'ERROR': 0}
        
        for entry in self.log_entries:
            level = entry['level']
            if level in stats:
                stats[level] += 1
        
        # 在主線程中安全地更新 UI
        if PYQT5_AVAILABLE:
            # 創建一個帶參數的函數
            stats_copy = stats.copy()  # 複製統計數據，避免函數內捕獲外部變量
            
            def update_stats_ui():
                try:
                    if hasattr(self, 'debug_count_label'):
                        self.debug_count_label.setText(str(stats_copy['DEBUG']))
                    
                    if hasattr(self, 'info_count_label'):
                        self.info_count_label.setText(str(stats_copy['INFO']))
                    
                    if hasattr(self, 'warning_count_label'):
                        self.warning_count_label.setText(str(stats_copy['WARNING']))
                    
                    if hasattr(self, 'error_count_label'):
                        self.error_count_label.setText(str(stats_copy['ERROR']))
                except Exception as e:
                    print(f"更新統計 UI 時出錯: {e}", file=sys.stderr)
            
            # 使用 QTimer.singleShot 確保在主線程中更新 UI
            QTimer.singleShot(0, update_stats_ui)
        else:
            # 如果 PyQt5 不可用，直接返回
            pass
    
    def clear_logs(self):
        """清空日誌"""
        self.log_entries.clear()
        self.filtered_entries.clear()
        
        if hasattr(self, 'log_display'):
            self.log_display.clear()
        
        if hasattr(self, 'recent_errors'):
            self.recent_errors.clear()
        
        self.update_statistics()
        
        # 更新日誌計數警告 (清理後應該隱藏警告)
        if hasattr(self, 'log_count_warning'):
            def hide_warning():
                self.log_count_warning.hide()
            QTimer.singleShot(0, hide_warning)
        
        debug_log(SYSTEM_LEVEL, "[LogViewerTab] 日誌已清空")
    
    def toggle_pause(self):
        """切換暫停狀態"""
        self.paused = not self.paused
        
        if hasattr(self, 'pause_btn'):
            if self.paused:
                self.pause_btn.setText("▶️ 繼續")
            else:
                self.pause_btn.setText("⏸️ 暫停")
    
    def toggle_autoscroll(self, enabled: bool):
        """切換自動滾動"""
        self.auto_scroll = enabled
    
    def goto_latest_error(self):
        """跳到最新錯誤"""
        for i in range(len(self.filtered_entries) - 1, -1, -1):
            if self.filtered_entries[i]['level'] == 'ERROR':
                # 滾動到該位置（簡化實現）
                self.log_display.moveCursor(QTextCursor.End)
                break
    
    def goto_latest_warning(self):
        """跳到最新警告"""
        for i in range(len(self.filtered_entries) - 1, -1, -1):
            if self.filtered_entries[i]['level'] == 'WARNING':
                # 滾動到該位置（簡化實現）
                self.log_display.moveCursor(QTextCursor.End)
                break
    
    def filter_only_errors(self):
        """只顯示錯誤"""
        # 關閉其他過濾器
        self.debug_checkbox.setChecked(False)
        self.info_checkbox.setChecked(False)
        self.warning_checkbox.setChecked(False)
        self.error_checkbox.setChecked(True)
        
        # 應用過濾器
        self.log_filters = {
            'DEBUG': False,
            'INFO': False,
            'WARNING': False,
            'ERROR': True
        }
        self.apply_filters()
    
    def reset_filters(self):
        """重置過濾器"""
        # 啟用所有過濾器
        self.debug_checkbox.setChecked(True)
        self.info_checkbox.setChecked(True)
        self.warning_checkbox.setChecked(True)
        self.error_checkbox.setChecked(True)
        
        # 清空搜尋
        self.search_input.clear()
        
        # 重置過濾器狀態
        self.log_filters = {
            'DEBUG': True,
            'INFO': True,
            'WARNING': True,
            'ERROR': True
        }
        self.apply_filters()
    
    def export_logs(self):
        """匯出日誌"""
        if not self.log_entries:
            QMessageBox.information(self, "資訊", "沒有日誌可匯出")
            return
        
        try:
            filename, _ = QFileDialog.getSaveFileName(
                self, "匯出日誌", 
                f"debug_logs_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.txt", 
                "Text Files (*.txt);;CSV Files (*.csv)")
            
            if filename:
                with open(filename, 'w', encoding='utf-8') as f:
                    if filename.endswith('.csv'):
                        import csv
                        writer = csv.writer(f)
                        writer.writerow(["時間戳", "級別", "訊息"])
                        for entry in self.log_entries:
                            # 確保時間戳格式正確
                            if isinstance(entry['timestamp'], datetime.datetime):
                                timestamp = entry['timestamp'].strftime('%Y-%m-%d %H:%M:%S')
                            elif 'timestamp_str' in entry:
                                timestamp = entry['timestamp_str']
                            else:
                                timestamp = str(entry['timestamp'])
                                
                            writer.writerow([
                                timestamp,
                                entry['level'],
                                entry['message']
                            ])
                    else:
                        for entry in self.log_entries:
                            # 確保時間戳格式正確
                            if isinstance(entry['timestamp'], datetime.datetime):
                                timestamp = entry['timestamp'].strftime('%Y-%m-%d %H:%M:%S')
                            elif 'timestamp_str' in entry:
                                timestamp = entry['timestamp_str']
                            else:
                                timestamp = str(entry['timestamp'])
                                
                            # 寫入格式化的行並保留原始換行
                            f.write(f"[{timestamp}] [{entry['level']}] {entry['message']}\n")
                
                QMessageBox.information(self, "成功", f"日誌已匯出至: {filename}")
                debug_log(1, f"[LogViewerTab] 日誌已匯出至: {filename}")
        
        except Exception as e:
            QMessageBox.critical(self, "錯誤", f"匯出失敗: {e}")
            error_log(f"[LogViewerTab] 匯出日誌失敗: {e}")
    
    def load_logs(self):
        """載入日誌"""
        try:
            filename, _ = QFileDialog.getOpenFileName(
                self, "載入日誌", "", 
                "Text Files (*.txt);;CSV Files (*.csv);;All Files (*)")
            
            if filename:
                with open(filename, 'r', encoding='utf-8') as f:
                    if filename.endswith('.csv'):
                        import csv
                        reader = csv.reader(f)
                        next(reader)  # 跳過標題行
                        for row in reader:
                            if len(row) >= 3:
                                timestamp = datetime.datetime.strptime(row[0], '%Y-%m-%d %H:%M:%S')
                                self.add_log_entry(row[1], row[2], timestamp)
                    else:
                        for line in f:
                            line = line.strip()
                            if line:
                                # 簡化的日誌解析
                                if '] [' in line:
                                    parts = line.split('] [', 2)
                                    if len(parts) >= 3:
                                        time_str = parts[0][1:]  # 移除開頭的 [
                                        level = parts[1]
                                        message = parts[2][:-1] if parts[2].endswith(']') else parts[2]
                                        
                                        try:
                                            timestamp = datetime.datetime.strptime(time_str, '%H:%M:%S')
                                            # 設置為今天的時間
                                            today = datetime.datetime.now().date()
                                            timestamp = datetime.datetime.combine(today, timestamp.time())
                                            self.add_log_entry(level, message, timestamp)
                                        except ValueError:
                                            # 如果解析失敗，就當作普通訊息
                                            self.add_log_entry('INFO', line)
                                else:
                                    self.add_log_entry('INFO', line)
                
                QMessageBox.information(self, "成功", f"已載入日誌: {filename}")
                debug_log(1, f"[LogViewerTab] 已載入日誌: {filename}")
        
        except Exception as e:
            QMessageBox.critical(self, "錯誤", f"載入失敗: {e}")
            error_log(f"[LogViewerTab] 載入日誌失敗: {e}")
    
    def add_test_result(self, test_id: str, result: dict):
        """新增測試結果日誌"""
        if result.get('success'):
            self.add_log_entry('INFO', f"測試 {test_id} 執行成功")
        else:
            error_msg = result.get('error', '未知錯誤')
            self.add_log_entry('ERROR', f"測試 {test_id} 執行失敗: {error_msg}")
    
    def save_logs(self):
        """儲存日誌（由外部呼叫）"""
        self.export_logs()
        
    def hideEvent(self, event):
        """當分頁隱藏時的處理"""
        # 繼續原有的隱藏事件處理
        super().hideEvent(event)
        
    def update_log_count_warning(self, log_count):
        """更新日誌數量警告顯示
        
        當日誌數量超過某個閾值時，顯示警告
        
        Args:
            log_count (int): 日誌數量
        """
        if hasattr(self, 'log_count_warning'):
            # 使用 QTimer.singleShot 確保在主線程中更新 UI
            count = log_count  # 複製值，避免捕獲外部變量
            
            def update_warning_ui():
                try:
                    self.log_count_warning.update_warning(count)
                except Exception as e:
                    debug_log(OPERATION_LEVEL, f"[LogViewerTab] 更新日誌數量警告時出錯: {e}")
            
            QTimer.singleShot(0, update_warning_ui)
    
    def closeEvent(self, event):
        """當分頁關閉時清理資源"""
        # 清理截取器資源
        if hasattr(self, 'interceptor_installed') and self.interceptor_installed:
            try:
                # 獲取截取器實例
                interceptor = get_log_interceptor()
                
                # 移除回調
                interceptor.remove_callback(self.process_intercepted_logs)
                
                debug_log(SYSTEM_LEVEL, "[LogViewerTab] 已清理日誌截取器資源")
            except Exception as e:
                error_log(f"[LogViewerTab] 清理日誌截取器失敗: {e}")
                
        # 停止計時器
        if hasattr(self, 'update_timer') and self.update_timer:
            self.update_timer.stop()
        
        if hasattr(self, 'stats_timer') and self.stats_timer:
            self.stats_timer.stop()
            
        # 繼續原有的關閉事件處理
        super().closeEvent(event)
