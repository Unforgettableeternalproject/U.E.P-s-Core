# access_widget.py
import sys, math, os
from functools import partial

from PyQt5.QtWidgets import (
    QApplication, QWidget, QPushButton, QVBoxLayout, QLabel,
    QFrame, QDialog, QHBoxLayout, QGraphicsDropShadowEffect, QMenu
)
from PyQt5.QtCore import Qt, QPropertyAnimation, QPoint, QEasingCurve, QTimer, pyqtSignal, QSize, QSettings, QRect
from PyQt5.QtGui import QPainter, QColor, QBrush, QIcon, QPixmap, QFont, QRegion, QCursor

from utils.debug_helper import debug_log, info_log, error_log, OPERATION_LEVEL

from .system_background import SystemBackgroundWindow
from .theme_manager import theme_manager, Theme

try:
    from .state_profile import StateProfileDialog
    info_log("[access_widget] Using StateProfileDialog")
except Exception as e:
    error_log(f"[access_widget] Failed to import StateProfileDialog, using placeholder: {e}")

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
    from .user_settings import UserMainWindow
    info_log("[access_widget] Using UserMainWindow from user_settings.py")
except Exception as e:
    error_log(f"[access_widget] Failed to import UserMainWindow, using placeholder: {e}")

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

# 使用正確的介面型別 Enum 以便查找主桌寵介面
try:
    from modules.ui_module.ui_module import UIInterfaceType
except Exception:
    UIInterfaceType = None


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
        elif fid == "tool_1":
            return self.toggle_desktop_pet()
        elif fid == "tool_2":
            info_log("[ControllerBridge] tool_2 (呼叫UEP) - 尚未實作")
            return {"success": False, "message": "功能開發中"}
        elif fid == "tool_3":
            info_log("[ControllerBridge] tool_3 (睡眠) - 尚未實作")
            return {"success": False, "message": "功能開發中"}
        else:
            info_log(f"[ControllerBridge] Unknown function id: {fid}")

    def open_user_settings(self):
        try:
            if self._user_settings_window is None:
                try:
                    self._user_settings_window = UserMainWindow(ui_module=self.controller)
                except TypeError:
                    self._user_settings_window = UserMainWindow()
                
                # 問題3修正：設定視窗關閉時不退出應用程式，只是隱藏
                self._user_settings_window.setAttribute(Qt.WA_QuitOnClose, False)
                
                # 不需要 window_closed 信號來清空引用，保留視窗實例以便重複開啟

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
            if self._bg_window is None:
                self._bg_window = SystemBackgroundWindow(ui_module=self.controller)
                # 問題3修正：設定視窗關閉時不退出應用程式
                self._bg_window.setAttribute(Qt.WA_QuitOnClose, False)
            
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

    def toggle_desktop_pet(self):
        """切換桌面寵物的顯示/隱藏狀態"""
        try:
            # self.controller 就是 ui_module 實例
            ui_module = self.controller
            
            if not ui_module:
                error_log("[ControllerBridge] UI 模組未初始化")
                return {"success": False, "error": "UI module not initialized"}
            
            # 正確使用 Enum key 取得主桌寵介面
            key = UIInterfaceType.MAIN_DESKTOP_PET if UIInterfaceType else 'main_desktop_pet'
            desktop_pet = ui_module.interfaces.get(key)
            
            if not desktop_pet:
                error_log("[ControllerBridge] 桌面寵物未初始化")
                return {"success": False, "error": "Desktop pet not initialized"}
            
            # 使用 handle_frontend_request 切換顯示狀態（會觸發動畫）
            if hasattr(desktop_pet, 'isVisible') and desktop_pet.isVisible():
                result = ui_module.handle_frontend_request({
                    "command": "hide_interface",
                    "interface": "main_desktop_pet"
                })
                if result and result.get('success'):
                    info_log("[ControllerBridge] 🙈 桌面寵物已隱藏")
                    return {"success": True, "state": "hidden"}
                else:
                    return {"success": False, "error": result.get('error', '隱藏失敗')}
            else:
                result = ui_module.handle_frontend_request({
                    "command": "show_interface",
                    "interface": "main_desktop_pet"
                })
                if result and result.get('success'):
                    info_log("[ControllerBridge] 👀 桌面寵物已顯示")
                    return {"success": True, "state": "visible"}
                else:
                    return {"success": False, "error": result.get('error', '顯示失敗')}
                
        except Exception as e:
            import traceback
            error_log(f"[ControllerBridge] 切換桌面寵物失敗: {e}")
            traceback.print_exc()
            return {"success": False, "error": str(e)}
    
    def show_state_profile(self):
        try:
            # 如果已有實例且可見，直接顯示
            if self._state_dialog is not None and not self._state_dialog.isVisible():
                self._state_dialog.show()
                self._state_dialog.raise_()
                self._state_dialog.activateWindow()
                return {"success": True}
            
            # 創建新實例
            if self._state_dialog is None:
                try:
                    dlg = StateProfileDialog(controller=self.controller)
                except TypeError:
                    try:
                        dlg = StateProfileDialog(self.controller)
                    except TypeError:
                        dlg = StateProfileDialog()

                self._state_dialog = dlg
                
                # 問題3修正：對話框關閉時不退出應用程式
                dlg.setAttribute(Qt.WA_QuitOnClose, False)
                # 不使用 WA_DeleteOnClose，保留實例以便重複開啟

                try:
                    dlg.panel.set_diary_texts(
                        feels="Calm & focused. Latency low; mood +8%.",
                        helped="Fixed UI bugs, refactored theme system, and arranged your study plan."
                    )
                    dlg.panel.set_random_tips(
                        "Tip: Press Shift+Enter to insert a line. Stay hydrated and take breaks!"
                    )
                    guess = os.path.join(os.path.dirname(__file__), "..", "..", "..", "arts", "U.E.P.png")
                    if os.path.exists(guess):
                        dlg.panel.set_uep_image(guess)
                except Exception as e:
                    error_log("[ControllerBridge] Failed to set default diary content:", e)

            self._state_dialog.show()
            self._state_dialog.raise_()
            self._state_dialog.activateWindow()

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
                # 拖曳時禁用 hover 樣式
                self.setAttribute(Qt.WA_UnderMouse, False)
                # 清除整個面板的 hover 樣式以避免白色邊框殘留
                try:
                    wnd = self.window()
                    if hasattr(wnd, 'mainButton'):
                        s = wnd.mainButton.style()
                        s.unpolish(wnd.mainButton)
                        s.polish(wnd.mainButton)
                        wnd.mainButton.update()
                    for b in getattr(wnd, 'options', []):
                        try:
                            sb = b.style()
                            sb.unpolish(b)
                            sb.polish(b)
                            b.update()
                        except Exception:
                            pass
                    for tb in getattr(wnd, 'tool_buttons', []):
                        try:
                            st = tb.style()
                            st.unpolish(tb)
                            st.polish(tb)
                            tb.update()
                        except Exception:
                            pass
                except Exception:
                    pass
                e.accept()
                return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        if self._dragging:
            self._drag_start = None
            self._dragging = False
            # 恢復 hover 檢測
            self.setAttribute(Qt.WA_UnderMouse, True)
            # 釋放後刷新按鈕樣式，確保邊框不殘留
            try:
                wnd = self.window()
                if hasattr(wnd, 'mainButton'):
                    s = wnd.mainButton.style()
                    s.unpolish(wnd.mainButton)
                    s.polish(wnd.mainButton)
                    wnd.mainButton.update()
                for b in getattr(wnd, 'options', []):
                    try:
                        sb = b.style()
                        sb.unpolish(b)
                        sb.polish(b)
                        b.update()
                    except Exception:
                        pass
                for tb in getattr(wnd, 'tool_buttons', []):
                    try:
                        st = tb.style()
                        st.unpolish(tb)
                        st.polish(tb)
                        tb.update()
                    except Exception:
                        pass
            except Exception:
                pass
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

        # 添加動態效果的樣式
        button.setStyleSheet(f"""
            QPushButton {{
                background-color: transparent;
                border: none;
                padding: 0px;
                border-radius: {d/2}px;
            }}
            QPushButton:hover {{
                background-color: rgba(255, 255, 255, 30);
                border: 2px solid rgba(255, 255, 255, 100);
            }}
            QPushButton:pressed {{
                background-color: rgba(255, 255, 255, 50);
                border: 2px solid rgba(255, 255, 255, 150);
            }}
        """)
        button.setMask(QRegion(0, 0, d, d, QRegion.Ellipse))
        
        # 為主按鈕添加縮放動畫
        button._scale_animation = QPropertyAnimation(button, b"geometry")
        button._scale_animation.setDuration(150)
        button._scale_animation.setEasingCurve(QEasingCurve.OutCubic)
        button._original_geometry = button.geometry()

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
        
        # 讀取使用者設定
        from configs.user_settings_manager import get_user_setting, user_settings_manager
        self.auto_hide_enabled = get_user_setting("interface.access_widget.auto_hide", True)
        self.hide_edge_threshold = get_user_setting("interface.access_widget.hide_edge_threshold", 200)
        
        # 註冊熱重載回調
        user_settings_manager.register_reload_callback("access_widget", self._reload_from_user_settings)

        self.color_opt1 = "#E3F2FD"
        self.color_opt2 = "#E8F5E9"
        self.color_opt3 = "#FFF3E0"

        # Main round button in the center
        self.mainButton = self._make_opt_btn(110, "", "transparent", self.toggleMenu)
        uep_icon_path = os.path.join(os.path.dirname(__file__), "..", "..", "..", "arts", "U.E.P.png")
        self.set_button_image_fit(
            self.mainButton,
            uep_icon_path,
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

        # Tool buttons (右側小按鈕)
        self.tool_buttons = []
        self.TOOL_SIZE = 41
        tools = [
            ("👁️", "tool_1"),  # 顯示/隱藏 UEP 桌面寵物
            ("🗣️", "tool_2"),   # 呼叫 UEP (待實作)
            ("😴", "tool_3"),   # 睡眠模式 (待實作)
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
        """計算初始位置，使用虛擬桌面邊界"""
        app = QApplication.instance()
        desktop = app.desktop()
        
        # 獲取虛擬桌面尺寸（多螢幕總範圍）
        virtual_rect = desktop.geometry()
        
        # 計算邊緣檢測區域（用於自動收合）
        self.edge_threshold = self.hide_edge_threshold  # 使用設定值
        self.virtual_rect = virtual_rect
        
        # 初始位置設在螢幕中央偏右上
        center_x = virtual_rect.center().x()
        center_y = virtual_rect.center().y()
        x = center_x + 100  # 偏右一點避免擋住中心
        y = center_y - 150  # 偏上一點
        
        # 初始化時不在邊緣，不啟用自動隱藏
        self.edge_side = None
        
        # 設置位置並記錄為完全可見位置
        self.move(x, y)
        self.original_position = QPoint(x, y)  # original_position 是完全可見的位置
        
        # 初始狀態為完全可見
        self.is_fully_visible = True

    def _check_hover_state(self):
        """檢查是否需要自動收合到邊緣"""
        if self.is_pinned or not hasattr(self, 'virtual_rect') or not self.auto_hide_enabled:
            return

        widget_center = self.geometry().center()
        
        # 檢查小工具是否在螢幕邊緣附近
        near_right_edge = (self.virtual_rect.right() - widget_center.x()) < self.edge_threshold
        near_left_edge = (widget_center.x() - self.virtual_rect.left()) < self.edge_threshold
        near_top_edge = (widget_center.y() - self.virtual_rect.top()) < self.edge_threshold
        near_bottom_edge = (self.virtual_rect.bottom() - widget_center.y()) < self.edge_threshold
        
        # 只有在邊緣附近才檢查游標懸停並啟用自動隱藏
        if near_right_edge or near_left_edge or near_top_edge or near_bottom_edge:
            # 動態更新邊緣方向
            distance_to_left = widget_center.x() - self.virtual_rect.left()
            distance_to_right = self.virtual_rect.right() - widget_center.x()
            
            # 根據距離決定是左邊還是右邊
            if distance_to_left < distance_to_right:
                self.edge_side = 'left'
            else:
                self.edge_side = 'right'
            
            # 檢查滑鼠是否在小工具附近
            global_cursor_pos = QCursor.pos()
            widget_rect = self.geometry()
            detection_margin = 50
            expanded_rect = widget_rect.adjusted(
                -detection_margin, -detection_margin,
                detection_margin, detection_margin
            )
            
            is_hovering = expanded_rect.contains(global_cursor_pos)
            
            # 根據滑鼠位置和當前狀態決定動作
            if is_hovering:
                # 滑鼠靠近：如果當前是隱藏狀態，則滑入顯示
                if not self.is_fully_visible:
                    self._slide_to_visible()
            else:
                # 滑鼠離開：如果當前是完全可見，則滑出隱藏
                if self.is_fully_visible:
                    self._slide_to_hidden()
                    if self.expanded:
                        self._schedule_auto_collapse(900)

    def _slide_to_visible(self):
        """滑入到完全可見位置（從隱藏位置恢復）"""
        if self.is_fully_visible:
            return
        
        # 計算完全可見的位置（從當前 Y 座標，但 X 在螢幕內）
        current_geom = self.geometry()
        current_y = current_geom.y()
        widget_width = self.width()
        
        if self.edge_side == 'left':
            # 從左側滑入：保持在邊緣範圍內（小工具中心點仍在 edge_threshold 內）
            # 讓小工具左邊緣剛好在螢幕左邊界，中心點距離邊界 = width/2
            visible_x = self.virtual_rect.left()
        else:
            # 從右側滑入：讓小工具右邊緣稍微往內一點，確保觸發條件
            visible_x = self.virtual_rect.right() - widget_width + 20
        
        target_pos = QPoint(visible_x, current_y)
        
        self.slide_animation.stop()
        self.slide_animation.setStartValue(self.pos())
        self.slide_animation.setEndValue(target_pos)
        self.slide_animation.start()
        self.is_fully_visible = True
        
        # 不更新 original_position，保持記憶原始拖曳位置
        debug_log(3, f"[AccessWidget] 滑入可見: edge={self.edge_side}, target=({visible_x}, {current_y})")

    def _slide_to_hidden(self):
        """滑出到隱藏位置（只露出一點邊緣）"""
        if not self.is_fully_visible:
            return
        
        # 使用當前位置計算隱藏位置
        current_geom = self.geometry()
        current_y = current_geom.y()
        widget_width = self.width()
        
        if self.edge_side == 'left':
            # 左側隱藏：讓一半在螢幕內，一半在外
            hidden_x = self.virtual_rect.left() - (widget_width // 2)
        else:
            # 右側隱藏：讓一半在螢幕內，一半在外
            hidden_x = self.virtual_rect.right() - (widget_width // 2)
        
        target_pos = QPoint(hidden_x, current_y)
        
        self.slide_animation.stop()
        self.slide_animation.setStartValue(self.pos())
        self.slide_animation.setEndValue(target_pos)
        self.slide_animation.start()
        self.is_fully_visible = False
        debug_log(3, f"[AccessWidget] 滑出隱藏: edge={self.edge_side}, target=({hidden_x}, {current_y})")

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

    def contextMenuEvent(self, event):
        """顯示右鍵選單"""
        # 檢查右鍵是否在主按鈕上
        if self.mainButton.geometry().contains(event.pos()):
            menu = QMenu(self)
            
            # 設定選單樣式
            menu.setStyleSheet("""
                QMenu {
                    background-color: rgba(45, 45, 45, 230);
                    color: #ffffff;
                    border: 1px solid rgba(255, 255, 255, 50);
                    border-radius: 6px;
                    padding: 4px;
                }
                QMenu::item {
                    padding: 6px 20px;
                    border-radius: 4px;
                }
                QMenu::item:selected {
                    background-color: rgba(70, 70, 70, 200);
                }
                QMenu::separator {
                    height: 1px;
                    background: rgba(255, 255, 255, 30);
                    margin: 4px 10px;
                }
            """)
            
            # 添加選單項目
            settings_action = menu.addAction("⚙️ 設定")
            background_action = menu.addAction("🖼️ 背景")
            profile_action = menu.addAction("📊  狀態")
            menu.addSeparator()
            exit_action = menu.addAction("🚪 離開應用程式")
            
            # 執行選單並取得使用者選擇
            action = menu.exec_(event.globalPos())
            
            if action == settings_action:
                self._handle_option("user_settings")
            elif action == background_action:
                self._handle_option("system_background")
            elif action == profile_action:
                self._handle_option("state_profile")
            elif action == exit_action:
                info_log("[MainButton] 使用者選擇離開應用程式")
                self._exit_application()
            
            event.accept()
        else:
            super().contextMenuEvent(event)

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
            # 檢查是否點擊在主按鈕上
            if self.mainButton.geometry().contains(e.pos()):
                # 主按鈕右鍵交由 contextMenuEvent 處理
                return
            
            # 其他區域的右鍵用於拖曳
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
            # 拖曳結束後，更新原始位置為當前位置
            if self.dragPos is not None:
                self.original_position = self.pos()
                # 檢查是否靠近邊緣
                if hasattr(self, 'virtual_rect'):
                    widget_center = self.geometry().center()
                    distance_to_left = widget_center.x() - self.virtual_rect.left()
                    distance_to_right = self.virtual_rect.right() - widget_center.x()
                    
                    # 檢查是否在邊緣範圍內
                    near_left = distance_to_left < self.edge_threshold
                    near_right = distance_to_right < self.edge_threshold
                    
                    if near_left or near_right:
                        # 在邊緣範圍內，設定邊緣方向
                        if distance_to_left < distance_to_right:
                            self.edge_side = 'left'
                        else:
                            self.edge_side = 'right'
                        # 設為完全可見狀態，等待滑鼠離開後才隱藏
                        self.is_fully_visible = True
                        debug_log(3, f"[AccessWidget] 拖曳到邊緣 ({self.edge_side})，啟用自動隱藏")
                    else:
                        # 不在邊緣範圍內，保持完全可見但不啟用自動隱藏
                        self.is_fully_visible = True
                        debug_log(3, f"[AccessWidget] 拖曳到中間區域，不啟用自動隱藏")
                
                # 強制刷新主按鈕樣式以清除懸停狀態
                self.mainButton.style().unpolish(self.mainButton)
                self.mainButton.style().polish(self.mainButton)
                self.mainButton.update()
            
            self.dragPos = None
            self.right_click_timer.stop()
            self.right_drag_enabled = False
            self.setCursor(Qt.ArrowCursor)
            e.accept()
            if self.expanded and not self.is_pinned:
                self._schedule_auto_collapse(1600)

    def _exit_application(self):
        """完全退出應用程式（包括 UEP 和系統）"""
        import sys
        try:
            info_log("[MainButton] 開始退出應用程式（優雅關閉）...")

            # 0. 先隱藏 U.E.P（播放離場動畫）
            try:
                # 使用 ui_module 的介面管理，確保播放離場動畫
                ui_module = getattr(self.bridge, 'controller', None)
                if ui_module and hasattr(ui_module, 'handle_frontend_request'):
                    result = ui_module.handle_frontend_request({
                        "command": "hide_interface",
                        "interface": "main_desktop_pet"
                    })
                    playing = bool(result.get('playing_leave_animation')) if isinstance(result, dict) else False
                    if playing:
                        info_log("[MainButton] 已啟動離場動畫，等待動畫完成後關閉...")
                        # 輕量輪詢：等待主桌寵不可見或最多 3 秒
                        def _wait_for_hide(max_wait_ms=5000, interval_ms=150):
                            waited = 0
                            def _check():
                                nonlocal waited
                                try:
                                    key = UIInterfaceType.MAIN_DESKTOP_PET if UIInterfaceType else 'main_desktop_pet'
                                    pet = ui_module.interfaces.get(key)
                                    is_hidden = (pet is None) or (hasattr(pet, 'isVisible') and not pet.isVisible())
                                    if is_hidden:
                                        info_log("[MainButton] 離場動畫完成，開始優雅關閉")
                                        self._perform_graceful_shutdown()
                                        return
                                except Exception:
                                    # 若檢查失敗，仍嘗試關閉
                                    self._perform_graceful_shutdown()
                                    return
                                waited += interval_ms
                                if waited >= max_wait_ms:
                                    info_log("[MainButton] 等待動畫逾時，繼續優雅關閉")
                                    self._perform_graceful_shutdown()
                                    return
                                QTimer.singleShot(interval_ms, _check)
                            QTimer.singleShot(interval_ms, _check)
                        _wait_for_hide()
                        return
                else:
                    error_log("[MainButton] ui_module 不可用，跳過離場動畫")
            except Exception as e:
                error_log(f"[MainButton] 隱藏 U.E.P 失敗: {e}")

            # 若無法播放離場動畫，直接進入優雅關閉
            self._perform_graceful_shutdown()
        except Exception as e:
            error_log(f"[MainButton] 退出應用程式時發生錯誤: {e}")
            import traceback, sys
            traceback.print_exc()
            sys.exit(0)

    def _perform_graceful_shutdown(self):
        """優雅關閉所有模組與執行緒，並退出應用程式"""
        try:
            info_log("[MainButton] 執行系統優雅關閉...")

            # 1. 停止 STT 持續監聽
            try:
                from core.registry import get_loaded, is_loaded
                stt_module = get_loaded("stt_module") if is_loaded("stt_module") else None
                if stt_module and hasattr(stt_module, 'stop_listening'):
                    info_log("[MainButton] 停止 STT 持續監聽...")
                    stt_module.stop_listening()
            except Exception as e:
                error_log(f"[MainButton] 停止 STT 失敗: {e}")

            # 2. 使用 unified_controller 進行優雅關閉
            try:
                if unified_controller:
                    info_log("[MainButton] 呼叫 unified_controller.shutdown()...")
                    unified_controller.shutdown()
                else:
                    error_log("[MainButton] unified_controller 不可用")
            except Exception as e:
                error_log(f"[MainButton] unified_controller 關閉失敗: {e}")

            # 3. 停止 Qt 系統循環線程
            try:
                from core.production_runner import production_runner
                if hasattr(production_runner, 'qt_loop_manager') and production_runner.qt_loop_manager:
                    info_log("[MainButton] 停止 Qt 系統循環線程...")
                    production_runner.qt_loop_manager.stop_system_loop()
            except Exception as e:
                error_log(f"[MainButton] 停止 Qt 系統循環失敗: {e}")

            # 4. 關閉所有視窗並退出
            app = QApplication.instance()
            if app:
                try:
                    info_log("[MainButton] 關閉所有視窗...")
                    app.closeAllWindows()
                except Exception:
                    pass
                info_log("[MainButton] 退出 Qt 事件迴圈...")
                app.quit()

            info_log("[MainButton] 退出序列完成")
            sys.exit(0)
        except Exception as e:
            error_log(f"[MainButton] 優雅關閉過程出錯: {e}")
            import traceback
            traceback.print_exc()
            import sys
            sys.exit(0)
    
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
        """處理選項按鈕點擊"""
        if self.bridge:
            result = self.bridge.dispatch(fid)
            info_log(f"[MainButton] 功能 '{fid}' 執行結果: {result}")
        else:
            error_log(f"[MainButton] Bridge 未初始化，無法執行功能 '{fid}'")
        
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
    
    def _reload_from_user_settings(self, key_path: str, value):
        """處理 user_settings 熱重載"""
        try:
            if key_path == "interface.access_widget.auto_hide":
                old_value = self.auto_hide_enabled
                self.auto_hide_enabled = bool(value)
                info_log(f"[AccessWidget] 自動隱藏: {old_value} → {self.auto_hide_enabled}")
                # 如果關閉自動隱藏且當前已隱藏，則滑出
                if not self.auto_hide_enabled and not self.is_fully_visible:
                    self._slide_to_visible()
            elif key_path == "interface.access_widget.hide_edge_threshold":
                old_threshold = self.hide_edge_threshold
                self.hide_edge_threshold = int(value)
                self.edge_threshold = self.hide_edge_threshold
                info_log(f"[AccessWidget] 邊緣隱藏距離: {old_threshold}px → {self.hide_edge_threshold}px")
        except Exception as e:
            error_log(f"[AccessWidget] 熱重載設定失敗: {e}")


# 為向後兼容提供包裝函數
def UserAccessWidget(ui_module):
    """
    包裝函數，用於從 ui_module 創建 MainButton
    這是為了向後兼容 ui_module.py 中的初始化方式
    """
    bridge = ControllerBridge(ui_module)
    return MainButton(bridge=bridge)


def main():
    app = QApplication(sys.argv)
    theme_manager.apply_app()
    bridge = ControllerBridge(unified_controller)

    circle = MainButton(bridge=bridge)
    circle.show()

    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
