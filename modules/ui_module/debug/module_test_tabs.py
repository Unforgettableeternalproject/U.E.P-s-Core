# -*- coding: utf-8 -*-
"""
模組測試分頁 - 重構版本
提供各個模組的測試介面
"""

import sys
import json
from typing import Dict, Any, Optional, List
from PyQt5.QtWidgets import *
from PyQt5.QtCore import *
from PyQt5.QtGui import *

from .module_manager import ModuleManager


# === 基礎測試分頁類別 ===
class BaseTestTab(QWidget):
    """測試分頁基礎類別"""
    
    def __init__(self, module_name: str, ui_module=None):
        super().__init__()
        self.module_name = module_name
        self.ui_module = ui_module
        self.module_manager = ModuleManager()
        
        # 設定大寫的模組顯示名稱屬性（向後相容）
        self.MODULE_DISPLAY_NAME = module_name.upper()
        self.module_display_name = module_name.upper()
        
        self.init_ui()
    
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
        self.load_module_btn.clicked.connect(self.load_module)
        self.load_module_btn.setMaximumWidth(120)
        control_row.addWidget(self.load_module_btn)
        
        # 卸載模組按鈕
        self.unload_module_btn = QPushButton("📤 卸載模組")
        self.unload_module_btn.clicked.connect(self.unload_module)
        self.unload_module_btn.setMaximumWidth(120)
        control_row.addWidget(self.unload_module_btn)
        
        # 重載模組按鈕
        self.reload_module_btn = QPushButton("🔄 重載模組")
        self.reload_module_btn.clicked.connect(self.reload_module)
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
    
    def load_module(self):
        """載入模組"""
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
        """卸載模組"""
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
        """重載模組"""
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
        import datetime
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


# === STT 模組測試分頁 ===
class STTTestTab(BaseTestTab):
    """STT 模組測試分頁"""
    
    def __init__(self, ui_module=None):
        super().__init__("stt", ui_module)
    
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
        params = {
            "enable_speaker_id": self.speaker_id_checkbox.isChecked(),
            "language": self.language_combo.currentText()
        }
        self.run_test("single_test", params)
    
    def run_continuous_test(self):
        """執行持續監聽測試"""
        params = {
            "duration": self.duration_spinbox.value()
        }
        self.run_test("continuous_test", params)
    
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


# === NLP 模組測試分頁 ===
class NLPTestTab(BaseTestTab):
    """NLP 模組測試分頁"""
    
    def __init__(self, ui_module=None):
        super().__init__("nlp", ui_module)
    
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
        
        control_layout.addWidget(text_group)
        
        # 基本分析功能
        analysis_group = QGroupBox("文本分析")
        analysis_layout = QVBoxLayout(analysis_group)
        
        # 第一排按鈕
        buttons_row1 = QHBoxLayout()
        
        tokenize_btn = QPushButton("🔤 分詞測試")
        tokenize_btn.clicked.connect(self.run_tokenize_test)
        buttons_row1.addWidget(tokenize_btn)
        
        sentiment_btn = QPushButton("😊 情感分析")
        sentiment_btn.clicked.connect(self.run_sentiment_test)
        buttons_row1.addWidget(sentiment_btn)
        
        ner_btn = QPushButton("🏷️ 實體識別")
        ner_btn.clicked.connect(self.run_ner_test)
        buttons_row1.addWidget(ner_btn)
        
        analysis_layout.addLayout(buttons_row1)
        
        # 第二排按鈕
        buttons_row2 = QHBoxLayout()
        
        similarity_btn = QPushButton("🔍 相似度測試")
        similarity_btn.clicked.connect(self.run_similarity_test)
        buttons_row2.addWidget(similarity_btn)
        
        keyword_btn = QPushButton("🗝️ 關鍵詞提取")
        keyword_btn.clicked.connect(self.run_keyword_test)
        buttons_row2.addWidget(keyword_btn)
        
        summary_btn = QPushButton("📄 文本摘要")
        summary_btn.clicked.connect(self.run_summary_test)
        buttons_row2.addWidget(summary_btn)
        
        analysis_layout.addLayout(buttons_row2)
        
        control_layout.addWidget(analysis_group)
        
        # 模型管理
        model_group = QGroupBox("模型管理")
        model_layout = QHBoxLayout(model_group)
        
        model_info_btn = QPushButton("ℹ️ 模型資訊")
        model_info_btn.clicked.connect(self.get_model_info)
        model_layout.addWidget(model_info_btn)
        
        reload_model_btn = QPushButton("🔄 重載模型")
        reload_model_btn.clicked.connect(self.reload_models)
        model_layout.addWidget(reload_model_btn)
        
        stats_btn = QPushButton("📊 處理統計")
        stats_btn.clicked.connect(self.get_processing_stats)
        model_layout.addWidget(stats_btn)
        
        control_layout.addWidget(model_group)
        
        main_layout.addWidget(control_group)
    
    def get_input_text(self):
        """獲取輸入文本"""
        text = self.text_input.toPlainText().strip()
        if not text:
            self.add_result("❌ 請先輸入文本", "ERROR")
            return None
        return text
    
    def run_tokenize_test(self):
        """執行分詞測試"""
        text = self.get_input_text()
        if text:
            params = {"text": text}
            self.run_test("tokenize", params)
    
    def run_sentiment_test(self):
        """執行情感分析測試"""
        text = self.get_input_text()
        if text:
            params = {"text": text}
            self.run_test("sentiment_analysis", params)
    
    def run_ner_test(self):
        """執行實體識別測試"""
        text = self.get_input_text()
        if text:
            params = {"text": text}
            self.run_test("named_entity_recognition", params)
    
    def run_similarity_test(self):
        """執行相似度測試"""
        text = self.get_input_text()
        if text:
            params = {"text": text}
            self.run_test("similarity_test", params)
    
    def run_keyword_test(self):
        """執行關鍵詞提取測試"""
        text = self.get_input_text()
        if text:
            params = {"text": text}
            self.run_test("extract_keywords", params)
    
    def run_summary_test(self):
        """執行文本摘要測試"""
        text = self.get_input_text()
        if text:
            params = {"text": text}
            self.run_test("text_summarization", params)
    
    def get_model_info(self):
        """獲取模型資訊"""
        self.run_test("get_model_info")
    
    def reload_models(self):
        """重載模型"""
        self.run_test("reload_models")
    
    def get_processing_stats(self):
        """獲取處理統計"""
        self.run_test("get_processing_stats")


# === 臨時佔位分頁類別（待重構模組使用） ===
class PlaceholderTestTab(BaseTestTab):
    """佔位測試分頁 - 用於尚未重構的模組"""
    
    def __init__(self, module_name, ui_module=None):
        super().__init__(module_name, ui_module)
        self.module_display_name = module_name.upper()
    
    def create_control_section(self, main_layout):
        """建立佔位控制區域"""
        control_group = QGroupBox(f"{self.module_display_name} 測試控制")
        control_layout = QVBoxLayout(control_group)
        
        # 提示信息
        info_label = QLabel(f"⚠️ {self.module_display_name} 模組尚未完成重構，測試功能暫不可用。")
        info_label.setStyleSheet("color: orange; font-weight: bold; padding: 10px;")
        control_layout.addWidget(info_label)
        
        # 基本信息按鈕
        info_layout = QHBoxLayout()
        
        status_btn = QPushButton("📊 模組狀態")
        status_btn.clicked.connect(lambda: self.run_test("status"))
        info_layout.addWidget(status_btn)
        
        config_btn = QPushButton("⚙️ 設定資訊")
        config_btn.clicked.connect(lambda: self.run_test("config"))
        info_layout.addWidget(config_btn)
        
        control_layout.addLayout(info_layout)
        main_layout.addWidget(control_group)


# === 佔位分頁別名 ===
class MEMTestTab(PlaceholderTestTab):
    def __init__(self, ui_module=None):
        super().__init__("mem", ui_module)

class LLMTestTab(PlaceholderTestTab):
    def __init__(self, ui_module=None):
        super().__init__("llm", ui_module)

class TTSTestTab(PlaceholderTestTab):
    def __init__(self, ui_module=None):
        super().__init__("tts", ui_module)

class SYSTestTab(PlaceholderTestTab):
    def __init__(self, ui_module=None):
        super().__init__("sys", ui_module)

class UITestTab(PlaceholderTestTab):
    def __init__(self, ui_module=None):
        super().__init__("ui", ui_module)

class ANITestTab(PlaceholderTestTab):
    def __init__(self, ui_module=None):
        super().__init__("ani", ui_module)

class MOVTestTab(PlaceholderTestTab):
    def __init__(self, ui_module=None):
        super().__init__("mov", ui_module)
