"""
ToolSummarizer — 工具輸出的結構化摘要

替代簡單的佔位字串裁切，按工具類型產生有資訊量的單行摘要。
例如：
  [read_file] read config.py (1,200 chars, 45 lines)
  [terminal] ran `npm test` -> 47 lines output
  [search] query='compress' -> 12 results

支援註冊自訂格式化器，擴展新工具類型。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Callable, Dict, Optional

logger = logging.getLogger(__name__)

# 通用截斷保留長度
_HEAD_CHARS = 150
_TAIL_CHARS = 50


def _count_lines(text: str) -> int:
    """計算文字行數"""
    return text.count("\n") + 1 if text.strip() else 0


def _safe_json_args(raw: str) -> dict:
    """安全解析 JSON 引數"""
    try:
        return json.loads(raw) if raw else {}
    except (json.JSONDecodeError, TypeError):
        return {}


# ── 內建格式化器 ──────────────────────────────────────────────────────────────


def _fmt_read_file(tool_name: str, content: str, metadata: dict) -> str:
    lines = _count_lines(content)
    chars = len(content)
    path = metadata.get("args", {}).get("path", "")
    if path:
        return f"[{tool_name}] read {path} ({chars:,} chars, {lines} lines)"
    return f"[{tool_name}] ({chars:,} chars, {lines} lines)"


def _fmt_terminal(tool_name: str, content: str, metadata: dict) -> str:
    cmd = metadata.get("args", {}).get("command", "")
    if len(cmd) > 80:
        cmd = cmd[:77] + "..."
    lines = _count_lines(content)
    exit_match = re.search(r'"?exit_code"?\s*[:=]\s*(-?\d+)', content)
    exit_code = exit_match.group(1) if exit_match else "?"
    if cmd:
        return f"[{tool_name}] ran `{cmd}` -> exit {exit_code}, {lines} lines"
    return f"[{tool_name}] exit {exit_code}, {lines} lines output"


def _fmt_search(tool_name: str, content: str, metadata: dict) -> str:
    query = metadata.get("args", {}).get("query", "")
    pattern = metadata.get("args", {}).get("pattern", query)
    lines = _count_lines(content)
    return f"[{tool_name}] pattern='{pattern}' -> {lines} result lines"


def _fmt_write_file(tool_name: str, content: str, metadata: dict) -> str:
    path = metadata.get("args", {}).get("path", "?")
    chars = len(content)
    return f"[{tool_name}] wrote to {path} ({chars:,} chars result)"


# ── ToolSummarizer ────────────────────────────────────────────────────────────


class ToolSummarizer:
    """按工具類型產生結構化的單行摘要。

    Usage:
        summarizer = ToolSummarizer()
        summary = summarizer.summarize("read_file", content, metadata)
        # -> "[read_file] read config.py (1,200 chars, 45 lines)"
    """

    def __init__(self) -> None:
        self._formatters: Dict[str, Callable[[str, str, dict], str]] = {}
        # 註冊內建格式化器
        self._register_builtins()

    def _register_builtins(self) -> None:
        """註冊內建的工具格式化器"""
        for name in ("read_file", "Read"):
            self._formatters[name] = _fmt_read_file
        for name in ("terminal", "bash", "Bash", "shell", "execute"):
            self._formatters[name] = _fmt_terminal
        for name in ("search", "search_files", "grep", "Grep", "find"):
            self._formatters[name] = _fmt_search
        for name in ("write_file", "Write", "edit", "Edit", "patch"):
            self._formatters[name] = _fmt_write_file

    def register_formatter(
        self,
        tool_name: str,
        formatter: Callable[[str, str, dict], str],
    ) -> None:
        """註冊自訂格式化器。

        formatter 簽章: (tool_name: str, content: str, metadata: dict) -> str
        """
        self._formatters[tool_name] = formatter

    def summarize(
        self,
        tool_name: Optional[str],
        content: str,
        metadata: Optional[dict] = None,
    ) -> str:
        """產生工具輸出的結構化摘要。

        若有對應的格式化器則使用，否則做通用縮短。
        """
        name = tool_name or "unknown"
        meta = metadata or {}

        formatter = self._formatters.get(name)
        if formatter:
            try:
                return formatter(name, content, meta)
            except Exception:
                pass

        # 通用 fallback
        return self._generic_summary(name, content)

    def _generic_summary(self, tool_name: str, content: str) -> str:
        """通用工具摘要：保留首尾 + 統計"""
        chars = len(content)
        lines = _count_lines(content)

        if chars <= _HEAD_CHARS + _TAIL_CHARS:
            return content  # 短到不需要摘要

        head = content[:_HEAD_CHARS].rstrip()
        tail = content[-_TAIL_CHARS:].lstrip()
        return (
            f"[{tool_name}] ({chars:,} chars, {lines} lines)\n"
            f"{head}\n...[{chars - _HEAD_CHARS - _TAIL_CHARS:,} chars omitted]...\n{tail}"
        )


# 全域單例
_default_summarizer: Optional[ToolSummarizer] = None


def get_tool_summarizer() -> ToolSummarizer:
    """取得全域 ToolSummarizer 實例"""
    global _default_summarizer
    if _default_summarizer is None:
        _default_summarizer = ToolSummarizer()
    return _default_summarizer
