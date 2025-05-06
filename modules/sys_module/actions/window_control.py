import os
import time
import platform
from typing import Tuple, Optional, Dict, Any
from utils.debug_helper import info_log, error_log

# Windows: use pywin32
# Linux: use python-xlib or subprocess with xdotool
# macOS: use applescript via subprocess

def push_window(window_id: str) -> None:
    raise NotImplementedError("push_window 尚未實作")

    info_log(f"[window] push_window: 推動視窗 {window_id}")
    try:
        system = platform.system()
        if system == "Windows":
            # TODO: 用 win32gui/GetWindowRect + SetWindowPos 做位移
            pass
        elif system == "Linux":
            # TODO: subprocess.call(['xdotool', 'windowmove', window_id, '…', '…'])
            pass
        elif system == "Darwin":
            # TODO: subprocess.call(['osascript', '-e', '…'])
            pass
        else:
            raise NotImplementedError(f"不支援的平台：{system}")
    except Exception as e:
        error_log(f"[window] push_window 失敗: {e}")
        raise

def fold_window(window_id: str) -> None:
    raise NotImplementedError("fold_window 尚未實作")

    info_log(f"[window] fold_window: 折疊視窗 {window_id}")
    try:
        system = platform.system()
        if system == "Windows":
            # TODO: win32gui.ShowWindow(handle, win32con.SW_MINIMIZE)
            pass
        elif system == "Linux":
            # TODO: subprocess.call(['xdotool', 'windowminimize', window_id])
            pass
        elif system == "Darwin":
            # TODO: applescript 最小化
            pass
        else:
            raise NotImplementedError(f"不支援的平台：{system}")
    except Exception as e:
        error_log(f"[window] fold_window 失敗: {e}")
        raise

def switch_workspace(workspace_name: str) -> None:
    raise NotImplementedError("switch_workspace 尚未實作")

    info_log(f"[window] switch_workspace: 切換到工作區 {workspace_name}")
    try:
        system = platform.system()
        if system == "Windows":
            # TODO: Win11 API 呼叫或 Powershell 指令
            pass
        elif system == "Linux":
            # TODO: subprocess.call(['wmctrl', '-s', workspace_index])
            pass
        elif system == "Darwin":
            # TODO: 使用 AppleScript 切換 Spaces
            pass
        else:
            raise NotImplementedError(f"不支援的平台：{system}")
    except Exception as e:
        error_log(f"[window] switch_workspace 失敗: {e}")
        raise

def screenshot_and_annotate(region: Optional[Dict[str, int]] = None) -> str:
    raise NotImplementedError("screenshot_and_annotate 尚未實作")

    from PIL import ImageGrab, Image, ImageDraw

    info_log(f"[window] screenshot_and_annotate: 區域 {region or '全螢幕'}")
    try:
        # 1. 擷取螢幕
        if region:
            bbox = (region['x'], region['y'],
                    region['x'] + region['width'],
                    region['y'] + region['height'])
            img = ImageGrab.grab(bbox)
        else:
            img = ImageGrab.grab()

        # 2. 暫存路徑
        out_dir = os.path.join("temp", "screenshots")
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, f"screenshot_{int(time.time())}.png")
        img.save(path)

        # 3. TODO: 打開簡易標註介面 (e.g. tkinter Canvas)
        #    或者呼叫外部程式，如 MSPaint, Preview…
        # Example: os.system(f"mspaint {path}")

        info_log(f"[window] 截圖已存到 {path}")
        return path

    except Exception as e:
        error_log(f"[window] screenshot_and_annotate 失敗: {e}")
        raise
