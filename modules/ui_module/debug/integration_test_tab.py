# debug/integration_test_tab.py
"""
Integration Test Tab

整合測試分頁
提供模組間整合測試功能
"""

import os
import sys
from typing import Dict, Any, Optional, List

try:
    from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QGroupBox,
                                QPushButton, QTextEdit, QLabel, QComboBox,
                                QCheckBox, QProgressBar, QTableWidget,
                                QTableWidgetItem, QHeaderView, QSplitter,
                                QTabWidget, QFormLayout, QSpinBox)
    from PyQt5.QtCore import Qt, QTimer, pyqtSignal, QThread
    from PyQt5.QtGui import QFont, QColor
    PYQT5_AVAILABLE = True
except ImportError:
    PYQT5_AVAILABLE = False
    QWidget = object
    pyqtSignal = None

# 添加項目根目錄到 Python 路徑
script_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.abspath(os.path.join(script_dir, '..', '..', '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from utils.debug_helper import debug_log, info_log, error_log


class IntegrationTestTab(QWidget):
    """
    整合測試分頁
    
    特性：
    - 模組間整合測試
    - 完整管道測試
    - 前端整合測試
    - 測試套件管理
    """
    
    test_requested = pyqtSignal(str, dict) if pyqtSignal else None
    
    def __init__(self):
        super().__init__()
        self.running_tests = []
        self.test_results = {}
        
        if PYQT5_AVAILABLE:
            self.init_ui()
            self.setup_connections()
        
        debug_log(1, "[IntegrationTestTab] 整合測試分頁初始化完成")
    
    def init_ui(self):
        """初始化介面"""
        layout = QVBoxLayout(self)
        
        # 建立標籤頁
        self.tab_widget = QTabWidget()
        
        # 模組整合測試
        self.create_module_integration_tab()
        
        # 前端整合測試
        self.create_frontend_integration_tab()
        
        # 完整管道測試
        self.create_pipeline_test_tab()
        
        # 測試結果檢視
        self.create_results_tab()
        
        layout.addWidget(self.tab_widget)
        
        # 設置樣式
        self.setup_styles()
    
    def create_module_integration_tab(self):
        """建立模組整合測試分頁"""
        module_widget = QWidget()
        layout = QVBoxLayout(module_widget)
        
        # 可用整合測試
        available_group = QGroupBox("可用的整合測試")
        available_layout = QVBoxLayout(available_group)
        
        # STT + NLP 整合 (已重構)
        stt_nlp_layout = QHBoxLayout()
        
        stt_nlp_btn = QPushButton("🎤🧠 STT + NLP 整合測試")
        stt_nlp_btn.clicked.connect(lambda: self.run_integration_test("stt+nlp"))
        stt_nlp_btn.setStyleSheet("QPushButton { background-color: #388e3c; }")
        stt_nlp_layout.addWidget(stt_nlp_btn)
        
        stt_nlp_label = QLabel("✅ 已重構")
        stt_nlp_label.setStyleSheet("color: #4caf50; font-weight: bold;")
        stt_nlp_layout.addWidget(stt_nlp_label)
        
        stt_nlp_layout.addStretch()
        available_layout.addLayout(stt_nlp_layout)
        
        layout.addWidget(available_group)
        
        # 待重構整合測試
        pending_group = QGroupBox("待重構的整合測試")
        pending_layout = QVBoxLayout(pending_group)
        
        pending_tests = [
            ("nlp+mem", "🧠💾 NLP + MEM"),
            ("nlp+llm", "🧠🤖 NLP + LLM"),
            ("llm+tts", "🤖🔊 LLM + TTS"),
            ("mem+llm", "💾🤖 MEM + LLM"),
            ("stt+tts", "🎤🔊 STT + TTS")
        ]
        
        for test_id, test_name in pending_tests:
            test_layout = QHBoxLayout()
            
            test_btn = QPushButton(test_name + " 整合測試")
            test_btn.clicked.connect(lambda checked, tid=test_id: self.run_integration_test(tid))
            test_btn.setEnabled(False)
            test_btn.setStyleSheet("QPushButton { background-color: #616161; }")
            test_layout.addWidget(test_btn)
            
            test_label = QLabel("⏳ 待重構")
            test_label.setStyleSheet("color: #ff9800; font-weight: bold;")
            test_layout.addWidget(test_label)
            
            test_layout.addStretch()
            pending_layout.addLayout(test_layout)
        
        layout.addWidget(pending_group)
        
        # 自訂整合測試
        custom_group = QGroupBox("自訂整合測試")
        custom_layout = QVBoxLayout(custom_group)
        
        # 模組選擇
        selection_layout = QFormLayout()
        
        self.module_checkboxes = {}
        modules = ["stt", "nlp", "mem", "llm", "tts", "sys"]
        
        checkbox_layout = QHBoxLayout()
        for module in modules:
            checkbox = QCheckBox(module.upper())
            self.module_checkboxes[module] = checkbox
            checkbox_layout.addWidget(checkbox)
        
        selection_layout.addRow("選擇模組:", checkbox_layout)
        custom_layout.addLayout(selection_layout)
        
        # 執行按鈕
        custom_test_btn = QPushButton("🚀 執行自訂整合測試")
        custom_test_btn.clicked.connect(self.run_custom_integration)
        custom_layout.addWidget(custom_test_btn)
        
        layout.addWidget(custom_group)
        
        self.tab_widget.addTab(module_widget, "🔗 模組整合")
    
    def create_frontend_integration_tab(self):
        """建立前端整合測試分頁"""
        frontend_widget = QWidget()
        layout = QVBoxLayout(frontend_widget)
        
        # 前端模組狀態
        status_group = QGroupBox("前端模組狀態")
        status_layout = QVBoxLayout(status_group)
        
        self.frontend_status_table = QTableWidget()
        self.frontend_status_table.setColumnCount(3)
        self.frontend_status_table.setHorizontalHeaderLabels(["模組", "狀態", "介面數量"])
        
        if QHeaderView:
            header = self.frontend_status_table.horizontalHeader()
            header.setStretchLastSection(True)
        
        status_layout.addWidget(self.frontend_status_table)
        
        refresh_frontend_btn = QPushButton("🔄 刷新前端狀態")
        refresh_frontend_btn.clicked.connect(self.refresh_frontend_status)
        status_layout.addWidget(refresh_frontend_btn)
        
        layout.addWidget(status_group)
        
        # 前端測試控制
        frontend_tests_group = QGroupBox("前端整合測試")
        frontend_tests_layout = QVBoxLayout(frontend_tests_group)
        
        # 完整前端測試
        full_test_btn = QPushButton("🎨 完整前端整合測試")
        full_test_btn.clicked.connect(lambda: self.run_frontend_test("full"))
        full_test_btn.setStyleSheet("QPushButton { background-color: #1976d2; font-size: 14px; padding: 10px; }")
        frontend_tests_layout.addWidget(full_test_btn)
        
        # 分項測試
        sub_tests_layout = QHBoxLayout()
        
        status_test_btn = QPushButton("📊 模組狀態測試")
        status_test_btn.clicked.connect(lambda: self.run_frontend_test("status"))
        sub_tests_layout.addWidget(status_test_btn)
        
        communication_test_btn = QPushButton("📡 通訊測試")
        communication_test_btn.clicked.connect(lambda: self.run_frontend_test("communication"))
        sub_tests_layout.addWidget(communication_test_btn)
        
        animation_test_btn = QPushButton("🎬 動畫測試")
        animation_test_btn.clicked.connect(lambda: self.run_frontend_test("animation"))
        sub_tests_layout.addWidget(animation_test_btn)
        
        frontend_tests_layout.addLayout(sub_tests_layout)
        
        layout.addWidget(frontend_tests_group)
        
        self.tab_widget.addTab(frontend_widget, "🎨 前端整合")
    
    def create_pipeline_test_tab(self):
        """建立完整管道測試分頁"""
        pipeline_widget = QWidget()
        layout = QVBoxLayout(pipeline_widget)
        
        # 管道配置
        config_group = QGroupBox("管道配置")
        config_layout = QFormLayout(config_group)
        
        # 測試模式選擇
        self.pipeline_mode = QComboBox()
        self.pipeline_mode.addItems(["除錯模式", "生產模式"])
        config_layout.addRow("測試模式:", self.pipeline_mode)
        
        # 測試輪數
        self.test_rounds = QSpinBox()
        self.test_rounds.setRange(1, 10)
        self.test_rounds.setValue(1)
        config_layout.addRow("測試輪數:", self.test_rounds)
        
        layout.addWidget(config_group)
        
        # 管道測試控制
        pipeline_control_group = QGroupBox("管道測試控制")
        pipeline_control_layout = QVBoxLayout(pipeline_control_group)
        
        # 完整管道測試
        full_pipeline_btn = QPushButton("🚀 完整管道測試")
        full_pipeline_btn.clicked.connect(self.run_full_pipeline)
        full_pipeline_btn.setStyleSheet("QPushButton { background-color: #7b1fa2; font-size: 16px; padding: 12px; }")
        pipeline_control_layout.addWidget(full_pipeline_btn)
        
        # 階段測試
        stage_layout = QHBoxLayout()
        
        input_stage_btn = QPushButton("1️⃣ 輸入階段")
        input_stage_btn.clicked.connect(lambda: self.run_pipeline_stage("input"))
        stage_layout.addWidget(input_stage_btn)
        
        process_stage_btn = QPushButton("2️⃣ 處理階段")
        process_stage_btn.clicked.connect(lambda: self.run_pipeline_stage("process"))
        stage_layout.addWidget(process_stage_btn)
        
        output_stage_btn = QPushButton("3️⃣ 輸出階段")
        output_stage_btn.clicked.connect(lambda: self.run_pipeline_stage("output"))
        stage_layout.addWidget(output_stage_btn)
        
        pipeline_control_layout.addLayout(stage_layout)
        
        layout.addWidget(pipeline_control_group)
        
        # 進度顯示
        progress_group = QGroupBox("執行進度")
        progress_layout = QVBoxLayout(progress_group)
        
        self.pipeline_progress = QProgressBar()
        self.pipeline_progress.setRange(0, 100)
        progress_layout.addWidget(self.pipeline_progress)
        
        self.pipeline_status = QLabel("就緒")
        progress_layout.addWidget(self.pipeline_status)
        
        layout.addWidget(progress_group)
        
        self.tab_widget.addTab(pipeline_widget, "🚀 完整管道")
    
    def create_results_tab(self):
        """建立測試結果分頁"""
        results_widget = QWidget()
        layout = QVBoxLayout(results_widget)
        
        # 結果表格
        self.results_table = QTableWidget()
        self.results_table.setColumnCount(5)
        self.results_table.setHorizontalHeaderLabels(["測試名稱", "狀態", "開始時間", "執行時間", "結果"])
        
        if QHeaderView:
            header = self.results_table.horizontalHeader()
            header.setStretchLastSection(True)
        
        layout.addWidget(self.results_table)
        
        # 控制按鈕
        control_layout = QHBoxLayout()
        
        clear_results_btn = QPushButton("🗑️ 清空結果")
        clear_results_btn.clicked.connect(self.clear_test_results)
        control_layout.addWidget(clear_results_btn)
        
        export_results_btn = QPushButton("💾 匯出結果")
        export_results_btn.clicked.connect(self.export_test_results)
        control_layout.addWidget(export_results_btn)
        
        refresh_results_btn = QPushButton("🔄 刷新")
        refresh_results_btn.clicked.connect(self.refresh_test_results)
        control_layout.addWidget(refresh_results_btn)
        
        control_layout.addStretch()
        
        stop_all_btn = QPushButton("⏹️ 停止所有測試")
        stop_all_btn.clicked.connect(self.stop_all_tests)
        stop_all_btn.setStyleSheet("QPushButton { background-color: #d32f2f; }")
        control_layout.addWidget(stop_all_btn)
        
        layout.addLayout(control_layout)
        
        self.tab_widget.addTab(results_widget, "📊 測試結果")
    
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
            
            QTableWidget {
                background-color: #2d2d2d;
                border: 1px solid #404040;
                border-radius: 4px;
                gridline-color: #404040;
            }
            
            QTableWidget::item {
                padding: 4px;
                border-bottom: 1px solid #404040;
            }
            
            QTableWidget::item:selected {
                background-color: #404040;
            }
            
            QHeaderView::section {
                background-color: #404040;
                color: #ffffff;
                padding: 4px;
                border: 1px solid #606060;
                font-weight: bold;
            }
        """)
    
    def setup_connections(self):
        """設置信號連接"""
        if pyqtSignal and self.test_requested:
            self.test_requested.connect(self.handle_test_request)
    
    def run_integration_test(self, test_combination: str):
        """執行整合測試"""
        debug_log(1, f"[IntegrationTestTab] 執行整合測試: {test_combination}")
        
        if "+" not in test_combination:
            self.add_test_result(test_combination, "ERROR", "無效的測試組合格式")
            return
        
        modules = test_combination.split("+")
        
        # 檢查模組可用性
        unavailable_modules = []
        for module in modules:
            if not self.is_module_available(module):
                unavailable_modules.append(module)
        
        if unavailable_modules:
            error_msg = f"模組未載入: {', '.join(unavailable_modules)}"
            self.add_test_result(test_combination, "ERROR", error_msg)
            return
        
        # 執行測試
        if self.test_requested:
            params = {
                "modules": modules,
                "type": "integration"
            }
            self.test_requested.emit(f"integration_{test_combination.replace('+', '_')}", params)
        
        self.add_test_result(test_combination, "RUNNING", "正在執行...")
    
    def run_custom_integration(self):
        """執行自訂整合測試"""
        selected_modules = []
        for module, checkbox in self.module_checkboxes.items():
            if checkbox.isChecked():
                selected_modules.append(module)
        
        if len(selected_modules) < 2:
            self.add_test_result("自訂整合", "ERROR", "至少需要選擇兩個模組")
            return
        
        test_name = "+".join(selected_modules)
        self.run_integration_test(test_name)
    
    def run_frontend_test(self, test_type: str):
        """執行前端測試"""
        debug_log(1, f"[IntegrationTestTab] 執行前端測試: {test_type}")
        
        if self.test_requested:
            params = {"type": test_type}
            self.test_requested.emit(f"frontend_{test_type}", params)
        
        self.add_test_result(f"前端_{test_type}", "RUNNING", "正在執行...")
    
    def run_full_pipeline(self):
        """執行完整管道測試"""
        debug_log(1, "[IntegrationTestTab] 執行完整管道測試")
        
        mode = "production" if self.pipeline_mode.currentText() == "生產模式" else "debug"
        rounds = self.test_rounds.value()
        
        if self.test_requested:
            params = {
                "mode": mode,
                "rounds": rounds
            }
            self.test_requested.emit("full_pipeline", params)
        
        self.pipeline_status.setText(f"執行 {rounds} 輪 {mode} 模式測試...")
        self.pipeline_progress.setValue(0)
        self.add_test_result("完整管道", "RUNNING", f"{mode} 模式, {rounds} 輪")
    
    def run_pipeline_stage(self, stage: str):
        """執行管道階段測試"""
        debug_log(1, f"[IntegrationTestTab] 執行管道階段測試: {stage}")
        
        if self.test_requested:
            params = {"stage": stage}
            self.test_requested.emit(f"pipeline_stage_{stage}", params)
        
        self.add_test_result(f"管道階段_{stage}", "RUNNING", "正在執行...")
    
    def refresh_frontend_status(self):
        """刷新前端狀態 - 獨立實現"""
        debug_log(1, "[IntegrationTestTab] 刷新前端狀態")
        
        # 透過模組管理器或配置系統獲取狀態
        try:
            # 這裡可以實現直接讀取配置或狀態文件的邏輯
            from .module_manager import ModuleManager
            module_manager = ModuleManager()
            
            # 構建前端狀態信息
            frontend_modules = ['ui', 'ani', 'mov']
            status = {'modules': {}}
            
            for module_id in frontend_modules:
                module_status = module_manager.get_module_status(module_id)
                status['modules'][module_id] = {
                    'state': module_status.get('status', 'unknown'),
                    'interfaces': module_status.get('interfaces', {})
                }
            
            self.update_frontend_status_table(status)
            
        except Exception as e:
            error_log(f"[IntegrationTestTab] 獲取前端狀態失敗: {e}")
            # 顯示錯誤狀態
            status = {'modules': {
                'ui': {'state': 'error', 'interfaces': {}},
                'ani': {'state': 'error', 'interfaces': {}},
                'mov': {'state': 'error', 'interfaces': {}}
            }}
            self.update_frontend_status_table(status)
    
    def update_frontend_status_table(self, status: dict):
        """更新前端狀態表格"""
        if not hasattr(self, 'frontend_status_table'):
            return
        
        modules = status.get('modules', {})
        self.frontend_status_table.setRowCount(len(modules))
        
        for row, (module_id, module_info) in enumerate(modules.items()):
            # 模組名稱
            self.frontend_status_table.setItem(row, 0, QTableWidgetItem(module_id.upper()))
            
            # 狀態
            state = module_info.get('state', 'unknown')
            status_item = QTableWidgetItem(state)
            if state == 'loaded':
                status_item.setBackground(QColor(40, 167, 69))
            elif state == 'error':
                status_item.setBackground(QColor(220, 53, 69))
            else:
                status_item.setBackground(QColor(108, 117, 125))
            self.frontend_status_table.setItem(row, 1, status_item)
            
            # 介面數量
            interface_count = len(module_info.get('interfaces', {}))
            self.frontend_status_table.setItem(row, 2, QTableWidgetItem(str(interface_count)))
    
    def is_module_available(self, module_name: str) -> bool:
        """檢查模組是否可用 - 獨立實現"""
        try:
            from .module_manager import ModuleManager
            module_manager = ModuleManager()
            module_status = module_manager.get_module_status(module_name)
            return module_status.get('status') in ['enabled', 'loaded', 'active']
        except Exception as e:
            error_log(f"[IntegrationTestTab] 檢查模組 {module_name} 可用性失敗: {e}")
            return False
    
    def add_test_result(self, test_name: str, status: str, details: str = ""):
        """新增測試結果"""
        import datetime
        
        timestamp = datetime.datetime.now()
        
        self.test_results[test_name] = {
            "status": status,
            "start_time": timestamp,
            "details": details,
            "duration": None
        }
        
        self.refresh_test_results()
    
    def refresh_test_results(self):
        """刷新測試結果表格"""
        if not hasattr(self, 'results_table'):
            return
        
        self.results_table.setRowCount(len(self.test_results))
        
        for row, (test_name, result) in enumerate(self.test_results.items()):
            # 測試名稱
            self.results_table.setItem(row, 0, QTableWidgetItem(test_name))
            
            # 狀態
            status = result.get('status', 'UNKNOWN')
            status_item = QTableWidgetItem(status)
            
            if status == 'SUCCESS':
                status_item.setBackground(QColor(40, 167, 69))
            elif status == 'ERROR':
                status_item.setBackground(QColor(220, 53, 69))
            elif status == 'RUNNING':
                status_item.setBackground(QColor(255, 152, 0))
            else:
                status_item.setBackground(QColor(108, 117, 125))
            
            self.results_table.setItem(row, 1, status_item)
            
            # 開始時間
            start_time = result.get('start_time')
            if start_time:
                time_str = start_time.strftime("%H:%M:%S")
                self.results_table.setItem(row, 2, QTableWidgetItem(time_str))
            
            # 執行時間
            duration = result.get('duration')
            if duration:
                self.results_table.setItem(row, 3, QTableWidgetItem(f"{duration:.2f}s"))
            elif status == 'RUNNING':
                self.results_table.setItem(row, 3, QTableWidgetItem("執行中..."))
            
            # 結果詳情
            details = result.get('details', '')
            self.results_table.setItem(row, 4, QTableWidgetItem(details))
    
    def clear_test_results(self):
        """清空測試結果"""
        self.test_results.clear()
        if hasattr(self, 'results_table'):
            self.results_table.setRowCount(0)
    
    def export_test_results(self):
        """匯出測試結果"""
        if not self.test_results:
            return
        
        try:
            from PyQt5.QtWidgets import QFileDialog
            filename, _ = QFileDialog.getSaveFileName(
                self, "匯出整合測試結果", 
                "integration_test_results.csv", 
                "CSV Files (*.csv)")
            
            if filename:
                import csv
                with open(filename, 'w', newline='', encoding='utf-8') as f:
                    writer = csv.writer(f)
                    writer.writerow(["測試名稱", "狀態", "開始時間", "執行時間", "結果詳情"])
                    
                    for test_name, result in self.test_results.items():
                        start_time = result.get('start_time', '')
                        if start_time:
                            start_time = start_time.strftime("%Y-%m-%d %H:%M:%S")
                        
                        duration = result.get('duration', '')
                        if duration:
                            duration = f"{duration:.2f}s"
                        
                        writer.writerow([
                            test_name,
                            result.get('status', ''),
                            start_time,
                            duration,
                            result.get('details', '')
                        ])
                
                debug_log(1, f"[IntegrationTestTab] 測試結果已匯出至: {filename}")
        except Exception as e:
            error_log(f"[IntegrationTestTab] 匯出失敗: {e}")
    
    def stop_all_tests(self):
        """停止所有測試 - 獨立實現"""
        debug_log(1, "[IntegrationTestTab] 停止所有測試")
        
        # 更新所有進行中的測試狀態
        for test_name, result in self.test_results.items():
            if result.get('status') == 'RUNNING':
                result['status'] = 'STOPPED'
                result['details'] = '已手動停止'
        
        self.refresh_test_results()
        
        # 整合測試分頁獨立處理停止邏輯
        info_log("[IntegrationTestTab] 所有測試已停止")
    
    def handle_test_request(self, test_id: str, params: dict):
        """處理測試請求"""
        debug_log(1, f"[IntegrationTestTab] 處理測試請求: {test_id}")
    
    def run_full_test_suite(self):
        """執行完整測試套件"""
        debug_log(1, "[IntegrationTestTab] 執行完整測試套件")
        
        # 這個方法由主視窗呼叫，執行所有可用的整合測試
        tests_to_run = [
            "stt+nlp",  # 已重構的測試
            "frontend_full",  # 前端整合測試
        ]
        
        for test in tests_to_run:
            if "+" in test:
                self.run_integration_test(test)
            else:
                self.run_frontend_test(test.replace("frontend_", ""))
    
    def refresh_status(self):
        """刷新狀態"""
        self.refresh_frontend_status()
        self.refresh_test_results()
