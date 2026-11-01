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


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Issue #8 — CRUD API 測試
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _make_episode(session_id: str = "s1", salience: float = 0.5, user_input: str = "hi") -> Episode:
    return Episode(
        user_input=user_input,
        agent_response="response",
        session_id=session_id,
        content_summary=user_input[:20],
        salience_score=salience,
        embedding=[1.0, 0.0],
    )


class TestListEpisodes:
    def test_returns_all_episodes_by_default(self, temp_storage):
        memory = EpisodicMemory(storage_path=temp_storage)
        for i in range(5):
            memory.write(_make_episode(user_input=f"msg {i}"))
        result = memory.list_episodes()
        assert len(result) == 5

    def test_session_filter(self, temp_storage):
        memory = EpisodicMemory(storage_path=temp_storage)
        memory.write(_make_episode(session_id="alpha"))
        memory.write(_make_episode(session_id="alpha"))
        memory.write(_make_episode(session_id="beta"))
        assert len(memory.list_episodes(session_id="alpha")) == 2
        assert len(memory.list_episodes(session_id="beta")) == 1
        assert len(memory.list_episodes(session_id="gamma")) == 0

    def test_pagination(self, temp_storage):
        memory = EpisodicMemory(storage_path=temp_storage)
        for i in range(7):
            memory.write(_make_episode(user_input=f"msg {i}"))
        page1 = memory.list_episodes(limit=3, offset=0)
        page2 = memory.list_episodes(limit=3, offset=3)
        page3 = memory.list_episodes(limit=3, offset=6)
        assert len(page1) == 3
        assert len(page2) == 3
        assert len(page3) == 1
        # 確保不重疊
        ids1 = {ep.id for ep in page1}
        ids2 = {ep.id for ep in page2}
        assert ids1.isdisjoint(ids2)

    def test_sort_by_salience_descending(self, temp_storage):
        memory = EpisodicMemory(storage_path=temp_storage)
        memory.write(_make_episode(salience=0.2, user_input="low"))
        memory.write(_make_episode(salience=0.9, user_input="high"))
        memory.write(_make_episode(salience=0.5, user_input="mid"))
        result = memory.list_episodes(sort_by="salience", ascending=False)
        scores = [ep.salience_score for ep in result]
        assert scores == sorted(scores, reverse=True)

    def test_limit_cap_at_200(self, temp_storage):
        memory = EpisodicMemory(storage_path=temp_storage)
        result = memory.list_episodes(limit=9999)
        # 內部已截斷為 200（此處 0 筆，但不應拋出錯誤）
        assert isinstance(result, list)


class TestDeleteEpisode:
    def test_delete_existing_episode(self, temp_storage):
        memory = EpisodicMemory(storage_path=temp_storage)
        ep = _make_episode()
        memory.write(ep)
        assert memory.count() == 1

        success = memory.delete_episode(ep.id)

        assert success is True
        assert memory.count() == 0
        assert memory.get(ep.id) is None

    def test_delete_nonexistent_returns_false(self, temp_storage):
        memory = EpisodicMemory(storage_path=temp_storage)
        assert memory.delete_episode("ep_doesnotexist") is False

    def test_delete_updates_embeddings(self, temp_storage):
        memory = EpisodicMemory(storage_path=temp_storage)
        ep1 = _make_episode(user_input="a")
        ep2 = _make_episode(user_input="b")
        ep3 = _make_episode(user_input="c")
        memory.write(ep1)
        memory.write(ep2)
        memory.write(ep3)

        memory.delete_episode(ep2.id)

        assert memory._embeddings is not None
        assert memory._embeddings.shape[0] == 2
        assert ep2.id not in memory._id_to_index
        # 確保剩餘兩筆的索引仍連續
        indices = sorted(memory._id_to_index.values())
        assert indices == list(range(len(indices)))

    def test_delete_removes_graph_node(self, temp_storage):
        memory = EpisodicMemory(storage_path=temp_storage)
        ep = _make_episode()
        memory.write(ep)
        assert memory.graph.has_node(ep.id)

        memory.delete_episode(ep.id)

        assert not memory.graph.has_node(ep.id)

    def test_delete_updates_last_episode_tracking(self, temp_storage):
        memory = EpisodicMemory(storage_path=temp_storage)
        ep1 = _make_episode(session_id="s1", user_input="first")
        ep2 = _make_episode(session_id="s1", user_input="second")
        memory.write(ep1)
        memory.write(ep2)

        # ep2 是 session s1 的最後一筆
        assert memory._last_episode_by_session.get("s1") == ep2.id

        memory.delete_episode(ep2.id)

        # 回溯到 ep1
        assert memory._last_episode_by_session.get("s1") == ep1.id

    def test_delete_last_episode_clears_session_tracking(self, temp_storage):
        memory = EpisodicMemory(storage_path=temp_storage)
        ep = _make_episode(session_id="lonely")
        memory.write(ep)

        memory.delete_episode(ep.id)

        assert "lonely" not in memory._last_episode_by_session

    def test_delete_persists_to_disk(self, temp_storage):
        memory = EpisodicMemory(storage_path=temp_storage)
        ep = _make_episode()
        memory.write(ep)
        memory.delete_episode(ep.id)

        # 重新載入，確認刪除已持久化
        memory2 = EpisodicMemory(storage_path=temp_storage)
        assert memory2.count() == 0
        assert memory2.get(ep.id) is None


class TestDeleteSession:
    def test_delete_entire_session(self, temp_storage):
        memory = EpisodicMemory(storage_path=temp_storage)
        for _ in range(3):
            memory.write(_make_episode(session_id="target"))
        memory.write(_make_episode(session_id="keep"))

        deleted = memory.delete_session("target")

        assert deleted == 3
        assert memory.count() == 1
        assert len(memory.list_episodes(session_id="target")) == 0
        assert len(memory.list_episodes(session_id="keep")) == 1

    def test_delete_nonexistent_session_returns_zero(self, temp_storage):
        memory = EpisodicMemory(storage_path=temp_storage)
        assert memory.delete_session("ghost") == 0

    def test_delete_session_rebuilds_embeddings(self, temp_storage):
        memory = EpisodicMemory(storage_path=temp_storage)
        for _ in range(3):
            memory.write(_make_episode(session_id="gone"))
        memory.write(_make_episode(session_id="stay"))

        memory.delete_session("gone")

        assert memory._embeddings is not None
        assert memory._embeddings.shape[0] == 1


class TestExportEpisodes:
    def test_export_json_format(self, temp_storage):
        memory = EpisodicMemory(storage_path=temp_storage)
        memory.write(_make_episode(user_input="export test"))
        content = memory.export_episodes(format="json")
        data = json.loads(content)
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["user_input"] == "export test"
        # embedding 應被省略
        assert "embedding" not in data[0]

    def test_export_csv_format(self, temp_storage):
        memory = EpisodicMemory(storage_path=temp_storage)
        memory.write(_make_episode(user_input="csv test"))
        content = memory.export_episodes(format="csv")
        lines = content.strip().splitlines()
        assert lines[0].startswith("id,")  # CSV header
        assert "csv test" in content

    def test_export_session_filter(self, temp_storage):
        memory = EpisodicMemory(storage_path=temp_storage)
        memory.write(_make_episode(session_id="alpha", user_input="alpha msg"))
        memory.write(_make_episode(session_id="beta", user_input="beta msg"))
        content = memory.export_episodes(session_id="alpha", format="json")
        data = json.loads(content)
        assert len(data) == 1
        assert data[0]["session_id"] == "alpha"

    def test_export_invalid_format_raises(self, temp_storage):
        memory = EpisodicMemory(storage_path=temp_storage)
        with pytest.raises(ValueError, match="不支援的匯出格式"):
            memory.export_episodes(format="xml")


class TestMaxEpisodes:
    def test_evicts_oldest_when_capacity_full(self, temp_storage):
        from datetime import datetime

        memory = EpisodicMemory(storage_path=temp_storage, max_episodes=3)

        ep1 = _make_episode(user_input="oldest")
        ep1.timestamp = datetime(2024, 1, 1)
        ep2 = _make_episode(user_input="middle")
        ep2.timestamp = datetime(2024, 1, 2)
        ep3 = _make_episode(user_input="recent")
        ep3.timestamp = datetime(2024, 1, 3)

        memory.write(ep1)
        memory.write(ep2)
        memory.write(ep3)
        assert memory.count() == 3

        ep4 = _make_episode(user_input="newest")
        ep4.timestamp = datetime(2024, 1, 4)
        memory.write(ep4)

        assert memory.count() == 3
        assert memory.get(ep1.id) is None  # 最舊的已被移除
        assert memory.get(ep4.id) is not None
