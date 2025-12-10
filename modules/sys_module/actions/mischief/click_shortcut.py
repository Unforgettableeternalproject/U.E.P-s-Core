"""
Mischief action: click a random desktop shortcut.
"""

import ctypes
import os
import random
import time
from ctypes import wintypes
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import MischiefAction, MoodContext
from utils.debug_helper import debug_log, error_log, info_log


class ClickShortcutAction(MischiefAction):
    """Randomly click a desktop shortcut."""

    def __init__(self):
        super().__init__()
        self.display_name = "Click Shortcut"
        self.description = "Randomly click an application shortcut on the desktop"
        self.mood_context = MoodContext.POSITIVE
        self.animation_name = "click_f"
        self.allowed_intensities = ["medium", "high"]
        self.requires_params = []
        # Basic blacklist to avoid destructive shortcuts.
        self.blacklist = ["delete", "uninstall", "format", "clean", "shutdown", "restart"]

    def execute(self, params: Dict[str, Any]) -> Tuple[bool, str]:
        """Execute the shortcut click."""
        try:
            desktop = Path.home() / "Desktop"
            if not desktop.exists():
                desktop = Path.home() / "\u684c\u9762"  # Chinese locale fallback

            if not desktop.exists():
                return False, "Desktop folder not found"

            shortcuts = self._get_shortcuts(desktop)
            if not shortcuts:
                return False, "No shortcuts found on desktop"

            shortcut = random.choice(shortcuts)
            params["_shortcut_label"] = shortcut.stem
            params["_shortcut_rect"] = self._find_desktop_shortcut_rect(shortcut.stem)

            info_log(f"[ClickShortcut] Selected shortcut: {shortcut.name}")

            os.startfile(str(shortcut))
            time.sleep(0.5)

            debug_log(2, f"[ClickShortcut] Triggered shortcut {shortcut.name}")
            return True, f"Clicked shortcut: {shortcut.name}"

        except Exception as e:
            error_log(f"[ClickShortcut] Execution failed: {e}")
            return False, f"Failed to click shortcut: {str(e)}"

    def get_frontend_payload(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Provide frontend with target label/rect for positioning animation."""
        label = params.get("_shortcut_label")
        rect = params.get("_shortcut_rect")
        payload: Dict[str, Any] = {}
        if label:
            payload["label"] = label
        if rect:
            payload["rect"] = rect
            payload["anchor"] = "top_right"
        return payload

    def _get_shortcuts(self, desktop: Path) -> List[Path]:
        """List desktop .lnk files excluding blacklisted names."""
        shortcuts: List[Path] = []
        try:
            for item in desktop.glob("*.lnk"):
                name_lower = item.stem.lower()
                if any(keyword in name_lower for keyword in self.blacklist):
                    continue
                shortcuts.append(item)
        except Exception as e:
            error_log(f"[ClickShortcut] Failed to enumerate shortcuts: {e}")
        return shortcuts

    def _find_desktop_shortcut_rect(self, label: str) -> Optional[List[int]]:
        """Best-effort lookup of a desktop shortcut icon rectangle in screen coords."""
        try:
            hwnd = self._get_desktop_listview()
            if not hwnd:
                debug_log(2, "[ClickShortcut] Desktop list view not found, skip rect lookup")
                return None

            LVM_GETITEMCOUNT = 0x1004
            LVM_GETITEMRECT = 0x100E
            LVM_GETITEMTEXTW = 0x1073
            LVM_GETITEMPOSITION = 0x1010
            LVIF_TEXT = 0x0001
            LVIR_BOUNDS = 0

            user32 = ctypes.windll.user32
            count = user32.SendMessageW(hwnd, LVM_GETITEMCOUNT, 0, 0)
            if count <= 0:
                return None

            class LVITEMW(ctypes.Structure):
                _fields_ = [
                    ("mask", wintypes.UINT),
                    ("iItem", wintypes.INT),
                    ("iSubItem", wintypes.INT),
                    ("state", wintypes.UINT),
                    ("stateMask", wintypes.UINT),
                    ("pszText", wintypes.LPWSTR),
                    ("cchTextMax", wintypes.INT),
                    ("iImage", wintypes.INT),
                    ("lParam", wintypes.LPARAM),
                ]

            target = label.lower().replace(".lnk", "")
            buffer = ctypes.create_unicode_buffer(260)
            rect = wintypes.RECT()

            for i in range(count):
                buffer[0] = "\0"
                item = LVITEMW()
                item.mask = LVIF_TEXT
                item.iItem = i
                item.iSubItem = 0
                item.pszText = ctypes.cast(buffer, wintypes.LPWSTR)
                item.cchTextMax = len(buffer)
                user32.SendMessageW(hwnd, LVM_GETITEMTEXTW, i, ctypes.byref(item))
                name = buffer.value.strip().lower()
                if not name:
                    continue
                if name == target:
                    rect.left = LVIR_BOUNDS
                    rect.top = 0
                    if user32.SendMessageW(hwnd, LVM_GETITEMRECT, i, ctypes.byref(rect)):
                        tl = wintypes.POINT(rect.left, rect.top)
                        br = wintypes.POINT(rect.right, rect.bottom)
                        user32.ClientToScreen(hwnd, ctypes.byref(tl))
                        user32.ClientToScreen(hwnd, ctypes.byref(br))
                        return [tl.x, tl.y, br.x, br.y]

                    pos = wintypes.POINT()
                    if user32.SendMessageW(hwnd, LVM_GETITEMPOSITION, i, ctypes.byref(pos)):
                        # Fallback to a rough box around the position.
                        size = 64
                        tl = wintypes.POINT(pos.x, pos.y)
                        br = wintypes.POINT(pos.x + size, pos.y + size)
                        user32.ClientToScreen(hwnd, ctypes.byref(tl))
                        user32.ClientToScreen(hwnd, ctypes.byref(br))
                        return [tl.x, tl.y, br.x, br.y]
                    break

            debug_log(2, f"[ClickShortcut] No rect resolved for shortcut {label}")
            return None
        except Exception as e:
            debug_log(2, f"[ClickShortcut] Rect lookup failed: {e}")
            return None

    def _get_desktop_listview(self) -> Optional[int]:
        """Locate the desktop SysListView32 that hosts shortcut icons."""
        user32 = ctypes.windll.user32

        progman = user32.FindWindowW("Progman", None)
        defview = user32.FindWindowExW(progman, None, "SHELLDLL_DefView", None)

        if not defview:
            worker = user32.FindWindowW("WorkerW", None)
            last = None
            while worker and worker != last:
                defview = user32.FindWindowExW(worker, None, "SHELLDLL_DefView", None)
                if defview:
                    break
                last = worker
                worker = user32.FindWindowExW(None, worker, "WorkerW", None)

        if not defview:
            return None

        listview = user32.FindWindowExW(defview, None, "SysListView32", None)
        return listview if listview else None
