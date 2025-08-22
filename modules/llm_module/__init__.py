# modules/llm_module/__init__.py
"""
LLM模組 - 大型語言模型模組

功能:
- 自然語言理解
- 文本生成
- 對話管理
- 函數呼叫
"""

from .llm_module import LLMModule
from configs.config_loader import load_module_config


def register():
    """註冊LLM模組"""
    try:
        config = load_module_config("llm_module")
        instance = LLMModule(config=config)
        # 注意：不在這裡初始化模組，讓 UnifiedController 負責統一初始化
        return instance
            
    except Exception as e:
        from utils.debug_helper import error_log
        error_log(f"[LLM] 模組註冊失敗：{e}")
        return None


# 匯出主要類別
__all__ = [
    "LLMModule",
    "register"
]
