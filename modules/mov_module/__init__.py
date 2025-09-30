# modules/mov_module/__init__.py
"""
MOV模組 - 行為移動控制模組

功能:
- 桌寵移動行為控制
- 物理運動模擬
- 模式切換管理
- 互動行為處理
- 拖拽投擲物理
"""

from .mov_module import MOVModule
from configs.config_loader import load_module_config


def register():
    """註冊MOV模組"""
    try:
        config = load_module_config("mov_module")
        instance = MOVModule(config=config)
        instance.initialize()
        return instance
            
    except Exception as e:
        from utils.debug_helper import error_log
        error_log(f"[MOV] 模組註冊失敗：{e}")
        return None


# 匯出主要類別
__all__ = ['MOVModule']
