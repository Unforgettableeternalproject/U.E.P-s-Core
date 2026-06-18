"""
MemoryBridge — Echo Memory 整合介面

輕量 Protocol，定義壓縮引擎與記憶系統的整合點。
使用 Protocol（PEP 544）而非 ABC，不強制繼承。

整合點：
  - on_pre_compress: 壓縮前提取需保留的記憶上下文
  - on_post_compress: 壓縮後通知記憶系統
  - on_session_start / on_session_end: 會話生命週期
  - prefetch: 每輪對話前預抓取記憶

Usage:
    class MyMemoryBridge:
        def on_pre_compress(self, messages):
            # 提取重要事實
            return "User prefers dark mode. Project deadline is June 30."

        def on_post_compress(self, result):
            # 持久化壓縮摘要
            save_to_echo_memory(result.summary_text)
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, runtime_checkable, TYPE_CHECKING

try:
    from typing import Protocol
except ImportError:
    from typing_extensions import Protocol  # type: ignore[assignment]

if TYPE_CHECKING:
    from echo_thought_core.schemas.message import Message
    from echo_thought_core.schemas.session import CompactionResult

logger = logging.getLogger(__name__)


@runtime_checkable
class MemoryBridge(Protocol):
    """Echo Memory 整合協定"""

    def on_pre_compress(self, messages: List["Message"]) -> str:
        """壓縮前呼叫 — 提取需保留在摘要中的記憶上下文。

        回傳的字串會被注入摘要提示，讓 LLM 知道哪些事實必須保留。
        回傳空字串表示無需額外保留。
        """
        ...

    def on_post_compress(self, result: "CompactionResult") -> None:
        """壓縮後呼叫 — 通知記憶系統壓縮結果。

        可用於將壓縮摘要持久化到 Echo Memory。
        """
        ...

    def on_session_start(self, session_id: str) -> None:
        """會話開始時呼叫。"""
        ...

    def on_session_end(
        self, session_id: str, messages: List["Message"]
    ) -> None:
        """會話結束時呼叫 — 可做事實提取、記憶鞏固等。"""
        ...

    def prefetch(self, query: str) -> str:
        """每輪對話前預抓取相關記憶上下文。

        回傳的字串可被注入 system prompt 或 user context。
        回傳空字串表示無相關記憶。
        """
        ...


class NullMemoryBridge:
    """無操作的預設 MemoryBridge 實作。

    所有方法都是 no-op，用於測試和無記憶模式。
    """

    def on_pre_compress(self, messages: List["Message"]) -> str:
        return ""

    def on_post_compress(self, result: "CompactionResult") -> None:
        pass

    def on_session_start(self, session_id: str) -> None:
        pass

    def on_session_end(
        self, session_id: str, messages: List["Message"]
    ) -> None:
        pass

    def prefetch(self, query: str) -> str:
        return ""
