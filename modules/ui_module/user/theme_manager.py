from enum import Enum
from PyQt5.QtCore import QObject, pyqtSignal, QSettings, Qt
from PyQt5.QtWidgets import QApplication


class Theme(Enum):
    LIGHT = "light"
    DARK = "dark"


class ThemeManager(QObject):

    theme_changed = pyqtSignal(str)

    BRAND_YELLOW = "#d5b618"
    BRAND_BLUE   = "#345ddb"
    LIGHT_GREY   = "#e6e6e6"  
    DARK_BG      = "#26272b" 

    _QSS_DARK = f"""
        * {{
            font-family: "Microsoft YaHei UI", "Segoe UI", "Noto Sans TC";
        }}

        /* Base widget background + text for dark mode */
        QWidget {{
            background:{DARK_BG};
            color:#e6e6e6;
        }}

        QMainWindow {{
            background:#000000;
        }}

        QFrame#bottomBar,
        QStatusBar {{
            background:#000000;
            color:#b5b8bf;
            border-top:1px solid #2f3136;
        }}

        QWidget#header {{
            background:#1f2023;
            border-bottom:1px solid #2f3136;
        }}

        QLabel#mainTitle {{
            color:#ffffff;
            font-size:28px;
            font-weight:800;
        }}

        QLabel#subtitle {{
            color:#b5b8bf;
            font-size:13px;
        }}

        QTabWidget#mainTabs::pane {{
            background:#26272b;
            border:1px solid #2f3136;
            border-radius:8px;
        }}

        QTabBar::tab {{
            background:#1f2023;
            color:#b5b8bf;
            padding:10px 18px;
            margin-right:4px;
            border-top-left-radius:8px;
            border-top-right-radius:8px;
            border:1px solid #2f3136;
            font-weight:600;
        }}

        QTabBar::tab:selected {{
            background:#26272b;
            color:#ffffff;
            border:1px solid {BRAND_YELLOW};
        }}

        QTabBar::tab:hover:!selected {{
            background:#232427;
        }}

        QGroupBox#settingsGroup {{
            background:#1f2023;
            border:1px solid #2f3136;
            border-radius:10px;
            margin-top:12px;
            padding-top:18px;
            color:#e6e6e6;
            font-weight:600;
        }}

        QGroupBox#settingsGroup::title {{
            subcontrol-origin: margin;
            left:15px;
            padding:0 8px;
            color:#e6e6e6;
        }}

        QLabel {{
            color:#e6e6e6;
        }}

        QLabel#infoText {{
            color:#b5b8bf;
            font-style:italic;
        }}

        QLabel#statusOk,
        QLabel#successText {{
            color:#10b981;
            font-weight:700;
        }}

        QComboBox,
        QLineEdit,
        QSpinBox,
        QPlainTextEdit,
        QTextEdit {{
            background:#26272b;
            color:#e6e6e6;
            border:1px solid #2f3136;
            border-radius:8px;
            padding:8px 12px;
            selection-background-color:{BRAND_YELLOW};
            selection-color:#000000;
        }}

        QComboBox:focus,
        QLineEdit:focus,
        QSpinBox:focus,
        QPlainTextEdit:focus,
        QTextEdit:focus {{
            border:1px solid {BRAND_YELLOW};
        }}

        QPushButton {{
            background:{BRAND_YELLOW};
            color:#000000;
            border:none;
            border-radius:10px;
            padding:10px 18px;
            font-weight:700;
        }}

        QPushButton:hover {{
            background:#e6c51c;
        }}

        QPushButton:pressed {{
            background:#b89f14;
        }}

        QPushButton#themeToggle {{
            background:#000000;
            color:#ffd85a;
            border:none;
            min-width:56px;
            min-height:56px;
            border-radius:28px;
            font-size:20px;
            padding:0;
        }}

        QPushButton#headerClose {{
            background:#1f2023;
            color:#e6e6e6;
            border:1px solid #2f3136;
            min-width:56px;
            min-height:56px;
            border-radius:28px;
            font-size:18px;
            padding:0;
        }}

        QCheckBox,
        QRadioButton {{
            color:#e6e6e6;
            spacing:8px;
        }}

        QCheckBox::indicator,
        QRadioButton::indicator {{
            width:18px;
            height:18px;
            border-radius:4px;
            border:2px solid #2f3136;
            background:#26272b;
        }}

        QCheckBox::indicator:checked,
        QRadioButton::indicator:checked {{
            background:{BRAND_YELLOW};
            border-color:{BRAND_YELLOW};
        }}

        QSlider::groove:horizontal {{
            background:#1f2023;
            height:8px;
            border-radius:4px;
        }}

        QSlider::handle:horizontal {{
            background:{BRAND_YELLOW};
            width:18px;
            height:18px;
            border-radius:9px;
            margin:-6px 0;
        }}

        QSlider::handle:horizontal:hover {{
            background:#e6c51c;
        }}

        QTreeWidget {{
            background:#1f2023;
            color:#e6e6e6;
            border:1px solid #2f3136;
            border-radius:8px;
        }}

        QTreeWidget::item:selected {{
            background:{BRAND_YELLOW};
            color:#000000;
        }}

        /* Menu / tooltip (match user_settings dark template) */
        QMenuBar {{
            background:#1f2023;
            color:#e6e6e6;
        }}

        QMenuBar::item:selected {{
            background:#232427;
        }}

        QToolTip {{
            background:#1f2023;
            color:#e6e6e6;
            border:1px solid #2f3136;
        }}

        /* Scroll containers */
        QScrollArea {{
            background:{DARK_BG};
            border:none;
        }}

        QAbstractScrollArea,
        QScrollArea,
        QTreeWidget,
        QListWidget,
        QTextEdit,
        QPlainTextEdit {{
            background:{DARK_BG};
            border:1px solid #2f3136;
            border-radius:8px;
        }}

        /* ==== Tables in dark mode ==== */
        QTableView,
        QTableWidget {{
            background: #26272b;
            color: #e6e6e6;
            gridline-color: #3a3b40;
            selection-background-color: {BRAND_YELLOW};
            selection-color: #000000;
            border: 1px solid #2f3136;
        }}

        /* Header row */
        QHeaderView::section {{
            background: #333439;
            color: #e6e6e6;
            padding: 4px 8px;
            border: 1px solid #3a3b40;
        }}

        /* Top-left corner piece of the table */
        QTableCornerButton::section {{
            background: #333439;
            border: 1px solid #3a3b40;
        }}


        QScrollArea > QWidget#qt_scrollarea_viewport,
        QAbstractScrollArea > QWidget#qt_scrollarea_viewport,
        QTreeWidget > QWidget#qt_scrollarea_viewport,
        QListWidget > QWidget#qt_scrollarea_viewport,
        QTextEdit > QWidget#qt_scrollarea_viewport,
        QPlainTextEdit > QWidget#qt_scrollarea_viewport {{
            background:{DARK_BG};
        }}

        QWidget#qt_scrollarea_vcontainer,
        QWidget#qt_scrollarea_hcontainer {{
            background:{DARK_BG};
        }}

        /* All scrollbars - dark background */
        QScrollBar {{
            background:#3a3b40;
            border:none;
        }}

        /* Vertical scrollbar */
        QScrollBar:vertical {{
            background:#3a3b40;
            width:12px;
            border:none;
            margin:0;
        }}

        QScrollBar::handle:vertical {{
            background:#5a5b61;
            border-radius:6px;
            min-height:40px;
        }}

        QScrollBar::handle:vertical:hover {{
            background:#6a6b71;
        }}

        QScrollBar::add-line:vertical,
        QScrollBar::sub-line:vertical {{
            background:#3a3b40;
            border:none;
            height:0px;
        }}

        QScrollBar::add-page:vertical,
        QScrollBar::sub-page:vertical {{
            background:#3a3b40;
        }}

        QScrollBar::up-arrow:vertical,
        QScrollBar::down-arrow:vertical {{
            background:#3a3b40;
        }}

        /* Horizontal scrollbar */
        QScrollBar:horizontal {{
            background:#3a3b40;
            height:12px;
            border:none;
            margin:0;
        }}

        QScrollBar::handle:horizontal {{
            background:#5a5b61;
            border-radius:6px;
            min-width:40px;
        }}

        QScrollBar::handle:horizontal:hover {{
            background:#6a6b71;
        }}

        QScrollBar::add-line:horizontal,
        QScrollBar::sub-line:horizontal {{
            background:#3a3b40;
            border:none;
            width:0px;
        }}

        QScrollBar::add-page:horizontal,
        QScrollBar::sub-page:horizontal {{
            background:#3a3b40;
        }}

        QScrollBar::left-arrow:horizontal,
        QScrollBar::right-arrow:horizontal {{
            background:#3a3b40;
        }}
    """

    _QSS_LIGHT = f"""
        * {{ font-family: "Microsoft YaHei UI", "Segoe UI", "Noto Sans TC"; }}
        QMainWindow{{ background-color:#f5f5f9; }}
        QStatusBar{{ background:#ffffff; color:#2d3142; border-top:1px solid #bccfef; }}
        QWidget#header{{ background:qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #d7deec, stop:1 #bccfef); border-bottom:1px solid #a2bef2; }}
        QLabel#mainTitle{{ color:#2d3142; font-size:28px; font-weight:700; }}
        QLabel#subtitle{{ color:#4a5568; font-size:13px; }}
        QPushButton#themeToggle{{ background:{BRAND_BLUE}; color:#ffffff; border:none; min-width:56px; min-height:56px; border-radius:28px; font-size:22px; padding:0; }}
        QPushButton#headerClose{{ background:#ffffff; color:#2d3142; border:1px solid #bccfef; min-width:56px; min-height:56px; border-radius:28px; font-size:18px; padding:0; }}
        QTabWidget#mainTabs::pane{{ border:1px solid #e0e0e8; background:#ffffff; border-radius:8px; }}
        QTabBar::tab{{ background:#d7deec; color:#5a5a66; padding:12px 24px; margin-right:4px; border-top-left-radius:8px; border-top-right-radius:8px; font-weight:600; }}
        QTabBar::tab:selected{{ background:#ffffff; color:#2d3142; }}
        QTabBar::tab:hover:!selected{{ background:#a2bef2; }}
        QGroupBox#settingsGroup{{ background:#ffffff; border:1px solid #bccfef; border-radius:10px; margin-top:12px; padding-top:20px; font-weight:600; font-size:14px; color:#2d3142; line-height:1.2; }}
        QGroupBox#settingsGroup::title{{ subcontrol-origin:margin; left:15px; padding:0 8px; }}
        QPushButton{{ background:#739ef0; color:#ffffff; border:none; min-width:56px; min-height:56px; padding:10px 20px; border-radius:8px; font-weight:600; font-size:13px; }}
        QPushButton:hover{{ background:#4a7cdb; }}
        QPushButton:pressed{{ background:#2558b5; }}
        QPushButton:disabled{{ background:#c5c5d0; color:#8a8a9a; }}
        QCheckBox, QRadioButton{{ color:#2d3142; spacing:8px; }}
        QCheckBox::indicator, QRadioButton::indicator{{ width:20px; height:20px; border-radius:4px; background:#ffffff; border:2px solid #bccfef; }}
        QCheckBox::indicator:checked, QRadioButton::indicator:checked{{ background:{BRAND_BLUE}; border:2px solid {BRAND_BLUE}; }}
        QSlider::groove:horizontal{{ background:#d7deec; height:8px; border-radius:4px; }}
        QSlider::handle:horizontal{{ background:#739ef0; width:20px; height:20px; border-radius:10px; margin:-6px 0; }}
        QSlider::handle:horizontal:hover{{ background:{BRAND_BLUE}; }}
        QComboBox, QLineEdit, QSpinBox, QPlainTextEdit, QTextEdit{{ background:#ffffff; color:#2d3142; border:1px solid #bccfef; border-radius:6px; padding:8px 12px; selection-background-color:#739ef0; selection-color:#ffffff; }}
        QLabel{{ color:#2d3142; }}
        QLabel#infoText{{ color:#739ef0; font-style:italic; }}
        QLabel#statusOk, QLabel#successText{{ color:#10b981; font-weight:700; }}
        QFrame#bottomBar{{ background:#ffffff; border-top:1px solid #bccfef; }}
        QTreeWidget{{ background:#ffffff; border:1px solid #bccfef; border-radius:6px; color:#2d3142; }}
        QTreeWidget::item:selected{{ background:#739ef0; color:#ffffff; }}

        /* Replace default/white backgrounds in certain widgets with white */
        QScrollArea {{ background:#ffffff; border:none; }}

        /* Scroll container viewport fix */
        QAbstractScrollArea,
        QScrollArea,
        QTreeWidget,
        QListWidget,
        QTextEdit,
        QPlainTextEdit {{ background:#ffffff; border:1px solid #bccfef; border-radius:6px; }}
        QScrollArea > QWidget#qt_scrollarea_viewport,
        QAbstractScrollArea > QWidget#qt_scrollarea_viewport,
        QTreeWidget > QWidget#qt_scrollarea_viewport,
        QListWidget > QWidget#qt_scrollarea_viewport,
        QTextEdit > QWidget#qt_scrollarea_viewport,
        QPlainTextEdit > QWidget#qt_scrollarea_viewport {{ background:#ffffff; }}
        QWidget#qt_scrollarea_vcontainer,
        QWidget#qt_scrollarea_hcontainer {{ background:#ffffff; }}

        /* Scrollbars - light theme */
        QScrollBar:vertical{{ background:#e0e0e0; width:12px; border-radius:6px; }}
        QScrollBar::handle:vertical{{ background:#a2bef2; border-radius:6px; min-height:40px; }}
        QScrollBar::handle:vertical:hover {{ background:#739ef0; }}
        QScrollBar::add-line:vertical,
        QScrollBar::sub-line:vertical {{ background:#e0e0e0; border:none; height:0px; }}
        QScrollBar::add-page:vertical,
        QScrollBar::sub-page:vertical {{ background:#e0e0e0; }}
        QScrollBar::up-arrow:vertical,
        QScrollBar::down-arrow:vertical {{ background:#e0e0e0; }}

        /* Horizontal scrollbar */
        QScrollBar:horizontal {{ background:#e0e0e0; height:12px; border-radius:6px; }}
        QScrollBar::handle:horizontal {{ background:#a2bef2; border-radius:6px; min-width:40px; }}
        QScrollBar::handle:horizontal:hover {{ background:#739ef0; }}
        QScrollBar::add-line:horizontal,
        QScrollBar::sub-line:horizontal {{ background:#e0e0e0; border:none; width:0px; }}
        QScrollBar::add-page:horizontal,
        QScrollBar::sub-page:horizontal {{ background:#e0e0e0; }}
        QScrollBar::left-arrow:horizontal,
        QScrollBar::right-arrow:horizontal {{ background:#e0e0e0; }}
    """

    def __init__(self):
        super().__init__()
        self._settings = QSettings("UEP", "Theme")
        saved = self._settings.value("theme", Theme.LIGHT.value, type=str)
        self._theme = Theme(saved) if saved in (t.value for t in Theme) else Theme.LIGHT

    @property
    def theme(self) -> Theme:
        return self._theme

    def qss(self) -> str:
        return self._QSS_DARK if self._theme == Theme.DARK else self._QSS_LIGHT

    def apply_app(self):
        app = QApplication.instance()
        if app:
            app.setStyleSheet(self.qss())

    def set_theme(self, theme: Theme):
        if theme != self._theme:
            self._theme = theme
            self._settings.setValue("theme", theme.value)
            self._settings.sync()
            self.apply_app()
            self.theme_changed.emit(theme.value)

    def toggle(self):
        self.set_theme(Theme.DARK if self._theme == Theme.LIGHT else Theme.LIGHT)


theme_manager = ThemeManager()


def _refresh_widget(widget):
    try:
        widget.style().unpolish(widget)
        widget.style().polish(widget)
        widget.update()
    except Exception:
        pass


def install_theme_hook(widget):
    try:
        widget.setAttribute(Qt.WA_StyledBackground, True)
    except Exception:
        pass

    try:
        s = widget.styleSheet()
        if s:
            cleaned = s
            try:
                if theme_manager and theme_manager.theme == Theme.DARK:
                    cleaned = cleaned.replace("background: #ffffff;", f"background: {ThemeManager.LIGHT_GREY};")
                    cleaned = cleaned.replace("background:#ffffff;", f"background:{ThemeManager.LIGHT_GREY};")
                    cleaned = cleaned.replace("background-color: #ffffff;", f"background-color: {ThemeManager.LIGHT_GREY};")
                    cleaned = cleaned.replace("background-color:#ffffff;", f"background-color:{ThemeManager.LIGHT_GREY};")
                    cleaned = cleaned.replace("background: white;", f"background: {ThemeManager.LIGHT_GREY};")
                    cleaned = cleaned.replace("background-color: white;", f"background-color: {ThemeManager.LIGHT_GREY};")
                    cleaned = cleaned.replace("background: #fff;", f"background: {ThemeManager.LIGHT_GREY};")
                    cleaned = cleaned.replace("background:#fff;", f"background:{ThemeManager.LIGHT_GREY};")
                else:
                    cleaned = s
            except Exception:
                cleaned = s

            if cleaned != s:
                widget.setStyleSheet(cleaned)
    except Exception:
        pass

    try:
        theme_manager.theme_changed.connect(lambda _: _refresh_widget(widget))
    except Exception:
        pass

    _refresh_widget(widget)
