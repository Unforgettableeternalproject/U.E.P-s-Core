"""sentence-transformers 模型行程共用：多個 EpisodicMemory 只載一次、並行首次呼叫只載一次。"""
from __future__ import annotations

import sys
import threading
import time
import types

import numpy as np
import pytest

from echo_memory.storage import episodic as episodic_mod
from echo_memory.storage.episodic import EpisodicMemory


@pytest.fixture
def counting_st(monkeypatch):
    created: list[tuple[str, str]] = []

    class FakeST:
        def __init__(self, model, device=None):
            time.sleep(0.05)  # 拉長載入窗口，讓並行呼叫有機會重疊
            created.append((model, device))

        def encode(self, text, convert_to_numpy=True):
            return np.ones(8, dtype=np.float32)

    module = types.ModuleType("sentence_transformers")
    module.SentenceTransformer = FakeST
    monkeypatch.setitem(sys.modules, "sentence_transformers", module)
    episodic_mod._clear_shared_embedders()
    yield created
    episodic_mod._clear_shared_embedders()


def _make(tmp_path, name):
    return EpisodicMemory(
        storage_path=tmp_path / name, embedding_model="m", device="cpu", embedding_backend="sentence-transformers",
    )


def test_多個實例只建一次(counting_st, tmp_path):
    mems = [_make(tmp_path, f"ns{i}") for i in range(3)]
    for mem in mems:
        assert len(mem._generate_embedding_st("你好")) == 8
    assert counting_st == [("m", "cpu")]


def test_並行首次呼叫只載一次(counting_st, tmp_path):
    mems = [_make(tmp_path, f"ns{i}") for i in range(6)]
    barrier = threading.Barrier(len(mems))

    def run(mem):
        barrier.wait()
        mem._generate_embedding_st("x")

    threads = [threading.Thread(target=run, args=(m,)) for m in mems]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert counting_st == [("m", "cpu")]


def test_不同_device_各載一份且模組級_warmup_預載(counting_st):
    episodic_mod.warmup("m", "cpu")
    episodic_mod.warmup("m", "cpu")
    episodic_mod.warmup("m", "cuda")
    assert counting_st == [("m", "cpu"), ("m", "cuda")]
