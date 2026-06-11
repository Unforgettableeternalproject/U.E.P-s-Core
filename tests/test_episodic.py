"""
測試 Episodic Memory 基本功能
"""

import json
import shutil

import numpy as np
import pytest

from echo_memory.schemas.episode import Episode
from echo_memory.storage.episodic import EpisodicMemory


@pytest.fixture
def temp_storage(tmp_path):
    """臨時存儲目錄"""
    yield tmp_path
    # 測試結束後清理
    if tmp_path.exists():
        shutil.rmtree(tmp_path)


def test_episode_creation():
    """測試 Episode 創建"""
    ep = Episode(
        user_input="test input",
        agent_response="test response",
        session_id="test_session",
        content_summary="test summary",
    )

    assert ep.id.startswith("ep_")
    assert ep.user_input == "test input"
    assert ep.salience_score == 0.5  # 預設值


def test_episode_serialization():
    """測試 Episode 序列化"""
    ep = Episode(
        user_input="test",
        agent_response="response",
        session_id="session",
        content_summary="summary",
    )

    # 序列化
    data = ep.to_dict()
    assert "id" in data
    assert "user_input" in data

    # 反序列化
    ep2 = Episode.from_dict(data)
    assert ep2.id == ep.id
    assert ep2.user_input == ep.user_input


def test_episodic_write_and_get(temp_storage):
    """測試寫入與讀取"""
    memory = EpisodicMemory(storage_path=temp_storage)

    ep = Episode(
        user_input="Hello",
        agent_response="Hi there!",
        session_id="test",
        content_summary="Greeting",
    )

    # 寫入
    ep_id = memory.write(ep)
    assert ep_id == ep.id

    # 讀取
    retrieved = memory.get(ep_id)
    assert retrieved is not None
    assert retrieved.user_input == "Hello"


def test_episodic_search(temp_storage):
    """測試向量檢索"""
    memory = EpisodicMemory(storage_path=temp_storage, device="cpu")

    # 寫入多個情節
    episodes = [
        Episode(
            user_input="今天天氣如何？",
            agent_response="晴朗",
            session_id="s1",
            content_summary="詢問天氣",
        ),
        Episode(
            user_input="推薦一部電影",
            agent_response="推薦《沙丘》",
            session_id="s1",
            content_summary="電影推薦",
        ),
    ]

    for ep in episodes:
        memory.write(ep)

    # 生成查詢向量（retrieval 場景，需加 query prefix）
    query_emb = memory.generate_query_embedding("天氣怎麼樣？")

    # 檢索（CI mock 生成確定性偽向量，不測語義相似度，只測機制正確性）
    results = memory.search(query_emb, top_k=2, min_similarity=-1.0)
    assert len(results) > 0
    # 確認返回的結果是有效的 Episode 物件
    assert results[0][0].session_id == "s1"


def test_episodic_persistence(temp_storage):
    """測試持久化"""
    # 第一次初始化
    memory1 = EpisodicMemory(storage_path=temp_storage)
    ep = Episode(
        user_input="Test",
        agent_response="Response",
        session_id="s1",
        content_summary="Test episode",
    )
    ep_id = memory1.write(ep)

    # 第二次初始化（模擬重啟）
    memory2 = EpisodicMemory(storage_path=temp_storage)
    retrieved = memory2.get(ep_id)

    assert retrieved is not None
    assert retrieved.user_input == "Test"


def test_episodic_count(temp_storage):
    """測試計數"""
    memory = EpisodicMemory(storage_path=temp_storage)

    assert memory.count() == 0

    ep = Episode(
        user_input="test",
        agent_response="response",
        session_id="s1",
        content_summary="summary",
    )
    memory.write(ep)

    assert memory.count() == 1


def test_search_uses_cosine_similarity_for_fixed_vectors(temp_storage):
    """Fixed vectors verify ranking, scores, thresholding, and zero handling."""
    memory = EpisodicMemory(storage_path=temp_storage)
    vectors = {
        "same": [10.0, 0.0],
        "orthogonal": [0.0, 3.0],
        "opposite": [-2.0, 0.0],
        "zero": [0.0, 0.0],
    }

    for name, vector in vectors.items():
        memory.write(
            Episode(
                user_input=name,
                agent_response="response",
                session_id="vectors",
                content_summary=name,
                embedding=vector,
            )
        )

    results = memory.search([5.0, 0.0], top_k=4, min_similarity=-1.0)
    scores = {episode.user_input: score for episode, score in results}

    ranked_names = [episode.user_input for episode, _ in results]
    assert ranked_names[0] == "same"
    assert set(ranked_names[1:3]) == {"orthogonal", "zero"}
    assert ranked_names[3] == "opposite"
    assert scores["same"] == pytest.approx(1.0)
    assert scores["orthogonal"] == pytest.approx(0.0)
    assert scores["opposite"] == pytest.approx(-1.0)
    assert scores["zero"] == pytest.approx(0.0)

    thresholded = memory.search([1.0, 0.0], top_k=4, min_similarity=0.5)
    assert [episode.user_input for episode, _ in thresholded] == ["same"]
    assert memory.search([0.0, 0.0]) == []


def test_load_migrates_legacy_unnormalized_embeddings(temp_storage):
    """Legacy embeddings.npy files are normalized in place when loaded."""
    episode = Episode(
        user_input="legacy",
        agent_response="response",
        session_id="legacy",
        content_summary="legacy",
        embedding=[3.0, 4.0],
    )
    with open(temp_storage / "episodes.json", "w", encoding="utf-8") as metadata_file:
        json.dump([episode.to_dict()], metadata_file)
    np.save(temp_storage / "embeddings.npy", np.array([[3.0, 4.0]], dtype=np.float32))

    # 使用自定義模型名避免維度相容性檢查（此測試專注於 normalization migration）
    memory = EpisodicMemory(storage_path=temp_storage, embedding_model="test-model")

    assert memory.search([3.0, 4.0], min_similarity=0.99)[0][1] == pytest.approx(1.0)
    assert np.load(temp_storage / "embeddings.npy")[0] == pytest.approx([0.6, 0.8])
    assert memory.get(episode.id).embedding == pytest.approx([0.6, 0.8])

    with open(temp_storage / "episodes.json", encoding="utf-8") as metadata_file:
        persisted_episode = json.load(metadata_file)[0]
    assert persisted_episode["embedding"] == pytest.approx([0.6, 0.8])


def test_load_repairs_metadata_after_partial_legacy_migration(temp_storage):
    """Normalized matrix repairs metadata left stale by an interrupted migration."""
    episode = Episode(
        user_input="partial migration",
        agent_response="response",
        session_id="legacy",
        content_summary="legacy",
        embedding=[3.0, 4.0],
    )
    with open(temp_storage / "episodes.json", "w", encoding="utf-8") as metadata_file:
        json.dump([episode.to_dict()], metadata_file)
    np.save(temp_storage / "embeddings.npy", np.array([[0.6, 0.8]], dtype=np.float32))

    # 使用自定義模型名避免維度相容性檢查
    memory = EpisodicMemory(storage_path=temp_storage, embedding_model="test-model")

    assert memory.get(episode.id).embedding == pytest.approx([0.6, 0.8])
    with open(temp_storage / "episodes.json", encoding="utf-8") as metadata_file:
        persisted_episode = json.load(metadata_file)[0]
    assert persisted_episode["embedding"] == pytest.approx([0.6, 0.8])
