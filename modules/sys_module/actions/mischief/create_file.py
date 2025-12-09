"""
MISCHIEF 行為實現：創建文字檔

在桌面上創建包含 UEP 訊息的文字檔。
"""

import os
from pathlib import Path
from typing import Dict, Any, Tuple
from datetime import datetime

from . import MischiefAction, MoodContext
from utils.debug_helper import debug_log, error_log, SYSTEM_LEVEL


class CreateTextFileAction(MischiefAction):
    """創建文字檔行為"""
    
    def __init__(self):
        super().__init__()
        self.display_name = "Create Text File"
        self.description = "Create a text file on the desktop with a message"
        self.mood_context = MoodContext.ANY
        self.animation_name = "data_processing_f"
        self.requires_params = ["message"]
    
    def execute(self, params: Dict[str, Any]) -> Tuple[bool, str]:
        """執行文字檔創建"""
        
        # 驗證參數
        valid, error = self.validate_params(params)
        if not valid:
            return False, error
        
        try:
            message = params.get("message", "")
            
            # 獲取桌面路徑
            desktop = Path.home() / "Desktop"
            if not desktop.exists():
                # Windows 中文系統可能是「桌面」
                desktop = Path.home() / "桌面"
            
            if not desktop.exists():
                return False, "無法找到桌面路徑"
            
            # 生成檔名
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"UEP_留言_{timestamp}.txt"
            filepath = desktop / filename
            
            # 寫入檔案
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(f"來自 U.E.P 的留言\n")
                f.write(f"時間: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write(f"{'-' * 40}\n\n")
                f.write(message)
                f.write(f"\n\n{'-' * 40}\n")
                f.write("（這是系統自主活動的結果）\n")
            
            debug_log(2, f"[CreateTextFile] 已創建文字檔: {filepath}")
            
            return True, f"成功創建文字檔：{filename}"
            
        except Exception as e:
            error_log(f"[CreateTextFile] 執行失敗: {e}")
            return False, f"創建文字檔時發生錯誤: {str(e)}"
