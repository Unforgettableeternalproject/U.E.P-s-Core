"""
MISCHIEF 行為實現：點擊桌面捷徑

隨機選擇並點擊桌面上的捷徑。
"""

import os
from pathlib import Path
from typing import Dict, Any, Tuple, List
import random
import pyautogui
import time

from . import MischiefAction, MoodContext
from utils.debug_helper import debug_log, error_log, info_log, SYSTEM_LEVEL


class ClickShortcutAction(MischiefAction):
    """點擊桌面捷徑行為"""
    
    def __init__(self):
        super().__init__()
        self.display_name = "Click Shortcut"
        self.description = "Randomly click an application shortcut on the desktop"
        self.mood_context = MoodContext.POSITIVE
        self.animation_name = "click_f"
        self.allowed_intensities = ["medium", "high"]
        self.requires_params = []
        
        # 黑名單：不應該點擊的捷徑關鍵字
        self.blacklist = [
            "刪除", "delete", "卸載", "uninstall",
            "格式化", "format", "清理", "clean",
            "關機", "shutdown", "重啟", "restart"
        ]
    
    def execute(self, params: Dict[str, Any]) -> Tuple[bool, str]:
        """執行捷徑點擊"""
        
        try:
            # 獲取桌面路徑
            desktop = Path.home() / "Desktop"
            if not desktop.exists():
                desktop = Path.home() / "桌面"
            
            if not desktop.exists():
                return False, "無法找到桌面路徑"
            
            # 獲取所有捷徑
            shortcuts = self._get_shortcuts(desktop)
            
            if not shortcuts:
                return False, "桌面上沒有找到安全的捷徑"
            
            # 隨機選擇一個
            shortcut = random.choice(shortcuts)
            
            info_log(f"[ClickShortcut] 準備點擊: {shortcut.name}")
            
            # 使用 os.startfile 啟動
            os.startfile(str(shortcut))
            
            # 等待一下確保啟動
            time.sleep(0.5)
            
            debug_log(2, f"[ClickShortcut] 已點擊捷徑: {shortcut.name}")
            
            return True, f"成功點擊捷徑：{shortcut.name}"
            
        except Exception as e:
            error_log(f"[ClickShortcut] 執行失敗: {e}")
            return False, f"點擊捷徑時發生錯誤: {str(e)}"
    
    def _get_shortcuts(self, desktop: Path) -> List[Path]:
        """
        獲取桌面上安全的捷徑
        
        Returns:
            捷徑路徑列表
        """
        shortcuts = []
        
        try:
            # 只尋找 .lnk 檔案（Windows 捷徑）
            for item in desktop.glob("*.lnk"):
                # 檢查是否在黑名單
                name_lower = item.stem.lower()
                if any(keyword in name_lower for keyword in self.blacklist):
                    continue
                
                shortcuts.append(item)
        
        except Exception as e:
            error_log(f"[ClickShortcut] 掃描捷徑失敗: {e}")
        
        return shortcuts
