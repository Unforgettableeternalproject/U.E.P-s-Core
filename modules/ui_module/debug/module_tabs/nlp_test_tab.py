# module_tabs/nlp_test_tab.py
"""
NLP 模組測試分頁

提供自然語言處理模組的完整測試功能
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


class NLPTestTab(BaseTestTab):
    """NLP 模組測試分頁"""
    
    def __init__(self):
        super().__init__("nlp")
    
    def create_control_section(self, main_layout):
        """建立 NLP 控制區域"""
        control_group = QGroupBox("NLP 測試控制")
        control_layout = QVBoxLayout(control_group)
        
        # 文本輸入區域
        text_group = QGroupBox("文本輸入")
        text_layout = QVBoxLayout(text_group)
        
        self.text_input = QTextEdit()
        self.text_input.setMaximumHeight(100)
        self.text_input.setPlaceholderText("請輸入要處理的文本...")
        text_layout.addWidget(self.text_input)
        
        # 選項區域
        options_layout = QHBoxLayout()
        
        self.identity_checkbox = QCheckBox("啟用語者身份處理")
        self.identity_checkbox.setChecked(True)
        options_layout.addWidget(self.identity_checkbox)
        
        self.segmentation_checkbox = QCheckBox("啟用意圖分段")
        self.segmentation_checkbox.setChecked(True)
        options_layout.addWidget(self.segmentation_checkbox)
        
        text_layout.addLayout(options_layout)
        control_layout.addWidget(text_group)
        
        # 基本測試功能
        test_group = QGroupBox("基本測試")
        test_layout = QVBoxLayout(test_group)
        
        # 基本測試按鈕
        basic_test_btn = QPushButton("🧠 基本 NLP 測試")
        basic_test_btn.clicked.connect(self.run_basic_test)
        test_layout.addWidget(basic_test_btn)
        
        # 進階測試按鈕組
        advanced_layout = QHBoxLayout()
        
        state_queue_btn = QPushButton("📋 狀態佇列測試")
        state_queue_btn.clicked.connect(self.run_state_queue_test)
        advanced_layout.addWidget(state_queue_btn)
        
        multi_intent_btn = QPushButton("🔀 多意圖測試")
        multi_intent_btn.clicked.connect(self.run_multi_intent_test)
        advanced_layout.addWidget(multi_intent_btn)
        
        identity_btn = QPushButton("👤 語者身份測試")
        identity_btn.clicked.connect(self.run_identity_test)
        advanced_layout.addWidget(identity_btn)
        
        test_layout.addLayout(advanced_layout)
        control_layout.addWidget(test_group)
        
        # 上下文管理
        context_group = QGroupBox("上下文管理")
        context_layout = QHBoxLayout(context_group)
        
        analyze_context_btn = QPushButton("📊 分析上下文佇列")
        analyze_context_btn.clicked.connect(self.analyze_context_queue)
        context_layout.addWidget(analyze_context_btn)
        
        clear_contexts_btn = QPushButton("🗑️ 清除所有上下文")
        clear_contexts_btn.clicked.connect(self.clear_contexts)
        context_layout.addWidget(clear_contexts_btn)
        
        control_layout.addWidget(context_group)
        
        main_layout.addWidget(control_group)
    
    def get_input_text(self):
        """獲取輸入文本"""
        text = self.text_input.toPlainText().strip()
        if not text:
            self.add_result("❌ 請先輸入文本", "ERROR")
            return None
        return text
    
    def run_basic_test(self):
        """執行基本 NLP 測試"""
        self.add_result("🧠 執行 NLP 基本測試...", "INFO")
        
        text = self.get_input_text()
        if not text:
            return
            
        # 修正 background_worker 導入路徑
        import sys
        import os
        debug_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
        if debug_dir not in sys.path:
            sys.path.insert(0, debug_dir)
        
        from background_worker import get_worker_manager
        worker_manager = get_worker_manager()
        
        # 獲取參數
        params = {
            "text": text,
            "enable_identity": self.identity_checkbox.isChecked(),
            "enable_segmentation": self.segmentation_checkbox.isChecked()
        }
        
        # 創建一個任務以在背景執行
        def run_nlp_test_task():
            try:
                return self.module_manager.run_test_function(self.module_name, "basic_test", params)
            except Exception as e:
                return {"success": False, "error": str(e)}
        
        # 設置任務完成後的回調
        def on_task_complete(task_id, result):
            if task_id != "nlp_basic_test_" + str(id(self)):
                return  # 不是我們的任務
                
            if result.get('success', False):
                self.add_result(f"✅ NLP 測試完成", "SUCCESS")
                if 'data' in result:
                    self.add_result(f"結果數據: {json.dumps(result['data'], ensure_ascii=False, indent=2)}", "INFO")
            else:
                self.add_result(f"❌ NLP 測試失敗: {result.get('error', '未知錯誤')}", "ERROR")
        
        # 啟動背景任務
        task_id = "nlp_basic_test_" + str(id(self))
        worker_manager.signals.finished.connect(on_task_complete)
        worker_manager.start_task(task_id, run_nlp_test_task)
        
        self.add_result("🔄 NLP 分析正在背景執行，請稍候...", "INFO")
    
    def run_state_queue_test(self):
        """執行狀態佇列整合測試"""
        text = self.get_input_text()
        if not text:
            return
            
        self.add_result("🔄 執行狀態佇列整合測試...", "INFO")
        
        # 修正 background_worker 導入路徑
        import sys
        import os
        debug_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
        if debug_dir not in sys.path:
            sys.path.insert(0, debug_dir)
        
        from background_worker import get_worker_manager
        worker_manager = get_worker_manager()
        
        # 獲取參數
        params = {"text": text}
        
        # 創建一個任務以在背景執行
        def run_state_queue_task():
            try:
                return self.module_manager.run_test_function(self.module_name, "state_queue_test", params)
            except Exception as e:
                return {"success": False, "error": str(e)}
        
        task_id = "nlp_state_queue_test_" + str(id(self))
        worker_manager.start_task(task_id, run_state_queue_task)
        self.add_result("🔄 狀態佇列測試正在背景執行，請稍候...", "INFO")
    
    def run_multi_intent_test(self):
        """執行多意圖測試"""
        text = self.get_input_text()
        if not text:
            return
            
        self.add_result("🔀 執行多意圖測試...", "INFO")
        
        # 修正 background_worker 導入路徑
        import sys
        import os
        debug_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
        if debug_dir not in sys.path:
            sys.path.insert(0, debug_dir)
        
        from background_worker import get_worker_manager
        worker_manager = get_worker_manager()
        
        # 獲取參數
        params = {"text": text}
        
        # 創建一個任務以在背景執行
        def run_multi_intent_task():
            try:
                return self.module_manager.run_test_function(self.module_name, "multi_intent_test", params)
            except Exception as e:
                return {"success": False, "error": str(e)}
        
        task_id = "nlp_multi_intent_test_" + str(id(self))
        worker_manager.start_task(task_id, run_multi_intent_task)
        self.add_result("🔄 多意圖測試正在背景執行，請稍候...", "INFO")
    
    def run_identity_test(self):
        """執行語者身份測試"""
        self.add_result("👤 執行語者身份測試...", "INFO")
        
        # 修正 background_worker 導入路徑
        import sys
        import os
        debug_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
        if debug_dir not in sys.path:
            sys.path.insert(0, debug_dir)
        
        from background_worker import get_worker_manager
        worker_manager = get_worker_manager()
        
        # 獲取參數 - 使用固定的測試用戶ID
        params = {"speaker_id": "test_user"}
        
        # 創建一個任務以在背景執行
        def run_identity_test_task():
            try:
                return self.module_manager.run_test_function(self.module_name, "identity_test", params)
            except Exception as e:
                return {"success": False, "error": str(e)}
        
        task_id = "nlp_identity_test_" + str(id(self))
        worker_manager.start_task(task_id, run_identity_test_task)
        self.add_result("🔄 身份測試正在背景執行，請稍候...", "INFO")
    
    def analyze_context_queue(self):
        """分析上下文佇列"""
        self.add_result("📊 分析上下文佇列...", "INFO")
        self.run_test("analyze_context")
    
    def clear_contexts(self):
        """清空所有上下文"""
        self.add_result("🗑️ 清空所有上下文...", "INFO")
        self.run_test("clear_contexts")
