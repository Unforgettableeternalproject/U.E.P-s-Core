# state_profile.py
import os
import sys
from typing import Dict, Any

try:
    from PyQt5.QtWidgets import (
        QWidget, QVBoxLayout, QHBoxLayout, QGroupBox, QScrollArea,
        QFrame, QPushButton, QSizePolicy, QComboBox, QLineEdit, QLabel,
        QApplication, QMessageBox, QStatusBar, QDialog
    )
    from PyQt5.QtCore import Qt, QTimer, pyqtSignal, QSettings, QSize, QRect
    from PyQt5.QtGui import QFont, QIcon, QPixmap, QPainter, QPainterPath, QColor, QBrush
    PYQT5_AVAILABLE = True
except ImportError:
    PYQT5_AVAILABLE = False
    print("[StateProfile] PyQt5 不可用")

try:
    from theme_manager import theme_manager, Theme, install_theme_hook
except Exception:
    theme_manager = None
    Theme = None
    def install_theme_hook(_): pass


class UEPStateProfileWidget(QWidget):
    settings_changed = pyqtSignal(str, object)
    apply_requested = pyqtSignal(dict)

    SCROLL_AREA_MIN_H = 620

    def __init__(self, parent=None):
        super().__init__(parent)
        if not PYQT5_AVAILABLE:
            return

        install_theme_hook(self)

        self.settings = QSettings("UEP", "DiaryStation")

        self._text_feels = "I feel bright and steady, like sunshine after rain."
        self._text_helped = "Organized your study plan and fixed two UI bugs."
        self._text_tips = "Tip: Short breaks (5–10m) boost focus. Try 25/5 Pomodoro."
        self._image_path = None

        self._build_ui()
        self._load_persisted()
        self._refresh_image()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        container = QWidget()
        install_theme_hook(container)
        containerLayout = QVBoxLayout(container)
        containerLayout.setContentsMargins(0, 0, 0, 0)
        containerLayout.setSpacing(0)
        root.addWidget(container)

        self.imageLabel = QLabel(container)
        self.imageLabel.setObjectName("diaryImageLabel")
        self.imageLabel.setAlignment(Qt.AlignCenter)
        self.imageLabel.setFixedSize(200, 200)
        self.imageLabel.move(0, 20)
        self.imageLabel.setScaledContents(False)
        self.imageLabel.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.imageLabel.raise_()

        scroll = QScrollArea()
        install_theme_hook(scroll)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        scroll.setMinimumHeight(self.SCROLL_AREA_MIN_H)
        containerLayout.addWidget(scroll)

        body = QWidget()
        install_theme_hook(body)
        scroll.setWidget(body)

        mainLayout = QVBoxLayout(body)
        mainLayout.setContentsMargins(30, 30, 30, 30)
        mainLayout.setSpacing(20)

        self.cardFeels = self._make_card(" U.E.P now feels…")
        self.cardHelped = self._make_card(" U.E.P lately helped you to…")
        self.cardTips = self._make_card(" Random facts / tips")

        self.feelsLabel = self._card_text_label()
        self.helpedLabel = self._card_text_label()
        self.tipsLabel = self._card_text_label()

        self._put_content(self.cardFeels, self.feelsLabel)
        self._put_content(self.cardHelped, self.helpedLabel)
        self._put_content(self.cardTips, self.tipsLabel)

        mainLayout.addWidget(self.cardFeels)
        mainLayout.addWidget(self.cardHelped)
        mainLayout.addWidget(self.cardTips)
        mainLayout.addStretch()

        self.containerWidget = container

    def _make_card(self, title: str) -> QGroupBox:
        box = QGroupBox(title)
        box.setObjectName("settingsGroup")
        install_theme_hook(box)
        box.setMinimumHeight(500)
        box.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        lay = QVBoxLayout(box)
        lay.setContentsMargins(18, 16, 18, 18)
        lay.setSpacing(10)
        return box

    def _card_text_label(self) -> QLabel:
        lb = QLabel()
        lb.setObjectName("diaryText")
        lb.setWordWrap(True)
        lb.setTextInteractionFlags(Qt.TextSelectableByMouse)
        f = QFont()
        f.setPointSize(11)
        lb.setFont(f)
        install_theme_hook(lb)
        return lb

    def _put_content(self, card: QGroupBox, label: QLabel):
        lay: QVBoxLayout = card.layout()
        lay.addWidget(label)

    def set_diary_texts(self, feels: str = None, helped: str = None):
        if feels is not None:
            self._text_feels = feels
        if helped is not None:
            self._text_helped = helped
        self._refresh_texts()
        self.settings_changed.emit("diary_texts", {
            "feels": self._text_feels, "helped": self._text_helped
        })

    def set_random_tips(self, tips: str):
        self._text_tips = tips
        self._refresh_texts()
        self.settings_changed.emit("diary_tips", tips)

    def set_uep_image(self, path: str):
        self._image_path = path
        self._refresh_image()
        self.settings_changed.emit("diary_image", path)

    def apply_theme(self):
        if theme_manager:
            theme_manager.apply_app()
        try:
            self.style().unpolish(self)
            self.style().polish(self)
            self.update()
        except Exception:
            pass

    def _load_persisted(self):
        self._text_feels  = self.settings.value("feels",  self._text_feels,  type=str)
        self._text_helped = self.settings.value("helped", self._text_helped, type=str)
        self._text_tips   = self.settings.value("tips",   self._text_tips,   type=str)
        self._image_path  = self.settings.value("image_path", None, type=str)
        self._refresh_texts()
        self._refresh_image()

    def load_settings(self):
        self._load_persisted()

    def save_to_qsettings(self):
        self.settings.setValue("feels", self._text_feels)
        self.settings.setValue("helped", self._text_helped)
        self.settings.setValue("tips", self._text_tips)
        if self._image_path:
            self.settings.setValue("image_path", self._image_path)
        self.settings.sync()

    def get_settings_dict(self) -> Dict[str, Any]:
        return {
            "diary": {
                "feels": self._text_feels,
                "helped": self._text_helped,
                "tips": self._text_tips,
                "image_path": self._image_path or ""
            }
        }

    def _refresh_texts(self):
        self.feelsLabel.setText(self._text_feels)
        self.helpedLabel.setText(self._text_helped)
        self.tipsLabel.setText(self._text_tips)

    def _rounded_pixmap(self, pm: QPixmap, target_size: QSize, radius: int = 22) -> QPixmap:
        if pm.isNull() or not target_size.isValid():
            return QPixmap()
        scaled = pm.scaled(target_size, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
        w, h = target_size.width(), target_size.height()
        result = QPixmap(w, h)
        result.fill(Qt.transparent)
        painter = QPainter(result)
        painter.setRenderHint(QPainter.Antialiasing, True)
        path = QPainterPath()
        path.addRoundedRect(QRect(0, 0, w, h), radius, radius)
        painter.setClipPath(path)
        painter.drawPixmap(0, 0, scaled)
        painter.end()
        return result

    def _refresh_image(self):
        path = self._image_path
        if not path or not os.path.exists(path):
            self.imageLabel.clear()
            return

        pm = QPixmap(path)
        if pm.isNull():
            self.imageLabel.clear()
            return

        inner_w = self.imageLabel.width() - 20
        inner_h = self.imageLabel.height() - 20
        rounded = self._rounded_pixmap(pm, QSize(max(40, inner_w), max(40, inner_h)), radius=24)
        self.imageLabel.setPixmap(rounded)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "containerWidget") and hasattr(self, "imageLabel"):
            w = self.containerWidget.width()
            self.imageLabel.move(max(0, w - self.imageLabel.width() - 24), 20)


class StateProfileDialog(QDialog):
    settings_changed = pyqtSignal(str, object)

    def __init__(self, controller=None, parent=None):
        super().__init__(parent)
        if not PYQT5_AVAILABLE:
            return

        self.controller = controller
        self.settings = QSettings("UEP", "StateProfile")

        self.setWindowTitle("UEP 狀態檔案")
        self.setWindowFlags(
            Qt.Window |
            Qt.WindowTitleHint |
            Qt.WindowMinMaxButtonsHint |
            Qt.WindowCloseButtonHint
        )

        self.setMinimumSize(900, 950)
        self.resize(1200, 950)

        try:
            icon_path = os.path.join(os.path.dirname(__file__), "../../../arts/U.E.P.png")
            if os.path.exists(icon_path):
                self.setWindowIcon(QIcon(icon_path))
        except Exception:
            pass

        install_theme_hook(self)
        self._build_ui()
        self.load_settings()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        header = QFrame()
        header.setObjectName("header")
        header.setFixedHeight(110)
        install_theme_hook(header)

        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(30, 16, 30, 16)
        header_layout.setSpacing(16)

        title_container = QVBoxLayout()
        title_label = QLabel("狀態檔案")
        title_label.setObjectName("mainTitle")
        subtitle = QLabel("調整 U.E.P 的行為、情緒與學習模式")
        subtitle.setObjectName("subtitle")

        title_container.addWidget(title_label)
        title_container.addWidget(subtitle)
        title_container.addStretch()

        header_layout.addLayout(title_container)
        header_layout.addStretch()

        self.theme_toggle = QPushButton("🌙")
        self.theme_toggle.setObjectName("themeToggle")
        self.theme_toggle.setFixedSize(56, 56)
        self.theme_toggle.setCursor(Qt.PointingHandCursor)
        btn_font = QFont("Segoe UI Emoji, Apple Color Emoji, Noto Color Emoji")
        btn_font.setPointSize(20)
        self.theme_toggle.setFont(btn_font)
        self.theme_toggle.clicked.connect(self.toggle_theme)

        header_layout.addWidget(self.theme_toggle)
        layout.addWidget(header)

        self.panel = UEPStateProfileWidget(parent=self)
        self.panel.settings_changed.connect(lambda k, v: self.settings_changed.emit(k, v))
        self.panel.apply_requested.connect(self._handle_apply_request)
        layout.addWidget(self.panel)

        bottom = QFrame()
        bottom.setObjectName("bottomBar")
        bottom.setFixedHeight(80)
        install_theme_hook(bottom)

        bottom_layout = QHBoxLayout(bottom)
        bottom_layout.setContentsMargins(30, 15, 30, 15)

        self.btn_apply = QPushButton("✓ 套用設定")
        self.btn_reset = QPushButton("🔄 重置為預設值")
        self.btn_cancel = QPushButton("取消")

        self.btn_apply.clicked.connect(self.apply_settings)
        self.btn_reset.clicked.connect(self.reset_to_defaults)
        self.btn_cancel.clicked.connect(self.close)

        bottom_layout.addWidget(self.btn_apply)
        bottom_layout.addWidget(self.btn_reset)
        bottom_layout.addStretch()
        bottom_layout.addWidget(self.btn_cancel)

        layout.addWidget(bottom)

        status = QStatusBar()
        install_theme_hook(status)
        status.showMessage("狀態檔案已就緒")
        layout.addWidget(status)

        if theme_manager:
            theme_manager.apply_app()
            theme_manager.theme_changed.connect(self.apply_theme)
            self.apply_theme(theme_manager.theme.value)

    def apply_theme(self, theme_name=None):
        if theme_manager:
            self.theme_toggle.setText("☀️" if theme_manager.theme == Theme.DARK else "🌙")
        try:
            self.style().unpolish(self)
            self.style().polish(self)
            self.update()
        except Exception:
            pass

    def toggle_theme(self):
        if theme_manager:
            theme_manager.toggle()
            self.apply_theme()
            self.panel.apply_theme()

    def load_settings(self):
        if theme_manager:
            self.theme_toggle.setText("☀️" if theme_manager.theme == Theme.DARK else "🌙")
        self.panel.load_settings()

    def apply_settings(self):
        settings_dict = self.panel.get_settings_dict()
        self.panel.save_to_qsettings()
        self._handle_apply_request(settings_dict)
        QMessageBox.information(self, "成功", "設定已套用！")

    def reset_to_defaults(self):
        reply = QMessageBox.question(
            self, "確認重置",
            "確定要將所有設定重置為預設值嗎？",
            QMessageBox.Yes | QMessageBox.No
        )
        if reply == QMessageBox.Yes:
            self.settings.clear()
            self.panel.load_settings()
            QMessageBox.information(self, "完成", "已重置為預設值")

    def _handle_apply_request(self, data: dict):
        if hasattr(self, 'controller') and self.controller:
            try:
                self.controller.process_input("apply_state_profile", {"settings": data, "source": "ui"})
            except Exception as e:
                print("[StateProfileDialog] Failed to communicate with controller:", e)


def create_test_window():
    if not PYQT5_AVAILABLE:
        return None
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    if theme_manager:
        theme_manager.apply_app()
    window = StateProfileDialog()
    window.show()
    return app, window


if __name__ == "__main__":
    app, window = create_test_window()
    if app and window:
        sys.exit(app.exec_())
