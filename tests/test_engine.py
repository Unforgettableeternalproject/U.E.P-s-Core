"""
測試 MemoryEngine 整合功能
"""

import pytest
from pathlib import Path
import shutil

from echo_memory import MemoryEngine


@pytest.fixture
def temp_storage(tmp_path):
    """臨時存儲目錄"""
    yield tmp_path
    if tmp_path.exists():
        shutil.rmtree(tmp_path)


def test_memory_engine_init(temp_storage):
    """測試記憶引擎初始化"""
    engine = MemoryEngine(storage_path=temp_storage)
    
    assert engine.storage_path == temp_storage
    assert engine.embedding_model == "all-MiniLM-L6-v2"


def test_store_and_retrieve(temp_storage):
    """測試存儲與檢索完整流程"""
    engine = MemoryEngine(storage_path=temp_storage)
    
    # 存儲對話
    ep_id = engine.store_episode(
        user_input="今天天氣如何？",
        agent_response="台北今天晴朗。",
        session_id="test_session",
        salience=0.7,
    )
    
    assert ep_id.startswith("ep_")
    
    # 檢索
    context = engine.retrieve(
        query="天氣怎麼樣？",
        dopamine=0.5,
        serotonin=0.5,
        top_k=5,
    )
    
    assert len(context.episodes) > 0
    assert "天氣" in context.episodes[0].content_summary


def test_memory_context_to_text(temp_storage):
    """測試 MemoryContext 文本序列化"""
    engine = MemoryEngine(storage_path=temp_storage)
    
    # 存儲多個對話
    engine.store_episode(
        user_input="我喜歡晴天",
        agent_response="好的，記住了！",
        session_id="s1",
        salience=0.8,
    )
    
    engine.store_episode(
        user_input="今天天氣如何？",
        agent_response="晴朗",
        session_id="s1",
        salience=0.5,
    )
    
    # 檢索並轉換為文本
    context = engine.retrieve("喜歡什麼天氣", top_k=2)
    text = context.to_text()
    
    assert "對話記憶" in text
    assert "核心記憶" in text or "重要記憶" in text  # salience >= 0.5


def test_stats(temp_storage):
    """測試統計資訊"""
    engine = MemoryEngine(storage_path=temp_storage)
    
    stats = engine.get_stats()
    assert stats["total_episodes"] == 0
    
    engine.store_episode(
        user_input="test",
        agent_response="response",
        session_id="s1",
    )
    
    stats = engine.get_stats()
    assert stats["total_episodes"] == 1
