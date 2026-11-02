"""
寫入冪等鍵測試（M-6a）

驗收重點：同 key 重送只存一筆且回同 id；不同 key 正常；並行同 key 只存一筆；
重啟後仍去重；上限與過期淘汰；損壞檔處理；未傳 key 行為不變。
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from echo_memory import MemoryEngine
from echo_memory.storage.idempotency import INDEX_FILENAME, IdempotencyIndex


class _NoopExtractor:
    calls = 0

    def extract(self, episode):
        type(self).calls += 1
        return []


@pytest.fixture
def engine(tmp_path: Path) -> MemoryEngine:
    eng = MemoryEngine(storage_path=tmp_path)
    eng._entity_extractor = _NoopExtractor()
    _NoopExtractor.calls = 0
    return eng


def _store(eng: MemoryEngine, text: str = "內容", **kw) -> str:
    return eng.store_episode(text, "好", "s1", salience=0.9, **kw)


def test_same_key_stores_once(engine: MemoryEngine):
    first = _store(engine, idempotency_key="k1")
    calls_after_first = _NoopExtractor.calls
    da = engine._neurochem.dopamine
    second = _store(engine, idempotency_key="k1")
    assert second == first
    assert engine.episodic.count() == 1
    assert _NoopExtractor.calls == calls_after_first, "重複請求不應再抽取實體"
    assert engine._neurochem.dopamine == da, "重複請求不應推進神經化學"


def test_different_keys_store_separately(engine: MemoryEngine):
    a = _store(engine, "甲", idempotency_key="a")
    b = _store(engine, "乙", idempotency_key="b")
    assert a != b
    assert engine.episodic.count() == 2


def test_no_key_unchanged(engine: MemoryEngine):
    a = _store(engine)
    b = _store(engine)
    assert a != b
    assert engine.episodic.count() == 2
    assert not (engine.storage_path / INDEX_FILENAME).exists()


def test_invalid_key_rejected(engine: MemoryEngine):
    with pytest.raises(ValueError):
        _store(engine, idempotency_key="  ")


def test_concurrent_same_key_single_episode(engine: MemoryEngine):
    ids: list[str] = []
    barrier = threading.Barrier(2)

    def _run():
        barrier.wait()
        ids.append(_store(engine, idempotency_key="race"))

    threads = [threading.Thread(target=_run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert len(ids) == 2 and ids[0] == ids[1]
    assert engine.episodic.count() == 1


def test_dedupe_survives_restart(tmp_path: Path):
    e1 = MemoryEngine(storage_path=tmp_path)
    first = _store(e1, idempotency_key="persist")
    e2 = MemoryEngine(storage_path=tmp_path)
    assert _store(e2, idempotency_key="persist") == first
    assert e2.episodic.count() == 1


def test_deleted_episode_treated_as_new(engine: MemoryEngine):
    first = _store(engine, idempotency_key="gone")
    assert engine.episodic.delete_episode(first)
    second = _store(engine, idempotency_key="gone")
    assert second != first
    assert engine.episodic.count() == 1
    # 索引已更新：再送一次回新 id
    assert _store(engine, idempotency_key="gone") == second


def test_namespace_isolated(tmp_path: Path):
    a = MemoryEngine(storage_path=tmp_path, namespace_id="ns-a")
    b = MemoryEngine(storage_path=tmp_path, namespace_id="ns-b")
    ia = _store(a, idempotency_key="same")
    ib = _store(b, idempotency_key="same")
    assert ia != ib


# ─── 索引單元：上限、過期、損壞 ────────────────────────────────────────────────


def test_index_max_keys_evicts_oldest(tmp_path: Path):
    idx = IdempotencyIndex(tmp_path, max_keys=3)
    for i in range(5):
        idx.record(f"k{i}", f"ep{i}")
    assert idx.get("k0") is None and idx.get("k1") is None
    assert [idx.get(f"k{i}") for i in (2, 3, 4)] == ["ep2", "ep3", "ep4"]


def test_index_retention_expires(tmp_path: Path):
    idx = IdempotencyIndex(tmp_path, retention_days=30)
    idx.record("old", "ep-old")
    path = tmp_path / INDEX_FILENAME
    data = json.loads(path.read_text(encoding="utf-8"))
    data["entries"]["old"]["at"] = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
    path.write_text(json.dumps(data), encoding="utf-8")
    assert idx.get("old") is None
    idx.record("new", "ep-new")
    assert "old" not in json.loads(path.read_text(encoding="utf-8"))["entries"]


@pytest.mark.parametrize("content", ["{not json", '{"entries": []}', '{"entries": {"k": 1}}'])
def test_index_corrupt_backed_up_and_empty(tmp_path: Path, content: str):
    path = tmp_path / INDEX_FILENAME
    path.write_text(content, encoding="utf-8")
    idx = IdempotencyIndex(tmp_path)
    assert idx.get("k") is None
    assert not path.exists()
    assert len(list(tmp_path.glob(f"{INDEX_FILENAME}.corrupt-*.bak"))) == 1
    idx.record("k", "ep")
    assert idx.get("k") == "ep"


def test_index_oserror_raises_not_treated_as_empty(tmp_path: Path, monkeypatch):
    path = tmp_path / INDEX_FILENAME
    path.write_text('{"version":1,"entries":{}}', encoding="utf-8")
    idx = IdempotencyIndex(tmp_path)
    monkeypatch.setattr("echo_memory.storage.idempotency.READ_RETRY_DELAY", 0)

    def _boom(self, *a, **k):
        raise PermissionError("locked")

    monkeypatch.setattr(Path, "read_text", _boom)
    with pytest.raises(PermissionError):
        idx.get("k")
    monkeypatch.undo()
    assert path.exists()
    assert not list(tmp_path.glob("*.bak"))
