"""有界串流通道——背壓與 push→pull 橋接。

## 為什麼必須有界

無界 queue 會讓 barge-in 失效。想像 TTS 一路合成完 10 句塞進無界 queue，
使用者在第 2 句時插話——後面 8 句的 GPU 時間全白花，而且取消的「上游傳播」
變成一句空話：上游早就跑完了，根本沒東西可以取消。

**有界 queue 的滿溢本身就是背壓訊號**：TTS 因為 put 阻塞而停在第 3 句，
這時取消才真的省得到 GPU 時間。所以 maxsize 不是效能調參，是取消機制的
一部分。

## 為什麼不直接用 asyncio.Queue

三個缺口：

1. **沒有「結束」的概念**——要自己約定 sentinel，每個 stage 各寫一次很容易漏
2. **不整合取消**——阻塞在 ``get()`` 時被取消，只能靠外層 task cancel
3. **不能從別的執行緒安全寫入**——而 Phase 1 的 IndexTTS2 是同步 push callback
   跑在 executor 執行緒裡，這是必要能力
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Generic, TypeVar

from .contracts.cancellation import CancellationToken, CancelledError, CancelReason

T = TypeVar("T")


class ChannelClosed(Exception):
    """對已關閉的通道寫入。"""


class StreamChannel(Generic[T]):
    """有界、可關閉、可從其他執行緒寫入的單向通道。

    典型用法（push→pull 橋接，Phase 1 的 TTS adapter 會這樣用）::

        channel = StreamChannel(maxsize=2, token=token)

        def on_segment(tensor, idx, total):       # 同步 callback，在推理執行緒
            chunk = to_audio_chunk(tensor, idx)
            channel.put_threadsafe(chunk, loop)   # 滿了會阻塞推理執行緒 = 背壓

        await loop.run_in_executor(None, backend.generate, text, on_segment)
        channel.close()

        async for chunk in channel:               # 在 event loop 這側消費
            ...
    """

    __slots__ = (
        "_queue",
        "_token",
        "_closed",
        "_error",
        "_maxsize",
        "_item_event",
        "_close_event",
    )

    def __init__(
        self,
        maxsize: int = 4,
        token: CancellationToken | None = None,
    ) -> None:
        if maxsize < 1:
            raise ValueError("maxsize 必須 >= 1；無界通道會讓取消機制失效")
        self._maxsize = maxsize
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self._token = token
        self._closed = False
        self._error: BaseException | None = None
        # 結束訊號用獨立事件而不是塞進 queue 的 sentinel——
        # 通道滿的時候 sentinel 放不進去會炸 QueueFull，
        # 而「產完最後一項時消費端還沒跟上」正是最常見的情況。
        self._item_event = asyncio.Event()
        self._close_event = asyncio.Event()

    @property
    def maxsize(self) -> int:
        return self._maxsize

    @property
    def is_closed(self) -> bool:
        return self._closed

    def qsize(self) -> int:
        return self._queue.qsize()

    # --- 寫入 ---

    async def put(self, item: T) -> None:
        """寫入。通道滿時阻塞——這就是背壓。

        阻塞期間仍可被取消。這條路徑是 barge-in 能穿透到 GPU 推理的關鍵：
        TTS 的同步 callback 透過 :meth:`put_threadsafe` 阻塞在這裡，
        取消時它會拿到 :class:`CancelledError`，從 callback 拋出去，
        中止正在跑的 ``generate()``。不能被取消的話，插話會卡死
        ——沒有消費者了，卻還在等空位。
        """
        if self._closed:
            raise ChannelClosed("通道已關閉")

        if self._token is None:
            await self._queue.put(item)
            self._item_event.set()
            return

        self._token.raise_if_cancelled()
        if not self._queue.full():
            self._queue.put_nowait(item)
            self._item_event.set()
            return

        put_task = asyncio.ensure_future(self._queue.put(item))
        cancel_task = asyncio.ensure_future(self._token.wait())
        try:
            done, _ = await asyncio.wait(
                {put_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED
            )
        except BaseException:
            # 外部把整個 task 取消掉（例如 feeder 被中止）。兩個內部 task
            # 都要清掉，否則會留下懸空的 Queue.put coroutine，
            # 在 event loop 關閉後才爆出 "Event loop is closed"。
            put_task.cancel()
            cancel_task.cancel()
            raise
        finally:
            if not cancel_task.done():
                cancel_task.cancel()
        if put_task not in done:
            put_task.cancel()
            raise CancelledError(self._token.reason or CancelReason.SHUTDOWN)
        self._item_event.set()

    def put_threadsafe(self, item: T, loop: asyncio.AbstractEventLoop) -> None:
        """從非 event loop 執行緒寫入，滿時阻塞呼叫端執行緒。

        給同步推理 callback 用（IndexTTS2 的 ``on_segment_audio``）。
        阻塞推理執行緒正是我們要的效果：**背壓要能傳到 GPU**，
        否則模型會一路算完，barge-in 就省不到任何東西。
        """
        if self._closed:
            raise ChannelClosed("通道已關閉")
        future = asyncio.run_coroutine_threadsafe(self.put(item), loop)
        future.result()  # 阻塞直到有空位

    def close(self) -> None:
        """正常結束。消費端讀完剩餘項目後迭代結束。冪等。"""
        if self._closed:
            return
        self._closed = True
        self._close_event.set()

    def close_threadsafe(self, loop: asyncio.AbstractEventLoop) -> None:
        """從其他執行緒關閉。"""
        if self._closed:
            return
        loop.call_soon_threadsafe(self.close)

    def fail(self, error: BaseException) -> None:
        """異常結束。消費端讀完剩餘項目後會拋出 ``error``。"""
        if self._closed:
            return
        self._error = error
        self._closed = True
        self._close_event.set()

    # --- 讀取 ---

    async def get(self) -> T:
        """讀一項。通道結束且讀完時拋 :class:`StopAsyncIteration`。

        取消、有新項目、通道關閉三者是 race——取消不必等 queue 有東西才生效，
        否則 barge-in 會卡在「等 TTS 吐出下一段」上。
        """
        while True:
            if self._token is not None and self._token.is_cancelled:
                raise CancelledError(self._token.reason or CancelReason.SHUTDOWN)

            if not self._queue.empty():
                item = self._queue.get_nowait()
                if self._queue.empty():
                    self._item_event.clear()
                return item

            # 關閉後仍要先讀完殘留項目（上面那段），空了才真的結束
            if self._closed:
                if self._error is not None:
                    raise self._error
                raise StopAsyncIteration

            waiters = [
                asyncio.ensure_future(self._item_event.wait()),
                asyncio.ensure_future(self._close_event.wait()),
            ]
            if self._token is not None:
                waiters.append(asyncio.ensure_future(self._token.wait()))
            try:
                await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for waiter in waiters:
                    if not waiter.done():
                        waiter.cancel()

    def __aiter__(self) -> AsyncIterator[T]:
        return self

    async def __anext__(self) -> T:
        return await self.get()
