"""
測試 Branch 8: Entity 提取功能

覆蓋：
  - Entity schema (to_dict / from_dict)
  - EntityStore CRUD + 持久化
  - EntityExtractor (規則 / mock LLM)
  - MemoryGraph MENTIONS 邊
  - MemoryContext entities 型別
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from echo_memory.schemas.entity import Entity
from echo_memory.schemas.memory_context import MemoryContext


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def temp_dir(tmp_path: Path) -> Path:
    """提供臨時目錄，測試結束後自動清除。"""
    yield tmp_path
    if tmp_path.exists():
        shutil.rmtree(tmp_path)


@pytest.fixture
def sample_entity() -> Entity:
    """建立範例實體物件。"""
    now = datetime.now(timezone.utc)
    return Entity(
        name="台北",
        entity_type="place",
        description="台灣首都",
        mention_count=1,
        source_episode_ids=["ep_test001"],
        first_seen=now,
        last_seen=now,
    )


@pytest.fixture
def entity_store(temp_dir: Path):
    """建立 EntityStore 實例，使用臨時目錄。"""
    from echo_memory.storage.entity import EntityStore
    return EntityStore(storage_path=temp_dir / "entities")


@pytest.fixture
def memory_graph(temp_dir: Path):
    """建立 MemoryGraph 實例，使用臨時目錄。"""
    from echo_memory.storage.graph import MemoryGraph
    return MemoryGraph(storage_path=temp_dir / "graph")


@pytest.fixture
def sample_episode():
    """建立範例 Episode 物件。"""
    from echo_memory.schemas.episode import Episode
    return Episode(
        user_input="我住在台北，喜歡用 iPhone。",
        agent_response="台北是個好地方，Apple 的產品品質不錯。",
        session_id="sess_test",
        content_summary="使用者談論居住地和手機",
    )


# ─────────────────────────────────────────────────────────────────────────────
# 1. Entity Schema
# ─────────────────────────────────────────────────────────────────────────────

class TestEntitySchema:
    """測試 Entity dataclass 序列化 / 反序列化。"""

    def test_auto_id_generation(self):
        """未提供 id 時應自動生成 ent_ 開頭的 ID。"""
        entity = Entity(name="測試")
        assert entity.id.startswith("ent_")
        assert len(entity.id) > 4

    def test_explicit_id_preserved(self):
        """提供 id 時應保留原值。"""
        entity = Entity(id="ent_custom123", name="測試")
        assert entity.id == "ent_custom123"

    def test_entity_schema_roundtrip(self, sample_entity: Entity):
        """to_dict → from_dict 應完整還原所有欄位。"""
        d = sample_entity.to_dict()
        restored = Entity.from_dict(d)

        assert restored.id == sample_entity.id
        assert restored.name == sample_entity.name
        assert restored.entity_type == sample_entity.entity_type
        assert restored.description == sample_entity.description
        assert restored.mention_count == sample_entity.mention_count
        assert restored.source_episode_ids == sample_entity.source_episode_ids

        # datetime 序列化後應精確還原（isoformat 往返）
        assert abs(
            (restored.first_seen - sample_entity.first_seen).total_seconds()
        ) < 1.0

    def test_to_dict_contains_required_keys(self, sample_entity: Entity):
        """to_dict 應包含所有必要鍵。"""
        d = sample_entity.to_dict()
        required_keys = {
            "id", "name", "entity_type", "description",
            "mention_count", "source_episode_ids", "first_seen", "last_seen",
            "metadata",
        }
        assert required_keys.issubset(d.keys())

    def test_datetime_stored_as_isoformat(self, sample_entity: Entity):
        """datetime 欄位在 dict 中應為 ISO 8601 字串。"""
        d = sample_entity.to_dict()
        # 驗證可以被正確解析
        datetime.fromisoformat(d["first_seen"])
        datetime.fromisoformat(d["last_seen"])

    def test_default_entity_type(self):
        """未指定 entity_type 時預設為 'other'。"""
        entity = Entity(name="未知事物")
        assert entity.entity_type == "other"


# ─────────────────────────────────────────────────────────────────────────────
# 2. EntityStore CRUD
# ─────────────────────────────────────────────────────────────────────────────

class TestEntityStoreCRUD:
    """測試 EntityStore 基本增刪查。"""

    def test_upsert_and_get(self, entity_store, sample_entity: Entity):
        """upsert 後應能用 ID 查詢到相同實體。"""
        ent_id = entity_store.upsert(sample_entity)
        assert ent_id == sample_entity.id

        retrieved = entity_store.get(ent_id)
        assert retrieved is not None
        assert retrieved.name == sample_entity.name
        assert retrieved.entity_type == sample_entity.entity_type

    def test_get_by_name(self, entity_store, sample_entity: Entity):
        """upsert 後應能用名稱（忽略大小寫）查詢到實體。"""
        entity_store.upsert(sample_entity)

        found = entity_store.get_by_name(sample_entity.name)
        assert found is not None
        assert found.id == sample_entity.id

    def test_get_by_name_case_insensitive(self, entity_store):
        """名稱查詢應忽略大小寫。"""
        entity = Entity(name="iPhone", entity_type="product", description="蘋果手機")
        entity_store.upsert(entity)

        assert entity_store.get_by_name("iphone") is not None
        assert entity_store.get_by_name("IPHONE") is not None

    def test_upsert_same_name_updates(self, entity_store):
        """相同名稱的 upsert 應更新既有實體而非新增。"""
        e1 = Entity(name="OpenAI", entity_type="product", description="舊描述")
        id1 = entity_store.upsert(e1)

        e2 = Entity(name="OpenAI", entity_type="product", description="新描述")
        id2 = entity_store.upsert(e2)

        # 應為同一筆
        assert id1 == id2
        assert entity_store.count() == 1

        # 描述應更新
        retrieved = entity_store.get(id1)
        assert retrieved.description == "新描述"

    def test_list_entities_all(self, entity_store):
        """list_entities 無篩選應返回所有實體。"""
        e1 = Entity(name="Alice", entity_type="person", description="")
        e2 = Entity(name="Tokyo", entity_type="place", description="")
        entity_store.upsert(e1)
        entity_store.upsert(e2)

        all_entities = entity_store.list_entities()
        assert len(all_entities) == 2

    def test_list_entities_filter_by_type(self, entity_store):
        """list_entities 指定 entity_type 時應只返回符合的實體。"""
        entity_store.upsert(Entity(name="Alice", entity_type="person", description=""))
        entity_store.upsert(Entity(name="Tokyo", entity_type="place", description=""))
        entity_store.upsert(Entity(name="Bob", entity_type="person", description=""))

        persons = entity_store.list_entities(entity_type="person")
        assert len(persons) == 2
        assert all(e.entity_type == "person" for e in persons)

    def test_count(self, entity_store, sample_entity: Entity):
        """count 應返回正確的實體數量。"""
        assert entity_store.count() == 0
        entity_store.upsert(sample_entity)
        assert entity_store.count() == 1

    def test_clear(self, entity_store, sample_entity: Entity):
        """clear 後 count 應為 0，且 get 應返回 None。"""
        entity_store.upsert(sample_entity)
        assert entity_store.count() == 1

        entity_store.clear()
        assert entity_store.count() == 0
        assert entity_store.get(sample_entity.id) is None


# ─────────────────────────────────────────────────────────────────────────────
# 3. EntityStore 持久化
# ─────────────────────────────────────────────────────────────────────────────

class TestEntityStorePersistence:
    """測試 EntityStore 的磁盤持久化。"""

    def test_entity_store_persistence(self, temp_dir: Path, sample_entity: Entity):
        """寫入後重新載入，資料應完整保留。"""
        from echo_memory.storage.entity import EntityStore

        store1 = EntityStore(storage_path=temp_dir / "entities")
        store1.upsert(sample_entity)
        ent_id = sample_entity.id
        assert store1.count() == 1

        # 建立新實例（重新從磁盤載入）
        store2 = EntityStore(storage_path=temp_dir / "entities")
        assert store2.count() == 1

        retrieved = store2.get(ent_id)
        assert retrieved is not None
        assert retrieved.name == sample_entity.name
        assert retrieved.entity_type == sample_entity.entity_type

    def test_entities_file_is_json(self, temp_dir: Path, sample_entity: Entity):
        """持久化檔案應為有效的 JSON 格式。"""
        from echo_memory.storage.entity import EntityStore

        store = EntityStore(storage_path=temp_dir / "entities")
        store.upsert(sample_entity)

        entities_file = temp_dir / "entities" / "entities.json"
        assert entities_file.exists()

        with open(entities_file, "r", encoding="utf-8") as f:
            data = json.load(f)

        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["name"] == sample_entity.name

    def test_atomic_write_uses_temp_file(self, temp_dir: Path):
        """寫入完成後不應存在 .json.tmp 暫存檔。"""
        from echo_memory.storage.entity import EntityStore

        store = EntityStore(storage_path=temp_dir / "entities")
        store.upsert(Entity(name="TestEntity", entity_type="other", description=""))

        tmp_file = temp_dir / "entities" / "entities.json.tmp"
        assert not tmp_file.exists(), "原子寫入後不應留下 .tmp 暫存檔"


# ─────────────────────────────────────────────────────────────────────────────
# 4. EntityStore increment_mention
# ─────────────────────────────────────────────────────────────────────────────

class TestEntityStoreIncrementMention:
    """測試 EntityStore.increment_mention。"""

    def test_entity_store_increment_mention(self, entity_store, sample_entity: Entity):
        """increment_mention 應正確累加計數並記錄 episode_id。"""
        entity_store.upsert(sample_entity)
        ent_id = sample_entity.id

        entity_store.increment_mention(ent_id, "ep_aaa")
        entity_store.increment_mention(ent_id, "ep_bbb")

        retrieved = entity_store.get(ent_id)
        # 初始 mention_count=1，再加兩次 = 3
        assert retrieved.mention_count == 3
        assert "ep_aaa" in retrieved.source_episode_ids
        assert "ep_bbb" in retrieved.source_episode_ids

    def test_increment_mention_no_duplicate_episode(
        self, entity_store, sample_entity: Entity
    ):
        """相同 episode_id 不應重複加入 source_episode_ids。"""
        entity_store.upsert(sample_entity)
        ent_id = sample_entity.id

        entity_store.increment_mention(ent_id, "ep_xxx")
        entity_store.increment_mention(ent_id, "ep_xxx")

        retrieved = entity_store.get(ent_id)
        assert retrieved.source_episode_ids.count("ep_xxx") == 1

    def test_increment_mention_nonexistent_entity(self, entity_store):
        """對不存在的 entity_id 呼叫 increment_mention 不應拋出例外。"""
        entity_store.increment_mention("ent_nonexistent", "ep_xyz")  # 不應 raise


# ─────────────────────────────────────────────────────────────────────────────
# 5. EntityExtractor - 無 LLM（規則提取）
# ─────────────────────────────────────────────────────────────────────────────

class TestEntityExtractorNoLLM:
    """測試 EntityExtractor 在無 LLM 時的規則降級提取。"""

    def test_entity_extractor_no_llm_no_error(self, sample_episode):
        """無 LLM 時呼叫 extract 不應拋出例外。"""
        from echo_memory.dream.entity_extractor import EntityExtractor
        extractor = EntityExtractor(llm_fn=None)

        result = extractor.extract(sample_episode)
        assert isinstance(result, list)

    def test_entity_extractor_no_llm_returns_entities(self):
        """有明確可識別詞語時，規則提取應返回至少一個實體。"""
        from echo_memory.dream.entity_extractor import EntityExtractor
        from echo_memory.schemas.episode import Episode

        episode = Episode(
            user_input='I use "Google Chrome" and "Visual Studio Code" daily.',
            agent_response="Those are great tools.",
            session_id="sess_test",
            content_summary="工具討論",
        )
        extractor = EntityExtractor(llm_fn=None)
        result = extractor.extract(episode)

        assert len(result) >= 1
        assert all(isinstance(e, Entity) for e in result)

    def test_entity_extractor_no_llm_empty_text(self):
        """純空白文字應返回空列表，不報錯。"""
        from echo_memory.dream.entity_extractor import EntityExtractor
        from echo_memory.schemas.episode import Episode

        episode = Episode(
            user_input="  ",
            agent_response="  ",
            session_id="sess_test",
            content_summary="空",
        )
        extractor = EntityExtractor(llm_fn=None)
        result = extractor.extract(episode)
        assert isinstance(result, list)


# ─────────────────────────────────────────────────────────────────────────────
# 6. EntityExtractor - Mock LLM
# ─────────────────────────────────────────────────────────────────────────────

class TestEntityExtractorWithMockLLM:
    """測試 EntityExtractor 使用 mock llm_fn 的 LLM 路徑。"""

    def _make_llm_fn(self, response: str):
        """建立固定回應的 mock llm_fn。"""
        def _fn(prompt: str) -> str:
            return response
        return _fn

    def test_entity_extractor_with_mock_llm(self, sample_episode):
        """mock llm_fn 回傳正確 JSON 時，應解析出對應的 Entity 物件。"""
        from echo_memory.dream.entity_extractor import EntityExtractor

        llm_response = json.dumps([
            {"name": "台北", "entity_type": "place", "description": "台灣首都"},
            {"name": "iPhone", "entity_type": "product", "description": "蘋果手機"},
        ], ensure_ascii=False)

        extractor = EntityExtractor(llm_fn=self._make_llm_fn(llm_response))
        result = extractor.extract(sample_episode)

        assert len(result) == 2
        names = {e.name for e in result}
        assert "台北" in names
        assert "iPhone" in names

    def test_entity_types_parsed_correctly(self, sample_episode):
        """LLM 返回的 entity_type 應正確映射。"""
        from echo_memory.dream.entity_extractor import EntityExtractor

        llm_response = json.dumps([
            {"name": "Alice", "entity_type": "person", "description": "某人"},
            {"name": "Unknown", "entity_type": "invalid_type", "description": "未知"},
        ], ensure_ascii=False)

        extractor = EntityExtractor(llm_fn=self._make_llm_fn(llm_response))
        result = extractor.extract(sample_episode)

        by_name = {e.name: e for e in result}
        assert by_name["Alice"].entity_type == "person"
        # 不合法的類型應退回 "other"
        assert by_name["Unknown"].entity_type == "other"

    def test_empty_llm_response(self, sample_episode):
        """LLM 回傳空陣列 [] 時，應返回空列表。"""
        from echo_memory.dream.entity_extractor import EntityExtractor

        extractor = EntityExtractor(llm_fn=self._make_llm_fn("[]"))
        result = extractor.extract(sample_episode)
        assert result == []

    def test_invalid_json_falls_back_to_rules(self, sample_episode):
        """LLM 回傳無效 JSON 時，應降級到規則提取，不拋出例外。"""
        from echo_memory.dream.entity_extractor import EntityExtractor

        extractor = EntityExtractor(llm_fn=self._make_llm_fn("not valid json at all"))
        # 不應拋出例外
        result = extractor.extract(sample_episode)
        assert isinstance(result, list)

    def test_duplicate_names_deduplicated(self, sample_episode):
        """LLM 回傳重複名稱時，應只保留第一筆。"""
        from echo_memory.dream.entity_extractor import EntityExtractor

        llm_response = json.dumps([
            {"name": "台北", "entity_type": "place", "description": "台灣首都"},
            {"name": "台北", "entity_type": "place", "description": "重複"},
        ], ensure_ascii=False)

        extractor = EntityExtractor(llm_fn=self._make_llm_fn(llm_response))
        result = extractor.extract(sample_episode)

        names = [e.name for e in result]
        assert names.count("台北") == 1

    def test_source_episode_id_set_correctly(self, sample_episode):
        """提取結果的 source_episode_ids 應包含目前 episode 的 ID。"""
        from echo_memory.dream.entity_extractor import EntityExtractor

        llm_response = json.dumps([
            {"name": "台北", "entity_type": "place", "description": "台灣首都"},
        ], ensure_ascii=False)

        extractor = EntityExtractor(llm_fn=self._make_llm_fn(llm_response))
        result = extractor.extract(sample_episode)

        assert len(result) == 1
        assert sample_episode.id in result[0].source_episode_ids


# ─────────────────────────────────────────────────────────────────────────────
# 7. MemoryGraph MENTIONS 邊
# ─────────────────────────────────────────────────────────────────────────────

class TestGraphMentionsEdges:
    """測試 MemoryGraph 的 MENTIONS 邊相關方法。"""

    def test_graph_mentions_edges(self, memory_graph, sample_episode):
        """add_mentions_edge + get_episode_entities 應正確回傳實體。"""
        entity = Entity(name="台北", entity_type="place", description="台灣首都")

        # 先把節點加入圖
        memory_graph.add_episode_node(sample_episode)
        memory_graph.add_entity_node(entity.id, entity.name, entity.entity_type)
        memory_graph.add_mentions_edge(sample_episode.id, entity.id, weight=0.9)

        # 查詢 Episode 提及的 Entity
        result = memory_graph.get_episode_entities(sample_episode.id)
        assert len(result) == 1
        assert result[0][0] == entity.id
        assert abs(result[0][1] - 0.9) < 1e-6

    def test_get_entity_episodes(self, memory_graph, sample_episode):
        """get_entity_episodes 應返回提及該實體的 Episode。"""
        entity = Entity(name="iPhone", entity_type="product", description="手機")

        memory_graph.add_episode_node(sample_episode)
        memory_graph.add_entity_node(entity.id, entity.name, entity.entity_type)
        memory_graph.add_mentions_edge(sample_episode.id, entity.id, weight=0.7)

        result = memory_graph.get_entity_episodes(entity.id)
        assert len(result) == 1
        assert result[0][0] == sample_episode.id

    def test_multiple_entities_for_episode(self, memory_graph, sample_episode):
        """一個 Episode 可以提及多個 Entity。"""
        e1 = Entity(name="台北", entity_type="place", description="")
        e2 = Entity(name="iPhone", entity_type="product", description="")

        memory_graph.add_episode_node(sample_episode)
        memory_graph.add_entity_node(e1.id, e1.name, e1.entity_type)
        memory_graph.add_entity_node(e2.id, e2.name, e2.entity_type)
        memory_graph.add_mentions_edge(sample_episode.id, e1.id)
        memory_graph.add_mentions_edge(sample_episode.id, e2.id)

        result = memory_graph.get_episode_entities(sample_episode.id)
        assert len(result) == 2
        entity_ids = {r[0] for r in result}
        assert e1.id in entity_ids
        assert e2.id in entity_ids

    def test_add_entity_node_registers_in_graph(self, memory_graph):
        """add_entity_node 後，has_node 應回傳 True。"""
        entity = Entity(name="Google", entity_type="product", description="")
        memory_graph.add_entity_node(entity.id, entity.name, entity.entity_type)
        assert memory_graph.has_node(entity.id)

    def test_mentions_edge_nonexistent_nodes(self, memory_graph):
        """節點不存在時 add_mentions_edge 不應拋出例外（記錄 warning 並忽略）。"""
        memory_graph.add_mentions_edge("ep_nonexistent", "ent_nonexistent")
        # 不應拋出例外

    def test_get_episode_entities_no_node(self, memory_graph):
        """對不存在的 episode_id 呼叫 get_episode_entities 應返回空列表。"""
        result = memory_graph.get_episode_entities("ep_nonexistent")
        assert result == []

    def test_get_entity_episodes_no_node(self, memory_graph):
        """對不存在的 entity_id 呼叫 get_entity_episodes 應返回空列表。"""
        result = memory_graph.get_entity_episodes("ent_nonexistent")
        assert result == []


# ─────────────────────────────────────────────────────────────────────────────
# 8. MemoryContext entities 型別
# ─────────────────────────────────────────────────────────────────────────────

class TestMemoryContextEntitiesType:
    """確認 MemoryContext.entities 是 list[Entity] 型別。"""

    def test_memory_context_entities_type(self):
        """entities 欄位應接受並保存 Entity 物件。"""
        entity = Entity(name="台北", entity_type="place", description="台灣首都")
        ctx = MemoryContext(entities=[entity])

        assert len(ctx.entities) == 1
        assert isinstance(ctx.entities[0], Entity)
        assert ctx.entities[0].name == "台北"

    def test_memory_context_entities_default_empty(self):
        """未指定 entities 時預設應為空列表。"""
        ctx = MemoryContext()
        assert ctx.entities == []

    def test_memory_context_to_text_with_entities(self):
        """to_text 應正確渲染 Entity 物件的屬性（不再呼叫 dict.get）。"""
        entity = Entity(
            name="iPhone",
            entity_type="product",
            description="蘋果公司旗艦手機",
        )
        ctx = MemoryContext(entities=[entity])
        text = ctx.to_text()

        assert "iPhone" in text
        assert "product" in text
        assert "蘋果公司旗艦手機" in text

    def test_memory_context_is_empty_with_entities(self):
        """有 entities 時 is_empty 應返回 False。"""
        entity = Entity(name="台北", entity_type="place", description="")
        ctx = MemoryContext(entities=[entity])
        assert not ctx.is_empty()

    def test_memory_context_summary_counts_entities(self):
        """summary() 應包含正確的 entities 計數。"""
        entities = [
            Entity(name="台北", entity_type="place", description=""),
            Entity(name="iPhone", entity_type="product", description=""),
        ]
        ctx = MemoryContext(entities=entities)
        summary = ctx.summary()
        assert "entities=2" in summary
