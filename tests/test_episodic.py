"""
測試 Episodic Memory 基本功能
"""

import pytest
from pathlib import Path
import shutil

from echo_memory.storage.episodic import EpisodicMemory
from echo_memory.schemas.episode import Episode


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
    
    # 生成查詢向量
    query_emb = memory._generate_embedding("天氣怎麼樣？")
    
    # 檢索
    results = memory.search(query_emb, top_k=1)
    assert len(results) > 0
    assert "天氣" in results[0].content_summary


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
