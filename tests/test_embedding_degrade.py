"""M-3：embedding 降級——查詢不崩潰並標記 degraded，寫入仍存 episode。"""

import pytest
import requests

from echo_memory.retrieval.ecphory import EcphoryRetrieval
from echo_memory.schemas.episode import Episode
from echo_memory.storage.episodic import EmbeddingUnavailable, EpisodicMemory
from echo_memory.storage.semantic import SemanticMemory


# conftest 會把 embedding 方法換成假的；這裡還原真實實作以測 Ollama 降級路徑
_REAL = {
    n: getattr(EpisodicMemory, n)
    for n in ("_generate_embedding", "generate_query_embedding")
}


@pytest.fixture(autouse=True)
def _real_embedding_methods(mock_embedding_model, monkeypatch):
    for name, fn in _REAL.items():
        monkeypatch.setattr(EpisodicMemory, name, fn)


def _make(tmp_path):
    return EpisodicMemory(
        storage_path=tmp_path / "ep",
        embedding_model="bge-m3",
        embedding_backend="ollama",
        query_timeout=0.1,
        write_timeout=0.1,
    )


def _ep(summary="測試摘要"):
    return Episode(
        user_input="u", agent_response="a", session_id="s", content_summary=summary
    )


def _down(monkeypatch, exc):
    def fake_post(*a, **k):
        raise exc

    monkeypatch.setattr(requests, "post", fake_post)


@pytest.mark.parametrize(
    "exc", [requests.exceptions.Timeout("t"), requests.exceptions.ConnectionError("c")]
)
def test_recall_degrades_instead_of_crashing(tmp_path, monkeypatch, exc):
    epi = _make(tmp_path)
    sem = SemanticMemory(storage_path=tmp_path / "sem")
    _down(monkeypatch, exc)
    ctx = EcphoryRetrieval(epi, sem).retrieve("查詢")
    assert ctx.degraded is True
    assert ctx.degraded_reason
    assert ctx.is_empty()
    assert epi.embedding_degraded is True


def test_query_uses_short_timeout(tmp_path, monkeypatch):
    epi = _make(tmp_path)
    seen = {}

    def fake_post(url, json=None, timeout=None):
        seen["timeout"] = timeout
        raise requests.exceptions.Timeout()

    monkeypatch.setattr(requests, "post", fake_post)
    assert epi.try_generate_query_embedding("q") == []
    assert seen["timeout"] == 0.1


def test_generate_query_embedding_still_raises(tmp_path, monkeypatch):
    epi = _make(tmp_path)
    _down(monkeypatch, requests.exceptions.ConnectionError("c"))
    with pytest.raises(EmbeddingUnavailable):
        epi.generate_query_embedding("q")


def test_write_keeps_episode_when_embedding_fails_then_backfills(tmp_path, monkeypatch):
    epi = _make(tmp_path)
    _down(monkeypatch, requests.exceptions.ConnectionError("c"))
    ep = _ep()
    epi.write(ep)

    reloaded = _make(tmp_path)
    reloaded._load(reset=True)
    stored = reloaded.get(ep.id)
    assert stored is not None
    assert stored.metadata.get("embedding_pending") is True
    # 零向量不會被召回
    assert reloaded.search([1.0] + [0.0] * 1023, min_similarity=0.1) == []

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"embeddings": [[1.0] + [0.0] * 1023]}

    monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp())
    assert reloaded.backfill_pending_embeddings() == 1
    again = _make(tmp_path)
    again._load(reset=True)
    assert "embedding_pending" not in again.get(ep.id).metadata
    hits = again.search([1.0] + [0.0] * 1023, min_similarity=0.5)
    assert [e.id for e, _ in hits] == [ep.id]


def test_warmup_failure_marks_degraded_without_raising(tmp_path, monkeypatch):
    epi = _make(tmp_path)
    _down(monkeypatch, requests.exceptions.ConnectionError("c"))
    assert epi.warmup(background=False) is False
    assert epi.embedding_degraded is True
    t = epi.warmup(background=True)
    t.join(timeout=5)
    assert not t.is_alive()
