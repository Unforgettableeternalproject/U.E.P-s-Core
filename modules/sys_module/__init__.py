# modules/sys_module/__init__.py
"""
SYS模組 - 系統管理模組

功能:
- 系統控制
- 工作流管理
- 文件操作
- 命令執行
"""

from .sys_module import SYSModule
from configs.config_loader import load_module_config


def register():
    """註冊SYS模組"""
    try:
        config = load_module_config("sys_module")
        instance = SYSModule(config=config)
        instance.initialize()
        return instance
            
    except Exception as e:
        from utils.debug_helper import error_log
        error_log(f"[SYS] 模組註冊失敗：{e}")
        return None


# 匯出主要類別
__all__ = [
    "SYSModule",
    "register"
]
