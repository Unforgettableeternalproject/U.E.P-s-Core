"""M7 驗收共用 fixtures：關閉 Dream 自動排程、提供 engine／sim 工廠。"""

from __future__ import annotations

import pytest

from echo_memory.config import get_config
from echo_memory.engine import MemoryEngine
from tests.m7.sim import EchoStreamSim


@pytest.fixture(autouse=True)
def _no_dream_scheduler(monkeypatch):
    cfg = get_config()
    monkeypatch.setattr(cfg, "dream_enable_idle", False)
    monkeypatch.setattr(cfg, "dream_enable_cron", False)


@pytest.fixture
def make_sim(tmp_path):
    """make_sim(llm=None, namespace_id="echo-stream") -> (EchoStreamSim, MemoryEngine)。"""
    sims: list[EchoStreamSim] = []

    def _make(llm=None, namespace_id: str = "echo-stream", subdir: str = "data"):
        engine = MemoryEngine(
            storage_path=tmp_path / subdir, namespace_id=namespace_id, llm_fn=llm,
        )
        engine.ensure_initialized()
        sim = EchoStreamSim(engine)
        sims.append(sim)
        return sim, engine

    yield _make
    for s in sims:
        s.close()
