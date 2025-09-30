# module_tabs/__init__.py
"""
模組測試分頁包

統一導入所有模組測試分頁類別
"""

import os
import sys

# 添加當前目錄到路徑
current_dir = os.path.dirname(os.path.abspath(__file__))
if current_dir not in sys.path:
    sys.path.insert(0, current_dir)

# 導入基礎類別
from base_test_tab import BaseTestTab

# 導入已重構的模組分頁
from stt_test_tab import STTTestTab
from nlp_test_tab import NLPTestTab
from mem_test_tab import MEMTestTab
from llm_test_tab import LLMTestTab

# 導入 Frontend 整合分頁（UI+ANI+MOV 的整合版本）
from frontend_test_tab import FrontendTestTab

# 導入待重構的模組分頁（使用佔位模式）
from placeholder_test_tab import (
    PlaceholderTestTab,
    TTSTestTab,
    SYSTestTab
)

# 向後兼容的導出
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
