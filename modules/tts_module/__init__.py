# modules/tts_module/__init__.py
"""
TTS模組 - 語音合成模組

功能:
- 文字轉語音
- 語音合成
- 語音調整
"""

from .tts_module import TTSModule
from configs.config_loader import load_module_config


def register():
    """註冊TTS模組"""
    try:
        config = load_module_config("tts_module")
        instance = TTSModule(config=config)
        instance.initialize()
        return instance
            
    except Exception as e:
        from utils.debug_helper import error_log
        error_log(f"[TTS] 模組註冊失敗：{e}")
        return None


# 匯出主要類別
__all__ = [
    "TTSModule",
    "register"
]
