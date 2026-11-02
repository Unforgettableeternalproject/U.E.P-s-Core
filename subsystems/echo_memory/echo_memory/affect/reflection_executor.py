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

import contextlib
import copy
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from echo_memory.affect.concept_patch import (
    ConceptPatch,
    ExpandMode,
    PatchEvidence,
    PatchHistoryEntry,
    PatchTransaction,
)
from echo_memory.affect.reflection import ReflectionOutput
from echo_memory.storage.locking import storage_lock

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
    *,
    cycle_id: str | None = None,
    source_episode_ids: list[str] | None = None,
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
        cycle_id: 本次反思週期 ID（寫入 params["_provenance"]，供 EpisodeGate 記錄來源）
        source_episode_ids: 觸發本次反思的 episode IDs

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
    provenance: dict[str, Any] | None = None
    if cycle_id or source_episode_ids:
        provenance = {
            "reflection_cycle_id": cycle_id,
            "source_episode_ids": list(source_episode_ids or []),
        }

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

        params = dict(op.get("params", {})) if isinstance(op.get("params"), dict) else {}
        if provenance is not None:
            params["_provenance"] = dict(provenance)
        actions.append({
            "action_type": str(op_type),
            "target_id": str(op.get("target", "")),
            "params": params,
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
        episode_gate: Any | None = None,
    ):
        """
        Args:
            engine: MemoryEngine 實例
            soul_notes: SoulNoteManager 實例（用於 add_note 操作）
            concept_inhibitor: ConceptInhibitor 實例（可選，用於概念變更 audit）
            episode_gate: EpisodeGate 實例（可選，adjust_salience／archive_episode／add_note 的
                audit-before-mutate 入口；未注入時依 engine.storage_path 建立）
        """
        self._engine = engine
        self._soul_notes = soul_notes
        self._inhibitor = concept_inhibitor
        self._episode_gate_inst = episode_gate

        # action_type → 執行方法映射
        self._dispatch: dict[str, Any] = {
            "adjust_salience": self._exec_adjust_salience,
            "tag_concept": self._exec_tag_concept,
            "add_note": self._exec_add_note,
            "merge_concepts": self._exec_merge_concepts,
            "archive_episode": self._exec_archive_episode,
            "create_concept": self._exec_create_concept,
        }

    def _gate(self) -> Any:
        """取得 Patch Gate（ConceptInhibitor）。未注入時沿用 engine 上的，仍沒有就建一個無 LLM 的。

        Reflection 來源的 patch 不需 LLM 佐證（process_reflection_patch 自動 approve），
        所以 llm_fn=None 的閘門足以保證每筆概念變更都留 PatchTransaction audit，
        不再有直寫 SemanticMemory 的旁路。
        """
        if self._inhibitor is None:
            ensure = getattr(self._engine, "ensure_initialized", None)
            if callable(ensure):
                ensure()  # 未注入 Gate 時由 engine 建立預設 Gate
            self._inhibitor = getattr(self._engine, "_concept_inhibitor", None)
        if self._inhibitor is None:
            from echo_memory.affect.concept_inhibitor import ConceptInhibitor

            self._inhibitor = ConceptInhibitor(semantic=self._engine.semantic, llm_fn=None)
        return self._inhibitor

    def _episode_gate(self) -> Any:
        """取得 EpisodeGate。audit 檔放在 engine.storage_path 下；無可用路徑時只留記憶體記錄。"""
        if self._episode_gate_inst is None:
            from echo_memory.affect.episode_gate import EpisodeGate

            storage_path = getattr(self._engine, "storage_path", None)
            audit_path = (
                Path(storage_path) / "episode_gate_audit.jsonl"
                if isinstance(storage_path, (str, Path)) else None
            )
            self._episode_gate_inst = EpisodeGate(audit_path)
        return self._episode_gate_inst

    @staticmethod
    def _gate_context(
        params: dict[str, Any] | None = None,
        pre_state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """組 EpisodeGate 的 audit 脈絡（行動 id、來源、理由）。前向取 params，回滾取 pre_state。"""
        src = params if params is not None else (pre_state or {})
        return {
            "action_id": src.get("_action_id") or src.get("action_id"),
            "provenance": src.get("_provenance") or src.get("provenance") or {},
            "reasoning": str(src.get("_reasoning") or src.get("reasoning") or ""),
        }

    def bind_action(self, action: Any) -> None:
        """把 queue 行動的 id／理由綁進 params 與 pre_state，讓 EpisodeGate 記得到行動 id。

        須在 enqueue 且設定 pre_state 之後、persist_action 之前呼叫；
        重建（_restore_pending_from_audit）後仍從持久化的 params／pre_state 取得。
        """
        action.params["_action_id"] = action.id
        action.params["_reasoning"] = action.reasoning
        if action.pre_state:
            action.pre_state["action_id"] = action.id
            action.pre_state["reasoning"] = action.reasoning

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
            # 概念變更在 storage_lock 內執行（鎖序 storage_lock -> semantic._lock）；
            # 這些 handler 不呼叫 LLM，持鎖不會卡住其他寫入者
            if action_type == "create_concept":
                params = self._with_similar_hint(target_id, params)
            with self._concept_guard(action_type):
                return handler(target_id, params)

        return executor_fn

    def _with_similar_hint(self, target_id: str, params: dict[str, Any]) -> dict[str, Any]:
        """create_concept 進 storage_lock 前先算好語意相近提示（embedding／LLM 都不得在鎖內）。

        只在 Gate 真的回傳 SimilarHint 時才帶入；失敗或 Gate 不支援則維持原 params
        （寫入端會自行評估，不影響 create）。
        """
        from echo_memory.affect.concept_inhibitor import SimilarHint

        # 同名概念已存在：_exec_create_concept 會直接拒絕，不必算 embedding
        if self._engine.semantic.get_by_name(target_id) is not None:
            return params
        try:
            hint = self._gate().evaluate_similar(
                target_id, str(params.get("description", "")), judge=False,
            )
        except Exception as e:
            logger.warning("[ReflectionExecutor] 語意相近預檢失敗（不影響 create）: %s", e)
            return params
        if not isinstance(hint, SimilarHint):
            return params
        return {**params, "similar_hint": hint}

    # 會改動 SemanticMemory／圖譜的 action：必須與 engine 其他寫入者序列化
    _CONCEPT_ACTIONS = frozenset({"tag_concept", "merge_concepts", "create_concept"})

    def _concept_guard(self, action_type: str):
        """概念類 action 回傳 storage_lock(engine.storage_path)；其餘（或無 storage_path）不加鎖。"""
        if action_type not in self._CONCEPT_ACTIONS:
            return contextlib.nullcontext()
        storage_path = getattr(self._engine, "storage_path", None)
        if not isinstance(storage_path, (str, Path)):
            return contextlib.nullcontext()
        # 進 storage_lock 前先建好引擎元件，避免持鎖時冷建構與他執行緒互相等待
        ensure = getattr(self._engine, "ensure_initialized", None)
        if callable(ensure):
            ensure()
        return storage_lock(storage_path)

    def _gated_undo(self, operation: str, target_name: str, params: dict[str, Any]) -> None:
        """回滾概念變更也走 Patch Gate：建 PatchTransaction 並寫入 Gate 的 audit log。

        失敗拋出，讓 ActionQueue 不誤報 ROLLED_BACK。
        """
        handlers = {
            "undo_create": self._undo_create,
            "undo_tag": self._undo_tag,
            "undo_merge": self._undo_merge,
        }
        tx = PatchTransaction(
            patches=[],
            user_input=f"[reflection] {operation}: {target_name}",
            llm_response="",
            session_id="reflection",
            turn_id="",
            triggered_by="reflection",
        )
        reasoning = f"ReflectionExecutor rollback ({operation})"
        try:
            success, result = handlers[operation](tx, target_name, params, reasoning)
            tx.status = "committed" if success else "failed"
        except Exception as e:
            tx.status = "failed"
            success, result = False, {"error": str(e)}
        if success and not tx.patches and result.get("removed") is not False:
            # 回滾確實改了資料卻沒有任何 patch 可記錄：audit 無法還原內容，至少留下警示
            logger.error("[ReflectionExecutor] 回滾 %s 成功但 audit 沒有 patch 記錄: %s", operation, target_name)
        self._gate()._write_audit(tx)
        if not success:
            raise RuntimeError(f"回滾 {operation} 失敗: {result.get('error', '未知')}")

    @staticmethod
    def _add_history(concept: Any, patch: ConceptPatch) -> None:
        """寫入輕量 patch_history 索引（與 ConceptInhibitor 同格式）。"""
        history = concept.metadata.setdefault("patch_history", [])
        history.append(PatchHistoryEntry(
            patch_id=patch.patch_id,
            transaction_id=patch.transaction_id,
            timestamp=datetime.now(timezone.utc),
            action=patch.action,
            old_confidence=patch.old_confidence,
            new_confidence=patch.new_confidence,
        ).to_dict())

    @staticmethod
    def _rollback_evidence(kind: str, reasoning: str, concept_ids: list[str]) -> PatchEvidence:
        return PatchEvidence(
            evidence_kind=kind,
            evidence_strength=0.8,
            verifier_status="valid",
            verifier_reason=reasoning,
            retrieved_concept_ids=concept_ids,
        )

    def _undo_create(
        self, tx: PatchTransaction, target_name: str, _params: dict[str, Any], reasoning: str,
    ) -> tuple[bool, dict[str, Any]]:
        """回滾 create：移除概念（remove_concept 會一併清理圖譜節點）。"""
        semantic = self._engine.semantic
        with semantic._lock:
            concept = semantic.get_by_name(target_name)
            if concept is None:
                return True, {"removed": False, "name": target_name}
            patch = ConceptPatch(
                transaction_id=tx.transaction_id,
                concept_id=concept.id,
                concept_name=target_name,
                action="deprecate",
                target_path="*",
                old_value={"name": concept.name, "description": concept.description},
                new_value={"rollback_of": "create", "removed": True},
                patch_confidence=0.8,
                evidence=self._rollback_evidence("reflection_create", reasoning, [concept.id]),
                risk_level="medium",
            )
            patch.status = "approved"
            tx.patches.append(patch)
            # 概念消失後就找不到投影：先撤回共享層的投影（M10）
            revoke = getattr(self._engine, "revoke_concept_projections", None)
            if concept is not None and callable(revoke):
                revoke(concept.id, "concept_removed")
            if not semantic.remove_concept(target_name):
                patch.status = "rejected"
                patch.rejection_reason = "remove_concept 失敗"
                return False, {"error": f"移除失敗: {target_name}"}
            patch.status = "applied"
            return True, {"removed": True, "name": target_name}

    def _undo_tag(
        self, tx: PatchTransaction, target_name: str, params: dict[str, Any], reasoning: str,
    ) -> tuple[bool, dict[str, Any]]:
        """回滾 tag：metadata.tags 還原成執行前的值。"""
        old_tags = params.get("tags")
        if not isinstance(old_tags, list):
            return False, {"error": "缺少 tags 參數"}
        semantic = self._engine.semantic
        with semantic._lock:
            concept = semantic.get_by_name(target_name)
            if concept is None:
                return False, {"error": f"概念不存在: {target_name}"}
            current_tags = list(concept.metadata.get("tags", []))
            patch = ConceptPatch(
                transaction_id=tx.transaction_id,
                concept_id=concept.id,
                concept_name=target_name,
                action="expand",
                expand_mode=ExpandMode.ADD_FACT,
                target_path="metadata.tags",
                old_value=current_tags,
                new_value=list(old_tags),
                old_confidence=concept.confidence,
                new_confidence=concept.confidence,
                patch_confidence=0.8,
                evidence=self._rollback_evidence("reflection_tag", reasoning, [concept.id]),
                risk_level="low",
                rollback_payload={"old_tags": current_tags},
            )
            patch.status = "approved"
            tx.patches.append(patch)
            try:
                concept.metadata["tags"] = list(old_tags)
                concept.updated_at = datetime.now(timezone.utc)
                self._add_history(concept, patch)
                concept.record_mutation()
                semantic._save()
            except Exception as e:
                patch.status = "rejected"
                patch.rejection_reason = str(e)
                concept.metadata["tags"] = current_tags
                return False, {"error": str(e)}
            patch.status = "applied"
            return True, {"old_tags": current_tags, "new_tags": list(old_tags)}

    @staticmethod
    def _capture_primary_state(primary: Any) -> dict[str, Any]:
        """擷取主概念會被 undo_merge 改動的欄位（深拷貝），供失敗時完整補償。"""
        return {
            "description": primary.description,
            "confidence": primary.confidence,
            "metadata": copy.deepcopy(primary.metadata),
            "embedding": list(primary.embedding) if primary.embedding is not None else None,
            "derivations": copy.deepcopy(primary.derivations),
            "updated_at": primary.updated_at,
            "version": primary.version,
            "mutation_count": primary.mutation_count,
        }

    @staticmethod
    def _restore_primary_state(semantic: Any, primary: Any, state: dict[str, Any]) -> None:
        """把主概念還原到 _capture_primary_state 的狀態並存檔；補償失敗只記錄，不再拋出。"""
        primary.description = state["description"]
        primary.confidence = state["confidence"]
        primary.metadata = copy.deepcopy(state["metadata"])
        primary.embedding = state["embedding"]
        primary.derivations = copy.deepcopy(state["derivations"])
        primary.updated_at = state["updated_at"]
        primary.version = state["version"]
        primary.mutation_count = state["mutation_count"]
        try:
            semantic._update_embedding_index(primary, is_update=True)
        except Exception as e:
            logger.error("[ReflectionExecutor] 補償 embedding index 失敗，改為重建: %s", e)
            try:
                semantic._rebuild_embeddings()
            except Exception as e2:
                logger.error("[ReflectionExecutor] 重建 embedding index 失敗: %s", e2)
        try:
            semantic._save()
        except Exception as e:
            logger.error("[ReflectionExecutor] 補償存檔失敗: %s", e)

    def _undo_merge(
        self, tx: PatchTransaction, target_name: str, params: dict[str, Any], reasoning: str,
    ) -> tuple[bool, dict[str, Any]]:
        """回滾 merge：次概念恢復 active（含圖譜節點），主概念還原為合併前版本。

        合併時 Gate 把主概念合併前快照（含合併完成後的 primary.version）存在次概念
        metadata["merge_primary_snapshot"]。主概念版本與快照不符（合併後又被改過）即拒絕回滾，
        以免蓋掉後續變更。

        順序：先備妥所有可能失敗的運算 -> 改 primary 並存檔 -> 最後才 restore_merged(secondary)；
        任一步失敗則補償 primary 與 secondary，記憶體與磁碟都回到回滾前。
        """
        merge_with = str(params.get("merge_with", ""))
        if not merge_with:
            return False, {"error": "缺少 merge_with 參數"}
        semantic = self._engine.semantic
        with semantic._lock:
            primary = semantic.get_by_name(target_name)
            secondary = semantic.get_by_name(merge_with)
            if primary is None or secondary is None:
                return False, {"error": f"概念不存在: {target_name} / {merge_with}"}
            snap = secondary.metadata.get("merge_primary_snapshot")
            if not snap or secondary.metadata.get("merged_into") != primary.id:
                return False, {"error": f"「{merge_with}」不是併入「{target_name}」的已合併概念"}
            expected_version = snap.get("merged_primary_version")
            if expected_version is not None:
                if primary.version != expected_version:
                    return False, {"error": f"「{target_name}」在合併後已被修改，拒絕回滾"}
            elif primary.description != snap.get("merged_description"):
                # 舊快照沒有版本欄位：退回只比對 description
                return False, {"error": f"「{target_name}」在合併後已被修改，拒絕回滾"}

            evidence = self._rollback_evidence(
                "reflection_merge", reasoning, [primary.id, secondary.id],
            )
            modify_patch = ConceptPatch(
                transaction_id=tx.transaction_id,
                concept_id=primary.id,
                concept_name=target_name,
                action="modify",
                target_path="description",
                old_value=primary.description,
                new_value=snap.get("old_description"),
                old_confidence=primary.confidence,
                new_confidence=snap.get("old_confidence"),
                patch_confidence=0.8,
                evidence=evidence,
                risk_level="high",
            )
            unmerge_patch = ConceptPatch(
                transaction_id=tx.transaction_id,
                concept_id=secondary.id,
                concept_name=merge_with,
                action="merge",
                target_path="*",
                old_value={"merged_into": primary.id},
                new_value={"rollback_of": "merge", "restored": True},
                merge_target_id=primary.id,
                patch_confidence=0.8,
                evidence=evidence,
                risk_level="high",
            )
            for p in (modify_patch, unmerge_patch):
                p.status = "approved"
                tx.patches.append(p)

            def _fail(msg: str, err: Exception) -> tuple[bool, dict[str, Any]]:
                modify_patch.status = "rejected"
                modify_patch.rejection_reason = str(err)
                unmerge_patch.status = "rejected"
                unmerge_patch.rejection_reason = str(err)
                return False, {"error": f"{msg}: {err}"}

            # 階段 1：備妥所有可能失敗的運算（深拷貝快照）；此階段不改動任何狀態
            try:
                prior = self._capture_primary_state(primary)
                new_meta = copy.deepcopy(primary.metadata)
                for key in ("facts", "merged_from", "merged_source_episodes"):
                    new_meta.pop(key, None)
                new_meta.update(copy.deepcopy(snap.get("old_metadata_keys", {})))
                old_embedding = snap.get("old_embedding")
            except Exception as e:
                return _fail("備妥回滾資料失敗", e)

            # 階段 2：改 primary 並存檔；失敗則完整還原 primary
            try:
                primary.description = snap.get("old_description", primary.description)
                if snap.get("old_confidence") is not None:
                    primary.confidence = snap["old_confidence"]
                primary.metadata = new_meta
                if old_embedding:
                    primary.embedding = list(old_embedding)
                    semantic._update_embedding_index(primary, is_update=True)
                primary.remove_derivations_by_patch(snap.get("merge_patch_id", ""))
                self._add_history(primary, modify_patch)
                primary.record_mutation()
                semantic._save()
            except Exception as e:
                self._restore_primary_state(semantic, primary, prior)
                return _fail("還原主概念失敗", e)
            modify_patch.status = "applied"

            # 階段 3：最後才還原次概念（restore_merged 內部先存概念檔、再還原圖譜）；
            # 失敗（回傳 False 或拋例外）則補償 primary，並把次概念快照放回去
            secondary.metadata.pop("merge_primary_snapshot", None)
            try:
                restored = semantic.restore_merged(merge_with)
                restore_err: Exception | None = (
                    None if restored else RuntimeError("restore_merged 回傳 False")
                )
            except Exception as e:
                restored, restore_err = False, e
            if not restored:
                secondary.metadata["merge_primary_snapshot"] = snap
                self._restore_primary_state(semantic, primary, prior)
                modify_patch.status = "rejected"
                modify_patch.rejection_reason = f"次概念還原失敗，已補償主概念: {restore_err}"
                unmerge_patch.status = "rejected"
                unmerge_patch.rejection_reason = str(restore_err)
                return False, {"error": f"次概念還原失敗: {merge_with}（{restore_err}）"}
            unmerge_patch.status = "applied"
            # 回滾不經 Gate，不會自動通知：主、次概念各通知一次（共享層據此撤回失效的投影）
            notify = getattr(self._inhibitor, "_notify_change", None)
            if callable(notify):
                for cid in (primary.id, secondary.id):
                    try:
                        notify(cid, "rollback:merge")
                    except Exception:
                        logger.warning("概念變更通知失敗 %s", cid)
            return True, {"primary": target_name, "secondary_restored": merge_with}

    def build_rollback_fn(self):
        """
        回傳可傳入 ActionQueue.rollback() 的回調函式。

        根據 pre_state 還原目標物件到執行前狀態。
        簽章：(action_type: str, target_id: str, pre_state: dict) → None
        """
        def rollback_fn(
            action_type: str, target_id: str, pre_state: dict[str, Any]
        ) -> None:
            with self._concept_guard(action_type):
                self._rollback_action(action_type, target_id, pre_state)

        return rollback_fn

    def _rollback_action(
        self, action_type: str, target_id: str, pre_state: dict[str, Any]
    ) -> None:
        """依 action_type 執行實際回滾（由 build_rollback_fn 包上 storage_lock 後呼叫）。"""
        ctx = self._gate_context(pre_state=pre_state)
        if action_type == "adjust_salience":
            old = pre_state.get("salience_score")
            if old is not None:
                self._episode_gate().restore_salience(
                    self._engine.episodic, target_id, old, ctx,
                )

        elif action_type == "tag_concept":
            old_tags = pre_state.get("tags")
            if old_tags is not None:
                self._gated_undo("undo_tag", target_id, {"tags": old_tags})

        elif action_type == "archive_episode":
            self._episode_gate().restore_archive(
                self._engine.episodic, target_id, pre_state, ctx,
            )

        elif action_type == "create_concept":
            if not pre_state.get("already_exists"):
                self._gated_undo("undo_create", target_id, {})

        elif action_type == "add_note":
            # SoulNote 不支援刪除：追加撤回標記（經 EpisodeGate 留 audit）
            note_id = pre_state.get("note_id")
            if not note_id or self._soul_notes is None:
                raise ValueError("add_note 回滾缺少 note_id 或 SoulNoteManager 未配置")
            self._episode_gate().retract_note(self._soul_notes, note_id, ctx)

        elif action_type == "merge_concepts":
            # 次概念只是被標成 merged（資料與合併前主概念快照都還在），可完整還原
            secondary = (pre_state.get("secondary") or {}).get("name")
            if not secondary:
                raise ValueError("merge_concepts 回滾缺少次概念名稱（pre_state.secondary）")
            self._gated_undo("undo_merge", target_id, {"merge_with": secondary})

        else:
            logger.warning(
                "[ReflectionExecutor] 未知 action_type 的 rollback: %s",
                action_type,
            )

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
                    return self._with_provenance(
                        {"salience_score": ep.salience_score}, params,
                    )

            elif action_type == "add_note":
                # 預先配發 note_id（寫入 params 與 pre_state），回滾才能精確撤回；
                # pre_state 為空時 ActionQueue.rollback 不會呼叫 rollback_fn
                note_id = params.get("_note_id") or f"note_{uuid.uuid4().hex[:12]}"
                params["_note_id"] = note_id
                return self._with_provenance({"note_id": note_id}, params)

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
                    return self._with_provenance({
                        "salience_score": ep.salience_score,
                        "archived": ep.metadata.get("archived", False),
                        "archived_by": ep.metadata.get("archived_by"),
                    }, params)

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

    @staticmethod
    def _with_provenance(state: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
        """把來源（反思週期／觸發 episode）帶進 pre_state，重建後回滾的 audit 仍有來源。"""
        prov = params.get("_provenance")
        if prov:
            state["provenance"] = copy.deepcopy(prov)
        return state

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 各行動的具體實作
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _exec_adjust_salience(
        self, target_id: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        """調整 episode 的顯著性分數（經 EpisodeGate：audit 先行、可回滾）。"""
        new_salience = float(params.get("new_salience", 0.5))
        new_salience = min(1.0, max(0.0, new_salience))
        return self._episode_gate().adjust_salience(
            self._engine.episodic, target_id, new_salience,
            self._gate_context(params),
        )

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

        # 一律走 ConceptInhibitor patch pipeline（有 audit trail）
        success, result = self._gate().process_reflection_patch(
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

    def _exec_add_note(
        self, _target_id: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        """寫入一則潛意識筆記。"""
        if self._soul_notes is None:
            raise RuntimeError("SoulNoteManager 未配置，無法寫入 note")

        content = str(params.get("content", ""))
        if not content:
            raise ValueError("add_note 缺少 content 參數")

        return self._episode_gate().add_note(
            self._soul_notes, content, params.get("_note_id"), self._gate_context(params),
        )

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

        # 一律走 ConceptInhibitor patch pipeline（有 audit trail）
        success, result = self._gate().process_reflection_patch(
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

    def _exec_archive_episode(
        self, target_id: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        """歸檔 episode（經 EpisodeGate：audit 先行；底層一次 lock 內完成 salience 降低 + metadata 標記）。"""
        return self._episode_gate().archive_episode(
            self._engine.episodic, target_id, self._gate_context(params),
        )

    def _derive_support_source(self, params: dict[str, Any]) -> str | None:
        """由來源 Episode 的證據角色推得 support_source（沿用 M-9 規則）；無來源 Episode 回傳 None。"""
        from echo_memory.utils.source_view import load_units, support_source

        episode_ids: list[str] = []
        for raw in (
            params.get("parent_episode_ids") or [],
            (params.get("_provenance") or {}).get("source_episode_ids") or [],
        ):
            for eid in raw:
                if str(eid) not in episode_ids:
                    episode_ids.append(str(eid))
        episodes = [
            ep for ep in (self._engine.episodic.get(eid) for eid in episode_ids) if ep is not None
        ]
        if not episodes:
            return None
        units = load_units(getattr(self._engine, "evidence", None), episodes)
        return support_source(episodes, units)

    def _exec_create_concept(
        self, target_id: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        """建立新概念（若同名概念已存在則拒絕）。優先走 ConceptInhibitor patch pipeline。"""
        description = str(params.get("description", ""))
        if not description:
            raise ValueError("create_concept 缺少 description 參數")

        # 同名判斷含已合併（deprecated + merged_into）的概念：它們仍占著名稱（別名），不重建
        existing = self._engine.semantic.get_by_name(target_id)
        if existing is not None:
            merged_note = "（已併入其他概念）" if self._engine.semantic.is_merged(existing) else ""
            raise ValueError(
                f"create_concept 拒絕：同名概念「{target_id}」已存在{merged_note}，"
                f"不允許覆寫（使用 tag_concept 或 merge_concepts 修改既有概念）"
            )

        # 一律走 ConceptInhibitor patch pipeline（有 audit trail）
        success, result = self._gate().process_reflection_patch(
            operation="create",
            target_name=target_id,
            params={
                "support_source": self._derive_support_source(params),
                "description": description,
                "concept_type": params.get("concept_type", "reflection"),
                "parent_concepts": params.get("parent_concepts"),
                "parent_episode_ids": params.get("parent_episode_ids"),
                "parent_evidence_ids": params.get("parent_evidence_ids"),
                **({"similar_hint": params["similar_hint"]} if "similar_hint" in params else {}),
            },
            reasoning="ReflectionExecutor create_concept",
        )
        if not success:
            raise RuntimeError(
                f"ConceptInhibitor create 失敗: {result.get('error', '未知')}"
            )
        return result
