# module_tabs/tts_test_tab.py
"""
TTS 模組測試分頁

提供文字轉語音模組的完整測試功能
包括文本輸入、情感向量調整、音頻生成和儲存選項
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


class TTSTestTab(BaseTestTab):
    """TTS 模組測試分頁"""
    
    def __init__(self):
        # 預設情感向量 (8D: happy, angry, sad, afraid, disgusted, melancholic, surprised, calm)
        self.emotion_vector = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.5]  # 預設平靜
        
        # 情感預設 - 必須在 super().__init__() 之前定義
        self.emotion_presets = {
            "中性": [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.5],
            "開心": [0.8, 0.0, 0.0, 0.0, 0.0, 0.0, 0.3, 0.4],
            "興奮": [0.9, 0.0, 0.0, 0.0, 0.0, 0.0, 0.7, 0.2],
            "悲傷": [0.0, 0.0, 0.8, 0.2, 0.0, 0.6, 0.0, 0.1],
            "生氣": [0.0, 0.9, 0.2, 0.0, 0.3, 0.0, 0.2, 0.0],
            "害怕": [0.0, 0.0, 0.3, 0.9, 0.0, 0.2, 0.5, 0.0],
            "平靜": [0.2, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.9],
            "愉快": [0.7, 0.0, 0.0, 0.0, 0.0, 0.0, 0.1, 0.6]
        }
        
        # 初始化父類 (會調用 create_control_section)
        super().__init__("tts")
    
    def create_control_section(self, main_layout):
        """建立 TTS 控制區域"""
        control_group = QGroupBox("TTS 測試控制")
        control_layout = QVBoxLayout(control_group)
        
        # 文本輸入區域
        input_group = QGroupBox("文本輸入")
        input_layout = QVBoxLayout(input_group)
        
        self.text_input = QTextEdit()
        self.text_input.setMaximumHeight(80)  # 減少高度
        self.text_input.setPlaceholderText("請輸入要合成語音的文本...")
        input_layout.addWidget(self.text_input)
        
        # 字數統計
        self.char_count_label = QLabel("字數: 0")
        self.text_input.textChanged.connect(self.update_char_count)
        input_layout.addWidget(self.char_count_label)
        
        control_layout.addWidget(input_group)
        
        # 情感控制區域
        emotion_group = QGroupBox("情感向量控制")
        emotion_layout = QVBoxLayout(emotion_group)
        
        # 情感預設選擇
        preset_layout = QHBoxLayout()
        preset_layout.addWidget(QLabel("情感預設:"))
        
        self.preset_combo = QComboBox()
        self.preset_combo.addItems(list(self.emotion_presets.keys()))
        self.preset_combo.currentTextChanged.connect(self.apply_emotion_preset)
        preset_layout.addWidget(self.preset_combo)
        
        # 從狀態管理器獲取按鈕
        status_btn = QPushButton("📊 從狀態管理器獲取")
        status_btn.clicked.connect(self.get_emotion_from_status)
        preset_layout.addWidget(status_btn)
        
        preset_layout.addStretch()
        emotion_layout.addLayout(preset_layout)
        
        # 情感向量滑桿 - 2x4 網格布局
        self.emotion_sliders = {}
        self.emotion_labels = {}
        emotion_names = [
            ("開心", "happy", "😊"),
            ("生氣", "angry", "😠"),
            ("悲傷", "sad", "😢"),
            ("害怕", "afraid", "😨"),
            ("厭惡", "disgusted", "🤢"),
            ("憂鬱", "melancholic", "😔"),
            ("驚訝", "surprised", "😮"),
            ("平靜", "calm", "😌")
        ]
        
        # 使用網格布局 - 每行2個滑桿
        sliders_grid = QGridLayout()
        for i, (cn_name, en_name, emoji) in enumerate(emotion_names):
            row = i // 2  # 每行2個
            col = (i % 2) * 4  # 每個滑桿組占4列
            
            # 標籤
            label = QLabel(f"{emoji} {cn_name}")
            label.setMinimumWidth(50)
            sliders_grid.addWidget(label, row, col)
            
            # 滑桿
            slider = QSlider(Qt.Horizontal)
            slider.setRange(0, 100)
            slider.setValue(int(self.emotion_vector[i] * 100))
            slider.valueChanged.connect(lambda v, idx=i: self.update_emotion_value(idx, v))
            self.emotion_sliders[en_name] = slider
            sliders_grid.addWidget(slider, row, col + 1)
            
            # 數值顯示
            value_label = QLabel(f"{self.emotion_vector[i]:.2f}")
            value_label.setMinimumWidth(35)
            value_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.emotion_labels[en_name] = value_label
            sliders_grid.addWidget(value_label, row, col + 2)
            
            # 列間距
            if col == 0:
                sliders_grid.setColumnMinimumWidth(col + 3, 20)
        
        emotion_layout.addLayout(sliders_grid)
        
        # 情感向量顯示
        vector_display_layout = QHBoxLayout()
        self.vector_display = QLineEdit()
        self.vector_display.setReadOnly(True)
        self.vector_display.setPlaceholderText("情感向量將顯示在這裡...")
        vector_display_layout.addWidget(QLabel("當前向量:"))
        vector_display_layout.addWidget(self.vector_display)
        emotion_layout.addLayout(vector_display_layout)
        
        # 更新向量顯示
        self.update_vector_display()
        
        control_layout.addWidget(emotion_group)
        
        # 合成選項區域
        options_group = QGroupBox("合成選項")
        options_layout = QVBoxLayout(options_group)
        
        # 第一行選項
        options_row1 = QHBoxLayout()
        
        # 儲存選項
        self.save_checkbox = QCheckBox("儲存音頻文件")
        self.save_checkbox.setChecked(False)
        self.save_checkbox.stateChanged.connect(self.toggle_save_options)
        options_row1.addWidget(self.save_checkbox)
        
        # 強制分段
        self.force_chunking_checkbox = QCheckBox("強制分段處理")
        self.force_chunking_checkbox.setChecked(False)
        options_row1.addWidget(self.force_chunking_checkbox)
        
        options_row1.addStretch()
        options_layout.addLayout(options_row1)
        
        # 儲存路徑選擇 (初始隱藏)
        self.save_path_widget = QWidget()
        save_path_layout = QHBoxLayout(self.save_path_widget)
        save_path_layout.setContentsMargins(0, 0, 0, 0)
        
        self.save_path_input = QLineEdit()
        self.save_path_input.setPlaceholderText("音頻文件將儲存到預設位置...")
        save_path_layout.addWidget(QLabel("儲存路徑:"))
        save_path_layout.addWidget(self.save_path_input)
        
        browse_btn = QPushButton("📁 瀏覽")
        browse_btn.clicked.connect(self.browse_save_path)
        save_path_layout.addWidget(browse_btn)
        
        self.save_path_widget.setVisible(False)
        options_layout.addWidget(self.save_path_widget)
        
        # 分段閾值設置
        threshold_layout = QHBoxLayout()
        threshold_layout.addWidget(QLabel("分段閾值:"))
        
        self.threshold_spinbox = QSpinBox()
        self.threshold_spinbox.setRange(50, 500)
        self.threshold_spinbox.setValue(150)
        self.threshold_spinbox.setSuffix(" 字符")
        threshold_layout.addWidget(self.threshold_spinbox)
        
        threshold_layout.addStretch()
        options_layout.addLayout(threshold_layout)
        
        control_layout.addWidget(options_group)
        
        # 執行按鈕區域
        action_group = QGroupBox("執行操作")
        action_layout = QVBoxLayout(action_group)
        
        # 主要合成按鈕
        synthesis_btn_layout = QHBoxLayout()
        
        self.synthesis_btn = QPushButton("🎵 生成語音")
        self.synthesis_btn.setStyleSheet("""
            QPushButton {
                background-color: #4CAF50;
                color: white;
                font-size: 13px;
                font-weight: bold;
                padding: 8px;
                border-radius: 4px;
            }
            QPushButton:hover {
                background-color: #45a049;
            }
            QPushButton:pressed {
                background-color: #3d8b40;
            }
            QPushButton:disabled {
                background-color: #cccccc;
                color: #666666;
            }
        """)
        self.synthesis_btn.clicked.connect(self.run_synthesis)
        synthesis_btn_layout.addWidget(self.synthesis_btn)
        
        action_layout.addLayout(synthesis_btn_layout)
        
        # 快速測試按鈕
        quick_test_layout = QHBoxLayout()
        
        quick_neutral_btn = QPushButton("⚡ 快速測試 (中性)")
        quick_neutral_btn.clicked.connect(lambda: self.quick_test("neutral"))
        quick_test_layout.addWidget(quick_neutral_btn)
        
        quick_happy_btn = QPushButton("⚡ 快速測試 (開心)")
        quick_happy_btn.clicked.connect(lambda: self.quick_test("happy"))
        quick_test_layout.addWidget(quick_happy_btn)
        
        quick_sad_btn = QPushButton("⚡ 快速測試 (悲傷)")
        quick_sad_btn.clicked.connect(lambda: self.quick_test("sad"))
        quick_test_layout.addWidget(quick_sad_btn)
        
        action_layout.addLayout(quick_test_layout)
        
        control_layout.addWidget(action_group)
        
        # 播放狀態顯示
        status_group = QGroupBox("播放狀態")
        status_layout = QVBoxLayout(status_group)
        
        self.playback_status = QTextEdit()
        self.playback_status.setMaximumHeight(70)  # 減少高度
        self.playback_status.setReadOnly(True)
        self.playback_status.setPlaceholderText("播放狀態信息將顯示在這裡...")
        status_layout.addWidget(self.playback_status)
        
        # 播放控制按鈕
        playback_btn_layout = QHBoxLayout()
        
        stop_btn = QPushButton("⏹️ 停止播放")
        stop_btn.clicked.connect(self.stop_playback)
        playback_btn_layout.addWidget(stop_btn)
        
        clear_queue_btn = QPushButton("🗑️ 清除隊列")
        clear_queue_btn.clicked.connect(self.clear_queue)
        playback_btn_layout.addWidget(clear_queue_btn)
        
        playback_btn_layout.addStretch()
        status_layout.addLayout(playback_btn_layout)
        
        control_layout.addWidget(status_group)
        
        main_layout.addWidget(control_group)
        
        # 減少整體間距
        main_layout.setSpacing(8)
        main_layout.setContentsMargins(10, 10, 10, 10)
    
    def update_char_count(self):
        """更新字數統計"""
        text = self.text_input.toPlainText()
        char_count = len(text)
        self.char_count_label.setText(f"字數: {char_count}")
        
        # 如果超過閾值，顯示警告
        if char_count > self.threshold_spinbox.value():
            self.char_count_label.setStyleSheet("color: orange; font-weight: bold;")
            self.char_count_label.setText(f"字數: {char_count} (將自動分段)")
        else:
            self.char_count_label.setStyleSheet("")
    
    def update_emotion_value(self, index, value):
        """更新情感向量值"""
        normalized_value = value / 100.0
        self.emotion_vector[index] = normalized_value
        
        # 更新對應的標籤
        emotion_names = ["happy", "angry", "sad", "afraid", "disgusted", "melancholic", "surprised", "calm"]
        if index < len(emotion_names):
            self.emotion_labels[emotion_names[index]].setText(f"{normalized_value:.2f}")
        
        # 更新向量顯示
        self.update_vector_display()
    
    def update_vector_display(self):
        """更新情感向量顯示"""
        vector_str = "[" + ", ".join([f"{v:.2f}" for v in self.emotion_vector]) + "]"
        self.vector_display.setText(vector_str)
    
    def apply_emotion_preset(self, preset_name):
        """應用情感預設"""
        if preset_name in self.emotion_presets:
            self.emotion_vector = self.emotion_presets[preset_name].copy()
            
            # 更新所有滑桿和標籤
            emotion_names = ["happy", "angry", "sad", "afraid", "disgusted", "melancholic", "surprised", "calm"]
            for i, name in enumerate(emotion_names):
                value = int(self.emotion_vector[i] * 100)
                self.emotion_sliders[name].setValue(value)
                self.emotion_labels[name].setText(f"{self.emotion_vector[i]:.2f}")
            
            # 更新向量顯示
            self.update_vector_display()
            
            self.add_result(f"✅ 已應用情感預設: {preset_name}", "SUCCESS")
    
    def get_emotion_from_status(self):
        """從狀態管理器獲取情感向量"""
        try:
            from core.status_manager import StatusManager
            status_manager = StatusManager()
            status_dict = status_manager.get_status_dict()
            
            # 簡單的映射邏輯 (可以根據需要調整)
            mood = status_dict.get('mood', 0.0)
            pride = status_dict.get('pride', 0.0)
            boredom = status_dict.get('boredom', 0.0)
            
            # 根據狀態計算情感向量
            if mood > 0.5:
                # 正面情緒
                self.emotion_vector = [0.7, 0.0, 0.0, 0.0, 0.0, 0.0, 0.2, 0.5]  # 開心
            elif mood < -0.5:
                # 負面情緒
                self.emotion_vector = [0.0, 0.0, 0.7, 0.2, 0.0, 0.5, 0.0, 0.1]  # 悲傷
            elif boredom > 0.7:
                # 無聊
                self.emotion_vector = [0.0, 0.0, 0.2, 0.0, 0.0, 0.3, 0.0, 0.8]  # 平靜但無聊
            else:
                # 中性
                self.emotion_vector = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.5]
            
            # 更新 UI
            emotion_names = ["happy", "angry", "sad", "afraid", "disgusted", "melancholic", "surprised", "calm"]
            for i, name in enumerate(emotion_names):
                value = int(self.emotion_vector[i] * 100)
                self.emotion_sliders[name].setValue(value)
                self.emotion_labels[name].setText(f"{self.emotion_vector[i]:.2f}")
            
            self.update_vector_display()
            
            self.add_result(f"✅ 已從狀態管理器獲取情感 (mood: {mood:.2f})", "SUCCESS")
            
        except ImportError:
            self.add_result("❌ 無法載入狀態管理器", "ERROR")
        except Exception as e:
            self.add_result(f"❌ 獲取狀態失敗: {str(e)}", "ERROR")
    
    def toggle_save_options(self, state):
        """切換儲存選項顯示"""
        self.save_path_widget.setVisible(state == Qt.Checked)
    
    def browse_save_path(self):
        """瀏覽儲存路徑"""
        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "選擇音頻儲存位置",
            "",
            "WAV 文件 (*.wav);;MP3 文件 (*.mp3);;所有文件 (*.*)"
        )
        
        if file_path:
            self.save_path_input.setText(file_path)
            self.add_result(f"✅ 已選擇儲存路徑: {file_path}", "SUCCESS")
    
    def get_synthesis_params(self):
        """獲取合成參數"""
        text = self.text_input.toPlainText().strip()
        if not text:
            self.add_result("❌ 請先輸入要合成的文本", "ERROR")
            return None
        
        params = {
            "text": text,
            "emotion_vector": self.emotion_vector.copy(),
            "save": self.save_checkbox.isChecked(),
            "force_chunking": self.force_chunking_checkbox.isChecked()
        }
        
        # 如果選擇儲存且指定了路徑
        if params["save"]:
            save_path = self.save_path_input.text().strip()
            if save_path:
                params["output_path"] = save_path
        
        return params
    
    def run_synthesis(self):
        """執行語音合成"""
        params = self.get_synthesis_params()
        if not params:
            return
        
        self.add_result(f"🎵 開始語音合成...", "INFO")
        self.add_result(f"📝 文本長度: {len(params['text'])} 字符", "INFO")
        self.add_result(f"🎭 情感向量: {params['emotion_vector']}", "INFO")
        
        # 禁用合成按鈕
        self.synthesis_btn.setEnabled(False)
        self.synthesis_btn.setText("⏳ 合成中...")
        
        # 修正 background_worker 導入路徑
        import sys
        import os
        debug_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
        if debug_dir not in sys.path:
            sys.path.insert(0, debug_dir)
        
        from background_worker import get_worker_manager
        worker_manager = get_worker_manager()
        
        # 創建背景任務
        def run_synthesis_task():
            try:
                result = self.module_manager.run_test_function(self.module_name, "synthesis", params)
                return result
            except Exception as e:
                return {"success": False, "error": str(e)}
        
        # 設置任務完成回調
        def on_task_complete(task_id, result):
            if task_id != "tts_synthesis_" + str(id(self)):
                return
            
            # 恢復合成按鈕
            self.synthesis_btn.setEnabled(True)
            self.synthesis_btn.setText("🎵 生成語音")
            
            if result.get('success', False):
                self.add_result(f"✅ 語音合成完成", "SUCCESS")
                
                # 顯示結果信息
                if 'duration' in result:
                    self.add_result(f"⏱️ 音頻時長: {result['duration']:.2f}s", "INFO")
                
                if 'processing_time' in result:
                    self.add_result(f"⚡ 處理時間: {result['processing_time']:.2f}s", "INFO")
                
                if 'chunk_count' in result and result['chunk_count'] > 1:
                    self.add_result(f"📦 分段數量: {result['chunk_count']}", "INFO")
                
                if params['save'] and 'output_path' in result:
                    self.add_result(f"💾 已儲存至: {result['output_path']}", "SUCCESS")
                
                # 更新播放狀態
                self.update_playback_status("播放中", result)
                
            else:
                error_msg = result.get('error', '未知錯誤')
                self.add_result(f"❌ 語音合成失敗: {error_msg}", "ERROR")
                self.update_playback_status("失敗", result)
        
        # 啟動背景任務
        task_id = "tts_synthesis_" + str(id(self))
        worker_manager.signals.finished.connect(on_task_complete)
        worker_manager.start_task(task_id, run_synthesis_task)
        
        self.add_result("🔄 合成任務正在背景執行，請稍候...", "INFO")
    
    def quick_test(self, emotion_type):
        """快速測試"""
        # 設置測試文本
        test_texts = {
            "neutral": "Hello! This is a quick test of the text to speech system.",
            "happy": "Wow! I'm so excited to test this amazing speech synthesis!",
            "sad": "I feel a bit down today... Everything seems so difficult."
        }
        
        # 設置對應情感
        emotion_presets_map = {
            "neutral": "中性",
            "happy": "開心",
            "sad": "悲傷"
        }
        
        self.text_input.setText(test_texts.get(emotion_type, test_texts["neutral"]))
        self.preset_combo.setCurrentText(emotion_presets_map.get(emotion_type, "中性"))
        
        self.add_result(f"⚡ 快速測試模式: {emotion_type.upper()}", "INFO")
        
        # 延遲一點再執行合成，讓 UI 更新
        QTimer.singleShot(100, self.run_synthesis)
    
    def update_playback_status(self, status, info=None):
        """更新播放狀態"""
        status_text = f"📻 播放狀態: {status}\n"
        status_text += "=" * 30 + "\n"
        
        if info:
            if 'duration' in info:
                status_text += f"⏱️ 音頻時長: {info['duration']:.2f}s\n"
            
            if 'chunk_count' in info:
                status_text += f"📦 分段數量: {info['chunk_count']}\n"
            
            if 'processing_time' in info:
                status_text += f"⚡ 處理時間: {info['processing_time']:.2f}s\n"
                
                # 計算實時因子
                if 'duration' in info and info['duration'] > 0:
                    rtf = info['processing_time'] / info['duration']
                    status_text += f"📊 實時因子: {rtf:.2f}x\n"
            
            if 'output_path' in info:
                status_text += f"💾 儲存位置: {info['output_path']}\n"
        
        self.playback_status.setText(status_text)
    
    def stop_playback(self):
        """停止播放"""
        self.add_result("⏹️ 嘗試停止播放...", "INFO")
        
        try:
            # 調用模組的停止功能
            result = self.module_manager.run_test_function(
                self.module_name, 
                "stop_playback", 
                {}
            )
            
            if result.get('success', False):
                self.add_result("✅ 已停止播放", "SUCCESS")
                self.update_playback_status("已停止", None)
            else:
                self.add_result(f"❌ 停止失敗: {result.get('error', '未知錯誤')}", "ERROR")
                
        except Exception as e:
            self.add_result(f"❌ 停止播放時發生錯誤: {str(e)}", "ERROR")
    
    def clear_queue(self):
        """清除隊列"""
        self.add_result("🗑️ 嘗試清除播放隊列...", "INFO")
        
        try:
            # 調用模組的清除隊列功能
            result = self.module_manager.run_test_function(
                self.module_name, 
                "clear_queue", 
                {}
            )
            
            if result.get('success', False):
                self.add_result("✅ 已清除播放隊列", "SUCCESS")
                self.update_playback_status("隊列已清空", None)
            else:
                self.add_result(f"❌ 清除失敗: {result.get('error', '未知錯誤')}", "ERROR")
                
        except Exception as e:
            self.add_result(f"❌ 清除隊列時發生錯誤: {str(e)}", "ERROR")
