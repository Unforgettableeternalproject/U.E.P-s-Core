from echo_memory.config import Config


def test_default_retrieval_similarity_is_documented_value(monkeypatch):
    monkeypatch.setattr("echo_memory.config.DOTENV_AVAILABLE", False)
    monkeypatch.delenv("RETRIEVAL_MIN_SIMILARITY", raising=False)

    # bge-m3 分布偏高，0.35 為實測有效閾值（0.25 無法過濾無關結果）
    assert Config().retrieval_min_similarity == 0.35
