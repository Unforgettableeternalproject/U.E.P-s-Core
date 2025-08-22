# modules/mem_module/__init__.py
"""
MEM模組 - 記憶與知識庫模組

功能:
- 長短期記憶管理
- 知識儲存與檢索
- 上下文管理
"""

from .mem_module import MEMModule
from configs.config_loader import load_module_config


def register():
    """註冊MEM模組"""
    try:
        config = load_module_config("mem_module")
        instance = MEMModule(config=config)
        instance.initialize()
        return instance
            
    except Exception as e:
        from utils.debug_helper import error_log
        error_log(f"[MEM] 模組註冊失敗：{e}")
        return None


# 匯出主要類別
__all__ = [
    "MEMModule",
    "register"
]
