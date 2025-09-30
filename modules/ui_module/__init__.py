# modules/ui_module/__init__.py
"""
UI模組 - 前端主要介面模組

功能:
- UEP桌寵主要UI實例管理
- 視窗狀態控制
- 用戶交互處理
- 拖放功能管理
- 透明度和顯示控制
"""

from .ui_module import UIModule
from configs.config_loader import load_module_config


def register():
    """註冊UI模組"""
    try:
        config = load_module_config("ui_module")
        instance = UIModule(config=config)
        instance.initialize()
        return instance
            
    except Exception as e:
        from utils.debug_helper import error_log
        error_log(f"[UI] 模組註冊失敗：{e}")
        return None


# 匯出主要類別
__all__ = ['UIModule']
