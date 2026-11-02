"""
Content Sanitizer — 內容消殺

對應 OpenSoul soul/core/agent.py Step 10。
清理 LLM 回覆中的偽造工具標記，防止使用者被誤導。

規則：
  1. 清除已知的偽造工具標記模式
  2. 清除後若回覆為空，保留 fallback
  3. 若有真正的工具呼叫，由呼叫方加回官方標記
"""

from __future__ import annotations

import re
import logging

logger = logging.getLogger("echo_memory.utils.sanitizer")

# 偽造工具標記模式（對齊 OpenSoul + Gemini/Claude 格式）
_FAKE_PATTERNS = [
    r"\[tool_?call[:\s].*?\]",
    r"\[tool_?result[:\s].*?\]",
    r"<tool_use>.*?</tool_use>",
    r"<function_call>.*?</function_call>",
    r"function_call\s*:\s*\{.*?\}",
    r"\[🛠️.*?實際執行.*?\]",
    r"\[系統已.*?執行.*?\]",
    r"\[系統已實際執行.*?\]",
]

# 預編譯 regex
_COMPILED_PATTERNS = [
    re.compile(p, re.IGNORECASE | re.DOTALL) for p in _FAKE_PATTERNS
]


def sanitize_llm_output(
    text: str,
    fallback: str = "（思考中...）",
) -> str:
    """
    清理 LLM 回覆中的偽造工具標記。

    Args:
        text: LLM 原始回覆
        fallback: 清除後若為空的替代文本

    Returns:
        清理後的文本
    """
    if not text:
        return text

    cleaned = text
    removed_count = 0

    for pattern in _COMPILED_PATTERNS:
        result = pattern.subn("", cleaned)
        cleaned = result[0]
        removed_count += result[1]

    if removed_count > 0:
        logger.debug(f"[Sanitizer] 清除 {removed_count} 個偽造工具標記")

    # 清除後若為空或只剩空白
    if not cleaned.strip():
        cleaned = fallback

    return cleaned.strip()


def has_fake_tool_markers(text: str) -> bool:
    """
    檢查文本是否包含偽造工具標記。

    Args:
        text: 要檢查的文本

    Returns:
        True 若包含偽造標記
    """
    for pattern in _COMPILED_PATTERNS:
        if pattern.search(text):
            return True
    return False
