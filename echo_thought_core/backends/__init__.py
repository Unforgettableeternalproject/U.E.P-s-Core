"""Backends — 多 LLM 後端抽象層"""

# 匯出子模組，讓 unittest.mock.patch("...backends.gemini.xxx") 可解析
from echo_thought_core.backends import gemini  # noqa: F401
