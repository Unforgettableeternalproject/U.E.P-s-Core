# -*- coding: utf-8 -*-
"""
模組測試分頁 - 過渡版本
提供向後兼容性，重新導向到新的模組化結構
"""

# 發出棄用警告
import warnings
warnings.warn(
    "module_test_tabs.py 已棄用，請改用 module_tabs 包中的個別模組",
    DeprecationWarning,
    stacklevel=2
)

# 從新的模組化結構導入所有類別，提供向後兼容性
try:
    from .module_tabs import *
    
    # 為了向後兼容，創建一些額外的別名
    # 這樣原有的導入語句仍然可以工作
    
except ImportError as e:
    # 如果模組化結構不可用，記錄錯誤但不中斷程序
    import sys
    print(f"警告: 無法導入新的模組化測試分頁結構: {e}", file=sys.stderr)
    print("請確認 module_tabs 包已正確配置", file=sys.stderr)
    
    # 提供最基本的錯誤處理
    class PlaceholderTestTab:
        def __init__(self, *args, **kwargs):
            raise ImportError("測試分頁模組化結構不可用")
    
    # 將所有可能的類別名稱設為佔位符
    STTTestTab = PlaceholderTestTab
    NLPTestTab = PlaceholderTestTab
    MEMTestTab = PlaceholderTestTab
    LLMTestTab = PlaceholderTestTab
    TTSTestTab = PlaceholderTestTab
    SYSTestTab = PlaceholderTestTab
    FrontendTestTab = PlaceholderTestTab
    BaseTestTab = PlaceholderTestTab
    PlaceholderTestTab = PlaceholderTestTab

# 導出所有類別名稱以確保向後兼容性
__all__ = [
    'BaseTestTab',
    'STTTestTab',
    'NLPTestTab',
    'FrontendTestTab',
    'MEMTestTab',
    'LLMTestTab', 
    'TTSTestTab',
    'SYSTestTab',
    'PlaceholderTestTab'
]
