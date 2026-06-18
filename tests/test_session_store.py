"""
SessionStore 測試

驗證：
- 儲存/載入 session（JSON 持久化）
- 列出歷史 session
- 刪除 session
- 模糊搜尋（前綴/子字串匹配）
- 索引一致性
"""

from __future__ import annotations

import json
import time

import pytest

from echo_thought_core.session_store import SessionStore


@pytest.fixture
def store(tmp_path):
    """建立臨時目錄的 SessionStore"""
    return SessionStore(data_dir=tmp_path)


@pytest.fixture
def sample_messages():
    """測試用訊息"""
    return [
        {"role": "user", "content": "Hello!"},
        {"role": "assistant", "content": "Hi there!"},
        {"role": "user", "content": "How are you?"},
        {"role": "assistant", "content": "I'm doing well, thanks!"},
    ]


class TestSessionStoreSaveLoad:
    """儲存/載入基本功能"""

    def test_save_and_load(self, store, sample_messages):
        """存然後讀"""
        store.save("test-001", sample_messages, {"total_tokens": 42})
        msgs, meta = store.load("test-001")
        assert len(msgs) == 4
        assert msgs[0]["content"] == "Hello!"
        assert meta["total_tokens"] == 42

    def test_save_creates_file(self, store, tmp_path, sample_messages):
        """儲存後檔案存在"""
        store.save("test-002", sample_messages)
        session_file = tmp_path / "sessions" / "test-002.json"
        assert session_file.exists()

    def test_save_updates_index(self, store, sample_messages):
        """儲存後索引更新"""
        store.save("test-003", sample_messages)
        assert store.exists("test-003")

    def test_load_nonexistent(self, store):
        """載入不存在的 session"""
        with pytest.raises(FileNotFoundError):
            store.load("nonexistent")

    def test_overwrite(self, store):
        """重複儲存覆蓋"""
        store.save("test-004", [{"role": "user", "content": "v1"}])
        store.save("test-004", [{"role": "user", "content": "v2"}])
        msgs, _ = store.load("test-004")
        assert len(msgs) == 1
        assert msgs[0]["content"] == "v2"

    def test_metadata_preserved(self, store, sample_messages):
        """元資料完整保留"""
        meta = {
            "total_tokens": 500,
            "compression_count": 3,
            "model": "gemini-2.5-flash-lite",
            "backend": "gemini",
            "created_at": 1718700000.0,
        }
        store.save("test-005", sample_messages, meta)
        _, loaded_meta = store.load("test-005")
        assert loaded_meta["total_tokens"] == 500
        assert loaded_meta["compression_count"] == 3
        assert loaded_meta["model"] == "gemini-2.5-flash-lite"

    def test_empty_messages(self, store):
        """空訊息列表"""
        store.save("empty-001", [])
        msgs, _ = store.load("empty-001")
        assert msgs == []


class TestSessionStoreList:
    """列出和搜尋"""

    def test_list_empty(self, store):
        """空 store"""
        assert store.list_sessions() == []

    def test_list_multiple(self, store):
        """多個 session 按時間排序"""
        store.save("s1", [{"role": "user", "content": "a"}], {"total_tokens": 10})
        time.sleep(0.01)
        store.save("s2", [{"role": "user", "content": "b"}], {"total_tokens": 20})
        time.sleep(0.01)
        store.save("s3", [{"role": "user", "content": "c"}], {"total_tokens": 30})

        sessions = store.list_sessions()
        assert len(sessions) == 3
        # 最新的在前
        assert sessions[0]["session_id"] == "s3"
        assert sessions[2]["session_id"] == "s1"

    def test_list_shows_metadata(self, store):
        """列表包含元資料"""
        store.save("s1", [{"role": "user", "content": "x"}] * 5, {
            "total_tokens": 100,
            "model": "gemma4",
        })
        sessions = store.list_sessions()
        assert sessions[0]["message_count"] == 5
        assert sessions[0]["total_tokens"] == 100
        assert sessions[0]["model"] == "gemma4"


class TestSessionStoreFind:
    """模糊搜尋"""

    def test_find_exact(self, store):
        """精確匹配"""
        store.save("abc-123", [{"role": "user", "content": "x"}])
        matches = store.find("abc-123")
        assert len(matches) == 1

    def test_find_prefix(self, store):
        """前綴匹配"""
        store.save("abc-001", [{"role": "user", "content": "x"}])
        store.save("abc-002", [{"role": "user", "content": "y"}])
        store.save("xyz-001", [{"role": "user", "content": "z"}])
        matches = store.find("abc")
        assert len(matches) == 2

    def test_find_substring(self, store):
        """子字串匹配"""
        store.save("session-abc-v1", [{"role": "user", "content": "x"}])
        matches = store.find("abc")
        assert len(matches) == 1

    def test_find_case_insensitive(self, store):
        """大小寫不敏感"""
        store.save("ABC-123", [{"role": "user", "content": "x"}])
        matches = store.find("abc")
        assert len(matches) == 1

    def test_find_no_match(self, store):
        """無匹配"""
        store.save("abc-123", [{"role": "user", "content": "x"}])
        matches = store.find("xyz")
        assert len(matches) == 0


class TestSessionStoreDelete:
    """刪除"""

    def test_delete_existing(self, store):
        """刪除存在的 session"""
        store.save("del-001", [{"role": "user", "content": "x"}])
        assert store.exists("del-001")
        assert store.delete("del-001") is True
        assert not store.exists("del-001")

    def test_delete_nonexistent(self, store):
        """刪除不存在的"""
        assert store.delete("nonexistent") is False

    def test_delete_removes_file(self, store, tmp_path):
        """刪除後檔案消失"""
        store.save("del-002", [{"role": "user", "content": "x"}])
        session_file = tmp_path / "sessions" / "del-002.json"
        assert session_file.exists()
        store.delete("del-002")
        assert not session_file.exists()


class TestSessionStorePersistence:
    """跨實例持久化"""

    def test_reload_index(self, tmp_path):
        """重新實例化後索引恢復"""
        store1 = SessionStore(data_dir=tmp_path)
        store1.save("persist-001", [{"role": "user", "content": "x"}])
        store1.save("persist-002", [{"role": "user", "content": "y"}])

        # 重新實例化
        store2 = SessionStore(data_dir=tmp_path)
        assert store2.exists("persist-001")
        assert store2.exists("persist-002")
        assert len(store2.list_sessions()) == 2

    def test_reload_data(self, tmp_path):
        """重新實例化後資料可讀"""
        store1 = SessionStore(data_dir=tmp_path)
        store1.save("persist-003", [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "World"},
        ])

        store2 = SessionStore(data_dir=tmp_path)
        msgs, _ = store2.load("persist-003")
        assert len(msgs) == 2
        assert msgs[1]["content"] == "World"
