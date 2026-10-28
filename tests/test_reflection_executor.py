"""
測試 ReflectionExecutor + parse_reflection_to_actions

涵蓋：
  - parse_reflection_to_actions：空/有效/無效 ops
  - ReflectionExecutor：各行動的 mock 驗證
  - capture_pre_state：快照捕捉
  - executor_fn 映射完整性
"""

import json
import pytest
from unittest.mock import MagicMock
from dataclasses import dataclass, field

from echo_memory.affect.reflection import ReflectionModule, ReflectionOutput
from echo_memory.affect.reflection_executor import (
    ReflectionExecutor,
    parse_reflection_to_actions,
)
from echo_memory.affect.action_queue import (
    ReflectionActionQueue,
    ActionPermission,
    ActionStatus,
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Fixtures
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@dataclass
class FakeEpisode:
    """測試用 Episode 替身。"""
    id: str = "ep_test123"
    salience_score: float = 0.5
    content_summary: str = "測試摘要"
    metadata: dict = field(default_factory=dict)


@dataclass
class FakeConcept:
    """測試用 Concept 替身。"""
    id: str = "concept_test"
    name: str = "測試概念"
    description: str = "這是測試概念"
    concept_type: str = "entity"
    source_episode_ids: list = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


@pytest.fixture
def mock_engine():
    """建立 mock MemoryEngine。"""
    engine = MagicMock()

    # episodic
    engine.episodic.get.return_value = FakeEpisode()
    engine.episodic.update_salience.return_value = True
    engine.episodic.update_episode_metadata.return_value = True

    # semantic
    engine.semantic.get_by_name.return_value = FakeConcept()
    engine.semantic.upsert_concept.return_value = "concept_test"
    engine.semantic.remove_concept.return_value = True
    engine.semantic.list_concepts.return_value = [FakeConcept()]

    return engine


@pytest.fixture
def mock_soul_notes():
    """建立 mock SoulNoteManager。"""
    return MagicMock()


@pytest.fixture
def mock_inhibitor():
    """Patch Gate mock：概念變更一律委派給它（不再有直寫 fallback）。"""
    gate = MagicMock()
    gate.process_reflection_patch.return_value = (True, {})
    return gate


@pytest.fixture
def executor(mock_engine, mock_soul_notes, mock_inhibitor):
    """建立 ReflectionExecutor 實例。"""
    return ReflectionExecutor(
        engine=mock_engine, soul_notes=mock_soul_notes, concept_inhibitor=mock_inhibitor,
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# parse_reflection_to_actions 測試
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestParseReflectionToActions:
    """測試反思輸出解析。"""

    def test_empty_memory_ops(self):
        """空 memory_ops 回傳空列表。"""
        r = ReflectionOutput(memory_ops=[])
        assert parse_reflection_to_actions(r) == []

    def test_valid_ops(self):
        """有效 ops 正確轉換。"""
        r = ReflectionOutput(memory_ops=[
            {
                "op": "adjust_salience",
                "target": "ep_123",
                "params": {"new_salience": 0.8},
                "reason": "重要對話",
            },
            {
                "op": "add_note",
                "params": {"content": "反思筆記"},
            },
        ])
        actions = parse_reflection_to_actions(r)
        assert len(actions) == 2
        assert actions[0]["action_type"] == "adjust_salience"
        assert actions[0]["target_id"] == "ep_123"
        assert actions[0]["params"]["new_salience"] == 0.8
        assert actions[1]["action_type"] == "add_note"
        assert actions[1]["target_id"] == ""

    def test_missing_op_field_skipped(self):
        """缺少 op 欄位的 dict 被跳過。"""
        r = ReflectionOutput(memory_ops=[
            {"target": "x", "reason": "no op"},
        ])
        actions = parse_reflection_to_actions(r)
        assert len(actions) == 0

    def test_non_dict_items_skipped(self):
        """非 dict 的 items 被跳過。"""
        r = ReflectionOutput(memory_ops=[
            "not_a_dict",
            42,
            None,
        ])
        actions = parse_reflection_to_actions(r)
        assert len(actions) == 0

    def test_invalid_params_fallback(self):
        """params 不是 dict 時 fallback 為空 dict。"""
        r = ReflectionOutput(memory_ops=[
            {"op": "add_note", "params": "invalid"},
        ])
        actions = parse_reflection_to_actions(r)
        assert len(actions) == 1
        assert actions[0]["params"] == {}

    def test_mixed_valid_and_invalid(self):
        """混合有效和無效 ops，只保留有效的（白名單過濾 + 截斷）。"""
        r = ReflectionOutput(memory_ops=[
            {"op": "add_note", "params": {"content": "ok"}},
            {"target": "no_op"},
            {"op": "tag_concept", "target": "概念A"},
        ])
        actions = parse_reflection_to_actions(r)
        assert len(actions) == 2
        assert actions[0]["action_type"] == "add_note"
        assert actions[1]["action_type"] == "tag_concept"

    def test_max_ops_truncation(self):
        """超過 3 個 ops 時截斷。"""
        r = ReflectionOutput(memory_ops=[
            {"op": "add_note", "params": {"content": "1"}},
            {"op": "add_note", "params": {"content": "2"}},
            {"op": "add_note", "params": {"content": "3"}},
            {"op": "add_note", "params": {"content": "4"}},
        ])
        actions = parse_reflection_to_actions(r)
        assert len(actions) == 3

    def test_unknown_op_rejected(self):
        """不在白名單的 op 被跳過。"""
        r = ReflectionOutput(memory_ops=[
            {"op": "destroy_world", "target": "x"},
        ])
        actions = parse_reflection_to_actions(r)
        assert len(actions) == 0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ReflectionExecutor 行動測試
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestExecutorActions:
    """測試各 executor 行動。"""

    def test_adjust_salience(self, executor, mock_engine):
        """adjust_salience 正確呼叫 update_salience。"""
        fn = executor.build_executor_fn()
        result = fn("adjust_salience", "ep_test123", {"new_salience": 0.9})

        mock_engine.episodic.update_salience.assert_called_once_with(
            "ep_test123", 0.9
        )
        assert result["old_salience"] == 0.5
        assert result["new_salience"] == 0.9

    def test_adjust_salience_clamp(self, executor, mock_engine):
        """adjust_salience 將值 clamp 到 0-1。"""
        fn = executor.build_executor_fn()
        fn("adjust_salience", "ep_test123", {"new_salience": 1.5})
        mock_engine.episodic.update_salience.assert_called_once_with(
            "ep_test123", 1.0
        )

    def test_adjust_salience_not_found(self, executor, mock_engine):
        """adjust_salience 目標不存在時拋出 ValueError。"""
        mock_engine.episodic.get.return_value = None
        fn = executor.build_executor_fn()
        with pytest.raises(ValueError, match="Episode 不存在"):
            fn("adjust_salience", "ep_ghost", {"new_salience": 0.5})

    def test_tag_concept(self, executor, mock_engine, mock_inhibitor):
        """tag_concept 委派 Patch Gate 追加標籤，不直寫 SemanticMemory。"""
        mock_engine.semantic.get_by_name.return_value = FakeConcept(
            metadata={"tags": ["existing"]}
        )
        mock_inhibitor.process_reflection_patch.return_value = (
            True, {"old_tags": ["existing"], "new_tags": ["existing", "new_tag"]},
        )
        fn = executor.build_executor_fn()
        result = fn("tag_concept", "測試概念", {"tags": ["new_tag"]})

        assert "existing" in result["old_tags"]
        assert "new_tag" in result["new_tags"]
        mock_inhibitor.process_reflection_patch.assert_called_once()
        assert mock_inhibitor.process_reflection_patch.call_args.kwargs["operation"] == "tag"
        mock_engine.semantic.upsert_concept.assert_not_called()

    def test_tag_concept_not_found(self, executor, mock_engine):
        """tag_concept 概念不存在時拋出 ValueError。"""
        mock_engine.semantic.get_by_name.return_value = None
        fn = executor.build_executor_fn()
        with pytest.raises(ValueError, match="概念不存在"):
            fn("tag_concept", "不存在", {"tags": ["x"]})

    def test_add_note(self, executor, mock_soul_notes):
        """add_note 正確建立 SoulNote。"""
        fn = executor.build_executor_fn()
        result = fn("add_note", "", {"content": "反思筆記內容"})

        assert result["written"] is True
        assert result["content_length"] > 0
        mock_soul_notes.add_note.assert_called_once()

    def test_add_note_no_soul_notes(self, mock_engine):
        """add_note 在 SoulNoteManager 未配置時拋出 RuntimeError。"""
        executor = ReflectionExecutor(engine=mock_engine, soul_notes=None)
        fn = executor.build_executor_fn()
        with pytest.raises(RuntimeError, match="SoulNoteManager 未配置"):
            fn("add_note", "", {"content": "test"})

    def test_add_note_empty_content(self, executor):
        """add_note 空 content 拋出 ValueError。"""
        fn = executor.build_executor_fn()
        with pytest.raises(ValueError, match="缺少 content"):
            fn("add_note", "", {"content": ""})

    def test_merge_concepts(self, executor, mock_engine, mock_inhibitor):
        """merge_concepts 委派 Patch Gate 合併，不直寫也不硬刪。"""
        primary = FakeConcept(
            name="主概念",
            description="主描述",
            source_episode_ids=["ep_1"],
        )
        secondary = FakeConcept(
            name="次概念",
            description="次描述",
            source_episode_ids=["ep_2"],
        )
        mock_engine.semantic.get_by_name.side_effect = lambda n: (
            primary if n == "主概念" else secondary if n == "次概念" else None
        )

        mock_inhibitor.process_reflection_patch.return_value = (
            True, {"primary": "主概念", "secondary_removed": "次概念"},
        )
        fn = executor.build_executor_fn()
        result = fn("merge_concepts", "主概念", {"merge_with": "次概念"})

        assert result["primary"] == "主概念"
        assert result["secondary_removed"] == "次概念"
        call = mock_inhibitor.process_reflection_patch.call_args.kwargs
        assert call["operation"] == "merge"
        assert call["params"] == {"merge_with": "次概念"}
        mock_engine.semantic.upsert_concept.assert_not_called()
        mock_engine.semantic.remove_concept.assert_not_called()

    def test_merge_concepts_self_merge_rejected(self, executor):
        """自我合併（target == merge_with）被拒絕。"""
        fn = executor.build_executor_fn()
        with pytest.raises(ValueError, match="不允許自我合併"):
            fn("merge_concepts", "Python", {"merge_with": "Python"})

    def test_merge_concepts_missing_merge_with(self, executor):
        """merge_concepts 缺少 merge_with 拋出 ValueError。"""
        fn = executor.build_executor_fn()
        with pytest.raises(ValueError, match="缺少 merge_with"):
            fn("merge_concepts", "主概念", {})

    def test_archive_episode(self, executor, mock_engine):
        """archive_episode 使用原子性 archive_episode 方法。"""
        mock_engine.episodic.archive_episode.return_value = True
        fn = executor.build_executor_fn()
        result = fn("archive_episode", "ep_test123", {})

        assert result["old_salience"] == 0.5
        assert result["new_salience"] == 0.05
        assert result["archived"] is True
        mock_engine.episodic.archive_episode.assert_called_once_with("ep_test123")

    def test_create_concept(self, executor, mock_engine, mock_inhibitor):
        """create_concept 委派 Patch Gate 建立新概念（目標不存在時）。"""
        # get_by_name 回傳 None 表示概念不存在
        mock_engine.semantic.get_by_name.return_value = None
        mock_inhibitor.process_reflection_patch.return_value = (
            True, {"concept_id": "concept_test", "name": "新概念"},
        )
        fn = executor.build_executor_fn()
        result = fn("create_concept", "新概念", {"description": "新描述"})

        assert result["concept_id"] == "concept_test"
        assert result["name"] == "新概念"
        assert mock_inhibitor.process_reflection_patch.call_args.kwargs["operation"] == "create"
        mock_engine.semantic.upsert_concept.assert_not_called()

    def test_create_concept_already_exists(self, executor, mock_engine):
        """create_concept 同名概念已存在時拒絕。"""
        # mock 預設回傳 FakeConcept（表示已存在）
        fn = executor.build_executor_fn()
        with pytest.raises(ValueError, match="已存在"):
            fn("create_concept", "測試概念", {"description": "覆寫"})

    def test_create_concept_no_description(self, executor, mock_engine):
        """create_concept 缺少 description 拋出 ValueError。"""
        mock_engine.semantic.get_by_name.return_value = None
        fn = executor.build_executor_fn()
        with pytest.raises(ValueError, match="缺少 description"):
            fn("create_concept", "新概念", {})

    def test_unknown_action_type(self, executor):
        """未知 action_type 拋出 ValueError。"""
        fn = executor.build_executor_fn()
        with pytest.raises(ValueError, match="未知的 action_type"):
            fn("destroy_world", "target", {})


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# capture_pre_state 測試
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestCapturePreState:
    """測試 pre_state 快照捕捉。"""

    def test_adjust_salience_pre_state(self, executor, mock_engine):
        """adjust_salience 捕捉舊 salience。"""
        state = executor.capture_pre_state(
            "adjust_salience", "ep_test123", {}
        )
        assert state["salience_score"] == 0.5

    def test_tag_concept_pre_state(self, executor, mock_engine):
        """tag_concept 捕捉現有 tags。"""
        mock_engine.semantic.get_by_name.return_value = FakeConcept(
            metadata={"tags": ["old_tag"]}
        )
        state = executor.capture_pre_state(
            "tag_concept", "測試概念", {}
        )
        assert state["tags"] == ["old_tag"]

    def test_merge_concepts_pre_state(self, executor, mock_engine):
        """merge_concepts 捕捉兩個概念的 snapshot。"""
        primary = FakeConcept(name="A", description="descA")
        secondary = FakeConcept(name="B", description="descB")
        mock_engine.semantic.get_by_name.side_effect = lambda n: (
            primary if n == "A" else secondary if n == "B" else None
        )
        state = executor.capture_pre_state(
            "merge_concepts", "A", {"merge_with": "B"}
        )
        assert state["primary"]["name"] == "A"
        assert state["secondary"]["name"] == "B"

    def test_archive_episode_pre_state(self, executor, mock_engine):
        """archive_episode 捕捉 salience 和 archived 狀態。"""
        state = executor.capture_pre_state(
            "archive_episode", "ep_test123", {}
        )
        assert state["salience_score"] == 0.5
        assert state["archived"] is False

    def test_create_concept_pre_state_new(self, executor, mock_engine):
        """create_concept 目標不存在時 pre_state 為空。"""
        mock_engine.semantic.get_by_name.return_value = None
        state = executor.capture_pre_state(
            "create_concept", "不存在的概念", {}
        )
        assert state == {}

    def test_create_concept_pre_state_exists(self, executor, mock_engine):
        """create_concept 目標已存在時捕捉描述。"""
        state = executor.capture_pre_state(
            "create_concept", "已存在概念", {}
        )
        assert state["already_exists"] is True

    def test_target_not_found_returns_empty(self, executor, mock_engine):
        """目標不存在時回傳空 dict（不拋出例外）。"""
        mock_engine.episodic.get.return_value = None
        state = executor.capture_pre_state(
            "adjust_salience", "ep_ghost", {}
        )
        assert state == {}

    def test_add_note_pre_state_has_note_id(self, executor):
        """add_note 預先配發 note_id（回滾撤回用），且同步寫入 params。"""
        params = {}
        state = executor.capture_pre_state("add_note", "", params)
        assert state["note_id"].startswith("note_")
        assert params["_note_id"] == state["note_id"]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ActionQueue 整合測試
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestActionQueueIntegration:
    """測試 Executor 與 ActionQueue 的整合。"""

    def test_auto_action_executes(self, executor, mock_engine):
        """AUTO 行動在 process_auto_actions 中自動執行。"""
        queue = ReflectionActionQueue()
        fn = executor.build_executor_fn()

        action = queue.enqueue(
            action_type="adjust_salience",
            target_id="ep_test123",
            params={"new_salience": 0.9},
            reasoning="測試",
        )
        assert action.permission == ActionPermission.AUTO

        executed = queue.process_auto_actions(fn)
        assert len(executed) == 1
        assert executed[0].status == ActionStatus.EXECUTED

    def test_confirm_action_requires_confirmation(self, executor, mock_engine, mock_inhibitor):
        """CONFIRM 行動需要先確認才能執行。"""
        # 設定正確的 mock 數據：不同名稱回傳不同 concept
        primary = FakeConcept(name="主概念", description="主描述")
        secondary = FakeConcept(name="次概念", description="次描述")
        mock_engine.semantic.get_by_name.side_effect = lambda n: (
            primary if n == "主概念" else secondary if n == "次概念" else None
        )

        queue = ReflectionActionQueue()
        fn = executor.build_executor_fn()

        action = queue.enqueue(
            action_type="merge_concepts",
            target_id="主概念",
            params={"merge_with": "次概念"},
            reasoning="概念重複",
        )
        assert action.permission == ActionPermission.CONFIRM

        # 自動執行不會處理 CONFIRM 行動
        executed = queue.process_auto_actions(fn)
        assert len(executed) == 0

        # 確認後才能執行
        queue.confirm(action.id)
        assert queue.execute(action, executor_fn=fn) is True
        mock_inhibitor.process_reflection_patch.assert_called_once()
        mock_engine.semantic.remove_concept.assert_not_called()

    def test_deny_action_rejected(self, executor):
        """DENY 行動在 enqueue 時即被拒絕。"""
        queue = ReflectionActionQueue()

        action = queue.enqueue(
            action_type="delete_episode",
            target_id="ep_test123",
            reasoning="測試禁止操作",
        )
        assert action.status == ActionStatus.REJECTED

    def test_pre_state_injected(self, executor, mock_engine):
        """pre_state 在 enqueue 後正確注入。"""
        queue = ReflectionActionQueue()

        pre_state = executor.capture_pre_state(
            "adjust_salience", "ep_test123", {}
        )
        action = queue.enqueue(
            action_type="adjust_salience",
            target_id="ep_test123",
            params={"new_salience": 0.8},
        )
        action.pre_state = pre_state

        assert action.pre_state["salience_score"] == 0.5

    def test_full_pipeline(self, executor, mock_engine):
        """完整 pipeline：parse → enqueue → execute。"""
        reflection = ReflectionOutput(
            action="optimize",
            content="整理概念",
            reasoning="定期反思",
            memory_ops=[
                {
                    "op": "adjust_salience",
                    "target": "ep_test123",
                    "params": {"new_salience": 0.9},
                    "reason": "重要",
                },
                {
                    "op": "add_note",
                    "params": {"content": "反思筆記"},
                    "reason": "記錄",
                },
            ],
        )

        queue = ReflectionActionQueue()
        fn = executor.build_executor_fn()

        ops = parse_reflection_to_actions(reflection)
        assert len(ops) == 2

        for op_dict in ops:
            pre_state = executor.capture_pre_state(
                op_dict["action_type"],
                op_dict["target_id"],
                op_dict["params"],
            )
            action = queue.enqueue(
                action_type=op_dict["action_type"],
                target_id=op_dict["target_id"],
                params=op_dict["params"],
                reasoning=op_dict["reasoning"],
            )
            action.pre_state = pre_state

        # 兩個都是 AUTO
        executed = queue.process_auto_actions(fn)
        assert len(executed) == 2

    def test_audit_log_written(self, executor, mock_engine, tmp_path):
        """稽核日誌正確寫入。"""
        audit_file = tmp_path / "test_audit.jsonl"
        queue = ReflectionActionQueue(audit_path=audit_file)
        fn = executor.build_executor_fn()

        queue.enqueue(
            action_type="adjust_salience",
            target_id="ep_test123",
            params={"new_salience": 0.7},
        )
        queue.process_auto_actions(fn)

        assert audit_file.exists()
        lines = audit_file.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) >= 1
        entry = json.loads(lines[-1])
        assert entry["action_type"] == "adjust_salience"
        assert entry["status"] == "executed"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ReflectionOutput 向下相容測試
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestReflectionOutputCompat:
    """測試 ReflectionOutput 新欄位的向下相容性。"""

    def test_default_memory_ops_empty(self):
        """預設 memory_ops 為空列表。"""
        r = ReflectionOutput()
        assert r.memory_ops == []

    def test_heuristic_reflect_has_empty_ops(self):
        """無 LLM 的啟發式反思回傳空 memory_ops。"""
        rm = ReflectionModule()
        result = rm.reflect(["test summary"])
        assert result.memory_ops == []

    def test_llm_reflect_no_memory_ops_in_json(self):
        """LLM 回傳的 JSON 不含 memory_ops 時不崩潰。"""
        mock_llm = MagicMock(return_value=json.dumps({
            "action": "question",
            "content": "測試問題？",
            "reasoning": "好奇",
        }))
        rm = ReflectionModule(llm_fn=mock_llm)
        result = rm.reflect(["test"])
        assert result.memory_ops == []

    def test_llm_reflect_with_memory_ops(self):
        """LLM 回傳帶 memory_ops 的 JSON 正確解析。"""
        mock_llm = MagicMock(return_value=json.dumps({
            "action": "optimize",
            "content": "整理",
            "reasoning": "反思",
            "memory_ops": [
                {"op": "add_note", "params": {"content": "test"}},
            ],
        }))
        rm = ReflectionModule(llm_fn=mock_llm)
        result = rm.reflect(["test"])
        assert len(result.memory_ops) == 1
        assert result.memory_ops[0]["op"] == "add_note"

    def test_llm_reflect_memory_ops_not_list(self):
        """LLM 回傳 memory_ops 不是 list 時 fallback 為空。"""
        mock_llm = MagicMock(return_value=json.dumps({
            "action": "none",
            "content": "",
            "reasoning": "ok",
            "memory_ops": "not_a_list",
        }))
        rm = ReflectionModule(llm_fn=mock_llm)
        result = rm.reflect(["test"])
        assert result.memory_ops == []
