"""
SelfModelGateMixin — Self Model 條目的專用 Gate action（M6）

Self Model 的載體沿用 Concept(subject="agent")，只多一個 metadata["self_model"] 標記；
不新增旁路 writer：標記只能經本 mixin 的 patch（action="self_model"）寫入，
audit-before-mutate、可 rollback，與其他 ConceptInhibitor patch 同一條管線。

規則（皆在 Gate 內重算，不信任呼叫端）：
  - 目標必須是 active、subject="agent" 的概念；
  - evidence_refs 必填，且每個 id 都必須是存在、未塗銷的證據 unit；
  - 標記存 description 指紋；描述之後被 expand／modify／merge 改變，effective_verification 即降為 unverified；
  - verification 只有 "unverified"／"verified"：僅 agent 自述（agent_only／沒有任何
    具證據資格的引用）的條目一律 unverified，且不得被要求升 verified（沿用 M-9 規則）；
  - Self Model 只是「觀察」，不是設定：本 mixin 不碰 persona／character。

本檔不使用任何關鍵字／regex 判斷。
"""

from __future__ import annotations

import copy
import logging
from datetime import datetime, timezone
from typing import Any

from echo_memory.affect.concept_patch import (
    ConceptPatch,
    PatchEvidence,
    PatchTransaction,
)
from echo_memory.schemas.hypothesis import hypothesis_evidence_kind
from echo_memory.schemas.self_model import (  # noqa: F401 - is_self_model_concept 供外部沿用此路徑
    SELF_MODEL_KEY,
    VERIFICATION_UNVERIFIED,
    VERIFICATION_VERIFIED,
    VERIFICATIONS,
    description_digest,
    is_self_model_concept,
)

logger = logging.getLogger("echo_memory.affect.self_model_gate")

class SelfModelGateMixin:
    """ConceptInhibitor 的 Self Model 子系統。依賴宿主的 _semantic／_lookup_units／_apply_patch。"""

    def process_self_model_patch(
        self,
        concept_id: str,
        evidence_refs: list[str],
        *,
        verification: str = VERIFICATION_UNVERIFIED,
        triggered_by: str = "dream",
    ) -> tuple[bool, PatchTransaction]:
        """把 agent 概念標記為 Self Model 條目（觀察，非設定）。規則層 approve，不呼叫 LLM。

        結構驗證不通過一律拒絕（概念不動，仍留 audit）：
          - 概念不存在／非 active／subject 不是 agent
          - evidence_refs 為空，或含不存在／已塗銷的 unit
          - verification 不合法，或 agent_only／無具資格引用卻要 verified

        Returns:
            (success, PatchTransaction)。概念不存在時 tx.patches 為空、status="failed"。
        """
        tx = PatchTransaction(
            patches=[],
            user_input=f"[self_model] {concept_id}",
            llm_response="",
            session_id="self-model",
            turn_id="",
            triggered_by=triggered_by,  # type: ignore[arg-type]
        )
        refs = list(dict.fromkeys(str(x) for x in (evidence_refs or []) if x))
        reject: str | None = None
        with self._semantic._lock:  # type: ignore[attr-defined]
            concept = self._semantic.get(concept_id)  # type: ignore[attr-defined]
            if concept is None:
                tx.status = "failed"
                return False, tx

            value: dict[str, Any] = {
                "evidence_refs": refs,
                "verification": VERIFICATION_UNVERIFIED,
                "description_digest": description_digest(concept.description),
                "tagged_at": datetime.now(timezone.utc).isoformat(),
            }
            if concept.status != "active" or concept.metadata.get("merged_into"):
                reject = "概念不是 active"
            elif concept.subject != "agent":
                reject = "Self Model 只能標記 subject=agent 的概念"
            elif not refs:
                reject = "no_evidence_refs: Self Model 條目必須帶 evidence_refs"
            elif verification not in VERIFICATIONS:
                reject = f"verification 無效: {verification!r}"
            else:
                units = self._lookup_units(refs)  # type: ignore[attr-defined]
                missing = [i for i in refs if i not in units]
                if missing:
                    reject = f"evidence_missing: {missing[0]}"
                elif any(getattr(units[i], "tombstoned_at", None) is not None for i in refs):
                    reject = "evidence_tombstoned: 引用了已塗銷的證據"
                else:
                    qualified = any(hypothesis_evidence_kind(units[i]) is not None for i in refs)
                    agent_only = (
                        concept.metadata.get("support_source") == "agent_only" or not qualified
                    )
                    if verification == VERIFICATION_VERIFIED:
                        if agent_only:
                            reject = "agent 自述不能自證：僅 agent 來源的條目不可為 verified"
                        else:
                            value["verification"] = VERIFICATION_VERIFIED

            patch = ConceptPatch(
                transaction_id=tx.transaction_id,
                concept_id=concept.id,
                concept_name=concept.name,
                action="self_model",
                target_path=f"metadata.{SELF_MODEL_KEY}",
                old_value=copy.deepcopy(concept.metadata.get(SELF_MODEL_KEY)),
                new_value=value,
                patch_confidence=0.7,
                evidence=PatchEvidence(
                    evidence_kind="self_model",
                    evidence_strength=0.7,
                    verifier_status="valid",
                    verifier_reason="self_model: 標記 Self Model 條目",
                    retrieved_concept_ids=[concept.id],
                ),
                risk_level="low",
            )
            tx.patches.append(patch)
            if reject is None:
                patch.status = "approved"

        if reject is not None:
            patch.status = "rejected"
            patch.rejection_reason = reject
            tx.status = "failed"
            self._write_audit(tx)  # type: ignore[attr-defined]
            return False, tx

        # audit-before-mutate：先寫 audit，失敗則概念不動
        if not self._try_write_audit(tx):  # type: ignore[attr-defined]
            patch.status = "rejected"
            patch.rejection_reason = "audit log 寫入失敗，阻止 mutation"
            tx.status = "failed"
            return False, tx

        if self._apply_patch(patch):  # type: ignore[attr-defined]
            patch.status = "applied"
            tx.status = "committed"
            self._write_audit(tx)  # type: ignore[attr-defined]
            emitter = getattr(self, "event_emitter", None)
            if emitter is not None:
                try:
                    emitter.emit_self_observation_proposed(concept.id, refs)
                except Exception as e:  # noqa: BLE001 - 事件層不影響寫入
                    logger.warning("[SelfModelGate] 事件發射失敗（已忽略）: %s", e)
            return True, tx

        self._rollback_patch(patch)  # type: ignore[attr-defined]
        patch.status = "rejected"
        patch.rejection_reason = "self_model 套用失敗（存檔失敗）"
        tx.status = "failed"
        self._write_audit(tx)  # type: ignore[attr-defined]
        return False, tx

    # 以下兩者由 ConceptInhibitor 的 _apply_patch／_rollback_patch_impl 在 SemanticMemory._lock 內呼叫

    def _apply_self_model(self, concept: Any, patch: ConceptPatch) -> bool:
        """寫入 metadata["self_model"]。不呼叫 _save()：外層統一 record_mutation 後持久化。"""
        value = patch.new_value if isinstance(patch.new_value, dict) else None
        if value is None or not value.get("evidence_refs"):
            return False
        # 套用時重查（規劃到套用之間狀態可能變動）：active、agent、描述未變、證據仍有效
        if concept.subject != "agent" or concept.status != "active" or concept.metadata.get("merged_into"):
            return False
        if value.get("description_digest") != description_digest(concept.description):
            return False
        refs = [str(x) for x in value["evidence_refs"]]
        units = self._lookup_units(refs)  # type: ignore[attr-defined]
        if any(i not in units or getattr(units[i], "tombstoned_at", None) is not None for i in refs):
            return False
        patch.rollback_payload = patch.rollback_payload or {}
        patch.rollback_payload["self_model_undo"] = {
            "present": SELF_MODEL_KEY in concept.metadata,
            "value": copy.deepcopy(concept.metadata.get(SELF_MODEL_KEY)),
        }
        concept.metadata[SELF_MODEL_KEY] = copy.deepcopy(value)
        self._add_history_entry(concept, patch)  # type: ignore[attr-defined]
        concept.updated_at = datetime.now(timezone.utc)
        return True

    @staticmethod
    def _rollback_self_model(concept: Any, patch: ConceptPatch) -> None:
        undo = (patch.rollback_payload or {}).get("self_model_undo")
        if undo is None:  # 沒有 undo ＝ apply 在改動前就失敗
            return
        if undo.get("present"):
            concept.metadata[SELF_MODEL_KEY] = copy.deepcopy(undo.get("value"))
        else:
            concept.metadata.pop(SELF_MODEL_KEY, None)
        concept.record_mutation()
