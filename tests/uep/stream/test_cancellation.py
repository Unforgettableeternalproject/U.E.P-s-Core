"""取消路徑的測試。

barge-in 是 Phase 0 最不能事後補的東西，所以測試密度最高。
"""

from __future__ import annotations

import asyncio

import pytest

from uep.stream.contracts.cancellation import (
    CancellationToken,
    CancelledError,
    CancelReason,
)


def test_初始狀態未取消():
    token = CancellationToken()
    assert not token.is_cancelled
    assert token.reason is None
    token.raise_if_cancelled()  # 不該拋


def test_取消後帶原因():
    token = CancellationToken()
    token.cancel(CancelReason.BARGE_IN, "使用者開口")
    assert token.is_cancelled
    assert token.reason is CancelReason.BARGE_IN
    assert token.detail == "使用者開口"


def test_取消是冪等的_只有第一次原因生效():
    token = CancellationToken()
    token.cancel(CancelReason.BARGE_IN)
    token.cancel(CancelReason.TIMEOUT)
    assert token.reason is CancelReason.BARGE_IN


def test_raise_if_cancelled_拋出帶原因的例外():
    token = CancellationToken()
    token.cancel(CancelReason.TIMEOUT)
    with pytest.raises(CancelledError) as exc:
        token.raise_if_cancelled()
    assert exc.value.reason is CancelReason.TIMEOUT


def test_取消例外不是_asyncio_CancelledError():
    """刻意不繼承 asyncio.CancelledError（BaseException），
    否則會穿透 stage 的 except Exception，讓善後邏輯做不了。"""
    assert not issubclass(CancelledError, asyncio.CancelledError)
    assert issubclass(CancelledError, Exception)


def test_callback_在取消時觸發():
    token = CancellationToken()
    seen: list[CancelReason] = []
    token.on_cancel(seen.append)
    token.cancel(CancelReason.BARGE_IN)
    assert seen == [CancelReason.BARGE_IN]


def test_已取消時註冊_callback_立即執行():
    """避免競態漏接——sink.stop 註冊得比取消晚，還是必須停。"""
    token = CancellationToken()
    token.cancel(CancelReason.SHUTDOWN)
    seen: list[CancelReason] = []
    token.on_cancel(seen.append)
    assert seen == [CancelReason.SHUTDOWN]


def test_壞掉的_callback_不擋住其他人():
    token = CancellationToken()
    seen: list[str] = []

    def boom(_reason):
        raise RuntimeError("這個訂閱者壞了")

    token.on_cancel(boom)
    token.on_cancel(lambda _r: seen.append("ok"))
    token.cancel()
    assert seen == ["ok"]


def test_父取消傳播給子():
    parent = CancellationToken()
    child = parent.child()
    parent.cancel(CancelReason.BARGE_IN)
    assert child.is_cancelled
    assert child.reason is CancelReason.BARGE_IN


def test_子取消不往上冒():
    """Memory retrieve 逾時不該連帶中止整個 turn。"""
    parent = CancellationToken()
    child = parent.child()
    child.cancel(CancelReason.TIMEOUT)
    assert child.is_cancelled
    assert not parent.is_cancelled


def test_父已取消時建立的子_立即繼承():
    parent = CancellationToken()
    parent.cancel(CancelReason.SHUTDOWN)
    child = parent.child()
    assert child.is_cancelled


async def test_wait_等到取消為止():
    token = CancellationToken()

    async def cancel_soon():
        await asyncio.sleep(0.01)
        token.cancel(CancelReason.BARGE_IN)

    asyncio.ensure_future(cancel_soon())
    reason = await asyncio.wait_for(token.wait(), timeout=1.0)
    assert reason is CancelReason.BARGE_IN
