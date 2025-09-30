# module_tabs/llm_test_tab.py
"""
LLM 模組測試分頁

提供大型語言模型模組的完整測試功能
包括對話、指令處理、快取功能、學習引擎和狀態監控
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


class LLMTestTab(BaseTestTab):
    """LLM 模組測試分頁"""
    
    def __init__(self):
        super().__init__("llm")
    
    def create_control_section(self, main_layout):
        """建立 LLM 控制區域"""
        control_group = QGroupBox("LLM 測試控制")
        control_layout = QVBoxLayout(control_group)
        
        # 輸入區域
        input_group = QGroupBox("對話輸入")
        input_layout = QVBoxLayout(input_group)
        
        # 文本輸入
        self.text_input = QTextEdit()
        self.text_input.setMaximumHeight(100)
        self.text_input.setPlaceholderText("請輸入要測試的對話內容或指令...")
        input_layout.addWidget(self.text_input)
        
        # 操作模式選擇
        mode_layout = QHBoxLayout()
        mode_layout.addWidget(QLabel("測試模式:"))
        
        self.mode_combo = QComboBox()
        self.mode_combo.addItems(["chat", "work"])
        self.mode_combo.setCurrentText("chat")
        mode_layout.addWidget(self.mode_combo)
        
        mode_layout.addStretch()
        input_layout.addLayout(mode_layout)
        control_layout.addWidget(input_group)
        
        # 基本測試功能
        basic_test_group = QGroupBox("基本對話測試")
        basic_test_layout = QVBoxLayout(basic_test_group)
        
        # CHAT 和 WORK 測試按鈕
        mode_test_layout = QHBoxLayout()
        
        chat_test_btn = QPushButton("💬 CHAT 對話測試")
        chat_test_btn.clicked.connect(self.run_chat_test)
        mode_test_layout.addWidget(chat_test_btn)
        
        work_test_btn = QPushButton("⚙️ WORK 指令測試")
        work_test_btn.clicked.connect(self.run_work_test)
        mode_test_layout.addWidget(work_test_btn)
        
        basic_test_layout.addLayout(mode_test_layout)
        control_layout.addWidget(basic_test_group)
        
        # 進階測試功能
        advanced_test_group = QGroupBox("進階功能測試")
        advanced_test_layout = QVBoxLayout(advanced_test_group)
        
        # 第一行進階測試
        advanced_row1 = QHBoxLayout()
        
        cache_test_btn = QPushButton("🧠 上下文快取測試")
        cache_test_btn.clicked.connect(self.run_cache_test)
        advanced_row1.addWidget(cache_test_btn)
        
        learning_test_btn = QPushButton("📚 學習引擎測試")
        learning_test_btn.clicked.connect(self.run_learning_test)
        advanced_row1.addWidget(learning_test_btn)
        
        advanced_test_layout.addLayout(advanced_row1)
        
        # 第二行進階測試
        advanced_row2 = QHBoxLayout()
        
        status_test_btn = QPushButton("📊 狀態監控測試")
        status_test_btn.clicked.connect(self.run_status_monitoring_test)
        advanced_row2.addWidget(status_test_btn)
        
        # 空白按鈕位置，保持版面平衡
        spacer_btn = QPushButton("")
        spacer_btn.setEnabled(False)
        spacer_btn.setVisible(False)
        advanced_row2.addWidget(spacer_btn)
        
        advanced_test_layout.addLayout(advanced_row2)
        control_layout.addWidget(advanced_test_group)
        
        # 系統狀態監控區域
        status_group = QGroupBox("系統狀態監控")
        status_layout = QVBoxLayout(status_group)
        
        # 狀態資訊顯示
        self.status_display = QTextEdit()
        self.status_display.setMaximumHeight(120)
        self.status_display.setReadOnly(True)
        self.status_display.setPlaceholderText("系統狀態將顯示在這裡...")
        status_layout.addWidget(self.status_display)
        
        # 狀態操作按鈕
        status_btn_layout = QHBoxLayout()
        
        refresh_status_btn = QPushButton("🔄 更新狀態")
        refresh_status_btn.clicked.connect(self.refresh_system_status)
        status_btn_layout.addWidget(refresh_status_btn)
        
        reset_status_btn = QPushButton("↺ 重設狀態")
        reset_status_btn.clicked.connect(self.reset_system_status)
        status_btn_layout.addWidget(reset_status_btn)
        
        status_layout.addLayout(status_btn_layout)
        control_layout.addWidget(status_group)
        
        main_layout.addWidget(control_group)
    
    def get_input_text(self):
        """獲取輸入文本"""
        text = self.text_input.toPlainText().strip()
        if not text:
            self.add_result("❌ 請先輸入測試內容", "ERROR")
            return None
        return text
    
    def get_mode(self):
        """獲取測試模式"""
        return self.mode_combo.currentText()
    
    def run_chat_test(self):
        """執行 CHAT 對話測試"""
        text = self.get_input_text()
        if not text:
            return
        
        self.add_result(f"💬 執行 CHAT 對話測試: '{text}'", "INFO")
        
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
        def run_chat_test_task():
            try:
                return self.module_manager.run_test_function(self.module_name, "chat", params)
            except Exception as e:
                return {"success": False, "error": str(e)}
        
        # 設置任務完成後的回調
        def on_task_complete(task_id, result):
            if task_id != "llm_chat_test_" + str(id(self)):
                return  # 不是我們的任務
                
            if result.get('success', False):
                response = result.get('response', '[無回應]')
                processing_time = result.get('processing_time', 0)
                self.add_result(f"✅ CHAT 測試完成", "SUCCESS")
                self.add_result(f"🧠 AI 回應: {response}", "INFO")
                self.add_result(f"⏱️ 處理時間: {processing_time:.2f}s", "INFO")
                
                # 刷新狀態顯示
                self.refresh_system_status()
            else:
                self.add_result(f"❌ CHAT 測試失敗: {result.get('error', '未知錯誤')}", "ERROR")
        
        # 啟動背景任務
        task_id = "llm_chat_test_" + str(id(self))
        worker_manager.signals.finished.connect(on_task_complete)
        worker_manager.start_task(task_id, run_chat_test_task)
        
        self.add_result("🔄 CHAT 測試正在背景執行，請稍候...", "INFO")
    
    def run_work_test(self):
        """執行 WORK 指令測試"""
        text = self.get_input_text()
        if not text:
            return
        
        self.add_result(f"⚙️ 執行 WORK 指令測試: '{text}'", "INFO")
        
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
        def run_command_test_task():
            try:
                return self.module_manager.run_test_function(self.module_name, "command", params)
            except Exception as e:
                return {"success": False, "error": str(e)}
        
        task_id = "llm_command_test_" + str(id(self))
        worker_manager.start_task(task_id, run_command_test_task)
        self.add_result("🔄 WORK 測試正在背景執行，請稍候...", "INFO")
    
    def run_cache_test(self):
        """執行上下文快取測試"""
        self.add_result("🧠 執行上下文快取功能測試...", "INFO")
        
        # 修正 background_worker 導入路徑
        import sys
        import os
        debug_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
        if debug_dir not in sys.path:
            sys.path.insert(0, debug_dir)
        
        from background_worker import get_worker_manager
        worker_manager = get_worker_manager()
        
        # 創建一個任務以在背景執行
        def run_cache_test_task():
            try:
                return self.module_manager.run_test_function(self.module_name, "cache_functionality", {})
            except Exception as e:
                return {"success": False, "error": str(e)}
        
        task_id = "llm_cache_test_" + str(id(self))
        worker_manager.start_task(task_id, run_cache_test_task)
        self.add_result("🔄 快取測試正在背景執行，請稍候...", "INFO")
    
    def run_learning_test(self):
        """執行學習引擎測試"""
        self.add_result("📚 執行學習引擎測試...", "INFO")
        
        # 修正 background_worker 導入路徑
        import sys
        import os
        debug_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
        if debug_dir not in sys.path:
            sys.path.insert(0, debug_dir)
        
        from background_worker import get_worker_manager
        worker_manager = get_worker_manager()
        
        # 創建一個任務以在背景執行
        def run_learning_test_task():
            try:
                return self.module_manager.run_test_function(self.module_name, "learning_engine", {})
            except Exception as e:
                return {"success": False, "error": str(e)}
        
        task_id = "llm_learning_test_" + str(id(self))
        worker_manager.start_task(task_id, run_learning_test_task)
        self.add_result("🔄 學習引擎測試正在背景執行，請稍候...", "INFO")
    
    def run_status_monitoring_test(self):
        """執行系統狀態監控測試"""
        self.add_result("📊 執行系統狀態監控測試...", "INFO")
        self.add_result("⚠️ 注意: 狀態監控測試為互動式，將在控制台中進行", "WARNING")
        
        # 修正 background_worker 導入路徑
        import sys
        import os
        debug_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
        if debug_dir not in sys.path:
            sys.path.insert(0, debug_dir)
        
        from background_worker import get_worker_manager
        worker_manager = get_worker_manager()
        
        # 創建一個任務以在背景執行
        def run_status_test_task():
            try:
                return self.module_manager.run_test_function(self.module_name, "system_status_monitoring", {})
            except Exception as e:
                return {"success": False, "error": str(e)}
        
        task_id = "llm_status_test_" + str(id(self))
        worker_manager.start_task(task_id, run_status_test_task)
        self.add_result("🔄 狀態監控測試正在背景執行，請查看控制台進行互動...", "INFO")
    
    def refresh_system_status(self):
        """更新系統狀態顯示"""
        try:
            # 嘗試導入並獲取狀態管理器
            from core.status_manager import StatusManager
            status_manager = StatusManager()
            status_dict = status_manager.get_status_dict()
            
            # 格式化狀態顯示
            status_text = "📊 當前系統狀態:\n"
            status_text += "=" * 30 + "\n"
            
            for key, value in status_dict.items():
                # 根據數值範圍添加適當的表情符號
                if isinstance(value, (int, float)):
                    if key.lower() == 'mood':
                        emoji = "😊" if value > 0.5 else "😐" if value > -0.5 else "😔"
                    elif key.lower() == 'pride':
                        emoji = "🦚" if value > 0.7 else "💪" if value > 0.3 else "😅"
                    elif key.lower() == 'helpfulness':
                        emoji = "🤝" if value > 0.7 else "👍" if value > 0.3 else "🤷"
                    elif key.lower() == 'boredom':
                        emoji = "😴" if value > 0.7 else "😑" if value > 0.3 else "😮"
                    else:
                        emoji = "📈"
                    
                    status_text += f"{emoji} {key}: {value:.3f}\n"
                else:
                    status_text += f"📋 {key}: {value}\n"
            
            self.status_display.setText(status_text)
            self.add_result("✅ 系統狀態已更新", "SUCCESS")
            
        except ImportError:
            self.status_display.setText("❌ 無法載入 StatusManager\n請確認模組是否正確安裝")
            self.add_result("❌ 無法載入狀態管理器", "ERROR")
        except Exception as e:
            self.status_display.setText(f"❌ 狀態獲取失敗: {str(e)}")
            self.add_result(f"❌ 狀態更新失敗: {str(e)}", "ERROR")
    
    def reset_system_status(self):
        """重設系統狀態到預設值"""
        try:
            from core.status_manager import StatusManager
            status_manager = StatusManager()
            status_manager.reset_status()
            
            self.add_result("↺ 系統狀態已重設為預設值", "SUCCESS")
            self.refresh_system_status()  # 立即更新顯示
            
        except ImportError:
            self.add_result("❌ 無法載入狀態管理器進行重設", "ERROR")
        except Exception as e:
            self.add_result(f"❌ 狀態重設失敗: {str(e)}", "ERROR")