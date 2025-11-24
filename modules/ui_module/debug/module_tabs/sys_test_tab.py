# module_tabs/sys_test_tab.py
"""
SYS 模組測試分頁

提供工作流程測試功能
包括測試工作流程、檔案工作流程、工作流程管理
"""

import os
import sys
import json
from PyQt5.QtWidgets import *
from PyQt5.QtCore import *
from PyQt5.QtGui import *

# 添加當前目錄以導入本地模組
current_dir = os.path.dirname(os.path.abspath(__file__))
if current_dir not in sys.path:
    sys.path.insert(0, current_dir)

from base_test_tab import BaseTestTab


class SYSTestTab(BaseTestTab):
    """SYS 模組測試分頁"""
    
    def __init__(self):
        super().__init__("sysmod")
    
    def create_control_section(self, main_layout):
        """建立 SYS 控制區域"""
        control_group = QGroupBox("SYS 測試控制")
        control_layout = QVBoxLayout(control_group)
        
        # 工作流程選擇區域
        workflow_group = QGroupBox("工作流程選擇")
        workflow_layout = QVBoxLayout(workflow_group)
        
        # 工作流程類型選擇
        type_layout = QHBoxLayout()
        type_layout.addWidget(QLabel("工作流程類型:"))
        
        self.workflow_combo = QComboBox()
        self.workflow_combo.addItems([
            "echo - 簡單回顯",
            "countdown - 倒數計時",
            "data_collector - 資料收集",
            "random_fail - 隨機失敗測試",
            "tts_test - TTS 測試",
            "drop_and_read - 檔案讀取",
            "intelligent_archive - 智慧歸檔",
            "summarize_tag - 摘要標籤"
        ])
        type_layout.addWidget(self.workflow_combo)
        
        workflow_layout.addLayout(type_layout)
        control_layout.addWidget(workflow_group)
        
        # 測試工作流程測試按鈕
        test_workflow_group = QGroupBox("測試工作流程")
        test_workflow_layout = QHBoxLayout(test_workflow_group)
        
        echo_btn = QPushButton("🔄 Echo")
        echo_btn.clicked.connect(lambda: self.run_workflow_test("echo"))
        test_workflow_layout.addWidget(echo_btn)
        
        countdown_btn = QPushButton("⏰ Countdown")
        countdown_btn.clicked.connect(lambda: self.run_workflow_test("countdown"))
        test_workflow_layout.addWidget(countdown_btn)
        
        data_collector_btn = QPushButton("📊 Data Collector")
        data_collector_btn.clicked.connect(lambda: self.run_workflow_test("data_collector"))
        test_workflow_layout.addWidget(data_collector_btn)
        
        control_layout.addWidget(test_workflow_group)
        
        # 測試工作流程測試按鈕 (第二行)
        test_workflow_group2 = QGroupBox("")
        test_workflow_layout2 = QHBoxLayout(test_workflow_group2)
        test_workflow_group2.setStyleSheet("QGroupBox { border: 0px; }")
        
        random_fail_btn = QPushButton("🎲 Random Fail")
        random_fail_btn.clicked.connect(lambda: self.run_workflow_test("random_fail"))
        test_workflow_layout2.addWidget(random_fail_btn)
        
        tts_btn = QPushButton("🔊 TTS Test")
        tts_btn.clicked.connect(lambda: self.run_workflow_test("tts"))
        test_workflow_layout2.addWidget(tts_btn)
        
        control_layout.addWidget(test_workflow_group2)
        
        # 檔案工作流程測試按鈕
        file_workflow_group = QGroupBox("檔案工作流程")
        file_workflow_layout = QHBoxLayout(file_workflow_group)
        
        file_read_btn = QPushButton("📄 File Read")
        file_read_btn.clicked.connect(lambda: self.run_workflow_test("file_read"))
        file_workflow_layout.addWidget(file_read_btn)
        
        file_archive_btn = QPushButton("📁 Archive")
        file_archive_btn.clicked.connect(lambda: self.run_workflow_test("file_archive"))
        file_workflow_layout.addWidget(file_archive_btn)
        
        file_summarize_btn = QPushButton("🏷️ Summarize")
        file_summarize_btn.clicked.connect(lambda: self.run_workflow_test("file_summarize"))
        file_workflow_layout.addWidget(file_summarize_btn)
        
        control_layout.addWidget(file_workflow_group)
        
        # 工作流程管理功能
        management_group = QGroupBox("工作流程管理")
        management_layout = QVBoxLayout(management_group)
        
        # 第一行管理按鈕
        mgmt_row1 = QHBoxLayout()
        
        list_btn = QPushButton("📋 列出工作流程")
        list_btn.clicked.connect(self.list_workflows)
        mgmt_row1.addWidget(list_btn)
        
        active_btn = QPushButton("🔍 查詢活躍工作流程")
        active_btn.clicked.connect(self.list_active_workflows)
        mgmt_row1.addWidget(active_btn)
        
        management_layout.addLayout(mgmt_row1)
        
        # 第二行管理按鈕
        mgmt_row2 = QHBoxLayout()
        
        self.session_id_input = QLineEdit()
        self.session_id_input.setPlaceholderText("輸入工作流程 ID...")
        mgmt_row2.addWidget(self.session_id_input)
        
        status_btn = QPushButton("📊 查詢狀態")
        status_btn.clicked.connect(self.check_workflow_status)
        mgmt_row2.addWidget(status_btn)
        
        cancel_btn = QPushButton("❌ 取消")
        cancel_btn.clicked.connect(self.cancel_workflow)
        mgmt_row2.addWidget(cancel_btn)
        
        management_layout.addLayout(mgmt_row2)
        control_layout.addWidget(management_group)
        
        main_layout.addWidget(control_group)
    
    def run_workflow_test(self, workflow_type):
        """執行工作流程測試"""
        self.add_result(f"🔄 執行 {workflow_type} 工作流程測試...", "INFO")
        
        # 修正 background_worker 導入路徑
        import sys
        import os
        debug_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
        if debug_dir not in sys.path:
            sys.path.insert(0, debug_dir)
        
        from background_worker import get_worker_manager
        worker_manager = get_worker_manager()
        
        # 創建測試任務
        def run_test_task():
            try:
                # 根據類型調用對應的測試函數
                test_func_name = f"test_{workflow_type}"
                return self.module_manager.run_test_function(self.module_name, test_func_name, {})
            except Exception as e:
                return {"success": False, "error": str(e)}
        
        # 設置完成回調
        def on_task_complete(task_id, result):
            if not task_id.startswith(f"sys_{workflow_type}_test"):
                return
            
            if result.get('success', False):
                self.add_result(f"✅ {workflow_type} 測試完成", "SUCCESS")
                if 'data' in result:
                    data_str = json.dumps(result['data'], ensure_ascii=False, indent=2)
                    self.add_result(f"📊 結果: {data_str}", "INFO")
            else:
                self.add_result(f"❌ {workflow_type} 測試失敗: {result.get('error', '未知錯誤')}", "ERROR")
        
        # 啟動背景任務
        task_id = f"sys_{workflow_type}_test_{id(self)}"
        worker_manager.signals.finished.connect(on_task_complete)
        worker_manager.start_task(task_id, run_test_task)
        
        self.add_result("⚠️ 注意: 某些測試需要在控制台進行互動輸入", "WARNING")
    
    def list_workflows(self):
        """列出所有可用工作流程"""
        self.add_result("📋 列出可用工作流程...", "INFO")
        
        # 修正 background_worker 導入路徑
        import sys
        import os
        debug_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
        if debug_dir not in sys.path:
            sys.path.insert(0, debug_dir)
        
        from background_worker import get_worker_manager
        worker_manager = get_worker_manager()
        
        def run_list_task():
            try:
                return self.module_manager.run_test_function(self.module_name, "test_list_workflows", {})
            except Exception as e:
                return {"success": False, "error": str(e)}
        
        task_id = f"sys_list_workflows_{id(self)}"
        worker_manager.start_task(task_id, run_list_task)
    
    def list_active_workflows(self):
        """查詢當前活躍的工作流程"""
        self.add_result("🔍 查詢活躍工作流程...", "INFO")
        
        # 修正 background_worker 導入路徑
        import sys
        import os
        debug_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
        if debug_dir not in sys.path:
            sys.path.insert(0, debug_dir)
        
        from background_worker import get_worker_manager
        worker_manager = get_worker_manager()
        
        def run_active_task():
            try:
                return self.module_manager.run_test_function(self.module_name, "test_active_workflows", {})
            except Exception as e:
                return {"success": False, "error": str(e)}
        
        def on_task_complete(task_id, result):
            if task_id != f"sys_active_workflows_{id(self)}":
                return
            
            if result.get('success', False):
                sessions = result.get('sessions', [])
                if sessions:
                    self.add_result(f"✅ 找到 {len(sessions)} 個活躍工作流程", "SUCCESS")
                    for session in sessions:
                        self.add_result(
                            f"  • {session.get('session_id')}: {session.get('workflow_type')} [{session.get('status')}]",
                            "INFO"
                        )
                else:
                    self.add_result("📭 目前沒有活躍的工作流程", "INFO")
            else:
                self.add_result(f"❌ 查詢失敗: {result.get('error', '未知錯誤')}", "ERROR")
        
        task_id = f"sys_active_workflows_{id(self)}"
        worker_manager.signals.finished.connect(on_task_complete)
        worker_manager.start_task(task_id, run_active_task)
    
    def check_workflow_status(self):
        """查詢工作流程狀態"""
        session_id = self.session_id_input.text().strip()
        if not session_id:
            self.add_result("❌ 請輸入工作流程 ID", "ERROR")
            return
        
        self.add_result(f"🔍 查詢工作流程狀態: {session_id}", "INFO")
        
        # 修正 background_worker 導入路徑
        import sys
        import os
        debug_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
        if debug_dir not in sys.path:
            sys.path.insert(0, debug_dir)
        
        from background_worker import get_worker_manager
        worker_manager = get_worker_manager()
        
        def run_status_task():
            try:
                return self.module_manager.run_test_function(
                    self.module_name,
                    "test_workflow_status",
                    {"session_id": session_id}
                )
            except Exception as e:
                return {"success": False, "error": str(e)}
        
        def on_task_complete(task_id, result):
            if task_id != f"sys_status_{session_id}_{id(self)}":
                return
            
            if result.get('success', False):
                info = result.get('info', {})
                self.add_result(f"✅ 工作流程資訊:", "SUCCESS")
                self.add_result(f"  類型: {info.get('workflow_type')}", "INFO")
                self.add_result(f"  狀態: {info.get('status')}", "INFO")
                self.add_result(f"  當前步驟: {info.get('current_step')}", "INFO")
            else:
                self.add_result(f"❌ 查詢失敗: {result.get('error', '未知錯誤')}", "ERROR")
        
        task_id = f"sys_status_{session_id}_{id(self)}"
        worker_manager.signals.finished.connect(on_task_complete)
        worker_manager.start_task(task_id, run_status_task)
    
    def cancel_workflow(self):
        """取消工作流程"""
        session_id = self.session_id_input.text().strip()
        if not session_id:
            self.add_result("❌ 請輸入工作流程 ID", "ERROR")
            return
        
        self.add_result(f"❌ 取消工作流程: {session_id}", "INFO")
        
        # 修正 background_worker 導入路徑
        import sys
        import os
        debug_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
        if debug_dir not in sys.path:
            sys.path.insert(0, debug_dir)
        
        from background_worker import get_worker_manager
        worker_manager = get_worker_manager()
        
        def run_cancel_task():
            try:
                return self.module_manager.run_test_function(
                    self.module_name,
                    "test_cancel_workflow",
                    {"session_id": session_id}
                )
            except Exception as e:
                return {"success": False, "error": str(e)}
        
        def on_task_complete(task_id, result):
            if task_id != f"sys_cancel_{session_id}_{id(self)}":
                return
            
            if result.get('success', False):
                self.add_result(f"✅ 工作流程已取消", "SUCCESS")
            else:
                self.add_result(f"❌ 取消失敗: {result.get('error', '未知錯誤')}", "ERROR")
        
        task_id = f"sys_cancel_{session_id}_{id(self)}"
        worker_manager.signals.finished.connect(on_task_complete)
        worker_manager.start_task(task_id, run_cancel_task)
