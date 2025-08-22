# modules/nlp_module/__init__.py
"""
NLP模組 Phase 2 - 重構版本

新功能：
- 語者身份管理
- 分段意圖分析  
- call類型支援
- Working Context整合
- 實體抽取
"""

from .nlp_module import NLPModule
from configs.config_loader import load_module_config


def register():
    """註冊NLP模組"""
    try:
        config = load_module_config("nlp_module")
        instance = NLPModule(config=config)
        
        # 注意：不在這裡初始化模組，讓 UnifiedController 負責統一初始化
        return instance
            
    except Exception as e:
        from utils.debug_helper import error_log
        error_log(f"[NLP] 模組註冊失敗：{e}")
        return None


# 匯出主要類別
__all__ = [
    "NLPModule",
    "register"
]
