import os
import sys
from typing import Dict, Any, Optional

try:
    from PyQt5.QtWidgets import (QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
                                QTabWidget, QLabel, QGroupBox, QScrollArea,
                                QFrame, QPushButton, QCheckBox, QSpinBox,
                                QSlider, QComboBox, QLineEdit, QTextEdit,
                                QSplitter, QTreeWidget, QTreeWidgetItem,
                                QFormLayout, QGridLayout, QSizePolicy, QGroupBox,
                                QApplication, QMessageBox, QFileDialog, QVBoxLayout,
                                QProgressBar, QStatusBar, QMenuBar, QGroupBox,
                                QToolBar, QAction, QButtonGroup)
    from PyQt5.QtCore import (Qt, QTimer, pyqtSignal, QSize, QRect,
                             QPropertyAnimation, QEasingCurve, QThread,
                             QSettings, QStandardPaths)
    from PyQt5.QtGui import (QIcon, QFont, QPixmap, QPalette, QColor,
                            QPainter, QLinearGradient, QBrush)
    PYQT5_AVAILABLE = True
except ImportError:
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

from theme_manager import theme_manager, Theme, install_theme_hook
from utils.debug_helper import debug_log, info_log, error_log, KEY_LEVEL, OPERATION_LEVEL, SYSTEM_LEVEL, ELABORATIVE_LEVEL


class UserMainWindow(QMainWindow):
    settings_changed = pyqtSignal(str, object)
    action_triggered = pyqtSignal(str, dict)
    window_closed = pyqtSignal()
    SCROLL_AREA_MIN_H = 620

    def __init__(self, ui_module=None):
        super().__init__()

        if not PYQT5_AVAILABLE:
            error_log("[UserMainWindow] PyQt5不可用，使用降級模式")
            return

        self.ui_module = ui_module
        self.settings = QSettings("UEP", "Core")

        self.is_minimized_to_orb = False
        self.original_geometry = None
        self.dark_mode = (theme_manager.theme == Theme.DARK)

        self.init_ui()
        install_theme_hook(self)
        self.load_settings()
        self.hide()

        theme_manager.theme_changed.connect(self._on_global_theme_changed)

        info_log("[UserMainWindow] 設定視窗初始化完成")

    def init_ui(self):
        self.setWindowTitle("UEP設定")
        self.setMinimumSize(900, 950)
        self.resize(1200, 950)

        debug_log(SYSTEM_LEVEL, "[UserMainWindow] 開始初始化使用者介面")

        try:
            icon_path = os.path.join(os.path.dirname(__file__), "../../../arts/U.E.P.png")
            if os.path.exists(icon_path):
                self.setWindowIcon(QIcon(icon_path))
        except Exception as e:
            error_log(f"[UserMainWindow] 無法載入圖標:{e}")

        central_widget = QWidget()
        self.setCentralWidget(central_widget)

        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        self.create_header(main_layout)
        self.create_tab_widget(main_layout)
        self.create_bottom_buttons(main_layout)
        self.create_status_bar()

        debug_log(SYSTEM_LEVEL, "[UserMainWindow] 使用者介面初始化完成")

    def create_header(self, parent_layout):
        header = QFrame()
        header.setObjectName("header")
        header.setFixedHeight(92)

        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(30, 16, 30, 16)
        header_layout.setSpacing(16)

        title_container = QVBoxLayout()
        title_label = QLabel("設定")
        title_label.setObjectName("mainTitle")
        title_label.setMinimumHeight(34)
        subtitle_label = QLabel("自訂您的UEP體驗")
        subtitle_label.setObjectName("subtitle")
        subtitle_label.setWordWrap(True)

        title_container.addWidget(title_label)
        title_container.addWidget(subtitle_label)
        title_container.addStretch()

        header_layout.addLayout(title_container)
        header_layout.addStretch()

        self.theme_toggle = QPushButton()
        self.theme_toggle.setObjectName("themeToggle")

        self.theme_toggle.setFixedSize(56, 56)
        self.theme_toggle.setCursor(Qt.PointingHandCursor)

        btn_font = QFont("Segoe UI Emoji, Apple Color Emoji, Noto Color Emoji")
        btn_font.setPointSize(20)
        self.theme_toggle.setFont(btn_font)
        self.theme_toggle.setText("☀️" if self.dark_mode else "🌙")

        self.theme_toggle.clicked.connect(self.toggle_theme)
        header_layout.addWidget(self.theme_toggle)

        parent_layout.addWidget(header)

    def create_tab_widget(self, parent_layout: QVBoxLayout):
        self.tab_widget = QTabWidget()
        self.tab_widget.setObjectName("mainTabs")
        self.tab_widget.setTabPosition(QTabWidget.North)

        tb = self.tab_widget.tabBar()
        tb.setElideMode(Qt.ElideNone)
        tb.setUsesScrollButtons(True)
        tb.setExpanding(False)
        tb.setStyleSheet("QTabBar::tab { min-height:42px; padding:12px 28px; }")

        self.create_personal_tab()
        self.create_performance_tab()
        self.create_behavior_tab()
        self.create_interaction_tab()
        self.create_other_tab()

        parent_layout.addWidget(self.tab_widget, 1)

    def _loose_group(self, group: QGroupBox):
        group.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        return group

    def create_personal_tab(self):
        personal_widget = QWidget()
        personal_layout = QVBoxLayout(personal_widget)
        personal_layout.setContentsMargins(30, 30, 30, 30)
        personal_layout.setSpacing(20)

        scroll_area = QScrollArea()
        self._tall_scroll(scroll_area)

        scroll_content = QWidget()
        scroll_content.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        scroll_layout = QVBoxLayout(scroll_content)
        scroll_layout.setSpacing(20)

        system_info_group = self.create_system_info_group()
        scroll_layout.addWidget(system_info_group)

        personal_prefs_group = self.create_personal_preferences_group()
        scroll_layout.addWidget(personal_prefs_group)

        account_group = self.create_account_settings_group()
        scroll_layout.addWidget(account_group)

        scroll_layout.addStretch()
        scroll_area.setWidget(scroll_content)
        personal_layout.addWidget(scroll_area, 1)

        self.tab_widget.addTab(personal_widget, "個人")

    def create_performance_tab(self):
        performance_widget = QWidget()
        performance_layout = QVBoxLayout(performance_widget)
        performance_layout.setContentsMargins(30, 30, 30, 30)
        performance_layout.setSpacing(20)

        scroll_area = QScrollArea()
        self._tall_scroll(scroll_area)

        scroll_content = QWidget()
        scroll_content.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        scroll_layout = QVBoxLayout(scroll_content)
        scroll_layout.setSpacing(20)

        tts_group = self.create_tts_settings_group()
        scroll_layout.addWidget(tts_group)

        subtitle_group = self.create_subtitle_settings_group()
        scroll_layout.addWidget(subtitle_group)

        animation_group = self.create_animation_settings_group()
        scroll_layout.addWidget(animation_group)

        visual_group = self.create_visual_effects_group()
        scroll_layout.addWidget(visual_group)

        scroll_layout.addStretch()
        scroll_area.setWidget(scroll_content)
        performance_layout.addWidget(scroll_area, 1)

        self.tab_widget.addTab(performance_widget, "表現")

    def create_behavior_tab(self):
        behavior_widget = QWidget()
        behavior_layout = QVBoxLayout(behavior_widget)
        behavior_layout.setContentsMargins(30, 30, 30, 30)
        behavior_layout.setSpacing(20)

        scroll_area = QScrollArea()
        self._tall_scroll(scroll_area)

        scroll_content = QWidget()
        scroll_content.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        scroll_layout = QVBoxLayout(scroll_content)
        scroll_layout.setSpacing(20)

        system_states_group = self.create_system_states_group()
        scroll_layout.addWidget(system_states_group)

        mov_limits_group = self.create_mov_limits_group()
        scroll_layout.addWidget(mov_limits_group)

        auto_behavior_group = self.create_auto_behavior_group()
        scroll_layout.addWidget(auto_behavior_group)

        scroll_layout.addStretch()
        scroll_area.setWidget(scroll_content)
        behavior_layout.addWidget(scroll_area, 1)

        self.tab_widget.addTab(behavior_widget, "行為模式")

    def create_interaction_tab(self):
        interaction_widget = QWidget()
        interaction_layout = QVBoxLayout(interaction_widget)
        interaction_layout.setContentsMargins(30, 30, 30, 30)
        interaction_layout.setSpacing(20)

        scroll_area = QScrollArea()
        self._tall_scroll(scroll_area)

        scroll_content = QWidget()
        scroll_content.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        scroll_layout = QVBoxLayout(scroll_content)
        scroll_layout.setSpacing(20)

        mouse_group = self.create_mouse_interaction_group()
        scroll_layout.addWidget(mouse_group)

        keyboard_group = self.create_keyboard_shortcuts_group()
        scroll_layout.addWidget(keyboard_group)

        drag_drop_group = self.create_drag_drop_group()
        scroll_layout.addWidget(drag_drop_group)

        notification_group = self.create_notification_group()
        scroll_layout.addWidget(notification_group)

        scroll_layout.addStretch()
        scroll_area.setWidget(scroll_content)
        interaction_layout.addWidget(scroll_area, 1)

        self.tab_widget.addTab(interaction_widget, "互動")

    def create_other_tab(self):
        other_widget = QWidget()
        other_layout = QVBoxLayout(other_widget)
        other_layout.setContentsMargins(30, 30, 30, 30)
        other_layout.setSpacing(20)

        scroll_area = QScrollArea()
        self._tall_scroll(scroll_area)

        scroll_content = QWidget()
        scroll_content.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        scroll_layout = QVBoxLayout(scroll_content)
        scroll_layout.setSpacing(20)

        advanced_group = self.create_advanced_settings_group()
        scroll_layout.addWidget(advanced_group)

        data_privacy_group = self.create_data_privacy_group()
        scroll_layout.addWidget(data_privacy_group)

        maintenance_group = self.create_maintenance_group()
        scroll_layout.addWidget(maintenance_group)

        about_group = self.create_about_group()
        scroll_layout.addWidget(about_group)

        scroll_layout.addStretch()
        scroll_area.setWidget(scroll_content)
        other_layout.addWidget(scroll_area, 1)

        self.tab_widget.addTab(other_widget, "其他")

    def create_system_info_group(self):
        group = QGroupBox("系統資訊")
        group.setObjectName("settingsGroup")
        layout = QFormLayout(group)
        layout.setSpacing(15)
        layout.setContentsMargins(20, 25, 20, 20)

        self.system_status_label = QLabel("正常運行")
        self.system_status_label.setObjectName("statusOk")
        layout.addRow("系統狀態:", self.system_status_label)

        self.uptime_label = QLabel("00:00:00")
        layout.addRow("運行時間:", self.uptime_label)

        self.memory_label = QLabel("0MB")
        layout.addRow("記憶體使用:", self.memory_label)

        self.cpu_label = QLabel("0%")
        layout.addRow("CPU使用率:", self.cpu_label)

        self.active_modules_label = QLabel("正在載入...")
        layout.addRow("活躍模組:", self.active_modules_label)

        return self._loose_group(group)

    def create_personal_preferences_group(self):
        group = QGroupBox("個人偏好")
        group.setObjectName("settingsGroup")
        layout = QFormLayout(group)
        layout.setSpacing(15)
        layout.setContentsMargins(20, 25, 20, 20)

        self.uep_name_edit = QLineEdit()
        self.uep_name_edit.setPlaceholderText("UEP")
        layout.addRow("UEP名稱:", self.uep_name_edit)

        self.user_name_edit = QLineEdit()
        self.user_name_edit.setPlaceholderText("使用者")
        layout.addRow("使用者名稱:", self.user_name_edit)

        self.language_combo = QComboBox()
        self.language_combo.addItems(["繁體中文", "簡體中文", "English", "日本語"])
        layout.addRow("語言:", self.language_combo)

        self.theme_combo = QComboBox()
        self.theme_combo.addItems(["預設主題", "深色主題", "淺色主題", "自訂主題"])
        layout.addRow("主題:", self.theme_combo)

        return self._loose_group(group)

    def create_account_settings_group(self):
        group = QGroupBox("帳戶設定")
        group.setObjectName("settingsGroup")
        layout = QVBoxLayout(group)
        layout.setContentsMargins(20, 25, 20, 20)
        layout.setSpacing(15)

        info_label = QLabel("管理UEP的帳戶相關設定")
        info_label.setObjectName("infoText")
        layout.addWidget(info_label)

        button_layout = QHBoxLayout()
        self.login_button = QPushButton("登入帳戶")
        self.logout_button = QPushButton("登出")
        self.logout_button.setEnabled(False)

        button_layout.addWidget(self.login_button)
        button_layout.addWidget(self.logout_button)
        button_layout.addStretch()

        layout.addLayout(button_layout)

        return self._loose_group(group)

    def create_tts_settings_group(self):
        group = QGroupBox("語音合成")
        group.setObjectName("settingsGroup")
        layout = QFormLayout(group)
        layout.setSpacing(15)
        layout.setContentsMargins(20, 25, 20, 20)

        self.enable_tts_checkbox = QCheckBox("啟用語音合成")
        self.enable_tts_checkbox.setChecked(True)
        layout.addRow(self.enable_tts_checkbox)

        self.tts_volume_slider = QSlider(Qt.Horizontal)
        self.tts_volume_slider.setRange(0, 100)
        self.tts_volume_slider.setValue(70)
        self.tts_volume_label = QLabel("70%")

        volume_layout = QHBoxLayout()
        volume_layout.addWidget(self.tts_volume_slider)
        volume_layout.addWidget(self.tts_volume_label)
        layout.addRow("音量:", volume_layout)

        self.tts_speed_slider = QSlider(Qt.Horizontal)
        self.tts_speed_slider.setRange(50, 200)
        self.tts_speed_slider.setValue(100)
        self.tts_speed_label = QLabel("100%")

        speed_layout = QHBoxLayout()
        speed_layout.addWidget(self.tts_speed_slider)
        speed_layout.addWidget(self.tts_speed_label)
        layout.addRow("語速:", speed_layout)

        self.voice_combo = QComboBox()
        self.voice_combo.addItems(["預設語音", "女聲A", "女聲B", "男聲A", "男聲B"])
        layout.addRow("語音:", self.voice_combo)

        self.tts_volume_slider.valueChanged.connect(
            lambda v: self.tts_volume_label.setText(f"{v}%")
        )
        self.tts_speed_slider.valueChanged.connect(
            lambda v: self.tts_speed_label.setText(f"{v}%")
        )

        return self._loose_group(group)

    def create_subtitle_settings_group(self):
        group = QGroupBox("字幕顯示")
        group.setObjectName("settingsGroup")
        layout = QFormLayout(group)
        layout.setSpacing(15)
        layout.setContentsMargins(20, 25, 20, 20)

        self.enable_subtitle_checkbox = QCheckBox("顯示字幕")
        self.enable_subtitle_checkbox.setChecked(True)
        layout.addRow(self.enable_subtitle_checkbox)

        self.subtitle_position_combo = QComboBox()
        self.subtitle_position_combo.addItems(["頂部", "中央", "底部", "跟隨UEP"])
        layout.addRow("字幕位置:", self.subtitle_position_combo)

        self.subtitle_size_spinbox = QSpinBox()
        self.subtitle_size_spinbox.setRange(8, 72)
        self.subtitle_size_spinbox.setValue(14)
        layout.addRow("字體大小:", self.subtitle_size_spinbox)

        self.subtitle_opacity_slider = QSlider(Qt.Horizontal)
        self.subtitle_opacity_slider.setRange(10, 100)
        self.subtitle_opacity_slider.setValue(90)
        self.subtitle_opacity_label = QLabel("90%")

        opacity_layout = QHBoxLayout()
        opacity_layout.addWidget(self.subtitle_opacity_slider)
        opacity_layout.addWidget(self.subtitle_opacity_label)
        layout.addRow("透明度:", opacity_layout)

        self.subtitle_opacity_slider.valueChanged.connect(
            lambda v: self.subtitle_opacity_label.setText(f"{v}%")
        )

        return self._loose_group(group)

    def create_animation_settings_group(self):
        group = QGroupBox("動畫設定")
        group.setObjectName("settingsGroup")
        layout = QFormLayout(group)
        layout.setSpacing(15)
        layout.setContentsMargins(20, 25, 20, 20)

        self.enable_animation_checkbox = QCheckBox("啟用動畫效果")
        self.enable_animation_checkbox.setChecked(True)
        layout.addRow(self.enable_animation_checkbox)

        self.animation_quality_combo = QComboBox()
        self.animation_quality_combo.addItems(["低", "中", "高", "極高"])
        self.animation_quality_combo.setCurrentText("高")
        layout.addRow("動畫品質:", self.animation_quality_combo)

        self.animation_speed_slider = QSlider(Qt.Horizontal)
        self.animation_speed_slider.setRange(50, 200)
        self.animation_speed_slider.setValue(100)
        self.animation_speed_label = QLabel("100%")

        speed_layout = QHBoxLayout()
        speed_layout.addWidget(self.animation_speed_slider)
        speed_layout.addWidget(self.animation_speed_label)
        layout.addRow("動畫速度:", speed_layout)

        self.animation_speed_slider.valueChanged.connect(
            lambda v: self.animation_speed_label.setText(f"{v}%")
        )

        return self._loose_group(group)

    def create_visual_effects_group(self):
        group = QGroupBox("視覺效果")
        group.setObjectName("settingsGroup")
        layout = QFormLayout(group)
        layout.setSpacing(15)
        layout.setContentsMargins(20, 25, 20, 20)

        self.shadow_checkbox = QCheckBox("啟用陰影效果")
        self.shadow_checkbox.setChecked(True)
        layout.addRow(self.shadow_checkbox)

        self.transparency_checkbox = QCheckBox("啟用半透明效果")
        self.transparency_checkbox.setChecked(True)
        layout.addRow(self.transparency_checkbox)

        self.particle_checkbox = QCheckBox("啟用粒子效果")
        self.particle_checkbox.setChecked(False)
        layout.addRow(self.particle_checkbox)

        return self._loose_group(group)

    def create_system_states_group(self):
        group = QGroupBox("系統狀態控制")
        group.setObjectName("settingsGroup")
        layout = QVBoxLayout(group)
        layout.setContentsMargins(20, 25, 20, 20)
        layout.setSpacing(15)

        self.state_tree = QTreeWidget()
        self.state_tree.setHeaderLabels(["模組/狀態", "狀態", "狀態2"])

        modules = ["STT模組", "NLP模組", "MEM模組", "LLM模組", "TTS模組", "SYS模組"]
        for module in modules:
            item = QTreeWidgetItem([module, "啟用", ""])
            self.state_tree.addTopLevelItem(item)

        layout.addWidget(self.state_tree)

        button_layout = QHBoxLayout()
        self.enable_all_button = QPushButton("全部啟用")
        self.disable_all_button = QPushButton("全部停用")
        self.reset_states_button = QPushButton("重置狀態")

        button_layout.addWidget(self.enable_all_button)
        button_layout.addWidget(self.disable_all_button)
        button_layout.addWidget(self.reset_states_button)
        button_layout.addStretch()

        layout.addLayout(button_layout)

        return self._loose_group(group)

    def create_mov_limits_group(self):
        group = QGroupBox("移動行為限制")
        group.setObjectName("settingsGroup")
        layout = QFormLayout(group)
        layout.setSpacing(15)
        layout.setContentsMargins(20, 25, 20, 20)

        self.enable_movement_checkbox = QCheckBox("允許UEP移動")
        self.enable_movement_checkbox.setChecked(True)
        layout.addRow(self.enable_movement_checkbox)

        self.movement_boundary_combo = QComboBox()
        self.movement_boundary_combo.addItems(["整個螢幕", "主螢幕", "當前視窗", "自訂區域"])
        layout.addRow("移動範圍:", self.movement_boundary_combo)

        self.movement_speed_slider = QSlider(Qt.Horizontal)
        self.movement_speed_slider.setRange(10, 100)
        self.movement_speed_slider.setValue(50)
        self.movement_speed_label = QLabel("50%")

        speed_layout = QHBoxLayout()
        speed_layout.addWidget(self.movement_speed_slider)
        speed_layout.addWidget(self.movement_speed_label)
        layout.addRow("移動速度:", speed_layout)

        self.gravity_checkbox = QCheckBox("啟用重力效果")
        self.gravity_checkbox.setChecked(True)
        layout.addRow(self.gravity_checkbox)

        self.movement_speed_slider.valueChanged.connect(
            lambda v: self.movement_speed_label.setText(f"{v}%")
        )

        return self._loose_group(group)

    def create_auto_behavior_group(self):
        group = QGroupBox("自動行為")
        group.setObjectName("settingsGroup")
        layout = QFormLayout(group)
        layout.setSpacing(15)
        layout.setContentsMargins(20, 25, 20, 20)

        self.auto_roam_checkbox = QCheckBox("啟用自動漫遊")
        self.auto_roam_checkbox.setChecked(False)
        layout.addRow(self.auto_roam_checkbox)

        self.smart_follow_checkbox = QCheckBox("智慧跟隨游標")
        self.smart_follow_checkbox.setChecked(False)
        layout.addRow(self.smart_follow_checkbox)

        self.auto_response_checkbox = QCheckBox("自動回應")
        self.auto_response_checkbox.setChecked(True)
        layout.addRow(self.auto_response_checkbox)

        self.sleep_mode_checkbox = QCheckBox("啟用休眠模式")
        self.sleep_mode_checkbox.setChecked(True)
        layout.addRow(self.sleep_mode_checkbox)

        self.sleep_time_spinbox = QSpinBox()
        self.sleep_time_spinbox.setRange(1, 60)
        self.sleep_time_spinbox.setValue(10)
        self.sleep_time_spinbox.setSuffix("分鐘")
        layout.addRow("休眠等待時間:", self.sleep_time_spinbox)

        return self._loose_group(group)

    def create_mouse_interaction_group(self):
        group = QGroupBox("滑鼠互動")
        group.setObjectName("settingsGroup")
        layout = QFormLayout(group)
        layout.setSpacing(15)
        layout.setContentsMargins(20, 25, 20, 20)

        self.mouse_hover_checkbox = QCheckBox("啟用滑鼠懸停反應")
        self.mouse_hover_checkbox.setChecked(True)
        layout.addRow(self.mouse_hover_checkbox)

        self.click_interaction_checkbox = QCheckBox("啟用點擊互動")
        self.click_interaction_checkbox.setChecked(True)
        layout.addRow(self.click_interaction_checkbox)

        self.drag_behavior_combo = QComboBox()
        self.drag_behavior_combo.addItems(["自由拖拽", "限制範圍", "禁止拖拽"])
        layout.addRow("拖拽行為:", self.drag_behavior_combo)

        self.double_click_combo = QComboBox()
        self.double_click_combo.addItems(["無動作", "開啟設定", "呼叫UEP", "隱藏/顯示"])
        layout.addRow("雙擊動作:", self.double_click_combo)

        return self._loose_group(group)

    def create_keyboard_shortcuts_group(self):
        group = QGroupBox("鍵盤快捷鍵")
        group.setObjectName("settingsGroup")
        layout = QFormLayout(group)
        layout.setSpacing(15)
        layout.setContentsMargins(20, 25, 20, 20)

        info_label = QLabel("設定系統快捷鍵(此功能正在開發中)")
        info_label.setObjectName("infoText")
        layout.addRow(info_label)

        shortcuts = [
            ("呼叫UEP", "Ctrl+Shift+U"),
            ("開啟設定", "Ctrl+Shift+S"),
            ("隱藏/顯示", "Ctrl+Shift+H"),
            ("緊急停止", "Ctrl+Shift+E")
        ]

        for action, shortcut in shortcuts:
            shortcut_edit = QLineEdit(shortcut)
            shortcut_edit.setReadOnly(True)
            layout.addRow(f"{action}:", shortcut_edit)

        return self._loose_group(group)

    def create_drag_drop_group(self):
        group = QGroupBox("檔案拖放")
        group.setObjectName("settingsGroup")
        layout = QFormLayout(group)
        layout.setSpacing(15)
        layout.setContentsMargins(20, 25, 20, 20)

        self.file_drop_checkbox = QCheckBox("啟用檔案拖放")
        self.file_drop_checkbox.setChecked(True)
        layout.addRow(self.file_drop_checkbox)

        self.supported_files_edit = QLineEdit("*.txt, *.pdf, *.doc, *.jpg, *.png")
        self.supported_files_edit.setPlaceholderText("例:*.txt, *.pdf, *.jpg")
        layout.addRow("支援檔案類型:", self.supported_files_edit)

        self.drop_action_combo = QComboBox()
        self.drop_action_combo.addItems(["分析檔案", "開啟檔案", "詢問動作"])
        layout.addRow("拖放動作:", self.drop_action_combo)

        return self._loose_group(group)

    def create_notification_group(self):
        group = QGroupBox("通知設定")
        group.setObjectName("settingsGroup")
        layout = QFormLayout(group)
        layout.setSpacing(15)
        layout.setContentsMargins(20, 25, 20, 20)

        self.notifications_checkbox = QCheckBox("啟用系統通知")
        self.notifications_checkbox.setChecked(True)
        layout.addRow(self.notifications_checkbox)

        self.notification_position_combo = QComboBox()
        self.notification_position_combo.addItems(["右下角", "右上角", "左上角", "左下角", "中央"])
        layout.addRow("通知位置:", self.notification_position_combo)

        self.notification_duration_spinbox = QSpinBox()
        self.notification_duration_spinbox.setRange(1, 30)
        self.notification_duration_spinbox.setValue(5)
        self.notification_duration_spinbox.setSuffix("秒")
        layout.addRow("顯示時間:", self.notification_duration_spinbox)

        return self._loose_group(group)

    def create_advanced_settings_group(self):
        group = QGroupBox("進階設定")
        group.setObjectName("settingsGroup")
        layout = QFormLayout(group)
        layout.setSpacing(15)
        layout.setContentsMargins(20, 25, 20, 20)

        self.developer_mode_checkbox = QCheckBox("開發者模式")
        self.developer_mode_checkbox.setChecked(False)
        layout.addRow(self.developer_mode_checkbox)

        self.debug_logging_checkbox = QCheckBox("啟用詳細日誌")
        self.debug_logging_checkbox.setChecked(False)
        layout.addRow(self.debug_logging_checkbox)

        self.performance_monitor_checkbox = QCheckBox("效能監控")
        self.performance_monitor_checkbox.setChecked(False)
        layout.addRow(self.performance_monitor_checkbox)

        self.auto_update_checkbox = QCheckBox("自動檢查更新")
        self.auto_update_checkbox.setChecked(True)
        layout.addRow(self.auto_update_checkbox)

        return self._loose_group(group)

    def create_data_privacy_group(self):
        group = QGroupBox("資料與隱私")
        group.setObjectName("settingsGroup")
        layout = QVBoxLayout(group)
        layout.setContentsMargins(20, 25, 20, 20)
        layout.setSpacing(15)

        info_label = QLabel("UEP重視您的隱私，所有資料均在本地處理")
        info_label.setObjectName("successText")
        layout.addWidget(info_label)

        form_layout = QFormLayout()
        form_layout.setSpacing(15)

        self.save_conversations_checkbox = QCheckBox("保存對話記錄")
        self.save_conversations_checkbox.setChecked(True)
        form_layout.addRow(self.save_conversations_checkbox)

        self.data_retention_spinbox = QSpinBox()
        self.data_retention_spinbox.setRange(1, 365)
        self.data_retention_spinbox.setValue(30)
        self.data_retention_spinbox.setSuffix("天")
        form_layout.addRow("資料保留時間:", self.data_retention_spinbox)

        layout.addLayout(form_layout)

        button_layout = QHBoxLayout()
        self.clear_data_button = QPushButton("清除所有資料")
        self.export_data_button = QPushButton("匯出資料")

        button_layout.addWidget(self.clear_data_button)
        button_layout.addWidget(self.export_data_button)
        button_layout.addStretch()

        layout.addLayout(button_layout)

        return self._loose_group(group)

    def create_maintenance_group(self):
        group = QGroupBox("系統維護")
        group.setObjectName("settingsGroup")
        layout = QVBoxLayout(group)
        layout.setContentsMargins(20, 25, 20, 20)
        layout.setSpacing(15)

        button_layout = QGridLayout()
        button_layout.setSpacing(10)

        self.restart_button = QPushButton("重新啟動UEP")
        self.reset_settings_button = QPushButton("重置所有設定")
        self.check_updates_button = QPushButton("檢查更新")
        self.repair_system_button = QPushButton("系統修復")

        button_layout.addWidget(self.restart_button, 0, 0)
        button_layout.addWidget(self.reset_settings_button, 0, 1)
        button_layout.addWidget(self.check_updates_button, 1, 0)
        button_layout.addWidget(self.repair_system_button, 1, 1)

        layout.addLayout(button_layout)

        return self._loose_group(group)

    def create_about_group(self):
        group = QGroupBox("關於UEP")
        group.setObjectName("settingsGroup")
        layout = QVBoxLayout(group)
        layout.setContentsMargins(20, 25, 20, 20)
        layout.setSpacing(15)

        info_text = """
        <h3 style='margin-bottom:10px;'>UEP (Unforgettable Eternal Project)</h3>
        <p style='margin:5px 0;'><b>版本:</b> 1.0.0</p>
        <p style='margin:5px 0;'><b>開發團隊:</b> UEP開發組</p>
        <p style='margin:5px 0;'><b>授權:</b> MIT License</p>
        <br>
        <p style='margin:5px 0;'>UEP是一個智慧型桌面助理系統，旨在提供自然、直觀的人機互動體驗。</p>
        """

        info_label = QLabel(info_text)
        info_label.setWordWrap(True)
        info_label.setOpenExternalLinks(True)
        layout.addWidget(info_label)

        button_layout = QHBoxLayout()
        self.website_button = QPushButton("官方網站")
        self.license_button = QPushButton("授權資訊")
        self.help_button = QPushButton("說明文件")

        button_layout.addWidget(self.website_button)
        button_layout.addWidget(self.license_button)
        button_layout.addWidget(self.help_button)
        button_layout.addStretch()

        layout.addLayout(button_layout)

        return self._loose_group(group)

    def create_bottom_buttons(self, parent_layout):
        button_frame = QFrame()
        button_frame.setObjectName("bottomBar")
        button_frame.setFixedHeight(70)
        button_layout = QHBoxLayout(button_frame)
        button_layout.setContentsMargins(30, 15, 30, 15)

        self.minimize_to_orb_button = QPushButton("最小化到球體")
        self.minimize_to_orb_button.clicked.connect(self.minimize_to_orb)

        self.apply_button = QPushButton("套用")
        self.ok_button = QPushButton("確定")
        self.cancel_button = QPushButton("取消")

        self.apply_button.clicked.connect(self.apply_settings)
        self.ok_button.clicked.connect(self.ok_clicked)
        self.cancel_button.clicked.connect(self.cancel_clicked)

        button_layout.addWidget(self.minimize_to_orb_button)
        button_layout.addStretch()
        button_layout.addWidget(self.apply_button)
        button_layout.addWidget(self.ok_button)
        button_layout.addWidget(self.cancel_button)

        parent_layout.addWidget(button_frame)

    def create_status_bar(self):
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("準備就緒")

        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        self.status_bar.addPermanentWidget(self.progress_bar)

    def _tall_scroll(self, scroll_area: QScrollArea):
        scroll_area.setWidgetResizable(True)
        scroll_area.setFrameShape(QFrame.NoFrame)
        scroll_area.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        scroll_area.setMinimumHeight(self.SCROLL_AREA_MIN_H if hasattr(self, 'SCROLL_AREA_MIN_H') else 620)
        scroll_area.setAlignment(Qt.AlignTop)

    def toggle_theme(self):
        new_theme = Theme.DARK if theme_manager.theme == Theme.LIGHT else Theme.LIGHT
        theme_manager.set_theme(new_theme)
        self.dark_mode = (new_theme == Theme.DARK)
        self.theme_toggle.setText("☀️" if self.dark_mode else "🌙")

    def _on_global_theme_changed(self, theme_str: str):
        try:
            t = Theme(theme_str)
        except ValueError:
            t = Theme.LIGHT
        self.dark_mode = (t == Theme.DARK)
        self.theme_toggle.setText("☀️" if self.dark_mode else "🌙")

    def load_settings(self):
        try:
            self.uep_name_edit.setText(self.settings.value("personal/uep_name", "UEP"))
            self.user_name_edit.setText(self.settings.value("personal/user_name", "使用者"))
            self.enable_tts_checkbox.setChecked(self.settings.value("performance/enable_tts", True, type=bool))
            self.tts_volume_slider.setValue(self.settings.value("performance/tts_volume", 70, type=int))
            self.enable_movement_checkbox.setChecked(self.settings.value("behavior/enable_movement", True, type=bool))
            self.mouse_hover_checkbox.setChecked(self.settings.value("interaction/mouse_hover", True, type=bool))
            info_log("[UserMainWindow] 設定載入完成")
        except Exception as e:
            error_log(f"[UserMainWindow] 載入設定時發生錯誤:{e}")

    def save_settings(self):
        try:
            self.settings.setValue("personal/uep_name", self.uep_name_edit.text())
            self.settings.setValue("personal/user_name", self.user_name_edit.text())
            self.settings.setValue("performance/enable_tts", self.enable_tts_checkbox.isChecked())
            self.settings.setValue("performance/tts_volume", self.tts_volume_slider.value())
            self.settings.setValue("behavior/enable_movement", self.enable_movement_checkbox.isChecked())
            self.settings.setValue("interaction/mouse_hover", self.mouse_hover_checkbox.isChecked())
            self.settings.sync()
            info_log("[UserMainWindow] 設定保存完成")
        except Exception as e:
            error_log(f"[UserMainWindow] 保存設定時發生錯誤:{e}")

    def apply_settings(self):
        self.save_settings()
        self.status_bar.showMessage("設定已套用", 3000)
        self.settings_changed.emit("applied", None)
        debug_log(OPERATION_LEVEL, "[UserMainWindow] 設定已套用")

    def ok_clicked(self):
        self.apply_settings()
        debug_log(OPERATION_LEVEL, "[UserMainWindow] 用戶點擊確定按鈕，關閉視窗")
        self.close()

    def cancel_clicked(self):
        debug_log(OPERATION_LEVEL, "[UserMainWindow] 用戶點擊取消按鈕，重新載入設定")
        self.load_settings()
        self.close()

    def minimize_to_orb(self):
        self.is_minimized_to_orb = True
        self.original_geometry = self.geometry()
        self.hide()
        self.action_triggered.emit("minimize_to_orb", {})
        debug_log(OPERATION_LEVEL, "[UserMainWindow] 已最小化到圓球")

    def restore_from_orb(self):
        if self.is_minimized_to_orb:
            if self.original_geometry:
                self.setGeometry(self.original_geometry)
            self.show()
            self.raise_()
            self.activateWindow()
            self.is_minimized_to_orb = False
            debug_log(OPERATION_LEVEL, "[UserMainWindow] 從圓球恢復視窗")

    def closeEvent(self, event):
        if not self.is_minimized_to_orb:
            debug_log(OPERATION_LEVEL, "[UserMainWindow] 視窗關閉請求，最小化到圓球")
            self.minimize_to_orb()
            event.ignore()
        else:
            info_log("[UserMainWindow] 視窗正在關閉")
            self.window_closed.emit()
            event.accept()

    def show_settings_page(self, page_name: str):
        page_map = {"personal": 0, "performance": 1, "behavior": 2, "interaction": 3, "other": 4}
        if page_name in page_map:
            self.tab_widget.setCurrentIndex(page_map[page_name])
            if self.is_minimized_to_orb:
                self.restore_from_orb()
            debug_log(OPERATION_LEVEL, f"[UserMainWindow] 顯示設定頁面:{page_name}")

    def update_system_info(self, info: Dict[str, Any]):
        try:
            if "status" in info:
                self.system_status_label.setText(info["status"])
                if info["status"] == "正常運行":
                    self.system_status_label.setObjectName("statusOk")
                else:
                    self.system_status_label.setStyleSheet("color:#f44336; font-weight:700;")
            if "uptime" in info:
                self.uptime_label.setText(info["uptime"])
            if "memory" in info:
                self.memory_label.setText(f"{info['memory']}MB")
            if "cpu" in info:
                self.cpu_label.setText(f"{info['cpu']}%")
            if "active_modules" in info:
                self.active_modules_label.setText(", ".join(info["active_modules"]))
        except Exception as e:
            error_log(f"[UserMainWindow] 更新系統資訊時發生錯誤:{e}")

    def handle_request(self, data: dict) -> dict:
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
                info_log(f"[UserMainWindow] 已更新設定:{list(settings.keys())}")
                return {"success": True, "updated_settings": list(settings.keys())}
            elif command == 'get_settings':
                current_settings = {}
                debug_log(OPERATION_LEVEL, "[UserMainWindow] 獲取當前設定")
                return {"success": True, "settings": current_settings}
            elif command == 'show_page':
                page_name = data.get('page_name')
                if page_name:
                    self.show_settings_page(page_name)
                    return {"success": True, "message": f"已切換到{page_name}頁面"}
                return {"error": "需要指定page_name參數"}
            elif command == 'update_system_info':
                info = data.get('info', {})
                self.update_system_info(info)
                return {"success": True, "message": "系統資訊已更新"}
            else:
                return {"error": f"未知命令:{command}"}
        except Exception as e:
            error_log(f"[UserMainWindow] 載入設定時發生錯誤:{e}")
            return {"error": str(e)}


def create_test_window():
    if not PYQT5_AVAILABLE:
        error_log("[UserMainWindow] PyQt5不可用，無法創建測試視窗")
        return None
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    theme_manager.apply_app()
    window = UserMainWindow()
    window.show()
    return app, window


if __name__ == "__main__":
    app, window = create_test_window()
    if app and window:
        sys.exit(app.exec_())
