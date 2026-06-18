"""
ContextEngine ABC — 可插拔的壓縮引擎抽象基類

定義了壓縮引擎的完整生命週期介面。內部可切換不同策略
（如 EchoCompressor vs MinimalCompressor），無需 plugin 系統。

引擎職責：
  - 判斷何時需要壓縮（should_compress）
  - 執行壓縮（compress / compress_async）
  - 追蹤 token 使用量（update_from_response）
  - 管理 session 生命週期（start / end / reset）
  - 報告狀態（get_status）

Lifecycle:
  1. 引擎在 ContextManager.__init__ 中建立
  2. on_session_start() 在會話開始時呼叫
  3. should_compress() 在每次 get_messages 時檢查
  4. compress() / compress_async() 在需要時執行
  5. update_from_response() 在每次 API 回應後更新
  6. on_session_end() 在會話結束時呼叫
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from echo_thought_core.schemas.message import Message
    from echo_thought_core.schemas.session import CompactionResult, CompressionTier

logger = logging.getLogger(__name__)


class ContextEngine(ABC):
    """壓縮引擎抽象基類 — 所有壓縮策略的共同介面"""

    # -- 身份 ---------------------------------------------------------------

    @property
    @abstractmethod
    def name(self) -> str:
        """引擎短名稱（如 'echo', 'minimal'）"""

    # -- 核心壓縮介面 -------------------------------------------------------

    @abstractmethod
    def should_compress(self, messages: List["Message"]) -> "CompressionTier":
        """判斷當前訊息列表需要的壓縮層級。

        回傳 TIER_0_NORMAL 表示不需要壓縮。
        """

    @abstractmethod
    def compress(
        self, messages: List["Message"], tier: "CompressionTier"
    ) -> Optional["CompactionResult"]:
        """同步壓縮（僅支援不需要 LLM 的層級，如 Layer 1）。

        回傳 None 表示無法在同步模式下壓縮此層級。
        """

    @abstractmethod
    async def compress_async(
        self,
        messages: List["Message"],
        tier: "CompressionTier",
        instructions: Optional[str] = None,
    ) -> Optional["CompactionResult"]:
        """異步壓縮（支援所有層級，含 LLM 呼叫）。"""

    # -- Token 追蹤 ---------------------------------------------------------

    def update_from_response(self, usage: Dict[str, Any]) -> None:
        """從 API 回應更新 token 使用量。

        usage dict 可包含：input_tokens, output_tokens,
        cache_read_tokens, cache_write_tokens 等。
        預設不做任何事——引擎可選擇性追蹤。
        """

    # -- Session 生命週期 ---------------------------------------------------

    def on_session_start(self, session_id: str, **kwargs: Any) -> None:
        """會話開始時呼叫。

        kwargs 可包含：platform, model, persona 等。
        """

    def on_session_end(
        self, session_id: str, messages: List["Message"]
    ) -> None:
        """會話結束時呼叫。

        用於刷新狀態、持久化摘要等。
        注意：僅在真正的 session 邊界呼叫，非每輪對話。
        """

    def on_session_reset(self) -> None:
        """會話重置時呼叫（/clear, /new）。

        重置壓縮計數器、冷卻時間等引擎內部狀態。
        """

    # -- 動態 Session 邊界（預留）------------------------------------------

    def should_end_session(
        self,
        messages: List["Message"],
        elapsed_seconds: float = 0.0,
        **signals: Any,
    ) -> bool:
        """系統自行判斷是否應結束當前 session。

        預設回傳 False（不主動結束）。
        子類可覆寫，基於時間、話題漂移、情境等信號判斷。

        signals 可包含：topic_drift_score, emotional_shift 等。
        """
        return False

    # -- 狀態報告 -----------------------------------------------------------

    def get_status(self) -> Dict[str, Any]:
        """回傳引擎狀態（供 CLI 儀表板顯示）。

        預設回傳基本資訊，子類應覆寫加入更多細節。
        """
        return {
            "engine": self.name,
        }

    # -- 模型切換 -----------------------------------------------------------

    def update_model(self, model: str, context_length: int = 0) -> None:
        """模型切換時呼叫。

        引擎可據此重新計算預算、調整壓縮閾值等。
        """
