"""EchoMemoryAdapter 的共用 executor / engine_lock（voice-memory 批 A3）。"""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from uep.stream.adapters.memory_echo import EchoMemoryAdapter

from .test_memory_echo import _Ctx, _Engine, _StoreEngine


async def test_注入executor_aclose不關它():
    shared = ThreadPoolExecutor(max_workers=1)
    try:
        adapter = EchoMemoryAdapter(engine=_Engine(_Ctx()), executor=shared)
        await adapter.retrieve("問題")
        await adapter.aclose()
        assert shared.submit(lambda: 42).result() == 42  # 仍可用
    finally:
        shared.shutdown(wait=True)


async def test_自建executor_aclose會關():
    adapter = EchoMemoryAdapter(engine=_Engine(_Ctx()))
    await adapter.aclose()
    with pytest.raises(RuntimeError):
        adapter._executor.submit(lambda: 1)


async def test_engine_lock_在engine呼叫期間持有():
    lock = threading.Lock()
    held: list[bool] = []

    class _LockedEngine(_StoreEngine):
        def retrieve(self, text, **kw):
            held.append(lock.locked())
            return super().retrieve(text, **kw)

        def store_episode(self, **kw):
            held.append(lock.locked())
            return super().store_episode(**kw)

    adapter = EchoMemoryAdapter(engine=_LockedEngine(), engine_lock=lock)
    adapter.assess_mode = "off"
    await adapter.retrieve("問題")
    await adapter.store("問題", "回答")
    assert held == [True, True]
    assert not lock.locked(), "工作結束要釋放"


async def test_外部持鎖時_adapter工作會等():
    lock = threading.Lock()
    adapter = EchoMemoryAdapter(engine=_Engine(_Ctx()), engine_lock=lock)
    lock.acquire()
    task = asyncio.ensure_future(adapter.retrieve("問題"))
    await asyncio.sleep(0.05)
    assert not task.done(), "鎖被別條路徑持有時要排隊"
    lock.release()
    await asyncio.wait_for(task, 2)


async def test_沒給lock時行為與原本相同():
    engine = _Engine(_Ctx())
    adapter = EchoMemoryAdapter(engine=engine)
    assert adapter._engine_lock is None
    await adapter.retrieve("問題")
    assert len(engine.calls) == 1


# --- 依引擎能力縮小鎖範圍 ---


class _GateEngine(_StoreEngine):
    """可用 Event 卡住指定呼叫的假引擎；``fine`` 決定有沒有 ``warmup``（新舊引擎）。"""

    def __init__(self, fine: bool):
        super().__init__()
        self.gates = {n: threading.Event() for n in ("retrieve", "store", "dream")}
        self.entered = {n: threading.Event() for n in ("retrieve", "store", "dream")}
        if fine:
            self.warmup = lambda background=True: None
            self.ensure_initialized = lambda: None

    def _enter(self, name):
        self.entered[name].set()
        assert self.gates[name].wait(5), f"{name} 沒被放行"

    def retrieve(self, text, **kw):
        self._enter("retrieve")
        return super().retrieve(text, **kw)

    def store_episode(self, **kw):
        self._enter("store")
        return super().store_episode(**kw)

    def dream(self, triggered_by="manual"):
        self._enter("dream")
        return {"ok": True}


async def _wait_set(ev: threading.Event, timeout=2.0) -> bool:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, ev.wait, timeout)


async def _still_blocked(ev: threading.Event) -> bool:
    """給排程一點機會；事件仍未觸發 = 被擋住。"""
    await asyncio.sleep(0.05)
    return not ev.is_set()


def _adapter(fine: bool, **kw):
    eng = _GateEngine(fine)
    adapter = EchoMemoryAdapter(engine=eng, **kw)
    adapter.assess_mode = "off"
    return eng, adapter


async def test_舊引擎_兩個呼叫不重疊():
    eng, adapter = _adapter(fine=False, engine_lock=threading.Lock())
    t_store = asyncio.ensure_future(adapter.store("問題", "回答"))
    assert await _wait_set(eng.entered["store"])
    t_ret = asyncio.ensure_future(adapter.retrieve("問題"))
    assert await _still_blocked(eng.entered["retrieve"]), "舊引擎 store 進行中 retrieve 必須等"
    for g in eng.gates.values():
        g.set()
    await asyncio.gather(t_store, t_ret)
    await adapter.aclose()


async def test_新引擎_store進行中_retrieve不被擋():
    eng, adapter = _adapter(fine=True, engine_lock=threading.Lock())
    t_store = asyncio.ensure_future(adapter.store("問題", "回答"))
    assert await _wait_set(eng.entered["store"])
    eng.gates["retrieve"].set()
    await adapter.retrieve("問題")
    assert eng.entered["retrieve"].is_set()
    assert not t_store.done()
    eng.gates["store"].set()
    await t_store
    await adapter.aclose()


async def test_新引擎_dream進行中_store被擋_retrieve不被擋():
    eng, adapter = _adapter(fine=True, engine_lock=threading.Lock())
    t_dream = asyncio.ensure_future(adapter.dream())
    assert await _wait_set(eng.entered["dream"])
    t_store = asyncio.ensure_future(adapter.store("問題", "回答"))
    assert await _still_blocked(eng.entered["store"]), "dream 期間 store 必須等"
    eng.gates["retrieve"].set()
    await adapter.retrieve("問題")
    assert eng.entered["retrieve"].is_set()
    eng.gates["dream"].set()
    await t_dream
    assert await _wait_set(eng.entered["store"]), "dream 結束後 store 要能進"
    eng.gates["store"].set()
    await t_store
    await adapter.aclose()


async def test_新引擎_store進行中_dream要等store完成():
    eng, adapter = _adapter(fine=True)
    t_store = asyncio.ensure_future(adapter.store("問題", "回答"))
    assert await _wait_set(eng.entered["store"])
    t_dream = asyncio.ensure_future(adapter.dream())
    assert await _still_blocked(eng.entered["dream"])
    eng.gates["store"].set()
    await t_store
    assert await _wait_set(eng.entered["dream"])
    eng.gates["dream"].set()
    await t_dream
    await adapter.aclose()


async def test_新引擎_reflection與dream互斥():
    """reflection 由宿主持 engine_lock 執行；dream 必須等它放手。"""
    lock = threading.Lock()
    eng, adapter = _adapter(fine=True, engine_lock=lock)
    lock.acquire()  # 模擬宿主正在跑 reflection
    t_dream = asyncio.ensure_future(adapter.dream())
    assert await _still_blocked(eng.entered["dream"])
    lock.release()
    assert await _wait_set(eng.entered["dream"])
    # dream 持有 engine_lock 期間，宿主 reflection 拿不到
    assert not lock.acquire(blocking=False)
    eng.gates["dream"].set()
    await t_dream
    assert lock.acquire(blocking=False)
    lock.release()
    await adapter.aclose()


async def test_新引擎_warmup呼叫引擎warmup():
    called: list[bool] = []
    eng, adapter = _adapter(fine=True)
    eng.warmup = lambda background=True: called.append(background)
    assert await adapter.warmup() is True
    assert called == [True]
    await adapter.aclose()
