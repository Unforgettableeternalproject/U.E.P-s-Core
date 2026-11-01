"""專屬 asyncio loop 執行緒（D70 (1)：ES 在 Core 行程內，跑在一條專屬 loop 執行緒）。

ES 是 asyncio，Core 是同步 SQLAlchemy 加執行緒：Core 對 ES 的呼叫一律經 ``call_soon`` 或 ``run``，
ES 的回呼（sink_factory、on_turn_ended 等）在這條 loop 上同步執行，**loop 上不得做阻塞 DB**。
本檔不 import echo_stream，也不認識傳輸方式（對外保持傳輸中立，P8 前再評估獨立行程）。
"""
from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Coroutine
from typing import Any, TypeVar

T = TypeVar("T")


class EsLoop:
    def __init__(self, name: str = "uep-echo-loop") -> None:
        self._name = name
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self.thread_id: int | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is None:
            raise RuntimeError("EsLoop 尚未啟動")
        return self._loop

    def in_loop_thread(self) -> bool:
        return self.thread_id is not None and threading.get_ident() == self.thread_id

    def start(self) -> None:
        if self.running:
            return
        self._ready.clear()
        self._thread = threading.Thread(target=self._run, name=self._name, daemon=True)
        self._thread.start()
        if not self._ready.wait(5.0):
            raise RuntimeError("ES loop 執行緒啟動逾時")

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self.thread_id = threading.get_ident()
        loop.call_soon(self._ready.set)
        try:
            loop.run_forever()
        finally:
            try:
                pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                loop.run_until_complete(loop.shutdown_asyncgens())
            finally:
                loop.close()

    def run(self, coro: Coroutine[Any, Any, T], timeout: float | None = None) -> T:
        """在 loop 上執行協程並等結果（呼叫端不得是 loop 執行緒本身）。"""
        if self.in_loop_thread():
            raise RuntimeError("不可在 ES loop 執行緒上同步等待自己")
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def call_soon(self, fn: Callable[..., Any], *args: Any) -> None:
        """從任何執行緒排程到 loop 上（冪等的控制訊號也走這條）。"""
        self.loop.call_soon_threadsafe(fn, *args)

    def stop(self, timeout: float = 5.0) -> None:
        thread, loop = self._thread, self._loop
        if thread is None or loop is None:
            return
        if thread.is_alive() and not loop.is_closed():
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout)
        self._thread = None
        self.thread_id = None
