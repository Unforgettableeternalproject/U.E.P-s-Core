"""
ReflectionExecutor — 反思行動閉環的執行器

將 ReflectionModule 輸出的結構化 memory_ops 映射到 MemoryEngine API。
搭配 ReflectionActionQueue 使用，提供：
  - executor_fn：傳入 ActionQueue.execute() 的回調
  - capture_pre_state()：在 enqueue 前捕捉目標物件的快照
  - parse_reflection_to_actions()：將 ReflectionOutput.memory_ops 轉為 enqueue 參數

設計原則：
  - Executor 只做映射，不做決策（決策已由 LLM + ActionQueue 權限完成）
  - 目標不存在時拋出 ValueError（讓 ActionQueue 標記 FAILED）
  - 所有寫入走 MemoryEngine 公開 API，不繞過 storage_lock
"""

from __future__ import annotations

import logging
from typing import Any

from echo_memory.affect.reflection import ReflectionOutput

logger = logging.getLogger("echo_memory.affect.reflection_executor")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# parse_reflection_to_actions
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


# 允許的操作類型白名單
_ALLOWED_OPS = frozenset({
    "adjust_salience", "tag_concept", "add_note",
    "merge_concepts", "archive_episode", "create_concept",
})

# 單次反思最多允許的操作數量
_MAX_OPS = 3


def parse_reflection_to_actions(
    reflection: ReflectionOutput,
) -> list[dict[str, Any]]:
    """
    將 ReflectionOutput.memory_ops 轉為 ActionQueue 可接受的 enqueue 參數列表。

    每個返回項目包含：
      - action_type: str
      - target_id: str
      - params: dict
      - reasoning: str

    安全限制：
      - 最多 _MAX_OPS（3）個操作，超過的截斷並記 warning
      - op_type 必須在 _ALLOWED_OPS 白名單中
      - 無效的 op（缺少 op 欄位 / 不在白名單）被跳過

    Args:
        reflection: 反思模組的輸出

    Returns:
        轉換後的行動參數列表
    """
    raw_ops = reflection.memory_ops
    if len(raw_ops) > _MAX_OPS:
        logger.warning(
            "[ReflectionExecutor] memory_ops 超過上限（%d > %d），截斷",
            len(raw_ops), _MAX_OPS,
        )
        raw_ops = raw_ops[:_MAX_OPS]

    actions: list[dict[str, Any]] = []

    for i, op in enumerate(raw_ops):
        if not isinstance(op, dict):
            logger.warning(
                "[ReflectionExecutor] memory_ops[%d] 不是 dict，跳過: %s",
                i, type(op).__name__,
            )
            continue

        op_type = op.get("op", "")
        if not op_type:
            logger.warning(
                "[ReflectionExecutor] memory_ops[%d] 缺少 'op' 欄位，跳過",
                i,
            )
            continue

        if op_type not in _ALLOWED_OPS:
            logger.warning(
                "[ReflectionExecutor] memory_ops[%d] 未知操作 '%s'，跳過",
                i, op_type,
            )
            continue

        actions.append({
            "action_type": str(op_type),
            "target_id": str(op.get("target", "")),
            "params": op.get("params", {}) if isinstance(op.get("params"), dict) else {},
            "reasoning": str(op.get("reason", "")),
        })

    return actions


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ReflectionExecutor
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class ReflectionExecutor:
    """
    反思行動的執行層——將 action_type 映射到 MemoryEngine API。

    概念相關操作（create_concept / tag_concept / merge_concepts）優先走
    ConceptInhibitor patch pipeline，確保所有 SemanticMemory 變更都經過
    audit trail。inhibitor=None 時退回直接寫入（向下相容）。

    Usage:
        executor = ReflectionExecutor(
            engine=engine,
            soul_notes=soul_notes,
            concept_inhibitor=inhibitor,
        )
        fn = executor.build_executor_fn()
        action_queue.execute(action, executor_fn=fn)
    """

    def __init__(
        self,
        engine: Any,
        soul_notes: Any | None = None,
        concept_inhibitor: Any | None = None,
    ):
        """
        Args:
            engine: MemoryEngine 實例
            soul_notes: SoulNoteManager 實例（用於 add_note 操作）
            concept_inhibitor: ConceptInhibitor 實例（可選，用於概念變更 audit）
        """
        self._engine = engine
        self._soul_notes = soul_notes
        self._inhibitor = concept_inhibitor

        # action_type → 執行方法映射
        self._dispatch: dict[str, Any] = {
            "adjust_salience": self._exec_adjust_salience,
            "tag_concept": self._exec_tag_concept,
            "add_note": self._exec_add_note,
            "merge_concepts": self._exec_merge_concepts,
            "archive_episode": self._exec_archive_episode,
            "create_concept": self._exec_create_concept,
        }

    def build_executor_fn(self):
        """
        回傳可傳入 ActionQueue.execute() 的回調函式。

        簽章：(action_type: str, target_id: str, params: dict) → dict
        """
        def executor_fn(
            action_type: str, target_id: str, params: dict[str, Any]
        ) -> dict[str, Any]:
            handler = self._dispatch.get(action_type)
            if handler is None:
                raise ValueError(f"未知的 action_type: {action_type}")
            return handler(target_id, params)

        return executor_fn

    def build_rollback_fn(self):
        """
        回傳可傳入 ActionQueue.rollback() 的回調函式。

        根據 pre_state 還原目標物件到執行前狀態。
        簽章：(action_type: str, target_id: str, pre_state: dict) → None
        """
        def rollback_fn(
            action_type: str, target_id: str, pre_state: dict[str, Any]
        ) -> None:
            if action_type == "adjust_salience":
                old = pre_state.get("salience_score")
                if old is not None:
                    self._engine.episodic.update_salience(target_id, old)

            elif action_type == "tag_concept":
                old_tags = pre_state.get("tags")
                if old_tags is not None:
                    concept = self._engine.semantic.get_by_name(target_id)
                    if concept:
                        concept.metadata["tags"] = old_tags
                        self._engine.semantic.upsert_concept(
                            name=concept.name,
                            description=concept.description,
                            concept_type=concept.concept_type,
                            metadata=concept.metadata,
                        )

            elif action_type == "archive_episode":
                old_salience = pre_state.get("salience_score")
                if old_salience is not None:
                    self._engine.episodic.update_salience(target_id, old_salience)
                self._engine.episodic.update_episode_metadata(
                    target_id, {"archived": False, "archived_by": None}
                )

            elif action_type == "create_concept":
                if not pre_state.get("already_exists"):
                    self._engine.semantic.remove_concept(target_id)

            elif action_type == "add_note":
                # SoulNote 不支援刪除，rollback 無法還原
                logger.warning(
                    "[ReflectionExecutor] add_note 不支援 rollback"
                )

            elif action_type == "merge_concepts":
                # merge 的 rollback 需要完整的 secondary 資料，
                # 目前 pre_state 只保留 name/description，無法完整還原。
                logger.warning(
                    "[ReflectionExecutor] merge_concepts rollback 尚不完整，"
                    "僅記錄狀態"
                )

            else:
                logger.warning(
                    "[ReflectionExecutor] 未知 action_type 的 rollback: %s",
                    action_type,
                )

        return rollback_fn

    def capture_pre_state(
        self, action_type: str, target_id: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        """
        在執行前捕捉目標物件的快照，供 rollback 使用。

        Args:
            action_type: 行動類型
            target_id: 目標 ID
            params: 行動參數

        Returns:
            pre_state dict（目標不存在時回傳空 dict）
        """
        try:
            if action_type == "adjust_salience":
                ep = self._engine.episodic.get(target_id)
                if ep:
                    return {"salience_score": ep.salience_score}

            elif action_type == "tag_concept":
                concept = self._engine.semantic.get_by_name(target_id)
                if concept:
                    return {
                        "tags": list(concept.metadata.get("tags", [])),
                    }

            elif action_type == "merge_concepts":
                primary = self._engine.semantic.get_by_name(target_id)
                merge_with = params.get("merge_with", "")
                secondary = (
                    self._engine.semantic.get_by_name(merge_with)
                    if merge_with else None
                )
                state: dict[str, Any] = {}
                if primary:
                    state["primary"] = {
                        "name": primary.name,
                        "description": primary.description,
                    }
                if secondary:
                    state["secondary"] = {
                        "name": secondary.name,
                        "description": secondary.description,
                    }
                return state

            elif action_type == "archive_episode":
                ep = self._engine.episodic.get(target_id)
                if ep:
                    return {
                        "salience_score": ep.salience_score,
                        "archived": ep.metadata.get("archived", False),
                    }

            elif action_type == "create_concept":
                existing = self._engine.semantic.get_by_name(target_id)
                if existing:
                    return {
                        "already_exists": True,
                        "description": existing.description,
                    }

        except Exception as e:
            logger.debug(
                "[ReflectionExecutor] pre_state 捕捉失敗 (%s on %s): %s",
                action_type, target_id, e,
            )

        return {}

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 各行動的具體實作
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _exec_adjust_salience(
        self, target_id: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        """調整 episode 的顯著性分數。"""
        new_salience = float(params.get("new_salience", 0.5))
        new_salience = min(1.0, max(0.0, new_salience))

        ep = self._engine.episodic.get(target_id)
        if ep is None:
            raise ValueError(f"Episode 不存在: {target_id}")

        old_salience = ep.salience_score
        ok = self._engine.episodic.update_salience(target_id, new_salience)
        if not ok:
            raise RuntimeError(f"update_salience 失敗: {target_id}")

        return {"old_salience": old_salience, "new_salience": new_salience}

    def _exec_tag_concept(
        self, target_id: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        """為概念追加標籤。優先走 ConceptInhibitor patch pipeline。"""
        concept = self._engine.semantic.get_by_name(target_id)
        if concept is None:
            raise ValueError(f"概念不存在: {target_id}")

        new_tags = params.get("tags", [])
        if not isinstance(new_tags, list):
            new_tags = [str(new_tags)]

        # 走 ConceptInhibitor patch pipeline（有 audit trail）
        if self._inhibitor is not None:
            success, result = self._inhibitor.process_reflection_patch(
                operation="tag",
                target_name=target_id,
                params={"tags": new_tags},
                reasoning="ReflectionExecutor tag_concept",
            )
            if not success:
                raise RuntimeError(
                    f"ConceptInhibitor tag 失敗: {result.get('error', '未知')}"
                )
            return result

        # fallback: 直接寫入（inhibitor=None 時向下相容）
        existing_tags = list(concept.metadata.get("tags", []))
        merged_tags = list(set(existing_tags + new_tags))
        concept.metadata["tags"] = merged_tags
        self._engine.semantic.upsert_concept(
            name=concept.name,
            description=concept.description,
            concept_type=concept.concept_type,
            metadata=concept.metadata,
        )

        return {"old_tags": existing_tags, "new_tags": merged_tags}

    def _exec_add_note(
        self, _target_id: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        """寫入一則潛意識筆記。"""
        if self._soul_notes is None:
            raise RuntimeError("SoulNoteManager 未配置，無法寫入 note")

        from echo_memory.affect.soul_note import SoulNote

        content = str(params.get("content", ""))
        if not content:
            raise ValueError("add_note 缺少 content 參數")

        self._soul_notes.add_note(SoulNote(
            category="reflection_action",
            content=content,
            metadata={"source": "reflection_executor"},
            tags=["reflection", "auto"],
        ))

        return {"written": True, "content_length": len(content)}

    def _exec_merge_concepts(
        self, target_id: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        """合併兩個概念（主概念吸收次概念）。優先走 ConceptInhibitor patch pipeline。"""
        merge_with = str(params.get("merge_with", ""))
        if not merge_with:
            raise ValueError("merge_concepts 缺少 merge_with 參數")

        if target_id == merge_with:
            raise ValueError(
                f"merge_concepts 不允許自我合併: {target_id}"
            )

        primary = self._engine.semantic.get_by_name(target_id)
        if primary is None:
            raise ValueError(f"主概念不存在: {target_id}")

        secondary = self._engine.semantic.get_by_name(merge_with)
        if secondary is None:
            raise ValueError(f"次概念不存在: {merge_with}")

        # 走 ConceptInhibitor patch pipeline（有 audit trail）
        if self._inhibitor is not None:
            success, result = self._inhibitor.process_reflection_patch(
                operation="merge",
                target_name=target_id,
                params={"merge_with": merge_with},
                reasoning="ReflectionExecutor merge_concepts",
            )
            if not success:
                raise RuntimeError(
                    f"ConceptInhibitor merge 失敗: {result.get('error', '未知')}"
                )
            return result

        # fallback: 直接寫入（inhibitor=None 時向下相容）
        merged_desc = (
            f"{primary.description}\n\n"
            f"（合併自「{secondary.name}」）{secondary.description}"
        )
        merged_meta = dict(primary.metadata)
        secondary_facts = secondary.metadata.get("facts", [])
        if secondary_facts:
            existing_facts = merged_meta.get("facts", [])
            merged_meta["facts"] = existing_facts + secondary_facts
        merged_meta["merged_from"] = merge_with
        merged_meta["merged_source_episodes"] = list(set(
            primary.source_episode_ids + secondary.source_episode_ids
        ))

        original_desc = primary.description
        original_meta = dict(primary.metadata)

        self._engine.semantic.upsert_concept(
            name=primary.name,
            description=merged_desc,
            concept_type=primary.concept_type,
            metadata=merged_meta,
        )

        removed = self._engine.semantic.remove_concept(merge_with)
        if not removed:
            logger.warning(
                "[ReflectionExecutor] merge remove 失敗，還原 primary: %s",
                primary.name,
            )
            self._engine.semantic.upsert_concept(
                name=primary.name,
                description=original_desc,
                concept_type=primary.concept_type,
                metadata=original_meta,
            )
            raise RuntimeError(
                f"merge_concepts 失敗：次概念移除失敗 ({merge_with})，"
                f"主概念已還原"
            )

        return {
            "primary": target_id,
            "secondary_removed": merge_with,
            "merged_description_length": len(merged_desc),
        }

    def _exec_archive_episode(
        self, target_id: str, _params: dict[str, Any]
    ) -> dict[str, Any]:
        """歸檔 episode（原子性：一次 lock 內完成 salience 降低 + metadata 標記）。"""
        ep = self._engine.episodic.get(target_id)
        if ep is None:
            raise ValueError(f"Episode 不存在: {target_id}")

        old_salience = ep.salience_score

        # 原子操作：一次 lock 內完成 salience + metadata 更新
        ok = self._engine.episodic.archive_episode(target_id)
        if not ok:
            raise RuntimeError(f"archive_episode 失敗: {target_id}")

        return {
            "old_salience": old_salience,
            "new_salience": 0.05,
            "archived": True,
        }

    def _exec_create_concept(
        self, target_id: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        """建立新概念（若同名概念已存在則拒絕）。優先走 ConceptInhibitor patch pipeline。"""
        description = str(params.get("description", ""))
        if not description:
            raise ValueError("create_concept 缺少 description 參數")

        existing = self._engine.semantic.get_by_name(target_id)
        if existing is not None:
            raise ValueError(
                f"create_concept 拒絕：同名概念「{target_id}」已存在，"
                f"不允許覆寫（使用 tag_concept 或 merge_concepts 修改既有概念）"
            )

        # 走 ConceptInhibitor patch pipeline（有 audit trail）
        if self._inhibitor is not None:
            success, result = self._inhibitor.process_reflection_patch(
                operation="create",
                target_name=target_id,
                params={
                    "description": description,
                    "concept_type": params.get("concept_type", "reflection"),
                },
                reasoning="ReflectionExecutor create_concept",
            )
            if not success:
                raise RuntimeError(
                    f"ConceptInhibitor create 失敗: {result.get('error', '未知')}"
                )
            return result

        # fallback: 直接寫入（inhibitor=None 時向下相容）
        concept_id = self._engine.semantic.upsert_concept(
            name=target_id,
            description=description,
            concept_type=params.get("concept_type", "reflection"),
            metadata={"created_by": "reflection_executor"},
        )

        return {"concept_id": concept_id, "name": target_id}
