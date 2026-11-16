"""SearchChain 的 INFO log：查詢（截斷）、依序嘗試的 provider 與結果、採用哪一個、耗時（用替身 provider）。"""
from __future__ import annotations

import logging

import pytest

from uep.tools.executors.web_search import (
    FakeSearchProvider,
    ProviderError,
    SearchChain,
    SearchChainError,
    SearchHit,
)

LOGGER = "uep.tools.executors.web_search"


def _hit(provider: str) -> SearchHit:
    return SearchHit("標題", f"https://{provider}.test/a", "摘要", provider)


def _messages(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == LOGGER and r.levelno == logging.INFO]


def test_依序嘗試後採用成功者並記錄各自結果(caplog):
    chain = SearchChain([
        FakeSearchProvider("ddg", error=ProviderError("blocked")),
        FakeSearchProvider("wiki", []),
        FakeSearchProvider("searx", [_hit("searx"), _hit("searx2")]),
    ])
    with caplog.at_level(logging.INFO, logger=LOGGER):
        chain.run("今天台北天氣", limit=5)
    (line,) = _messages(caplog)
    assert "今天台北天氣" in line
    assert line.index("ddg:blocked") < line.index("wiki:empty") < line.index("searx:ok(2 筆")
    assert "採用=searx（備援=否，2 筆）" in line and "耗時=" in line


def test_全部失敗也記一行(caplog):
    chain = SearchChain(
        [FakeSearchProvider("a", error=ProviderError("network"))],
        fallback=FakeSearchProvider("o", error=ProviderError("timeout")),
    )
    with caplog.at_level(logging.INFO, logger=LOGGER), pytest.raises(SearchChainError):
        chain.run("q", limit=3)
    (line,) = _messages(caplog)
    assert "a:network" in line and "o:timeout" in line and "採用=無（全部失敗）" in line


def test_備援採用時標示備援與查詢截斷(caplog):
    chain = SearchChain(
        [FakeSearchProvider("a", error=ProviderError("blocked"))], fallback=FakeSearchProvider("o", [_hit("o")]),
    )
    with caplog.at_level(logging.INFO, logger=LOGGER):
        chain.run("長" * 200, limit=3)
    (line,) = _messages(caplog)
    assert "採用=o（備援=是，1 筆）" in line and "長" * 81 not in line and "…" in line


def test_log不改變回傳與attempts形狀():
    chain = SearchChain([FakeSearchProvider("a", error=ProviderError("blocked")), FakeSearchProvider("b", [_hit("b")])])
    result = chain.run("q", limit=3)
    assert result.attempts == [{"provider": "a", "outcome": "blocked"}, {"provider": "b", "outcome": "ok"}]
