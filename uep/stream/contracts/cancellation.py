"""取消路徑（barge-in 的骨幹）。

設計決策（艾斯維爾 2026-08-20 §7.2）：插話中止訊號必須往**上游**傳播——
使用者一開口，TTS 停播、LLM 停止生成、剩餘 token 丟棄。

為什麼不直接用 ``asyncio.CancelledError``：

1. CancelledError 只能中斷「正在 await 的那個 task」，無法表達「為什麼取消」。
   我們需要區分 barge-in、逾時、系統關閉——三者的善後行為不同
   （barge-in 要把「已播出的半句」寫回對話歷史，逾時不用）。
2. 真模組裡有大量**同步阻塞**的推理呼叫（IndexTTS2 的 generate、
   faster-whisper 的 transcribe），它們不在 asyncio 的取消範圍內，
   只能靠協作式輪詢 flag 來中止。
3. 取消需要「扇出」：一個 turn 的 token 要同時通知三層 + N 個 queue。

所以用**協作式取消**：一個可等待、可查詢、可註冊 callback 的 token。
每一層在自己的 loop 裡主動檢查，這是唯一能同時涵蓋 async 與同步推理的做法。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from enum import Enum


class CancelReason(str, Enum):
    """取消原因。善後行為依此分流。"""

    BARGE_IN = "barge_in"
    """使用者插話。已播出的部分音訊要回寫對話歷史（LLM 需知道自己只講到一半）。"""

    TIMEOUT = "timeout"
    """階段逾時。視為失敗，不回寫。"""

    UPSTREAM_ERROR = "upstream_error"
    """上游拋錯，下游連帶取消。"""

    SHUTDOWN = "shutdown"
    """系統關閉。盡快釋放資源，不做善後。"""

    SUPERSEDED = "superseded"
    """同一使用者送出新 turn，舊 turn 作廢。"""

    HOST = "host_cancelled"
    """宿主明確取消單輪整輪（``TurnCoordinator.cancel_turn``）。
    細項原因看 outcome 的 ``reason_code``。"""


class CancelledError(Exception):
    """管線內部的取消例外。

    刻意**不繼承** ``asyncio.CancelledError``——後者在 Python 3.8+ 繼承自
    BaseException，會穿透一般的 ``except Exception``，讓 stage 難以做善後。
    我們要的是「可被攔截、可善後」的取消。
    """

    def __init__(self, reason: CancelReason, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"cancelled: {reason.value}{f' ({detail})' if detail else ''}")


class CancellationToken:
    """協作式取消 token，支援樹狀傳播。

    典型用法::

        token = CancellationToken()

        # 在 stage 的串流迴圈裡
        async for chunk in source:
            token.raise_if_cancelled()   # 協作點
            yield chunk

        # 在同步推理迴圈裡（真模組會用到）
        for step in range(total_steps):
            if token.is_cancelled:
                break

    子 token 用於「取消單一 stage 但不影響整個 turn」的場景
    （例如 Memory retrieve 逾時了，但 LLM 照樣繼續）。父取消會傳給子，
    子取消**不會**往上冒。
    """

    __slots__ = ("_event", "_reason", "_detail", "_callbacks", "_children", "_parent")

    def __init__(self, parent: CancellationToken | None = None) -> None:
        self._event = asyncio.Event()
        self._reason: CancelReason | None = None
        self._detail: str = ""
        self._callbacks: list[Callable[[CancelReason], None]] = []
        self._children: list[CancellationToken] = []
        self._parent = parent
        if parent is not None:
            parent._children.append(self)
            # 父已取消 → 子立刻繼承狀態，避免建立時序造成漏接
            if parent.is_cancelled:
                self.cancel(parent.reason or CancelReason.SHUTDOWN, parent.detail)

    # --- 查詢 ---

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> CancelReason | None:
        return self._reason

    @property
    def detail(self) -> str:
        return self._detail

    # --- 觸發 ---

    def cancel(self, reason: CancelReason = CancelReason.BARGE_IN, detail: str = "") -> None:
        """觸發取消。冪等——重複呼叫只有第一次的原因生效。"""
        if self._event.is_set():
            return
        self._reason = reason
        self._detail = detail
        self._event.set()

        # callback 不能讓一個壞掉的訂閱者擋住其他人的取消
        for cb in list(self._callbacks):
            try:
                cb(reason)
            except Exception:  # noqa: BLE001 - 取消路徑必須是 best-effort
                pass

        for child in list(self._children):
            child.cancel(reason, detail)

    # --- 協作點 ---

    def raise_if_cancelled(self) -> None:
        """在串流迴圈中呼叫。已取消則拋 :class:`CancelledError`。"""
        if self._event.is_set():
            raise CancelledError(self._reason or CancelReason.SHUTDOWN, self._detail)

    async def wait(self) -> CancelReason:
        """等到被取消為止。用於「取消 vs 正常完成」的 race。"""
        await self._event.wait()
        return self._reason or CancelReason.SHUTDOWN

    # --- 訂閱 ---

    def on_cancel(self, callback: Callable[[CancelReason], None]) -> None:
        """註冊取消時的同步 callback。

        給「必須立刻停」的資源用——例如音訊播放裝置的 stop()，
        它不能等到下一次協作點才執行。
        若註冊時已取消，callback 立即執行（避免競態漏接）。
        """
        if self._event.is_set():
            try:
                callback(self._reason or CancelReason.SHUTDOWN)
            except Exception:  # noqa: BLE001
                pass
            return
        self._callbacks.append(callback)

    def child(self) -> CancellationToken:
        """建立子 token。父取消會傳下來，子取消不會往上冒。"""
        return CancellationToken(parent=self)

    def detach(self) -> None:
        """從父節點解除連結，避免長壽命父 token 累積已結束的子節點。"""
        if self._parent is not None:
            try:
                self._parent._children.remove(self)
            except ValueError:
                pass
            self._parent = None
