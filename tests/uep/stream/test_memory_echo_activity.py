"""EchoMemoryAdapter.profile 的常玩遊戲行（假 engine，不碰真資料）。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from uep.stream.adapters.memory_echo import EchoMemoryAdapter, _format_activity_line

from .test_memory_echo import _StoreEngine

_LINE = "常玩：Deadlock（最近 10/27）、Minecraft（最近 10/20）"


def _rows():
    return [
        {"name": "Deadlock", "play_count": 5, "total_minutes": 300.0,
         "last_played": "2026-10-27T20:00:00+00:00"},
        {"name": "Minecraft", "play_count": 2, "total_minutes": 90.0,
         "last_played": datetime(2026, 10, 20, 9, 0)},
    ]


def _adapter(profile_text="喜歡貓", rows=None):
    engine = _StoreEngine()
    engine.get_profile_calls = []
    if profile_text is not None:

        def get_profile(max_chars=800):
            engine.get_profile_calls.append(max_chars)
            return profile_text[:max_chars]

        engine.get_profile = get_profile
    if rows is not None:
        engine.activity_profile = lambda top_n=3: rows[:top_n]
    return engine, EchoMemoryAdapter(engine=engine)


def test_format_有資料():
    assert _format_activity_line(_rows()) == _LINE


def test_format_空資料回空字串():
    assert _format_activity_line([]) == ""
    assert _format_activity_line(None) == ""


@pytest.mark.parametrize(
    "value, expected",
    [
        (datetime(2026, 10, 27), "X（最近 10/27）"),
        (datetime(2026, 10, 27, tzinfo=timezone.utc), "X（最近 10/27）"),
        ("2026-10-27", "X（最近 10/27）"),
        ("2026-10-27T20:00:00Z", "X（最近 10/27）"),
        (None, "X"),
        ("", "X"),
        ("不是日期", "X"),
        (12345, "X"),
    ],
)
def test_format_last_played各型別(value, expected):
    assert _format_activity_line([{"name": "X", "last_played": value}]) == f"常玩：{expected}"


def test_format_略過無名稱與非dict項目():
    assert _format_activity_line([{"name": ""}, "oops", {"name": "A"}]) == "常玩：A"


async def test_profile_有資料放在開頭(monkeypatch):
    monkeypatch.setenv("ECHO_STREAM_MEMORY_ACTIVITY_TOP_N", "3")
    _, adapter = _adapter(rows=_rows())
    assert await adapter.profile() == f"{_LINE}\n喜歡貓"


async def test_profile_top_n限制筆數(monkeypatch):
    monkeypatch.setenv("ECHO_STREAM_MEMORY_ACTIVITY_TOP_N", "1")
    _, adapter = _adapter(rows=_rows())
    assert (await adapter.profile()).startswith("常玩：Deadlock（最近 10/27）\n")


async def test_profile_無遊玩資料原樣回傳():
    _, adapter = _adapter(rows=[])
    assert await adapter.profile() == "喜歡貓"
    _, adapter = _adapter(profile_text="", rows=[])
    assert await adapter.profile() == ""


async def test_profile_引擎無activity_profile原樣回傳():
    _, adapter = _adapter()
    assert await adapter.profile() == "喜歡貓"


async def test_profile_activity失敗只降級():
    engine, adapter = _adapter()

    def boom(top_n=3):
        raise RuntimeError("壞了")

    engine.activity_profile = boom
    assert await adapter.profile() == "喜歡貓"


async def test_profile_top_n為0停用(monkeypatch):
    monkeypatch.setenv("ECHO_STREAM_MEMORY_ACTIVITY_TOP_N", "0")
    _, adapter = _adapter(rows=_rows())
    assert await adapter.profile() == "喜歡貓"


async def test_profile_無get_profile時只回activity行():
    _, adapter = _adapter(profile_text=None, rows=_rows())
    assert await adapter.profile() == _LINE


async def test_profile_總長不超過上限且activity優先(monkeypatch):
    limit = len(_LINE) + 10
    monkeypatch.setenv("ECHO_STREAM_MEMORY_PROFILE_MAX_CHARS", str(limit))
    engine, adapter = _adapter(profile_text="長" * 500, rows=_rows())
    out = await adapter.profile()
    assert len(out) == limit
    assert out.startswith(_LINE)
    assert engine.get_profile_calls == [limit - len(_LINE) - 1]


async def test_profile_activity行過長時截斷到上限(monkeypatch):
    monkeypatch.setenv("ECHO_STREAM_MEMORY_PROFILE_MAX_CHARS", "10")
    engine, adapter = _adapter(profile_text="長" * 500, rows=_rows())
    out = await adapter.profile()
    assert len(out) == 10
    assert engine.get_profile_calls == []
