"""
ConceptInhibitor — 概念 Patch 閘門

Semantic Memory 的變更安全閘門。根據 ConceptVerifier 的判定結果 + LLM 回覆，
決定哪些概念變更要真正寫入，向 SemanticMemory 提交 patch transaction。

核心原則：
  - user_input > Verifier evidence >> LLM response（輔助，不可當主要證據）
  - deprecate ≠ delete, modify ≠ overwrite
  - 所有變更走 patch transaction，保留版本 + 來源 + confidence + rollback path
  - Hybrid 決策：規則先處理 70% 明確案例，LLM 只處理高風險/模糊場景

對應設計文件：docs/design/thought.md
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from echo_memory.affect.concept_patch import (
    ConceptPatch,
    ConceptState,
    ExpandMode,
    PatchEvidence,
    PatchHistoryEntry,
    PatchTransaction,
)
from echo_memory.affect.concept_verifier import ConceptAssessment, ConceptVerification
from echo_memory.schemas.concept import Concept
from echo_memory.schemas.memory_context import MemoryContext
from echo_memory.storage.semantic import SemanticMemory

logger = logging.getLogger("echo_memory.affect.concept_inhibitor")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# LLM Corroboration Prompt
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_CORROBORATION_PROMPT = """\
你是記憶系統的概念變更驗證器。
以下是一個概念變更提案，請判斷此變更是否合理。

概念名稱：{concept_name}
概念描述：{concept_description}
變更動作：{action}
建議更新：{suggested_update}

用戶原始輸入：
{user_input}

LLM 回覆：
{llm_response}

驗證器理由：{verifier_reason}

以純 JSON 回覆：
{{"approve": true/false, "confidence": 0.0-1.0, "reason": "判斷理由"}}

判斷標準：
- 用戶輸入中是否有明確事實支持此變更？
- 變更是否過度推論？
- 動作類型（expand/modify/deprecate）是否恰當？"""


class ConceptInhibitor:
    """
    概念 Patch 閘門。

    從 ConceptVerifier 的判定結果 + LLM 回覆出發，
    建立 PatchTransaction → 驗證每個 Patch → 提交到 SemanticMemory。

    使用範例::

        inhibitor = ConceptInhibitor(
            semantic=engine.semantic,
            llm_fn=eval_fn,
        )
        transaction = inhibitor.process(
            verification=concept_verification,
            user_input=user_input,
            llm_response=response,
            session_id=session_id,
            turn_id=episode_id,
        )
    """

    def __init__(
        self,
        semantic: SemanticMemory,
        llm_fn: Callable[[str], str] | None = None,
        audit_log_path: str | None = None,
    ):
        """
        Args:
            semantic: SemanticMemory 實例（patch 目標）
            llm_fn: LLM 呼叫函式（EVAL 角色）。None 時跳過 LLM corroboration。
            audit_log_path: 獨立 audit log 路徑。None 時使用 semantic 目錄下的預設路徑。
        """
        self._semantic = semantic
        self._llm_fn = llm_fn
        self._audit_log_path = audit_log_path

    # ──────────────────────────────────────────────
    # 核心流程
    # ──────────────────────────────────────────────

    def process(
        self,
        verification: ConceptVerification,
        user_input: str,
        llm_response: str,
        session_id: str,
        turn_id: str,
    ) -> PatchTransaction:
        """
        處理 ConceptVerifier 的結果，建立並執行 PatchTransaction。

        流程：
        1. 從 verification.changes 建立 patches
        2. 對每個 patch 做 hybrid 決策（規則 + 可選 LLM）
        3. 提交 approved patches 到 SemanticMemory
        4. 寫入 audit log

        Args:
            verification: ConceptVerifier 的輸出
            user_input: 觸發此輪的用戶輸入
            llm_response: LLM 回覆
            session_id: 會話 ID
            turn_id: 輪次 ID（通常是 episode_id）

        Returns:
            PatchTransaction（包含所有 patch 的最終狀態）
        """
        # 無變更 → 空 transaction
        if not verification.has_changes:
            return PatchTransaction(
                patches=[],
                user_input=user_input,
                llm_response=llm_response,
                session_id=session_id,
                turn_id=turn_id,
                status="committed",
            )

        # 1. 建立 patches
        tx = PatchTransaction(
            patches=[],
            user_input=user_input,
            llm_response=llm_response,
            session_id=session_id,
            turn_id=turn_id,
        )
        for assessment in verification.changes:
            patch = self._build_patch(assessment, tx.transaction_id, user_input)
            if patch is not None:
                tx.patches.append(patch)

        if not tx.patches:
            tx.status = "committed"
            return tx

        # 2. Hybrid 決策
        for patch in tx.patches:
            self._evaluate_patch(patch, user_input, llm_response)

        # 3. 提交 approved patches（原子性：任一失敗 → rollback 全部已 applied）
        approved_count = sum(1 for p in tx.patches if p.status == "approved")
        applied_patches: list[ConceptPatch] = []
        failed = False
        for patch in tx.patches:
            if patch.status == "approved":
                success = self._apply_patch(patch)
                if success:
                    patch.status = "applied"
                    applied_patches.append(patch)
                else:
                    patch.status = "rejected"
                    patch.rejection_reason = "SemanticMemory apply 失敗"
                    failed = True
                    break  # 停止後續 apply

        # 原子性保證：有 patch 失敗 → rollback 所有已 applied
        if failed and applied_patches:
            logger.warning(
                "[ConceptInhibitor] Patch 失敗，rollback %d 個已 applied patches",
                len(applied_patches),
            )
            for p in reversed(applied_patches):
                self._rollback_patch(p)
                p.status = "rolled_back"
            applied_patches.clear()

        # 4. 更新 transaction 狀態
        if approved_count == 0:
            tx.status = "committed"
        elif failed:
            tx.status = "failed"
        else:
            tx.status = "committed"

        # 5. 寫入 audit log
        self._write_audit(tx)

        if applied_patches:
            logger.info(
                "[ConceptInhibitor] %s — applied %d/%d patches",
                tx.summary(), len(applied_patches), len(tx.patches),
            )

        return tx

    # ──────────────────────────────────────────────
    # Extractor Pipeline 入口
    # ──────────────────────────────────────────────

    def process_extractor_patch(
        self,
        noun: str,
        desc: str,
        episode_id: str,
        evidence_kind: str,
        similarity_score: float = 0.0,
        existing_concept_id: str | None = None,
        embedding: list[float] | None = None,
        triggered_by: str = "extractor",
    ) -> tuple[str | None, PatchTransaction]:
        """
        處理來自 ConceptExtractor 的概念寫入請求。

        ConceptExtractor 不再直接呼叫 SemanticMemory，而是透過此方法
        產生 ConceptPatch → 規則決策 → apply → audit。

        Extractor 來源的 patch 不走 LLM corroboration（可信度足夠），
        規則層直接 approve。

        Args:
            noun: 概念名稱
            desc: 概念描述
            episode_id: 來源 episode ID
            evidence_kind: 證據類型（extractor_new_concept / extractor_synonym_merge / extractor_polysemy / extractor_fallback）
            similarity_score: 與最近鄰的相似度（用於 merge/polysemy 路徑）
            existing_concept_id: 現有概念 ID（用於 merge 路徑）
            embedding: 概念嵌入向量（用於 create 路徑）

        Returns:
            (concept_id | None, PatchTransaction)
        """
        tx = PatchTransaction(
            patches=[],
            user_input=f"[extractor] {noun}: {desc}",
            llm_response="",
            session_id="extractor-bg",
            turn_id=episode_id,
            triggered_by=triggered_by,
        )

        evidence = PatchEvidence(
            evidence_kind=evidence_kind,
            evidence_strength=0.7,  # extractor 來源的基礎可信度
            verifier_status="valid",
            verifier_reason=f"ConceptExtractor 背景提取 (sim={similarity_score:.3f})",
            source_episode_ids=[episode_id],
        )

        concept_id: str | None = None

        if evidence_kind == "extractor_synonym_merge":
            # 同義詞合併 → expand (ADD_FACT: 新增 source_episode_id)
            if existing_concept_id is None:
                tx.status = "failed"
                self._write_audit(tx)
                return None, tx

            concept = self._semantic.get(existing_concept_id)
            if concept is None:
                tx.status = "failed"
                self._write_audit(tx)
                return None, tx

            patch = ConceptPatch(
                transaction_id=tx.transaction_id,
                concept_id=existing_concept_id,
                concept_name=concept.name,
                action="expand",
                expand_mode=ExpandMode.ADD_FACT,
                target_path="source_episode_ids",
                old_value=list(concept.source_episode_ids),
                new_value=episode_id,
                old_confidence=concept.confidence,
                new_confidence=concept.confidence,
                patch_confidence=0.8,
                evidence=evidence,
                risk_level="low",
                rollback_payload={
                    "old_source_episode_ids": list(concept.source_episode_ids),
                },
            )
            tx.patches.append(patch)
            concept_id = existing_concept_id

        else:
            # 全新概念 / 多義詞 / fallback → create
            patch = ConceptPatch(
                transaction_id=tx.transaction_id,
                concept_name=noun,
                action="create",
                target_path="*",
                new_value={"name": noun, "description": desc, "episode_id": episode_id},
                patch_confidence=0.7,
                evidence=evidence,
                risk_level="low",
                rollback_payload={"embedding": embedding},
            )
            tx.patches.append(patch)

        # Extractor 來源 → 規則層直接 approve（不走 LLM corroboration）
        for patch in tx.patches:
            patch.status = "approved"

        # Apply
        applied_patches: list[ConceptPatch] = []
        failed = False
        for patch in tx.patches:
            if patch.action == "create":
                success, new_id = self._apply_create(patch, embedding)
                if success:
                    patch.status = "applied"
                    patch.concept_id = new_id
                    concept_id = new_id
                    applied_patches.append(patch)
                else:
                    patch.status = "rejected"
                    patch.rejection_reason = "SemanticMemory create 失敗"
                    failed = True
            elif patch.action == "expand":
                success = self._apply_expand_source(patch)
                if success:
                    patch.status = "applied"
                    applied_patches.append(patch)
                else:
                    patch.status = "rejected"
                    patch.rejection_reason = "SemanticMemory expand 失敗"
                    failed = True

        # Rollback（若有任何失敗）
        if failed and applied_patches:
            for p in reversed(applied_patches):
                self._rollback_patch(p)
                p.status = "rolled_back"
            applied_patches.clear()
            concept_id = None

        tx.status = "committed" if not failed else "failed"
        self._write_audit(tx)

        if applied_patches:
            logger.info(
                "[ConceptInhibitor] extractor patch: %s — %d applied",
                tx.summary(), len(applied_patches),
            )

        return concept_id, tx

    def _apply_create(
        self,
        patch: ConceptPatch,
        embedding: list[float] | None = None,
    ) -> tuple[bool, str | None]:
        """
        執行 create patch — 新建概念到 SemanticMemory。

        Returns:
            (success, concept_id)
        """
        try:
            payload = patch.new_value or {}
            concept_id = self._semantic.upsert_concept(
                name=payload.get("name", patch.concept_name),
                description=payload.get("description", ""),
                source_episode_id=payload.get("episode_id"),
                embedding=embedding,
            )
            if concept_id:
                # 設定 rollback payload
                patch.rollback_payload = patch.rollback_payload or {}
                patch.rollback_payload["created_concept_name"] = patch.concept_name
                return True, concept_id
            return False, None
        except Exception as e:
            logger.error(
                "[ConceptInhibitor] _apply_create 失敗 (%s): %s",
                patch.concept_name, e,
            )
            return False, None

    def _apply_expand_source(self, patch: ConceptPatch) -> bool:
        """
        執行 expand/source_episode_ids — 同義詞合併，
        新增 episode_id 到現有概念的 source_episode_ids。
        """
        concept = self._semantic.get(patch.concept_id or "")
        if concept is None:
            return False

        try:
            new_episode_id = patch.new_value
            if isinstance(new_episode_id, str):
                if new_episode_id not in concept.source_episode_ids:
                    concept.source_episode_ids.append(new_episode_id)
                    concept.access_count += 1
                    concept.record_mutation()
                    self._semantic._save()
                    self._add_history_entry(concept, patch)
            return True
        except Exception as e:
            logger.error(
                "[ConceptInhibitor] _apply_expand_source 失敗 (%s): %s",
                patch.patch_id, e,
            )
            return False

    # ──────────────────────────────────────────────
    # Patch 建立
    # ──────────────────────────────────────────────

    def _build_patch(
        self,
        assessment: ConceptAssessment,
        transaction_id: str,
        user_input: str,
    ) -> ConceptPatch | None:
        """從 ConceptAssessment 建立 ConceptPatch。"""
        concept = self._semantic.get(assessment.concept_id)
        if concept is None:
            concept = self._semantic.get_by_name(assessment.concept_name)
        if concept is None:
            logger.warning(
                "[ConceptInhibitor] 找不到概念 %s (%s)，跳過",
                assessment.concept_name, assessment.concept_id,
            )
            return None

        # 判定 evidence_kind
        evidence_kind = self._infer_evidence_kind(assessment)

        evidence = PatchEvidence(
            evidence_kind=evidence_kind,
            evidence_strength=self._estimate_evidence_strength(assessment),
            verifier_status=assessment.status,
            verifier_reason=assessment.reason,
            retrieved_concept_ids=[concept.id],
            user_input_excerpt=user_input[:200],
        )

        # 依據 action 建立不同類型的 patch
        if assessment.status == "expand":
            return self._build_expand_patch(
                concept, assessment, evidence, transaction_id,
            )
        elif assessment.status == "modify":
            return self._build_modify_patch(
                concept, assessment, evidence, transaction_id,
            )
        elif assessment.status == "deprecate":
            return self._build_deprecate_patch(
                concept, assessment, evidence, transaction_id,
            )

        return None

    def _build_expand_patch(
        self,
        concept: Concept,
        assessment: ConceptAssessment,
        evidence: PatchEvidence,
        transaction_id: str,
    ) -> ConceptPatch:
        """建立 expand patch — 預設 ADD_FACT。"""
        new_fact = {
            "text": assessment.suggested_update or assessment.reason,
            "source": "concept_verifier",
            "confidence": evidence.evidence_strength,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        # Confidence 調整：只有強證據 + 無衝突才加
        new_confidence = concept.confidence
        if evidence.evidence_strength >= 0.8:
            new_confidence = min(concept.confidence + 0.05, 0.95)

        return ConceptPatch(
            transaction_id=transaction_id,
            concept_id=concept.id,
            concept_name=concept.name,
            action="expand",
            expand_mode=ExpandMode.ADD_FACT,
            target_path="metadata.facts",
            old_value=list(concept.facts),  # 複製，避免引用
            new_value=new_fact,
            old_confidence=concept.confidence,
            new_confidence=new_confidence,
            patch_confidence=evidence.evidence_strength,
            evidence=evidence,
            risk_level="low",
            rollback_payload={
                "old_facts": list(concept.facts),
                "old_confidence": concept.confidence,
            },
        )

    def _build_modify_patch(
        self,
        concept: Concept,
        assessment: ConceptAssessment,
        evidence: PatchEvidence,
        transaction_id: str,
    ) -> ConceptPatch:
        """建立 modify patch。"""
        new_description = assessment.suggested_update or concept.description

        # modify confidence: 舊版本信心下降，新版本取決於證據強度
        new_confidence = max(
            concept.confidence * 0.7,
            min(evidence.evidence_strength, 0.85),
        )

        return ConceptPatch(
            transaction_id=transaction_id,
            concept_id=concept.id,
            concept_name=concept.name,
            action="modify",
            target_path="description",
            old_value=concept.description,
            new_value=new_description,
            old_confidence=concept.confidence,
            new_confidence=new_confidence,
            patch_confidence=evidence.evidence_strength,
            evidence=evidence,
            risk_level="high",
            rollback_payload={
                "old_description": concept.description,
                "old_confidence": concept.confidence,
            },
        )

    def _build_deprecate_patch(
        self,
        concept: Concept,
        assessment: ConceptAssessment,
        evidence: PatchEvidence,
        transaction_id: str,
    ) -> ConceptPatch:
        """建立 deprecate patch。"""
        state = ConceptState(
            status="deprecated",
            deprecated_at=datetime.now(timezone.utc),
            deprecated_reason=assessment.reason,
            replaced_by=None,
        )

        return ConceptPatch(
            transaction_id=transaction_id,
            concept_id=concept.id,
            concept_name=concept.name,
            action="deprecate",
            target_path="status",
            old_value=concept.status,
            new_value=state.to_dict(),
            old_confidence=concept.confidence,
            new_confidence=None,  # deprecated 由 ConceptState 接管
            patch_confidence=evidence.evidence_strength,
            evidence=evidence,
            risk_level="high",
            rollback_payload={
                "old_status": concept.status,
                "old_confidence": concept.confidence,
                "old_metadata": dict(concept.metadata),
            },
        )

    # ──────────────────────────────────────────────
    # Hybrid 決策
    # ──────────────────────────────────────────────

    def _evaluate_patch(
        self,
        patch: ConceptPatch,
        user_input: str,
        llm_response: str,
    ) -> None:
        """
        Hybrid 決策：規則先行，高風險才呼叫 LLM。

        決策結果直接修改 patch.status。
        """
        # ── 規則層 ──
        if self._should_approve_by_rule(patch):
            patch.status = "approved"
            return

        if self._should_reject_by_rule(patch):
            patch.status = "rejected"
            return

        # ── LLM 層（高風險 / 模糊）──
        if self._should_call_llm(patch):
            llm_result = self._llm_corroborate(patch, user_input, llm_response)
            if llm_result is not None:
                patch.evidence.llm_corroboration = llm_result.get("reason", "")
                if llm_result.get("approve"):
                    # LLM 佐證，提升 patch_confidence
                    patch.patch_confidence = min(
                        patch.patch_confidence + 0.15,
                        float(llm_result.get("confidence", 0.7)),
                    )
                    patch.status = "approved"
                else:
                    patch.status = "rejected"
                    patch.rejection_reason = (
                        f"LLM 不佐證: {llm_result.get('reason', '未知')}"
                    )
                return
            else:
                # LLM 呼叫失敗 → held（不 commit 也不 reject）
                patch.status = "held"
                return

        # 預設：中等風險且無 LLM → held
        patch.status = "held"

    def _should_approve_by_rule(self, patch: ConceptPatch) -> bool:
        """規則層：明確可批准的案例。"""
        # expand/ADD_FACT + 強證據 → 直接批准
        if (
            patch.action == "expand"
            and patch.expand_mode == ExpandMode.ADD_FACT
            and patch.evidence.evidence_strength >= 0.75
        ):
            return True

        return False

    def _should_reject_by_rule(self, patch: ConceptPatch) -> bool:
        """規則層：明確可拒絕的案例。"""
        # 極弱證據 → 拒絕
        if patch.evidence.evidence_strength < 0.2:
            patch.rejection_reason = (
                f"證據強度過低 ({patch.evidence.evidence_strength:.2f})"
            )
            return True

        # deprecate 但沒有 reason → 拒絕
        if (
            patch.action == "deprecate"
            and not patch.evidence.verifier_reason.strip()
        ):
            patch.rejection_reason = "deprecate 缺少理由"
            return True

        return False

    def _should_call_llm(self, patch: ConceptPatch) -> bool:
        """判斷是否需要 LLM corroboration。"""
        if self._llm_fn is None:
            return False

        # modify/deprecate 一定要 LLM
        if patch.action in {"modify", "deprecate"}:
            return True

        # 中等證據強度 → 需要 LLM
        if patch.evidence.evidence_strength < 0.55:
            return True

        # 高風險 → 需要 LLM
        if patch.risk_level in {"medium", "high"}:
            return True

        return False

    def _llm_corroborate(
        self,
        patch: ConceptPatch,
        user_input: str,
        llm_response: str,
    ) -> dict[str, Any] | None:
        """呼叫 LLM 做語意佐證。失敗回傳 None。"""
        assert self._llm_fn is not None

        concept = self._semantic.get(patch.concept_id or "")
        concept_desc = concept.description if concept else "(未知)"

        prompt = _CORROBORATION_PROMPT.format(
            concept_name=patch.concept_name,
            concept_description=concept_desc,
            action=patch.action,
            suggested_update=patch.new_value or "(無)",
            user_input=user_input[:500],
            llm_response=llm_response[:500],
            verifier_reason=patch.evidence.verifier_reason,
        )

        try:
            raw = self._llm_fn(prompt)
            return self._parse_corroboration(raw)
        except Exception as e:
            logger.warning("[ConceptInhibitor] LLM corroboration 失敗: %s", e)
            return None

    def _parse_corroboration(self, raw: str) -> dict[str, Any] | None:
        """解析 LLM corroboration 的 JSON 回應。"""
        text = raw.strip()
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            text = text[start:end + 1]

        try:
            result = json.loads(text)
            if isinstance(result, dict) and "approve" in result:
                return result
        except json.JSONDecodeError:
            pass

        logger.warning("[ConceptInhibitor] LLM 回應格式錯誤: %s", raw[:200])
        return None

    # ──────────────────────────────────────────────
    # Patch 執行
    # ──────────────────────────────────────────────

    def _apply_patch(self, patch: ConceptPatch) -> bool:
        """執行單一 patch 到 SemanticMemory。成功時更新 Concept Health 指標。"""
        concept = self._semantic.get(patch.concept_id or "")
        if concept is None:
            return False

        try:
            if patch.action == "expand":
                success = self._apply_expand(concept, patch)
            elif patch.action == "modify":
                success = self._apply_modify(concept, patch)
            elif patch.action == "deprecate":
                success = self._apply_deprecate(concept, patch)
            else:
                logger.warning(
                    "[ConceptInhibitor] 不支援的 action: %s", patch.action
                )
                return False

            # 成功 apply → 更新 Concept Health 指標 + 再次持久化
            # record_mutation 必須在 _save 之前呼叫，確保寫入磁碟
            if success:
                concept.record_mutation()
                self._semantic._save()

            return success
        except Exception as e:
            logger.error(
                "[ConceptInhibitor] apply patch 失敗 (%s): %s",
                patch.patch_id, e,
            )
            return False

    def _apply_expand(self, concept: Concept, patch: ConceptPatch) -> bool:
        """執行 expand/ADD_FACT — 新增 fact 到 metadata["facts"]。"""
        if "facts" not in concept.metadata:
            concept.metadata["facts"] = []

        concept.metadata["facts"].append(patch.new_value)

        # 更新 confidence
        if patch.new_confidence is not None:
            concept.confidence = patch.new_confidence

        # 寫入 patch_history
        self._add_history_entry(concept, patch)

        concept.updated_at = datetime.now(timezone.utc)
        self._semantic._save()
        return True

    def _apply_modify(self, concept: Concept, patch: ConceptPatch) -> bool:
        """執行 modify — 更新 description + confidence。"""
        if isinstance(patch.new_value, str):
            concept.description = patch.new_value

        if patch.new_confidence is not None:
            concept.confidence = patch.new_confidence

        # 需要重新生成 embedding（description 變了）
        # 先保存舊 embedding 供 rollback 用
        if self._semantic._embed_fn is not None:
            patch.rollback_payload = patch.rollback_payload or {}
            patch.rollback_payload["old_embedding"] = list(concept.embedding)
            new_vec = self._semantic._embed_fn(
                f"{concept.name}: {concept.description}"
            )
            concept.embedding = self._semantic._normalize_vector(new_vec).tolist()
            self._semantic._update_embedding_index(concept, is_update=True)

        self._add_history_entry(concept, patch)

        concept.updated_at = datetime.now(timezone.utc)
        self._semantic._save()
        return True

    def _apply_deprecate(self, concept: Concept, patch: ConceptPatch) -> bool:
        """執行 deprecate — 標記狀態 + 降權。"""
        state_dict = patch.new_value
        if isinstance(state_dict, dict):
            concept.status = "deprecated"
            concept.metadata["deprecated_at"] = state_dict.get(
                "deprecated_at", datetime.now(timezone.utc).isoformat()
            )
            concept.metadata["deprecated_reason"] = state_dict.get(
                "deprecated_reason", ""
            )
            if state_dict.get("replaced_by"):
                concept.metadata["replaced_by"] = state_dict["replaced_by"]

        self._add_history_entry(concept, patch)

        concept.updated_at = datetime.now(timezone.utc)
        self._semantic._save()
        return True

    def _add_history_entry(self, concept: Concept, patch: ConceptPatch) -> None:
        """寫入輕量 patch_history 索引。"""
        if "patch_history" not in concept.metadata:
            concept.metadata["patch_history"] = []

        entry = PatchHistoryEntry(
            patch_id=patch.patch_id,
            transaction_id=patch.transaction_id,
            timestamp=datetime.now(timezone.utc),
            action=patch.action,
            old_confidence=patch.old_confidence,
            new_confidence=patch.new_confidence,
        )
        concept.metadata["patch_history"].append(entry.to_dict())

    def _rollback_patch(self, patch: ConceptPatch) -> None:
        """
        回滾一個已 apply 的 patch。

        使用 patch.old_value 還原概念狀態。
        如果 old_value 不可用，記錄警告但不崩潰。
        """
        concept = self._semantic.get(patch.concept_id or "")
        if concept is None:
            logger.warning(
                "[ConceptInhibitor] rollback 找不到概念 %s",
                patch.concept_id,
            )
            return

        try:
            if patch.action == "create":
                # create rollback → 移除概念（最終一致性：graph 殘留由 Dream 清理）
                name = (patch.rollback_payload or {}).get(
                    "created_concept_name", patch.concept_name
                )
                self._semantic.remove_concept(name)
                logger.info(
                    "[ConceptInhibitor] rollback create: 移除 %s", name,
                )
                return  # create rollback 不需要後續的 history/save 操作
            elif patch.action == "expand":
                # 移除最後加入的 fact
                facts = concept.metadata.get("facts", [])
                if facts and facts[-1] == patch.new_value:
                    facts.pop()
                # expand/source_episode_ids rollback
                old_sources = (patch.rollback_payload or {}).get(
                    "old_source_episode_ids"
                )
                if old_sources is not None:
                    concept.source_episode_ids = old_sources
                # 還原 confidence
                if patch.old_confidence is not None:
                    concept.confidence = patch.old_confidence
            elif patch.action == "modify":
                # 還原 description
                if patch.old_value is not None:
                    concept.description = str(patch.old_value)
                if patch.old_confidence is not None:
                    concept.confidence = patch.old_confidence
                # 還原 embedding（從 rollback_payload）
                old_emb = (patch.rollback_payload or {}).get("old_embedding")
                if old_emb is not None:
                    concept.embedding = old_emb
                    self._semantic._update_embedding_index(
                        concept, is_update=True
                    )
            elif patch.action == "deprecate":
                # 還原為 active
                concept.status = "active"
                concept.metadata.pop("deprecated_at", None)
                concept.metadata.pop("deprecated_reason", None)
                concept.metadata.pop("replaced_by", None)

            # 移除最後的 patch_history 條目
            history = concept.metadata.get("patch_history", [])
            if history:
                history.pop()

            # mutation_count 不回滾——rollback 本身也是一次 mutation
            self._semantic._save()
            logger.info(
                "[ConceptInhibitor] rollback 完成: %s (%s)",
                patch.patch_id, patch.action,
            )
        except Exception as e:
            logger.error(
                "[ConceptInhibitor] rollback 失敗 (%s): %s",
                patch.patch_id, e,
            )

    # ──────────────────────────────────────────────
    # Audit Log
    # ──────────────────────────────────────────────

    def _write_audit(self, tx: PatchTransaction) -> None:
        """寫入獨立 audit log（JSONL）。"""
        from pathlib import Path

        if self._audit_log_path:
            log_path = Path(self._audit_log_path)
        else:
            log_path = self._semantic.storage_path / "audit_log.jsonl"

        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(log_path, "a", encoding="utf-8") as f:
                line = json.dumps(tx.to_dict(), ensure_ascii=False)
                f.write(line + "\n")
            tx.audit_path = str(log_path)
        except Exception as e:
            logger.warning("[ConceptInhibitor] audit log 寫入失敗: %s", e)

    # ──────────────────────────────────────────────
    # 輔助方法
    # ──────────────────────────────────────────────

    @staticmethod
    def _infer_evidence_kind(
        assessment: ConceptAssessment,
    ) -> str:
        """從 assessment 推斷 evidence_kind。"""
        if assessment.status == "deprecate":
            return "user_correction"
        elif assessment.status == "modify":
            return "user_correction"
        elif assessment.status == "expand":
            return "explicit_user_claim"
        return "llm_inference"

    @staticmethod
    def _estimate_evidence_strength(assessment: ConceptAssessment) -> float:
        """從 assessment 估算 evidence_strength。"""
        # 基礎：relevance 分數
        base = assessment.relevance

        # 有 suggested_update → 加分（verifier LLM 有具體建議）
        if assessment.suggested_update:
            base = min(base + 0.15, 0.95)

        # 有明確的 reason → 加分
        if len(assessment.reason) > 20:
            base = min(base + 0.05, 0.95)

        return max(0.1, base)
