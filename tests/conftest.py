"""
tests/conftest.py - 全域測試配置與 fixtures

由 pytest 自動載入，提供所有測試模組共用的 fixtures。
CI 環境下避免下載 Hugging Face 模型或呼叫外部 API。
所有需要嵌入生成的測試都透過 autouse fixture 自動 patch。
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from typing import Any

import numpy as np
import pytest

_MOCK_DIM = 512  # 匹配 BAAI/bge-small-zh-v1.5 輸出維度


def _deterministic_embedding(text: str) -> list[float]:
    """根據文字 hash 產生確定性的 512 維 L2-normalized 向量。"""
    import hashlib
    seed = int.from_bytes(hashlib.sha256(text.encode()).digest()[:4], "little")
    rng = np.random.RandomState(seed)
    vec = rng.randn(_MOCK_DIM).astype(np.float32)
    norm = float(np.linalg.norm(vec))
    if norm > 0:
        vec /= norm
    return vec.tolist()


# ─── CI Mock Fixtures ──────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _disable_embedding_warmup(monkeypatch):
    """測試中關閉 MemoryEngine 的背景 embedding 暖機（避免背景執行緒干擾計數 / 連外部後端）。"""
    monkeypatch.setenv("ECHO_DISABLE_EMBEDDING_WARMUP", "1")


@pytest.fixture(autouse=True)
def mock_embedding_model(monkeypatch):
    """
    自動 patch EpisodicMemory._generate_embedding 和 generate_query_embedding，
    避免 CI 環境下載 HF 模型。

    直接 patch 方法層級（而非 SentenceTransformer），更可靠且不依賴 import 順序。

    回傳確定性的 512 維向量（匹配 BAAI/bge-small-zh-v1.5 的輸出維度），
    基於文字 hash 產生不同向量。

    若測試本身已手動 monkeypatch _generate_embedding（例如
    test_dream_engine.py、test_dream_quick.py），那個 patch 優先。
    """
    monkeypatch.setattr(
        "echo_memory.storage.episodic.EpisodicMemory._generate_embedding",
        lambda self, text: _deterministic_embedding(text),
    )

    # generate_query_embedding 透過 self._generate_embedding 呼叫，
    # 確保 instance-level patch（如 benchmark test）能正確攔截 query 向量維度
    def _mock_query_embedding(self, text):
        prefixed_text = self.query_prefix + text if self.query_prefix else text
        return self._generate_embedding(prefixed_text)

    monkeypatch.setattr(
        "echo_memory.storage.episodic.EpisodicMemory.generate_query_embedding",
        _mock_query_embedding,
    )

    # 仍保留假的 sentence_transformers 模組，防止任何地方意外 import 時出錯
    if "sentence_transformers" not in sys.modules:
        class _DummyST:
            def __init__(self, *a, **kw):
                pass
            def encode(self, texts, **kw):
                if isinstance(texts, str):
                    return np.array(_deterministic_embedding(texts), dtype=np.float32)
                return np.array([_deterministic_embedding(t) for t in texts], dtype=np.float32)

        fake_module = types.ModuleType("sentence_transformers")
        fake_module.SentenceTransformer = _DummyST
        sys.modules["sentence_transformers"] = fake_module


# ─── 品質基準資料集 Fixtures ──────────────────────────────────────────────────

_FIXTURES_DIR = Path(__file__).parent / "fixtures"
_DATASET_PATH = _FIXTURES_DIR / "benchmark_dataset.json"


@pytest.fixture(scope="session")
def benchmark_dataset() -> dict[str, Any]:
    """
    載入 benchmark_dataset.json。

    Scope: session（整個測試執行期間只載入一次）。
    若資料集不存在，測試將以明確錯誤訊息失敗。
    """
    if not _DATASET_PATH.exists():
        pytest.fail(
            f"找不到品質基準資料集：{_DATASET_PATH}\n"
            "請確認 tests/fixtures/benchmark_dataset.json 已存在。"
        )
    with open(_DATASET_PATH, encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture(scope="session")
def episodes_by_id(benchmark_dataset: dict[str, Any]) -> dict[str, dict]:
    """
    建立 episode_id -> episode dict 的快速查詢表。

    Scope: session（與 benchmark_dataset 相同生命週期）。
    """
    return {ep["id"]: ep for ep in benchmark_dataset.get("episodes", [])}
