"""
測試 MemoryEngine 整合功能
"""

import shutil

import pytest

from echo_memory import MemoryEngine
from echo_memory.schemas.entity import Entity


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
    assert engine.embedding_model == "BAAI/bge-small-zh-v1.5"


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

    # 檢索（CI mock 不測語義相似度，設 min_similarity=0.0）
    context = engine.retrieve(
        query="天氣怎麼樣？",
        dopamine=0.5,
        serotonin=0.5,
        top_k=5,
        min_similarity=-1.0,
    )

    assert len(context.episodes) > 0
    assert context.episodes[0].session_id == "test_session"


def test_default_summary_combines_user_and_agent_text(temp_storage):
    engine = MemoryEngine(storage_path=temp_storage)

    episode_id = engine.store_episode(
        user_input="user detail",
        agent_response="agent detail",
        session_id="summary-test",
    )

    assert engine.episodic.get(episode_id).content_summary == "user detail agent detail"


def test_set_llm_fn_updates_existing_entity_extractor(temp_storage):
    engine = MemoryEngine(storage_path=temp_storage)
    extractor = engine.entity_extractor
    llm_fn = lambda prompt: "[]"

    engine.set_llm_fn(llm_fn)

    assert engine._llm_fn is llm_fn
    assert extractor._llm_fn is llm_fn


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

    # 檢索並轉換為文本（CI mock 設 min_similarity=0.0）
    context = engine.retrieve("喜歡什麼天氣", top_k=2, min_similarity=-1.0)
    text = context.to_text()

    assert "對話記憶" in text
    assert "核心記憶" in text or "重要記憶" in text  # salience >= 0.5


def test_stats(temp_storage):
    """測試統計資訊"""
    engine = MemoryEngine(storage_path=temp_storage)

    stats = engine.get_stats()
    assert stats["total_episodes"] == 0
    assert stats["total_entities"] == 0

    engine.store_episode(
        user_input="test",
        agent_response="response",
        session_id="s1",
    )

    stats = engine.get_stats()
    assert stats["total_episodes"] == 1


def test_clear_all_clears_entities(temp_storage):
    """clear_all 應清除實體記憶及其持久化資料。"""
    engine = MemoryEngine(storage_path=temp_storage)
    engine.entity_store.upsert(Entity(name="Bernie", entity_type="person"))

    engine.clear_all()

    assert engine.entity_store.count() == 0
    assert not (temp_storage / "entities" / "entities.json").exists()
