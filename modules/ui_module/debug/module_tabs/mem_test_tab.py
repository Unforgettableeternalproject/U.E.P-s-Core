# module_tabs/mem_test_tab.py
"""
MEM 模組測試分頁

提供記憶模組的完整測試功能，包括記憶存儲、查詢、統計等
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


class MEMTestTab(BaseTestTab):
    """MEM 模組測試分頁"""
    
    def __init__(self):
        super().__init__("mem")
    
    def create_control_section(self, main_layout):
        """建立 MEM 控制區域"""
        control_group = QGroupBox("MEM 測試控制")
        control_layout = QVBoxLayout(control_group)
        
        # 記憶輸入區域
        memory_group = QGroupBox("記憶輸入")
        memory_layout = QVBoxLayout(memory_group)
        
        form_layout = QFormLayout()
        
        # 身份ID輸入
        self.identity_input = QLineEdit()
        self.identity_input.setText("test_user")
        form_layout.addRow("語者ID:", self.identity_input)
        
        # 記憶內容輸入
        self.content_input = QTextEdit()
        self.content_input.setMaximumHeight(100)
        self.content_input.setPlaceholderText("請輸入記憶內容...")
        form_layout.addRow("記憶內容:", self.content_input)
        
        # 查詢關鍵詞
        self.query_input = QLineEdit()
        self.query_input.setPlaceholderText("請輸入查詢關鍵詞...")
        form_layout.addRow("查詢關鍵詞:", self.query_input)
        
        memory_layout.addLayout(form_layout)
        
        # 記憶類型選擇
        memory_type_group = QHBoxLayout()
        self.memory_type_combo = QComboBox()
        self.memory_type_combo.addItems(["long_term", "snapshot", "profile", "preference"])
        memory_type_group.addWidget(QLabel("記憶類型:"))
        memory_type_group.addWidget(self.memory_type_combo)
        memory_type_group.addStretch()
        
        memory_layout.addLayout(memory_type_group)
        control_layout.addWidget(memory_group)
        
        # 基本測試功能
        test_group = QGroupBox("基本測試功能")
        test_layout = QVBoxLayout(test_group)
        
        # 記憶存儲與查詢
        basic_layout = QHBoxLayout()
        
        store_memory_btn = QPushButton("💾 存儲記憶")
        store_memory_btn.clicked.connect(self.run_store_memory)
        basic_layout.addWidget(store_memory_btn)
        
        query_memory_btn = QPushButton("🔍 查詢記憶")
        query_memory_btn.clicked.connect(self.run_query_memory)
        basic_layout.addWidget(query_memory_btn)
        
        create_snapshot_btn = QPushButton("📸 建立快照")
        create_snapshot_btn.clicked.connect(self.run_create_snapshot)
        basic_layout.addWidget(create_snapshot_btn)
        
        test_layout.addLayout(basic_layout)
        
        # 進階測試功能
        advanced_layout = QHBoxLayout()
        
        write_query_btn = QPushButton("🔄 寫入後查詢測試")
        write_query_btn.clicked.connect(self.run_write_then_query)
        advanced_layout.addWidget(write_query_btn)
        
        snapshot_query_btn = QPushButton("💬 對話快照查詢")
        snapshot_query_btn.clicked.connect(self.run_conversation_snapshot)
        advanced_layout.addWidget(snapshot_query_btn)
        
        memory_stats_btn = QPushButton("📊 記憶統計")
        memory_stats_btn.clicked.connect(self.run_identity_manager_stats)
        advanced_layout.addWidget(memory_stats_btn)
        
        test_layout.addLayout(advanced_layout)
        control_layout.addWidget(test_group)
        
        # 記憶庫管理
        mem_admin_group = QGroupBox("記憶庫管理")
        mem_admin_layout = QHBoxLayout(mem_admin_group)
        
        list_memories_btn = QPushButton("📋 列出記憶庫")
        list_memories_btn.clicked.connect(self.run_memory_access_control)
        mem_admin_layout.addWidget(list_memories_btn)
        
        clear_memories_btn = QPushButton("🗑️ 清除測試記憶")
        clear_memories_btn.clicked.connect(self.clear_test_memories)
        mem_admin_layout.addWidget(clear_memories_btn)
        
        control_layout.addWidget(mem_admin_group)
        
        main_layout.addWidget(control_group)
    
    def get_identity(self):
        """獲取身份ID"""
        return self.identity_input.text().strip() or "test_user"
    
    def get_content(self):
        """獲取記憶內容"""
        content = self.content_input.toPlainText().strip()
        if not content:
            self.add_result("❌ 請先輸入記憶內容", "ERROR")
            return None
        return content
    
    def get_query(self):
        """獲取查詢關鍵詞"""
        query = self.query_input.text().strip()
        if not query:
            self.add_result("❌ 請先輸入查詢關鍵詞", "ERROR")
            return None
        return query
    
    def get_memory_type(self):
        """獲取記憶類型"""
        return self.memory_type_combo.currentText()
    
    def run_store_memory(self):
        """執行記憶存儲測試"""
        self.add_result("💾 執行記憶存儲測試...", "INFO")
        
        content = self.get_content()
        if not content:
            return
        
        # 獲取參數
        params = {
            "identity": self.get_identity(),
            "content": content,
            "memory_type": self.get_memory_type()
        }
        
        self.run_background_task("store_memory", params)
    
    def run_query_memory(self):
        """執行記憶查詢測試"""
        self.add_result("🔍 執行記憶查詢測試...", "INFO")
        
        query = self.get_query()
        if not query:
            return
        
        # 獲取參數
        params = {
            "identity": self.get_identity(),
            "query_text": query
        }
        
        self.run_background_task("memory_query", params)
    
    def run_create_snapshot(self):
        """執行建立快照測試"""
        self.add_result("📸 執行建立快照測試...", "INFO")
        
        content = self.get_content()
        if not content:
            return
        
        # 獲取參數
        params = {
            "identity": self.get_identity(),
            "conversation_text": content
        }
        
        self.run_background_task("create_snapshot", params)
    
    def run_write_then_query(self):
        """執行寫入後查詢測試"""
        self.add_result("🔄 執行寫入後查詢測試...", "INFO")
        
        # 獲取參數
        params = {
            "identity": self.get_identity()
        }
        
        self.run_background_task("write_then_query", params)
    
    def run_conversation_snapshot(self):
        """執行對話快照查詢測試"""
        self.add_result("💬 執行對話快照查詢測試...", "INFO")
        
        content = self.get_content()
        if not content:
            return
        
        # 獲取參數
        params = {
            "identity": self.get_identity(),
            "conversation": content
        }
        
        self.run_background_task("conversation_snapshot", params)
    
    def run_memory_access_control(self):
        """執行記憶庫列表測試"""
        self.add_result("📋 執行記憶庫列表測試...", "INFO")
        
        # 獲取參數
        params = {
            "identity": self.get_identity()
        }
        
        self.run_background_task("memory_access_control", params)
    
    def run_identity_manager_stats(self):
        """執行記憶統計測試"""
        self.add_result("📊 執行記憶統計測試...", "INFO")
        
        # 獲取參數
        params = {
            "identity": self.get_identity()
        }
        
        self.run_background_task("identity_manager_stats", params)
    
    def clear_test_memories(self):
        """清除測試記憶"""
        self.add_result("🗑️ 嘗試清除測試記憶...", "INFO")
        self.add_result("⚠️ 功能未實現，請手動刪除記憶文件", "WARNING")
        
        # 清除功能可以在未來實現
        # TODO: 實現清除測試記憶的功能
    
    def run_background_task(self, test_function, params):
        """執行背景任務"""
        try:
            # 修正 background_worker 導入路徑
            import sys
            import os
            debug_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
            if debug_dir not in sys.path:
                sys.path.insert(0, debug_dir)
            
            from background_worker import get_worker_manager
            worker_manager = get_worker_manager()
            
            # 創建一個任務以在背景執行
            def run_mem_task():
                try:
                    return self.module_manager.run_test_function(self.module_name, test_function, params)
                except Exception as e:
                    return {"success": False, "error": str(e)}
            
            # 設置任務完成後的回調
            def on_task_complete(task_id, result):
                if task_id != f"mem_{test_function}_{id(self)}":
                    return  # 不是我們的任務
                    
                if isinstance(result, dict) and result.get('success', False):
                    self.add_result(f"✅ MEM {test_function} 測試完成", "SUCCESS")
                    
                    # 格式化結果顯示
                    data = {k: v for k, v in result.items() if k != 'success'}
                    if data:
                        self.add_result(f"結果數據: {json.dumps(data, ensure_ascii=False, indent=2)}", "INFO")
                else:
                    error_msg = result.get('error', '未知錯誤') if isinstance(result, dict) else str(result)
                    self.add_result(f"❌ MEM {test_function} 測試失敗: {error_msg}", "ERROR")
            
            # 啟動背景任務
            task_id = f"mem_{test_function}_{id(self)}"
            worker_manager.signals.finished.connect(on_task_complete)
            worker_manager.start_task(task_id, run_mem_task)
            
            self.add_result(f"🔄 MEM {test_function} 測試正在背景執行，請稍候...", "INFO")
            
        except Exception as e:
            self.add_result(f"❌ 無法啟動背景任務: {str(e)}", "ERROR")