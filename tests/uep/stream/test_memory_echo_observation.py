"""EchoMemoryAdapter 的 record_observation / forget_activity 透傳（假 engine）。"""

from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timezone

from uep.stream.adapters.memory_echo import EchoMemoryAdapter

from .test_memory_echo import _Ctx, _Engine
from .test_memory_echo_shared import _still_blocked, _wait_set

T0 = datetime(2026, 10, 27, 1, 0, tzinfo=timezone.utc)
T1 = datetime(2026, 10, 27, 2, 0, tzinfo=timezone.utc)


class _ObsEngine(_Engine):
    def __init__(self, fine: bool = False):
        super().__init__(_Ctx())
        self.obs_calls: list[dict] = []
        self.forget_calls: list[str] = []
        self.entered = threading.Event()
        self.gate = threading.Event()
        self.gate.set()
        self.fail = False
        if fine:
            self.warmup = lambda background=True: None
            self.ensure_initialized = lambda: None

    def record_observation(self, **kw):
        self.entered.set()
        assert self.gate.wait(5)
        if self.fail:
            raise ValueError("壞資料")
        self.obs_calls.append(kw)
        return "ev_obs_1"

    def forget_activity(self, kind="game_activity"):
        if self.fail:
            raise RuntimeError("爆了")
        self.forget_calls.append(kind)
        return 3


async def test_record_observation_參數透傳():
    eng = _ObsEngine()
    adapter = EchoMemoryAdapter(engine=eng)
    out = await adapter.record_observation(
        kind="game_activity", subject="Hades", started_at=T0, ended_at=T1,
        idempotency_key="u1:Hades:t0", attrs={"a": 1},
    )
    assert out == "ev_obs_1"
    assert eng.obs_calls == [{
        "kind": "game_activity", "subject": "Hades", "started_at": T0, "ended_at": T1,
        "idempotency_key": "u1:Hades:t0", "attrs": {"a": 1},
    }]
    await adapter.aclose()


async def test_record_observation_未給選填就不傳():
    eng = _ObsEngine()
    adapter = EchoMemoryAdapter(engine=eng)
    await adapter.record_observation(kind="game_activity", subject="X", started_at=T0, ended_at=T1)
    assert set(eng.obs_calls[0]) == {"kind", "subject", "started_at", "ended_at"}
    await adapter.aclose()


async def test_forget_activity_透傳():
    eng = _ObsEngine()
    adapter = EchoMemoryAdapter(engine=eng)
    assert await adapter.forget_activity() is True
    assert await adapter.forget_activity("game_activity") is True
    assert eng.forget_calls == ["game_activity", "game_activity"]
    await adapter.aclose()


async def test_引擎沒有方法_回None與False():
    adapter = EchoMemoryAdapter(engine=_Engine(_Ctx()))
    out = await adapter.record_observation(
        kind="game_activity", subject="X", started_at=T0, ended_at=T1)
    assert out is None
    assert await adapter.forget_activity() is False
    await adapter.aclose()


async def test_例外不外拋():
    eng = _ObsEngine()
    eng.fail = True
    adapter = EchoMemoryAdapter(engine=eng)
    out = await adapter.record_observation(
        kind="game_activity", subject="X", started_at=T0, ended_at=T1)
    assert out is None
    assert await adapter.forget_activity() is False
    await adapter.aclose()


async def test_細粒度模式_dream獨佔時record被擋_forget亦同():
    eng = _ObsEngine(fine=True)
    gate = threading.Event()
    dream_in = threading.Event()

    def _dream(triggered_by="manual"):
        dream_in.set()
        assert gate.wait(5)
        return {"ok": True}

    eng.dream = _dream
    adapter = EchoMemoryAdapter(engine=eng, engine_lock=threading.Lock())
    t_dream = asyncio.ensure_future(adapter.dream())
    assert await _wait_set(dream_in)
    t_rec = asyncio.ensure_future(adapter.record_observation(
        kind="game_activity", subject="X", started_at=T0, ended_at=T1))
    t_forget = asyncio.ensure_future(adapter.forget_activity())
    assert await _still_blocked(eng.entered), "dream 期間 record_observation 必須等"
    assert not t_forget.done()
    assert eng.forget_calls == []
    gate.set()
    await t_dream
    assert await t_rec == "ev_obs_1"
    assert await t_forget is True
    await adapter.aclose()


async def test_舊引擎_record持有engine_lock():
    lock = threading.Lock()
    eng = _ObsEngine()
    held: list[bool] = []
    orig = eng.record_observation
    eng.record_observation = lambda **kw: (held.append(lock.locked()), orig(**kw))[1]
    adapter = EchoMemoryAdapter(engine=eng, engine_lock=lock)
    await adapter.record_observation(kind="game_activity", subject="X", started_at=T0, ended_at=T1)
    assert held == [True]
    await adapter.aclose()
