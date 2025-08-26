# module_tabs/base_test_tab.py
"""
基礎測試分頁類別

提供所有模組測試分頁的共用基礎功能
包含執行緒化的模組載入操作，避免阻塞主 UI
"""

import sys
import os
import json
import datetime
from typing import Dict, Any, Optional, List
from PyQt5.QtWidgets import *
from PyQt5.QtCore import *
from PyQt5.QtGui import *

# 添加項目根目錄到 Python 路徑
script_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.abspath(os.path.join(script_dir, '..', '..', '..', '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

# 添加 debug 目錄到路徑以導入 module_manager
debug_dir = os.path.abspath(os.path.join(script_dir, '..'))
if debug_dir not in sys.path:
    sys.path.insert(0, debug_dir)

from module_manager import ModuleManager


class ModuleOperationWorker(QThread):
    """模組操作執行緒工作器"""
    operation_finished = pyqtSignal(str, dict)  # (operation_type, result)
    progress_update = pyqtSignal(str)  # progress message
    
    def __init__(self, module_manager, operation_type, module_name):
        super().__init__()
        self.module_manager = module_manager
        self.operation_type = operation_type  # 'load', 'unload', 'reload'
        self.module_name = module_name
    
    def run(self):
        """執行模組操作"""
        try:
            self.progress_update.emit(f"正在{self._get_operation_name()}模組: {self.module_name}")
            
            if self.operation_type == 'load':
                result = self.module_manager.load_module(self.module_name)
            elif self.operation_type == 'unload':
                result = self.module_manager.unload_module(self.module_name)
            elif self.operation_type == 'reload':
                result = self.module_manager.reload_module(self.module_name)
            else:
                result = {'success': False, 'error': f'未知的操作類型: {self.operation_type}'}
            
            self.operation_finished.emit(self.operation_type, result)
            
        except Exception as e:
            error_result = {'success': False, 'error': str(e)}
            self.operation_finished.emit(self.operation_type, error_result)
    
    def _get_operation_name(self):
        """取得操作名稱"""
        operation_names = {
            'load': '載入',
            'unload': '卸載', 
            'reload': '重載'
        }
        return operation_names.get(self.operation_type, '處理')


class BaseTestTab(QWidget):
    """測試分頁基礎類別"""
    
    def __init__(self, module_name: str):
        super().__init__()
        self.module_name = "ui" if module_name == "frontend" else module_name
        self.module_manager = ModuleManager()
        
        # 設定大寫的模組顯示名稱屬性（向後相容）
        self.MODULE_DISPLAY_NAME = module_name.upper()
        self.module_display_name = module_name.upper()
        
        # 執行緒相關
        self.operation_worker = None
        
        self.init_ui()
        
        # 連接執行緒信號
        self._connect_worker_signals()
    
    def init_ui(self):
        """初始化 UI"""
        main_layout = QVBoxLayout()
        self.setLayout(main_layout)
        
        # 狀態區域
        self.create_status_section(main_layout)
        
        # 控制區域
        self.create_control_section(main_layout)
        
        # 結果顯示區域
        self.create_result_section(main_layout)
        
        # 初始化狀態
        self.refresh_status()
    
    def create_status_section(self, main_layout):
        """建立狀態區域"""
        status_group = QGroupBox("模組狀態")
        status_layout = QVBoxLayout(status_group)
        
        # 第一排：狀態顯示和刷新
        status_row1 = QHBoxLayout()
        
        # 狀態標籤
        self.status_label = QLabel("檢查中...")
        self.status_label.setStyleSheet("font-weight: bold; padding: 5px;")
        status_row1.addWidget(self.status_label)
        
        # 刷新按鈕
        refresh_btn = QPushButton("🔄 刷新狀態")
        refresh_btn.clicked.connect(self.refresh_status)
        refresh_btn.setMaximumWidth(120)
        status_row1.addWidget(refresh_btn)
        
        status_layout.addLayout(status_row1)
        
        # 第二排：模組控制
        control_row = QHBoxLayout()
        
        # 載入模組按鈕
        self.load_module_btn = QPushButton("📥 載入模組")
        self.load_module_btn.clicked.connect(self.load_module_threaded)
        self.load_module_btn.setMaximumWidth(120)
        control_row.addWidget(self.load_module_btn)
        
        # 卸載模組按鈕
        self.unload_module_btn = QPushButton("📤 卸載模組")
        self.unload_module_btn.clicked.connect(self.unload_module_threaded)
        self.unload_module_btn.setMaximumWidth(120)
        control_row.addWidget(self.unload_module_btn)
        
        # 重載模組按鈕
        self.reload_module_btn = QPushButton("🔄 重載模組")
        self.reload_module_btn.clicked.connect(self.reload_module_threaded)
        self.reload_module_btn.setMaximumWidth(120)
        control_row.addWidget(self.reload_module_btn)
        
        control_row.addStretch()
        status_layout.addLayout(control_row)
        
        main_layout.addWidget(status_group)
    
    def create_control_section(self, main_layout):
        """建立控制區域 - 子類別需要重寫此方法"""
        control_group = QGroupBox(f"{self.module_name.upper()} 測試控制")
        control_layout = QVBoxLayout(control_group)
        
        info_label = QLabel("此模組的測試功能尚未實現")
        info_label.setAlignment(Qt.AlignCenter)
        info_label.setStyleSheet("color: gray; font-style: italic;")
        control_layout.addWidget(info_label)
        
        main_layout.addWidget(control_group)
    
    def create_result_section(self, main_layout):
        """建立結果顯示區域"""
        result_group = QGroupBox("測試結果")
        result_layout = QVBoxLayout(result_group)
        
        # 結果顯示區域
        self.result_area = QTextEdit()
        self.result_area.setMinimumHeight(200)
        self.result_area.setReadOnly(True)
        result_layout.addWidget(self.result_area)
        
        # 清除按鈕
        clear_btn = QPushButton("🗑️ 清除結果")
        clear_btn.clicked.connect(self.clear_results)
        clear_btn.setMaximumWidth(120)
        result_layout.addWidget(clear_btn)
        
        main_layout.addWidget(result_group)
    
    def refresh_status(self):
        """刷新模組狀態"""
        try:
            status_info = self.module_manager.get_module_status(self.module_name)
            status = status_info['status']
            loaded = status_info.get('loaded', False)
            
            # 根據設定檔狀態設置顯示
            if status == 'disabled':
                # 模組在設定檔中被禁用
                self.status_label.setText("狀態: 已禁用 (設定檔)")
                self.status_label.setStyleSheet("color: red; font-weight: bold; padding: 5px;")
                
                # 禁用所有控制按鈕
                self.load_module_btn.setEnabled(False)
                self.unload_module_btn.setEnabled(False)
                self.reload_module_btn.setEnabled(False)
                
                # 禁用其他測試功能
                self.setEnabled(False)
                
            elif status == 'enabled':
                # 模組在設定檔中啟用
                if loaded:
                    self.status_label.setText("狀態: 已載入")
                    self.status_label.setStyleSheet("color: green; font-weight: bold; padding: 5px;")
                    
                    # 設置按鈕狀態
                    self.load_module_btn.setEnabled(False)
                    self.unload_module_btn.setEnabled(True)
                    self.reload_module_btn.setEnabled(True)
                else:
                    self.status_label.setText("狀態: 未載入")
                    self.status_label.setStyleSheet("color: orange; font-weight: bold; padding: 5px;")
                    
                    # 設置按鈕狀態
                    self.load_module_btn.setEnabled(True)
                    self.unload_module_btn.setEnabled(False)
                    self.reload_module_btn.setEnabled(False)
                
                # 啟用測試功能
                self.setEnabled(True)
                
            else:
                # 未知狀態
                self.status_label.setText(f"狀態: 未知 ({status})")
                self.status_label.setStyleSheet("color: gray; font-weight: bold; padding: 5px;")
                
                # 謹慎啟用按鈕
                self.load_module_btn.setEnabled(True)
                self.unload_module_btn.setEnabled(True)
                self.reload_module_btn.setEnabled(True)
            
        except Exception as e:
            self.status_label.setText(f"狀態獲取失敗: {str(e)}")
            self.status_label.setStyleSheet("color: red; font-weight: bold; padding: 5px;")
            
            # 發生錯誤時啟用基本按鈕
            self.load_module_btn.setEnabled(True)
            self.unload_module_btn.setEnabled(False)
            self.reload_module_btn.setEnabled(False)
    
    def _connect_worker_signals(self):
        """連接執行緒工作器信號"""
        # 在初始化時不創建工作器，只在需要時創建
        pass
    
    def _set_buttons_loading_state(self, loading: bool):
        """設置按鈕的載入狀態"""
        if loading:
            # 載入中，禁用所有模組操作按鈕
            self.load_module_btn.setEnabled(False)
            self.unload_module_btn.setEnabled(False)
            self.reload_module_btn.setEnabled(False)
            self.load_module_btn.setText("⏳ 處理中...")
        else:
            # 載入完成，恢復正常狀態
            self.load_module_btn.setText("📥 載入模組")
            self.refresh_status()  # 根據實際狀態更新按鈕
    
    def _start_operation_worker(self, operation_type: str):
        """啟動操作執行緒"""
        if self.operation_worker and self.operation_worker.isRunning():
            self.add_result("上一個操作仍在進行中，請稍候", "WARNING")
            return
        
        # 創建新的工作器
        self.operation_worker = ModuleOperationWorker(
            self.module_manager, operation_type, self.module_name
        )
        
        # 連接信號
        self.operation_worker.operation_finished.connect(self._on_operation_finished)
        self.operation_worker.progress_update.connect(self._on_progress_update)
        
        # 設置載入狀態
        self._set_buttons_loading_state(True)
        
        # 啟動執行緒
        self.operation_worker.start()
    
    def _on_progress_update(self, message: str):
        """處理進度更新"""
        self.add_result(message, "INFO")
    
    def _on_operation_finished(self, operation_type: str, result: dict):
        """處理操作完成"""
        operation_names = {
            'load': '載入',
            'unload': '卸載',
            'reload': '重載'
        }
        operation_name = operation_names.get(operation_type, '操作')
        
        if result.get('success', False):
            self.add_result(f"模組{operation_name}成功: {result.get('message', '完成')}", "SUCCESS")
        else:
            self.add_result(f"模組{operation_name}失敗: {result.get('error', '未知錯誤')}", "ERROR")
        
        # 恢復按鈕狀態
        self._set_buttons_loading_state(False)
    
    def load_module_threaded(self):
        """執行緒化載入模組"""
        self._start_operation_worker('load')
    
    def unload_module_threaded(self):
        """執行緒化卸載模組"""
        self._start_operation_worker('unload')
    
    def reload_module_threaded(self):
        """執行緒化重載模組"""
        self._start_operation_worker('reload')
    
    def load_module(self):
        """載入模組 (同步版本 - 內部使用)"""
        try:
            self.add_result(f"正在載入模組: {self.module_name}", "INFO")
            result = self.module_manager.load_module(self.module_name)
            
            if result.get('success', False):
                self.add_result(f"模組載入成功: {result.get('message', '完成')}", "SUCCESS")
            else:
                self.add_result(f"模組載入失敗: {result.get('error', '未知錯誤')}", "ERROR")
                
        except Exception as e:
            self.add_result(f"載入模組時發生錯誤: {str(e)}", "ERROR")
        finally:
            # 刷新狀態
            self.refresh_status()
    
    def unload_module(self):
        """卸載模組 (同步版本 - 內部使用)"""
        try:
            self.add_result(f"正在卸載模組: {self.module_name}", "INFO")
            result = self.module_manager.unload_module(self.module_name)
            
            if result.get('success', False):
                self.add_result(f"模組卸載成功: {result.get('message', '完成')}", "SUCCESS")
            else:
                self.add_result(f"模組卸載失敗: {result.get('error', '未知錯誤')}", "ERROR")
                
        except Exception as e:
            self.add_result(f"卸載模組時發生錯誤: {str(e)}", "ERROR")
        finally:
            # 刷新狀態
            self.refresh_status()
    
    def reload_module(self):
        """重載模組 (同步版本 - 內部使用)"""
        try:
            self.add_result(f"正在重載模組: {self.module_name}", "INFO")
            result = self.module_manager.reload_module(self.module_name)
            
            if result.get('success', False):
                self.add_result(f"模組重載成功: {result.get('message', '完成')}", "SUCCESS")
            else:
                self.add_result(f"模組重載失敗: {result.get('error', '未知錯誤')}", "ERROR")
                
        except Exception as e:
            self.add_result(f"重載模組時發生錯誤: {str(e)}", "ERROR")
        finally:
            # 刷新狀態
            self.refresh_status()
    
    def add_result(self, text: str, level: str = "INFO"):
        """添加結果到顯示區域"""
        timestamp = datetime.datetime.now().strftime("%H:%M:%S")
        
        # 根據級別設置顏色
        color_map = {
            "INFO": "black",
            "SUCCESS": "green", 
            "WARNING": "orange",
            "ERROR": "red",
            "DEBUG": "blue"
        }
        color = color_map.get(level, "black")
        
        formatted_text = f'<span style="color: gray;">[{timestamp}]</span> <span style="color: {color}; font-weight: bold;">[{level}]</span> {text}'
        self.result_area.append(formatted_text)
        
        # 滾動到底部
        scrollbar = self.result_area.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())
    
    def clear_results(self):
        """清除結果顯示"""
        self.result_area.clear()
        self.add_result("結果已清除", "INFO")
    
    def run_test(self, test_name: str, params: Dict[str, Any] = None):
        """執行測試"""
        try:
            self.add_result(f"開始執行測試: {test_name}", "INFO")
            
            if params:
                self.add_result(f"參數: {json.dumps(params, ensure_ascii=False, indent=2)}", "DEBUG")
            
            # 使用 ModuleManager 執行測試
            result = self.module_manager.run_test_function(self.module_name, test_name, params or {})
            
            if result.get('success', False):
                self.add_result(f"測試完成: {result.get('message', '成功')}", "SUCCESS")
                if 'data' in result:
                    self.add_result(f"結果數據: {json.dumps(result['data'], ensure_ascii=False, indent=2)}", "INFO")
            else:
                self.add_result(f"測試失敗: {result.get('error', '未知錯誤')}", "ERROR")
                
        except Exception as e:
            self.add_result(f"執行測試時發生錯誤: {str(e)}", "ERROR")
