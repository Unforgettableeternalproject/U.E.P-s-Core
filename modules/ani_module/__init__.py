# modules/ani_module/__init__.py
"""
ANI模組 - 動畫控制模組

功能:
- 動畫狀態管理
- 幀序列控制
- 動畫切換邏輯
- 表情和動作動畫
- 動畫資源載入
"""

from .ani_module import ANIModule
from configs.config_loader import load_module_config


def register():
    """註冊ANI模組"""
    try:
        config = load_module_config("ani_module")
        instance = ANIModule(config=config)
        instance.initialize()
        return instance
            
    except Exception as e:
        from utils.debug_helper import error_log
        error_log(f"[ANI] 模組註冊失敗：{e}")
        return None


# 匯出主要類別
__all__ = ['ANIModule']
