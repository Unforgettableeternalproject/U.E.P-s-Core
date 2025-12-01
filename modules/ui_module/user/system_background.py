#system_background.py
import os
import sys
from typing import Dict, Any, Optional
from datetime import datetime
from .theme_manager import theme_manager, Theme

from configs.config_loader import load_config
from utils.debug_helper import debug_log, info_log, error_log, OPERATION_LEVEL
from modules.sys_module.actions.monitoring_interface import get_monitoring_interface
from modules.sys_module.actions.monitoring_events import MonitoringEventType, MonitoringEventData

try:
    from PyQt5.QtWidgets import (QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
                                QTabWidget, QLabel, QGroupBox, QScrollArea, QAbstractScrollArea,
                                QFrame, QPushButton, QCheckBox, QSpinBox, QSizePolicy, 
                                QSlider, QComboBox, QLineEdit, QTextEdit,
                                QSplitter, QTreeWidget, QTreeWidgetItem,
                                QFormLayout, QGridLayout, QSizePolicy,
                                QApplication, QMessageBox, QFileDialog,
                                QProgressBar, QStatusBar, QMenuBar,
                                QToolBar, QAction, QButtonGroup, QListWidget,
                                QListWidgetItem, QDialog, QDialogButtonBox, 
                                QDateTimeEdit)
    from PyQt5.QtCore import (Qt, QTimer, pyqtSignal, QSize, QRect,
                             QPropertyAnimation, QEasingCurve, QThread,
                             QSettings, QStandardPaths, QDateTime)
    from PyQt5.QtGui import (QIcon, QFont, QPixmap, QPalette, QColor,
                            QPainter, QLinearGradient, QBrush)
    PYQT5_AVAILABLE = True
except ImportError:
    PYQT5_AVAILABLE = False
    debug_log(2, "[SystemBackground] PyQt5 不可用")


class SystemBackgroundWindow(QMainWindow):
    settings_changed = pyqtSignal(str, object)
    action_triggered = pyqtSignal(str, dict)
    window_closed = pyqtSignal()

    def __init__(self, ui_module=None):
        super().__init__()

        if not PYQT5_AVAILABLE:
            debug_log(2, "[SystemBackground] PyQt5不可用，無法初始化")
            return

        self.ui_module = ui_module
        self.settings = QSettings("UEP", "SystemBackground")
        
        self.is_minimized_to_orb = False
        self.original_geometry = None

        # 音樂播放器狀態
        self.current_music_player = None
        self.is_music_playing = False
        self.current_playlist = []  # 當前播放列表
        self.current_track_index = -1  # 當前播放索引
        self.current_volume = 70  # 當前音量
        
        # 對話記錄
        self.dialog_history = []
        
        # 監控接口 - 根據全域 debug 配置決定是否使用 Mock 模式
        config = load_config()
        debug_config = config.get('debug', {})
        self.use_mock_data = debug_config.get('enabled', False)
        
        self.mock_todos = []  # Mock 待辦事項列表
        self.mock_calendar_events = []  # Mock 行事曆列表
        self.mock_id_counter = 1  # Mock ID 計數器
        
        if self.use_mock_data:
            debug_log(OPERATION_LEVEL, "[SystemBackground] Debug 模式已啟用，使用 Mock 資料模式")
            self.monitoring_interface = None
            self._initialize_mock_data()
            debug_log(OPERATION_LEVEL, f"[SystemBackground] Mock 資料已初始化: {len(self.mock_todos)} 個待辦, {len(self.mock_calendar_events)} 個行事曆")
        else:
            try:
                self.monitoring_interface = get_monitoring_interface()
                debug_log(OPERATION_LEVEL, "[SystemBackground] 成功連接到監控後端，使用真實資料模式")
            except Exception as e:
                error_log(f"[SystemBackground] 無法連接到監控後端: {e}")
                self.monitoring_interface = None
        
        self._monitoring_data = None  # 儲存快照資料

        self.init_ui()
        self._wire_theme_manager()
        self.load_settings()
        
        # 訂閱監控事件
        self._subscribe_monitoring_events()
        
        # 載入初始資料快照
        self._load_monitoring_snapshot()
        
        # 載入預設媒體資料夾的音樂
        self._load_default_music_folder()
        
        # 播放進度追蹤定時器
        from PyQt5.QtCore import QTimer
        self.progress_timer = QTimer(self)
        self.progress_timer.timeout.connect(self._update_playback_progress)
        self.progress_timer.start(500)  # 每 500ms 更新一次
        
        debug_log(OPERATION_LEVEL, "[SystemBackground] 系統背景視窗初始化完成")

    def init_ui(self):
        mode_suffix = " (Mock 模式)" if self.use_mock_data else ""
        self.setWindowTitle(f"UEP系統背景{mode_suffix}")
        self.setMinimumSize(900, 700)
        self.resize(1000, 750)

        try:
            icon_path = os.path.join(os.path.dirname(__file__), "../../../resources/assets/static/Logo.ico")
            if os.path.exists(icon_path):   
                self.setWindowIcon(QIcon(icon_path))
        except Exception as e:
            debug_log(2, f"[SystemBackground] 無法載入圖標: {e}")

        central_widget = QWidget()
        self.setCentralWidget(central_widget)

        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        self.create_header(main_layout)
        self.create_tab_widget(main_layout)
        self.create_bottom_buttons(main_layout)
        self.create_status_bar()

    def _wire_theme_manager(self):
        try:
            theme_manager.apply_app()
            theme_manager.theme_changed.connect(self._on_theme_changed)
        except Exception as e:
            debug_log(2, f"[SystemBackground] 無法連接 theme_changed: {e}")

        self._on_theme_changed()

    def _on_theme_changed(self, name: str = None):
        is_dark = self._tm_is_dark(name)
        self.theme_toggle.setText("☀️" if is_dark else "🌙")

    def _tm_is_dark(self, name: str = None) -> bool:
        try:
            if isinstance(name, str):
                return name.lower() == "dark"
            cur = getattr(theme_manager, "current", None)
            if cur is not None:
                if isinstance(cur, Theme):
                    return cur == Theme.DARK
                if isinstance(cur, str):
                    return cur.lower() == "dark"
            getter = getattr(theme_manager, "current_theme", None) or getattr(theme_manager, "get_theme", None)
            if callable(getter):
                val = getter()
                if isinstance(val, Theme):
                    return val == Theme.DARK
                if isinstance(val, str):
                    return val.lower() == "dark"
        except Exception:
            pass
        return False

    def _tall_scroll(self, scroll_area: QScrollArea):
        scroll_area.setWidgetResizable(True)
        scroll_area.setFrameShape(QFrame.NoFrame)
        scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll_area.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        scroll_area.setAlignment(Qt.AlignTop)

    def create_header(self, parent_layout):
        header = QFrame()
        header.setObjectName("header")
        header.setFixedHeight(110)

        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(30, 16, 30, 16)
        header_layout.setSpacing(16)

        title_container = QVBoxLayout()
        title_label = QLabel("系統背景")
        title_label.setObjectName("mainTitle")
        title_label.setMinimumHeight(34)
        subtitle_label = QLabel("整合工作區與娛樂中心")
        subtitle_label.setObjectName("subtitle")
        subtitle_label.setWordWrap(False)

        title_container.addWidget(title_label)
        title_container.addWidget(subtitle_label)
        title_container.addStretch()

        header_layout.addLayout(title_container)
        header_layout.addStretch()

        # 主題切換按鈕
        self.theme_toggle = QPushButton("🌙")
        self.theme_toggle.setObjectName("themeToggle")
        self.theme_toggle.setFixedSize(56, 56)
        self.theme_toggle.setCursor(Qt.PointingHandCursor)

        btn_font = QFont("Segoe UI Emoji, Apple Color Emoji, Noto Color Emoji")
        btn_font.setPointSize(20)
        self.theme_toggle.setFont(btn_font)

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

        # 創建各個分頁
        self.create_reminder_tab()
        self.create_calendar_tab()
        self.create_music_tab()
        self.create_folder_monitor_tab()

        parent_layout.addWidget(self.tab_widget, 1)

    def create_reminder_tab(self):
        reminder_widget = QWidget()
        reminder_layout = QVBoxLayout(reminder_widget)
        reminder_layout.setContentsMargins(30, 30, 30, 30)
        reminder_layout.setSpacing(20)

        scroll_area = QScrollArea()
        self._tall_scroll(scroll_area)

        #scroll area
        scroll_content = QWidget()
        scroll_content.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
        scroll_layout = QVBoxLayout(scroll_content)
        scroll_layout.setContentsMargins(0, 0, 0, 0)
        scroll_layout.setSpacing(20)

        # 今日任務組
        today_group = self.create_today_tasks_group()
        scroll_layout.addWidget(today_group)

        # 分類任務組
        sorting_group = self.create_sorting_tasks_group()
        scroll_layout.addWidget(sorting_group)

        # 過期任務組
        expired_group = self.create_expired_tasks_group()
        scroll_layout.addWidget(expired_group)

        scroll_layout.addStretch()
        scroll_area.setWidget(scroll_content)
        reminder_layout.addWidget(scroll_area, 1)

        self.tab_widget.addTab(reminder_widget, "📋 待辦事項")

    def create_today_tasks_group(self):
        group = QGroupBox("今日任務")
        group.setObjectName("settingsGroup")
        layout = QVBoxLayout(group)
        layout.setContentsMargins(20, 25, 20, 20)
        layout.setSpacing(15)

        # 任務列表
        self.today_tasks_list = QListWidget()
        self.today_tasks_list.setStyleSheet("""
            QListWidget {
                background: transparent;
                border: 1px solid #2f3136;
                border-radius: 8px;
            }
            QListWidget::item {
                padding: 10px;
                border-bottom: 1px solid #2f3136;
            }
            QListWidget::item:hover {
                background: #232427;
            }
        """)
        # 連接雙擊編輯和右鍵選單
        self.today_tasks_list.itemDoubleClicked.connect(self.edit_todo_item)
        self.today_tasks_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.today_tasks_list.customContextMenuRequested.connect(lambda pos: self.show_todo_context_menu(pos, self.today_tasks_list))
        layout.addWidget(self.today_tasks_list)

        # 按鈕區
        button_layout = QHBoxLayout()
        add_task_btn = QPushButton("➕ 新增任務")
        refresh_btn = QPushButton("🔄 重新整理")
        
        add_task_btn.clicked.connect(self.add_new_task)
        refresh_btn.clicked.connect(self.refresh_today_tasks)

        button_layout.addWidget(add_task_btn)
        button_layout.addWidget(refresh_btn)
        button_layout.addStretch()
        
        layout.addLayout(button_layout)

        return self._loose_group(group)

    def create_sorting_tasks_group(self):
        group = QGroupBox("任務分類")
        group.setObjectName("settingsGroup")
        layout = QVBoxLayout(group)
        layout.setContentsMargins(20, 25, 20, 20)
        layout.setSpacing(15)

        # 優先級篩選
        filter_layout = QHBoxLayout()
        filter_label = QLabel("篩選:")
        self.priority_filter = QComboBox()
        self.priority_filter.addItems(["全部", "高優先級", "中優先級", "低優先級"])
        self.priority_filter.currentTextChanged.connect(self.filter_tasks_by_priority)
        
        filter_layout.addWidget(filter_label)
        filter_layout.addWidget(self.priority_filter)
        filter_layout.addStretch()
        
        layout.addLayout(filter_layout)

        # 分類任務樹狀圖
        self.sorting_tree = QTreeWidget()
        self.sorting_tree.setHeaderLabels(["任務", "優先級", "日期"])
        self.sorting_tree.setStyleSheet("""
            QTreeWidget {
                background: transparent;
                border: 1px solid #2f3136;
                border-radius: 8px;
            }
        """)
        layout.addWidget(self.sorting_tree)

        return self._loose_group(group)

    def create_expired_tasks_group(self):
        group = QGroupBox("過期任務")
        group.setObjectName("settingsGroup")
        layout = QVBoxLayout(group)
        layout.setContentsMargins(20, 25, 20, 20)
        layout.setSpacing(15)

        info_label = QLabel("⚠️ 這些任務已逾期，請盡快處理")
        info_label.setObjectName("infoText")
        layout.addWidget(info_label)

        self.expired_tasks_list = QListWidget()
        self.expired_tasks_list.setStyleSheet("""
            QListWidget {
                background: transparent;
                border: 1px solid #f44336;
                border-radius: 8px;
            }
            QListWidget::item {
                padding: 10px;
                color: #f44336;
            }
        """)
        layout.addWidget(self.expired_tasks_list)

        clear_btn = QPushButton("🗑️ 清除已完成")
        clear_btn.clicked.connect(self.clear_expired_tasks)
        layout.addWidget(clear_btn)

        return self._loose_group(group)

    def create_calendar_tab(self):
        calendar_widget = QWidget()
        calendar_layout = QVBoxLayout(calendar_widget)
        calendar_layout.setContentsMargins(30, 30, 30, 30)
        calendar_layout.setSpacing(20)

        scroll_area = QScrollArea()
        self._tall_scroll(scroll_area)

        #scroll area
        scroll_content = QWidget()
        scroll_content.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
        scroll_layout = QVBoxLayout(scroll_content)
        scroll_layout.setContentsMargins(0, 0, 0, 0)
        scroll_layout.setSpacing(20)

        # 行事曆概覽
        calendar_overview_group = self.create_calendar_overview_group()
        scroll_layout.addWidget(calendar_overview_group)

        # 排程管理
        scheduler_group = self.create_scheduler_group()
        scroll_layout.addWidget(scheduler_group)

        scroll_layout.addStretch()
        scroll_area.setWidget(scroll_content)
        calendar_layout.addWidget(scroll_area, 1)

        self.tab_widget.addTab(calendar_widget, "📅 行事曆")

    def create_calendar_overview_group(self):
        group = QGroupBox("本週行程")
        group.setObjectName("settingsGroup")
        layout = QVBoxLayout(group)
        layout.setContentsMargins(20, 25, 20, 20)
        layout.setSpacing(15)

        # 週視圖
        self.week_view = QTreeWidget()
        self.week_view.setHeaderLabels(["日期", "事件", "時間"])
        self.week_view.setStyleSheet("""
            QTreeWidget {
                background: transparent;
                border: 1px solid #2f3136;
                border-radius: 8px;
            }
        """)
        # 連接雙擊編輯和右鍵選單
        self.week_view.itemDoubleClicked.connect(self.edit_calendar_event_item)
        self.week_view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.week_view.customContextMenuRequested.connect(lambda pos: self.show_calendar_context_menu(pos, self.week_view))
        layout.addWidget(self.week_view)

        # 快速操作
        button_layout = QHBoxLayout()
        add_event_btn = QPushButton("➕ 新增事件")
        sync_btn = QPushButton("🔄 同步日曆")
        
        add_event_btn.clicked.connect(self.add_calendar_event)
        sync_btn.clicked.connect(self.sync_calendar)

        button_layout.addWidget(add_event_btn)
        button_layout.addWidget(sync_btn)
        button_layout.addStretch()
        
        layout.addLayout(button_layout)

        return self._loose_group(group)



    def create_scheduler_group(self):
        group = QGroupBox("系統排程")
        group.setObjectName("settingsGroup")
        layout = QVBoxLayout(group)
        layout.setContentsMargins(20, 25, 20, 20)
        layout.setSpacing(15)

        info_label = QLabel("管理系統定時任務與腳本")
        info_label.setObjectName("infoText")
        layout.addWidget(info_label)

        # 排程列表
        self.scheduler_tree = QTreeWidget()
        self.scheduler_tree.setHeaderLabels(["任務名稱", "排程", "狀態"])
        self.scheduler_tree.setStyleSheet("""
            QTreeWidget {
                background: transparent;
                border: 1px solid #2f3136;
                border-radius: 8px;
            }
        """)
        layout.addWidget(self.scheduler_tree)

        # 控制按鈕
        button_layout = QHBoxLayout()
        register_btn = QPushButton("➕ 註冊任務")
        run_btn = QPushButton("▶️ 執行")
        delete_btn = QPushButton("🗑️ 刪除")

        button_layout.addWidget(register_btn)
        button_layout.addWidget(run_btn)
        button_layout.addWidget(delete_btn)
        button_layout.addStretch()
        
        layout.addLayout(button_layout)

        return self._loose_group(group)

    def create_music_tab(self):
        music_widget = QWidget()
        music_layout = QVBoxLayout(music_widget)
        music_layout.setContentsMargins(30, 30, 30, 30)
        music_layout.setSpacing(20)

        scroll_area = QScrollArea()
        self._tall_scroll(scroll_area)

        #scroll area
        scroll_content = QWidget()
        scroll_content.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
        scroll_layout = QVBoxLayout(scroll_content)
        scroll_layout.setContentsMargins(0, 0, 0, 0)
        scroll_layout.setSpacing(20)

        # 正在播放
        now_playing_group = self.create_now_playing_group()
        scroll_layout.addWidget(now_playing_group)

        # 播放控制
        playback_control_group = self.create_playback_control_group()
        scroll_layout.addWidget(playback_control_group)

        # 播放列表
        playlist_group = self.create_playlist_group()
        scroll_layout.addWidget(playlist_group)

        # 搜尋與來源
        music_source_group = self.create_music_source_group()
        scroll_layout.addWidget(music_source_group)

        scroll_layout.addStretch()
        scroll_area.setWidget(scroll_content)
        music_layout.addWidget(scroll_area, 1)

        self.tab_widget.addTab(music_widget, "🎵 音樂")

    def create_now_playing_group(self):
        group = QGroupBox("正在播放")
        group.setObjectName("settingsGroup")
        layout = QVBoxLayout(group)
        layout.setContentsMargins(20, 25, 20, 20)
        layout.setSpacing(15)

        # 專輯封面區域
        cover_container = QWidget()
        cover_layout = QVBoxLayout(cover_container)
        cover_layout.setAlignment(Qt.AlignCenter)
        
        self.album_cover_label = QLabel()
        self.album_cover_label.setFixedSize(280, 280)
        self.album_cover_label.setAlignment(Qt.AlignCenter)
        self.album_cover_label.setStyleSheet("""
            QLabel {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 #739ef0, stop:1 #346ddb);
                border-radius: 20px;
                border: 3px solid #2f3136;
            }
        """)
        
        # 預設封面圖示
        default_cover_text = QLabel("🎵")
        default_cover_text.setAlignment(Qt.AlignCenter)
        default_cover_font = QFont()
        default_cover_font.setPointSize(72)
        default_cover_text.setFont(default_cover_font)
        
        cover_layout.addWidget(self.album_cover_label)
        layout.addWidget(cover_container)

        # 歌曲資訊
        info_layout = QVBoxLayout()
        info_layout.setSpacing(8)
        
        self.song_title_label = QLabel("未播放任何歌曲")
        self.song_title_label.setObjectName("mainTitle")
        self.song_title_label.setAlignment(Qt.AlignCenter)
        self.song_title_label.setStyleSheet("font-size: 20px; font-weight: 700;")
        
        self.song_artist_label = QLabel("選擇歌曲開始播放")
        self.song_artist_label.setObjectName("subtitle")
        self.song_artist_label.setAlignment(Qt.AlignCenter)
        
        info_layout.addWidget(self.song_title_label)
        info_layout.addWidget(self.song_artist_label)
        
        layout.addLayout(info_layout)

        return self._loose_group(group)

    def create_playback_control_group(self):
        """創建播放控制組"""
        group = QGroupBox("播放控制")
        group.setObjectName("settingsGroup")
        layout = QVBoxLayout(group)
        layout.setContentsMargins(20, 25, 20, 20)
        layout.setSpacing(15)

        # 進度條
        progress_layout = QVBoxLayout()
        self.progress_slider = QSlider(Qt.Horizontal)
        self.progress_slider.setRange(0, 100)
        self.progress_slider.setValue(0)
        self.progress_slider.sliderMoved.connect(self._seek_playback)
        self.progress_slider.setEnabled(False)  # 預設禁用，播放時啟用
        
        time_layout = QHBoxLayout()
        self.current_time_label = QLabel("0:00")
        self.total_time_label = QLabel("0:00")
        time_layout.addWidget(self.current_time_label)
        time_layout.addStretch()
        time_layout.addWidget(self.total_time_label)
        
        progress_layout.addWidget(self.progress_slider)
        progress_layout.addLayout(time_layout)
        layout.addLayout(progress_layout)

        # 播放控制按鈕
        control_layout = QHBoxLayout()
        control_layout.setSpacing(15)
        control_layout.setAlignment(Qt.AlignCenter)

        self.previous_btn = QPushButton("⏮️")
        self.previous_btn.setFixedSize(50, 50)
        self.previous_btn.clicked.connect(self.play_previous_song)

        self.play_pause_btn = QPushButton("▶️")
        self.play_pause_btn.setFixedSize(70, 70)
        self.play_pause_btn.clicked.connect(self.toggle_music_playback)

        self.next_btn = QPushButton("⏭️")
        self.next_btn.setFixedSize(50, 50)
        self.next_btn.clicked.connect(self.play_next_song)

        self.loop_btn = QPushButton("🔂")
        self.loop_btn.setFixedSize(50, 50)
        self.loop_btn.setCheckable(True)
        self.loop_btn.clicked.connect(self.toggle_loop_mode)

        control_layout.addWidget(self.previous_btn)
        control_layout.addWidget(self.play_pause_btn)
        control_layout.addWidget(self.next_btn)
        control_layout.addWidget(self.loop_btn)

        layout.addLayout(control_layout)

        # 音量控制
        volume_layout = QHBoxLayout()
        volume_icon = QLabel("🔊")
        self.volume_slider = QSlider(Qt.Horizontal)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setValue(70)
        self.volume_label = QLabel("70%")
        
        volume_layout.addWidget(volume_icon)
        volume_layout.addWidget(self.volume_slider)
        volume_layout.addWidget(self.volume_label)
        
        self.volume_slider.valueChanged.connect(self.adjust_volume)
        
        layout.addLayout(volume_layout)

        return self._loose_group(group)

    def create_playlist_group(self):
        """創建播放列表組"""
        group = QGroupBox("播放列表")
        group.setObjectName("settingsGroup")
        layout = QVBoxLayout(group)
        layout.setContentsMargins(20, 25, 20, 20)
        layout.setSpacing(15)

        self.playlist_widget = QListWidget()
        self.playlist_widget.setStyleSheet("""
            QListWidget {
                background: transparent;
                border: 1px solid #2f3136;
                border-radius: 8px;
            }
            QListWidget::item {
                padding: 12px;
                border-bottom: 1px solid #2f3136;
            }
            QListWidget::item:hover {
                background: #232427;
            }
            QListWidget::item:selected {
                background: #739ef0;
                color: #ffffff;
            }
        """)
        self.playlist_widget.itemDoubleClicked.connect(self.play_selected_song)
        layout.addWidget(self.playlist_widget)

        # 列表控制
        button_layout = QHBoxLayout()
        add_file_btn = QPushButton("➕ 新增檔案")
        add_folder_btn = QPushButton("📁 新增資料夾")
        clear_btn = QPushButton("🗑️ 清空列表")

        add_file_btn.clicked.connect(self.add_music_file)
        add_folder_btn.clicked.connect(self.add_music_folder)
        clear_btn.clicked.connect(self.clear_playlist)

        button_layout.addWidget(add_file_btn)
        button_layout.addWidget(add_folder_btn)
        button_layout.addWidget(clear_btn)
        button_layout.addStretch()

        layout.addLayout(button_layout)

        return self._loose_group(group)

    def create_music_source_group(self):
        group = QGroupBox("音樂來源")
        group.setObjectName("settingsGroup")
        layout = QVBoxLayout(group)
        layout.setContentsMargins(20, 25, 20, 20)
        layout.setSpacing(15)

        # 搜尋框
        search_layout = QHBoxLayout()
        self.music_search_input = QLineEdit()
        self.music_search_input.setPlaceholderText("搜尋歌曲...")
        search_btn = QPushButton("🔍 搜尋")
        search_btn.clicked.connect(self.search_music)

        search_layout.addWidget(self.music_search_input)
        search_layout.addWidget(search_btn)
        
        layout.addLayout(search_layout)

        # 來源選擇
        source_layout = QHBoxLayout()
        source_label = QLabel("播放來源:")
        self.music_source_combo = QComboBox()
        self.music_source_combo.addItems(["本地檔案", "YouTube", "Spotify"])

        source_layout.addWidget(source_label)
        source_layout.addWidget(self.music_source_combo)
        source_layout.addStretch()

        layout.addLayout(source_layout)

        # 快速操作
        quick_layout = QHBoxLayout()
        youtube_btn = QPushButton("▶️ YouTube")
        spotify_btn = QPushButton("🎵 Spotify")

        youtube_btn.clicked.connect(self.open_youtube)
        spotify_btn.clicked.connect(self.open_spotify)

        quick_layout.addWidget(youtube_btn)
        quick_layout.addWidget(spotify_btn)
        quick_layout.addStretch()

        layout.addLayout(quick_layout)

        return self._loose_group(group)

    def create_folder_monitor_tab(self):
        """資料夾監控分頁（即將推出）"""
        folder_widget = QWidget()
        folder_layout = QVBoxLayout(folder_widget)
        folder_layout.setContentsMargins(30, 30, 30, 30)
        folder_layout.setSpacing(20)

        # Coming Soon 提示
        coming_soon_label = QLabel("📁 資料夾監控功能")
        coming_soon_label.setObjectName("mainTitle")
        coming_soon_label.setAlignment(Qt.AlignCenter)
        folder_layout.addWidget(coming_soon_label)

        info_label = QLabel("此功能即將推出\n\n將支援監控指定資料夾的檔案變化\n自動整理和管理檔案")
        info_label.setObjectName("subtitle")
        info_label.setAlignment(Qt.AlignCenter)
        info_label.setWordWrap(True)
        folder_layout.addWidget(info_label)

        folder_layout.addStretch()

        self.tab_widget.addTab(folder_widget, "📁 資料夾監控")

    def create_current_dialog_group(self):
        """創建當前對話組"""
        group = QGroupBox("當前對話")
        group.setObjectName("settingsGroup")
        layout = QVBoxLayout(group)
        layout.setContentsMargins(20, 25, 20, 20)
        layout.setSpacing(15)

        # U.E.P 狀態指示
        status_layout = QHBoxLayout()
        status_icon = QLabel("🤖")
        status_icon.setStyleSheet("font-size: 24px;")
        self.dialog_status_label = QLabel("待命中")
        self.dialog_status_label.setStyleSheet("font-size: 14px; color: #10b981;")
        
        status_layout.addWidget(status_icon)
        status_layout.addWidget(self.dialog_status_label)
        status_layout.addStretch()
        
        layout.addLayout(status_layout)

        # 當前對話內容（打字機效果區域）
        self.current_dialog_text = QTextEdit()
        self.current_dialog_text.setReadOnly(True)
        self.current_dialog_text.setMinimumHeight(180)
        self.current_dialog_text.setStyleSheet("""
            QTextEdit {
                background: transparent;
                border: 1px solid #2f3136;
                border-radius: 8px;
                padding: 15px;
                font-size: 14px;
                line-height: 1.6;
            }
        """)
        layout.addWidget(self.current_dialog_text)

        # 對話速度控制
        speed_layout = QHBoxLayout()
        speed_label = QLabel("對話速度:")
        self.dialog_speed_slider = QSlider(Qt.Horizontal)
        self.dialog_speed_slider.setRange(50, 200)
        self.dialog_speed_slider.setValue(100)
        self.dialog_speed_label = QLabel("100%")
        
        speed_layout.addWidget(speed_label)
        speed_layout.addWidget(self.dialog_speed_slider)
        speed_layout.addWidget(self.dialog_speed_label)
        
        self.dialog_speed_slider.valueChanged.connect(
            lambda v: self.dialog_speed_label.setText(f"{v}%")
        )
        
        layout.addLayout(speed_layout)

        return self._loose_group(group)

    def create_dialog_history_group(self):
        """創建對話歷史組"""
        group = QGroupBox("對話記錄")
        group.setObjectName("settingsGroup")
        layout = QVBoxLayout(group)
        layout.setContentsMargins(20, 25, 20, 20)
        layout.setSpacing(15)

        # 篩選器
        filter_layout = QHBoxLayout()
        filter_label = QLabel("顯示:")
        self.dialog_filter_combo = QComboBox()
        self.dialog_filter_combo.addItems(["全部", "今天", "最近7天", "本月"])
        self.dialog_filter_combo.currentTextChanged.connect(self.filter_dialog_history)
        
        filter_layout.addWidget(filter_label)
        filter_layout.addWidget(self.dialog_filter_combo)
        filter_layout.addStretch()
        
        layout.addLayout(filter_layout)

        # 歷史記錄列表
        self.dialog_history_list = QListWidget()
        self.dialog_history_list.setStyleSheet("""
            QListWidget {
                background: transparent;
                border: 1px solid #2f3136;
                border-radius: 8px;
            }
            QListWidget::item {
                padding: 15px;
                border-bottom: 1px solid #2f3136;
            }
            QListWidget::item:hover {
                background: #232427;
            }
        """)
        self.dialog_history_list.itemClicked.connect(self.view_dialog_detail)
        layout.addWidget(self.dialog_history_list)

        # 控制按鈕
        button_layout = QHBoxLayout()
        export_btn = QPushButton("💾 匯出記錄")
        clear_btn = QPushButton("🗑️ 清除歷史")
        
        export_btn.clicked.connect(self.export_dialog_history)
        clear_btn.clicked.connect(self.clear_dialog_history)

        button_layout.addWidget(export_btn)
        button_layout.addWidget(clear_btn)
        button_layout.addStretch()
        
        layout.addLayout(button_layout)

        return self._loose_group(group)

    def create_dialog_control_group(self):
        """創建對話設定組"""
        group = QGroupBox("對話設定")
        group.setObjectName("settingsGroup")
        layout = QFormLayout(group)
        layout.setSpacing(15)
        layout.setContentsMargins(20, 25, 20, 20)

        # 自動回應
        self.auto_response_checkbox = QCheckBox("啟用自動回應")
        self.auto_response_checkbox.setChecked(True)
        layout.addRow(self.auto_response_checkbox)

        # 對話框顯示
        self.show_dialog_box_checkbox = QCheckBox("顯示對話框視窗")
        self.show_dialog_box_checkbox.setChecked(True)
        self.show_dialog_box_checkbox.stateChanged.connect(self.toggle_dialog_box)
        layout.addRow(self.show_dialog_box_checkbox)

        # 字體大小
        self.dialog_font_size_spinbox = QSpinBox()
        self.dialog_font_size_spinbox.setRange(8, 24)
        self.dialog_font_size_spinbox.setValue(12)
        layout.addRow("字體大小:", self.dialog_font_size_spinbox)

        # 最大記錄數
        self.max_history_spinbox = QSpinBox()
        self.max_history_spinbox.setRange(10, 1000)
        self.max_history_spinbox.setValue(100)
        layout.addRow("最大記錄數:", self.max_history_spinbox)

        # 對話框透明度
        self.dialog_opacity_slider = QSlider(Qt.Horizontal)
        self.dialog_opacity_slider.setRange(30, 100)
        self.dialog_opacity_slider.setValue(90)
        self.dialog_opacity_label = QLabel("90%")
        
        opacity_layout = QHBoxLayout()
        opacity_layout.addWidget(self.dialog_opacity_slider)
        opacity_layout.addWidget(self.dialog_opacity_label)
        
        self.dialog_opacity_slider.valueChanged.connect(
            lambda v: self.dialog_opacity_label.setText(f"{v}%")
        )
        
        layout.addRow("對話框透明度:", opacity_layout)

        return self._loose_group(group)

    def create_bottom_buttons(self, parent_layout):
        button_frame = QFrame()
        button_frame.setObjectName("bottomBar")
        button_frame.setFixedHeight(80)
        button_layout = QHBoxLayout(button_frame)
        button_layout.setContentsMargins(30, 15, 30, 15)

        self.minimize_to_orb_button = QPushButton("最小化到球體")
        self.minimize_to_orb_button.clicked.connect(self.minimize_to_orb)

        self.refresh_all_button = QPushButton("🔄 全部重新整理")
        self.refresh_all_button.clicked.connect(self.refresh_all_modules)

        self.close_button = QPushButton("關閉")
        self.close_button.clicked.connect(self.close)

        button_layout.addWidget(self.minimize_to_orb_button)
        button_layout.addStretch()
        button_layout.addWidget(self.refresh_all_button)
        button_layout.addWidget(self.close_button)

        parent_layout.addWidget(button_frame)

    def create_status_bar(self):
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("系統背景已就緒")

        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        self.status_bar.addPermanentWidget(self.progress_bar)


    def _loose_group(self, group: QGroupBox):
        group.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        return group

 
    def toggle_theme(self):
        try:
            if hasattr(theme_manager, "toggle") and callable(theme_manager.toggle):
                theme_manager.toggle()
            else:

                is_dark = self._tm_is_dark()
                setter = getattr(theme_manager, "set_theme", None) or getattr(theme_manager, "apply", None)
                if callable(setter):
                    setter(Theme.LIGHT if is_dark else Theme.DARK)
                else:
                    debug_log(2, "[SystemBackground] theme_manager 缺少 toggle/set_theme/apply，無法切換主題")
        except Exception as e:
            debug_log(2, f"[SystemBackground] 切換主題失敗: {e}")

    
    def add_new_task(self):
        """新增待辦事項"""
        debug_log(2, "[SystemBackground] 新增任務")
        try:
            # 創建輸入對話框
            dialog = TodoDialog(self)
            if dialog.exec_() == dialog.Accepted:
                todo_data = dialog.get_todo_data()
                
                if self.use_mock_data:
                    # Mock 模式：添加到記憶體
                    self._mock_create_todo(todo_data)
                    self.status_bar.showMessage(f"✅ 已新增任務（Mock）：{todo_data['title']}", 3000)
                    self._load_monitoring_snapshot()  # 重新整理 UI
                else:
                    # 真實模式：調用 local_todo
                    from modules.sys_module.actions.automation_helper import local_todo
                    result = local_todo(
                        'CREATE',
                        task_name=todo_data['title'],
                        task_description=todo_data.get('description', ''),
                        priority=todo_data.get('priority', 'none'),
                        deadline=todo_data.get('deadline', '')
                    )
                    
                    if result.get('success'):
                        self.status_bar.showMessage(f"✅ 已新增任務：{todo_data['title']}", 3000)
                        # 事件系統會自動更新 UI
                    else:
                        self.status_bar.showMessage(f"❌ 新增失敗：{result.get('error', '未知錯誤')}", 3000)
        except Exception as e:
            debug_log(2, f"[SystemBackground] 新增任務失敗: {e}")
            self.status_bar.showMessage(f"❌ 新增任務失敗: {e}", 3000)

    def refresh_today_tasks(self):
        """重新整理今日任務 - 從監控接口載入快照"""
        debug_log(2, "[SystemBackground] 重新整理今日任務")
        self._load_monitoring_snapshot()
        self.status_bar.showMessage("已重新整理今日任務", 2000)

    def filter_tasks_by_priority(self, priority):
        """根據優先級篩選任務"""
        debug_log(2, f"[SystemBackground] 篩選任務: {priority}")
        # 重新加載快照會根據當前篩選器更新 UI
        if hasattr(self, '_monitoring_data') and self._monitoring_data:
            self._update_todos_ui(self._monitoring_data.get('todos', {}))

    def clear_expired_tasks(self):
        """清除已完成的過期任務"""
        debug_log(2, "[SystemBackground] 清除過期任務")
        try:
            from modules.sys_module.actions.automation_helper import local_todo
            
            if self.use_mock_data:
                # Mock 模式：從記憶體清除
                from datetime import datetime
                now = datetime.now()
                cleared_count = 0
                todos_to_remove = []
                
                for todo in self.mock_todos:
                    if todo.get('status') == 'completed' and todo.get('deadline'):
                        try:
                            deadline = datetime.strptime(todo['deadline'], '%Y-%m-%d %H:%M:%S')
                            if deadline < now:
                                todos_to_remove.append(todo['id'])
                        except:
                            pass
                
                for todo_id in todos_to_remove:
                    self._mock_delete_todo(todo_id)
                    cleared_count += 1
                
                self._load_monitoring_snapshot()
            else:
                # 真實模式：調用 monitoring_interface
                expired_todos = self.monitoring_interface.get_expired_todos()
                cleared_count = 0
                
                for todo in expired_todos:
                    if todo.get('status') == 'completed':
                        result = local_todo('DELETE', task_id=todo['id'])
                        if result.get('success'):
                            cleared_count += 1
            
            self.status_bar.showMessage(f"✅ 已清除 {cleared_count} 個已完成的過期任務", 3000)
        except Exception as e:
            debug_log(2, f"[SystemBackground] 清除過期任務失敗: {e}")
            self.status_bar.showMessage(f"❌ 清除失敗: {e}", 3000)
    
    def add_calendar_event(self):
        """新增行事曆事件"""
        debug_log(2, "[SystemBackground] 新增行事曆事件")
        try:
            # 創建輸入對話框
            dialog = CalendarEventDialog(self)
            if dialog.exec_() == dialog.Accepted:
                event_data = dialog.get_event_data()
                
                if self.use_mock_data:
                    # Mock 模式：添加到記憶體
                    self._mock_create_calendar_event(event_data)
                    self.status_bar.showMessage(f"✅ 已新增事件（Mock）：{event_data['title']}", 3000)
                    self._load_monitoring_snapshot()  # 重新整理 UI
                else:
                    # 真實模式：調用 local_calendar
                    from modules.sys_module.actions.automation_helper import local_calendar
                    result = local_calendar(
                        'CREATE',
                        summary=event_data['title'],
                        start_time=event_data['start_time'],
                        end_time=event_data.get('end_time', ''),
                        description=event_data.get('description', ''),
                        location=event_data.get('location', '')
                    )
                    
                    if result.get('success'):
                        self.status_bar.showMessage(f"✅ 已新增事件：{event_data['title']}", 3000)
                        # 事件系統會自動更新 UI
                    else:
                        self.status_bar.showMessage(f"❌ 新增失敗：{result.get('error', '未知錯誤')}", 3000)
        except Exception as e:
            debug_log(2, f"[SystemBackground] 新增行事曆事件失敗: {e}")
            self.status_bar.showMessage(f"❌ 新增事件失敗: {e}", 3000)

    def sync_calendar(self):
        debug_log(2, "[SystemBackground] 同步行事曆")
        self.status_bar.showMessage("正在同步...", 2000)

    def toggle_music_playback(self):
        """切換播放/暫停"""
        try:
            from modules.sys_module.actions.automation_helper import media_control
            
            if self.is_music_playing:
                # 暫停
                result = media_control(action="pause")
                self.is_music_playing = False
                self.play_pause_btn.setText("▶️")
                self.progress_slider.setEnabled(False)  # 禁用進度條
                debug_log(OPERATION_LEVEL, "[SystemBackground] 暫停音樂")
                self.status_bar.showMessage("已暫停", 2000)
            else:
                # 播放
                result = media_control(action="play")
                self.is_music_playing = True
                self.play_pause_btn.setText("⏸️")
                self.progress_slider.setEnabled(True)  # 啟用進度條
                debug_log(OPERATION_LEVEL, "[SystemBackground] 播放音樂")
                self.status_bar.showMessage("正在播放", 2000)
        except Exception as e:
            error_log(f"[SystemBackground] 切換播放狀態失敗: {e}")
            self.status_bar.showMessage(f"操作失敗: {e}", 3000)

    def play_next_song(self):
        """播放下一首"""
        try:
            from modules.sys_module.actions.automation_helper import media_control
            
            result = media_control(action="next")
            
            # 更新本地播放列表索引
            if self.current_playlist and self.current_track_index < len(self.current_playlist) - 1:
                self.current_track_index += 1
                self.playlist_widget.setCurrentRow(self.current_track_index)
                current_song = self.current_playlist[self.current_track_index]
                from pathlib import Path
                self.song_title_label.setText(Path(current_song).stem)
            
            debug_log(OPERATION_LEVEL, "[SystemBackground] 下一首")
            self.status_bar.showMessage("播放下一首", 2000)
        except Exception as e:
            error_log(f"[SystemBackground] 播放下一首失敗: {e}")
            self.status_bar.showMessage(f"操作失敗: {e}", 3000)

    def play_previous_song(self):
        """播放上一首"""
        try:
            from modules.sys_module.actions.automation_helper import media_control
            
            result = media_control(action="previous")
            
            # 更新本地播放列表索引
            if self.current_playlist and self.current_track_index > 0:
                self.current_track_index -= 1
                self.playlist_widget.setCurrentRow(self.current_track_index)
                current_song = self.current_playlist[self.current_track_index]
                from pathlib import Path
                self.song_title_label.setText(Path(current_song).stem)
            
            debug_log(OPERATION_LEVEL, "[SystemBackground] 上一首")
            self.status_bar.showMessage("播放上一首", 2000)
        except Exception as e:
            error_log(f"[SystemBackground] 播放上一首失敗: {e}")
            self.status_bar.showMessage(f"操作失敗: {e}", 3000)

    def adjust_volume(self, value):
        """調整音量"""
        try:
            from modules.sys_module.actions.automation_helper import media_control
            
            self.current_volume = value
            self.volume_label.setText(f"{value}%")
            
            # 調用 media_control 設置音量
            result = media_control(action="volume", volume=value)
            
            debug_log(OPERATION_LEVEL, f"[SystemBackground] 調整音量: {value}% - {result}")
        except Exception as e:
            error_log(f"[SystemBackground] 調整音量失敗: {e}")
    
    def _update_playback_progress(self):
        """更新播放進度條和時間標籤"""
        if not self.is_music_playing:
            return
        
        try:
            from modules.sys_module.actions import automation_helper
            player = automation_helper._music_player
            
            if player and player.is_playing:
                position_ms = player.get_playback_position()
                duration_ms = player.current_duration_ms
                
                if duration_ms > 0:
                    # 更新進度條
                    progress_percent = int((position_ms / duration_ms) * 100)
                    self.progress_slider.blockSignals(True)  # 避免觸發 seek
                    self.progress_slider.setValue(progress_percent)
                    self.progress_slider.blockSignals(False)
                    self.progress_slider.setEnabled(True)
                    
                    # 更新時間標籤
                    current_sec = position_ms // 1000
                    total_sec = duration_ms // 1000
                    
                    self.current_time_label.setText(f"{current_sec // 60}:{current_sec % 60:02d}")
                    self.total_time_label.setText(f"{total_sec // 60}:{total_sec % 60:02d}")
                else:
                    self.progress_slider.setEnabled(False)
            else:
                # 沒有播放，重置
                self.progress_slider.setValue(0)
                self.progress_slider.setEnabled(False)
                self.current_time_label.setText("0:00")
                self.total_time_label.setText("0:00")
                
        except Exception as e:
            pass  # 靜默處理錯誤
    
    def _seek_playback(self, value):
        """拖動進度條時 seek 到指定位置"""
        try:
            from modules.sys_module.actions import automation_helper
            player = automation_helper._music_player
            
            if player and player.current_duration_ms > 0:
                target_ms = int((value / 100) * player.current_duration_ms)
                debug_log(OPERATION_LEVEL, f"[SystemBackground] Seek 請求: {value}% ({target_ms}ms)")
                self.status_bar.showMessage(f"Seek 功能開發中", 2000)
                
        except Exception as e:
            error_log(f"[SystemBackground] Seek 失敗: {e}")
    
    def toggle_loop_mode(self):
        """切換循環模式"""
        try:
            from modules.sys_module.actions.automation_helper import media_control
            
            if self.loop_btn.isChecked():
                # 啟用循環
                result = media_control(action="loop", loop=True)
                debug_log(OPERATION_LEVEL, "[SystemBackground] 啟用循環播放")
                self.status_bar.showMessage("已啟用循環播放", 2000)
            else:
                # 關閉循環
                result = media_control(action="loop", loop=False)
                debug_log(OPERATION_LEVEL, "[SystemBackground] 關閉循環播放")
                self.status_bar.showMessage("已關閉循環播放", 2000)
        except Exception as e:
            error_log(f"[SystemBackground] 切換循環模式失敗: {e}")
            self.status_bar.showMessage(f"操作失敗: {e}", 3000)

    def play_selected_song(self, item):
        """播放選中的歌曲"""
        try:
            from modules.sys_module.actions.automation_helper import media_control
            from pathlib import Path
            
            # 獲取選中的索引
            self.current_track_index = self.playlist_widget.currentRow()
            
            if 0 <= self.current_track_index < len(self.current_playlist):
                selected_file = self.current_playlist[self.current_track_index]
                song_name = Path(selected_file).stem
                
                # 調用 media_control 播放
                result = media_control(
                    action="play",
                    song_query=song_name,
                    music_folder=str(Path(selected_file).parent)
                )
                
                self.song_title_label.setText(song_name)
                self.is_music_playing = True
                self.play_pause_btn.setText("⏸️")
                
                debug_log(OPERATION_LEVEL, f"[SystemBackground] 播放: {song_name}")
                self.status_bar.showMessage(f"正在播放: {song_name}", 3000)
        except Exception as e:
            error_log(f"[SystemBackground] 播放歌曲失敗: {e}")
            self.status_bar.showMessage(f"播放失敗: {e}", 3000)

    def add_music_file(self):
        debug_log(2, "[SystemBackground] 新增音樂檔案")
        self.status_bar.showMessage("功能開發中...", 2000)

    def add_music_folder(self):
        """新增音樂資料夾"""
        try:
            folder_path = QFileDialog.getExistingDirectory(
                self, "選擇音樂資料夾", ""
            )
            
            if folder_path:
                from pathlib import Path
                music_path = Path(folder_path)
                music_extensions = ['.mp3', '.wav', '.flac', '.m4a', '.ogg', '.wma']
                music_files = []
                
                for ext in music_extensions:
                    music_files.extend(music_path.glob(f'**/*{ext}'))
                
                # 添加到播放列表
                for music_file in music_files:
                    self.current_playlist.append(str(music_file))
                    self.playlist_widget.addItem(music_file.name)
                
                debug_log(OPERATION_LEVEL, f"[SystemBackground] 新增 {len(music_files)} 首音樂")
                self.status_bar.showMessage(f"已新增 {len(music_files)} 首音樂", 3000)
        except Exception as e:
            error_log(f"[SystemBackground] 新增音樂資料夾失敗: {e}")
            self.status_bar.showMessage(f"新增失敗: {e}", 3000)

    def clear_playlist(self):
        self.playlist_widget.clear()
        debug_log(2, "[SystemBackground] 已清空播放列表")
        self.status_bar.showMessage("已清空播放列表", 2000)

    def search_music(self):
        keyword = self.music_search_input.text()
        debug_log(2, f"[SystemBackground] 搜尋音樂: {keyword}")
        self.status_bar.showMessage(f"搜尋: {keyword}", 2000)

    def open_youtube(self):
        debug_log(2, "[SystemBackground] 開啟 YouTube")
        self.status_bar.showMessage("功能開發中...", 2000)

    def open_spotify(self):
        debug_log(2, "[SystemBackground] 開啟 Spotify")
        self.status_bar.showMessage("功能開發中...", 2000)
    
    def filter_dialog_history(self, filter_type):
        debug_log(2, f"[SystemBackground] 篩選對話: {filter_type}")

    def view_dialog_detail(self, item):
        dialog_text = item.text()
        debug_log(2, f"[SystemBackground] 查看對話: {dialog_text}")

    def export_dialog_history(self):

        debug_log(2, "[SystemBackground] 匯出對話記錄")
        self.status_bar.showMessage("功能開發中...", 2000)

    def clear_dialog_history(self):
        reply = QMessageBox.question(
            self, "確認清除", 
            "確定要清除所有對話記錄嗎？此操作無法復原。",
            QMessageBox.Yes | QMessageBox.No
        )
        if reply == QMessageBox.Yes:
            self.dialog_history_list.clear()
            self.dialog_history = []
            debug_log(2, "[SystemBackground] 已清除對話歷史")
            self.status_bar.showMessage("已清除對話歷史", 2000)

    def toggle_dialog_box(self, state):
        if state == Qt.Checked:
            debug_log(2, "[SystemBackground] 顯示對話框")
        else:
            debug_log(2, "[SystemBackground] 隱藏對話框")

    
    def minimize_to_orb(self):
        self.is_minimized_to_orb = True
        self.original_geometry = self.geometry()
        self.hide()
        self.action_triggered.emit("minimize_to_orb", {})
        debug_log(2, "[SystemBackground] 已最小化到球體")

    def restore_from_orb(self):
        if self.is_minimized_to_orb:
            if self.original_geometry:
                self.setGeometry(self.original_geometry)
            self.show()
            self.raise_()
            self.activateWindow()
            self.is_minimized_to_orb = False
            debug_log(2, "[SystemBackground] 從球體恢復視窗")

    def refresh_all_modules(self):
        debug_log(2, "[SystemBackground] 重新整理所有模組")
        self.status_bar.showMessage("正在重新整理...", 2000)
        self.refresh_today_tasks()

    def load_settings(self):
        try:
            self.dark_mode = self.settings.value("theme/dark_mode", False, type=bool)
            self.theme_toggle.setText("☀️" if self.dark_mode else "🌙")
            debug_log(2, "[SystemBackground] 設定載入完成")
        except Exception as e:
            debug_log(2, f"[SystemBackground] 載入設定時發生錯誤: {e}")

    def save_settings(self):
        try:
            self.settings.setValue("theme/dark_mode", self.dark_mode)
            self.settings.sync()
            debug_log(2, "[SystemBackground] 設定儲存完成")
        except Exception as e:
            debug_log(2, f"[SystemBackground] 儲存設定時發生錯誤: {e}")

    def showEvent(self, event):
        """視窗顯示時加載最新快照"""
        super().showEvent(event)
        try:
            # 每次顯示視窗時重新加載快照，確保資料同步
            self._load_monitoring_snapshot()
            debug_log(2, "[SystemBackground] 視窗顯示，已加載最新快照")
        except Exception as e:
            debug_log(2, f"[SystemBackground] 加載快照失敗: {e}")
    
    def closeEvent(self, event):
        self.save_settings()
        # 取消訂閱監控事件
        try:
            # MonitoringInterface 會自動清理所有訂閱
            debug_log(2, "[SystemBackground] 視窗關閉，事件訂閱將由接口管理")
        except Exception as e:
            debug_log(2, f"[SystemBackground] 處理關閉事件失敗: {e}")
        
        if not self.is_minimized_to_orb:
            debug_log(2, "[SystemBackground] 視窗關閉")
            self.window_closed.emit()
        event.accept()
    
    # ==================== 監控接口整合 ====================
    
    def _load_default_music_folder(self):
        """載入預設媒體資料夾的音樂"""
        try:
            from pathlib import Path
            from configs.user_settings_manager import get_user_setting
            
            # 從 user_settings 讀取音樂資料夾
            music_folder = get_user_setting("monitoring.background_tasks.default_media_folder", "")
            
            if not music_folder or not Path(music_folder).exists():
                debug_log(OPERATION_LEVEL, "[SystemBackground] 未設定或找不到預設媒體資料夾")
                return
            
            # 掃描音樂檔案
            music_path = Path(music_folder)
            music_extensions = ['.mp3', '.wav', '.flac', '.m4a', '.ogg', '.wma']
            music_files = []
            
            for ext in music_extensions:
                music_files.extend(music_path.glob(f'**/*{ext}'))
            
            # 添加到播放列表
            self.current_playlist = [str(f) for f in music_files]
            self.playlist_widget.clear()
            
            for music_file in music_files:
                self.playlist_widget.addItem(music_file.name)
            
            debug_log(OPERATION_LEVEL, f"[SystemBackground] 已載入 {len(music_files)} 首音樂")
            self.status_bar.showMessage(f"已載入 {len(music_files)} 首音樂", 3000)
            
        except Exception as e:
            error_log(f"[SystemBackground] 載入預設音樂資料夾失敗: {e}")
    
    def _subscribe_monitoring_events(self):
        """訂閱監控事件"""
        if self.use_mock_data:
            debug_log(OPERATION_LEVEL, "[SystemBackground] Mock 模式：跳過事件訂閱")
            return
        
        if not self.monitoring_interface:
            error_log("[SystemBackground] 監控接口未初始化，無法訂閱事件")
            return
        
        try:
            # 訂閱待辦事項事件
            self.monitoring_interface.subscribe_todo_events(self._handle_todo_event)
            # 訂閱行事曆事件
            self.monitoring_interface.subscribe_calendar_events(self._handle_calendar_event)
            debug_log(OPERATION_LEVEL, "[SystemBackground] 已訂閱監控事件")
        except Exception as e:
            error_log(f"[SystemBackground] 訂閱監控事件失敗: {e}")
    
    def _load_monitoring_snapshot(self):
        """加載監控快照資料"""
        try:
            mode = "Mock" if self.use_mock_data else "真實"
            debug_log(OPERATION_LEVEL, f"[SystemBackground] 開始加載監控快照 ({mode}模式)")
            
            if self.use_mock_data:
                # 使用 Mock 資料生成快照
                snapshot = self._get_mock_snapshot()
                debug_log(OPERATION_LEVEL, f"[SystemBackground] Mock 快照已生成: {len(snapshot.get('todos', {}).get('all', []))} todos, {len(snapshot.get('calendar', {}).get('all', []))} events")
            else:
                # 使用真實監控接口
                if self.monitoring_interface:
                    snapshot = self.monitoring_interface.get_monitoring_snapshot()
                else:
                    error_log("[SystemBackground] 監控接口未初始化，無法加載快照")
                    return
            
            self._monitoring_data = snapshot
            
            # 更新待辦事項 UI
            debug_log(OPERATION_LEVEL, "[SystemBackground] 正在更新待辦事項 UI...")
            self._update_todos_ui(snapshot.get('todos', {}))
            
            # 更新行事曆 UI
            debug_log(OPERATION_LEVEL, "[SystemBackground] 正在更新行事曆 UI...")
            self._update_calendar_ui(snapshot.get('calendar', {}))
            
            debug_log(OPERATION_LEVEL, f"[SystemBackground] ✅ 監控快照加載完成 ({mode}模式)")
        except Exception as e:
            import traceback
            error_log(f"[SystemBackground] ❌ 加載監控快照失敗: {e}")
            debug_log(OPERATION_LEVEL, traceback.format_exc())
    
    def _handle_todo_event(self, event_data: MonitoringEventData):
        """處理待辦事項事件"""
        try:
            event_type = event_data.event_type
            item_data = event_data.item_data
            
            if event_type == MonitoringEventType.ITEM_ADDED:
                self._add_todo_to_ui(item_data)
            elif event_type == MonitoringEventType.ITEM_UPDATED:
                self._update_todo_in_ui(item_data)
            elif event_type == MonitoringEventType.ITEM_COMPLETED:
                self._complete_todo_in_ui(item_data)
            elif event_type == MonitoringEventType.ITEM_DELETED:
                self._remove_todo_from_ui(item_data)
            elif event_type == MonitoringEventType.SYSTEM_STARTUP:
                self._load_monitoring_snapshot()
                
            debug_log(OPERATION_LEVEL, f"[SystemBackground] 處理待辦事件: {event_type.name}")
        except Exception as e:
            error_log(f"[SystemBackground] 處理待辦事件失敗: {e}")
    
    def _handle_calendar_event(self, event_data: MonitoringEventData):
        """處理行事曆事件"""
        try:
            event_type = event_data.event_type
            item_data = event_data.item_data
            
            if event_type == MonitoringEventType.ITEM_ADDED:
                self._add_calendar_event_to_ui(item_data)
            elif event_type == MonitoringEventType.ITEM_UPDATED:
                self._update_calendar_event_in_ui(item_data)
            elif event_type == MonitoringEventType.ITEM_DELETED:
                self._remove_calendar_event_from_ui(item_data)
            elif event_type == MonitoringEventType.SYSTEM_STARTUP:
                self._load_monitoring_snapshot()
                
            debug_log(OPERATION_LEVEL, f"[SystemBackground] 處理行事曆事件: {event_type.name}")
        except Exception as e:
            error_log(f"[SystemBackground] 處理行事曆事件失敗: {e}")
    
    def _update_todos_ui(self, todos_data: dict):
        """更新待辦事項 UI"""
        try:
            all_todos = todos_data.get('all', [])
            by_priority = todos_data.get('by_priority', {})
            expired = todos_data.get('expired', [])
            
            # 清空現有列表
            self.today_tasks_list.clear()
            self.sorting_tree.clear()
            self.expired_tasks_list.clear()
            
            # 今日任務 - 顯示未完成的任務
            today_todos = [t for t in all_todos if t.get('status') != 'completed']
            for todo in today_todos[:10]:  # 最多顯示10個
                self._add_todo_item(self.today_tasks_list, todo)
            
            # 分類任務 - 使用 TreeWidget 顯示
            current_filter = self.priority_filter.currentText()
            todos_to_show = []
            
            if current_filter == "全部":
                todos_to_show = all_todos
            elif current_filter == "高優先級":
                todos_to_show = by_priority.get('high', [])
            elif current_filter == "中優先級":
                todos_to_show = by_priority.get('medium', [])
            elif current_filter == "低優先級":
                todos_to_show = by_priority.get('low', [])
            
            # 將待辦事項添加到 TreeWidget
            for todo in todos_to_show:
                self._add_todo_tree_item(self.sorting_tree, todo)
            
            # 過期任務
            for todo in expired:
                self._add_todo_item(self.expired_tasks_list, todo, is_expired=True)
            
            mode = "Mock" if self.use_mock_data else "真實"
            debug_log(2, f"[SystemBackground] 待辦事項 UI 更新完成 ({mode}): {len(today_todos)} 今日, {len(expired)} 過期, 總計 {len(all_todos)} 項")
        except Exception as e:
            debug_log(2, f"[SystemBackground] 更新待辦事項 UI 失敗: {e}")
    
    def _update_calendar_ui(self, calendar_data: dict):
        """更新行事曆 UI"""
        try:
            all_events = calendar_data.get('all', [])
            
            # 清空現有列表
            if hasattr(self, 'week_view'):
                self.week_view.clear()
            
            # 按日期分組
            
            # 行事曆樹狀結構 - 按日期分組
            events_by_date = {}
            for event in all_events:
                start_time = event.get('start_time', '')
                if start_time:
                    date_key = start_time.split(' ')[0]  # 取日期部分
                    if date_key not in events_by_date:
                        events_by_date[date_key] = []
                    events_by_date[date_key].append(event)
            
            # 添加到樹狀結構
            if hasattr(self, 'week_view'):
                for date_key in sorted(events_by_date.keys()):
                    date_item = QTreeWidgetItem([date_key, '', ''])
                    for event in events_by_date[date_key]:
                        title = event.get('title', '未命名')
                        start_time = event.get('start_time', '')
                        time_part = start_time.split(' ')[1] if ' ' in start_time else ''
                        event_item = QTreeWidgetItem(['', title, time_part])
                        # 儲存完整事件資料到 item
                        event_item.setData(0, Qt.UserRole, event)
                        date_item.addChild(event_item)
                    self.week_view.addTopLevelItem(date_item)
                    date_item.setExpanded(True)
            
            debug_log(2, f"[SystemBackground] 行事曆 UI 更新完成 ({len(all_events)} 項)")
        except Exception as e:
            debug_log(2, f"[SystemBackground] 更新行事曆 UI 失敗: {e}")
    
    def _add_todo_item(self, list_widget: 'QListWidget', todo: dict, is_expired: bool = False):
        """添加待辦事項到列表"""
        try:
            title = todo.get('title', '未命名')
            priority = todo.get('priority', 'none')
            deadline = todo.get('deadline', '')
            status = todo.get('status', 'pending')
            
            # 格式化顯示文字
            priority_icon = {'high': '🔴', 'medium': '🟡', 'low': '🟢', 'none': '⚪'}.get(priority, '⚪')
            status_icon = '✅' if status == 'completed' else '⏳'
            
            display_text = f"{priority_icon} {status_icon} {title}"
            if deadline:
                display_text += f" (截止: {deadline})"
            
            if is_expired:
                display_text += " ⚠️"
            
            debug_log(OPERATION_LEVEL, f"[SystemBackground] 添加待辦項目到 UI: {title} (priority={priority}, status={status})")
            
            item = QListWidgetItem(display_text)
            item.setData(Qt.UserRole, todo.get('id'))  # 儲存 ID 以便後續操作
            list_widget.addItem(item)
        except Exception as e:
            error_log(f"[SystemBackground] 添加待辦項目失敗: {e}")
    
    def _add_todo_tree_item(self, tree_widget: 'QTreeWidget', todo: dict):
        """添加待辦事項到樹狀圖"""
        try:
            title = todo.get('title', '未命名')
            priority = todo.get('priority', 'none')
            deadline = todo.get('deadline', '')
            status = todo.get('status', 'pending')
            
            # 格式化顯示
            priority_map = {'high': '🔴 高', 'medium': '🟡 中', 'low': '🟢 低', 'none': '⚪ 無'}
            priority_text = priority_map.get(priority, '⚪ 無')
            
            status_icon = '✅' if status == 'completed' else '⏳'
            title_with_status = f"{status_icon} {title}"
            
            # 創建樹狀項目 [任務, 優先級, 日期]
            item = QTreeWidgetItem([title_with_status, priority_text, deadline])
            item.setData(0, Qt.UserRole, todo.get('id'))  # 儲存 ID
            tree_widget.addTopLevelItem(item)
            
            debug_log(OPERATION_LEVEL, f"[SystemBackground] 添加待辦項目到 Tree: {title}")
        except Exception as e:
            error_log(f"[SystemBackground] 添加待辦項目到 Tree 失敗: {e}")
    
    def _add_calendar_event_item(self, list_widget: 'QListWidget', event: dict):
        """添加行事曆事件到列表"""
        try:
            title = event.get('title', '未命名')
            start_time = event.get('start_time', '')
            location = event.get('location', '')
            
            display_text = f"📅 {title}"
            if start_time:
                display_text += f" - {start_time}"
            if location:
                display_text += f" @ {location}"
            
            item = QListWidgetItem(display_text)
            item.setData(Qt.UserRole, event.get('id'))
            list_widget.addItem(item)
        except Exception as e:
            debug_log(2, f"[SystemBackground] 添加行事曆項目失敗: {e}")
    
    def _add_todo_to_ui(self, todo: dict):
        """增量添加待辦事項"""
        self._load_monitoring_snapshot()  # 重新加載以確保一致性
    
    def _update_todo_in_ui(self, todo: dict):
        """增量更新待辦事項"""
        self._load_monitoring_snapshot()
    
    def _complete_todo_in_ui(self, todo: dict):
        """標記待辦事項完成"""
        self._load_monitoring_snapshot()
    
    def _remove_todo_from_ui(self, todo: dict):
        """增量移除待辦事項"""
        self._load_monitoring_snapshot()
    
    def _add_calendar_event_to_ui(self, event: dict):
        """增量添加行事曆事件"""
        self._load_monitoring_snapshot()
    
    def _update_calendar_event_in_ui(self, event: dict):
        """增量更新行事曆事件"""
        self._load_monitoring_snapshot()
    
    def _remove_calendar_event_from_ui(self, event: dict):
        """增量移除行事曆事件"""
        self._load_monitoring_snapshot()
    
    # ==================== Mock 資料管理 ====================
    
    def _initialize_mock_data(self):
        """初始化 Mock 資料"""
        from datetime import datetime, timedelta
        
        # Mock 待辦事項
        self.mock_todos = [
            {
                'id': 1,
                'title': '完成專案文件',
                'description': '撰寫技術文件和使用手冊',
                'priority': 'high',
                'deadline': (datetime.now() + timedelta(days=2)).strftime('%Y-%m-%d %H:%M:%S'),
                'status': 'pending',
                'created_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            },
            {
                'id': 2,
                'title': '準備會議簡報',
                'description': '下週一的產品展示會議',
                'priority': 'medium',
                'deadline': (datetime.now() + timedelta(days=5)).strftime('%Y-%m-%d %H:%M:%S'),
                'status': 'pending',
                'created_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            },
            {
                'id': 3,
                'title': '回覆客戶郵件',
                'description': '',
                'priority': 'low',
                'deadline': (datetime.now() + timedelta(days=1)).strftime('%Y-%m-%d %H:%M:%S'),
                'status': 'pending',
                'created_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            },
            {
                'id': 4,
                'title': '更新系統文件',
                'description': '已完成的過期任務',
                'priority': 'none',
                'deadline': (datetime.now() - timedelta(days=2)).strftime('%Y-%m-%d %H:%M:%S'),
                'status': 'completed',
                'created_at': (datetime.now() - timedelta(days=5)).strftime('%Y-%m-%d %H:%M:%S')
            }
        ]
        
        # Mock 行事曆事件
        self.mock_calendar_events = [
            {
                'id': 1,
                'title': '團隊會議',
                'description': '每週固定會議',
                'start_time': (datetime.now() + timedelta(hours=2)).strftime('%Y-%m-%d %H:%M:%S'),
                'end_time': (datetime.now() + timedelta(hours=3)).strftime('%Y-%m-%d %H:%M:%S'),
                'location': '會議室A',
                'created_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            },
            {
                'id': 2,
                'title': '產品展示',
                'description': '向客戶展示新功能',
                'start_time': (datetime.now() + timedelta(days=7)).strftime('%Y-%m-%d %H:%M:%S'),
                'end_time': (datetime.now() + timedelta(days=7, hours=2)).strftime('%Y-%m-%d %H:%M:%S'),
                'location': '線上會議',
                'created_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            },
            {
                'id': 3,
                'title': '午餐約會',
                'description': '',
                'start_time': (datetime.now() + timedelta(days=1, hours=12)).strftime('%Y-%m-%d %H:%M:%S'),
                'end_time': (datetime.now() + timedelta(days=1, hours=13)).strftime('%Y-%m-%d %H:%M:%S'),
                'location': '市區餐廳',
                'created_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            }
        ]
        
        self.mock_id_counter = 5  # 從 5 開始編號新項目
        debug_log(2, f"[SystemBackground] Mock 資料已初始化：{len(self.mock_todos)} 個待辦，{len(self.mock_calendar_events)} 個事件")
    
    def _get_mock_snapshot(self) -> dict:
        """生成 Mock 快照"""
        from datetime import datetime
        
        debug_log(2, f"[SystemBackground] 生成 Mock 快照，來源資料: {len(self.mock_todos)} 個待辦, {len(self.mock_calendar_events)} 個事件")
        
        # 待辦事項分類
        all_todos = [t for t in self.mock_todos]
        by_priority = {
            'high': [t for t in all_todos if t['priority'] == 'high'],
            'medium': [t for t in all_todos if t['priority'] == 'medium'],
            'low': [t for t in all_todos if t['priority'] == 'low'],
            'none': [t for t in all_todos if t['priority'] == 'none']
        }
        
        # 過期任務
        now = datetime.now()
        expired = []
        for todo in all_todos:
            if todo.get('deadline'):
                try:
                    deadline = datetime.strptime(todo['deadline'], '%Y-%m-%d %H:%M:%S')
                    if deadline < now:
                        expired.append({**todo, 'is_expired': True})
                except:
                    pass
        
        # 行事曆事件
        all_events = [e for e in self.mock_calendar_events]
        
        return {
            'todos': {
                'all': all_todos,
                'by_priority': by_priority,
                'expired': expired
            },
            'calendar': {
                'all': all_events,
                'upcoming_24h': []  # 簡化處理
            }
        }
    
    def _mock_create_todo(self, todo_data: dict):
        """Mock 新增待辦事項"""
        from datetime import datetime
        new_todo = {
            'id': self.mock_id_counter,
            'title': todo_data['title'],
            'description': todo_data.get('description', ''),
            'priority': todo_data.get('priority', 'none'),
            'deadline': todo_data.get('deadline', ''),
            'status': 'pending',
            'created_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }
        self.mock_todos.append(new_todo)
        self.mock_id_counter += 1
        debug_log(2, f"[SystemBackground Mock] 已新增待辦：{new_todo['title']} (ID: {new_todo['id']})")
    
    def _mock_update_todo(self, task_id: int, todo_data: dict):
        """Mock 更新待辦事項"""
        for todo in self.mock_todos:
            if todo['id'] == task_id:
                todo['title'] = todo_data['title']
                todo['description'] = todo_data.get('description', '')
                todo['priority'] = todo_data.get('priority', 'none')
                todo['deadline'] = todo_data.get('deadline', '')
                debug_log(2, f"[SystemBackground Mock] 已更新待辦：{todo['title']} (ID: {task_id})")
                return True
        return False
    
    def _mock_delete_todo(self, task_id: int):
        """Mock 刪除待辦事項"""
        self.mock_todos = [t for t in self.mock_todos if t['id'] != task_id]
        debug_log(2, f"[SystemBackground Mock] 已刪除待辦 (ID: {task_id})")
    
    def _mock_complete_todo(self, task_id: int):
        """Mock 完成待辦事項"""
        for todo in self.mock_todos:
            if todo['id'] == task_id:
                todo['status'] = 'completed'
                debug_log(2, f"[SystemBackground Mock] 已完成待辦：{todo['title']} (ID: {task_id})")
                return True
        return False
    
    def _mock_create_calendar_event(self, event_data: dict):
        """Mock 新增行事曆事件"""
        from datetime import datetime
        new_event = {
            'id': self.mock_id_counter,
            'title': event_data['title'],
            'description': event_data.get('description', ''),
            'start_time': event_data['start_time'],
            'end_time': event_data.get('end_time', ''),
            'location': event_data.get('location', ''),
            'created_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }
        self.mock_calendar_events.append(new_event)
        self.mock_id_counter += 1
        debug_log(2, f"[SystemBackground Mock] 已新增事件：{new_event['title']} (ID: {new_event['id']})")
    
    def _mock_update_calendar_event(self, event_id: int, event_data: dict):
        """Mock 更新行事曆事件"""
        for event in self.mock_calendar_events:
            if event['id'] == event_id:
                event['title'] = event_data['title']
                event['description'] = event_data.get('description', '')
                event['start_time'] = event_data['start_time']
                event['end_time'] = event_data.get('end_time', '')
                event['location'] = event_data.get('location', '')
                debug_log(2, f"[SystemBackground Mock] 已更新事件：{event['title']} (ID: {event_id})")
                return True
        return False
    
    def _mock_delete_calendar_event(self, event_id: int):
        """Mock 刪除行事曆事件"""
        self.mock_calendar_events = [e for e in self.mock_calendar_events if e['id'] != event_id]
        debug_log(2, f"[SystemBackground Mock] 已刪除事件 (ID: {event_id})")
    
    # ==================== 編輯/刪除功能 ====================
    
    def edit_todo_item(self, item: 'QListWidgetItem'):
        """編輯待辦事項"""
        try:
            todo_id = item.data(Qt.UserRole)
            if not todo_id:
                return
            
            # 從當前資料中找到完整的 todo
            if not self._monitoring_data:
                return
            
            all_todos = self._monitoring_data.get('todos', {}).get('all', [])
            todo = next((t for t in all_todos if t.get('id') == todo_id), None)
            
            if not todo:
                return
            
            # 顯示編輯對話框
            dialog = TodoDialog(self, todo_data=todo)
            if dialog.exec_() == dialog.Accepted:
                updated_data = dialog.get_todo_data()
                
                if self.use_mock_data:
                    # Mock 模式：更新記憶體
                    if self._mock_update_todo(updated_data['task_id'], updated_data):
                        self.status_bar.showMessage(f"✅ 已更新任務（Mock）：{updated_data['title']}", 3000)
                        self._load_monitoring_snapshot()
                    else:
                        self.status_bar.showMessage("❌ 更新失敗：找不到任務", 3000)
                else:
                    # 真實模式：調用 local_todo
                    from modules.sys_module.actions.automation_helper import local_todo
                    result = local_todo(
                        'UPDATE',
                        task_id=updated_data['task_id'],
                        task_name=updated_data['title'],
                        task_description=updated_data.get('description', ''),
                        priority=updated_data.get('priority', 'none'),
                        deadline=updated_data.get('deadline', '')
                    )
                    
                    if result.get('success'):
                        self.status_bar.showMessage(f"✅ 已更新任務：{updated_data['title']}", 3000)
                    else:
                        self.status_bar.showMessage(f"❌ 更新失敗：{result.get('error', '未知錯誤')}", 3000)
        except Exception as e:
            debug_log(2, f"[SystemBackground] 編輯待辦事項失敗: {e}")
            self.status_bar.showMessage(f"❌ 編輯失敗: {e}", 3000)
    
    def delete_todo_item(self, item: 'QListWidgetItem'):
        """刪除待辦事項"""
        try:
            todo_id = item.data(Qt.UserRole)
            if not todo_id:
                return
            
            # 確認刪除
            from PyQt5.QtWidgets import QMessageBox
            reply = QMessageBox.question(
                self, '確認刪除',
                '確定要刪除這個待辦事項嗎？',
                QMessageBox.Yes | QMessageBox.No
            )
            
            if reply == QMessageBox.Yes:
                if self.use_mock_data:
                    # Mock 模式：從記憶體刪除
                    self._mock_delete_todo(todo_id)
                    self.status_bar.showMessage("✅ 已刪除任務（Mock）", 3000)
                    self._load_monitoring_snapshot()
                else:
                    # 真實模式：調用 local_todo
                    from modules.sys_module.actions.automation_helper import local_todo
                    result = local_todo('DELETE', task_id=todo_id)
                    
                    if result.get('success'):
                        self.status_bar.showMessage("✅ 已刪除任務", 3000)
                    else:
                        self.status_bar.showMessage(f"❌ 刪除失敗：{result.get('error', '未知錯誤')}", 3000)
        except Exception as e:
            debug_log(2, f"[SystemBackground] 刪除待辦事項失敗: {e}")
            self.status_bar.showMessage(f"❌ 刪除失敗: {e}", 3000)
    
    def complete_todo_item(self, item: 'QListWidgetItem'):
        """標記待辦事項為完成"""
        try:
            todo_id = item.data(Qt.UserRole)
            if not todo_id:
                return
            
            if self.use_mock_data:
                # Mock 模式：更新記憶體
                if self._mock_complete_todo(todo_id):
                    self.status_bar.showMessage("✅ 任務已完成（Mock）", 3000)
                    self._load_monitoring_snapshot()
                else:
                    self.status_bar.showMessage("❌ 標記完成失敗：找不到任務", 3000)
            else:
                # 真實模式：調用 local_todo
                from modules.sys_module.actions.automation_helper import local_todo
                result = local_todo('COMPLETE', task_id=todo_id)
                
                if result.get('success'):
                    self.status_bar.showMessage("✅ 任務已完成", 3000)
                else:
                    self.status_bar.showMessage(f"❌ 標記完成失敗：{result.get('error', '未知錯誤')}", 3000)
        except Exception as e:
            debug_log(2, f"[SystemBackground] 標記完成失敗: {e}")
            self.status_bar.showMessage(f"❌ 標記完成失敗: {e}", 3000)
    
    def show_todo_context_menu(self, pos, list_widget: 'QListWidget'):
        """顯示待辦事項右鍵選單"""
        try:
            item = list_widget.itemAt(pos)
            if not item:
                return
            
            from PyQt5.QtWidgets import QMenu
            menu = QMenu(self)
            
            edit_action = menu.addAction("✏️ 編輯")
            complete_action = menu.addAction("✅ 標記完成")
            delete_action = menu.addAction("🗑️ 刪除")
            
            action = menu.exec_(list_widget.mapToGlobal(pos))
            
            if action == edit_action:
                self.edit_todo_item(item)
            elif action == complete_action:
                self.complete_todo_item(item)
            elif action == delete_action:
                self.delete_todo_item(item)
        except Exception as e:
            debug_log(2, f"[SystemBackground] 顯示右鍵選單失敗: {e}")
    
    def edit_calendar_event_item(self, item: 'QTreeWidgetItem', column: int):
        """編輯行事曆事件"""
        try:
            # 只處理子項目（實際事件），不處理日期標題
            if item.parent() is None:
                return
            
            event_data = item.data(0, Qt.UserRole)
            if not event_data:
                return
            
            # 顯示編輯對話框
            dialog = CalendarEventDialog(self, event_data=event_data)
            if dialog.exec_() == dialog.Accepted:
                updated_data = dialog.get_event_data()
                
                if self.use_mock_data:
                    # Mock 模式：更新記憶體
                    if self._mock_update_calendar_event(updated_data['event_id'], updated_data):
                        self.status_bar.showMessage(f"✅ 已更新事件（Mock）：{updated_data['title']}", 3000)
                        self._load_monitoring_snapshot()
                    else:
                        self.status_bar.showMessage("❌ 更新失敗：找不到事件", 3000)
                else:
                    # 真實模式：調用 local_calendar
                    from modules.sys_module.actions.automation_helper import local_calendar
                    result = local_calendar(
                        'UPDATE',
                        event_id=updated_data['event_id'],
                        summary=updated_data['title'],
                        start_time=updated_data['start_time'],
                        end_time=updated_data.get('end_time', ''),
                        description=updated_data.get('description', ''),
                        location=updated_data.get('location', '')
                    )
                    
                    if result.get('success'):
                        self.status_bar.showMessage(f"✅ 已更新事件：{updated_data['title']}", 3000)
                    else:
                        self.status_bar.showMessage(f"❌ 更新失敗：{result.get('error', '未知錯誤')}", 3000)
        except Exception as e:
            debug_log(2, f"[SystemBackground] 編輯行事曆事件失敗: {e}")
            self.status_bar.showMessage(f"❌ 編輯失敗: {e}", 3000)
    
    def delete_calendar_event_item(self, item: 'QTreeWidgetItem'):
        """刪除行事曆事件"""
        try:
            event_data = item.data(0, Qt.UserRole)
            if not event_data:
                return
            
            event_id = event_data.get('id')
            if not event_id:
                return
            
            # 確認刪除
            from PyQt5.QtWidgets import QMessageBox
            reply = QMessageBox.question(
                self, '確認刪除',
                '確定要刪除這個行事曆事件嗎？',
                QMessageBox.Yes | QMessageBox.No
            )
            
            if reply == QMessageBox.Yes:
                if self.use_mock_data:
                    # Mock 模式：從記憶體刪除
                    self._mock_delete_calendar_event(event_id)
                    self.status_bar.showMessage("✅ 已刪除事件（Mock）", 3000)
                    self._load_monitoring_snapshot()
                else:
                    # 真實模式：調用 local_calendar
                    from modules.sys_module.actions.automation_helper import local_calendar
                    result = local_calendar('DELETE', event_id=event_id)
                    
                    if result.get('success'):
                        self.status_bar.showMessage("✅ 已刪除事件", 3000)
                    else:
                        self.status_bar.showMessage(f"❌ 刪除失敗：{result.get('error', '未知錯誤')}", 3000)
        except Exception as e:
            debug_log(2, f"[SystemBackground] 刪除行事曆事件失敗: {e}")
            self.status_bar.showMessage(f"❌ 刪除失敗: {e}", 3000)
    
    def show_calendar_context_menu(self, pos, tree_widget: 'QTreeWidget'):
        """顯示行事曆右鍵選單"""
        try:
            item = tree_widget.itemAt(pos)
            if not item or item.parent() is None:
                return  # 只對事件項目顯示選單，不對日期標題
            
            from PyQt5.QtWidgets import QMenu
            menu = QMenu(self)
            
            edit_action = menu.addAction("✏️ 編輯")
            delete_action = menu.addAction("🗑️ 刪除")
            
            action = menu.exec_(tree_widget.mapToGlobal(pos))
            
            if action == edit_action:
                self.edit_calendar_event_item(item, 0)
            elif action == delete_action:
                self.delete_calendar_event_item(item)
        except Exception as e:
            debug_log(2, f"[SystemBackground] 顯示行事曆右鍵選單失敗: {e}")


# ==================== 對話框類別 ====================

class TodoDialog(QDialog if PYQT5_AVAILABLE else object):
    """待辦事項新增/編輯對話框"""
    
    def __init__(self, parent=None, todo_data=None):
        super().__init__(parent)
        self.todo_data = todo_data  # 用於編輯模式
        self.init_ui()
    
    def init_ui(self):
        self.setWindowTitle("新增待辦事項" if not self.todo_data else "編輯待辦事項")
        self.setMinimumWidth(500)
        
        layout = QVBoxLayout(self)
        form_layout = QFormLayout()
        
        # 標題
        self.title_input = QLineEdit()
        self.title_input.setPlaceholderText("請輸入任務標題")
        if self.todo_data:
            self.title_input.setText(self.todo_data.get('title', ''))
        form_layout.addRow("標題*:", self.title_input)
        
        # 描述
        self.description_input = QTextEdit()
        self.description_input.setPlaceholderText("請輸入任務描述（可選）")
        self.description_input.setMaximumHeight(100)
        if self.todo_data:
            self.description_input.setPlainText(self.todo_data.get('description', ''))
        form_layout.addRow("描述:", self.description_input)
        
        # 優先級
        self.priority_combo = QComboBox()
        self.priority_combo.addItems(["無", "低", "中", "高"])
        priority_map = {'none': 0, 'low': 1, 'medium': 2, 'high': 3}
        if self.todo_data:
            priority_index = priority_map.get(self.todo_data.get('priority', 'none'), 0)
            self.priority_combo.setCurrentIndex(priority_index)
        form_layout.addRow("優先級:", self.priority_combo)
        
        # 截止日期 - 使用日期時間選擇器
        deadline_layout = QHBoxLayout()
        self.deadline_edit = QDateTimeEdit()
        self.deadline_edit.setCalendarPopup(True)
        self.deadline_edit.setDisplayFormat("yyyy-MM-dd HH:mm:ss")
        self.deadline_edit.setDateTime(QDateTime.currentDateTime().addDays(1))  # 預設明天
        
        # 啟用/停用截止日期的複選框
        self.deadline_enabled = QCheckBox("設定截止日期")
        self.deadline_enabled.setChecked(False)
        self.deadline_edit.setEnabled(False)
        self.deadline_enabled.toggled.connect(lambda checked: self.deadline_edit.setEnabled(checked))
        
        if self.todo_data and self.todo_data.get('deadline'):
            try:
                from datetime import datetime as dt
                deadline_dt = dt.strptime(self.todo_data['deadline'], '%Y-%m-%d %H:%M:%S')
                self.deadline_edit.setDateTime(QDateTime(deadline_dt.year, deadline_dt.month, deadline_dt.day,
                                                         deadline_dt.hour, deadline_dt.minute, deadline_dt.second))
                self.deadline_enabled.setChecked(True)
                self.deadline_edit.setEnabled(True)
            except:
                pass
        
        deadline_layout.addWidget(self.deadline_enabled)
        deadline_layout.addWidget(self.deadline_edit)
        form_layout.addRow("截止日期:", deadline_layout)
        
        layout.addLayout(form_layout)
        
        # 按鈕
        button_layout = QHBoxLayout()
        save_btn = QPushButton("✅ 儲存")
        cancel_btn = QPushButton("❌ 取消")
        
        save_btn.clicked.connect(self.accept)
        cancel_btn.clicked.connect(self.reject)
        
        button_layout.addWidget(save_btn)
        button_layout.addWidget(cancel_btn)
        layout.addLayout(button_layout)
    
    def get_todo_data(self) -> dict:
        """獲取待辦事項資料"""
        priority_map = {0: 'none', 1: 'low', 2: 'medium', 3: 'high'}
        
        data = {
            'title': self.title_input.text().strip(),
            'description': self.description_input.toPlainText().strip(),
            'priority': priority_map[self.priority_combo.currentIndex()],
        }
        
        # 截止日期（可選）
        if self.deadline_enabled.isChecked():
            qdt = self.deadline_edit.dateTime()
            data['deadline'] = qdt.toString('yyyy-MM-dd HH:mm:ss')
        
        # 編輯模式：保留 ID
        if self.todo_data and 'id' in self.todo_data:
            data['task_id'] = self.todo_data['id']
        
        return data


class CalendarEventDialog(QDialog if PYQT5_AVAILABLE else object):
    """行事曆事件新增/編輯對話框"""
    
    def __init__(self, parent=None, event_data=None):
        super().__init__(parent)
        self.event_data = event_data  # 用於編輯模式
        self.init_ui()
    
    def init_ui(self):
        self.setWindowTitle("新增行事曆事件" if not self.event_data else "編輯行事曆事件")
        self.setMinimumWidth(500)
        
        layout = QVBoxLayout(self)
        form_layout = QFormLayout()
        
        # 標題
        self.title_input = QLineEdit()
        self.title_input.setPlaceholderText("請輸入事件標題")
        if self.event_data:
            self.title_input.setText(self.event_data.get('title', ''))
        form_layout.addRow("標題*:", self.title_input)
        
        # 描述
        self.description_input = QTextEdit()
        self.description_input.setPlaceholderText("請輸入事件描述（可選）")
        self.description_input.setMaximumHeight(100)
        if self.event_data:
            self.description_input.setPlainText(self.event_data.get('description', ''))
        form_layout.addRow("描述:", self.description_input)
        
        # 開始時間
        self.start_time_input = QLineEdit()
        self.start_time_input.setPlaceholderText("YYYY-MM-DD HH:MM:SS")
        if self.event_data and self.event_data.get('start_time'):
            self.start_time_input.setText(self.event_data['start_time'])
        form_layout.addRow("開始時間*:", self.start_time_input)
        
        # 結束時間
        self.end_time_input = QLineEdit()
        self.end_time_input.setPlaceholderText("YYYY-MM-DD HH:MM:SS（可選）")
        if self.event_data and self.event_data.get('end_time'):
            self.end_time_input.setText(self.event_data['end_time'])
        form_layout.addRow("結束時間:", self.end_time_input)
        
        # 地點
        self.location_input = QLineEdit()
        self.location_input.setPlaceholderText("請輸入地點（可選）")
        if self.event_data:
            self.location_input.setText(self.event_data.get('location', ''))
        form_layout.addRow("地點:", self.location_input)
        
        layout.addLayout(form_layout)
        
        # 按鈕
        button_layout = QHBoxLayout()
        save_btn = QPushButton("✅ 儲存")
        cancel_btn = QPushButton("❌ 取消")
        
        save_btn.clicked.connect(self.accept)
        cancel_btn.clicked.connect(self.reject)
        
        button_layout.addWidget(save_btn)
        button_layout.addWidget(cancel_btn)
        layout.addLayout(button_layout)
    
    def get_event_data(self) -> dict:
        """獲取行事曆事件資料"""
        data = {
            'title': self.title_input.text().strip(),
            'description': self.description_input.toPlainText().strip(),
            'start_time': self.start_time_input.text().strip(),
        }
        
        # 結束時間（可選）
        end_time = self.end_time_input.text().strip()
        if end_time:
            data['end_time'] = end_time
        
        # 地點（可選）
        location = self.location_input.text().strip()
        if location:
            data['location'] = location
        
        # 編輯模式：保留 ID
        if self.event_data and 'id' in self.event_data:
            data['event_id'] = self.event_data['id']
        
        return data


def create_test_window():
    if not PYQT5_AVAILABLE:
        debug_log(2, "[SystemBackground] PyQt5不可用，無法創建測試視窗")
        return None
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    window = SystemBackgroundWindow()
    window.show()
    return app, window


if __name__ == "__main__":
    app, window = create_test_window()
    if app and window:
        sys.exit(app.exec_())