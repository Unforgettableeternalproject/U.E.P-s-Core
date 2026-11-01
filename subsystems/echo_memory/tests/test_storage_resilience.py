"""M-2：Windows 檔案替換重試、唯一暫存檔、_save 失敗拋出。"""

import os
import threading
from pathlib import Path

import pytest

from echo_memory.storage import atomic
from echo_memory.storage.atomic import atomic_write_json, replace_with_retry
from echo_memory.storage.entity import EntityStore
from echo_memory.storage.semantic import SemanticMemory


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(atomic.time, "sleep", lambda s: None)


def _flaky_replace(fail_times: int):
    real = os.replace
    state = {"calls": 0}

    def fake(src, dst):
        state["calls"] += 1
        if state["calls"] <= fail_times:
            raise PermissionError(5, "拒絕存取")
        return real(src, dst)

    return fake, state


def test_replace_retries_then_succeeds(tmp_path, monkeypatch):
    src, dst = tmp_path / "a.tmp", tmp_path / "a.json"
    src.write_text("x", encoding="utf-8")
    fake, state = _flaky_replace(3)
    monkeypatch.setattr(atomic.os, "replace", fake)
    replace_with_retry(src, dst, retries=6)
    assert dst.read_text(encoding="utf-8") == "x"
    assert state["calls"] == 4


def test_replace_persistent_failure_raises(tmp_path, monkeypatch):
    src, dst = tmp_path / "a.tmp", tmp_path / "a.json"
    src.write_text("x", encoding="utf-8")
    fake, state = _flaky_replace(10**6)
    monkeypatch.setattr(atomic.os, "replace", fake)
    with pytest.raises(PermissionError):
        replace_with_retry(src, dst, retries=4)
    assert state["calls"] == 4


def test_atomic_write_cleans_tmp_on_failure(tmp_path, monkeypatch):
    fake, _ = _flaky_replace(10**6)
    monkeypatch.setattr(atomic.os, "replace", fake)
    with pytest.raises(PermissionError):
        atomic_write_json(tmp_path / "d.json", {"k": "中文"})
    assert not list(tmp_path.glob("*.tmp"))
    assert not (tmp_path / "d.json").exists()


def test_semantic_save_raises_on_persistent_failure(tmp_path, monkeypatch):
    sem = SemanticMemory(storage_path=tmp_path / "sem")
    fake, _ = _flaky_replace(10**6)
    monkeypatch.setattr(atomic.os, "replace", fake)
    with pytest.raises(PermissionError):
        sem._save()
    assert not list((tmp_path / "sem").glob("*.tmp"))


def test_entity_save_raises_and_recovers_with_retry(tmp_path, monkeypatch):
    store = EntityStore(tmp_path / "ent")
    fake, _ = _flaky_replace(2)
    monkeypatch.setattr(atomic.os, "replace", fake)
    store._save()  # 前 2 次被拒，重試後成功
    assert (tmp_path / "ent" / "entities.json").exists()

    fake2, _ = _flaky_replace(10**6)
    monkeypatch.setattr(atomic.os, "replace", fake2)
    with pytest.raises(PermissionError):
        store._save()


def test_concurrent_writes_do_not_collide_on_tmp_name(tmp_path):
    target = tmp_path / "shared.json"
    seen_tmp: list[Path] = []
    real_open = open
    lock = threading.Lock()

    def spy_open(file, *a, **k):
        if str(file).endswith(".tmp"):
            with lock:
                seen_tmp.append(Path(file))
        return real_open(file, *a, **k)

    errors: list[BaseException] = []

    def work(i):
        try:
            for _ in range(20):
                atomic_write_json(target, {"writer": i})
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    import builtins
    orig = builtins.open
    builtins.open = spy_open
    try:
        threads = [threading.Thread(target=work, args=(i,)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        builtins.open = orig

    assert not errors
    assert len(seen_tmp) == len(set(seen_tmp)) == 6 * 20
    assert not list(tmp_path.glob("*.tmp"))
    assert target.exists()
