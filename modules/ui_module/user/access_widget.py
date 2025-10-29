#access widget
import sys, math, json
from functools import partial

from PyQt5.QtWidgets import (
    QApplication, QWidget, QPushButton, QLineEdit, QVBoxLayout, QLabel,
    QFrame, QDialog, QHBoxLayout, QTextEdit, QGraphicsDropShadowEffect
)
from PyQt5.QtCore import  Qt, QPropertyAnimation, QPoint, QEasingCurve, QTimer, pyqtSignal, QSize, QSettings
from PyQt5.QtGui import QPainter, QColor, QBrush, QPen, QFont, QPixmap, QIcon, QFont, QRegion
from user_settings import UserMainWindow
from system_background import SystemBackgroundWindow
from state_profile import StateProfileWindow


import os
try:
    from core.unified_controller import unified_controller
except Exception:
    unified_controller = None

def info_log(*a): print(*a)
def debug_log(*a): print(*a)
def error_log(*a): print(*a)

class ControllerBridge:
    def __init__(self, controller):
        self.controller = controller
        self._settings_dialog = None
        self._bg_window = None
        self._state_dialog = None

    def dispatch(self, fid: str):
        info_log(f"[ControllerBridge] dispatch:{fid}")
        if fid == "user_settings":
            return self.open_user_settings()
        if fid == "system_background":
            return self.open_system_background()
        if fid == "state_profile":
            return self.show_state_profile()
        info_log(f"[ControllerBridge] ?芰?:{fid}")

    def open_user_settings(self):
        try: 
            if self._settings_dialog is None or not self._settings_dialog.isVisible(): 
                self._settings_dialog = PersonalSettingsDialog(controller=self.controller) 
                self._settings_dialog.show()
                self._settings_dialog.raise_() 
                self._settings_dialog.activateWindow() 
                info_log("[ControllerBridge] 已開啟個人設定對話框") 
                return {"success": True}
        except Exception as e: 
            error_log("[ControllerBridge] 開啟個人設定失敗:", e) 
            return {"success": False, "error": str(e)}

    def open_system_background(self):
        try:
            if self._bg_window is None or not self._bg_window.isVisible():
               self._bg_window = SystemBackgroundWindow(ui_module=self.controller)
               self._bg_window.window_closed.connect(lambda: setattr(self, "_bg_window", None))
            self._bg_window.show()
            self._bg_window.raise_()
            self._bg_window.activateWindow()
            info_log("[ControllerBridge] 已開啟 SystemBackgroundWindow")
            return {"success": True}
        except Exception as e:
            error_log("[ControllerBridge] 開啟 SystemBackgroundWindow 失敗:", e)
            return {"success": False, "error": str(e)}           

    def show_state_profile(self):
        try:
            if self._state_dialog is None or not self._state_dialog.isVisible():
                self._state_dialog = StateProfileWindow(ui_module=self.controller)
                self._state_dialog.window_closed.connect(lambda: setattr(self, "_state_dialog", None))
            self._state_dialog.show()
            self._state_dialog.raise_()
            self._state_dialog.activateWindow()
            info_log("[ControllerBridge] 已開啟狀態檔案設定")
            return {"success": True}
        except Exception as e:
            error_log("[ControllerBridge] 開啟狀態檔案設定失敗:", e)
            return {"success": False, "error": str(e)}

class PersonalSettingsDialog(QDialog):
    def __init__(self, controller=None, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.setWindowTitle("UEP?犖閮剖?")
        self.setModal(False)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.resize(900, 650)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        self.settings_window = UserMainWindow(ui_module=controller)
        outer.addWidget(self.settings_window)

    def show(self):
        super().show()
        self.settings_window.show()

    def close(self):
        self.settings_window.close()
        super().close()


class StateProfileDialog(QDialog):
    def __init__(self, controller=None, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.setWindowTitle("UEP 狀態檔案設定")
        self.setModal(False)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.resize(720, 560)

        self._settings = QSettings("UEP", "SystemBackground")
        self.dark_mode = self._settings.value("theme/dark_mode", False, type=bool)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)

        self.frame = QFrame(self)
        self.frame_layout = QVBoxLayout(self.frame)
        self.frame_layout.setContentsMargins(20, 20, 20, 20)
        outer.addWidget(self.frame)

        self.title = QLabel("狀態檔案")
        self.subtitle = QLabel("調整 U.E.P 的行為、情緒與學習模式")
        self.title.setObjectName("mainTitle")
        self.subtitle.setObjectName("subtitle")
        self.frame_layout.addWidget(self.title)
        self.frame_layout.addWidget(self.subtitle)

        self.panel = UEPStateProfileWidget(parent=self.frame)
        self.frame_layout.addWidget(self.panel, 1)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self.btn_apply = QPushButton("套用")
        self.btn_ok = QPushButton("套用")
        self.btn_cancel = QPushButton("取消")
        btn_row.addWidget(self.btn_apply)
        btn_row.addWidget(self.btn_ok)
        btn_row.addWidget(self.btn_cancel)
        self.frame_layout.addLayout(btn_row)

        self.btn_apply.clicked.connect(self.apply_settings)
        self.btn_ok.clicked.connect(self.confirm_and_close)
        self.btn_cancel.clicked.connect(self.close)

        self.apply_theme()

        self.panel.dark_mode = self.dark_mode
        self.panel.apply_theme()

    def apply_theme(self):
        if self.dark_mode:
            self.setStyleSheet("""
                QDialog { background: transparent; }
                QFrame { background:#1f2023; border:1px solid #2f3136; border-radius:14px; }
                QLabel#mainTitle { color:#ffffff; font-size:18px; font-weight:700; }
                QLabel#subtitle { color:#b5b8bf; font-size:13px; }
                QPushButton { background:#d5b618; color:#000; border:none; border-radius:8px; padding:10px 20px; font-weight:700; }
                QPushButton:hover { background:#e6c51c; }
                QPushButton:pressed { background:#b89f14; }
            """)
        else:
            self.setStyleSheet("""
                QDialog { background: transparent; }
                QFrame { background:#ffffff; border:1px solid #e0e0e8; border-radius:14px; }
                QLabel#mainTitle { color:#2d3142; font-size:18px; font-weight:700; }
                QLabel#subtitle { color:#4a5568; font-size:13px; }
                QPushButton { background:#739ef0; color:#fff; border:none; border-radius:8px; padding:10px 20px; font-weight:600; }
                QPushButton:hover { background:#4a7cdb; }
                QPushButton:pressed { background:#2558b5; }
            """)

    def apply_settings(self):
        self.panel.save_to_qsettings()

    def confirm_and_close(self):
        self.apply_settings()
        self.close()


    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setBrush(QBrush(QColor(0, 0, 0, 80)))
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(self.rect(), 16, 16)

    def _add_shadow(self, widget):
        effect = QGraphicsDropShadowEffect()
        effect.setBlurRadius(10)
        effect.setOffset(0, 2)
        effect.setColor(QColor(0, 0, 0, 80))
        widget.setGraphicsEffect(effect)

    def _apply_from_dict(self, data: dict):
        self.panel.save_to_qsettings()
        if self.controller:
            try:
                self.controller.process_input("apply_state_profile", {"settings": data, "source": "ui"})
            except Exception as e:
                error_log("[StateProfileDialog] ?controller憭望?:", e)

    def _bubble_setting(self, key, value):
        pass

    def apply_settings(self):
        self._apply_from_dict(self.panel.get_settings_dict())

    def confirm_and_close(self):
        self.apply_settings()
        self.close()

class DraggableButton(QPushButton):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._drag_start = None
        self._dragging = False

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._drag_start = e.globalPos()
            self._widget_offset = self.window().frameGeometry().topLeft()
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self._drag_start and (e.buttons() & Qt.LeftButton):
            if (e.globalPos() - self._drag_start).manhattanLength() > 6:
                self._dragging = True
            if self._dragging:
                self.window().move(e.globalPos() - (self._drag_start - self._widget_offset))
                e.accept()
                return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        if self._dragging:
            self._drag_start = None
            self._dragging = False
            e.accept()
            return
        self._drag_start = None
        self._dragging = False
        super().mouseReleaseEvent(e)

class UserAccessWidget(QWidget):
    function_requested = pyqtSignal(str)
    expanded_changed = pyqtSignal(bool)

    def __init__(self, bridge: ControllerBridge):
        super().__init__()
        self.bridge = bridge
        self.is_expanded = False
        self.is_dragging = False
        self.drag_offset = QPoint()
        self._build_ui()
        self._place_top_right()
        info_log("[UserAccessWidget] init done")

    def _build_ui(self):
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setFixedSize(70, 140)

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        self.title_label = QLabel("UEP")
        self.title_label.setAlignment(Qt.AlignCenter)
        self.title_label.setFont(QFont("Arial", 11, QFont.Bold))
        self.title_label.setStyleSheet("""
            QLabel {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #346ddb, stop:1 #4a7cdb);
                color: white;
                border-radius: 8px;
                padding: 6px;
                letter-spacing: 1px;
            }
        """)
        self._add_shadow(self.title_label)
        root.addWidget(self.title_label)

        self.toggle_button = QPushButton("▼")
        self.toggle_button.setFixedSize(54, 30)
        self.toggle_button.clicked.connect(self.toggle_expanded)
        self.toggle_button.setStyleSheet("""
            QPushButton {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #fafafa, stop:1 #e0e0e8);
                color: #2d3142;
                border: none;
                border-radius: 8px;
                font-weight: 700;
                font-size: 14px;
            }
            QPushButton:hover {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #f0f0f5, stop:1 #dadade);
            }
            QPushButton:pressed {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #e0e0e8, stop:1 #c0c0c8);
            }
        """)
        self._add_shadow(self.toggle_button)
        root.addWidget(self.toggle_button)

        self.function_container = QFrame()
        self.function_container.setVisible(False)
        self.function_container.setStyleSheet("QFrame { background: transparent; border: none; }")
        fn_layout = QVBoxLayout(self.function_container)
        fn_layout.setContentsMargins(0, 0, 0, 0)
        fn_layout.setSpacing(6)

        buttons = [
            ("⚙️", "user_settings", "使用者設定"), 
            ("🖼️", "system_background", "系統背景"), 
            ("📊", "state_profile", "系統監控"), 
        ]
        for icon, fid, tip in buttons:
            b = QPushButton(icon)
            b.setToolTip(tip)
            b.setFixedSize(54, 36)
            b.clicked.connect(lambda _=False, f=fid: self._emit_request(f))
            b.setStyleSheet("""
                QPushButton {
                    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #ffffff, stop:1 #f5f5fa);
                    border: None;
                    border-radius: 10px;
                    font-size: 18px;
                }
                QPushButton:hover {
                    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #f0f0f5, stop:1 #e6e6fa);
                    border-color: #346ddb;
                }
                QPushButton:pressed {
                    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #e0e0e8, stop:1 #c0c0c8);
                }
            """)
            self._add_shadow(b)
            fn_layout.addWidget(b)

        root.addWidget(self.function_container)
        root.addStretch()

    def _add_shadow(self, widget):
        effect = QGraphicsDropShadowEffect()
        effect.setBlurRadius(8)
        effect.setOffset(0, 2)
        effect.setColor(QColor(0, 0, 0, 60))
        widget.setGraphicsEffect(effect)

    def _place_top_right(self):
        app = QApplication.instance()
        screen = app.primaryScreen()
        if screen:
            g = screen.availableGeometry()
            x = g.right() - self.width() + 30
            y = g.top() + 180
            self.move(x, y)
        else:
            self.move(1200, 500)

    def toggle_expanded(self):
        self.is_expanded = not self.is_expanded
        if self.is_expanded:
            self.setFixedSize(70, 280)
            self.function_container.setVisible(True)
            self.toggle_button.setText("▲")
        else:
            self.setFixedSize(70, 140)
            self.function_container.setVisible(False)
            self.toggle_button.setText("▼")
        self.expanded_changed.emit(self.is_expanded)
        debug_log(f"[UserAccessWidget] {'展開' if self.is_expanded else '摺疊'}")

    def _emit_request(self, fid: str):
        debug_log(f"[UserAccessWidget] request:{fid}")
        self.function_requested.emit(fid)
        if self.bridge:
            self.bridge.dispatch(fid)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.is_dragging = True
            self.drag_offset = e.globalPos() - self.frameGeometry().topLeft()
            self.setCursor(Qt.ClosedHandCursor)
            e.accept()

    def mouseMoveEvent(self, e):
        if self.is_dragging and (e.buttons() & Qt.LeftButton):
            self.move(e.globalPos() - self.drag_offset)
            e.accept()

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.is_dragging = False
            self.setCursor(Qt.ArrowCursor)
            e.accept()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = self.rect()
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 255, 255, 250))
        p.drawRoundedRect(r, 12, 12)

class MainButton(QWidget):

    @staticmethod
    def set_button_image_fit(button:QPushButton, img_path: str, margin: int=0):
        d = min(button.width(), button.height())
        inner = max(0, d-2*margin)

        pix = QPixmap(img_path)
        if pix.isNull():
            error_log(f"[MainButton] Image not found: {img_path}")
            return

        scaled = pix.scaled(inner, inner, Qt.KeepAspectRatio, Qt.SmoothTransformation)

        button.setIcon(QIcon(scaled))
        button.setIconSize(QSize(inner,inner))
        button.setText("")
        button.setFlat(True)
        button.setFocusPolicy(Qt.NoFocus)

        button.setStyleSheet(f"""
            QPushButton, QPushButton:hover, QPushButton:pressed{{
                background: transparent;
                border: none;
                padding: 0px;
                border-radius: {d/2}px;
            }}
        """)

        button.setMask(QRegion(0,0,d,d,QRegion.Ellipse))
    
    def __init__(self, bridge: ControllerBridge = None, access_widget: UserAccessWidget = None):
        super().__init__()
        self.bridge = bridge
        self.access_widget = access_widget

        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.resize(400, 400)

        self.color_opt1 = "#E3F2FD"
        self.color_opt2 = "#E8F5E9"
        self.color_opt3 = "#FFF3E0"

        self.mainButton = self._make_opt_btn(110, "", "transparent", self.toggleMenu)
        self.set_button_image_fit(
            self.mainButton,
            r"C:\Users\Elisa Kao\Source\Repos\U.E.P-s-Core\arts\U.E.P.png",
            margin=6
        )

        self.mainButton.move(self.width()//2 - self.mainButton.width()//2,
                             self.height()//2 - self.mainButton.height()//2)

        #option buttons
        self.options = []
        opts = [ ("⚙️", "user_settings", self.color_opt1), 
                 ("🖼️", "system_background", self.color_opt2),
                 ("📊", "state_profile", self.color_opt3), 
        ]
        for label, fid, col in opts:
            b = self._make_opt_btn(60, label, col, partial(self._handle_option, fid))
            self._add_shadow(b)
            b.hide()
            self.options.append(b)


        #tool buttons
        self.tool_buttons = []
        self.TOOL_SIZE = 38
        self.TOOL_GAP  = 8      

        tools = [
            ("🗣", "tool_1"), 
            ("👂🏼", "tool_2"),
            ("😴", "tool_3"),
        ]
        for label, tid in tools:
            tb = self._make_tool_btn(self.TOOL_SIZE, label, partial(self._handle_option, tid))
            self._add_shadow(tb)
            tb.hide()
            self.tool_buttons.append(tb)

        self.dragPos = None
        self.right_click_timer = QTimer(self)
        self.right_click_timer.setSingleShot(True)
        self.right_click_timer.timeout.connect(self._enable_right_drag)
        self.right_drag_enabled = False

        self.is_pinned = False
        self.is_fully_visible = False
        self.original_position = None
        self.visible_position = None

        self.slide_animation = QPropertyAnimation(self, b"pos")
        self.slide_animation.setDuration(300)
        self.slide_animation.setEasingCurve(QEasingCurve.OutCubic)

        self.hover_check_timer = QTimer(self)
        self.hover_check_timer.timeout.connect(self._check_hover_state)
        self.hover_check_timer.start(100)

        self.expanded = False
        self._place_circle()

        self.auto_collapse_timer = QTimer(self)
        self.auto_collapse_timer.setSingleShot(True)
        self.auto_collapse_timer.timeout.connect(self._collapse_menu_if_needed)

    def _set_button_image(self, button: QPushButton, size: int, img_path: str):
        pix = QPixmap(img_path)
        if pix.isNull():
            error_log(f"[FloatingCircle] Image not found or invalid: {img_path}")
            return
        target = int(size * 0.85)  
        scaled = pix.scaled(target, target, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        button.setIcon(QIcon(scaled))
        button.setIconSize(QSize(target, target))
        button.setText("")
        button.setFlat(True)
        button.setFocusPolicy(Qt.NoFocus)

        button.setStyleSheet(f"""
            QPushButton,
            QPushButton:hover,
            QPushButton:pressed {{
                background: transparent;
                border: none;
                border-radius: {size/2}px;
            }}
        """)

        from PyQt5.QtGui import QRegion
        button.setMask(QRegion(0, 0, size, size, QRegion.Ellipse))

    def _place_circle(self):
        app = QApplication.instance()
        screen = app.primaryScreen()
        if screen:
            g = screen.availableGeometry()
            x = g.right() - self.width() // 2
            y = g.top() + 80
            self.move(x, y)
            self.original_position = QPoint(x, y)
            self.visible_position = QPoint(g.right() - self.width() + 20, y)
        else:
            self.move(1200, 40)
            self.original_position = QPoint(1200, 40)
            self.visible_position = QPoint(1030, 40)

    def _check_hover_state(self):
        if self.is_pinned:
            return
        global_cursor_pos = QApplication.instance().desktop().cursor().pos()
        widget_rect = self.geometry()
        detection_margin = 10
        expanded_rect = widget_rect.adjusted(-detection_margin, -detection_margin,
                                            detection_margin, detection_margin)
        is_hovering = expanded_rect.contains(global_cursor_pos)
        if is_hovering and not self.is_fully_visible:
            self._slide_to_visible()
        elif not is_hovering and self.is_fully_visible and not self.is_pinned:
            self._slide_to_hidden()
            if self.expanded:
                self._schedule_auto_collapse(900)
    def _slide_to_visible(self):
        if self.is_fully_visible or not self.visible_position:
            return
        self.slide_animation.stop()
        self.slide_animation.setStartValue(self.pos())
        self.slide_animation.setEndValue(self.visible_position)
        self.slide_animation.start()
        self.is_fully_visible = True

    def _slide_to_hidden(self):
        if not self.is_fully_visible or not self.original_position:
            return
        self.slide_animation.stop()
        self.slide_animation.setStartValue(self.pos())
        self.slide_animation.setEndValue(self.original_position)
        self.slide_animation.start()
        self.is_fully_visible = False

    def _enable_right_drag(self):
        self.right_drag_enabled = True
        if self.dragPos:
            cursor = QApplication.instance().desktop().cursor().pos()
            self.move(cursor - self.dragPos)

    def _add_shadow(self, w: QWidget):
        if sys.platform.startswith("win") and self.testAttribute(Qt.WA_TranslucentBackground):
            return
        eff = QGraphicsDropShadowEffect(self)
        eff.setOffset(0, 6)
        eff.setBlurRadius(20)
        eff.setColor(QColor(0, 0, 0, 60))
        w.setGraphicsEffect(eff)


    def _make_opt_btn(self, size, text, color, callback):
        b = DraggableButton(text, self)
        b.setFixedSize(size, size)
        b.setCursor(Qt.PointingHandCursor)

        font = QFont("Segoe UI Emoji")
        font.setPixelSize(int(size * 0.42))
        b.setFont(font)

        b.setStyleSheet(f"""
            QPushButton {{
                background: qlineargradient(x1:0, y1:1, x2:1, y2:0,
                    stop:0 rgba(201, 150, 20, 1),
                    stop:1 rgba(0, 67, 173, 1));
                    border-radius: {size/2}px;
                    color: #fff;
                    padding: 0px;
                    border: none;
            }}
            QPushButton:hover {{
                background: qlineargradient(x1:0, y1:1, x2:1, y2:0,
                    stop:0 rgba(201, 150, 20, 0.75),
                    stop:1 rgba(0, 67, 173, 0.75));
                border: none;
            }}
            QPushButton:pressed {{
                background: qlineargradient(x1:0, y1:1, x2:1, y2:0,
                    stop:0 rgba(201, 150, 20, 0.9),
                    stop:1 rgba(0, 67, 173, 0.9));
                border: none;
            }}
        """)

        b.clicked.connect(callback)
        return b

    def _make_tool_btn(self, size, text, callback):
        b = DraggableButton(text, self)
        b.setFixedSize(size, size)
        b.setCursor(Qt.PointingHandCursor)

        font = QFont("Segoe UI Emoji")
        font.setPixelSize(int(size * 0.42))
        b.setFont(font)

       
        b.setStyleSheet(f"""
            QPushButton {{
                background: qlineargradient(x1:0, y1:1, x2:1, y2:0,
                    stop:0 rgba(255, 174, 183,  1.00),      /* #ff4d4f */
                    stop:1 rgba(255, 174, 183,  1.00);     /* #d9363e */
                border-radius: {size/2}px;
                color: #fff;
                padding: 0px;
                border: none;
            }}
            QPushButton:hover {{
                background: qlineargradient(x1:0, y1:1, x2:1, y2:0,
                    stop:0 rgba(255, 174, 183,  0.75),
                    stop:1 rgba(255, 174, 183,  0.75);
                border: none;
            }}
            QPushButton:pressed {{
                background: qlineargradient(x1:0, y1:1, x2:1, y2:0,
                    stop:0 rgba(255, 174, 183,  0.90),
                    stop:1 rgba(255, 174, 183,  0.90);
                border: none;
            }}
        """)
        b.clicked.connect(callback)
        return b

    def mousePressEvent(self, e):
        self._cancel_auto_collapse()

        if e.button() == Qt.LeftButton:
            if self.expanded and not any(w.isVisible() and w.geometry().contains(e.pos())
                                         for w in [self.mainButton] + self.options + self.tool_buttons):
                self._collapse_menu()
                e.accept()
                return

            if not any(btn.geometry().contains(e.pos()) for btn in [self.mainButton] + self.options):
                self.dragPos = e.globalPos() - self.frameGeometry().topLeft()
                self.setCursor(Qt.ClosedHandCursor)
                e.accept()
        elif e.button() == Qt.RightButton:
            self.dragPos = e.globalPos() - self.frameGeometry().topLeft()
            self.right_click_timer.start(500)
            self.setCursor(Qt.ClosedHandCursor)
            e.accept()

    def mouseMoveEvent(self, e):
        self._cancel_auto_collapse()
        if (e.buttons() & Qt.LeftButton) and self.dragPos is not None:
            self.move(e.globalPos() - self.dragPos)
            e.accept()
        elif (e.buttons() & Qt.RightButton) and self.dragPos is not None and self.right_drag_enabled:
            self.move(e.globalPos() - self.dragPos)
            e.accept()

    def mouseReleaseEvent(self, e):
        if e.button() in (Qt.LeftButton, Qt.RightButton):
            self.dragPos = None
            self.right_click_timer.stop()
            self.right_drag_enabled = False
            self.setCursor(Qt.ArrowCursor)
            e.accept()
            if self.expanded and not self.is_pinned:
                self._schedule_auto_collapse(1600)

    def enterEvent(self, event):
        self._cancel_auto_collapse()
        super().enterEvent(event)

    def leaveEvent(self, event):
        if self.expanded and not self.is_pinned:
            self._schedule_auto_collapse(900)
        super().leaveEvent(event)


    def toggleMenu(self):
        self.expanded = not self.expanded
        self.is_pinned = self.expanded 

        radius = 110
        angle_start = 240
        angle_step = 60

        main_center = self.mainButton.pos() + QPoint(self.mainButton.width()//2,
                                                     self.mainButton.height()//2)

        if self.expanded:
            for i, btn in enumerate(self.options):
                btn.show()
                anim = QPropertyAnimation(btn, b"pos")
                anim.setDuration(320)
                anim.setEasingCurve(QEasingCurve.OutBack)

                angle_deg = angle_start - i * angle_step
                angle_rad = math.radians(angle_deg)
                dx = int(radius * math.cos(angle_rad))
                dy = int(radius * math.sin(angle_rad))
                target = main_center + QPoint(dx, dy) - QPoint(btn.width()//2, btn.height()//2)

                anim.setStartValue(self.mainButton.pos())
                anim.setEndValue(target)
                anim.start()
                btn._anim = anim

            TOOL_ARC_RADIUS = 110
            ANGLE_CENTER    = 0
            ANGLE_STEP      = 24

            for i, tb in enumerate(self.tool_buttons):
                tb.show()
                anim = QPropertyAnimation(tb, b"pos")
                anim.setDuration(260)
                anim.setEasingCurve(QEasingCurve.OutBack)

                anim.setStartValue(self.mainButton.pos())

                angle_deg = ANGLE_CENTER + (i - 1) * ANGLE_STEP
                angle_rad = math.radians(angle_deg)
                dx = int(TOOL_ARC_RADIUS * math.cos(angle_rad))
                dy = int(TOOL_ARC_RADIUS * math.sin(angle_rad))

                target = main_center + QPoint(dx, dy) - QPoint(tb.width()//2, tb.height()//2)
                anim.setEndValue(target)
                anim.start()
                tb._anim = anim


            self._cancel_auto_collapse()
            self._schedule_auto_collapse(1800)
        else:
            self._collapse_menu()



    def _handle_option(self, fid: str):
        if self.bridge:
            self.bridge.dispatch(fid)
        if self.access_widget and not self.access_widget.isVisible():
            self.access_widget.show()
        if self.expanded:
            self._collapse_menu()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), Qt.transparent)

    def _schedule_auto_collapse(self, ms: int = 1800):
        if self.is_pinned:
            return
        self.auto_collapse_timer.start(ms)

    def _cancel_auto_collapse(self):
        self.auto_collapse_timer.stop()

    def _collapse_menu_if_needed(self):
        if self.expanded and not self.is_pinned:
            self._collapse_menu()

    def _collapse_menu(self):

        for btn in self.options:
            anim = QPropertyAnimation(btn, b"pos")
            anim.setDuration(260)
            anim.setEasingCurve(QEasingCurve.InBack)
            anim.setStartValue(btn.pos())
            anim.setEndValue(self.mainButton.pos())
            anim.finished.connect(btn.hide)
            anim.start()
            btn._anim = anim

        for tb in self.tool_buttons:
            anim = QPropertyAnimation(tb, b"pos")
            anim.setDuration(220)
            anim.setEasingCurve(QEasingCurve.InBack)
            anim.setStartValue(tb.pos())
            anim.setEndValue(self.mainButton.pos())
            anim.finished.connect(tb.hide)
            anim.start()
            tb._anim = anim

        self.expanded = False
        self.is_pinned = False
        self._cancel_auto_collapse()



def main():
    app = QApplication(sys.argv)
    bridge = ControllerBridge(unified_controller)
    circle = MainButton(bridge=bridge, access_widget=None)
    circle.show()
    sys.exit(app.exec_())

if __name__ == "__main__":
    main()
