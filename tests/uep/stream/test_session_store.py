"""session_store adapter 的整合測試。

需要 TestSeparateSessionControl repo 在同層目錄（或 .env 有設定）——
沒有就跳過，不讓跨 repo 依賴弄紅單元測試。
"""

from __future__ import annotations

import pytest


def _build_store_or_skip(tmp_path):
    from uep.stream.adapters.session_store import build_store

    try:
        return build_store(data_dir=tmp_path)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"SessionControl repo 不可用：{exc}")


def test_存取往返(tmp_path):
    store = _build_store_or_skip(tmp_path)
    messages = [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "嗨，有什麼事？"},
    ]
    store.save("abc123", messages, {"title": "你好", "created_at": 1.0})

    loaded, meta = store.load("abc123")
    assert loaded == messages
    assert meta["title"] == "你好"

    listing = store.list_sessions()
    assert any(s["session_id"] == "abc123" for s in listing)

    assert store.delete("abc123")
    assert not store.exists("abc123")


def test_檔案落在指定目錄(tmp_path):
    store = _build_store_or_skip(tmp_path)
    store.save("xyz", [{"role": "user", "content": "hi"}], {})
    assert (tmp_path / "sessions" / "xyz.json").exists()
