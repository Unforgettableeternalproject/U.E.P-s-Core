#system_background.py
import os
import sys
from typing import Dict, Any, Optional
from theme_manager import theme_manager, Theme

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
                                QListWidgetItem)
    from PyQt5.QtCore import (Qt, QTimer, pyqtSignal, QSize, QRect,
                             QPropertyAnimation, QEasingCurve, QThread,
                             QSettings, QStandardPaths)
    from PyQt5.QtGui import (QIcon, QFont, QPixmap, QPalette, QColor,
                            QPainter, QLinearGradient, QBrush)
    PYQT5_AVAILABLE = True
except ImportError:
    PYQT5_AVAILABLE = False
    print("[SystemBackground] PyQt5 不可用")


class SystemBackgroundWindow(QMainWindow):
    settings_changed = pyqtSignal(str, object)
    action_triggered = pyqtSignal(str, dict)
    window_closed = pyqtSignal()
    SCROLL_AREA_MIN_H = 620

    def __init__(self, ui_module=None):
        super().__init__()

        if not PYQT5_AVAILABLE:
            print("[SystemBackground] PyQt5不可用，無法初始化")
            return

        self.ui_module = ui_module
        self.settings = QSettings("UEP", "SystemBackground")
        
        self.is_minimized_to_orb = False
        self.original_geometry = None

        # 音樂播放器狀態
        self.current_music_player = None
        self.is_music_playing = False
        
        # 對話記錄
        self.dialog_history = []

        self.init_ui()
        self._wire_theme_manager()
        self.load_settings()
        
        print("[SystemBackground] 系統背景視窗初始化完成")

    def init_ui(self):
        self.setWindowTitle("UEP系統背景")
        self.setMinimumSize(900, 950)
        self.resize(1200, 950)

        try:
            icon_path = os.path.join(os.path.dirname(__file__), "../../../arts/U.E.P.png")
            if os.path.exists(icon_path):   
                self.setWindowIcon(QIcon(icon_path))
        except Exception as e:
            print(f"[SystemBackground] 無法載入圖標: {e}")

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
            print(f"[SystemBackground] 無法連接 theme_changed: {e}")

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
            scroll_area.setSizeAdjustPolicy(QAbstractScrollArea.AdjustToContents)


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
        self.create_dialog_tab()

        parent_layout.addWidget(self.tab_widget, 1)

    def create_reminder_tab(self):
        reminder_widget = QWidget()
        reminder_layout = QVBoxLayout(reminder_widget)
        reminder_layout.setContentsMargins(30, 30, 30, 200)
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
        calendar_layout.setContentsMargins(30, 30, 30, 100)
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

        # Google Calendar 整合
        google_calendar_group = self.create_google_calendar_group()
        scroll_layout.addWidget(google_calendar_group)

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

    def create_google_calendar_group(self):
        group = QGroupBox("Google Calendar 整合")
        group.setObjectName("settingsGroup")
        layout = QFormLayout(group)
        layout.setSpacing(15)
        layout.setContentsMargins(20, 25, 20, 20)

        # 授權狀態
        self.auth_status_label = QLabel("❌ 未授權")
        self.auth_status_label.setStyleSheet("color: #f44336;")
        layout.addRow("授權狀態:", self.auth_status_label)

        # 授權按鈕
        button_layout = QHBoxLayout()
        self.authorize_btn = QPushButton("🔐 授權連結")
        self.revoke_btn = QPushButton("🚫 撤銷授權")
        self.revoke_btn.setEnabled(False)

        self.authorize_btn.clicked.connect(self.authorize_google_calendar)
        self.revoke_btn.clicked.connect(self.revoke_authorization)

        button_layout.addWidget(self.authorize_btn)
        button_layout.addWidget(self.revoke_btn)
        button_layout.addStretch()
        
        layout.addRow("", button_layout)

        # 自動同步
        self.auto_sync_checkbox = QCheckBox("啟用自動同步")
        self.auto_sync_checkbox.setChecked(True)
        layout.addRow(self.auto_sync_checkbox)

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
        
        self.volume_slider.valueChanged.connect(
            lambda v: self.volume_label.setText(f"{v}%")
        )
        
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

    def create_dialog_tab(self):
        dialog_widget = QWidget()
        dialog_layout = QVBoxLayout(dialog_widget)
        dialog_layout.setContentsMargins(30, 30, 30, 30)
        dialog_layout.setSpacing(20)

        scroll_area = QScrollArea()
        self._tall_scroll(scroll_area)

        #scroll area
        scroll_content = QWidget()
        scroll_content.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
        scroll_layout = QVBoxLayout(scroll_content)
        scroll_layout.setContentsMargins(0, 0, 0, 0)
        scroll_layout.setSpacing(20)

        # 當前對話
        current_dialog_group = self.create_current_dialog_group()
        scroll_layout.addWidget(current_dialog_group)

        # 對話歷史
        dialog_history_group = self.create_dialog_history_group()
        scroll_layout.addWidget(dialog_history_group)

        # 對話控制
        dialog_control_group = self.create_dialog_control_group()
        scroll_layout.addWidget(dialog_control_group)

        scroll_layout.addStretch()
        scroll_area.setWidget(scroll_content)
        dialog_layout.addWidget(scroll_area, 1)

        self.tab_widget.addTab(dialog_widget, "💬 對話狀態")

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
        """創建底部按鈕區"""
        button_frame = QFrame()
        button_frame.setObjectName("bottomBar")
        button_frame.setFixedHeight(70)
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

    def _tall_scroll(self, scroll_area: QScrollArea):
        scroll_area.setWidgetResizable(True)
        scroll_area.setFrameShape(QFrame.NoFrame)
        scroll_area.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        scroll_area.setMinimumHeight(self.SCROLL_AREA_MIN_H)
        scroll_area.setAlignment(Qt.AlignTop)

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
                    print("[SystemBackground] theme_manager 缺少 toggle/set_theme/apply，無法切換主題")
        except Exception as e:
            print(f"[SystemBackground] 切換主題失敗: {e}")

    
    def add_new_task(self):
        """新增任務"""
        print("[SystemBackground] 新增任務")
        self.status_bar.showMessage("功能開發中...", 2000)

    def refresh_today_tasks(self):
        """重新整理今日任務"""
        print("[SystemBackground] 重新整理今日任務")
        self.status_bar.showMessage("已重新整理今日任務", 2000)

    def filter_tasks_by_priority(self, priority):
        """根據優先級篩選任務"""
        print(f"[SystemBackground] 篩選任務: {priority}")

    def clear_expired_tasks(self):
        """清除過期任務"""
        print("[SystemBackground] 清除過期任務")
        self.status_bar.showMessage("已清除完成的過期任務", 2000)

    # ==================== 行事曆功能 ====================
    
    def add_calendar_event(self):
        """新增行事曆事件"""
        print("[SystemBackground] 新增行事曆事件")
        self.status_bar.showMessage("功能開發中...", 2000)

    def sync_calendar(self):
        """同步行事曆"""
        print("[SystemBackground] 同步行事曆")
        self.status_bar.showMessage("正在同步...", 2000)

    def authorize_google_calendar(self):
        """授權 Google Calendar"""
        print("[SystemBackground] 授權 Google Calendar")
        self.auth_status_label.setText("✅ 已授權")
        self.auth_status_label.setStyleSheet("color: #10b981;")
        self.revoke_btn.setEnabled(True)

    def revoke_authorization(self):
        """撤銷授權"""
        print("[SystemBackground] 撤銷授權")
        self.auth_status_label.setText("❌ 未授權")
        self.auth_status_label.setStyleSheet("color: #f44336;")
        self.revoke_btn.setEnabled(False)
    
    def toggle_music_playback(self):
        self.is_music_playing = not self.is_music_playing
        if self.is_music_playing:
            self.play_pause_btn.setText("⏸️")
            print("[SystemBackground] 播放音樂")
        else:
            self.play_pause_btn.setText("▶️")
            print("[SystemBackground] 暫停音樂")

    def play_next_song(self):
        print("[SystemBackground] 下一首")
        self.status_bar.showMessage("播放下一首", 2000)

    def play_previous_song(self):
        print("[SystemBackground] 上一首")
        self.status_bar.showMessage("播放上一首", 2000)

    def toggle_loop_mode(self):
        if self.loop_btn.isChecked():
            print("[SystemBackground] 啟用單曲循環")
            self.status_bar.showMessage("已啟用單曲循環", 2000)
        else:
            print("[SystemBackground] 關閉單曲循環")
            self.status_bar.showMessage("已關閉單曲循環", 2000)

    def play_selected_song(self, item):
        song_name = item.text()
        print(f"[SystemBackground] 播放: {song_name}")
        self.song_title_label.setText(song_name)
        self.is_music_playing = True
        self.play_pause_btn.setText("⏸️")

    def add_music_file(self):
        print("[SystemBackground] 新增音樂檔案")
        self.status_bar.showMessage("功能開發中...", 2000)

    def add_music_folder(self):
        print("[SystemBackground] 新增音樂資料夾")
        self.status_bar.showMessage("功能開發中...", 2000)

    def clear_playlist(self):
        self.playlist_widget.clear()
        print("[SystemBackground] 已清空播放列表")
        self.status_bar.showMessage("已清空播放列表", 2000)

    def search_music(self):
        keyword = self.music_search_input.text()
        print(f"[SystemBackground] 搜尋音樂: {keyword}")
        self.status_bar.showMessage(f"搜尋: {keyword}", 2000)

    def open_youtube(self):
        print("[SystemBackground] 開啟 YouTube")
        self.status_bar.showMessage("功能開發中...", 2000)

    def open_spotify(self):
        print("[SystemBackground] 開啟 Spotify")
        self.status_bar.showMessage("功能開發中...", 2000)
    
    def filter_dialog_history(self, filter_type):
        print(f"[SystemBackground] 篩選對話: {filter_type}")

    def view_dialog_detail(self, item):
        dialog_text = item.text()
        print(f"[SystemBackground] 查看對話: {dialog_text}")

    def export_dialog_history(self):

        print("[SystemBackground] 匯出對話記錄")
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
            print("[SystemBackground] 已清除對話歷史")
            self.status_bar.showMessage("已清除對話歷史", 2000)

    def toggle_dialog_box(self, state):
        if state == Qt.Checked:
            print("[SystemBackground] 顯示對話框")
        else:
            print("[SystemBackground] 隱藏對話框")

    
    def minimize_to_orb(self):
        self.is_minimized_to_orb = True
        self.original_geometry = self.geometry()
        self.hide()
        self.action_triggered.emit("minimize_to_orb", {})
        print("[SystemBackground] 已最小化到球體")

    def restore_from_orb(self):
        if self.is_minimized_to_orb:
            if self.original_geometry:
                self.setGeometry(self.original_geometry)
            self.show()
            self.raise_()
            self.activateWindow()
            self.is_minimized_to_orb = False
            print("[SystemBackground] 從球體恢復視窗")

    def refresh_all_modules(self):
        print("[SystemBackground] 重新整理所有模組")
        self.status_bar.showMessage("正在重新整理...", 2000)
        self.refresh_today_tasks()

    def load_settings(self):
        try:
            self.dark_mode = self.settings.value("theme/dark_mode", False, type=bool)
            self.theme_toggle.setText("☀️" if self.dark_mode else "🌙")
            print("[SystemBackground] 設定載入完成")
        except Exception as e:
            print(f"[SystemBackground] 載入設定時發生錯誤: {e}")

    def save_settings(self):
        try:
            self.settings.setValue("theme/dark_mode", self.dark_mode)
            self.settings.sync()
            print("[SystemBackground] 設定儲存完成")
        except Exception as e:
            print(f"[SystemBackground] 儲存設定時發生錯誤: {e}")

    def closeEvent(self, event):
        self.save_settings()
        if not self.is_minimized_to_orb:
            print("[SystemBackground] 視窗關閉")
            self.window_closed.emit()
        event.accept()



def create_test_window():
    if not PYQT5_AVAILABLE:
        print("[SystemBackground] PyQt5不可用，無法創建測試視窗")
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