# debug/debug_main_window.py
"""
Debug Main Window

新版除錯介面主視窗
整合所有測試功能，提供分頁式操作介面
"""

import os
import sys
import platform
import gc
from typing import Dict, Any, Optional, List

# 從集中管理的 imports.py 導入 PyQt5 相關內容
from .imports import (
    PYQT5_AVAILABLE, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, 
    QTabWidget, QLabel, QPushButton, QStatusBar, QMenuBar, QAction, 
    QSplitter, QFrame, Qt, QTimer, pyqtSignal, QFont, QIcon, register_qt_types
)

# 添加項目根目錄到 Python 路徑
script_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.abspath(os.path.join(script_dir, '..', '..', '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from utils.debug_helper import debug_log, info_log, error_log, KEY_LEVEL, OPERATION_LEVEL, SYSTEM_LEVEL, ELABORATIVE_LEVEL

# 導入各個測試分頁
from .module_test_tabs import *
from .integration_test_tab import IntegrationTestTab
from .system_monitor_tab import SystemMonitorTab

if PYQT5_AVAILABLE:
    from .log_viewer_tab import LogViewerTab
else:
    LogViewerTab = None
    
from .log_interceptor import install_interceptor, uninstall_interceptor


class DebugMainWindow(QMainWindow):
    """
    除錯主視窗
    
    特性：
    - 分頁式操作介面
    - 模組測試功能
    - 整合測試控制
    - 系統狀態監控
    - 即時日誌顯示
    """
    
    # 信號定義
    test_requested = pyqtSignal(str, dict) if pyqtSignal else None
    module_action = pyqtSignal(str, str) if pyqtSignal else None
    
    def __init__(self, ui_module=None):
        super().__init__()
        self.ui_module = ui_module
        self.test_tabs = {}
        self.current_test_session = None
        
        if not PYQT5_AVAILABLE:
            error_log("[DebugMainWindow] PyQt5 未安裝，無法初始化除錯介面")
            return
        
        # 初始化日誌攔截器
        try:
            install_interceptor()
            debug_log(OPERATION_LEVEL, "[DebugMainWindow] 日誌攔截器初始化成功")
        except Exception as e:
            error_log(f"[DebugMainWindow] 日誌攔截器初始化失敗: {str(e)}")
        
        self.init_ui()
        self.setup_connections()
        self.load_module_states()
        
        info_log("[DebugMainWindow] 除錯主視窗初始化完成")
    
    def init_ui(self):
        """初始化使用者介面"""
        self.setWindowTitle("U.E.P Debug Interface v2.0")
        self.setGeometry(100, 100, 1200, 800)
        
        # 設置應用程式樣式
        self.setup_styles()
        
        # 建立選單列
        self.create_menu_bar()
        
        # 建立主要介面
        self.create_main_interface()
        
        # 建立狀態列
        self.create_status_bar()
        
        # 設置視窗圖示（如果有的話）
        try:
            icon_path = os.path.join(project_root, "arts", "U.E.P.png")
            if os.path.exists(icon_path):
                self.setWindowIcon(QIcon(icon_path))
        except:
            pass
    
    def setup_styles(self):
        """設置應用程式樣式"""
        self.setStyleSheet("""
            QMainWindow {
                background-color: #1e1e1e;
                color: #ffffff;
            }
            
            QTabWidget::pane {
                border: 1px solid #404040;
                background-color: #2d2d2d;
                border-radius: 4px;
            }
            
            QTabWidget::tab-bar {
                alignment: center;
            }
            
            QTabBar::tab {
                background-color: #404040;
                color: #ffffff;
                padding: 8px 16px;
                margin-right: 2px;
                border-top-left-radius: 4px;
                border-top-right-radius: 4px;
                min-width: 120px;
                font-weight: bold;
            }
            
            QTabBar::tab:selected {
                background-color: #2d2d2d;
                border-bottom: 2px solid #0078d4;
            }
            
            QTabBar::tab:hover {
                background-color: #505050;
            }
            
            QPushButton {
                background-color: #404040;
                color: #ffffff;
                border: 1px solid #606060;
                padding: 8px 16px;
                border-radius: 4px;
                font-weight: bold;
                min-width: 80px;
            }
            
            QPushButton:hover {
                background-color: #505050;
                border-color: #707070;
            }
            
            QPushButton:pressed {
                background-color: #353535;
            }
            
            QPushButton:disabled {
                background-color: #2a2a2a;
                color: #808080;
                border-color: #404040;
            }
            
            QLabel {
                color: #ffffff;
                font-family: "Segoe UI", Arial, sans-serif;
            }
            
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
            
            QStatusBar {
                background-color: #2d2d2d;
                color: #ffffff;
                border-top: 1px solid #404040;
            }
            
            QMenuBar {
                background-color: #2d2d2d;
                color: #ffffff;
                border-bottom: 1px solid #404040;
            }
            
            QMenuBar::item {
                padding: 4px 8px;
                background-color: transparent;
            }
            
            QMenuBar::item:selected {
                background-color: #404040;
                border-radius: 2px;
            }
            
            QMenu {
                background-color: #2d2d2d;
                color: #ffffff;
                border: 1px solid #404040;
            }
            
            QMenu::item {
                padding: 4px 16px;
            }
            
            QMenu::item:selected {
                background-color: #404040;
            }
            
            QSplitter::handle {
                background-color: #404040;
            }
        """)
    
    def create_menu_bar(self):
        """建立選單列"""
        menubar = self.menuBar()
        
        # 檔案選單
        file_menu = menubar.addMenu('檔案(&F)')
        
        save_log_action = QAction('儲存日誌', self)
        save_log_action.triggered.connect(self.save_logs)
        file_menu.addAction(save_log_action)
        
        file_menu.addSeparator()
        
        exit_action = QAction('退出', self)
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)
        
        # 測試選單
        test_menu = menubar.addMenu('測試(&T)')
        
        run_all_action = QAction('執行所有測試', self)
        run_all_action.triggered.connect(self.run_all_tests)
        test_menu.addAction(run_all_action)
        
        stop_tests_action = QAction('停止測試', self)
        stop_tests_action.triggered.connect(self.stop_tests)
        test_menu.addAction(stop_tests_action)
        
        # 檢視選單
        view_menu = menubar.addMenu('檢視(&V)')
        
        refresh_action = QAction('刷新狀態', self)
        refresh_action.triggered.connect(self.refresh_all_status)
        view_menu.addAction(refresh_action)
        
        # 說明選單
        help_menu = menubar.addMenu('說明(&H)')
        
        about_action = QAction('關於', self)
        about_action.triggered.connect(self.show_about)
        help_menu.addAction(about_action)
    
    def create_main_interface(self):
        """建立主要介面"""
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        
        main_layout = QVBoxLayout(central_widget)
        
        # 建立標題區域
        title_layout = QHBoxLayout()
        
        title_label = QLabel("U.E.P 除錯介面")
        title_label.setFont(QFont("Segoe UI", 16, QFont.Bold))
        title_label.setStyleSheet("color: #0078d4; margin: 8px;")
        title_layout.addWidget(title_label)
        
        title_layout.addStretch()
        
        # 全域控制按鈕
        self.refresh_btn = QPushButton("🔄 刷新")
        self.refresh_btn.clicked.connect(self.refresh_all_status)
        title_layout.addWidget(self.refresh_btn)
        
        self.emergency_stop_btn = QPushButton("🛑 緊急停止")
        self.emergency_stop_btn.clicked.connect(self.emergency_stop)
        self.emergency_stop_btn.setStyleSheet("QPushButton { background-color: #d32f2f; }")
        title_layout.addWidget(self.emergency_stop_btn)
        
        main_layout.addLayout(title_layout)
        
        # 建立分頁介面
        self.create_tab_interface(main_layout)
    
    def create_tab_interface(self, main_layout):
        """建立分頁介面"""
        try:
            self.tab_widget = QTabWidget()
            
            # 系統監控分頁
            if PYQT5_AVAILABLE:
                self.system_tab = SystemMonitorTab(self.ui_module)
                self.tab_widget.addTab(self.system_tab, "📊 系統監控")
                
                # 日誌檢視分頁
                try:
                    self.log_tab = LogViewerTab()
                    # 確保 log_tab 是 QWidget 的實例
                    if isinstance(self.log_tab, QWidget):
                        self.tab_widget.addTab(self.log_tab, "📋 日誌檢視")
                    else:
                        error_log("[DebugMainWindow] LogViewerTab 不是 QWidget 的實例，跳過添加")
                except Exception as e:
                    error_log(f"[DebugMainWindow] 創建日誌分頁失敗: {str(e)}")
                
                # 模組測試分頁
                self.create_module_test_tabs()
                
                # 整合測試分頁
                self.integration_tab = IntegrationTestTab(self.ui_module)
                self.tab_widget.addTab(self.integration_tab, "🔗 整合測試")
                
            main_layout.addWidget(self.tab_widget)
            
        except Exception as e:
            error_log(f"[DebugMainWindow] 建立分頁介面失敗: {str(e)}")
            # 建立一個簡單的標籤，顯示錯誤消息
            error_widget = QLabel("無法建立分頁介面，請確認 PyQt5 已正確安裝") if PYQT5_AVAILABLE else QLabel()
            main_layout.addWidget(error_widget)
    
    def create_module_test_tabs(self):
        """建立模組測試分頁 - 使用延遲載入方式"""
        # 基礎功能模組
        self.module_classes = {
            # 基礎功能模組
            "stt": {"name": "🎤 STT", "class": STTTestTab, "instance": None, "placeholder": None},
            "nlp": {"name": "🧠 NLP", "class": NLPTestTab, "instance": None, "placeholder": None},
            "mem": {"name": "💾 MEM", "class": MEMTestTab, "instance": None, "placeholder": None},
            "llm": {"name": "🤖 LLM", "class": LLMTestTab, "instance": None, "placeholder": None},
            "tts": {"name": "🔊 TTS", "class": TTSTestTab, "instance": None, "placeholder": None},
            "sys": {"name": "⚙️ SYS", "class": SYSTestTab, "instance": None, "placeholder": None},
            
            # 前端模組
            "ui": {"name": "🎨 UI", "class": UITestTab, "instance": None, "placeholder": None},
            "ani": {"name": "🎬 ANI", "class": ANITestTab, "instance": None, "placeholder": None},
            "mov": {"name": "🏃 MOV", "class": MOVTestTab, "instance": None, "placeholder": None}
        }
        
        # 創建空的佔位標籤頁，僅在使用者點擊時才載入實際內容
        for module_id, info in self.module_classes.items():
            placeholder = QWidget()
            placeholder_layout = QVBoxLayout(placeholder)
            
            try:
                # 檢查模組在設定檔中的狀態
                from .module_manager import ModuleManager
                module_manager = ModuleManager()
                module_status = module_manager.get_module_status(module_id)
                
                if module_status['status'] == 'disabled':
                    # 模組被禁用，顯示禁用信息，但仍然允許載入標籤頁
                    disabled_label = QLabel(f"⚠️ {info['name']} 模組已在設定檔中禁用")
                    disabled_label.setAlignment(Qt.AlignCenter)
                    disabled_label.setStyleSheet("color: orange; font-size: 14px; font-weight: bold;")
                    placeholder_layout.addWidget(disabled_label)
                    
                    # 添加提示信息
                    hint_label = QLabel("請在 config.yaml 中啟用該模組以使用測試功能")
                    hint_label.setAlignment(Qt.AlignCenter)
                    hint_label.setStyleSheet("color: white; font-style: italic;")
                    placeholder_layout.addWidget(hint_label)
                    
                    # 添加載入按鈕，仍然允許用戶嘗試載入模組
                    load_btn = QPushButton("嘗試手動載入模組")
                    load_btn.setProperty("module_id", module_id)
                    load_btn.clicked.connect(lambda checked, mid=module_id: self.load_module_manually(mid))
                    placeholder_layout.addWidget(load_btn)
                    
                    tab_name = f"⚠️ {info['name'].split(' ', 1)[1]}"  # 保留原來的名稱，但使用警告標記
                else:
                    # 模組啟用，顯示載入信息
                    loading_label = QLabel(f"載入中 {info['name']}...")
                    loading_label.setAlignment(Qt.AlignCenter)
                    placeholder_layout.addWidget(loading_label)
                    
                    tab_name = info["name"]
                
            except Exception as e:
                # 如果狀態檢查失敗，使用默認設置
                error_log(f"[DebugMainWindow] 檢查模組 {module_id} 狀態失敗: {e}")
                loading_label = QLabel(f"載入中 {info['name']}...")
                loading_label.setAlignment(Qt.AlignCenter)
                placeholder_layout.addWidget(loading_label)
                tab_name = info["name"]
                module_status = {'status': 'unknown'}
            
            self.test_tabs[module_id] = placeholder
            self.module_classes[module_id]["placeholder"] = placeholder
            tab_index = self.tab_widget.addTab(placeholder, tab_name)
            
            # 如果模組被禁用，禁用整個分頁
            if module_status.get('status') == 'disabled':
                self.tab_widget.setTabEnabled(tab_index, False)
            
            # 為佔位標籤頁儲存相關資訊，以便後續載入
            placeholder.setProperty("module_id", module_id)
            
            debug_log(ELABORATIVE_LEVEL, f"[DebugMainWindow] 創建模組佔位分頁: {module_id} ({tab_name}) 於索引 {tab_index}, 狀態: {module_status.get('status', 'unknown')}")
            
        # 連接標籤頁變更信號以實現延遲載入
        self.tab_widget.currentChanged.connect(self.on_tab_changed_lazy_load)
    
    def create_status_bar(self):
        """建立狀態列"""
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        
        # 狀態資訊
        self.status_label = QLabel("就緒")
        self.status_bar.addWidget(self.status_label)
        
        self.status_bar.addPermanentWidget(QLabel("U.E.P Debug Interface v2.0"))
        
        # 更新狀態定時器
        self.status_timer = QTimer()
        self.status_timer.timeout.connect(self.update_status)
        self.status_timer.start(5000)  # 每5秒更新，降低頻率以減輕負擔
    
    def setup_connections(self):
        """設置信號連接"""
        if not pyqtSignal:
            return
        
        # 連接測試請求信號
        if self.test_requested:
            self.test_requested.connect(self.handle_test_request)
            
        # 注意：延遲載入邏輯已在 create_module_test_tabs 中連接了分頁切換信號
        # 我們不需要在這裡再次連接 self.tab_widget.currentChanged.connect(self.on_tab_changed)
    
    def load_module_states(self):
        """載入模組狀態"""
        if self.ui_module and hasattr(self.ui_module, 'get_module_states'):
            try:
                states = self.ui_module.get_module_states()
                self.update_module_states(states)
            except Exception as e:
                error_log(f"[DebugMainWindow] 載入模組狀態失敗: {e}")
    
    def update_module_states(self, states: dict):
        """更新模組狀態"""
        for module_id, state in states.items():
            if module_id in self.test_tabs:
                tab = self.test_tabs[module_id]
                if hasattr(tab, 'update_module_state'):
                    tab.update_module_state(state)
    
    def handle_test_request(self, test_id: str, params: dict):
        """處理測試請求"""
        debug_log(SYSTEM_LEVEL, f"[DebugMainWindow] 處理測試請求: {test_id}, 參數: {params}")
        
        try:
            # 更新狀態
            self.status_label.setText(f"執行測試: {test_id}")
            
            # 透過 UI 模組執行測試
            if self.ui_module and hasattr(self.ui_module, 'run_test'):
                result = self.ui_module.run_test(test_id, params)
                self.handle_test_result(test_id, result)
            else:
                error_log("[DebugMainWindow] UI 模組不支援測試執行")
                
        except Exception as e:
            error_log(f"[DebugMainWindow] 測試執行異常: {e}")
            self.status_label.setText("測試執行失敗")
    
    def handle_test_result(self, test_id: str, result: dict):
        """處理測試結果"""
        if result.get('success'):
            self.status_label.setText(f"測試完成: {test_id}")
            info_log(f"[DebugMainWindow] 測試 {test_id} 執行成功")
        else:
            self.status_label.setText(f"測試失敗: {test_id}")
            error_log(f"[DebugMainWindow] 測試 {test_id} 執行失敗: {result.get('error', 'Unknown error')}")
        
        # 將結果傳遞給日誌分頁
        if hasattr(self, 'log_tab'):
            self.log_tab.add_test_result(test_id, result)
    
    def on_tab_changed_lazy_load(self, index: int):
        """延遲載入標籤頁內容"""
        if index < 0:  # 避免無效的索引
            return
            
        current_widget = self.tab_widget.currentWidget()
        
        # 檢查是否為佔位標籤頁
        if current_widget and current_widget.property("module_id"):
            module_id = current_widget.property("module_id")
            
            # 確保當前標籤頁已啟用
            # 注意：被禁用的標籤頁仍然應該可以顯示"模組已禁用"信息，所以不再跳過禁用的分頁
            debug_log(OPERATION_LEVEL, f"[DebugMainWindow] 載入標籤頁內容: {module_id} (index: {index})")
            
            # 檢查是否已經創建了該模組的實例
            if module_id in self.module_classes:
                info = self.module_classes[module_id]
                
                if info["instance"] is None:
                    try:
                        # 創建實際的標籤頁內容
                        info_log(f"[DebugMainWindow] 延遲載入模組: {module_id}")
                        tab_class = info["class"]
                        new_tab = tab_class(self.ui_module)
                        
                        # 儲存實例
                        info["instance"] = new_tab
                        self.test_tabs[module_id] = new_tab
                        
                        # 替換佔位標籤頁
                        tab_name = info["name"]
                        debug_log(SYSTEM_LEVEL, f"[DebugMainWindow] 替換佔位標籤頁: {module_id} ({tab_name}) 於索引 {index}")
                        
                        # 獲取當前標籤頁的索引（可能已經更改）
                        current_index = self.tab_widget.indexOf(current_widget)
                        if current_index >= 0:  # 確保找到了標籤頁
                            self.tab_widget.removeTab(current_index)
                            self.tab_widget.insertTab(current_index, new_tab, tab_name)
                            self.tab_widget.setCurrentIndex(current_index)
                            
                            # 刷新新載入的標籤頁
                            if hasattr(new_tab, 'refresh_status'):
                                new_tab.refresh_status()
                        else:
                            error_log(KEY_LEVEL, f"[DebugMainWindow] 無法找到佔位標籤頁 {module_id} 的索引")
                    except Exception as e:
                        error_log(KEY_LEVEL, f"[DebugMainWindow] 延遲載入 {module_id} 測試分頁失敗: {e}")
                        return  # 載入失敗時直接返回，不執行後續邏輯
                else:
                    # 模組實例已經存在但可能未顯示，確保切換到正確的標籤頁
                    instance = info["instance"]
                    tab_index = self.tab_widget.indexOf(instance)
                    
                    if tab_index >= 0 and tab_index != index:
                        debug_log(OPERATION_LEVEL, f"[DebugMainWindow] 切換到已存在的模組標籤頁: {module_id} (索引 {tab_index})")
                        self.tab_widget.setCurrentIndex(tab_index)
                        return  # 避免呼叫 on_tab_changed
                        
        # 執行標準的標籤頁切換邏輯
        self.on_tab_changed(index)
            
    def load_module_manually(self, module_id):
        """手動載入模組（即使在設定檔中被禁用）"""
        debug_log(KEY_LEVEL, f"[DebugMainWindow] 嘗試手動載入模組: {module_id}")
        
        try:
            # 獲取模組管理器實例
            from .module_manager import ModuleManager
            module_manager = ModuleManager()
            
            # 嘗試載入模組
            result = module_manager.load_module(module_id)
            
            if result.get('success', False):
                info_log(f"[DebugMainWindow] 成功手動載入模組: {module_id}")
                
                # 找到並重新載入相應的標籤頁
                info = self.module_classes.get(module_id)
                if info:
                    # 創建實際的標籤頁內容
                    tab_class = info["class"]
                    new_tab = tab_class(self.ui_module)
                    
                    # 儲存實例
                    self.module_classes[module_id]["instance"] = new_tab
                    self.test_tabs[module_id] = new_tab
                    
                    # 找到相應的標籤頁索引
                    for i in range(self.tab_widget.count()):
                        widget = self.tab_widget.widget(i)
                        if widget and widget.property("module_id") == module_id:
                            # 替換標籤頁
                            tab_name = info["name"]
                            self.tab_widget.removeTab(i)
                            self.tab_widget.insertTab(i, new_tab, tab_name)
                            self.tab_widget.setCurrentIndex(i)
                            break
                
                self.status_label.setText(f"模組 {module_id} 已手動載入")
            else:
                error_log(f"[DebugMainWindow] 手動載入模組 {module_id} 失敗: {result.get('error', '未知錯誤')}")
                self.status_label.setText(f"模組 {module_id} 載入失敗")
        
        except Exception as e:
            error_log(f"[DebugMainWindow] 手動載入模組 {module_id} 出錯: {e}")
            self.status_label.setText(f"模組 {module_id} 載入出錯")
            
            # 確認這是佔位符而不是已加載的標籤頁
            if module_id in self.module_classes:
                info = self.module_classes[module_id]
                
                # 檢查是否已經創建了該模組的實例
                if info["instance"] is None:
                    try:
                        # 創建實際的標籤頁內容
                        info_log(f"[DebugMainWindow] 延遲載入模組: {module_id}")
                        tab_class = info["class"]
                        new_tab = tab_class(self.ui_module)
                        
                        # 儲存實例
                        self.module_classes[module_id]["instance"] = new_tab
                        self.test_tabs[module_id] = new_tab
                        
                        # 找到相應的標籤頁索引
                        found_tab = False
                        for i in range(self.tab_widget.count()):
                            widget = self.tab_widget.widget(i)
                            if widget and widget.property("module_id") == module_id:
                                # 替換標籤頁
                                tab_name = info["name"]
                                debug_log(SYSTEM_LEVEL, f"[DebugMainWindow] 替換佔位標籤頁: {module_id} ({tab_name}) 於索引 {i}")
                                self.tab_widget.removeTab(i)
                                self.tab_widget.insertTab(i, new_tab, tab_name)
                                self.tab_widget.setCurrentIndex(i)
                                found_tab = True
                                
                                # 刷新新載入的標籤頁
                                if hasattr(new_tab, 'refresh_status'):
                                    new_tab.refresh_status()
                                break
                                
                        if not found_tab:
                            error_log(KEY_LEVEL, f"[DebugMainWindow] 無法找到佔位標籤頁 {module_id} 的索引")
                    
                    except Exception as e:
                        error_log(KEY_LEVEL, f"[DebugMainWindow] 延遲載入 {module_id} 測試分頁失敗: {e}")
                else:
                    # 模組實例已經存在但可能未顯示，確保切換到正確的標籤頁
                    instance = info["instance"]
                    tab_index = self.tab_widget.indexOf(instance)
                    
                    if tab_index >= 0:
                        debug_log(OPERATION_LEVEL, f"[DebugMainWindow] 切換到已存在的模組標籤頁: {module_id} (索引 {tab_index})")
                        self.tab_widget.setCurrentIndex(tab_index)
    
    def on_tab_changed(self, index: int):
        """分頁切換事件"""
        if index < 0 or index >= self.tab_widget.count():
            return
            
        tab_name = self.tab_widget.tabText(index)
        debug_log(ELABORATIVE_LEVEL, f"[DebugMainWindow] 切換到分頁: {tab_name}")
        
        # 刷新當前分頁的狀態
        current_widget = self.tab_widget.widget(index)
        if current_widget and hasattr(current_widget, 'refresh_status'):
            current_widget.refresh_status()
    
    def run_all_tests(self):
        """執行所有測試"""
        debug_log(OPERATION_LEVEL, "[DebugMainWindow] 開始執行所有測試")
        self.status_label.setText("執行所有測試...")
        
        # 這裡可以實現批次測試邏輯
        if hasattr(self, 'integration_tab'):
            self.integration_tab.run_full_test_suite()
    
    def stop_tests(self):
        """停止測試"""
        debug_log(OPERATION_LEVEL, "[DebugMainWindow] 停止測試")
        self.status_label.setText("正在停止測試...")
        
        # 通知所有分頁停止測試
        for tab in self.test_tabs.values():
            if hasattr(tab, 'stop_tests'):
                tab.stop_tests()
    
    def emergency_stop(self):
        """緊急停止"""
        debug_log(KEY_LEVEL, "[DebugMainWindow] 緊急停止")
        self.status_label.setText("緊急停止")
        
        # 立即停止所有操作
        self.stop_tests()
        
        # 通知 UI 模組
        if self.ui_module and hasattr(self.ui_module, 'emergency_stop'):
            self.ui_module.emergency_stop()
    
    def refresh_all_status(self):
        """刷新所有狀態"""
        debug_log(ELABORATIVE_LEVEL, "[DebugMainWindow] 刷新所有狀態")
        self.status_label.setText("刷新狀態中...")
        
        # 刷新系統監控
        if hasattr(self, 'system_tab'):
            self.system_tab.refresh_status()
        
        # 刷新所有測試分頁
        for tab in self.test_tabs.values():
            if hasattr(tab, 'refresh_status'):
                tab.refresh_status()
        
        # 重新載入模組狀態
        self.load_module_states()
        
        self.status_label.setText("狀態已刷新")
    
    def update_status(self):
        """定期更新狀態"""
        # 更新日誌分頁資訊
        if hasattr(self, 'log_tab') and self.log_tab:
            try:
                # 獲取日誌條目數量
                log_count = len(getattr(self.log_tab, 'log_entries', []))
                filtered_count = len(getattr(self.log_tab, 'filtered_entries', []))
                
                # 獲取日誌級別分布
                debug_count = info_count = warning_count = error_count = 0
                for entry in getattr(self.log_tab, 'log_entries', []):
                    level = entry.get('level', '').upper()
                    if level == 'DEBUG':
                        debug_count += 1
                    elif level == 'INFO':
                        info_count += 1
                    elif level == 'WARNING':
                        warning_count += 1
                    elif level in ['ERROR', 'CRITICAL']:
                        error_count += 1
                
                # 更新狀態欄顯示
                self.status_label.setText(f"日誌: {filtered_count}/{log_count} 條 [E:{error_count} W:{warning_count} I:{info_count} D:{debug_count}]")
                
                # 如果日誌數量過多，顯示警告
                if log_count > 5000:
                    warning_threshold = 5000
                    critical_threshold = 10000
                    
                    if log_count > critical_threshold:
                        # 嚴重警告
                        info_log(f"[DebugMainWindow] 日誌數量過多 ({log_count} > {critical_threshold})，強烈建議清理!", "WARNING")
                        # 通過改變狀態欄顏色提醒用戶
                        self.status_bar.setStyleSheet("QStatusBar { background-color: #d32f2f; color: white; }")
                    elif log_count > warning_threshold:
                        # 一般警告
                        info_log(f"[DebugMainWindow] 日誌數量較多 ({log_count} > {warning_threshold})，建議清理", "INFO")
                        self.status_bar.setStyleSheet("QStatusBar { background-color: #ff9800; color: black; }")
                    
                    # 更新日誌分頁的UI，顯示警告
                    if hasattr(self.log_tab, 'update_log_count_warning'):
                        self.log_tab.update_log_count_warning(log_count)
                else:
                    # 恢復正常狀態
                    self.status_bar.setStyleSheet("")
            except Exception as e:
                debug_log(OPERATION_LEVEL, f"[DebugMainWindow] 更新日誌狀態時出錯: {e}")
    
    def save_logs(self):
        """儲存日誌"""
        if hasattr(self, 'log_tab'):
            self.log_tab.save_logs()
    
    def show_about(self):
        """顯示關於對話框"""
        from PyQt5.QtWidgets import QMessageBox
        
        QMessageBox.about(self, "關於 U.E.P Debug Interface", 
                         "U.E.P Debug Interface v2.0\n\n"
                         "統一除錯介面系統\n"
                         "支援模組測試、整合測試、系統監控等功能")
    
    def closeEvent(self, event):
        """視窗關閉事件"""
        debug_log(OPERATION_LEVEL, "[DebugMainWindow] 除錯介面正在關閉")
        
        # 停止所有正在進行的測試
        self.stop_tests()
        
        # 停止所有計時器
        if hasattr(self, 'status_timer') and self.status_timer:
            self.status_timer.stop()
        
        # 清理所有測試分頁
        for module_id, tab in self.test_tabs.items():
            if hasattr(tab, 'closeEvent'):
                try:
                    tab.closeEvent(event)
                except:
                    pass
        
        # 卸載日誌攔截器
        try:
            uninstall_interceptor()
            debug_log(OPERATION_LEVEL, "[DebugMainWindow] 日誌攔截器已卸載")
        except Exception as e:
            error_log(f"[DebugMainWindow] 日誌攔截器卸載失敗: {str(e)}")
        
        # 清理其他分頁
        for attr in ['system_tab', 'log_tab', 'integration_tab']:
            if hasattr(self, attr) and getattr(self, attr) and hasattr(getattr(self, attr), 'closeEvent'):
                try:
                    getattr(self, attr).closeEvent(event)
                except:
                    pass
        
        # 清理背景工作線程
        try:
            from .background_worker import get_worker_manager
            worker_manager = get_worker_manager()
            worker_manager.stop_all_tasks()
        except:
            pass
            
        # 儲存設定等清理工作
        event.accept()


def launch_debug_interface(ui_module=None, blocking=True):
    """
    啟動除錯介面
    
    Args:
        ui_module: UI 模組實例
        blocking: 是否阻塞執行（啟動事件循環）
    
    Returns:
        除錯介面實例或 None
    """
    if not PYQT5_AVAILABLE:
        error_log("PyQt5 未安裝，無法啟動除錯介面")
        return None
    
    from PyQt5.QtWidgets import QApplication, QSplashScreen
    from PyQt5.QtGui import QPixmap
    from PyQt5.QtCore import Qt, QTimer, QCoreApplication
    import sys
    import time
    
    # 設置進程優先級
    try:
        import platform
        import os
        if platform.system() == "Windows":
            try:
                # 在Windows上使用psutil設置進程優先級
                import psutil
                p = psutil.Process(os.getpid())
                p.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
            except:
                pass
        elif platform.system() in ("Linux", "Darwin"):
            try:
                # 在Linux/macOS上設置進程優先級
                os.nice(10)
            except:
                pass
    except:
        pass
    
    # 檢查是否已有 QApplication 實例
    app = QApplication.instance()
    if app is None:
        # 設置應用程式屬性以優化效能
        QCoreApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
        QCoreApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
        
        # 建立應用程式實例，設置風格
        app = QApplication(sys.argv)
        app.setStyle('Fusion')  # 使用Fusion風格以提高一致性和效能
    
    # 顯示啟動畫面，減輕使用者等待感
    splash = None
    try:
        splash_path = os.path.join(project_root, "arts", "U.E.P.png")
        if os.path.exists(splash_path):
            splash = QSplashScreen(QPixmap(splash_path))
            splash.showMessage("正在載入除錯介面...", Qt.AlignBottom | Qt.AlignCenter, Qt.white)
            splash.show()
            app.processEvents()
    except Exception as e:
        error_log(f"無法顯示啟動畫面: {e}")
    
    # 建立主視窗
    window = DebugMainWindow(ui_module)
    
    def finish_loading():
        window.show()
        if splash:
            splash.finish(window)
        info_log("除錯介面已啟動")
    
    # 延遲顯示主視窗，先處理初始化工作
    QTimer.singleShot(500, finish_loading)
    
    # 添加垃圾回收以優化記憶體使用
    import gc
    gc.collect()
    
    # 如果是阻塞模式，啟動事件循環
    if blocking:
        try:
            info_log("除錯介面進入事件循環")
            return_code = app.exec_()
            if return_code != 0:
                sys.exit(return_code)
        except KeyboardInterrupt:
            info_log("用戶中斷除錯介面")
        except Exception as e:
            error_log(f"除錯介面異常: {e}")
        finally:
            # 確保資源釋放
            if window:
                window.close()
            # 再次執行垃圾回收
            gc.collect()
    
    return window


if __name__ == "__main__":
    # 直接運行測試
    window = launch_debug_interface()
    if window and PYQT5_AVAILABLE:
        from PyQt5.QtWidgets import QApplication
        import sys
        sys.exit(QApplication.instance().exec_())
