"""
WorkingMemory 查詢快取測試 — Phase 2 Branch 6 驗收

驗收標準：
  1. 連續相同查詢第二次跳過 embedding 生成
  2. 不同查詢不命中快取
  3. 快取過期後重新生成
  4. working=None 時行為不變（向後相容）
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest

from echo_memory.retrieval.ecphory import EcphoryRetrieval
from echo_memory.storage.episodic import EpisodicMemory
from echo_memory.storage.semantic import SemanticMemory
from echo_memory.storage.working import WorkingMemory


MOCK_DIM = 512


def _mock_embed_fn(text: str) -> list[float]:
    rng = np.random.RandomState(hash(text) & 0xFFFFFFFF)
    vec = rng.randn(MOCK_DIM).astype(np.float32)
    norm = float(np.linalg.norm(vec))
    if norm > 0:
        vec /= norm
    return vec.tolist()


@pytest.fixture
def storage(tmp_path: Path):
    ep_dir = tmp_path / "ep"
    sem_dir = tmp_path / "sem"
    ep_dir.mkdir()
    sem_dir.mkdir()

    episodic = EpisodicMemory(storage_path=ep_dir)
    semantic = SemanticMemory(storage_path=sem_dir, embed_fn=_mock_embed_fn)
    working = WorkingMemory(ttl_seconds=60)

    return episodic, semantic, working


class TestQueryEmbeddingCache:
    """查詢向量快取核心功能。"""

    def test_second_query_uses_cache(self, storage, monkeypatch):
        """相同查詢第二次應命中快取，跳過 embedding。"""
        episodic, semantic, working = storage
        retrieval = EcphoryRetrieval(
            episodic=episodic, semantic=semantic, working=working,
        )

        call_count = 0
        original_fn = episodic.generate_query_embedding

        def _counting_embed(query):
            nonlocal call_count
            call_count += 1
            return original_fn(query)

        monkeypatch.setattr(episodic, "generate_query_embedding", _counting_embed)

        # 第一次查詢：生成 embedding
        retrieval.retrieve("壽司", min_similarity=0.0)
        assert call_count == 1

        # 第二次相同查詢：應命中快取
        retrieval.retrieve("壽司", min_similarity=0.0)
        assert call_count == 1  # 沒有再呼叫

    def test_different_query_misses_cache(self, storage, monkeypatch):
        """不同查詢不命中快取。"""
        episodic, semantic, working = storage
        retrieval = EcphoryRetrieval(
            episodic=episodic, semantic=semantic, working=working,
        )

        call_count = 0
        original_fn = episodic.generate_query_embedding

        def _counting_embed(query):
            nonlocal call_count
            call_count += 1
            return original_fn(query)

        monkeypatch.setattr(episodic, "generate_query_embedding", _counting_embed)

        retrieval.retrieve("壽司", min_similarity=0.0)
        assert call_count == 1

        retrieval.retrieve("拉麵", min_similarity=0.0)
        assert call_count == 2  # 不同查詢，重新生成

    def test_cache_expired_regenerates(self, storage, monkeypatch):
        """快取過期後重新生成。"""
        episodic, semantic, _ = storage
        # 極短 TTL
        working = WorkingMemory(ttl_seconds=0)
        retrieval = EcphoryRetrieval(
            episodic=episodic, semantic=semantic, working=working,
        )

        call_count = 0
        original_fn = episodic.generate_query_embedding

        def _counting_embed(query):
            nonlocal call_count
            call_count += 1
            return original_fn(query)

        monkeypatch.setattr(episodic, "generate_query_embedding", _counting_embed)

        retrieval.retrieve("壽司", min_similarity=0.0)
        assert call_count == 1

        # TTL=0 → 立即過期
        retrieval.retrieve("壽司", min_similarity=0.0)
        assert call_count == 2  # 快取過期，重新生成


class TestCacheBackwardCompatibility:
    """向後相容：working=None 時不影響行為。"""

    def test_no_working_memory_still_works(self, storage, monkeypatch):
        """working=None 時正常運作。"""
        episodic, semantic, _ = storage
        retrieval = EcphoryRetrieval(
            episodic=episodic, semantic=semantic, working=None,
        )

        call_count = 0
        original_fn = episodic.generate_query_embedding

        def _counting_embed(query):
            nonlocal call_count
            call_count += 1
            return original_fn(query)

        monkeypatch.setattr(episodic, "generate_query_embedding", _counting_embed)

        retrieval.retrieve("壽司", min_similarity=0.0)
        retrieval.retrieve("壽司", min_similarity=0.0)
        assert call_count == 2  # 每次都重新生成（無快取）

    def test_cache_does_not_affect_results(self, storage):
        """快取和非快取的結果應一致。"""
        episodic, semantic, working = storage
        retrieval_cached = EcphoryRetrieval(
            episodic=episodic, semantic=semantic, working=working,
        )
        retrieval_uncached = EcphoryRetrieval(
            episodic=episodic, semantic=semantic, working=None,
        )

        ctx1 = retrieval_cached.retrieve("壽司", min_similarity=0.0)
        ctx2 = retrieval_uncached.retrieve("壽司", min_similarity=0.0)

        assert len(ctx1.episodes) == len(ctx2.episodes)
