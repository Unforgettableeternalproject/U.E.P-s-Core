# user/user_main_window.py
"""
User Main Settings Window

UEP 主要設定視窗
提供完整的系統設定和控制功能

功能分頁：
1. 個人 - UEP 系統資訊和基本設定
2. 表現 - TTS、字幕等表現形式設定
3. 行為模式 - 系統狀態和 MOV 相關限制
4. 互動 - 使用者互動細節設置
5. 其他 - 其他功能和進階設定
"""

import os
import sys
from typing import Dict, Any, Optional

try:
    from PyQt5.QtWidgets import (QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, 
                                QTabWidget, QLabel, QGroupBox, QScrollArea,
                                QFrame, QPushButton, QCheckBox, QSpinBox,
                                QSlider, QComboBox, QLineEdit, QTextEdit,
                                QSplitter, QTreeWidget, QTreeWidgetItem,
                                QFormLayout, QGridLayout, QSizePolicy,
                                QApplication, QMessageBox, QFileDialog,
                                QProgressBar, QStatusBar, QMenuBar,
                                QToolBar, QAction, QButtonGroup)
    from PyQt5.QtCore import (Qt, QTimer, pyqtSignal, QSize, QRect, 
                             QPropertyAnimation, QEasingCurve, QThread,
                             QSettings, QStandardPaths)
    from PyQt5.QtGui import (QIcon, QFont, QPixmap, QPalette, QColor, 
                            QPainter, QLinearGradient, QBrush)
    PYQT5_AVAILABLE = True
except ImportError:
    # 降級處理
    QMainWindow = object
    QWidget = object
    QVBoxLayout = object
    QHBoxLayout = object
    QTabWidget = object
    QLabel = object
    QGroupBox = object
    QScrollArea = object
    QFrame = object
    QPushButton = object
    QCheckBox = object
    QSpinBox = object
    QSlider = object
    QComboBox = object
    QLineEdit = object
    QTextEdit = object
    QSplitter = object
    QTreeWidget = object
    QTreeWidgetItem = object
    QFormLayout = object
    QGridLayout = object
    QSizePolicy = object
    QApplication = None
    QMessageBox = object
    QFileDialog = object
    QProgressBar = object
    QStatusBar = object
    QMenuBar = object
    QToolBar = object
    QAction = object
    QButtonGroup = object
    Qt = None
    QTimer = None
    pyqtSignal = None
    QSize = None
    QRect = None
    QPropertyAnimation = None
    QEasingCurve = None
    QThread = None
    QSettings = None
    QStandardPaths = None
    QIcon = None
    QFont = None
    QPixmap = None
    QPalette = None
    QColor = None
    QPainter = None
    QLinearGradient = None
    QBrush = None
    PYQT5_AVAILABLE = False

# 嘗試導入調試和日誌模組
from utils.debug_helper import debug_log, info_log, error_log, KEY_LEVEL, OPERATION_LEVEL, SYSTEM_LEVEL, ELABORATIVE_LEVEL

class UserMainWindow(QMainWindow):
    """
    UEP 主要設定視窗
    
    提供五個主要分頁的設定功能：
    - 個人設定
    - 表現設定  
    - 行為模式設定
    - 互動設定
    - 其他設定
    """
    
    # 定義信號
    settings_changed = pyqtSignal(str, object)  # 設定值變更信號
    action_triggered = pyqtSignal(str, dict)   # 動作觸發信號
    window_closed = pyqtSignal()               # 視窗關閉信號
    
    def __init__(self, ui_module=None):
        super().__init__()
        
        if not PYQT5_AVAILABLE:
            error_log("[UserMainWindow] PyQt5 不可用，使用降級模式")
            return
            
        self.ui_module = ui_module
        self.settings = QSettings("UEP", "Core")
        
        # 視窗屬性
        self.is_minimized_to_orb = False
        self.original_geometry = None
        
        # 初始化界面
        self.init_ui()
        self.load_settings()
        
        # 預設隱藏設定視窗，只有透過子球體才顯示
        self.hide()
        
        info_log("[UserMainWindow] 設定視窗初始化完成")
    
    def init_ui(self):
        """初始化使用者介面"""
        self.setWindowTitle("UEP 設定")
        self.setMinimumSize(800, 600)
        self.resize(1000, 700)
        
        debug_log(SYSTEM_LEVEL, "[UserMainWindow] 開始初始化使用者介面")
        
        # 設定視窗圖標（如果有的話）
        try:
            icon_path = os.path.join(os.path.dirname(__file__), "../../../arts/U.E.P.png")
            if os.path.exists(icon_path):
                self.setWindowIcon(QIcon(icon_path))
        except Exception as e:
            error_log(f"[UserMainWindow] 無法載入圖標: {e}")
        
        # 創建中央小工具
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        
        # 創建主佈局
        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(10, 10, 10, 10)
        
        # 創建標題區域
        self.create_title_area(main_layout)
        
        # 創建分頁控件
        self.create_tab_widget(main_layout)
        
        # 創建底部按鈕區域
        self.create_bottom_buttons(main_layout)
        
        # 創建狀態列
        self.create_status_bar()
        
        # 應用樣式
        self.apply_styles()
        
        debug_log(SYSTEM_LEVEL, "[UserMainWindow] 使用者介面初始化完成")
    
    def create_title_area(self, parent_layout):
        """創建標題區域"""
        title_frame = QFrame()
        title_frame.setFrameStyle(QFrame.NoFrame)
        title_frame.setMaximumHeight(80)
        
        title_layout = QHBoxLayout(title_frame)
        
        # UEP 標題
        title_label = QLabel("UEP 設定控制台")
        title_font = QFont("Arial", 18, QFont.Bold)
        title_label.setFont(title_font)
        title_label.setStyleSheet("color: #2c3e50; margin: 10px;")
        
        # 版本信息
        version_label = QLabel("v1.0.0")
        version_label.setStyleSheet("color: #7f8c8d; font-size: 12px; margin: 10px;")
        version_label.setAlignment(Qt.AlignRight | Qt.AlignBottom)
        
        title_layout.addWidget(title_label)
        title_layout.addStretch()
        title_layout.addWidget(version_label)
        
        parent_layout.addWidget(title_frame)
    
    def create_tab_widget(self, parent_layout):
        """創建分頁控件"""
        self.tab_widget = QTabWidget()
        self.tab_widget.setTabPosition(QTabWidget.North)
        
        # 創建各個分頁
        self.create_personal_tab()
        self.create_performance_tab()  
        self.create_behavior_tab()
        self.create_interaction_tab()
        self.create_other_tab()
        
        parent_layout.addWidget(self.tab_widget)
    
    def create_personal_tab(self):
        """創建個人設定分頁"""
        personal_widget = QWidget()
        personal_layout = QVBoxLayout(personal_widget)
        
        # 創建滾動區域
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_content = QWidget()
        scroll_layout = QVBoxLayout(scroll_content)
        
        # UEP 系統資訊群組
        system_info_group = self.create_system_info_group()
        scroll_layout.addWidget(system_info_group)
        
        # 個人偏好設定群組  
        personal_prefs_group = self.create_personal_preferences_group()
        scroll_layout.addWidget(personal_prefs_group)
        
        # 帳戶設定群組
        account_group = self.create_account_settings_group()
        scroll_layout.addWidget(account_group)
        
        scroll_layout.addStretch()
        scroll_area.setWidget(scroll_content)
        personal_layout.addWidget(scroll_area)
        
        self.tab_widget.addTab(personal_widget, "個人")
    
    def create_performance_tab(self):
        """創建表現設定分頁"""
        performance_widget = QWidget()
        performance_layout = QVBoxLayout(performance_widget)
        
        # 創建滾動區域
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_content = QWidget()
        scroll_layout = QVBoxLayout(scroll_content)
        
        # TTS 設定群組
        tts_group = self.create_tts_settings_group()
        scroll_layout.addWidget(tts_group)
        
        # 字幕設定群組
        subtitle_group = self.create_subtitle_settings_group()
        scroll_layout.addWidget(subtitle_group)
        
        # 動畫設定群組
        animation_group = self.create_animation_settings_group()
        scroll_layout.addWidget(animation_group)
        
        # 視覺效果設定群組
        visual_group = self.create_visual_effects_group()
        scroll_layout.addWidget(visual_group)
        
        scroll_layout.addStretch()
        scroll_area.setWidget(scroll_content)
        performance_layout.addWidget(scroll_area)
        
        self.tab_widget.addTab(performance_widget, "表現")
    
    def create_behavior_tab(self):
        """創建行為模式設定分頁"""
        behavior_widget = QWidget()
        behavior_layout = QVBoxLayout(behavior_widget)
        
        # 創建滾動區域
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_content = QWidget()
        scroll_layout = QVBoxLayout(scroll_content)
        
        # 系統狀態控制群組
        system_states_group = self.create_system_states_group()
        scroll_layout.addWidget(system_states_group)
        
        # MOV 行為限制群組
        mov_limits_group = self.create_mov_limits_group()
        scroll_layout.addWidget(mov_limits_group)
        
        # 自動行為設定群組
        auto_behavior_group = self.create_auto_behavior_group()
        scroll_layout.addWidget(auto_behavior_group)
        
        scroll_layout.addStretch()
        scroll_area.setWidget(scroll_content)
        behavior_layout.addWidget(scroll_area)
        
        self.tab_widget.addTab(behavior_widget, "行為模式")
    
    def create_interaction_tab(self):
        """創建互動設定分頁"""
        interaction_widget = QWidget()
        interaction_layout = QVBoxLayout(interaction_widget)
        
        # 創建滾動區域
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_content = QWidget()
        scroll_layout = QVBoxLayout(scroll_content)
        
        # 滑鼠互動設定群組
        mouse_group = self.create_mouse_interaction_group()
        scroll_layout.addWidget(mouse_group)
        
        # 鍵盤快捷鍵群組
        keyboard_group = self.create_keyboard_shortcuts_group()
        scroll_layout.addWidget(keyboard_group)
        
        # 檔案拖放設定群組
        drag_drop_group = self.create_drag_drop_group()
        scroll_layout.addWidget(drag_drop_group)
        
        # 通知設定群組
        notification_group = self.create_notification_group()
        scroll_layout.addWidget(notification_group)
        
        scroll_layout.addStretch()
        scroll_area.setWidget(scroll_content)
        interaction_layout.addWidget(scroll_area)
        
        self.tab_widget.addTab(interaction_widget, "互動")
    
    def create_other_tab(self):
        """創建其他設定分頁"""
        other_widget = QWidget()
        other_layout = QVBoxLayout(other_widget)
        
        # 創建滾動區域
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_content = QWidget()
        scroll_layout = QVBoxLayout(scroll_content)
        
        # 進階設定群組
        advanced_group = self.create_advanced_settings_group()
        scroll_layout.addWidget(advanced_group)
        
        # 資料與隱私群組
        data_privacy_group = self.create_data_privacy_group()
        scroll_layout.addWidget(data_privacy_group)
        
        # 系統維護群組
        maintenance_group = self.create_maintenance_group()
        scroll_layout.addWidget(maintenance_group)
        
        # 關於群組
        about_group = self.create_about_group()
        scroll_layout.addWidget(about_group)
        
        scroll_layout.addStretch()
        scroll_area.setWidget(scroll_content)
        other_layout.addWidget(scroll_area)
        
        self.tab_widget.addTab(other_widget, "其他")
    
    def create_system_info_group(self):
        """創建系統資訊群組"""
        group = QGroupBox("UEP 系統資訊")
        layout = QFormLayout(group)
        
        # 系統狀態
        self.system_status_label = QLabel("正常運行")
        self.system_status_label.setStyleSheet("color: green; font-weight: bold;")
        layout.addRow("系統狀態:", self.system_status_label)
        
        # 運行時間
        self.uptime_label = QLabel("00:00:00")
        layout.addRow("運行時間:", self.uptime_label)
        
        # 記憶體使用
        self.memory_label = QLabel("0 MB")
        layout.addRow("記憶體使用:", self.memory_label)
        
        # CPU 使用率
        self.cpu_label = QLabel("0%")
        layout.addRow("CPU 使用率:", self.cpu_label)
        
        # 活躍模組
        self.active_modules_label = QLabel("正在載入...")
        layout.addRow("活躍模組:", self.active_modules_label)
        
        return group
    
    def create_personal_preferences_group(self):
        """創建個人偏好設定群組"""
        group = QGroupBox("個人偏好")
        layout = QFormLayout(group)
        
        # UEP 名稱
        self.uep_name_edit = QLineEdit()
        self.uep_name_edit.setPlaceholderText("UEP")
        layout.addRow("UEP 名稱:", self.uep_name_edit)
        
        # 使用者名稱
        self.user_name_edit = QLineEdit()
        self.user_name_edit.setPlaceholderText("使用者")
        layout.addRow("使用者名稱:", self.user_name_edit)
        
        # 語言設定
        self.language_combo = QComboBox()
        self.language_combo.addItems(["繁體中文", "簡體中文", "English", "日本語"])
        layout.addRow("語言:", self.language_combo)
        
        # 主題設定
        self.theme_combo = QComboBox()
        self.theme_combo.addItems(["預設主題", "深色主題", "淺色主題", "自訂主題"])
        layout.addRow("主題:", self.theme_combo)
        
        return group
    
    def create_account_settings_group(self):
        """創建帳戶設定群組"""
        group = QGroupBox("帳戶設定")
        layout = QVBoxLayout(group)
        
        # 說明文字
        info_label = QLabel("這裡可以管理 UEP 的帳戶相關設定")
        info_label.setStyleSheet("color: #7f8c8d; font-style: italic;")
        layout.addWidget(info_label)
        
        # 按鈕區域
        button_layout = QHBoxLayout()
        
        self.login_button = QPushButton("登入帳戶")
        self.logout_button = QPushButton("登出")
        self.logout_button.setEnabled(False)
        
        button_layout.addWidget(self.login_button)
        button_layout.addWidget(self.logout_button)
        button_layout.addStretch()
        
        layout.addLayout(button_layout)
        
        return group
    
    def create_tts_settings_group(self):
        """創建 TTS 設定群組"""
        group = QGroupBox("語音合成 (TTS)")
        layout = QFormLayout(group)
        
        # 啟用 TTS
        self.enable_tts_checkbox = QCheckBox("啟用語音合成")
        self.enable_tts_checkbox.setChecked(True)
        layout.addRow(self.enable_tts_checkbox)
        
        # 音量設定
        self.tts_volume_slider = QSlider(Qt.Horizontal)
        self.tts_volume_slider.setRange(0, 100)
        self.tts_volume_slider.setValue(70)
        self.tts_volume_label = QLabel("70%")
        
        volume_layout = QHBoxLayout()
        volume_layout.addWidget(self.tts_volume_slider)
        volume_layout.addWidget(self.tts_volume_label)
        layout.addRow("音量:", volume_layout)
        
        # 語音速度
        self.tts_speed_slider = QSlider(Qt.Horizontal)
        self.tts_speed_slider.setRange(50, 200)
        self.tts_speed_slider.setValue(100)
        self.tts_speed_label = QLabel("100%")
        
        speed_layout = QHBoxLayout()
        speed_layout.addWidget(self.tts_speed_slider)
        speed_layout.addWidget(self.tts_speed_label)
        layout.addRow("語速:", speed_layout)
        
        # 語音選擇
        self.voice_combo = QComboBox()
        self.voice_combo.addItems(["預設語音", "女聲A", "女聲B", "男聲A", "男聲B"])
        layout.addRow("語音:", self.voice_combo)
        
        # 連接信號
        self.tts_volume_slider.valueChanged.connect(
            lambda v: self.tts_volume_label.setText(f"{v}%")
        )
        self.tts_speed_slider.valueChanged.connect(
            lambda v: self.tts_speed_label.setText(f"{v}%")
        )
        
        return group
    
    def create_subtitle_settings_group(self):
        """創建字幕設定群組"""
        group = QGroupBox("字幕顯示")
        layout = QFormLayout(group)
        
        # 啟用字幕
        self.enable_subtitle_checkbox = QCheckBox("顯示字幕")
        self.enable_subtitle_checkbox.setChecked(True)
        layout.addRow(self.enable_subtitle_checkbox)
        
        # 字幕位置
        self.subtitle_position_combo = QComboBox()
        self.subtitle_position_combo.addItems(["頂部", "中央", "底部", "跟隨UEP"])
        layout.addRow("字幕位置:", self.subtitle_position_combo)
        
        # 字幕大小
        self.subtitle_size_spinbox = QSpinBox()
        self.subtitle_size_spinbox.setRange(8, 72)
        self.subtitle_size_spinbox.setValue(14)
        layout.addRow("字體大小:", self.subtitle_size_spinbox)
        
        # 字幕透明度
        self.subtitle_opacity_slider = QSlider(Qt.Horizontal)
        self.subtitle_opacity_slider.setRange(10, 100)
        self.subtitle_opacity_slider.setValue(90)
        self.subtitle_opacity_label = QLabel("90%")
        
        opacity_layout = QHBoxLayout()
        opacity_layout.addWidget(self.subtitle_opacity_slider)
        opacity_layout.addWidget(self.subtitle_opacity_label)
        layout.addRow("透明度:", opacity_layout)
        
        # 連接信號
        self.subtitle_opacity_slider.valueChanged.connect(
            lambda v: self.subtitle_opacity_label.setText(f"{v}%")
        )
        
        return group
    
    def create_animation_settings_group(self):
        """創建動畫設定群組"""
        group = QGroupBox("動畫設定")
        layout = QFormLayout(group)
        
        # 啟用動畫
        self.enable_animation_checkbox = QCheckBox("啟用動畫效果")
        self.enable_animation_checkbox.setChecked(True)
        layout.addRow(self.enable_animation_checkbox)
        
        # 動畫品質
        self.animation_quality_combo = QComboBox()
        self.animation_quality_combo.addItems(["低", "中", "高", "極高"])
        self.animation_quality_combo.setCurrentText("高")
        layout.addRow("動畫品質:", self.animation_quality_combo)
        
        # 動畫速度
        self.animation_speed_slider = QSlider(Qt.Horizontal)
        self.animation_speed_slider.setRange(50, 200)
        self.animation_speed_slider.setValue(100)
        self.animation_speed_label = QLabel("100%")
        
        speed_layout = QHBoxLayout()
        speed_layout.addWidget(self.animation_speed_slider)
        speed_layout.addWidget(self.animation_speed_label)
        layout.addRow("動畫速度:", speed_layout)
        
        # 連接信號
        self.animation_speed_slider.valueChanged.connect(
            lambda v: self.animation_speed_label.setText(f"{v}%")
        )
        
        return group
    
    def create_visual_effects_group(self):
        """創建視覺效果群組"""
        group = QGroupBox("視覺效果")
        layout = QFormLayout(group)
        
        # 陰影效果
        self.shadow_checkbox = QCheckBox("啟用陰影效果")
        self.shadow_checkbox.setChecked(True)
        layout.addRow(self.shadow_checkbox)
        
        # 半透明效果
        self.transparency_checkbox = QCheckBox("啟用半透明效果")
        self.transparency_checkbox.setChecked(True)
        layout.addRow(self.transparency_checkbox)
        
        # 粒子效果
        self.particle_checkbox = QCheckBox("啟用粒子效果")
        self.particle_checkbox.setChecked(False)
        layout.addRow(self.particle_checkbox)
        
        return group
    
    def create_system_states_group(self):
        """創建系統狀態控制群組"""
        group = QGroupBox("系統狀態控制")
        layout = QVBoxLayout(group)
        
        # 創建狀態樹
        self.state_tree = QTreeWidget()
        self.state_tree.setHeaderLabels(["模組/狀態", "狀態", "操作"])
        
        # 添加模組狀態項目
        modules = ["STT模組", "NLP模組", "MEM模組", "LLM模組", "TTS模組", "SYS模組"]
        for module in modules:
            item = QTreeWidgetItem([module, "啟用", ""])
            self.state_tree.addTopLevelItem(item)
        
        layout.addWidget(self.state_tree)
        
        # 操作按鈕
        button_layout = QHBoxLayout()
        self.enable_all_button = QPushButton("全部啟用")
        self.disable_all_button = QPushButton("全部停用")
        self.reset_states_button = QPushButton("重置狀態")
        
        button_layout.addWidget(self.enable_all_button)
        button_layout.addWidget(self.disable_all_button)
        button_layout.addWidget(self.reset_states_button)
        button_layout.addStretch()
        
        layout.addLayout(button_layout)
        
        return group
    
    def create_mov_limits_group(self):
        """創建 MOV 行為限制群組"""
        group = QGroupBox("移動行為限制")
        layout = QFormLayout(group)
        
        # 啟用移動
        self.enable_movement_checkbox = QCheckBox("允許 UEP 移動")
        self.enable_movement_checkbox.setChecked(True)
        layout.addRow(self.enable_movement_checkbox)
        
        # 移動範圍限制
        self.movement_boundary_combo = QComboBox()
        self.movement_boundary_combo.addItems(["整個螢幕", "主螢幕", "當前視窗", "自訂區域"])
        layout.addRow("移動範圍:", self.movement_boundary_combo)
        
        # 移動速度
        self.movement_speed_slider = QSlider(Qt.Horizontal)
        self.movement_speed_slider.setRange(10, 100)
        self.movement_speed_slider.setValue(50)
        self.movement_speed_label = QLabel("50%")
        
        speed_layout = QHBoxLayout()
        speed_layout.addWidget(self.movement_speed_slider)
        speed_layout.addWidget(self.movement_speed_label)
        layout.addRow("移動速度:", speed_layout)
        
        # 重力效果
        self.gravity_checkbox = QCheckBox("啟用重力效果")
        self.gravity_checkbox.setChecked(True)
        layout.addRow(self.gravity_checkbox)
        
        # 連接信號
        self.movement_speed_slider.valueChanged.connect(
            lambda v: self.movement_speed_label.setText(f"{v}%")
        )
        
        return group
    
    def create_auto_behavior_group(self):
        """創建自動行為設定群組"""
        group = QGroupBox("自動行為")
        layout = QFormLayout(group)
        
        # 自動漫遊
        self.auto_roam_checkbox = QCheckBox("啟用自動漫遊")
        self.auto_roam_checkbox.setChecked(False)
        layout.addRow(self.auto_roam_checkbox)
        
        # 智慧跟隨
        self.smart_follow_checkbox = QCheckBox("智慧跟隨游標")
        self.smart_follow_checkbox.setChecked(False)
        layout.addRow(self.smart_follow_checkbox)
        
        # 自動回應
        self.auto_response_checkbox = QCheckBox("自動回應")
        self.auto_response_checkbox.setChecked(True)
        layout.addRow(self.auto_response_checkbox)
        
        # 休眠模式
        self.sleep_mode_checkbox = QCheckBox("啟用休眠模式")
        self.sleep_mode_checkbox.setChecked(True)
        layout.addRow(self.sleep_mode_checkbox)
        
        # 休眠時間
        self.sleep_time_spinbox = QSpinBox()
        self.sleep_time_spinbox.setRange(1, 60)
        self.sleep_time_spinbox.setValue(10)
        self.sleep_time_spinbox.setSuffix(" 分鐘")
        layout.addRow("休眠等待時間:", self.sleep_time_spinbox)
        
        return group
    
    def create_mouse_interaction_group(self):
        """創建滑鼠互動群組"""
        group = QGroupBox("滑鼠互動")
        layout = QFormLayout(group)
        
        # 滑鼠懸停
        self.mouse_hover_checkbox = QCheckBox("啟用滑鼠懸停反應")
        self.mouse_hover_checkbox.setChecked(True)
        layout.addRow(self.mouse_hover_checkbox)
        
        # 點擊互動
        self.click_interaction_checkbox = QCheckBox("啟用點擊互動")
        self.click_interaction_checkbox.setChecked(True)
        layout.addRow(self.click_interaction_checkbox)
        
        # 拖拽行為
        self.drag_behavior_combo = QComboBox()
        self.drag_behavior_combo.addItems(["自由拖拽", "限制範圍", "禁止拖拽"])
        layout.addRow("拖拽行為:", self.drag_behavior_combo)
        
        # 雙擊動作
        self.double_click_combo = QComboBox()
        self.double_click_combo.addItems(["無動作", "開啟設定", "呼叫 UEP", "隱藏/顯示"])
        layout.addRow("雙擊動作:", self.double_click_combo)
        
        return group
    
    def create_keyboard_shortcuts_group(self):
        """創建鍵盤快捷鍵群組"""
        group = QGroupBox("鍵盤快捷鍵")
        layout = QFormLayout(group)
        
        # 說明文字
        info_label = QLabel("設定系統快捷鍵 (此功能正在開發中)")
        info_label.setStyleSheet("color: #7f8c8d; font-style: italic;")
        layout.addRow(info_label)
        
        # 快捷鍵列表
        shortcuts = [
            ("呼叫 UEP", "Ctrl+Shift+U"),
            ("開啟設定", "Ctrl+Shift+S"),
            ("隱藏/顯示", "Ctrl+Shift+H"),
            ("緊急停止", "Ctrl+Shift+E")
        ]
        
        for action, shortcut in shortcuts:
            shortcut_edit = QLineEdit(shortcut)
            shortcut_edit.setReadOnly(True)  # 暫時只讀
            layout.addRow(f"{action}:", shortcut_edit)
        
        return group
    
    def create_drag_drop_group(self):
        """創建檔案拖放群組"""
        group = QGroupBox("檔案拖放")
        layout = QFormLayout(group)
        
        # 啟用檔案拖放
        self.file_drop_checkbox = QCheckBox("啟用檔案拖放")
        self.file_drop_checkbox.setChecked(True)
        layout.addRow(self.file_drop_checkbox)
        
        # 支援的檔案類型
        self.supported_files_edit = QLineEdit("*.txt, *.pdf, *.doc, *.jpg, *.png")
        self.supported_files_edit.setPlaceholderText("例: *.txt, *.pdf, *.jpg")
        layout.addRow("支援檔案類型:", self.supported_files_edit)
        
        # 拖放動作
        self.drop_action_combo = QComboBox()
        self.drop_action_combo.addItems(["分析檔案", "開啟檔案", "詢問動作"])
        layout.addRow("拖放動作:", self.drop_action_combo)
        
        return group
    
    def create_notification_group(self):
        """創建通知設定群組"""
        group = QGroupBox("通知設定")
        layout = QFormLayout(group)
        
        # 啟用通知
        self.notifications_checkbox = QCheckBox("啟用系統通知")
        self.notifications_checkbox.setChecked(True)
        layout.addRow(self.notifications_checkbox)
        
        # 通知位置
        self.notification_position_combo = QComboBox()
        self.notification_position_combo.addItems(["右下角", "右上角", "左上角", "左下角", "中央"])
        layout.addRow("通知位置:", self.notification_position_combo)
        
        # 通知持續時間
        self.notification_duration_spinbox = QSpinBox()
        self.notification_duration_spinbox.setRange(1, 30)
        self.notification_duration_spinbox.setValue(5)
        self.notification_duration_spinbox.setSuffix(" 秒")
        layout.addRow("顯示時間:", self.notification_duration_spinbox)
        
        return group
    
    def create_advanced_settings_group(self):
        """創建進階設定群組"""
        group = QGroupBox("進階設定")
        layout = QFormLayout(group)
        
        # 開發者模式
        self.developer_mode_checkbox = QCheckBox("開發者模式")
        self.developer_mode_checkbox.setChecked(False)
        layout.addRow(self.developer_mode_checkbox)
        
        # 除錯日誌
        self.debug_logging_checkbox = QCheckBox("啟用詳細日誌")
        self.debug_logging_checkbox.setChecked(False)
        layout.addRow(self.debug_logging_checkbox)
        
        # 效能監控
        self.performance_monitor_checkbox = QCheckBox("效能監控")
        self.performance_monitor_checkbox.setChecked(False)
        layout.addRow(self.performance_monitor_checkbox)
        
        # 自動更新
        self.auto_update_checkbox = QCheckBox("自動檢查更新")
        self.auto_update_checkbox.setChecked(True)
        layout.addRow(self.auto_update_checkbox)
        
        return group
    
    def create_data_privacy_group(self):
        """創建資料與隱私群組"""
        group = QGroupBox("資料與隱私")
        layout = QVBoxLayout(group)
        
        # 說明文字
        info_label = QLabel("UEP 重視您的隱私，所有資料均在本地處理")
        info_label.setStyleSheet("color: #27ae60; font-weight: bold;")
        layout.addWidget(info_label)
        
        # 資料設定
        form_layout = QFormLayout()
        
        # 保存對話記錄
        self.save_conversations_checkbox = QCheckBox("保存對話記錄")
        self.save_conversations_checkbox.setChecked(True)
        form_layout.addRow(self.save_conversations_checkbox)
        
        # 資料保留時間
        self.data_retention_spinbox = QSpinBox()
        self.data_retention_spinbox.setRange(1, 365)
        self.data_retention_spinbox.setValue(30)
        self.data_retention_spinbox.setSuffix(" 天")
        form_layout.addRow("資料保留時間:", self.data_retention_spinbox)
        
        layout.addLayout(form_layout)
        
        # 按鈕區域
        button_layout = QHBoxLayout()
        self.clear_data_button = QPushButton("清除所有資料")
        self.export_data_button = QPushButton("匯出資料")
        
        button_layout.addWidget(self.clear_data_button)
        button_layout.addWidget(self.export_data_button)
        button_layout.addStretch()
        
        layout.addLayout(button_layout)
        
        return group
    
    def create_maintenance_group(self):
        """創建系統維護群組"""
        group = QGroupBox("系統維護")
        layout = QVBoxLayout(group)
        
        # 按鈕區域
        button_layout = QGridLayout()
        
        self.restart_button = QPushButton("重新啟動 UEP")
        self.reset_settings_button = QPushButton("重置所有設定")
        self.check_updates_button = QPushButton("檢查更新")
        self.repair_system_button = QPushButton("系統修復")
        
        button_layout.addWidget(self.restart_button, 0, 0)
        button_layout.addWidget(self.reset_settings_button, 0, 1)
        button_layout.addWidget(self.check_updates_button, 1, 0)
        button_layout.addWidget(self.repair_system_button, 1, 1)
        
        layout.addLayout(button_layout)
        
        return group
    
    def create_about_group(self):
        """創建關於群組"""
        group = QGroupBox("關於 UEP")
        layout = QVBoxLayout(group)
        
        # UEP 資訊
        info_text = """
        <h3>UEP (Unforgettable Eternal Project)</h3>
        <p><b>版本:</b> 1.0.0</p>
        <p><b>開發團隊:</b> UEP 開發組</p>
        <p><b>授權:</b> MIT License</p>
        <br>
        <p>UEP 是一個智慧型桌面助理系統，旨在提供自然、直觀的人機互動體驗。</p>
        """
        
        info_label = QLabel(info_text)
        info_label.setWordWrap(True)
        info_label.setOpenExternalLinks(True)
        layout.addWidget(info_label)
        
        # 按鈕區域
        button_layout = QHBoxLayout()
        self.website_button = QPushButton("官方網站")
        self.license_button = QPushButton("授權資訊")
        self.help_button = QPushButton("說明文件")
        
        button_layout.addWidget(self.website_button)
        button_layout.addWidget(self.license_button)
        button_layout.addWidget(self.help_button)
        button_layout.addStretch()
        
        layout.addLayout(button_layout)
        
        return group
    
    def create_bottom_buttons(self, parent_layout):
        """創建底部按鈕區域"""
        button_frame = QFrame()
        button_frame.setFrameStyle(QFrame.StyledPanel)
        button_layout = QHBoxLayout(button_frame)
        
        # 左側按鈕
        self.minimize_to_orb_button = QPushButton("最小化到球體")
        self.minimize_to_orb_button.clicked.connect(self.minimize_to_orb)
        
        # 右側按鈕
        self.apply_button = QPushButton("套用")
        self.ok_button = QPushButton("確定")
        self.cancel_button = QPushButton("取消")
        
        # 連接信號
        self.apply_button.clicked.connect(self.apply_settings)
        self.ok_button.clicked.connect(self.ok_clicked)
        self.cancel_button.clicked.connect(self.cancel_clicked)
        
        # 佈局
        button_layout.addWidget(self.minimize_to_orb_button)
        button_layout.addStretch()
        button_layout.addWidget(self.apply_button)
        button_layout.addWidget(self.ok_button)
        button_layout.addWidget(self.cancel_button)
        
        parent_layout.addWidget(button_frame)
    
    def create_status_bar(self):
        """創建狀態列"""
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        
        # 狀態資訊
        self.status_bar.showMessage("準備就緒")
        
        # 進度條（用於某些操作）
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        self.status_bar.addPermanentWidget(self.progress_bar)
    
    def apply_styles(self):
        """應用視窗樣式"""
        self.setStyleSheet("""
            QMainWindow {
                background-color: #f8f9fa;
            }
            
            QTabWidget::pane {
                border: 1px solid #dee2e6;
                background-color: white;
                border-radius: 5px;
            }
            
            QTabWidget::tab-bar {
                alignment: center;
            }
            
            QTabBar::tab {
                background-color: #e9ecef;
                border: 1px solid #dee2e6;
                padding: 8px 16px;
                margin-right: 2px;
                border-top-left-radius: 5px;
                border-top-right-radius: 5px;
            }
            
            QTabBar::tab:selected {
                background-color: white;
                border-bottom-color: white;
            }
            
            QTabBar::tab:hover:!selected {
                background-color: #f8f9fa;
            }
            
            QGroupBox {
                font-weight: bold;
                border: 2px solid #dee2e6;
                border-radius: 5px;
                margin-top: 10px;
                padding-top: 10px;
            }
            
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 5px 0 5px;
                color: #495057;
            }
            
            QPushButton {
                background-color: #007bff;
                color: white;
                border: none;
                padding: 8px 16px;
                border-radius: 4px;
                font-weight: bold;
            }
            
            QPushButton:hover {
                background-color: #0056b3;
            }
            
            QPushButton:pressed {
                background-color: #004085;
            }
            
            QPushButton:disabled {
                background-color: #6c757d;
            }
            
            QCheckBox {
                spacing: 8px;
            }
            
            QCheckBox::indicator {
                width: 18px;
                height: 18px;
            }
            
            QCheckBox::indicator:unchecked {
                border: 2px solid #dee2e6;
                background-color: white;
                border-radius: 3px;
            }
            
            QCheckBox::indicator:checked {
                border: 2px solid #007bff;
                background-color: #007bff;
                border-radius: 3px;
            }
            
            QSlider::groove:horizontal {
                border: 1px solid #dee2e6;
                height: 6px;
                background-color: #e9ecef;
                border-radius: 3px;
            }
            
            QSlider::handle:horizontal {
                background-color: #007bff;
                border: 1px solid #007bff;
                width: 18px;
                height: 18px;
                border-radius: 9px;
                margin: -6px 0;
            }
            
            QSlider::handle:horizontal:hover {
                background-color: #0056b3;
            }
            
            QComboBox {
                border: 1px solid #dee2e6;
                border-radius: 4px;
                padding: 4px 8px;
                background-color: white;
            }
            
            QComboBox:hover {
                border-color: #007bff;
            }
            
            QLineEdit, QSpinBox {
                border: 1px solid #dee2e6;
                border-radius: 4px;
                padding: 4px 8px;
                background-color: white;
            }
            
            QLineEdit:focus, QSpinBox:focus {
                border-color: #007bff;
                outline: none;
            }
        """)
    
    def load_settings(self):
        """載入設定值"""
        try:
            # 載入個人設定
            self.uep_name_edit.setText(self.settings.value("personal/uep_name", "UEP"))
            self.user_name_edit.setText(self.settings.value("personal/user_name", "使用者"))
            
            # 載入表現設定
            self.enable_tts_checkbox.setChecked(
                self.settings.value("performance/enable_tts", True, type=bool)
            )
            self.tts_volume_slider.setValue(
                self.settings.value("performance/tts_volume", 70, type=int)
            )
            
            # 載入行為設定
            self.enable_movement_checkbox.setChecked(
                self.settings.value("behavior/enable_movement", True, type=bool)
            )
            
            # 載入互動設定
            self.mouse_hover_checkbox.setChecked(
                self.settings.value("interaction/mouse_hover", True, type=bool)
            )
            
            info_log("[UserMainWindow] 設定載入完成")
            
        except Exception as e:
            error_log(f"[UserMainWindow] 載入設定時發生錯誤: {e}")
    
    def save_settings(self):
        """保存設定值"""
        try:
            # 保存個人設定
            self.settings.setValue("personal/uep_name", self.uep_name_edit.text())
            self.settings.setValue("personal/user_name", self.user_name_edit.text())
            
            # 保存表現設定
            self.settings.setValue("performance/enable_tts", self.enable_tts_checkbox.isChecked())
            self.settings.setValue("performance/tts_volume", self.tts_volume_slider.value())
            
            # 保存行為設定
            self.settings.setValue("behavior/enable_movement", self.enable_movement_checkbox.isChecked())
            
            # 保存互動設定
            self.settings.setValue("interaction/mouse_hover", self.mouse_hover_checkbox.isChecked())
            
            # 同步設定
            self.settings.sync()
            
            info_log("[UserMainWindow] 設定保存完成")
            
        except Exception as e:
            error_log(f"[UserMainWindow] 保存設定時發生錯誤: {e}")
    
    def apply_settings(self):
        """套用設定"""
        self.save_settings()
        self.status_bar.showMessage("設定已套用", 3000)
        
        # 發出設定變更信號
        self.settings_changed.emit("applied", None)
        
        debug_log(OPERATION_LEVEL, "[UserMainWindow] 設定已套用")
    
    def ok_clicked(self):
        """確定按鈕點擊"""
        self.apply_settings()
        debug_log(OPERATION_LEVEL, "[UserMainWindow] 用戶點擊確定按鈕，關閉視窗")
        self.close()
    
    def cancel_clicked(self):
        """取消按鈕點擊"""
        debug_log(OPERATION_LEVEL, "[UserMainWindow] 用戶點擊取消按鈕，重新載入設定")
        self.load_settings()  # 重新載入設定
        self.close()
    
    def minimize_to_orb(self):
        """最小化到圓球"""
        self.is_minimized_to_orb = True
        self.original_geometry = self.geometry()
        self.hide()
        
        # 發出信號通知 UI 模組顯示圓球
        self.action_triggered.emit("minimize_to_orb", {})
        
        debug_log(OPERATION_LEVEL, "[UserMainWindow] 已最小化到圓球")
    
    def restore_from_orb(self):
        """從圓球恢復視窗"""
        if self.is_minimized_to_orb:
            if self.original_geometry:
                self.setGeometry(self.original_geometry)
            self.show()
            self.raise_()
            self.activateWindow()
            self.is_minimized_to_orb = False
            
            debug_log(OPERATION_LEVEL, "[UserMainWindow] 從圓球恢復視窗")
    
    def closeEvent(self, event):
        """視窗關閉事件"""
        if not self.is_minimized_to_orb:
            # 正常關閉時，最小化到圓球而不是真的關閉
            debug_log(OPERATION_LEVEL, "[UserMainWindow] 視窗關閉請求，最小化到圓球")
            self.minimize_to_orb()
            event.ignore()
        else:
            # 發出關閉信號
            info_log("[UserMainWindow] 視窗正在關閉")
            self.window_closed.emit()
            event.accept()
    
    def show_settings_page(self, page_name: str):
        """顯示特定設定頁面"""
        page_map = {
            "personal": 0,
            "performance": 1,
            "behavior": 2,
            "interaction": 3,
            "other": 4
        }
        
        if page_name in page_map:
            self.tab_widget.setCurrentIndex(page_map[page_name])
            if self.is_minimized_to_orb:
                self.restore_from_orb()
            
            debug_log(OPERATION_LEVEL, f"[UserMainWindow] 顯示設定頁面: {page_name}")
    
    def update_system_info(self, info: Dict[str, Any]):
        """更新系統資訊顯示"""
        try:
            if "status" in info:
                self.system_status_label.setText(info["status"])
                if info["status"] == "正常運行":
                    self.system_status_label.setStyleSheet("color: green; font-weight: bold;")
                else:
                    self.system_status_label.setStyleSheet("color: red; font-weight: bold;")
            
            if "uptime" in info:
                self.uptime_label.setText(info["uptime"])
            
            if "memory" in info:
                self.memory_label.setText(f"{info['memory']} MB")
            
            if "cpu" in info:
                self.cpu_label.setText(f"{info['cpu']}%")
            
            if "active_modules" in info:
                self.active_modules_label.setText(", ".join(info["active_modules"]))
                
        except Exception as e:
            error_log(f"[UserMainWindow] 更新系統資訊時發生錯誤: {e}")
    
    def handle_request(self, data: dict) -> dict:
        """處理來自UI模組的請求"""
        try:
            command = data.get('command')
            
            if command == 'show_settings':
                self.show()
                self.raise_()
                self.activateWindow()
                info_log("[UserMainWindow] 顯示設定視窗")
                return {"success": True, "message": "設定視窗已顯示"}
            
            elif command == 'hide_settings':
                self.hide()
                info_log("[UserMainWindow] 隱藏設定視窗")
                return {"success": True, "message": "設定視窗已隱藏"}
            
            elif command == 'update_settings':
                settings = data.get('settings', {})
                for key, value in settings.items():
                    if hasattr(self, key):
                        setattr(self, key, value)
                info_log(f"[UserMainWindow] 已更新設定: {list(settings.keys())}")
                return {"success": True, "updated_settings": list(settings.keys())}
            
            elif command == 'get_settings':
                current_settings = {}
                # 這裡可以實現獲取當前設定的邏輯
                debug_log(OPERATION_LEVEL, "[UserMainWindow] 獲取當前設定")
                return {"success": True, "settings": current_settings}
            
            elif command == 'show_page':
                page_name = data.get('page_name')
                if page_name:
                    self.show_settings_page(page_name)
                    return {"success": True, "message": f"已切換到 {page_name} 頁面"}
                return {"error": "需要指定 page_name 參數"}
            
            elif command == 'update_system_info':
                info = data.get('info', {})
                self.update_system_info(info)
                return {"success": True, "message": "系統資訊已更新"}
            
            else:
                return {"error": f"未知命令: {command}"}
                
        except Exception as e:
            error_log(f"[UserMainWindow] 處理請求時發生錯誤: {e}")
            return {"error": str(e)}


# 用於模組測試的便利函數
def create_test_window():
    """創建測試用的設定視窗"""
    if not PYQT5_AVAILABLE:
        error_log("[UserMainWindow] PyQt5 不可用，無法創建測試視窗")
        return None
        
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    
    window = UserMainWindow()
    window.show()
    
    return app, window


if __name__ == "__main__":
    # 獨立測試模式
    app, window = create_test_window()
    if app and window:
        sys.exit(app.exec_())
