from echo_memory.config import Config


def test_default_retrieval_similarity_is_documented_value(monkeypatch):
    monkeypatch.setattr("echo_memory.config.DOTENV_AVAILABLE", False)
    monkeypatch.delenv("RETRIEVAL_MIN_SIMILARITY", raising=False)

    assert Config().retrieval_min_similarity == 0.25
