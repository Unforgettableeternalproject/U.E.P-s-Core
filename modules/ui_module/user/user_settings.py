# user_settings_v2.py - 完整版使用者設定視窗
# 與 configs/user_settings.yaml 100% 對應

import os
import sys
from typing import Dict, Any, Optional

try:
    from PyQt5.QtWidgets import (
        QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
        QTabWidget, QLabel, QGroupBox, QScrollArea,
        QFrame, QPushButton, QCheckBox, QSpinBox, QDoubleSpinBox,
        QSlider, QComboBox, QLineEdit, QTextEdit,
        QFormLayout, QSizePolicy, QApplication, QMessageBox,
        QListWidget, QListWidgetItem, QDialog, QDialogButtonBox
    )
    from PyQt5.QtCore import Qt, QTimer, pyqtSignal
    from PyQt5.QtGui import QFont
    PYQT5_AVAILABLE = True
except ImportError:
    PYQT5_AVAILABLE = False
    QMainWindow = object
    QWidget = object
    pyqtSignal = None
    QListWidget = object
    QListWidgetItem = object
    QDialog = object
    QDialogButtonBox = object

try:
    from .theme_manager import theme_manager, Theme, install_theme_hook
except ImportError:
    try:
        from theme_manager import theme_manager, Theme, install_theme_hook
    except ImportError:
        theme_manager = None
        Theme = None
        install_theme_hook = lambda x: None

try:
    from configs.user_settings_manager import (
        load_user_settings, get_user_setting, set_user_setting, 
        user_settings_manager
    )
except ImportError:
    # Fallback 如果 user_settings_manager 不可用
    def load_user_settings(): return {}
    def get_user_setting(path, default=None): return default
    def set_user_setting(path, value): pass
    user_settings_manager = None

from utils.debug_helper import debug_log, info_log, error_log, OPERATION_LEVEL, SYSTEM_LEVEL


class UserMainWindow(QMainWindow):
    """使用者設定視窗 - 完整版本，對應所有 YAML 設定"""
    
    settings_changed = pyqtSignal(str, object)
    window_closed = pyqtSignal()
    
    def __init__(self, ui_module=None):
        super().__init__()
        
        if not PYQT5_AVAILABLE:
            error_log("[UserMainWindow] PyQt5 不可用")
            return
            
        self.ui_module = ui_module
        self.is_minimized_to_orb = False
        self.original_geometry = None
        
        self.init_ui()
        if theme_manager:
            install_theme_hook(self)
            theme_manager.theme_changed.connect(self._on_theme_changed)
        
        self.load_settings()
        self.hide()
        
        info_log("[UserMainWindow] 設定視窗初始化完成")
    
    def init_ui(self):
        """初始化 UI"""
        self.setWindowTitle("UEP 設定")
        self.setMinimumSize(900, 700)
        self.resize(1100, 800)
        
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        
        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)
        
        # 創建標題列
        self.create_header(main_layout)
        
        # 創建分頁
        self.create_tabs(main_layout)
        
        # 創建底部按鈕
        self.create_bottom_buttons(main_layout)
        
        debug_log(SYSTEM_LEVEL, "[UserMainWindow] UI 初始化完成")
    
    def create_header(self, parent_layout):
        """創建標題列"""
        header = QFrame()
        header.setObjectName("header")
        header.setFixedHeight(80)
        
        layout = QHBoxLayout(header)
        layout.setContentsMargins(24, 12, 24, 12)
        
        # 標題
        title_container = QVBoxLayout()
        title = QLabel("設定")
        title.setObjectName("mainTitle")
        title.setStyleSheet("font-size: 24px; font-weight: bold;")
        
        subtitle = QLabel("管理您的 UEP 系統設定")
        subtitle.setObjectName("subtitle")
        subtitle.setStyleSheet("font-size: 13px; color: gray;")
        
        title_container.addWidget(title)
        title_container.addWidget(subtitle)
        
        layout.addLayout(title_container)
        layout.addStretch()
        
        # 主題切換按鈕
        if theme_manager:
            self.theme_toggle = QPushButton()
            self.theme_toggle.setFixedSize(48, 48)
            self.theme_toggle.setCursor(Qt.PointingHandCursor)
            self.theme_toggle.setFont(QFont("Segoe UI Emoji", 18))
            self.theme_toggle.setText("☀️" if theme_manager.theme == Theme.DARK else "🌙")
            self.theme_toggle.clicked.connect(self.toggle_theme)
            layout.addWidget(self.theme_toggle)
        
        parent_layout.addWidget(header)
    
    def create_tabs(self, parent_layout):
        """創建分頁容器"""
        self.tab_widget = QTabWidget()
        self.tab_widget.setObjectName("mainTabs")
        
        # 5 個分頁
        self.create_tab1_basic()
        self.create_tab2_speech()
        self.create_tab3_memory()
        self.create_tab4_behavior()
        self.create_tab5_advanced()
        
        parent_layout.addWidget(self.tab_widget, 1)
    
    def create_bottom_buttons(self, parent_layout):
        """創建底部按鈕列"""
        button_frame = QFrame()
        button_frame.setFixedHeight(60)
        button_frame.setObjectName("buttonFrame")
        
        layout = QHBoxLayout(button_frame)
        layout.setContentsMargins(24, 12, 24, 12)
        
        layout.addStretch()
        
        self.apply_btn = QPushButton("套用")
        self.apply_btn.setFixedSize(100, 36)
        self.apply_btn.clicked.connect(self.apply_settings)
        
        self.ok_btn = QPushButton("確定")
        self.ok_btn.setFixedSize(100, 36)
        self.ok_btn.clicked.connect(self.ok_clicked)
        
        self.cancel_btn = QPushButton("取消")
        self.cancel_btn.setFixedSize(100, 36)
        self.cancel_btn.clicked.connect(self.cancel_clicked)
        
        layout.addWidget(self.apply_btn)
        layout.addWidget(self.ok_btn)
        layout.addWidget(self.cancel_btn)
        
        parent_layout.addWidget(button_frame)
    
    def _make_scroll_area(self) -> QScrollArea:
        """創建標準捲軸區域"""
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setMinimumHeight(500)
        return scroll
    
    def _make_group(self, title: str) -> QGroupBox:
        """創建標準群組框"""
        group = QGroupBox(title)
        group.setObjectName("settingsGroup")
        group.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        return group
    
    # ============================================================================
    # Tab 1: 基本設定 (身分、系統、介面)
    # ============================================================================
    
    def create_tab1_basic(self):
        """Tab 1: 基本設定"""
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(16)
        
        scroll = self._make_scroll_area()
        scroll_content = QWidget()
        scroll_layout = QVBoxLayout(scroll_content)
        scroll_layout.setSpacing(16)
        
        # 1. 身分設定
        identity_group = self._make_group("身分設定")
        identity_main_layout = QVBoxLayout(identity_group)
        identity_main_layout.setSpacing(12)
        identity_main_layout.setContentsMargins(16, 20, 16, 16)
        
        # 基本名稱設定
        identity_layout = QFormLayout()
        identity_layout.setSpacing(8)
        
        self.user_name_edit = QLineEdit()
        self.user_name_edit.setPlaceholderText("例如：小明")
        identity_layout.addRow("使用者名稱:", self.user_name_edit)
        
        self.uep_name_edit = QLineEdit()
        self.uep_name_edit.setPlaceholderText("例如：U.E.P")
        identity_layout.addRow("UEP 名稱:", self.uep_name_edit)
        
        identity_main_layout.addLayout(identity_layout)
        
        # 身分清單區域
        identity_list_label = QLabel("身分清單:")
        identity_list_label.setStyleSheet("font-weight: bold; margin-top: 8px;")
        identity_main_layout.addWidget(identity_list_label)
        
        # 身分列表
        self.identity_list_widget = QListWidget()
        self.identity_list_widget.setMaximumHeight(150)
        self.identity_list_widget.itemDoubleClicked.connect(self._on_identity_double_clicked)
        identity_main_layout.addWidget(self.identity_list_widget)
        
        # 身分操作按鈕
        identity_btn_layout = QHBoxLayout()
        
        self.switch_identity_btn = QPushButton("切換身分")
        self.switch_identity_btn.clicked.connect(self._switch_identity)
        identity_btn_layout.addWidget(self.switch_identity_btn)
        
        self.create_identity_btn = QPushButton("新增身分")
        self.create_identity_btn.clicked.connect(self._create_identity)
        identity_btn_layout.addWidget(self.create_identity_btn)
        
        self.delete_identity_btn = QPushButton("刪除身分")
        self.delete_identity_btn.clicked.connect(self._delete_identity)
        identity_btn_layout.addWidget(self.delete_identity_btn)
        
        self.refresh_identity_btn = QPushButton("刷新")
        self.refresh_identity_btn.clicked.connect(self._refresh_identity_list)
        identity_btn_layout.addWidget(self.refresh_identity_btn)
        
        identity_btn_layout.addStretch()
        identity_main_layout.addLayout(identity_btn_layout)
        
        # 身分選項
        self.allow_identity_creation_cb = QCheckBox("允許創建新身分")
        identity_main_layout.addWidget(self.allow_identity_creation_cb)
        
        scroll_layout.addWidget(identity_group)
        
        # 2. 系統行為
        system_group = self._make_group("系統行為")
        system_layout = QFormLayout(system_group)
        system_layout.setSpacing(12)
        system_layout.setContentsMargins(16, 20, 16, 16)
        
        self.language_combo = QComboBox()
        self.language_combo.addItems(["zh-TW", "zh-CN", "en-US", "ja-JP"])
        system_layout.addRow("語言 ⚠️:", self.language_combo)
        
        self.enable_debug_mode_cb = QCheckBox("啟用除錯模式 ⚠️")
        system_layout.addRow("", self.enable_debug_mode_cb)
        
        self.debug_level_spin = QSpinBox()
        self.debug_level_spin.setRange(0, 5)
        system_layout.addRow("除錯級別:", self.debug_level_spin)
        
        self.enable_frontend_debug_cb = QCheckBox("啟用前端除錯")
        system_layout.addRow("", self.enable_frontend_debug_cb)
        
        self.auto_save_settings_cb = QCheckBox("自動保存設定")
        system_layout.addRow("", self.auto_save_settings_cb)
        
        self.confirm_before_exit_cb = QCheckBox("退出前確認")
        system_layout.addRow("", self.confirm_before_exit_cb)
        
        self.main_loop_interval_spin = QDoubleSpinBox()
        self.main_loop_interval_spin.setRange(0.01, 1.0)
        self.main_loop_interval_spin.setSingleStep(0.01)
        self.main_loop_interval_spin.setDecimals(2)
        self.main_loop_interval_spin.setSuffix(" 秒")
        system_layout.addRow("主循環間隔 ⚠️:", self.main_loop_interval_spin)
        
        self.shutdown_timeout_spin = QDoubleSpinBox()
        self.shutdown_timeout_spin.setRange(1.0, 30.0)
        self.shutdown_timeout_spin.setSingleStep(0.5)
        self.shutdown_timeout_spin.setSuffix(" 秒")
        system_layout.addRow("關機超時:", self.shutdown_timeout_spin)
        
        scroll_layout.addWidget(system_group)
        
        # 3. 介面設定
        interface_group = self._make_group("介面設定")
        interface_layout = QFormLayout(interface_group)
        interface_layout.setSpacing(12)
        interface_layout.setContentsMargins(16, 20, 16, 16)
        
        self.theme_combo = QComboBox()
        self.theme_combo.addItems(["auto", "light", "dark"])
        interface_layout.addRow("主題:", self.theme_combo)
        
        self.ui_scale_spin = QDoubleSpinBox()
        self.ui_scale_spin.setRange(0.5, 2.0)
        self.ui_scale_spin.setSingleStep(0.1)
        self.ui_scale_spin.setDecimals(1)
        interface_layout.addRow("UI 縮放 ⚠️:", self.ui_scale_spin)
        
        self.animation_quality_combo = QComboBox()
        self.animation_quality_combo.addItems(["low", "medium", "high"])
        interface_layout.addRow("動畫品質 ⚠️:", self.animation_quality_combo)
        
        self.enable_effects_cb = QCheckBox("啟用視覺效果")
        interface_layout.addRow("", self.enable_effects_cb)
        
        self.font_size_spin = QSpinBox()
        self.font_size_spin.setRange(8, 24)
        interface_layout.addRow("字體大小:", self.font_size_spin)
        
        scroll_layout.addWidget(interface_group)
        
        # 4. 小工具設定
        widget_group = self._make_group("小工具設定")
        widget_layout = QFormLayout(widget_group)
        widget_layout.setSpacing(12)
        widget_layout.setContentsMargins(16, 20, 16, 16)
        
        self.auto_hide_cb = QCheckBox("允許自動隱藏")
        widget_layout.addRow("", self.auto_hide_cb)
        
        self.hide_edge_threshold_spin = QSpinBox()
        self.hide_edge_threshold_spin.setRange(50, 500)
        self.hide_edge_threshold_spin.setSuffix(" px")
        widget_layout.addRow("隱藏觸發距離:", self.hide_edge_threshold_spin)
        
        self.animation_speed_spin = QSpinBox()
        self.animation_speed_spin.setRange(100, 1000)
        self.animation_speed_spin.setSuffix(" ms")
        widget_layout.addRow("動畫速度:", self.animation_speed_spin)
        
        scroll_layout.addWidget(widget_group)
        
        # 5. 視窗顯示控制
        window_group = self._make_group("視窗顯示控制")
        window_layout = QFormLayout(window_group)
        window_layout.setSpacing(12)
        window_layout.setContentsMargins(16, 20, 16, 16)
        
        self.always_on_top_cb = QCheckBox("固定在最上層")
        window_layout.addRow("", self.always_on_top_cb)
        
        self.transparency_cb = QCheckBox("啟用透明度")
        window_layout.addRow("", self.transparency_cb)
        
        self.show_hitbox_cb = QCheckBox("顯示碰撞框")
        window_layout.addRow("", self.show_hitbox_cb)
        
        self.show_desktop_pet_cb = QCheckBox("顯示桌面寵物")
        window_layout.addRow("", self.show_desktop_pet_cb)
        
        self.show_access_widget_cb = QCheckBox("顯示存取小工具")
        window_layout.addRow("", self.show_access_widget_cb)
        
        self.show_debug_window_cb = QCheckBox("顯示除錯視窗")
        window_layout.addRow("", self.show_debug_window_cb)
        
        scroll_layout.addWidget(window_group)
        
        scroll_layout.addStretch()
        scroll.setWidget(scroll_content)
        layout.addWidget(scroll)
        
        self.tab_widget.addTab(widget, "基本設定")
    
    # ============================================================================
    # Tab 2: 語音互動 (STT、TTS)
    # ============================================================================
    
    def create_tab2_speech(self):
        """Tab 2: 語音互動"""
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(16)
        
        scroll = self._make_scroll_area()
        scroll_content = QWidget()
        scroll_layout = QVBoxLayout(scroll_content)
        scroll_layout.setSpacing(16)
        
        # 1. STT 語音輸入設定
        stt_group = self._make_group("STT 語音輸入設定")
        stt_layout = QFormLayout(stt_group)
        stt_layout.setSpacing(12)
        stt_layout.setContentsMargins(16, 20, 16, 16)
        
        self.stt_enabled_cb = QCheckBox("啟用語音輸入 ⚠️")
        stt_layout.addRow("", self.stt_enabled_cb)
        
        self.microphone_device_index_spin = QSpinBox()
        self.microphone_device_index_spin.setRange(0, 10)
        stt_layout.addRow("麥克風裝置索引 ⚠️:", self.microphone_device_index_spin)
        
        self.vad_sensitivity_spin = QDoubleSpinBox()
        self.vad_sensitivity_spin.setRange(0.0, 1.0)
        self.vad_sensitivity_spin.setSingleStep(0.1)
        self.vad_sensitivity_spin.setDecimals(1)
        stt_layout.addRow("VAD 靈敏度:", self.vad_sensitivity_spin)
        
        self.min_speech_duration_spin = QDoubleSpinBox()
        self.min_speech_duration_spin.setRange(0.1, 3.0)
        self.min_speech_duration_spin.setSingleStep(0.1)
        self.min_speech_duration_spin.setDecimals(1)
        self.min_speech_duration_spin.setSuffix(" 秒")
        stt_layout.addRow("最小語音持續:", self.min_speech_duration_spin)
        
        self.enable_continuous_mode_cb = QCheckBox("連續模式 ⚠️")
        stt_layout.addRow("", self.enable_continuous_mode_cb)
        
        self.wake_word_confidence_spin = QDoubleSpinBox()
        self.wake_word_confidence_spin.setRange(0.0, 1.0)
        self.wake_word_confidence_spin.setSingleStep(0.1)
        self.wake_word_confidence_spin.setDecimals(1)
        stt_layout.addRow("喚醒詞信心度:", self.wake_word_confidence_spin)
        
        scroll_layout.addWidget(stt_group)
        
        # 2. TTS 語音輸出設定
        tts_group = self._make_group("TTS 語音輸出設定")
        tts_layout = QFormLayout(tts_group)
        tts_layout.setSpacing(12)
        tts_layout.setContentsMargins(16, 20, 16, 16)
        
        self.tts_enabled_cb = QCheckBox("啟用語音輸出 ⚠️")
        tts_layout.addRow("", self.tts_enabled_cb)
        
        # 音量滑桿
        volume_container = QHBoxLayout()
        self.tts_volume_slider = QSlider(Qt.Horizontal)
        self.tts_volume_slider.setRange(0, 100)
        self.tts_volume_label = QLabel("70")
        self.tts_volume_slider.valueChanged.connect(
            lambda v: self.tts_volume_label.setText(str(v))
        )
        volume_container.addWidget(self.tts_volume_slider)
        volume_container.addWidget(self.tts_volume_label)
        tts_layout.addRow("音量:", volume_container)
        
        self.tts_speed_spin = QDoubleSpinBox()
        self.tts_speed_spin.setRange(0.5, 2.0)
        self.tts_speed_spin.setSingleStep(0.1)
        self.tts_speed_spin.setDecimals(1)
        tts_layout.addRow("語速倍率:", self.tts_speed_spin)
        
        self.default_emotion_combo = QComboBox()
        self.default_emotion_combo.addItems(["neutral", "happy", "sad", "angry", "excited"])
        tts_layout.addRow("預設情緒:", self.default_emotion_combo)
        
        self.emotion_intensity_spin = QDoubleSpinBox()
        self.emotion_intensity_spin.setRange(0.0, 1.0)
        self.emotion_intensity_spin.setSingleStep(0.1)
        self.emotion_intensity_spin.setDecimals(1)
        tts_layout.addRow("情緒強度:", self.emotion_intensity_spin)
        
        scroll_layout.addWidget(tts_group)
        
        scroll_layout.addStretch()
        scroll.setWidget(scroll_content)
        layout.addWidget(scroll)
        
        self.tab_widget.addTab(widget, "語音互動")
    
    # ============================================================================
    # Tab 3: 記憶與對話 (MEM、LLM、主動性、隱私)
    # ============================================================================
    
    def create_tab3_memory(self):
        """Tab 3: 記憶與對話"""
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(16)
        
        scroll = self._make_scroll_area()
        scroll_content = QWidget()
        scroll_layout = QVBoxLayout(scroll_content)
        scroll_layout.setSpacing(16)
        
        # 1. MEM 記憶系統設定
        mem_group = self._make_group("MEM 記憶系統設定")
        mem_layout = QFormLayout(mem_group)
        mem_layout.setSpacing(12)
        mem_layout.setContentsMargins(16, 20, 16, 16)
        
        self.mem_enabled_cb = QCheckBox("啟用記憶系統 ⚠️")
        mem_layout.addRow("", self.mem_enabled_cb)
        
        # 注意：對話已在快照中自動保存，記憶管理基於 GS 疊代數而非天數
        
        scroll_layout.addWidget(mem_group)
        
        # 2. LLM 對話設定
        llm_group = self._make_group("LLM 對話設定")
        llm_layout = QFormLayout(llm_group)
        llm_layout.setSpacing(12)
        llm_layout.setContentsMargins(16, 20, 16, 16)
        
        self.user_additional_prompt_edit = QTextEdit()
        self.user_additional_prompt_edit.setMaximumHeight(80)
        self.user_additional_prompt_edit.setPlaceholderText("輸入額外提示（最多 200 字元）")
        llm_layout.addRow("使用者額外提示:", self.user_additional_prompt_edit)
        
        self.temperature_spin = QDoubleSpinBox()
        self.temperature_spin.setRange(0.0, 2.0)
        self.temperature_spin.setSingleStep(0.1)
        self.temperature_spin.setDecimals(1)
        llm_layout.addRow("對話溫度:", self.temperature_spin)
        
        self.enable_learning_cb = QCheckBox("啟用學習系統")
        llm_layout.addRow("", self.enable_learning_cb)
        
        scroll_layout.addWidget(llm_group)
        
        # 3. 系統主動性設定
        proactivity_group = self._make_group("系統主動性設定")
        proactivity_layout = QFormLayout(proactivity_group)
        proactivity_layout.setSpacing(12)
        proactivity_layout.setContentsMargins(16, 20, 16, 16)
        
        self.allow_system_initiative_cb = QCheckBox("允許系統主動觸發")
        proactivity_layout.addRow("", self.allow_system_initiative_cb)
        
        self.initiative_cooldown_spin = QSpinBox()
        self.initiative_cooldown_spin.setRange(10, 3600)
        self.initiative_cooldown_spin.setSuffix(" 秒")
        proactivity_layout.addRow("主動觸發冷卻:", self.initiative_cooldown_spin)
        
        self.require_user_input_cb = QCheckBox("所有對話等待使用者輸入")
        proactivity_layout.addRow("", self.require_user_input_cb)
        
        scroll_layout.addWidget(proactivity_group)
        
        # 4. 隱私與安全設定
        privacy_group = self._make_group("隱私與安全設定")
        privacy_layout = QFormLayout(privacy_group)
        privacy_layout.setSpacing(12)
        privacy_layout.setContentsMargins(16, 20, 16, 16)
        
        self.allow_usage_statistics_cb = QCheckBox("允許使用統計")
        privacy_layout.addRow("", self.allow_usage_statistics_cb)
        
        self.allow_error_reporting_cb = QCheckBox("允許錯誤回報")
        privacy_layout.addRow("", self.allow_error_reporting_cb)
        
        self.anonymize_data_cb = QCheckBox("匿名化資料")
        privacy_layout.addRow("", self.anonymize_data_cb)
        
        self.auto_delete_old_conversations_cb = QCheckBox("自動刪除舊對話")
        privacy_layout.addRow("", self.auto_delete_old_conversations_cb)
        
        self.conversation_retention_days_spin = QSpinBox()
        self.conversation_retention_days_spin.setRange(1, 3650)
        self.conversation_retention_days_spin.setSuffix(" 天")
        privacy_layout.addRow("對話保留天數:", self.conversation_retention_days_spin)
        
        self.clear_cache_on_exit_cb = QCheckBox("退出時清除快取")
        privacy_layout.addRow("", self.clear_cache_on_exit_cb)
        
        scroll_layout.addWidget(privacy_group)
        
        scroll_layout.addStretch()
        scroll.setWidget(scroll_content)
        layout.addWidget(scroll)
        
        self.tab_widget.addTab(widget, "記憶與對話")
    
    # ============================================================================
    # Tab 4: 行為與移動 (調皮、權限、自動睡眠、MOV)
    # ============================================================================
    
    def create_tab4_behavior(self):
        """Tab 4: 行為與移動"""
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(16)
        
        scroll = self._make_scroll_area()
        scroll_content = QWidget()
        scroll_layout = QVBoxLayout(scroll_content)
        scroll_layout.setSpacing(16)
        
        # 1. 搗蛋模式設定
        mischief_group = self._make_group("搗蛋模式設定")
        mischief_layout = QFormLayout(mischief_group)
        mischief_layout.setSpacing(12)
        mischief_layout.setContentsMargins(16, 20, 16, 16)
        
        self.mischief_enabled_cb = QCheckBox("啟用搗蛋模式")
        mischief_layout.addRow("", self.mischief_enabled_cb)
        
        self.intensity_combo = QComboBox()
        self.intensity_combo.addItems(["low", "medium", "high"])
        mischief_layout.addRow("行為強度上限:", self.intensity_combo)
        
        self.tease_frequency_spin = QDoubleSpinBox()
        self.tease_frequency_spin.setRange(0.0, 1.0)
        self.tease_frequency_spin.setSingleStep(0.01)
        self.tease_frequency_spin.setDecimals(2)
        mischief_layout.addRow("調皮頻率:", self.tease_frequency_spin)
        
        self.easter_egg_enabled_cb = QCheckBox("啟用彩蛋動畫 ⚠️")
        mischief_layout.addRow("", self.easter_egg_enabled_cb)
        
        scroll_layout.addWidget(mischief_group)
        
        # 2. 系統權限設定
        permissions_group = self._make_group("系統權限設定")
        permissions_layout = QFormLayout(permissions_group)
        permissions_layout.setSpacing(12)
        permissions_layout.setContentsMargins(16, 20, 16, 16)
        
        self.allow_file_creation_cb = QCheckBox("允許創建檔案")
        permissions_layout.addRow("", self.allow_file_creation_cb)
        
        self.allow_file_modification_cb = QCheckBox("允許修改檔案")
        permissions_layout.addRow("", self.allow_file_modification_cb)
        
        self.allow_file_deletion_cb = QCheckBox("允許刪除檔案")
        permissions_layout.addRow("", self.allow_file_deletion_cb)
        
        self.allow_app_launch_cb = QCheckBox("允許啟動應用程式")
        permissions_layout.addRow("", self.allow_app_launch_cb)
        
        self.allow_system_commands_cb = QCheckBox("允許執行系統命令")
        permissions_layout.addRow("", self.allow_system_commands_cb)
        
        self.require_confirmation_cb = QCheckBox("敏感操作需確認")
        permissions_layout.addRow("", self.require_confirmation_cb)
        
        scroll_layout.addWidget(permissions_group)
        
        # 3. 自動睡眠設定
        auto_sleep_group = self._make_group("自動睡眠設定")
        auto_sleep_layout = QFormLayout(auto_sleep_group)
        auto_sleep_layout.setSpacing(12)
        auto_sleep_layout.setContentsMargins(16, 20, 16, 16)
        
        self.auto_sleep_enabled_cb = QCheckBox("啟用自動睡眠")
        auto_sleep_layout.addRow("", self.auto_sleep_enabled_cb)
        
        self.max_idle_time_spin = QSpinBox()
        self.max_idle_time_spin.setRange(60, 1800)
        self.max_idle_time_spin.setSuffix(" 秒")
        auto_sleep_layout.addRow("最大閒置時間:", self.max_idle_time_spin)
        
        self.sleep_animation_edit = QLineEdit()
        self.sleep_animation_edit.setPlaceholderText("例如：sleep_l")
        auto_sleep_layout.addRow("睡眠動畫名稱:", self.sleep_animation_edit)
        
        self.wake_on_interaction_cb = QCheckBox("互動時自動喚醒")
        auto_sleep_layout.addRow("", self.wake_on_interaction_cb)
        
        scroll_layout.addWidget(auto_sleep_group)
        
        # 4. MOV 移動與物理設定
        mov_group = self._make_group("MOV 移動與物理設定")
        mov_layout = QFormLayout(mov_group)
        mov_layout.setSpacing(12)
        mov_layout.setContentsMargins(16, 20, 16, 16)
        
        self.boundary_mode_combo = QComboBox()
        self.boundary_mode_combo.addItems(["barrier", "wrap"])
        mov_layout.addRow("邊界模式 ⚠️:", self.boundary_mode_combo)
        
        self.enable_throw_behavior_cb = QCheckBox("啟用投擲行為 ⚠️")
        mov_layout.addRow("", self.enable_throw_behavior_cb)
        
        self.max_throw_speed_spin = QDoubleSpinBox()
        self.max_throw_speed_spin.setRange(10.0, 200.0)
        self.max_throw_speed_spin.setSingleStep(10.0)
        self.max_throw_speed_spin.setDecimals(1)
        mov_layout.addRow("投擲速度上限 ⚠️:", self.max_throw_speed_spin)
        
        self.enable_cursor_tracking_cb = QCheckBox("啟用滑鼠追蹤 ⚠️")
        mov_layout.addRow("", self.enable_cursor_tracking_cb)
        
        self.movement_smoothing_cb = QCheckBox("移動平滑化 ⚠️")
        mov_layout.addRow("", self.movement_smoothing_cb)
        
        self.ground_friction_spin = QDoubleSpinBox()
        self.ground_friction_spin.setRange(0.0, 1.0)
        self.ground_friction_spin.setSingleStep(0.05)
        self.ground_friction_spin.setDecimals(2)
        mov_layout.addRow("地面摩擦係數 ⚠️:", self.ground_friction_spin)
        
        scroll_layout.addWidget(mov_group)
        
        scroll_layout.addStretch()
        scroll.setWidget(scroll_content)
        layout.addWidget(scroll)
        
        self.tab_widget.addTab(widget, "行為與移動")
    
    # ============================================================================
    # Tab 5: 監控與進階 (背景任務、效能、日誌、模組、快捷鍵)
    # ============================================================================
    
    def create_tab5_advanced(self):
        """Tab 5: 監控與進階"""
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(16)
        
        scroll = self._make_scroll_area()
        scroll_content = QWidget()
        scroll_layout = QVBoxLayout(scroll_content)
        scroll_layout.setSpacing(16)
        
        # 1. 背景工作設定
        bg_tasks_group = self._make_group("背景工作設定")
        bg_tasks_layout = QFormLayout(bg_tasks_group)
        bg_tasks_layout.setSpacing(12)
        bg_tasks_layout.setContentsMargins(16, 20, 16, 16)
        
        self.bg_tasks_enabled_cb = QCheckBox("啟用背景工作")
        bg_tasks_layout.addRow("", self.bg_tasks_enabled_cb)
        
        self.default_media_folder_edit = QLineEdit()
        self.default_media_folder_edit.setPlaceholderText("預設媒體資料夾路徑")
        bg_tasks_layout.addRow("媒體資料夾:", self.default_media_folder_edit)
        
        self.allow_internet_access_cb = QCheckBox("允許網路存取")
        bg_tasks_layout.addRow("", self.allow_internet_access_cb)
        
        self.allow_api_calls_cb = QCheckBox("允許 API 呼叫")
        bg_tasks_layout.addRow("", self.allow_api_calls_cb)
        
        self.network_timeout_spin = QSpinBox()
        self.network_timeout_spin.setRange(5, 120)
        self.network_timeout_spin.setSuffix(" 秒")
        bg_tasks_layout.addRow("網路請求超時:", self.network_timeout_spin)
        
        scroll_layout.addWidget(bg_tasks_group)
        
        # 2. 效能設定
        performance_group = self._make_group("效能設定")
        performance_layout = QFormLayout(performance_group)
        performance_layout.setSpacing(12)
        performance_layout.setContentsMargins(16, 20, 16, 16)
        
        self.max_fps_spin = QSpinBox()
        self.max_fps_spin.setRange(15, 120)
        self.max_fps_spin.setSuffix(" FPS")
        performance_layout.addRow("最大幀率 ⚠️:", self.max_fps_spin)
        
        self.enable_hardware_acceleration_cb = QCheckBox("硬體加速 ⚠️")
        performance_layout.addRow("", self.enable_hardware_acceleration_cb)
        
        self.reduce_animations_on_battery_cb = QCheckBox("電池模式減少動畫")
        performance_layout.addRow("", self.reduce_animations_on_battery_cb)
        
        self.gc_interval_spin = QSpinBox()
        self.gc_interval_spin.setRange(60, 3600)
        self.gc_interval_spin.setSuffix(" 秒")
        performance_layout.addRow("垃圾回收間隔:", self.gc_interval_spin)
        
        scroll_layout.addWidget(performance_group)
        
        # 3. 日誌設定
        logging_group = self._make_group("日誌設定")
        logging_layout = QFormLayout(logging_group)
        logging_layout.setSpacing(12)
        logging_layout.setContentsMargins(16, 20, 16, 16)
        
        self.logging_enabled_cb = QCheckBox("啟用日誌系統 ⚠️")
        logging_layout.addRow("", self.logging_enabled_cb)
        
        self.log_level_combo = QComboBox()
        self.log_level_combo.addItems(["DEBUG", "INFO", "WARNING", "ERROR"])
        logging_layout.addRow("日誌級別:", self.log_level_combo)
        
        self.log_dir_edit = QLineEdit()
        self.log_dir_edit.setPlaceholderText("logs")
        logging_layout.addRow("日誌目錄:", self.log_dir_edit)
        
        self.enable_split_logs_cb = QCheckBox("分割日誌檔案 ⚠️")
        logging_layout.addRow("", self.enable_split_logs_cb)
        
        self.enable_console_output_cb = QCheckBox("啟用控制台輸出")
        logging_layout.addRow("", self.enable_console_output_cb)
        
        self.save_logs_cb = QCheckBox("保存日誌檔案")
        logging_layout.addRow("", self.save_logs_cb)
        
        self.max_log_size_mb_spin = QSpinBox()
        self.max_log_size_mb_spin.setRange(1, 500)
        self.max_log_size_mb_spin.setSuffix(" MB")
        logging_layout.addRow("最大日誌大小:", self.max_log_size_mb_spin)
        
        self.log_rotation_days_spin = QSpinBox()
        self.log_rotation_days_spin.setRange(1, 90)
        self.log_rotation_days_spin.setSuffix(" 天")
        logging_layout.addRow("日誌輪替天數:", self.log_rotation_days_spin)
        
        scroll_layout.addWidget(logging_group)
        
        # 4. 模組控制
        modules_group = self._make_group("模組控制 (進階用戶)")
        modules_layout = QFormLayout(modules_group)
        modules_layout.setSpacing(12)
        modules_layout.setContentsMargins(16, 20, 16, 16)
        
        self.stt_module_enabled_cb = QCheckBox("STT 模組 ⚠️")
        modules_layout.addRow("", self.stt_module_enabled_cb)
        
        self.nlp_module_enabled_cb = QCheckBox("NLP 模組 ⚠️")
        modules_layout.addRow("", self.nlp_module_enabled_cb)
        
        self.mem_module_enabled_cb = QCheckBox("MEM 模組 ⚠️")
        modules_layout.addRow("", self.mem_module_enabled_cb)
        
        self.llm_module_enabled_cb = QCheckBox("LLM 模組 ⚠️")
        modules_layout.addRow("", self.llm_module_enabled_cb)
        
        self.tts_module_enabled_cb = QCheckBox("TTS 模組 ⚠️")
        modules_layout.addRow("", self.tts_module_enabled_cb)
        
        self.sys_module_enabled_cb = QCheckBox("SYS 模組 ⚠️")
        modules_layout.addRow("", self.sys_module_enabled_cb)
        
        self.ui_module_enabled_cb = QCheckBox("UI 模組 ⚠️")
        modules_layout.addRow("", self.ui_module_enabled_cb)
        
        self.ani_module_enabled_cb = QCheckBox("ANI 模組 ⚠️")
        modules_layout.addRow("", self.ani_module_enabled_cb)
        
        self.mov_module_enabled_cb = QCheckBox("MOV 模組 ⚠️")
        modules_layout.addRow("", self.mov_module_enabled_cb)
        
        scroll_layout.addWidget(modules_group)
        
        # 5. 快捷鍵設定 (僅顯示)
        shortcuts_group = self._make_group("快捷鍵設定 (僅供參考)")
        shortcuts_layout = QFormLayout(shortcuts_group)
        shortcuts_layout.setSpacing(12)
        shortcuts_layout.setContentsMargins(16, 20, 16, 16)
        
        self.toggle_visibility_label = QLabel("Ctrl+Alt+U")
        shortcuts_layout.addRow("切換可見性:", self.toggle_visibility_label)
        
        self.open_settings_label = QLabel("Ctrl+Alt+S")
        shortcuts_layout.addRow("開啟設定:", self.open_settings_label)
        
        self.open_debug_label = QLabel("Ctrl+Alt+D")
        shortcuts_layout.addRow("開啟除錯:", self.open_debug_label)
        
        self.force_sleep_label = QLabel("Ctrl+Alt+Z")
        shortcuts_layout.addRow("強制睡眠:", self.force_sleep_label)
        
        self.emergency_stop_label = QLabel("Ctrl+Alt+X")
        shortcuts_layout.addRow("緊急停止:", self.emergency_stop_label)
        
        scroll_layout.addWidget(shortcuts_group)
        
        scroll_layout.addStretch()
        scroll.setWidget(scroll_content)
        layout.addWidget(scroll)
        
        self.tab_widget.addTab(widget, "監控與進階")
    
    # ============================================================================
    # 載入與保存設定
    # ============================================================================
    
    def load_settings(self):
        """從 user_settings.yaml 載入所有設定"""
        try:
            # Tab 1: 基本設定
            # 身分
            self.user_name_edit.setText(get_user_setting("general.identity.user_name", "user"))
            self.uep_name_edit.setText(get_user_setting("general.identity.uep_name", "U.E.P"))
            self.allow_identity_creation_cb.setChecked(get_user_setting("general.identity.allow_identity_creation", True))
            
            # 載入身分清單
            self._refresh_identity_list()
            
            # 系統
            lang = get_user_setting("general.system.language", "zh-TW")
            idx = self.language_combo.findText(lang)
            if idx >= 0:
                self.language_combo.setCurrentIndex(idx)
            
            self.enable_debug_mode_cb.setChecked(get_user_setting("general.system.enable_debug_mode", False))
            self.debug_level_spin.setValue(get_user_setting("general.system.debug_level", 3))
            self.enable_frontend_debug_cb.setChecked(get_user_setting("general.system.enable_frontend_debug", True))
            self.auto_save_settings_cb.setChecked(get_user_setting("general.system.auto_save_settings", True))
            self.confirm_before_exit_cb.setChecked(get_user_setting("general.system.confirm_before_exit", True))
            self.main_loop_interval_spin.setValue(get_user_setting("general.system.main_loop_interval", 0.1))
            self.shutdown_timeout_spin.setValue(get_user_setting("general.system.shutdown_timeout", 5.0))
            
            # 介面
            theme = get_user_setting("interface.appearance.theme", "auto")
            idx = self.theme_combo.findText(theme)
            if idx >= 0:
                self.theme_combo.setCurrentIndex(idx)
            
            self.ui_scale_spin.setValue(get_user_setting("interface.appearance.ui_scale", 1.0))
            
            anim_quality = get_user_setting("interface.appearance.animation_quality", "high")
            idx = self.animation_quality_combo.findText(anim_quality)
            if idx >= 0:
                self.animation_quality_combo.setCurrentIndex(idx)
            
            self.enable_effects_cb.setChecked(get_user_setting("interface.appearance.enable_effects", True))
            self.font_size_spin.setValue(get_user_setting("interface.appearance.font_size", 12))
            
            # 小工具
            self.auto_hide_cb.setChecked(get_user_setting("interface.access_widget.auto_hide", True))
            self.hide_edge_threshold_spin.setValue(get_user_setting("interface.access_widget.hide_edge_threshold", 200))
            self.animation_speed_spin.setValue(get_user_setting("interface.access_widget.animation_speed", 320))
            
            # 視窗
            self.always_on_top_cb.setChecked(get_user_setting("interface.main_window.always_on_top", True))
            self.transparency_cb.setChecked(get_user_setting("interface.main_window.transparency", True))
            self.show_hitbox_cb.setChecked(get_user_setting("interface.main_window.show_hitbox", False))
            self.show_desktop_pet_cb.setChecked(get_user_setting("interface.windows.show_desktop_pet", False))
            self.show_access_widget_cb.setChecked(get_user_setting("interface.windows.show_access_widget", True))
            self.show_debug_window_cb.setChecked(get_user_setting("interface.windows.show_debug_window", False))
            
            # Tab 2: 語音互動
            # STT
            self.stt_enabled_cb.setChecked(get_user_setting("interaction.speech_input.enabled", True))
            self.microphone_device_index_spin.setValue(get_user_setting("interaction.speech_input.microphone_device_index", 1))
            self.vad_sensitivity_spin.setValue(get_user_setting("interaction.speech_input.vad_sensitivity", 0.7))
            self.min_speech_duration_spin.setValue(get_user_setting("interaction.speech_input.min_speech_duration", 0.3))
            self.enable_continuous_mode_cb.setChecked(get_user_setting("interaction.speech_input.enable_continuous_mode", False))
            self.wake_word_confidence_spin.setValue(get_user_setting("interaction.speech_input.wake_word_confidence", 0.8))
            
            # TTS
            self.tts_enabled_cb.setChecked(get_user_setting("interaction.speech_output.enabled", True))
            self.tts_volume_slider.setValue(get_user_setting("interaction.speech_output.volume", 70))
            self.tts_speed_spin.setValue(get_user_setting("interaction.speech_output.speed", 1.0))
            
            emotion = get_user_setting("interaction.speech_output.default_emotion", "neutral")
            idx = self.default_emotion_combo.findText(emotion)
            if idx >= 0:
                self.default_emotion_combo.setCurrentIndex(idx)
            
            self.emotion_intensity_spin.setValue(get_user_setting("interaction.speech_output.emotion_intensity", 0.5))
            
            # Tab 3: 記憶與對話
            # MEM
            self.mem_enabled_cb.setChecked(get_user_setting("interaction.memory.enabled", True))
            
            # LLM
            self.user_additional_prompt_edit.setPlainText(get_user_setting("interaction.conversation.user_additional_prompt", ""))
            self.temperature_spin.setValue(get_user_setting("interaction.conversation.temperature", 0.8))
            self.enable_learning_cb.setChecked(get_user_setting("interaction.conversation.enable_learning", True))
            
            # 主動性
            self.allow_system_initiative_cb.setChecked(get_user_setting("interaction.proactivity.allow_system_initiative", True))
            self.initiative_cooldown_spin.setValue(get_user_setting("interaction.proactivity.initiative_cooldown", 300))
            self.require_user_input_cb.setChecked(get_user_setting("interaction.proactivity.require_user_input", False))
            
            # 隱私
            self.allow_usage_statistics_cb.setChecked(get_user_setting("privacy.data_collection.allow_usage_statistics", False))
            self.allow_error_reporting_cb.setChecked(get_user_setting("privacy.data_collection.allow_error_reporting", True))
            self.anonymize_data_cb.setChecked(get_user_setting("privacy.data_collection.anonymize_data", True))
            self.auto_delete_old_conversations_cb.setChecked(get_user_setting("privacy.data_retention.auto_delete_old_conversations", False))
            self.conversation_retention_days_spin.setValue(get_user_setting("privacy.data_retention.conversation_retention_days", 365))
            self.clear_cache_on_exit_cb.setChecked(get_user_setting("privacy.data_retention.clear_cache_on_exit", False))
            
            # Tab 4: 行為與移動
            # 搗蛋
            self.mischief_enabled_cb.setChecked(get_user_setting("behavior.mischief.enabled", False))
            
            intensity = get_user_setting("behavior.mischief.intensity", "medium")
            idx = self.intensity_combo.findText(intensity)
            if idx >= 0:
                self.intensity_combo.setCurrentIndex(idx)
            
            self.tease_frequency_spin.setValue(get_user_setting("behavior.mischief.tease_frequency", 0.03))
            self.easter_egg_enabled_cb.setChecked(get_user_setting("behavior.mischief.easter_egg_enabled", True))
            
            # 權限
            self.allow_file_creation_cb.setChecked(get_user_setting("behavior.permissions.allow_file_creation", True))
            self.allow_file_modification_cb.setChecked(get_user_setting("behavior.permissions.allow_file_modification", False))
            self.allow_file_deletion_cb.setChecked(get_user_setting("behavior.permissions.allow_file_deletion", False))
            self.allow_app_launch_cb.setChecked(get_user_setting("behavior.permissions.allow_app_launch", True))
            self.allow_system_commands_cb.setChecked(get_user_setting("behavior.permissions.allow_system_commands", False))
            self.require_confirmation_cb.setChecked(get_user_setting("behavior.permissions.require_confirmation", True))
            
            # 自動睡眠
            self.auto_sleep_enabled_cb.setChecked(get_user_setting("behavior.auto_sleep.enabled", True))
            self.max_idle_time_spin.setValue(get_user_setting("behavior.auto_sleep.max_idle_time", 1800))
            self.sleep_animation_edit.setText(get_user_setting("behavior.auto_sleep.sleep_animation", "sleep_l"))
            self.wake_on_interaction_cb.setChecked(get_user_setting("behavior.auto_sleep.wake_on_interaction", True))
            
            # MOV
            boundary = get_user_setting("behavior.movement.boundary_mode", "wrap")
            idx = self.boundary_mode_combo.findText(boundary)
            if idx >= 0:
                self.boundary_mode_combo.setCurrentIndex(idx)
            
            self.enable_throw_behavior_cb.setChecked(get_user_setting("behavior.movement.enable_throw_behavior", True))
            self.max_throw_speed_spin.setValue(get_user_setting("behavior.movement.max_throw_speed", 110.0))
            self.enable_cursor_tracking_cb.setChecked(get_user_setting("behavior.movement.enable_cursor_tracking", True))
            self.movement_smoothing_cb.setChecked(get_user_setting("behavior.movement.movement_smoothing", True))
            self.ground_friction_spin.setValue(get_user_setting("behavior.movement.ground_friction", 0.95))
            
            # Tab 5: 監控與進階
            # 背景工作
            self.bg_tasks_enabled_cb.setChecked(get_user_setting("monitoring.background_tasks.enabled", True))
            self.default_media_folder_edit.setText(get_user_setting("monitoring.background_tasks.default_media_folder", ""))
            self.allow_internet_access_cb.setChecked(get_user_setting("monitoring.network.allow_internet_access", True))
            self.allow_api_calls_cb.setChecked(get_user_setting("monitoring.network.allow_api_calls", True))
            self.network_timeout_spin.setValue(get_user_setting("monitoring.network.timeout", 30))
            
            # 效能
            self.max_fps_spin.setValue(get_user_setting("advanced.performance.max_fps", 60))
            self.enable_hardware_acceleration_cb.setChecked(get_user_setting("advanced.performance.enable_hardware_acceleration", True))
            self.reduce_animations_on_battery_cb.setChecked(get_user_setting("advanced.performance.reduce_animations_on_battery", True))
            self.gc_interval_spin.setValue(get_user_setting("advanced.performance.gc_interval", 300))
            
            # 日誌
            self.logging_enabled_cb.setChecked(get_user_setting("advanced.logging.enabled", True))
            
            log_level = get_user_setting("advanced.logging.log_level", "INFO")
            idx = self.log_level_combo.findText(log_level)
            if idx >= 0:
                self.log_level_combo.setCurrentIndex(idx)
            
            self.log_dir_edit.setText(get_user_setting("advanced.logging.log_dir", "logs"))
            self.enable_split_logs_cb.setChecked(get_user_setting("advanced.logging.enable_split_logs", False))
            self.enable_console_output_cb.setChecked(get_user_setting("advanced.logging.enable_console_output", False))
            self.save_logs_cb.setChecked(get_user_setting("advanced.logging.save_logs", True))
            self.max_log_size_mb_spin.setValue(get_user_setting("advanced.logging.max_log_size_mb", 50))
            self.log_rotation_days_spin.setValue(get_user_setting("advanced.logging.log_rotation_days", 7))
            
            # 模組
            self.stt_module_enabled_cb.setChecked(get_user_setting("advanced.modules.stt_enabled", True))
            self.nlp_module_enabled_cb.setChecked(get_user_setting("advanced.modules.nlp_enabled", True))
            self.mem_module_enabled_cb.setChecked(get_user_setting("advanced.modules.mem_enabled", True))
            self.llm_module_enabled_cb.setChecked(get_user_setting("advanced.modules.llm_enabled", True))
            self.tts_module_enabled_cb.setChecked(get_user_setting("advanced.modules.tts_enabled", True))
            self.sys_module_enabled_cb.setChecked(get_user_setting("advanced.modules.sys_enabled", True))
            self.ui_module_enabled_cb.setChecked(get_user_setting("advanced.modules.ui_enabled", True))
            self.ani_module_enabled_cb.setChecked(get_user_setting("advanced.modules.ani_enabled", True))
            self.mov_module_enabled_cb.setChecked(get_user_setting("advanced.modules.mov_enabled", True))
            
            info_log("[UserMainWindow] 設定載入完成")
            
        except Exception as e:
            error_log(f"[UserMainWindow] 載入設定時發生錯誤: {e}")
            import traceback
            traceback.print_exc()
    
    def save_settings(self):
        """保存所有設定到 user_settings.yaml"""
        try:
            # Tab 1: 基本設定
            set_user_setting("general.identity.user_name", self.user_name_edit.text())
            set_user_setting("general.identity.uep_name", self.uep_name_edit.text())
            set_user_setting("general.identity.allow_identity_creation", self.allow_identity_creation_cb.isChecked())
            
            set_user_setting("general.system.language", self.language_combo.currentText())
            set_user_setting("general.system.enable_debug_mode", self.enable_debug_mode_cb.isChecked())
            set_user_setting("general.system.debug_level", self.debug_level_spin.value())
            set_user_setting("general.system.enable_frontend_debug", self.enable_frontend_debug_cb.isChecked())
            set_user_setting("general.system.auto_save_settings", self.auto_save_settings_cb.isChecked())
            set_user_setting("general.system.confirm_before_exit", self.confirm_before_exit_cb.isChecked())
            set_user_setting("general.system.main_loop_interval", self.main_loop_interval_spin.value())
            set_user_setting("general.system.shutdown_timeout", self.shutdown_timeout_spin.value())
            
            set_user_setting("interface.appearance.theme", self.theme_combo.currentText())
            set_user_setting("interface.appearance.ui_scale", self.ui_scale_spin.value())
            set_user_setting("interface.appearance.animation_quality", self.animation_quality_combo.currentText())
            set_user_setting("interface.appearance.enable_effects", self.enable_effects_cb.isChecked())
            set_user_setting("interface.appearance.font_size", self.font_size_spin.value())
            
            set_user_setting("interface.access_widget.auto_hide", self.auto_hide_cb.isChecked())
            set_user_setting("interface.access_widget.hide_edge_threshold", self.hide_edge_threshold_spin.value())
            set_user_setting("interface.access_widget.animation_speed", self.animation_speed_spin.value())
            
            set_user_setting("interface.main_window.always_on_top", self.always_on_top_cb.isChecked())
            set_user_setting("interface.main_window.transparency", self.transparency_cb.isChecked())
            set_user_setting("interface.main_window.show_hitbox", self.show_hitbox_cb.isChecked())
            set_user_setting("interface.windows.show_desktop_pet", self.show_desktop_pet_cb.isChecked())
            set_user_setting("interface.windows.show_access_widget", self.show_access_widget_cb.isChecked())
            set_user_setting("interface.windows.show_debug_window", self.show_debug_window_cb.isChecked())
            
            # Tab 2: 語音互動
            set_user_setting("interaction.speech_input.enabled", self.stt_enabled_cb.isChecked())
            set_user_setting("interaction.speech_input.microphone_device_index", self.microphone_device_index_spin.value())
            set_user_setting("interaction.speech_input.vad_sensitivity", self.vad_sensitivity_spin.value())
            set_user_setting("interaction.speech_input.min_speech_duration", self.min_speech_duration_spin.value())
            set_user_setting("interaction.speech_input.enable_continuous_mode", self.enable_continuous_mode_cb.isChecked())
            set_user_setting("interaction.speech_input.wake_word_confidence", self.wake_word_confidence_spin.value())
            
            set_user_setting("interaction.speech_output.enabled", self.tts_enabled_cb.isChecked())
            set_user_setting("interaction.speech_output.volume", self.tts_volume_slider.value())
            set_user_setting("interaction.speech_output.speed", self.tts_speed_spin.value())
            set_user_setting("interaction.speech_output.default_emotion", self.default_emotion_combo.currentText())
            set_user_setting("interaction.speech_output.emotion_intensity", self.emotion_intensity_spin.value())
            
            # Tab 3: 記憶與對話
            set_user_setting("interaction.memory.enabled", self.mem_enabled_cb.isChecked())
            
            set_user_setting("interaction.conversation.user_additional_prompt", self.user_additional_prompt_edit.toPlainText()[:200])
            set_user_setting("interaction.conversation.temperature", self.temperature_spin.value())
            set_user_setting("interaction.conversation.enable_learning", self.enable_learning_cb.isChecked())
            
            set_user_setting("interaction.proactivity.allow_system_initiative", self.allow_system_initiative_cb.isChecked())
            set_user_setting("interaction.proactivity.initiative_cooldown", self.initiative_cooldown_spin.value())
            set_user_setting("interaction.proactivity.require_user_input", self.require_user_input_cb.isChecked())
            
            set_user_setting("privacy.data_collection.allow_usage_statistics", self.allow_usage_statistics_cb.isChecked())
            set_user_setting("privacy.data_collection.allow_error_reporting", self.allow_error_reporting_cb.isChecked())
            set_user_setting("privacy.data_collection.anonymize_data", self.anonymize_data_cb.isChecked())
            set_user_setting("privacy.data_retention.auto_delete_old_conversations", self.auto_delete_old_conversations_cb.isChecked())
            set_user_setting("privacy.data_retention.conversation_retention_days", self.conversation_retention_days_spin.value())
            set_user_setting("privacy.data_retention.clear_cache_on_exit", self.clear_cache_on_exit_cb.isChecked())
            
            # Tab 4: 行為與移動
            set_user_setting("behavior.mischief.enabled", self.mischief_enabled_cb.isChecked())
            set_user_setting("behavior.mischief.intensity", self.intensity_combo.currentText())
            set_user_setting("behavior.mischief.tease_frequency", self.tease_frequency_spin.value())
            set_user_setting("behavior.mischief.easter_egg_enabled", self.easter_egg_enabled_cb.isChecked())
            
            set_user_setting("behavior.permissions.allow_file_creation", self.allow_file_creation_cb.isChecked())
            set_user_setting("behavior.permissions.allow_file_modification", self.allow_file_modification_cb.isChecked())
            set_user_setting("behavior.permissions.allow_file_deletion", self.allow_file_deletion_cb.isChecked())
            set_user_setting("behavior.permissions.allow_app_launch", self.allow_app_launch_cb.isChecked())
            set_user_setting("behavior.permissions.allow_system_commands", self.allow_system_commands_cb.isChecked())
            set_user_setting("behavior.permissions.require_confirmation", self.require_confirmation_cb.isChecked())
            
            set_user_setting("behavior.auto_sleep.enabled", self.auto_sleep_enabled_cb.isChecked())
            set_user_setting("behavior.auto_sleep.max_idle_time", self.max_idle_time_spin.value())
            set_user_setting("behavior.auto_sleep.sleep_animation", self.sleep_animation_edit.text())
            set_user_setting("behavior.auto_sleep.wake_on_interaction", self.wake_on_interaction_cb.isChecked())
            
            set_user_setting("behavior.movement.boundary_mode", self.boundary_mode_combo.currentText())
            set_user_setting("behavior.movement.enable_throw_behavior", self.enable_throw_behavior_cb.isChecked())
            set_user_setting("behavior.movement.max_throw_speed", self.max_throw_speed_spin.value())
            set_user_setting("behavior.movement.enable_cursor_tracking", self.enable_cursor_tracking_cb.isChecked())
            set_user_setting("behavior.movement.movement_smoothing", self.movement_smoothing_cb.isChecked())
            set_user_setting("behavior.movement.ground_friction", self.ground_friction_spin.value())
            
            # Tab 5: 監控與進階
            set_user_setting("monitoring.background_tasks.enabled", self.bg_tasks_enabled_cb.isChecked())
            set_user_setting("monitoring.background_tasks.default_media_folder", self.default_media_folder_edit.text())
            set_user_setting("monitoring.network.allow_internet_access", self.allow_internet_access_cb.isChecked())
            set_user_setting("monitoring.network.allow_api_calls", self.allow_api_calls_cb.isChecked())
            set_user_setting("monitoring.network.timeout", self.network_timeout_spin.value())
            
            set_user_setting("advanced.performance.max_fps", self.max_fps_spin.value())
            set_user_setting("advanced.performance.enable_hardware_acceleration", self.enable_hardware_acceleration_cb.isChecked())
            set_user_setting("advanced.performance.reduce_animations_on_battery", self.reduce_animations_on_battery_cb.isChecked())
            set_user_setting("advanced.performance.gc_interval", self.gc_interval_spin.value())
            
            set_user_setting("advanced.logging.enabled", self.logging_enabled_cb.isChecked())
            set_user_setting("advanced.logging.log_level", self.log_level_combo.currentText())
            set_user_setting("advanced.logging.log_dir", self.log_dir_edit.text())
            set_user_setting("advanced.logging.enable_split_logs", self.enable_split_logs_cb.isChecked())
            set_user_setting("advanced.logging.enable_console_output", self.enable_console_output_cb.isChecked())
            set_user_setting("advanced.logging.save_logs", self.save_logs_cb.isChecked())
            set_user_setting("advanced.logging.max_log_size_mb", self.max_log_size_mb_spin.value())
            set_user_setting("advanced.logging.log_rotation_days", self.log_rotation_days_spin.value())
            
            set_user_setting("advanced.modules.stt_enabled", self.stt_module_enabled_cb.isChecked())
            set_user_setting("advanced.modules.nlp_enabled", self.nlp_module_enabled_cb.isChecked())
            set_user_setting("advanced.modules.mem_enabled", self.mem_module_enabled_cb.isChecked())
            set_user_setting("advanced.modules.llm_enabled", self.llm_module_enabled_cb.isChecked())
            set_user_setting("advanced.modules.tts_enabled", self.tts_module_enabled_cb.isChecked())
            set_user_setting("advanced.modules.sys_enabled", self.sys_module_enabled_cb.isChecked())
            set_user_setting("advanced.modules.ui_enabled", self.ui_module_enabled_cb.isChecked())
            set_user_setting("advanced.modules.ani_enabled", self.ani_module_enabled_cb.isChecked())
            set_user_setting("advanced.modules.mov_enabled", self.mov_module_enabled_cb.isChecked())
            
            info_log("[UserMainWindow] 設定保存完成")
            
        except Exception as e:
            error_log(f"[UserMainWindow] 保存設定時發生錯誤: {e}")
            import traceback
            traceback.print_exc()
    
    # ============================================================================
    # 按鈕事件處理
    # ============================================================================
    
    def toggle_theme(self):
        """切換主題"""
        if theme_manager:
            theme_manager.toggle()
    
    def _on_theme_changed(self, theme_str: str):
        """主題變更回調"""
        if theme_manager and hasattr(self, 'theme_toggle'):
            is_dark = (theme_str == Theme.DARK.value)
            self.theme_toggle.setText("☀️" if is_dark else "🌙")
    
    def apply_settings(self):
        """套用設定"""
        self.save_settings()
        self.settings_changed.emit("applied", None)
        if hasattr(self, 'statusBar'):
            self.statusBar().showMessage("設定已套用", 3000)
        info_log("[UserMainWindow] 設定已套用")
    
    def ok_clicked(self):
        """確定按鈕"""
        self.apply_settings()
        self.close()
    
    def cancel_clicked(self):
        """取消按鈕"""
        self.load_settings()
        self.close()
    
    # ============================================================================
    # 身分管理功能
    # ============================================================================
    
    def _refresh_identity_list(self):
        """刷新身分清單"""
        try:
            from modules.nlp_module.identity_manager import IdentityManager
            from pathlib import Path
            
            # 獲取 IdentityManager 實例
            identity_storage_path = Path("memory") / "identities"
            identity_manager = IdentityManager(storage_path=str(identity_storage_path))
            
            # 清空列表
            self.identity_list_widget.clear()
            
            # 獲取當前身分 ID
            current_id = get_user_setting("general.identity.current_identity_id", None)
            
            # 載入所有身分
            identities = identity_manager.identities
            if not identities:
                item = QListWidgetItem("（尚無身分）")
                item.setData(Qt.UserRole, None)
                self.identity_list_widget.addItem(item)
                debug_log(2, "[UserMainWindow] 身分清單為空")
                return
            
            # 添加身分到列表
            for identity_id, profile in identities.items():
                # 顯示格式：「名稱 (ID) [樣本: X]」
                sample_count = profile.speaker_accumulation.total_samples if profile.speaker_accumulation else 0
                display_text = f"{profile.display_name} ({identity_id[:8]}...) [樣本: {sample_count}]"
                
                if identity_id == current_id:
                    display_text = f"✓ {display_text}"  # 標記當前身分
                
                item = QListWidgetItem(display_text)
                item.setData(Qt.UserRole, identity_id)  # 儲存完整 ID
                self.identity_list_widget.addItem(item)
            
            info_log(f"[UserMainWindow] 已載入 {len(identities)} 個身分")
            
        except Exception as e:
            error_log(f"[UserMainWindow] 刷新身分清單失敗: {e}")
            import traceback
            traceback.print_exc()
    
    def _on_identity_double_clicked(self, item):
        """雙擊身分項目時切換身分"""
        self._switch_identity()
    
    def _switch_identity(self):
        """切換到選中的身分"""
        try:
            current_item = self.identity_list_widget.currentItem()
            if not current_item:
                QMessageBox.warning(self, "提示", "請先選擇要切換的身分")
                return
            
            identity_id = current_item.data(Qt.UserRole)
            if not identity_id:
                QMessageBox.warning(self, "提示", "無效的身分")
                return
            
            # 更新 user_settings.yaml
            set_user_setting("general.identity.current_identity_id", identity_id)
            
            # 設置到 Working Context
            from core.working_context import working_context_manager
            working_context_manager.set_declared_identity(identity_id)
            
            # 同步到 StatusManager
            from core.status_manager import status_manager
            status_manager.switch_identity(identity_id)
            
            # 刷新列表顯示
            self._refresh_identity_list()
            
            QMessageBox.information(self, "成功", f"已切換到身分: {identity_id[:16]}...")
            info_log(f"[UserMainWindow] 已切換到身分: {identity_id}")
            
        except Exception as e:
            error_log(f"[UserMainWindow] 切換身分失敗: {e}")
            QMessageBox.critical(self, "錯誤", f"切換身分失敗: {e}")
            import traceback
            traceback.print_exc()
    
    def _create_identity(self):
        """創建新身分"""
        try:
            # 檢查是否允許創建
            if not get_user_setting("general.identity.allow_identity_creation", True):
                QMessageBox.warning(self, "提示", "目前設定不允許創建新身分")
                return
            
            # 顯示輸入對話框
            from PyQt5.QtWidgets import QInputDialog
            display_name, ok = QInputDialog.getText(
                self, "新增身分", "請輸入身分名稱:", 
                QLineEdit.Normal, ""
            )
            
            if not ok or not display_name.strip():
                return
            
            display_name = display_name.strip()
            
            # 創建新身分
            from modules.nlp_module.identity_manager import IdentityManager
            from pathlib import Path
            
            identity_storage_path = Path("memory") / "identities"
            identity_manager = IdentityManager(storage_path=str(identity_storage_path))
            
            # 使用 create_identity 方法（speaker_id 使用隨機值）
            import uuid
            speaker_id = f"manual_created_{uuid.uuid4().hex[:8]}"
            new_identity = identity_manager.create_identity(
                speaker_id=speaker_id,
                display_name=display_name,
                force_new=True
            )
            
            # 刷新列表
            self._refresh_identity_list()
            
            QMessageBox.information(self, "成功", f"已創建新身分: {display_name}\nID: {new_identity.identity_id[:16]}...")
            info_log(f"[UserMainWindow] 已創建新身分: {display_name} ({new_identity.identity_id})")
            
        except Exception as e:
            error_log(f"[UserMainWindow] 創建身分失敗: {e}")
            QMessageBox.critical(self, "錯誤", f"創建身分失敗: {e}")
            import traceback
            traceback.print_exc()
    
    def _delete_identity(self):
        """刪除選中的身分"""
        try:
            current_item = self.identity_list_widget.currentItem()
            if not current_item:
                QMessageBox.warning(self, "提示", "請先選擇要刪除的身分")
                return
            
            identity_id = current_item.data(Qt.UserRole)
            if not identity_id:
                QMessageBox.warning(self, "提示", "無效的身分")
                return
            
            # 檢查是否為當前身分
            current_id = get_user_setting("general.identity.current_identity_id", None)
            if identity_id == current_id:
                QMessageBox.warning(self, "提示", "無法刪除當前正在使用的身分")
                return
            
            # 確認刪除
            reply = QMessageBox.question(
                self, "確認刪除", 
                f"確定要刪除身分 {identity_id[:16]}... 嗎？\n此操作無法撤銷！",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No
            )
            
            if reply != QMessageBox.Yes:
                return
            
            # 刪除身分檔案
            from pathlib import Path
            identity_file = Path("memory") / "identities" / f"{identity_id}.json"
            if identity_file.exists():
                identity_file.unlink()
                info_log(f"[UserMainWindow] 已刪除身分檔案: {identity_file}")
            
            # 刷新列表
            self._refresh_identity_list()
            
            QMessageBox.information(self, "成功", f"已刪除身分: {identity_id[:16]}...")
            
        except Exception as e:
            error_log(f"[UserMainWindow] 刪除身分失敗: {e}")
            QMessageBox.critical(self, "錯誤", f"刪除身分失敗: {e}")
            import traceback
            traceback.print_exc()
    
    def closeEvent(self, event):
        """視窗關閉事件"""
        self.window_closed.emit()
        event.accept()
    
    def minimize_to_orb(self):
        """最小化到圓球"""
        self.is_minimized_to_orb = True
        self.original_geometry = self.geometry()
        self.hide()
        debug_log(OPERATION_LEVEL, "[UserMainWindow] 已最小化到圓球")
    
    def restore_from_orb(self):
        """從圓球還原"""
        if self.is_minimized_to_orb and self.original_geometry:
            self.setGeometry(self.original_geometry)
            self.is_minimized_to_orb = False
        self.show()
        self.raise_()
        self.activateWindow()
        debug_log(OPERATION_LEVEL, "[UserMainWindow] 已從圓球還原")


# ============================================================================
# 測試程式
# ============================================================================

if __name__ == "__main__":
    if not PYQT5_AVAILABLE:
        print("PyQt5 不可用，無法執行測試")
        sys.exit(1)
    
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    
    if theme_manager:
        theme_manager.apply_app()
    
    window = UserMainWindow()
    window.show()
    
    sys.exit(app.exec_())
