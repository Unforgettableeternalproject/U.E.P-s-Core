# access_widget.py
import sys, math, os
from functools import partial

from PyQt5.QtWidgets import (
    QApplication, QWidget, QPushButton, QVBoxLayout, QLabel,
    QFrame, QDialog, QHBoxLayout, QGraphicsDropShadowEffect
)
from PyQt5.QtCore import Qt, QPropertyAnimation, QPoint, QEasingCurve, QTimer, pyqtSignal, QSize, QSettings
from PyQt5.QtGui import QPainter, QColor, QBrush, QIcon, QPixmap, QFont, QRegion

try:
    from state_profile import StateProfileDialog as PersonalSettingsDialog
    print("[access_widget] Using StateProfileDialog as PersonalSettingsDialog")
except Exception as e:
    print("[access_widget] Failed to import StateProfileDialog, using placeholder:", e)

    class PersonalSettingsDialog(QDialog):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
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

from system_background import SystemBackgroundWindow
from state_profile import StateProfileDialog
from theme_manager import theme_manager, Theme

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
        elif fid == "system_background":
            return self.open_system_background()
        elif fid == "state_profile":
            return self.show_state_profile()
        else:
            info_log(f"[ControllerBridge] Unknown function id: {fid}")

    def open_user_settings(self):
        try:
            if self._settings_dialog is None or not self._settings_dialog.isVisible():
                # Try with controller first; fall back if signature does not match.
                try:
                    if self.controller is not None:
                        self._settings_dialog = PersonalSettingsDialog(self.controller)
                    else:
                        self._settings_dialog = PersonalSettingsDialog()
                except TypeError:
                    self._settings_dialog = PersonalSettingsDialog()

            self._settings_dialog.show()
            self._settings_dialog.raise_()
            self._settings_dialog.activateWindow()
            info_log("[ControllerBridge] User settings opened")
            return {"success": True}
        except Exception as e:
            error_log("[ControllerBridge] Failed to open user settings:", e)
            return {"success": False, "error": str(e)}

    def open_system_background(self):
        try:
            if self._bg_window is None or not self._bg_window.isVisible():
                self._bg_window = SystemBackgroundWindow(ui_module=self.controller)
                if hasattr(self._bg_window, "window_closed"):
                    # Reset reference when the window is closed
                    self._bg_window.window_closed.connect(
                        lambda: setattr(self, "_bg_window", None)
                    )
            self._bg_window.show()
            self._bg_window.raise_()
            self._bg_window.activateWindow()
            info_log("[ControllerBridge] SystemBackgroundWindow opened")
            return {"success": True}
        except Exception as e:
            error_log("[ControllerBridge] Failed to open SystemBackgroundWindow:", e)
            return {"success": False, "error": str(e)}

    def show_state_profile(self):
        try:
            if self._state_dialog is None or not self._state_dialog.isVisible():
                self._state_dialog = StateProfileDialog(controller=self.controller)

                # Optional: pre-fill some default texts and image
                try:
                    self._state_dialog.panel.set_diary_texts(
                        feels="Calm & focused. Latency low; mood +8%.",
                        helped="Fixed UI bugs, refactored theme system, and arranged your study plan."
                    )
                    self._state_dialog.panel.set_random_tips(
                        "Tip: Press Shift+Enter to insert a line. Stay hydrated and take breaks!"
                    )
                    guess = os.path.join(os.path.dirname(__file__), "arts", "U.E.P.png")
                    if os.path.exists(guess):
                        self._state_dialog.panel.set_uep_image(guess)
                except Exception as e:
                    error_log("[ControllerBridge] Failed to set default diary content:", e)

            self._state_dialog.show()
            self._state_dialog.raise_()
            self._state_dialog.activateWindow()
            info_log("[ControllerBridge] State profile opened")
            return {"success": True}
        except Exception as e:
            error_log("[ControllerBridge] Failed to open state profile:", e)
            return {"success": False, "error": str(e)}


class DraggableButton(QPushButton):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._drag_start = None
        self._dragging = False
        self._widget_offset = None

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            # Remember the global position and the current window position
            self._drag_start = e.globalPos()
            self._widget_offset = self.window().frameGeometry().topLeft()
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self._drag_start and (e.buttons() & Qt.LeftButton):
            # Only treat it as a drag if the movement passes a small threshold
            if (e.globalPos() - self._drag_start).manhattanLength() > 6:
                self._dragging = True
            if self._dragging:
                # Move the whole window together with the cursor
                self.window().move(e.globalPos() - (self._drag_start - self._widget_offset))
                e.accept()
                return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        if self._dragging:
            # Reset drag state
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
        """
        Load an image and fit it inside the button, keeping aspect ratio,
        with an optional margin.
        """
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

        # Make the button visually and logically circular
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

        # Frameless, always-on-top overlay style window
        self.setWindowFlags(
            Qt.FramelessWindowHint |
            Qt.WindowStaysOnTopHint |
            Qt.Tool |
            Qt.NoDropShadowWindowHint
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.resize(400, 400)

        # Colors for options (not heavily used now, but kept for styling)
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
        # Center the main button inside this widget
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
            b = self._make_opt_btn(60, label, col, partial(self._handle_option, fid))
            self._add_shadow(b)
            b.hide()
            self.options.append(b)

        # Extra tool buttons (small circle icons around the main button)
        self.tool_buttons = []
        self.TOOL_SIZE = 38

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

        # Drag + hover state
        self.dragPos = None
        self.right_click_timer = QTimer(self)
        self.right_click_timer.setSingleShot(True)
        self.right_click_timer.timeout.connect(self._enable_right_drag)
        self.right_drag_enabled = False

        # Slide-in/slide-out state
        self.is_pinned = False       # true if user has the menu expanded
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
        """
        Update button styles based on the global theme (light/dark).
        """
        is_dark = (theme_name == Theme.DARK.value)

        if is_dark:
            grad = "qlineargradient(x1:0,y1:1,x2:1,y2:0, stop:0 rgba(40,40,44,1), stop:1 rgba(64,64,72,1))"
            hover = "qlineargradient(x1:0,y1:1,x2:1,y2:0, stop:0 rgba(52,52,58,1), stop:1 rgba(80,80,88,1))"
            press = "qlineargradient(x1:0,y1:1,x2:1,y2:0, stop:0 rgba(34,34,38,1), stop:1 rgba(56,56,64,1))"
            fg = "#e6e6e6"
            sh_col = QColor(0, 0, 0, 90)
        else:
            grad = "qlineargradient(x1:0,y1:1,x2:1,y2:0, stop:0 rgba(201,150,20,255), stop:1 rgba(0,67,173,255))"
            hover = "qlineargradient(x1:0,y1:1,x2:1,y2:0, stop:0 rgba(201,150,20,191), stop:1 rgba(0,67,173,191))"
            press = "qlineargradient(x1:0,y1:1,x2:1,y2:0, stop:0 rgba(201,150,20,230), stop:1 rgba(0,67,173,230))"
            fg = "#ffffff"
            sh_col = QColor(0, 0, 0, 60)

        # Style main option buttons
        for b in self.options:
            sz = b.width()
            b.setStyleSheet(f"""
                QPushButton {{
                    background-color: {grad};
                    border-radius: {sz/2}px; color: {fg};
                    padding: 0px; border: none;
                }}
                QPushButton:hover {{ background-color: {hover}; }}
                QPushButton:pressed {{ background-color: {press}; }}
            """)
            eff = b.graphicsEffect()
            if isinstance(eff, QGraphicsDropShadowEffect):
                eff.setColor(sh_col)

        # Style smaller tool buttons
        for tb in self.tool_buttons:
            sz = tb.width()
            if is_dark:
                tgrad  = "qlineargradient(x1:0,y1:1,x2:1,y2:0, stop:0 rgba(90,90,98,1), stop:1 rgba(110,110,120,1))"
                thover = "qlineargradient(x1:0,y1:1,x2:1,y2:0, stop:0 rgba(104,104,112,1), stop:1 rgba(124,124,134,1))"
                tpress = "qlineargradient(x1:0,y1:1,x2:1,y2:0, stop:0 rgba(84,84,92,1), stop:1 rgba(100,100,110,1))"
                tfg    = "#e6e6e6"
            else:
                tgrad  = "qlineargradient(x1:0,y1:1,x2:1,y2:0, stop:0 rgba(255,174,183,1), stop:1 rgba(255,174,183,1))"
                thover = "qlineargradient(x1:0,y1:1,x2:1,y2:0, stop:0 rgba(255,174,183,0.75), stop:1 rgba(255,174,183,0.75))"
                tpress = "qlineargradient(x1:0,y1:1,x2:1,y2:0, stop:0 rgba(255,174,183,0.9), stop:1 rgba(255,174,183,0.9))"
                tfg    = "#ffffff"

            tb.setStyleSheet(f"""
                QPushButton {{
                    background-color: {tgrad};
                    border-radius: {sz/2}px; color: {tfg};
                    padding: 0px; border: none;
                }}
                QPushButton:hover {{ background-color: {thover}; }}
                QPushButton:pressed {{ background-color: {tpress}; }}
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
            # Fallback positions if we can't get screen geometry
            self.move(1200, 40)
            self.original_position = QPoint(1200, 40)
            self.visible_position = QPoint(1030, 40)

    def _check_hover_state(self):
        if self.is_pinned:
            return

        global_cursor_pos = QApplication.instance().desktop().cursor().pos()
        widget_rect = self.geometry()

        # Slightly enlarge the hover area so it feels more forgiving
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
                # If menu is open, schedule auto-collapse after sliding out
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
        b.setCursor(Qt.PointingHandCursor)

        font = QFont("Segoe UI Emoji")
        font.setPixelSize(int(size * 0.42))
        b.setFont(font)

        b.setStyleSheet(f"""
            QPushButton {{
                background-color: qlineargradient(
                    x1:0, y1:1, x2:1, y2:0,
                    stop:0 rgba(201, 150, 20, 255),
                    stop:1 rgba(0, 67, 173, 255)
                );
                border-radius: {size/2}px;
                color: #fff;
                padding: 0px;
                border: none;
            }}
            QPushButton:hover {{
                background-color: qlineargradient(
                    x1:0, y1:1, x2:1, y2:0,
                    stop:0 rgba(201, 150, 20, 191),
                    stop:1 rgba(0, 67, 173, 191)
                );
                border: none;
            }}
            QPushButton:pressed {{
                background-color: qlineargradient(
                    x1:0, y1:1, x2:1, y2:0,
                    stop:0 rgba(201, 150, 20, 230),
                    stop:1 rgba(0, 67, 173, 230)
                );
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
                background-color: qlineargradient(
                    x1:0, y1:1, x2:1, y2:0,
                    stop:0 rgba(255, 174, 183, 255),
                    stop:1 rgba(255, 174, 183, 255)
                );
                border-radius: {size/2}px;
                color: #fff;
                padding: 0px;
                border: none;
            }}
            QPushButton:hover {{
                background-color: qlineargradient(
                    x1:0, y1:1, x2:1, y2:0,
                    stop:0 rgba(255, 174, 183, 191),
                    stop:1 rgba(255, 174, 183, 191)
                );
                border: none;
            }}
            QPushButton:pressed {{
                background-color: qlineargradient(
                    x1:0, y1:1, x2:1, y2:0,
                    stop:0 rgba(255, 174, 183, 230),
                    stop:1 rgba(255, 174, 183, 230)
                );
                border: none;
            }}
        """)
        b.clicked.connect(callback)
        return b

    def mousePressEvent(self, e):
        self._cancel_auto_collapse()

        if e.button() == Qt.LeftButton:
            # Clicked outside all visible buttons -> collapse menu
            if self.expanded and not any(
                w.isVisible() and w.geometry().contains(e.pos())
                for w in [self.mainButton] + self.options + self.tool_buttons
            ):
                self._collapse_menu()
                e.accept()
                return

            # Otherwise, prepare for dragging if not clicking on the main/options
            if not any(btn.geometry().contains(e.pos()) for btn in [self.mainButton] + self.options):
                self.dragPos = e.globalPos() - self.frameGeometry().topLeft()
                self.setCursor(Qt.ClosedHandCursor)
                e.accept()

        elif e.button() == Qt.RightButton:
            # Right click: hold a bit, then allow dragging with right button
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
            # Animate option buttons outwards along an arc
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
                btn._anim = anim  # keep a ref to avoid GC

            # Place tool buttons in a smaller arc
            TOOL_ARC_RADIUS = 110
            ANGLE_CENTER = 0
            ANGLE_STEP = 35

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
