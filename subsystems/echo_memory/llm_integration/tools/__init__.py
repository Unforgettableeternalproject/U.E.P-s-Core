"""
LLM Tools — Gemini Function Calling 工具層

匯出工具定義和轉換函式供 LLMClient 使用。
"""

from llm_integration.tools.definitions import TOOL_DECLARATIONS, to_gemini_tools

__all__ = ["TOOL_DECLARATIONS", "to_gemini_tools"]
