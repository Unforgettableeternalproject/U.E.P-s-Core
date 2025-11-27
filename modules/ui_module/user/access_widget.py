# access_widget.py
import sys, math, os
from functools import partial

from PyQt5.QtWidgets import (
    QApplication, QWidget, QPushButton, QVBoxLayout, QLabel,
    QFrame, QDialog, QHBoxLayout, QGraphicsDropShadowEffect
)
from PyQt5.QtCore import Qt, QPropertyAnimation, QPoint, QEasingCurve, QTimer, pyqtSignal, QSize, QSettings, QRect
from PyQt5.QtGui import QPainter, QColor, QBrush, QIcon, QPixmap, QFont, QRegion

from system_background import SystemBackgroundWindow
from theme_manager import theme_manager, Theme

try:
    from state_profile import StateProfileDialog
    print("[access_widget] Using StateProfileDialog")
except Exception as e:
    print("[access_widget] Failed to import StateProfileDialog, using placeholder:", e)

    class StateProfileDialog(QDialog):
        def __init__(self, controller=None, parent=None):
            super().__init__(parent)
            self.setWindowTitle("User Settings (placeholder)")
            self.setMinimumSize(600, 420)

            lay = QVBoxLayout(self)
            lay.addWidget(QLabel(
                "Could not load StateProfileDialog.\n"
                "Please check state_profile.py import."
            ))
            btn = QPushButton("Close")
            btn.clicked.connect(self.close)
            lay.addWidget(btn)

try:
    from user_settings import UserMainWindow
    print("[access_widget] Using UserMainWindow from user_settings.py")
except Exception as e:
    print("[access_widget] Failed to import UserMainWindow, using placeholder:", e)

    class UserMainWindow(QDialog):
        """Very simple placeholder if user_settings.py is missing or broken."""
        def __init__(self, ui_module=None, parent=None):
            super().__init__(parent)
            self.setWindowTitle("User Settings (placeholder)")
            self.setMinimumSize(600, 420)
            lay = QVBoxLayout(self)
            lay.addWidget(QLabel(
                "Could not load UserMainWindow.\n"
                "Please check user_settings.py import."
            ))
            btn = QPushButton("Close")
            btn.clicked.connect(self.close)
            lay.addWidget(btn)

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
        #user settings window
        self._user_settings_window = None
        #system background window
        self._bg_window = None
        #state profile dialog
        self._state_dialog = None

    def dispatch(self, fid: str):
        info_log(f"[ControllerBridge] dispatch:{fid}")
        if fid == "user_settings":
            return self.open_user_settings()
        elif fid == "system_background":
            return self.open_system_background()
        elif fid == "state_profile":
            return self.show_state_profile()
        else:
            info_log(f"[ControllerBridge] Unknown function id: {fid}")

    def open_user_settings(self):
        try:
            if self._user_settings_window is None:
                try:
                    self._user_settings_window = UserMainWindow(ui_module=self.controller)
                except TypeError:
                    self._user_settings_window = UserMainWindow()

                if hasattr(self._user_settings_window, "window_closed"):
                    self._user_settings_window.window_closed.connect(
                        lambda: setattr(self, "_user_settings_window", None)
                    )

            wnd = self._user_settings_window
            if hasattr(wnd, "is_minimized_to_orb") and getattr(wnd, "is_minimized_to_orb", False):
                if hasattr(wnd, "restore_from_orb"):
                    wnd.restore_from_orb()
                else:
                    wnd.show()
            else:
                wnd.show()
                wnd.raise_()
                wnd.activateWindow()

            info_log("[ControllerBridge] User settings (UserMainWindow) opened")
            return {"success": True}

        except Exception as e:
            import traceback
            error_log("[ControllerBridge] Failed to open user settings:", e)
            traceback.print_exc()
            return {"success": False, "error": str(e)}


    def open_system_background(self):
        try:
            if self._bg_window is None or not self._bg_window.isVisible():
                self._bg_window = SystemBackgroundWindow(ui_module=self.controller)
                if hasattr(self._bg_window, "window_closed"):

                    self._bg_window.window_closed.connect(
                        lambda: setattr(self, "_bg_window", None)
                    )
            self._bg_window.show()
            self._bg_window.raise_()
            self._bg_window.activateWindow()
            info_log("[ControllerBridge] SystemBackgroundWindow opened")
            return {"success": True}
        except Exception as e:
            import traceback
            error_log("[ControllerBridge] Failed to open SystemBackgroundWindow:", e)
            traceback.print_exc()
            return {"success": False, "error": str(e)}

    def show_state_profile(self):
        try:
            dlg = None
            try:
                dlg = StateProfileDialog(controller=self.controller)
            except TypeError:
                try:
                    dlg = StateProfileDialog(self.controller)
                except TypeError:
                    dlg = StateProfileDialog()

            self._state_dialog = dlg

            try:
                dlg.panel.set_diary_texts(
                    feels="Calm & focused. Latency low; mood +8%.",
                    helped="Fixed UI bugs, refactored theme system, and arranged your study plan."
                )
                dlg.panel.set_random_tips(
                    "Tip: Press Shift+Enter to insert a line. Stay hydrated and take breaks!"
                )
                guess = os.path.join(os.path.dirname(__file__), "arts", "U.E.P.png")
                if os.path.exists(guess):
                    dlg.panel.set_uep_image(guess)
            except Exception as e:
                error_log("[ControllerBridge] Failed to set default diary content:", e)

            dlg.setAttribute(Qt.WA_DeleteOnClose, True)
            dlg.show()
            dlg.raise_()
            dlg.activateWindow()

            info_log("[ControllerBridge] State profile opened")
            return {"success": True}

        except Exception as e:
            import traceback
            error_log("[ControllerBridge] Failed to open state profile:", e)
            traceback.print_exc()
            return {"success": False, "error": str(e)}



class DraggableButton(QPushButton):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._drag_start = None
        self._dragging = False
        self._widget_offset = None

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


class MainButton(QWidget):

    @staticmethod
    def set_button_image_fit(button: QPushButton, img_path: str, margin: int = 0):
        d = min(button.width(), button.height())
        inner = max(0, d - 2 * margin)

        pix = QPixmap(img_path)
        if pix.isNull():
            error_log(f"[MainButton] Image not found: {img_path}")
            return

        scaled = pix.scaled(inner, inner, Qt.KeepAspectRatio, Qt.SmoothTransformation)

        button.setIcon(QIcon(scaled))
        button.setIconSize(QSize(inner, inner))
        button.setText("")
        button.setFlat(True)
        button.setFocusPolicy(Qt.NoFocus)

        button.setStyleSheet(f"""
            QPushButton, QPushButton:hover, QPushButton:pressed{{
                background-color: transparent;
                border: none;
                padding: 0px;
                border-radius: {d/2}px;
            }}
        """)
        button.setMask(QRegion(0, 0, d, d, QRegion.Ellipse))

    def __init__(self, bridge: ControllerBridge = None):
        super().__init__()
        self.bridge = bridge

        self.setWindowFlags(
            Qt.FramelessWindowHint |
            Qt.WindowStaysOnTopHint |
            Qt.Tool |
            Qt.NoDropShadowWindowHint
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.resize(400, 400)

        self.color_opt1 = "#E3F2FD"
        self.color_opt2 = "#E8F5E9"
        self.color_opt3 = "#FFF3E0"

        # Main round button in the center
        self.mainButton = self._make_opt_btn(110, "", "transparent", self.toggleMenu)
        self.set_button_image_fit(
            self.mainButton,
            r"C:\Users\Elisa Kao\Source\Repos\U.E.P-s-Core\arts\U.E.P.png",
            margin=6
        )
        self.mainButton.move(
            self.width() // 2 - self.mainButton.width() // 2,
            self.height() // 2 - self.mainButton.height() // 2
        )

        # Option buttons (radial menu entries)
        self.options = []
        opts = [
            ("⚙️", "user_settings", self.color_opt1),
            ("🖼️", "system_background", self.color_opt2),
            ("📊", "state_profile", self.color_opt3),
        ]
        for label, fid, col in opts:
            b = self._make_opt_btn(75, label, col, partial(self._handle_option, fid))
            self._add_shadow(b)
            b.hide()
            self.options.append(b)

        # Tool buttons
        self.tool_buttons = []
        self.TOOL_SIZE = 41
        tools = [
            ("🗣", "tool_1"),
            ("👂🏼", "tool_2"),
            ("😴", "tool_3"),
        ]
        for label, tid in tools:
            tb = self._make_tool_btn(self.TOOL_SIZE, label, partial(self._handle_option, tid))
            tb.hide()
            self.tool_buttons.append(tb)

        # Drag + hover state
        self.dragPos = None
        self.right_click_timer = QTimer(self)
        self.right_click_timer.setSingleShot(True)
        self.right_click_timer.timeout.connect(self._enable_right_drag)
        self.right_drag_enabled = False

        # Slide-in/slide-out state
        self.is_pinned = False
        self.is_fully_visible = False
        self.original_position = None
        self.visible_position = None

        # Animation for sliding in/out from the right edge
        self.slide_animation = QPropertyAnimation(self, b"pos")
        self.slide_animation.setDuration(300)
        self.slide_animation.setEasingCurve(QEasingCurve.OutCubic)

        # Periodically check if cursor is hovering near the widget
        self.hover_check_timer = QTimer(self)
        self.hover_check_timer.timeout.connect(self._check_hover_state)
        self.hover_check_timer.start(100)

        # Menu expansion state
        self.expanded = False
        self._place_circle()

        # Auto collapse timer for the radial menu
        self.auto_collapse_timer = QTimer(self)
        self.auto_collapse_timer.setSingleShot(True)
        self.auto_collapse_timer.timeout.connect(self._collapse_menu_if_needed)

        # React to theme changes from the central theme manager
        theme_manager.theme_changed.connect(self.apply_theme)
        self.apply_theme(theme_manager.theme.value)

    def apply_theme(self, theme_name: str):
        is_dark = (theme_name == Theme.DARK.value)

        # Option buttons
        for b in self.options:
            sz = getattr(b, "_circle_size", 65)

            try:
                b.setMask(QRegion(0, 0, sz, sz, QRegion.Ellipse))
            except Exception:
                pass

            if is_dark:
                grad  = ("qlineargradient(x1:0,y1:1,x2:1,y2:0, "
                         "stop:0 rgba(60,60,60,180), stop:1 rgba(90,90,90,180))")
                hover = ("qlineargradient(x1:0,y1:1,x2:1,y2:0, "
                         "stop:0 rgba(75,75,75,200), stop:1 rgba(105,105,105,200))")
                press = ("qlineargradient(x1:0,y1:1,x2:1,y2:0, "
                         "stop:0 rgba(50,50,50,220), stop:1 rgba(80,80,80,220))")
                fg = "#e6e6e6"
                shadow = QColor(0, 0, 0, 100)
            else:
                grad  = ("qlineargradient(x1:0,y1:1,x2:1,y2:0, "
                         "stop:0 rgba(140,140,140,150), stop:1 rgba(110,110,110,150))")
                hover = ("qlineargradient(x1:0,y1:1,x2:1,y2:0, "
                         "stop:0 rgba(155,155,155,170), stop:1 rgba(125,125,125,170))")
                press = ("qlineargradient(x1:0,y1:1,x2:1,y2:0, "
                         "stop:0 rgba(120,120,120,190), stop:1 rgba(100,100,100,190))")
                fg = "#ffffff"
                shadow = QColor(0, 0, 0, 70)

            b.setStyleSheet(f"""
                QPushButton {{
                    background-color: {grad};
                    border-radius: {sz/2}px;
                    color: {fg};
                    padding: 0px;
                    border: none;
                }}
                QPushButton:hover {{
                    background-color: {hover};
                }}
                QPushButton:pressed {{
                    background-color: {press};
                }}
            """)

            eff = b.graphicsEffect()
            if isinstance(eff, QGraphicsDropShadowEffect):
                eff.setColor(shadow)

        # Tool buttons
        for tb in self.tool_buttons:
            sz = tb.width()

            if is_dark:
                tgrad  = ("qlineargradient(x1:0,y1:1,x2:1,y2:0, "
                          "stop:0 rgba(50,50,50,220), stop:1 rgba(36,36,36,200))")
                thover = ("qlineargradient(x1:0,y1:1,x2:1,y2:0, "
                          "stop:0 rgba(64,64,64,230), stop:1 rgba(44,44,44,210))")
                tpress = ("qlineargradient(x1:0,y1:1,x2:1,y2:0, "
                          "stop:0 rgba(40,40,40,240), stop:1 rgba(24,24,24,220))")
                tfg    = "#e6e6e6"
            else:
                tgrad  = ("qlineargradient(x1:0,y1:1,x2:1,y2:0, "
                          "stop:0 rgba(80,80,80,180), stop:1 rgba(56,56,56,160))")
                thover = ("qlineargradient(x1:0,y1:1,x2:1,y2:0, "
                          "stop:0 rgba(96,96,96,200), stop:1 rgba(68,68,68,180))")
                tpress = ("qlineargradient(x1:0,y1:1,x2:1,y2:0, "
                          "stop:0 rgba(70,70,70,220), stop:1 rgba(40,40,40,200))")
                tfg    = "#f0f0f0"

            tb.setStyleSheet(f"""
                QPushButton {{
                    background-color: {tgrad};
                    border-radius: {sz/2}px;
                    color: {tfg};
                    padding: 0px;
                    border: none;
                }}
                QPushButton:hover {{
                    background-color: {thover};
                }}
                QPushButton:pressed {{
                    background-color: {tpress};
                }}
            """)

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
        expanded_rect = widget_rect.adjusted(
            -detection_margin, -detection_margin,
            detection_margin, detection_margin
        )

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
        b._circle_size = size
        b.setCursor(Qt.PointingHandCursor)

        b.setObjectName("uepOptButton")

        if text:
            pix = QPixmap(size, size)
            pix.fill(Qt.transparent)
            painter = QPainter(pix)
            painter.setRenderHint(QPainter.Antialiasing)
            emoji_font = QFont("Segoe UI Emoji")
            emoji_font.setPixelSize(int(size * 0.4))
            painter.setFont(emoji_font)
            painter.setPen(QColor(255, 255, 255))
            painter.drawText(QRect(0, 0, size, size), Qt.AlignCenter, text)
            painter.end()
            b.setIcon(QIcon(pix))
            inset = max(2, int(size * 0.10))
            b.setIconSize(QSize(size - inset * 2, size - inset * 2))
            b.setText("")

        b.setStyleSheet(f"""
            QPushButton#uepOptButton {{
                background-color: qlineargradient(
                    x1:0, y1:1, x2:1, y2:0,
                    stop:0 rgba(201,150,20,255),
                    stop:1 rgba(0,67,173,255)
                );
                border-radius: {size/2}px;
                color: #fff;
                padding:0px;
                border: none;
                outline: none;
            }}
            QPushButton#uepOptButton:hover {{
                background-color: qlineargradient(
                    x1:0, y1:1, x2:1, y2:0,
                    stop:0 rgba(201,150,20,191),
                    stop:1 rgba(0,67,173,191)
                );
                border: none;
                outline: none;
            }}
            QPushButton#uepOptButton:pressed {{
                background-color: qlineargradient(
                    x1:0, y1:1, x2:1, y2:0,
                    stop:0 rgba(201,150,20,230),
                    stop:1 rgba(0,67,173,230)
                );
                border: none;
                outline: none;
            }}
        """)

        try:
            b.setMask(QRegion(0, 0, size, size, QRegion.Ellipse))
        except Exception:
            pass

        b.clicked.connect(callback)
        return b

    def _make_tool_btn(self, size, text, callback):
        b = DraggableButton(text, self)
        b.setFixedSize(size, size)
        b._circle_size = size
        b.setCursor(Qt.PointingHandCursor)

        b.setObjectName("uepToolButton")

        if text:
            pix = QPixmap(size, size)
            pix.fill(Qt.transparent)
            painter = QPainter(pix)
            painter.setRenderHint(QPainter.Antialiasing)

            emoji_font = QFont("Segoe UI Emoji")
            emoji_font.setPixelSize(int(size * 0.5))
            painter.setFont(emoji_font)
            painter.setPen(QColor(255, 255, 255))

            painter.drawText(QRect(0, 0, size, size), Qt.AlignCenter, text)
            painter.end()

            b.setIcon(QIcon(pix))
            inset = max(2, int(size * 0.10))
            b.setIconSize(QSize(size - inset * 2, size - inset * 2))
            b.setText("")

        b.setStyleSheet(f"""
            QPushButton#uepToolButton {{
                background-color: qlineargradient(
                    x1:0, y1:1, x2:1, y2:0,
                    stop:0 rgba(40,40,40,200),
                    stop:1 rgba(28,28,28,180)
                );
                border-radius: {size/2}px;
                color: #fff;
                padding: 0px;
                border: none;
                outline: none;
            }}
            QPushButton#uepToolButton:hover {{
                background-color: qlineargradient(
                    x1:0, y1:1, x2:1, y2:0,
                    stop:0 rgba(52,52,52,210),
                    stop:1 rgba(34,34,34,190)
                );
                border: none;
                outline: none;
            }}
            QPushButton#uepToolButton:pressed {{
                background-color: qlineargradient(
                    x1:0, y1:1, x2:1, y2:0,
                    stop:0 rgba(36,36,36,220),
                    stop:1 rgba(20,20,20,200)
                );
                border: none;
                outline: none;
            }}
        """)

        try:
            b.setMask(QRegion(6, 6, size, size, QRegion.Ellipse))
        except Exception:
            pass

        b.clicked.connect(callback)
        return b

    def mousePressEvent(self, e):
        self._cancel_auto_collapse()

        if e.button() == Qt.LeftButton:
            if self.expanded and not any(
                w.isVisible() and w.geometry().contains(e.pos())
                for w in [self.mainButton] + self.options + self.tool_buttons
            ):
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

        main_center = self.mainButton.pos() + QPoint(
            self.mainButton.width() // 2,
            self.mainButton.height() // 2
        )

        if self.expanded:
            # Option buttons
            for i, btn in enumerate(self.options):
                btn.show()
                anim = QPropertyAnimation(btn, b"pos")
                anim.setDuration(320)
                anim.setEasingCurve(QEasingCurve.OutBack)

                angle_deg = angle_start - i * angle_step
                angle_rad = math.radians(angle_deg)
                dx = int(radius * math.cos(angle_rad))
                dy = int(radius * math.sin(angle_rad))
                target = main_center + QPoint(dx, dy) - QPoint(
                    btn.width() // 2,
                    btn.height() // 2
                )

                anim.setStartValue(self.mainButton.pos())
                anim.setEndValue(target)
                anim.start()
                btn._anim = anim

            # Tool buttons
            TOOL_ARC_RADIUS = 85
            ANGLE_CENTER = 9
            ANGLE_STEP = 40

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

                target = main_center + QPoint(dx, dy) - QPoint(
                    tb.width() // 2,
                    tb.height() // 2
                )
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
    theme_manager.apply_app()
    bridge = ControllerBridge(unified_controller)

    circle = MainButton(bridge=bridge)
    circle.show()

    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
