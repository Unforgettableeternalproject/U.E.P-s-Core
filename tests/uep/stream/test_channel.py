"""StreamChannel 的背壓與取消測試。

有界 queue 不是效能調參，是取消機制的一部分——無界 queue 會讓
TTS 一路合成到底，barge-in 就白做了。
"""

from __future__ import annotations

import asyncio

import pytest

from uep.stream.contracts.cancellation import (
    CancellationToken,
    CancelledError,
    CancelReason,
)
from uep.stream.channel import ChannelClosed, StreamChannel


def test_拒絕無界通道():
    """無界會讓取消機制失效，所以直接不給建。"""
    with pytest.raises(ValueError):
        StreamChannel(maxsize=0)


async def test_基本收送():
    channel = StreamChannel(maxsize=2)
    await channel.put(1)
    await channel.put(2)
    channel.close()
    assert [x async for x in channel] == [1, 2]


async def test_滿了會阻塞_這就是背壓():
    channel = StreamChannel(maxsize=1)
    await channel.put("a")

    put_task = asyncio.ensure_future(channel.put("b"))
    await asyncio.sleep(0.01)
    assert not put_task.done(), "通道滿了就該阻塞——這是背壓傳到上游的方式"

    assert await channel.get() == "a"
    await asyncio.wait_for(put_task, timeout=1.0)


async def test_關閉後迭代結束():
    channel = StreamChannel()
    await channel.put(1)
    channel.close()
    got = [x async for x in channel]
    assert got == [1]


async def test_關閉後不能再寫():
    channel = StreamChannel()
    channel.close()
    with pytest.raises(ChannelClosed):
        await channel.put(1)


async def test_fail_把例外傳給消費端():
    channel = StreamChannel()
    channel.fail(RuntimeError("上游爆了"))
    with pytest.raises(RuntimeError, match="上游爆了"):
        await channel.get()


async def test_取消不必等有東西才生效():
    """barge-in 不能卡在「等 TTS 吐出下一段」上。"""
    token = CancellationToken()
    channel = StreamChannel(maxsize=2, token=token)

    get_task = asyncio.ensure_future(channel.get())
    await asyncio.sleep(0.01)
    assert not get_task.done()

    token.cancel(CancelReason.BARGE_IN)
    with pytest.raises(CancelledError):
        await asyncio.wait_for(get_task, timeout=1.0)


async def test_已取消時_get_立刻拋():
    token = CancellationToken()
    token.cancel(CancelReason.BARGE_IN)
    channel = StreamChannel(token=token)
    with pytest.raises(CancelledError):
        await channel.get()


async def test_取消時_put_也拋():
    token = CancellationToken()
    channel = StreamChannel(token=token)
    token.cancel(CancelReason.BARGE_IN)
    with pytest.raises(CancelledError):
        await channel.put(1)


async def test_從其他執行緒寫入():
    """Phase 1 的 IndexTTS2 是同步 push callback，跑在 executor 執行緒裡。"""
    loop = asyncio.get_running_loop()
    channel = StreamChannel(maxsize=4)

    def producer():
        for i in range(3):
            channel.put_threadsafe(i, loop)
        channel.close_threadsafe(loop)

    await loop.run_in_executor(None, producer)
    assert [x async for x in channel] == [0, 1, 2]


async def test_執行緒寫入時滿了會阻塞推理執行緒():
    """背壓要能傳到 GPU，否則模型會一路算完，barge-in 省不到東西。"""
    loop = asyncio.get_running_loop()
    channel = StreamChannel(maxsize=1)
    progress: list[int] = []

    def producer():
        for i in range(3):
            channel.put_threadsafe(i, loop)
            progress.append(i)
        channel.close_threadsafe(loop)

    task = asyncio.ensure_future(loop.run_in_executor(None, producer))
    await asyncio.sleep(0.05)
    assert len(progress) <= 2, "滿了之後生產端應該被擋住"

    got = [x async for x in channel]
    await task
    assert got == [0, 1, 2]
