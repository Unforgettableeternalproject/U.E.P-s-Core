# modules/stt_module/__init__.py
"""
STT模組 - 語音識別模組

功能:
- 語音轉文字
- 語者識別
- 語音活動檢測
- 實時轉錄
"""

from .stt_module import STTModule
from configs.config_loader import load_module_config


def register():
    """註冊STT模組"""
    try:
        config = load_module_config("stt_module")
        instance = STTModule(config=config)
        # 注意：不在這裡初始化模組，讓 UnifiedController 負責統一初始化
        return instance
            
    except Exception as e:
        from utils.debug_helper import error_log
        error_log(f"[STT] 模組註冊失敗：{e}")
        return None


# 匯出主要類別
__all__ = [
    "STTModule",
    "register"
]
