# modules/sys_module/__init__.py
"""
SYS模組 - 系統管理模組

功能:
- 系統控制
- 工作流管理
- 文件操作
- 命令執行

為了讓獨立子模組測試（例如 mischief actions）不受 Windows 依賴影響，導入時若缺少依賴會退回輕量化 stub。
"""
import os
from configs.config_loader import load_module_config

try:
    from .sys_module import SYSModule  # 正常情況
except ModuleNotFoundError as e:
    # 減少測試時的依賴（例如缺少 win32clipboard），提供 stub 以便 actions 子模組可被載入
    class SYSModule:  # type: ignore
        def __init__(self, *args, **kwargs):
            raise ImportError(f"SYSModule unavailable due to missing dependency: {e}")
    os.environ.setdefault("UEP_SKIP_SYS_INIT", "1")


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
