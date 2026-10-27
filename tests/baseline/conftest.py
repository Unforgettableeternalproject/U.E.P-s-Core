"""基線測試 fixtures（EM-001）。

- restored_dir：把固定快照還原到 tmp 目錄（不碰快照本身、不碰真實 data/）
- restored_engine / engine_factory：由還原目錄建出 MemoryEngine
- frozen_retrieval_clock：凍結 ecphory 的 time.time()，讓排名不隨時間飄移
- 自動停用 Dream 的 idle / cron 排程（避免測試中啟動 APScheduler 執行緒）

父層 tests/conftest.py 的 autouse embedding mock 會自動套用。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.baseline._support import FROZEN_NOW, SNAPSHOT_DIR, restore_snapshot


@pytest.fixture(autouse=True)
def _no_dream_scheduler(monkeypatch):
    """關閉 Dream 自動排程；config 為模組層級單例，直接 patch 屬性。"""
    from echo_memory.config import get_config

    cfg = get_config()
    monkeypatch.setattr(cfg, "dream_enable_idle", False)
    monkeypatch.setattr(cfg, "dream_enable_cron", False)


@pytest.fixture
def frozen_retrieval_clock(monkeypatch):
    """凍結 ecphory fusion 使用的 time.time()（只影響該模組的引用）。"""
    from echo_memory.retrieval import ecphory

    monkeypatch.setattr(ecphory, "time", SimpleNamespace(time=lambda: FROZEN_NOW))
    return FROZEN_NOW


@pytest.fixture
def snapshot_dir() -> Path:
    return SNAPSHOT_DIR


@pytest.fixture
def restored_dir(tmp_path: Path) -> Path:
    """還原後的資料目錄（可安全寫入）。"""
    return restore_snapshot(SNAPSHOT_DIR, tmp_path / "restored_data")


@pytest.fixture
def engine_factory(tmp_path: Path):
    """每次呼叫在新的 tmp 目錄還原快照並回傳 MemoryEngine。"""
    from echo_memory.engine import MemoryEngine

    count = {"n": 0}

    def _make(**kwargs):
        count["n"] += 1
        dest = restore_snapshot(SNAPSHOT_DIR, tmp_path / f"engine_data_{count['n']}")
        return MemoryEngine(storage_path=dest, **kwargs)

    return _make


@pytest.fixture
def restored_engine(engine_factory):
    return engine_factory()


@pytest.fixture
def empty_engine(tmp_path: Path):
    """空白 engine（不載入快照），給需要乾淨環境的寫入路徑測試。"""
    from echo_memory.engine import MemoryEngine

    return MemoryEngine(storage_path=tmp_path / "empty_data")
