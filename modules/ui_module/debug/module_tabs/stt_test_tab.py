# module_tabs/stt_test_tab.py
"""
STT 模組測試分頁

提供語音轉文字模組的完整測試功能
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


class STTTestTab(BaseTestTab):
    """STT 模組測試分頁"""
    
    def __init__(self):
        super().__init__("stt")
    
    def create_control_section(self, main_layout):
        """建立 STT 控制區域"""
        control_group = QGroupBox("STT 測試控制")
        control_layout = QVBoxLayout(control_group)
        
        # 基本測試區域
        basic_group = QGroupBox("基本測試")
        basic_layout = QVBoxLayout(basic_group)
        
        # 單次測試
        single_test_layout = QHBoxLayout()
        single_test_btn = QPushButton("🎤 單次語音測試")
        single_test_btn.clicked.connect(self.run_single_test)
        single_test_layout.addWidget(single_test_btn)
        
        # 語言選擇
        self.language_combo = QComboBox()
        self.language_combo.addItems(["en-US", "zh-TW", "zh-CN", "ja-JP"])
        single_test_layout.addWidget(QLabel("語言:"))
        single_test_layout.addWidget(self.language_combo)
        
        # 說話人識別
        self.speaker_id_checkbox = QCheckBox("啟用說話人識別")
        self.speaker_id_checkbox.setChecked(True)
        single_test_layout.addWidget(self.speaker_id_checkbox)
        
        basic_layout.addLayout(single_test_layout)
        
        # 持續監聽測試
        continuous_layout = QHBoxLayout()
        continuous_test_btn = QPushButton("🔄 持續監聽測試")
        continuous_test_btn.clicked.connect(self.run_continuous_test)
        continuous_layout.addWidget(continuous_test_btn)
        
        # 持續時間
        self.duration_spinbox = QSpinBox()
        self.duration_spinbox.setRange(5, 300)
        self.duration_spinbox.setValue(30)
        self.duration_spinbox.setSuffix(" 秒")
        continuous_layout.addWidget(QLabel("持續時間:"))
        continuous_layout.addWidget(self.duration_spinbox)
        
        basic_layout.addLayout(continuous_layout)
        
        # 統計信息
        stats_btn = QPushButton("📊 獲取統計信息")
        stats_btn.clicked.connect(self.get_stats)
        basic_layout.addWidget(stats_btn)
        
        control_layout.addWidget(basic_group)
        
        # 說話人管理區域
        speaker_group = QGroupBox("說話人管理")
        speaker_layout = QVBoxLayout(speaker_group)
        
        # 說話人操作按鈕
        speaker_buttons_layout = QHBoxLayout()
        
        list_speakers_btn = QPushButton("📋 列出說話人")
        list_speakers_btn.clicked.connect(self.list_speakers)
        speaker_buttons_layout.addWidget(list_speakers_btn)
        
        speaker_info_btn = QPushButton("ℹ️ 說話人資訊")
        speaker_info_btn.clicked.connect(self.get_speaker_info)
        speaker_buttons_layout.addWidget(speaker_info_btn)
        
        clear_speakers_btn = QPushButton("🗑️ 清除所有說話人")
        clear_speakers_btn.clicked.connect(self.clear_all_speakers)
        speaker_buttons_layout.addWidget(clear_speakers_btn)
        
        speaker_layout.addLayout(speaker_buttons_layout)
        
        # 說話人重命名
        rename_layout = QHBoxLayout()
        self.old_speaker_input = QLineEdit()
        self.old_speaker_input.setPlaceholderText("舊說話人ID")
        self.new_speaker_input = QLineEdit()
        self.new_speaker_input.setPlaceholderText("新說話人ID")
        rename_btn = QPushButton("重命名說話人")
        rename_btn.clicked.connect(self.rename_speaker)
        
        rename_layout.addWidget(self.old_speaker_input)
        rename_layout.addWidget(self.new_speaker_input)
        rename_layout.addWidget(rename_btn)
        
        speaker_layout.addLayout(rename_layout)
        control_layout.addWidget(speaker_group)
        
        main_layout.addWidget(control_group)
    
    def run_single_test(self):
        """執行單次語音測試"""
        self.add_result("🎤 啟動語音測試任務...", "INFO")
        
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
            "enable_speaker_id": self.speaker_id_checkbox.isChecked(),
            "language": self.language_combo.currentText()
        }
        
        # 創建一個任務以在背景執行
        def run_stt_test_task():
            try:
                return self.module_manager.run_test_function(self.module_name, "single_test", params)
            except Exception as e:
                return {"success": False, "error": str(e)}
        
        # 設置任務完成後的回調
        def on_task_complete(task_id, result):
            if result.get('success', False):
                self.add_result(f"✅ 測試完成: {result.get('message', '成功')}", "SUCCESS")
                if 'data' in result:
                    self.add_result(f"結果數據: {json.dumps(result['data'], ensure_ascii=False, indent=2)}", "INFO")
            else:
                self.add_result(f"❌ 測試失敗: {result.get('error', '未知錯誤')}", "ERROR")
        
        # 啟動背景任務
        task_id = "stt_single_test_" + str(id(self))
        worker_manager.signals.finished.connect(on_task_complete)
        worker_manager.start_task(task_id, run_stt_test_task)
        
        self.add_result("🔄 語音測試正在背景執行，請稍候...", "INFO")
    
    def run_continuous_test(self):
        """執行持續監聽測試"""
        self.add_result("🎤 啟動持續監聽任務...", "INFO")
        
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
            "duration": self.duration_spinbox.value()
        }
        
        # 創建一個任務以在背景執行
        def run_continuous_test_task():
            try:
                return self.module_manager.run_test_function(self.module_name, "continuous_test", params)
            except Exception as e:
                return {"success": False, "error": str(e)}
        
        # 設置任務完成後的回調
        def on_task_complete(task_id, result):
            if task_id != "stt_continuous_test_" + str(id(self)):
                return  # 不是我們的任務
                
            if result.get('success', False):
                self.add_result(f"✅ 持續監聽完成: {result.get('message', '成功')}", "SUCCESS")
                if 'data' in result:
                    self.add_result(f"結果數據: {json.dumps(result['data'], ensure_ascii=False, indent=2)}", "INFO")
            else:
                self.add_result(f"❌ 持續監聽失敗: {result.get('error', '未知錯誤')}", "ERROR")
        
        # 啟動背景任務
        task_id = "stt_continuous_test_" + str(id(self))
        worker_manager.signals.finished.connect(on_task_complete)
        worker_manager.start_task(task_id, run_continuous_test_task)
        
        self.add_result(f"🔄 持續監聽（{params['duration']}秒）正在背景執行，UI 將保持響應...", "INFO")
    
    def get_stats(self):
        """獲取統計信息"""
        self.run_test("get_stats")
    
    def list_speakers(self):
        """列出說話人"""
        self.run_test("speaker_list")
    
    def get_speaker_info(self):
        """獲取說話人資訊"""
        self.run_test("speaker_info")
    
    def clear_all_speakers(self):
        """清除所有說話人"""
        self.run_test("speaker_clear_all")
    
    def rename_speaker(self):
        """重命名說話人"""
        old_id = self.old_speaker_input.text().strip()
        new_id = self.new_speaker_input.text().strip()
        
        if not old_id or not new_id:
            self.add_result("❌ 請輸入舊說話人ID和新說話人ID", "ERROR")
            return
        
        params = {
            "old_id": old_id,
            "new_id": new_id
        }
        self.run_test("speaker_rename", params)
