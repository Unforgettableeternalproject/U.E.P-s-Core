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

import contextlib
import copy
import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
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
from echo_memory.storage.atomic import atomic_write_json, replace_with_retry
from echo_memory.storage.locking import storage_lock
from echo_memory.storage.semantic import SemanticMemory
from echo_memory.utils.source_view import SUPPORT_RANK

logger = logging.getLogger("echo_memory.affect.concept_inhibitor")

# held_patches.json 的行程內鎖：同一路徑的多個 ConceptInhibitor 實例共用同一把
_HELD_LOCKS_GUARD = threading.Lock()
_HELD_LOCKS: dict[str, threading.RLock] = {}
# held patch 被某個 reviewer 認領（in-flight）後，超過此秒數視為認領者已中斷，可被重新認領
_HELD_CLAIM_TTL_SECONDS = 600.0


def _held_file_lock(path: Path) -> threading.RLock:
    key = str(Path(path).resolve()).casefold()
    with _HELD_LOCKS_GUARD:
        return _HELD_LOCKS.setdefault(key, threading.RLock())


def _producer_for_kind(evidence_kind: str | None) -> str:
    """由 patch 的 evidence_kind（結構化列舉值）推導 derivation 的產生者。"""
    kind = evidence_kind or ""
    if kind == "distillation":
        return "dream"
    if kind.startswith("extractor"):
        return "extractor"
    if kind.startswith("reflection"):
        return "reflection"
    return "unknown"


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


_SIMILAR_HINT_PROMPT = """\
你是記憶系統的概念閘門。系統準備新建一個概念，但 embedding 比對發現已有語意相近的 active 概念。
相似度只是提示，不代表同一件事；請依語意判斷。

準備新建的概念：
名稱：{name}
描述：{description}

語意相近的既有概念：
{candidates}

判斷：
- 新概念與某個既有概念指的是同一件事（只是說法或細節不同）→ decision 為 "expand"，
  target_id 填該既有概念的 id（新內容會補進既有概念，不另建）。
- 新概念是不同的概念（即使主題相關）→ decision 為 "create"，target_id 為 null。

以純 JSON 回覆：
{{"decision": "create" | "expand", "target_id": "既有概念 id 或 null", "reason": "判斷理由"}}"""


@dataclass
class SimilarHint:
    """新概念 create 前的「語意相近提示」。

    candidates 只由 embedding cosine 決定（不看名稱字串）。decision 在有 llm_fn 時由 LLM 給，
    沒有 llm_fn 或 LLM 失敗時維持 create（只記候選，不自動合併、不擋 create）。
    embedding 是評估時順便算好的向量，供 create 重用（避免鎖內再算一次），不進 audit。
    """
    candidates: list[dict[str, Any]] = field(default_factory=list)
    decision: str = "create"
    target_id: str | None = None
    reason: str = ""
    llm_used: bool = False
    embedding: list[float] | None = None

    def audit_candidates(self) -> list[dict[str, Any]]:
        return [
            {
                "concept_id": c["concept_id"],
                "name": c["name"],
                "similarity": c["similarity"],
            }
            for c in self.candidates
        ]

    def audit_decision(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "target_id": self.target_id,
            "reason": self.reason,
            "llm_used": self.llm_used,
        }


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
        held_max_retries: int | None = None,
        held_max_age_days: float | None = None,
        held_review_batch: int | None = None,
    ):
        """
        Args:
            semantic: SemanticMemory 實例（patch 目標）
            llm_fn: LLM 呼叫函式（EVAL 角色）。None 時跳過 LLM corroboration。
            audit_log_path: 獨立 audit log 路徑。None 時使用 semantic 目錄下的預設路徑。
            held_max_retries: held patch 重審失敗上限。None 時取 config。
            held_max_age_days: held patch 最長懸置天數。None 時取 config。
            held_review_batch: 單次觸發最多重審幾筆。None 時取 config。
        """
        self._semantic = semantic
        self._llm_fn = llm_fn
        self._audit_log_path = audit_log_path

        from echo_memory.config import get_config
        cfg = get_config()
        self._held_max_retries = (
            held_max_retries if held_max_retries is not None
            else cfg.concept_held_max_retries
        )
        self._held_max_age_days = (
            held_max_age_days if held_max_age_days is not None
            else cfg.concept_held_max_age_days
        )
        self._held_review_batch = (
            held_review_batch if held_review_batch is not None
            else cfg.concept_held_review_batch
        )
        self._similar_hint_threshold = float(cfg.concept_similar_hint_threshold)
        # held 登記簿：patch_id -> {patch, retries, held_at, context}；持久化於 held_patches.json
        self._held_lock = _held_file_lock(self._held_path())
        self._held: dict[str, dict[str, Any]] | None = None

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
        # 觸發點：每次進入 Gate 先重審先前 held 的 patch（有批次上限，無 LLM 時略過）
        try:
            self.review_held_patches()
        except Exception as e:
            logger.warning("[ConceptInhibitor] held 重審失敗（不影響本輪）: %s", e)

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
            if patch.status == "held":
                self._register_held(patch, tx, user_input, llm_response)

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
        concept_type: str = "entity",
        source_episode_ids: list[str] | None = None,
        subject: str | None = None,
        support_source: str | None = None,
        similar_hint: SimilarHint | None = None,
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
            subject: 概念主體（user / agent / world）。提供時隨 create patch 寫入，
                進入 patch trail（new_value["subject"]）。
            support_source: 支撐來源（user / legacy_unknown / agent_only）。由 create／expand
                patch 本身攜帶，apply 時只升不降地寫入 metadata，舊值記在 rollback_payload，
                rollback 可還原。
            similar_hint: 呼叫端已在鎖外算好的語意相近提示（create 路徑）。None 時由本方法
                自行評估（會算 embedding；LLM 呼叫）——呼叫端持有 storage_lock 時必須預先傳入。

        Returns:
            (concept_id | None, PatchTransaction)
        """
        if support_source is not None and support_source not in SUPPORT_RANK:
            raise ValueError(f"support_source 無效: {support_source!r}")

        tx = PatchTransaction(
            patches=[],
            user_input=f"[extractor] {noun}: {desc}",
            llm_response="",
            session_id="extractor-bg",
            turn_id=episode_id,
            triggered_by=triggered_by,
        )

        all_evidence_sources = source_episode_ids if source_episode_ids else [episode_id]
        evidence = PatchEvidence(
            evidence_kind=evidence_kind,
            evidence_strength=0.7,  # extractor 來源的基礎可信度
            verifier_status="valid",
            verifier_reason=f"ConceptExtractor 背景提取 (sim={similarity_score:.3f})",
            source_episode_ids=list(all_evidence_sources),
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
                support_source=support_source,
            )
            tx.patches.append(patch)
            concept_id = existing_concept_id

        else:
            # 全新概念 / 多義詞 / fallback → create
            all_source_ids = source_episode_ids if source_episode_ids else [episode_id]
            hint = similar_hint if similar_hint is not None else self.evaluate_similar(
                noun, desc, embedding,
            )
            if embedding is None and hint.embedding is not None:
                embedding = hint.embedding
            patch = ConceptPatch(
                transaction_id=tx.transaction_id,
                concept_name=noun,
                action="create",
                target_path="*",
                new_value={
                    "name": noun,
                    "description": desc,
                    "episode_id": all_source_ids[0],
                    "concept_type": concept_type,
                    "extra_source_episode_ids": all_source_ids[1:],
                    "producer": _producer_for_kind(evidence_kind),
                    **({"subject": subject} if subject else {}),
                    **self._similar_payload(hint),
                },
                patch_confidence=0.7,
                evidence=evidence,
                risk_level="low",
                rollback_payload={"embedding": embedding},
                support_source=support_source,
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
                    patch.rejection_reason = patch.rejection_reason or "SemanticMemory create 失敗"
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
            producer = payload.get("producer") or _producer_for_kind(
                patch.evidence.evidence_kind if patch.evidence else None
            )
            # subject 先驗證：非法值不能在 upsert 之後才失敗，留下一個半成品概念
            subject = payload.get("subject")
            if subject:
                self._semantic.validate_subject(subject)
            create_name = payload.get("name", patch.concept_name)
            expand_target_id = payload.get("similar_expand_target")
            if expand_target_id:
                # Gate 的 LLM 判定「與既有概念是同一件事」：改以 expand 補進該概念，不另建
                with self._semantic._lock:
                    target = self._semantic.get(str(expand_target_id))
                    if (
                        target is not None
                        and target.status == "active"
                        and not self._semantic.is_merged(target)
                    ):
                        return self._redirect_create_to_primary(
                            patch, target, payload, producer, similar=True,
                        )
                logger.info(
                    "[ConceptInhibitor] 相近概念 %s 已不可用，退回一般 create (%s)",
                    expand_target_id, create_name,
                )
            with self._semantic._lock:
                existing = self._semantic.get_by_name(create_name)
                if existing is not None and self._semantic.is_merged(existing):
                    # 已合併的名稱視為主概念的別名：導向主概念，不覆寫已合併的次概念
                    return self._redirect_create_to_primary(patch, existing, payload, producer)
            concept_id = self._semantic.upsert_concept(
                name=payload.get("name", patch.concept_name),
                description=payload.get("description", ""),
                concept_type=payload.get("concept_type", "entity"),
                source_episode_id=payload.get("episode_id"),
                embedding=embedding,
                producer=producer,
            )
            if concept_id:
                # subject 隨 create patch 寫入（已記錄於 patch.new_value，可由 audit 追溯）
                if subject:
                    self._semantic.set_subject(concept_id, subject)
                if patch.support_source:
                    with self._semantic._lock:
                        created = self._semantic.get(concept_id)
                        if created is not None and self._apply_support_source(created, patch):
                            self._semantic._save()
                # 補入剩餘的 source_episode_ids（#1 provenance 修復）與 derivations
                extra_sources = payload.get("extra_source_episode_ids", [])
                parent_concepts = payload.get("parent_concept_ids") or []
                parent_episodes = payload.get("parent_episode_ids") or []
                parent_evidence = payload.get("parent_evidence_ids") or []
                if extra_sources or parent_concepts or parent_episodes or parent_evidence:
                    ref = {"patch_id": patch.patch_id, "transaction_id": patch.transaction_id}
                    with self._semantic._lock:
                        concept = self._semantic.get(concept_id)
                        if concept is not None:
                            for ep_id in extra_sources:
                                if ep_id not in concept.source_episode_ids:
                                    concept.source_episode_ids.append(ep_id)
                                concept.add_derivation("episode", ep_id, producer, ref=ref)
                            for ep_id in parent_episodes:
                                concept.add_derivation("episode", ep_id, producer, ref=ref)
                            for ev_id in parent_evidence:
                                concept.add_derivation("evidence", ev_id, producer, ref=ref)
                            for pid in parent_concepts:
                                parent = self._semantic.get(pid)
                                concept.add_derivation(
                                    "concept", pid, producer,
                                    parent_version=parent.version if parent else None,
                                    ref=ref,
                                )
                            self._semantic._save()
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

    def _redirect_create_to_primary(
        self,
        patch: ConceptPatch,
        merged: Concept,
        payload: dict[str, Any],
        producer: str,
        similar: bool = False,
    ) -> tuple[bool, str | None]:
        """create 命中已合併的次概念：改以 expand 方式補進它併入的主概念。

        similar=True 時 merged 即為目標主概念（Gate 判定新概念與它是同一件事）。

        補入 source_episode_ids / derivation 與一筆 fact（描述），audit 記錄導向原因。
        主概念不存在、非 active 或自己也已被合併時拒絕（patch.rejection_reason 說明）。
        """
        name = str(payload.get("name", patch.concept_name)) if similar else merged.name
        with self._semantic._lock:
            if similar:
                primary = merged
            else:
                primary = self._semantic.get(str(merged.metadata.get("merged_into") or ""))
            if (
                primary is None
                or primary.status != "active"
                or self._semantic.is_merged(primary)
            ):
                patch.rejection_reason = (
                    f"同名概念「{name}」已併入其他概念，但主概念不存在或非 active，拒絕建立"
                )
                return False, None

            if similar:
                reason = f"語意相近：新概念「{name}」經 Gate 判定與「{primary.name}」為同一件事，改以 expand 補入既有概念"
            else:
                reason = f"同名概念「{name}」已併入「{primary.name}」，改以 expand 補入主概念"
            description = str(payload.get("description") or "")
            episodes = [
                e for e in [payload.get("episode_id"), *(payload.get("extra_source_episode_ids") or [])]
                if e
            ]
            old_sources = list(primary.source_episode_ids)
            old_confidence = primary.confidence
            had_facts_key = "facts" in primary.metadata
            added_fact = None
            ref = {"patch_id": patch.patch_id, "transaction_id": patch.transaction_id}
            try:
                if description and not any(
                    isinstance(f, dict) and f.get("text") == description
                    for f in primary.metadata.get("facts", [])
                ):
                    added_fact = {
                        "text": description,
                        "source": "similar_concept_expand" if similar else "merged_alias_redirect",
                        "alias": name,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                    primary.metadata.setdefault("facts", []).append(added_fact)
                for ep_id in episodes:
                    if ep_id not in primary.source_episode_ids:
                        primary.source_episode_ids.append(ep_id)
                self._apply_support_source(primary, patch)
                primary.record_mutation()
                for ep_id in episodes:
                    primary.add_derivation("episode", ep_id, producer, ref=ref)
                self._add_history_entry(primary, patch)
                self._semantic._save()
            except Exception as e:
                # 補償：回到導向前的狀態
                self._restore_support_source(primary, patch)
                primary.source_episode_ids = old_sources
                primary.remove_derivations_by_patch(patch.patch_id)
                if added_fact is not None:
                    cur = primary.metadata.get("facts", [])
                    if added_fact in cur:
                        cur.remove(added_fact)
                    if not had_facts_key and not cur:
                        primary.metadata.pop("facts", None)
                history = primary.metadata.get("patch_history", [])
                if history and history[-1].get("patch_id") == patch.patch_id:
                    history.pop()
                logger.error("[ConceptInhibitor] 同名導向主概念失敗 (%s): %s", name, e)
                patch.rejection_reason = f"導向主概念失敗: {e}"
                return False, None

            patch.rollback_payload = {
                **(patch.rollback_payload or {}),
                "redirected_to": primary.id,
                "redirect_reason": reason,
                "old_source_episode_ids": old_sources,
                "old_confidence": old_confidence,
                "added_fact": added_fact,
            }
            if patch.evidence is not None:
                patch.evidence.verifier_reason = (
                    f"{patch.evidence.verifier_reason}；{reason}"
                    if patch.evidence.verifier_reason else reason
                )
            logger.info("[ConceptInhibitor] %s", reason)
            return True, primary.id

    def _apply_expand_source(self, patch: ConceptPatch) -> bool:
        """
        執行 expand/source_episode_ids — 同義詞合併，
        新增 episode_id 到現有概念的 source_episode_ids。

        讀取 + 檢查 + 寫入在同一個 critical section，避免 TOCTOU。
        """
        concept = self._semantic.get(patch.concept_id or "")
        if concept is None:
            return False

        try:
            new_value = patch.new_value
            if isinstance(new_value, str):
                episode_ids = [new_value]
            elif isinstance(new_value, list):
                episode_ids = [e for e in new_value if isinstance(e, str)]
            else:
                episode_ids = []
            if episode_ids or patch.support_source:
                with self._semantic._lock:
                    fresh = [e for e in episode_ids if e not in concept.source_episode_ids]
                    support_changed = self._apply_support_source(concept, patch)
                    if fresh or support_changed:
                        for ep_id in fresh:
                            concept.source_episode_ids.append(ep_id)
                        if fresh:
                            concept.access_count += 1
                        concept.record_mutation()
                        # 先升版再記 derivation，紀錄才落在新版本
                        for ep_id in fresh:
                            concept.add_derivation(
                                "episode", ep_id,
                                _producer_for_kind(
                                    patch.evidence.evidence_kind if patch.evidence else None
                                ),
                                ref={"patch_id": patch.patch_id, "transaction_id": patch.transaction_id},
                            )
                        # history 先寫再存檔，否則這筆紀錄不會落地
                        self._add_history_entry(concept, patch)
                        self._semantic._save()
            return True
        except Exception as e:
            logger.error(
                "[ConceptInhibitor] _apply_expand_source 失敗 (%s): %s",
                patch.patch_id, e,
            )
            return False

    # ──────────────────────────────────────────────
    # 語意相近提示（create 前）
    # ──────────────────────────────────────────────

    def evaluate_similar(
        self,
        noun: str,
        desc: str,
        embedding: list[float] | None = None,
        judge: bool = True,
    ) -> SimilarHint:
        """新概念要 create 前，以 embedding 找語意相近的 active 概念並（有 llm_fn 時）請 LLM 判斷。

        embedding 計算與 LLM 呼叫都在這裡，且不持有 SemanticMemory／storage_lock 的鎖——
        持有 storage_lock 的呼叫端（蒸餾、Reflection）必須在進鎖前先呼叫本方法，
        再把結果以 similar_hint 帶進 process_*_patch。

        只用 embedding cosine 選候選（門檻 CONCEPT_SIMILAR_HINT_THRESHOLD、top 3），
        不用名稱字串相似度或 keyword。已合併／非 active 概念不列入；與新概念同名者（同一個
        upsert 目標，不是相近概念）也不列入。沒有 llm_fn 或 LLM 失敗時只記候選，維持 create。
        任何失敗都不影響 create。judge=False 時只找候選、不呼叫 LLM（Reflection 用：
        它是明確提議新概念，候選只記 audit）。
        """
        hint = SimilarHint()
        try:
            vec = embedding
            if vec is None:
                embed_fn = getattr(self._semantic, "_embed_fn", None)
                if embed_fn is None:
                    return hint
                vec = embed_fn(f"{noun}: {desc}")
                hint.embedding = [float(x) for x in vec]
            found = self._semantic.find_similar_concepts(
                vec, self._similar_hint_threshold, top_k=3, exclude_names=[noun],
            )
        except Exception as e:
            logger.warning("[ConceptInhibitor] 語意相近提示失敗（不影響 create）: %s", e)
            return hint

        hint.candidates = [
            {
                "concept_id": c.id,
                "name": c.name,
                "description": (c.description or "")[:120],
                "similarity": round(sim, 4),
            }
            for c, sim in found
        ]
        if judge and hint.candidates and self._llm_fn is not None:
            self._judge_similar(noun, desc, hint)
        return hint

    def _judge_similar(self, noun: str, desc: str, hint: SimilarHint) -> None:
        """請 LLM 判斷新概念是同一件事（expand 既有）或不同概念（create）；失敗維持 create。"""
        assert self._llm_fn is not None
        lines = [
            f"{i}. [id={c['concept_id']}] {c['name']}（cosine {c['similarity']:.3f}）：{c['description']}"
            for i, c in enumerate(hint.candidates, 1)
        ]
        prompt = _SIMILAR_HINT_PROMPT.format(
            name=noun, description=desc[:300], candidates="\n".join(lines),
        )
        try:
            raw = self._llm_fn(prompt)
        except Exception as e:
            hint.reason = f"LLM 判斷失敗，維持 create: {e}"
            logger.warning("[ConceptInhibitor] 相近概念 LLM 判斷失敗: %s", e)
            return
        hint.llm_used = True
        text = (raw or "").strip()
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            text = text[start:end + 1]
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
        if not isinstance(parsed, dict):
            hint.reason = "LLM 回應格式錯誤，維持 create"
            return
        hint.reason = str(parsed.get("reason") or "")
        if parsed.get("decision") != "expand":
            return
        target_id = str(parsed.get("target_id") or "")
        if target_id not in {c["concept_id"] for c in hint.candidates}:
            hint.reason = f"LLM 指定的 expand 目標不在候選內，維持 create（{hint.reason}）"
            return
        hint.decision = "expand"
        hint.target_id = target_id

    @staticmethod
    def _similar_payload(hint: SimilarHint) -> dict[str, Any]:
        """把提示轉成 create patch new_value 的欄位（進入 patch trail／audit）。"""
        if not hint.candidates:
            return {}
        payload: dict[str, Any] = {
            "similar_candidates": hint.audit_candidates(),
            "similar_decision": hint.audit_decision(),
        }
        if hint.decision == "expand" and hint.target_id:
            payload["similar_expand_target"] = hint.target_id
        return payload

    def _apply_support_source(self, concept: Concept, patch: ConceptPatch) -> bool:
        """套用 patch 攜帶的 support_source（只升不降）。須在 SemanticMemory._lock 內呼叫。

        有變更時把舊值記入 patch.rollback_payload["support_source_undo"]，供 rollback 還原。
        回傳 metadata 是否有變。
        """
        value = patch.support_source
        if not value:
            return False
        rank = SUPPORT_RANK.get(value)
        if rank is None:
            raise ValueError(f"support_source 無效: {value!r}")
        current = concept.metadata.get("support_source")
        if rank <= SUPPORT_RANK.get(current, -1):
            return False
        if patch.rollback_payload is None:
            patch.rollback_payload = {}
        patch.rollback_payload["support_source_undo"] = {
            "present": current is not None,
            "value": current,
        }
        concept.metadata["support_source"] = value
        return True

    @staticmethod
    def _restore_support_source(concept: Concept, patch: ConceptPatch) -> None:
        """還原 _apply_support_source 前的 support_source。須在 SemanticMemory._lock 內呼叫。"""
        undo = (patch.rollback_payload or {}).get("support_source_undo")
        if not undo:
            return
        if undo.get("present"):
            concept.metadata["support_source"] = undo.get("value")
        else:
            concept.metadata.pop("support_source", None)

    # ──────────────────────────────────────────────
    # 矛盾標記入口（ContradictionDetector / Dream）
    # ──────────────────────────────────────────────

    def process_conflict_mark(
        self,
        concept_a_id: str,
        concept_b_id: str,
        strength: float = 0.0,
        reason: str = "",
        triggered_by: str = "dream",
    ) -> tuple[bool, PatchTransaction]:
        """
        對一組互相矛盾的概念各建立一筆 mark_conflict patch 並套用。

        機器偵測的標記，不需 LLM 佐證（規則層直接 approve，evidence 比照 dream 來源）。
        流程：建 patch → audit-before-mutate（audit 寫入失敗則整筆不套用）→
        逐一 apply（record_mutation 使 version 遞增）→ 任一失敗則回滾已套用者 → 終態 audit。
        不在 Semantic 中的概念略過（與舊行為相同）；兩端皆不存在視為無變更（成功）。

        Returns:
            (success, PatchTransaction) — success=False 時沒有任何概念被改動
        """
        tx = PatchTransaction(
            patches=[],
            user_input=f"[contradiction] {concept_a_id} <-> {concept_b_id}",
            llm_response="",
            session_id="contradiction-dream",
            turn_id="",
            triggered_by=triggered_by,  # type: ignore[arg-type]
        )
        evidence = PatchEvidence(
            evidence_kind="dream_consolidation",
            evidence_strength=0.7,
            verifier_status="valid",
            verifier_reason=reason or f"ContradictionDetector 偵測矛盾 (strength={strength:.2f})",
            retrieved_concept_ids=[concept_a_id, concept_b_id],
        )

        with self._semantic._lock:
            for cid, rival in ((concept_a_id, concept_b_id), (concept_b_id, concept_a_id)):
                concept = self._semantic.get(cid)
                if concept is None:
                    continue
                patch = ConceptPatch(
                    transaction_id=tx.transaction_id,
                    concept_id=cid,
                    concept_name=concept.name,
                    action="mark_conflict",
                    target_path="conflict_count",
                    old_value=concept.conflict_count,
                    new_value={"conflict_count_delta": 1, "rival_concept_id": rival},
                    old_confidence=concept.confidence,
                    new_confidence=concept.confidence,
                    patch_confidence=0.7,
                    evidence=evidence,
                    risk_level="low",
                )
                patch.status = "approved"
                tx.patches.append(patch)

        if not tx.patches:
            tx.status = "committed"
            return True, tx

        # audit-before-mutate：先寫 audit，失敗則不改動任何概念
        if not self._try_write_audit(tx):
            for p in tx.patches:
                p.status = "rejected"
                p.rejection_reason = "audit log 寫入失敗，阻止 mutation"
            tx.status = "failed"
            return False, tx

        applied: list[ConceptPatch] = []
        failed = False
        for patch in tx.patches:
            if self._apply_patch(patch):
                patch.status = "applied"
                applied.append(patch)
            else:
                patch.status = "rejected"
                patch.rejection_reason = "SemanticMemory mark_conflict 失敗"
                failed = True
                # 失敗的 patch 可能已改了記憶體（例如存檔失敗），一併回滾
                self._rollback_patch(patch)
                break

        if failed:
            for p in reversed(applied):
                self._rollback_patch(p)
                p.status = "rolled_back"
            for p in tx.patches:
                if p.status == "approved":
                    p.status = "rejected"
                    p.rejection_reason = "同一 transaction 其他 patch 失敗"
            tx.status = "failed"
        else:
            tx.status = "committed"
        self._write_audit(tx)
        return (not failed), tx

    def rollback_conflict_mark(self, tx: PatchTransaction) -> None:
        """回滾 process_conflict_mark 已套用的 patch（呼叫端後續步驟失敗時使用）。"""
        for p in reversed(tx.applied_patches):
            if p.action == "mark_conflict":
                self._rollback_patch(p)
                p.status = "rolled_back"
        tx.status = "rolled_back"
        self._write_audit(tx)

    # ──────────────────────────────────────────────
    # Reflection Pipeline 入口
    # ──────────────────────────────────────────────

    def process_reflection_patch(
        self,
        operation: str,
        target_name: str,
        params: dict[str, Any],
        reasoning: str = "",
    ) -> tuple[bool, dict[str, Any]]:
        """
        處理來自 ReflectionExecutor 的概念變更請求。

        Reflection 來源的 patch 已經過 LLM 反思決策，自動 approve。
        主要目的是確保所有 SemanticMemory 變更都經過 audit pipeline，
        不再直接呼叫 SemanticMemory 公開 API。

        支援的 operation:
          - create: 建立新概念
          - tag: 追加標籤到概念 metadata
          - merge: 合併兩個概念（主概念吸收次概念）

        Args:
            operation: 操作類型 ("create" | "tag" | "merge")
            target_name: 目標概念名稱
            params: 操作參數（各 operation 不同）
            reasoning: 反思理由（寫入 audit）

        Returns:
            (success, result_dict)
        """
        tx = PatchTransaction(
            patches=[],
            user_input=f"[reflection] {operation}: {target_name}",
            llm_response="",
            session_id="reflection",
            turn_id="",
            triggered_by="reflection",
        )

        try:
            if operation == "create":
                success, result = self._reflection_create(tx, target_name, params, reasoning)
            elif operation == "tag":
                success, result = self._reflection_tag(tx, target_name, params, reasoning)
            elif operation == "merge":
                success, result = self._reflection_merge(tx, target_name, params, reasoning)
            else:
                raise ValueError(f"未知的 reflection operation: {operation}")

            tx.status = "committed" if success else "failed"
        except Exception as e:
            tx.status = "failed"
            success = False
            result = {"error": str(e)}
            logger.error("[ConceptInhibitor] reflection patch 失敗: %s", e)

        self._write_audit(tx)

        if success:
            logger.info(
                "[ConceptInhibitor] reflection %s: %s — %d patches applied",
                operation, target_name, len(tx.applied_patches),
            )

        return success, result

    def _reflection_create(
        self,
        tx: PatchTransaction,
        target_name: str,
        params: dict[str, Any],
        reasoning: str,
    ) -> tuple[bool, dict[str, Any]]:
        """Reflection create — 建立新概念並走 patch audit。"""
        description = str(params.get("description", ""))
        concept_type = str(params.get("concept_type", "reflection"))

        existing = self._semantic.get_by_name(target_name)
        if existing is not None and self._semantic.is_merged(existing):
            # 已合併的名稱是主概念的占位（別名），不重建同名概念
            return False, {
                "error": f"create 拒絕：「{target_name}」已併入其他概念，不重建同名概念",
            }

        evidence = PatchEvidence(
            evidence_kind="reflection_create",
            evidence_strength=0.8,
            verifier_status="valid",
            verifier_reason=reasoning or "ReflectionExecutor create_concept",
        )

        # 語意相近提示：Reflection 是明確提議新概念，只把候選記進 audit，不導向 expand、不問 LLM。
        # 呼叫端（持 storage_lock 的 executor）應已預先算好並放進 params；沒有才在這裡評估
        hint = params.get("similar_hint")
        if not isinstance(hint, SimilarHint):
            hint = self.evaluate_similar(target_name, description, judge=False)
        support_source = params.get("support_source")
        if support_source is not None and support_source not in SUPPORT_RANK:
            raise ValueError(f"support_source 無效: {support_source!r}")

        patch = ConceptPatch(
            transaction_id=tx.transaction_id,
            concept_name=target_name,
            action="create",
            target_path="*",
            new_value={
                "name": target_name,
                "description": description,
                "concept_type": concept_type,
                # 來源：Reflection 從既有概念產生時，parent 指向那些概念（而非只抄 episode）
                "parent_concept_ids": self._resolve_parent_concept_ids(
                    params.get("parent_concepts"),
                ),
                "parent_episode_ids": [str(x) for x in params.get("parent_episode_ids") or []],
                "parent_evidence_ids": [str(x) for x in params.get("parent_evidence_ids") or []],
                **{
                    k: v for k, v in self._similar_payload(hint).items()
                    if k != "similar_expand_target"
                },
            },
            patch_confidence=0.8,
            evidence=evidence,
            risk_level="low",
            rollback_payload={},
            support_source=support_source,
        )
        patch.status = "approved"
        tx.patches.append(patch)

        # 執行 create
        success, concept_id = self._apply_create(patch, embedding=hint.embedding)
        if success:
            patch.status = "applied"
            patch.concept_id = concept_id
            return True, {"concept_id": concept_id, "name": target_name}
        else:
            patch.status = "rejected"
            patch.rejection_reason = patch.rejection_reason or "SemanticMemory create 失敗"
            return False, {"error": f"create 失敗: {target_name}"}

    def _resolve_parent_concept_ids(self, refs: Any) -> list[str]:
        """把 parent 概念的名稱或 ID 解析為 ID；找不到的略過（記 warning）。"""
        if not refs:
            return []
        if isinstance(refs, str):
            refs = [refs]
        ids: list[str] = []
        for ref in refs:
            found = self._semantic.get(str(ref)) or self._semantic.get_by_name(str(ref))
            if found is None:
                logger.warning("[ConceptInhibitor] reflection parent 概念不存在，略過: %s", ref)
                continue
            if found.id not in ids:
                ids.append(found.id)
        return ids

    def _reflection_tag(
        self,
        tx: PatchTransaction,
        target_name: str,
        params: dict[str, Any],
        reasoning: str,
    ) -> tuple[bool, dict[str, Any]]:
        """Reflection tag — 追加標籤到概念 metadata 並走 patch audit。

        讀取現有 tags → 計算合併 → 建 patch → mutation → save
        全部在同一個 critical section 內，避免併發 lost update。
        """
        new_tags = params.get("tags", [])
        if not isinstance(new_tags, list):
            new_tags = [str(new_tags)]

        with self._semantic._lock:
            concept = self._semantic.get_by_name(target_name)
            if concept is None:
                return False, {"error": f"概念不存在: {target_name}"}

            # 在 lock 內讀取現有 tags 並計算合併結果
            existing_tags = list(concept.metadata.get("tags", []))
            merged_tags = list(set(existing_tags + new_tags))

            evidence = PatchEvidence(
                evidence_kind="reflection_tag",
                evidence_strength=0.8,
                verifier_status="valid",
                verifier_reason=reasoning or "ReflectionExecutor tag_concept",
                retrieved_concept_ids=[concept.id],
            )

            patch = ConceptPatch(
                transaction_id=tx.transaction_id,
                concept_id=concept.id,
                concept_name=target_name,
                action="expand",
                expand_mode=ExpandMode.ADD_FACT,
                target_path="metadata.tags",
                old_value=existing_tags,
                new_value=merged_tags,
                old_confidence=concept.confidence,
                new_confidence=concept.confidence,
                patch_confidence=0.8,
                evidence=evidence,
                risk_level="low",
                rollback_payload={"old_tags": existing_tags},
            )
            patch.status = "approved"
            tx.patches.append(patch)

            # 執行 tag 更新
            try:
                concept.metadata["tags"] = merged_tags
                concept.updated_at = datetime.now(timezone.utc)
                self._add_history_entry(concept, patch)
                concept.record_mutation()
                self._semantic._save()
                patch.status = "applied"
                return True, {"old_tags": existing_tags, "new_tags": merged_tags}
            except Exception as e:
                patch.status = "rejected"
                patch.rejection_reason = str(e)
                concept.metadata["tags"] = existing_tags
                return False, {"error": str(e)}

    def _reflection_merge(
        self,
        tx: PatchTransaction,
        target_name: str,
        params: dict[str, Any],
        reasoning: str,
    ) -> tuple[bool, dict[str, Any]]:
        """Reflection merge — 合併兩個概念並走 patch audit。

        讀取 primary/secondary → 計算合併後狀態 → 建 patch → mutation → save
        → remove secondary 全部在同一個 critical section 內。
        RLock 允許 remove_concept 內部的重入加鎖。
        """
        merge_with = str(params.get("merge_with", ""))
        if not merge_with:
            return False, {"error": "merge_concepts 缺少 merge_with 參數"}

        if target_name == merge_with:
            return False, {"error": f"merge_concepts 不允許自我合併: {target_name}"}

        with self._semantic._lock:
            primary = self._semantic.get_by_name(target_name)
            if primary is None:
                return False, {"error": f"主概念不存在: {target_name}"}

            secondary = self._semantic.get_by_name(merge_with)
            if secondary is None:
                return False, {"error": f"次概念不存在: {merge_with}"}

            # 兩邊都必須是 active 且未被合併，否則會把已併入他處／已棄用的概念再合併一次
            for label, c in (("主概念", primary), ("次概念", secondary)):
                if c.status != "active" or self._semantic.is_merged(c):
                    return False, {
                        "error": f"{label}「{c.name}」不是 active 或已被合併，拒絕 merge",
                    }

            evidence = PatchEvidence(
                evidence_kind="reflection_merge",
                evidence_strength=0.8,
                verifier_status="valid",
                verifier_reason=reasoning or "ReflectionExecutor merge_concepts",
                retrieved_concept_ids=[primary.id, secondary.id],
            )

            # 在 lock 內讀取並計算合併後的資料
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

            # Patch 1: modify primary（用 lock 內讀到的最新狀態建 patch）
            modify_patch = ConceptPatch(
                transaction_id=tx.transaction_id,
                concept_id=primary.id,
                concept_name=target_name,
                action="modify",
                target_path="description",
                old_value=primary.description,
                new_value=merged_desc,
                old_confidence=primary.confidence,
                new_confidence=primary.confidence,
                patch_confidence=0.8,
                evidence=evidence,
                risk_level="high",
                rollback_payload={
                    "old_description": primary.description,
                    "old_confidence": primary.confidence,
                    "old_metadata": copy.deepcopy(primary.metadata),
                },
            )
            modify_patch.status = "approved"
            tx.patches.append(modify_patch)

            # Patch 2: merge (remove secondary)
            merge_patch = ConceptPatch(
                transaction_id=tx.transaction_id,
                concept_id=secondary.id,
                concept_name=merge_with,
                action="merge",
                target_path="*",
                old_value={
                    "name": secondary.name,
                    "description": secondary.description,
                },
                merge_target_id=primary.id,
                patch_confidence=0.8,
                evidence=evidence,
                risk_level="high",
                rollback_payload={
                    "secondary_name": secondary.name,
                    "secondary_description": secondary.description,
                    "secondary_concept_type": secondary.concept_type,
                    "secondary_metadata": dict(secondary.metadata),
                    "secondary_source_episode_ids": list(secondary.source_episode_ids),
                },
            )
            merge_patch.status = "approved"
            tx.patches.append(merge_patch)

            # 保存 primary 原始狀態供失敗時還原
            original_desc = primary.description
            original_meta = copy.deepcopy(primary.metadata)

            # 執行 Step 1: 更新 primary
            old_embedding = list(primary.embedding)
            try:
                # 先算新 embedding：失敗時 primary 尚未被改動
                new_embedding = None
                if self._semantic._embed_fn is not None:
                    modify_patch.rollback_payload = modify_patch.rollback_payload or {}
                    modify_patch.rollback_payload["old_embedding"] = old_embedding
                    new_vec = self._semantic._embed_fn(f"{primary.name}: {merged_desc}")
                    new_embedding = self._semantic._normalize_vector(new_vec).tolist()
                primary.description = merged_desc
                primary.metadata = merged_meta
                primary.updated_at = datetime.now(timezone.utc)
                if new_embedding is not None:
                    primary.embedding = new_embedding
                    self._semantic._update_embedding_index(primary, is_update=True)
                self._add_history_entry(primary, modify_patch)
                primary.record_mutation()
                # primary 吸收 secondary 的來源：複製 secondary 的有效 derivation
                # （secondary 之後會被移除，不能只指向它）
                merge_ref = {
                    "patch_id": merge_patch.patch_id,
                    "transaction_id": tx.transaction_id,
                    "merged_from": secondary.id,
                }
                for raw in secondary.derivations:
                    primary.add_derivation(
                        raw.get("kind", "episode"), raw["id"], "reflection",
                        parent_version=raw.get("parent_version"), ref=merge_ref,
                    )
                for ep_id in secondary.source_episode_ids:
                    primary.add_derivation("episode", ep_id, "reflection", ref=merge_ref)
                self._semantic._save()
                modify_patch.status = "applied"
            except Exception as e:
                modify_patch.status = "rejected"
                modify_patch.rejection_reason = str(e)
                # 半套變更還原（description／metadata／embedding／derivation 可能已被改）
                self._compensate_merged_primary(
                    primary, original_desc, original_meta, merge_patch.patch_id,
                    (modify_patch.rollback_payload or {}).get("old_embedding"),
                )
                return False, {"error": f"merge modify 失敗: {e}"}

            # 執行 Step 2: 次概念轉非 active（deprecate != delete）；主概念合併前快照存進
            # 次概念 metadata，供 ReflectionExecutor 回滾（RLock 允許重入）
            secondary.metadata["merge_primary_snapshot"] = {
                "merge_patch_id": merge_patch.patch_id,
                "old_description": original_desc,
                "old_confidence": modify_patch.old_confidence,
                "old_embedding": old_embedding,
                "old_metadata_keys": copy.deepcopy({
                    k: original_meta[k]
                    for k in ("facts", "merged_from", "merged_source_episodes")
                    if k in original_meta
                }),
                "merged_description": merged_desc,
                # 合併完成後的主概念版本：回滾時版本不符代表合併後又被改過，拒絕回滾
                "merged_primary_version": primary.version,
            }
            try:
                removed = self._semantic.mark_merged(merge_with, primary.id, merge_patch.patch_id)
                mark_error = "" if removed else "mark_merged 回傳 False"
            except Exception as e:
                removed = False
                mark_error = str(e)
            if not removed:
                secondary.metadata.pop("merge_primary_snapshot", None)
                # 還原 primary 到合併前狀態
                logger.warning(
                    "[ConceptInhibitor] reflection merge 標記次概念失敗（%s），還原 primary: %s",
                    mark_error, primary.name,
                )
                self._compensate_merged_primary(
                    primary, original_desc, original_meta, merge_patch.patch_id,
                    (modify_patch.rollback_payload or {}).get("old_embedding"),
                )
                modify_patch.status = "rolled_back"
                merge_patch.status = "rejected"
                merge_patch.rejection_reason = "次概念移除失敗"
                return False, {"error": f"merge remove 失敗: {merge_with}，已還原 primary"}

            merge_patch.status = "applied"
            return True, {
                "primary": target_name,
                "secondary_removed": merge_with,
                "merged_description_length": len(merged_desc),
            }

    def _compensate_merged_primary(
        self,
        primary: Concept,
        original_desc: str,
        original_meta: dict[str, Any],
        merge_patch_id: str,
        old_embedding: list[float] | None,
    ) -> None:
        """merge 失敗時把主概念還原到合併前（記憶體 + 磁碟）；補償本身失敗只記錄，不再拋出。"""
        primary.description = original_desc
        primary.metadata = copy.deepcopy(original_meta)
        primary.remove_derivations_by_patch(merge_patch_id)
        if old_embedding is not None:
            primary.embedding = list(old_embedding)
            try:
                self._semantic._update_embedding_index(primary, is_update=True)
            except Exception as e:
                logger.error("[ConceptInhibitor] 補償 embedding index 失敗: %s", e)
        try:
            self._semantic._save()
        except Exception as e:
            logger.error("[ConceptInhibitor] 補償存檔失敗: %s", e)

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
    # Held 重審
    # ──────────────────────────────────────────────

    def _held_path(self) -> Path:
        return Path(self._semantic.storage_path) / "held_patches.json"

    @staticmethod
    def _read_text_retry(path: Path, retries: int = 5, delay: float = 0.05) -> str:
        """讀檔；OSError（如跨程序 os.replace 瞬間的 PermissionError）短暫重試，仍失敗才拋出。"""
        for attempt in range(retries):
            try:
                return path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                raise  # 內容損壞，不是暫時性錯誤（UnicodeDecodeError 屬 ValueError）
            except OSError:
                if attempt == retries - 1:
                    raise
                time.sleep(delay * (2 ** attempt))
        raise OSError(f"無法讀取 {path}")  # 不可達，滿足型別檢查

    @contextlib.contextmanager
    def _held_guard(self):
        """held 讀改寫臨界區：storage_lock（與 engine 同一把，同執行緒可重入）再 _held_lock。

        鎖序 storage_lock -> _held_lock；臨界區內不得呼叫 LLM。
        """
        storage = Path(self._semantic.storage_path)
        ns_path = storage.parent if storage.name == "semantic" else storage
        with storage_lock(ns_path):
            with self._held_lock:
                yield

    def _read_held_file(self) -> dict[str, dict[str, Any]]:
        """從磁碟讀 held 登記簿；檔案損壞時先備份壞檔再視為空。呼叫方須持有 _held_lock。"""
        path = self._held_path()
        try:
            if not path.exists():
                return {}
            text = self._read_text_retry(path)
            data = json.loads(text)
            if isinstance(data, dict):
                return data
            raise ValueError("held 登記簿根節點不是 dict")
        except OSError:
            raise  # 暫時性 I/O 錯誤：不改名、不當空檔，交給呼叫端略過本次處理
        except Exception as e:
            logger.warning("[ConceptInhibitor] 讀取 held 登記簿失敗，備份壞檔並視為空: %s", e)
            try:
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
                replace_with_retry(path, path.with_name(f"held_patches.corrupt-{stamp}.json"))
            except Exception as backup_err:
                logger.warning("[ConceptInhibitor] 備份損壞的 held 登記簿失敗: %s", backup_err)
            return {}

    def _held_registry(self) -> dict[str, dict[str, Any]]:
        """取得 held 登記簿。每次都從磁碟 reload（多個實例共用同一份檔），呼叫方須持有 _held_lock。

        回傳的 dict 即 self._held；修改後須呼叫 _save_held() 才會落地。
        """
        self._held = self._read_held_file()
        return self._held

    def _save_held(self) -> None:
        """持久化 held 登記簿（唯一暫存檔 + 原子替換）。呼叫方須持有 _held_lock。"""
        try:
            atomic_write_json(self._held_path(), self._held or {}, indent=None)
        except Exception as e:
            logger.warning("[ConceptInhibitor] 寫入 held 登記簿失敗: %s", e)

    def _register_held(
        self,
        patch: ConceptPatch,
        tx: PatchTransaction,
        user_input: str,
        llm_response: str,
    ) -> None:
        """將 held patch 登記進重審佇列（登記簿暫時讀不到時略過並記 warning，不覆寫健康檔）。"""
        try:
            self._register_held_locked(patch, tx, user_input, llm_response)
        except OSError as e:
            logger.warning("[ConceptInhibitor] held 登記簿暫時無法讀取，略過登記 %s: %s", patch.patch_id, e)

    def _register_held_locked(
        self,
        patch: ConceptPatch,
        tx: PatchTransaction,
        user_input: str,
        llm_response: str,
    ) -> None:
        with self._held_guard():
            self._held_registry()[patch.patch_id] = {
                "patch": patch.to_dict(),
                "retries": 0,
                "held_at": datetime.now(timezone.utc).isoformat(),
                "context": {
                    "user_input": user_input,
                    "llm_response": llm_response,
                    "session_id": tx.session_id,
                    "turn_id": tx.turn_id,
                    "triggered_by": tx.triggered_by,
                },
            }
            self._save_held()

    def held_count(self) -> int:
        """目前仍待重審的 held patch 數量。"""
        try:
            with self._held_guard():
                return len(self._held_registry())
        except OSError as e:
            logger.warning("[ConceptInhibitor] held 登記簿暫時無法讀取: %s", e)
            return 0

    def review_held_patches(
        self,
        guard: Callable[[], Any] | None = None,
        limit: int | None = None,
    ) -> list[PatchTransaction]:
        """
        重審先前 held 的 patch（公共入口）。

        觸發點：process() 開頭、Dream 週期開始（DreamPatchCoordinator.review_held）。
        每筆 patch 的流程：
          1. 逾期（held 超過 max_age_days）-> 最終 rejected
          2. 概念已不存在或內容已被他處改動（stale）-> 最終 rejected
          3. 無 llm_fn 無法重審 -> 略過（不計次數，等 LLM 恢復或逾期）
          4. 重走 _evaluate_patch（LLM 在 guard 外）；approved 則在 guard 內再檢查
             staleness 後 apply；仍 held 則計一次，達 max_retries 轉最終 rejected
        每次狀態變化都寫一筆 audit transaction，讓 held 不再是軌跡終點。

        Args:
            guard: 回傳 context manager 的工廠（如 storage_lock）。apply 時使用；None 則不加鎖。
            limit: 本次最多重審筆數。None 取 config（concept_held_review_batch）。

        Returns:
            本次產生的 audit PatchTransaction 列表
        """
        batch = limit if limit is not None else self._held_review_batch
        # 認領：在鎖內重讀磁碟、挑出未被他人認領的項目並標記 in-flight，之後才處理；
        # 其他實例／執行緒看到認領標記就略過，同一筆 patch 不會被重複套用
        try:
            claimed_ctx = self._claim_held(batch)
        except OSError as e:
            logger.warning("[ConceptInhibitor] held 登記簿暫時無法讀取，略過本次重審: %s", e)
            return []
        if not claimed_ctx:
            return []
        entries = claimed_ctx
        results: list[PatchTransaction] = []
        for entry in entries:
            patch_id = (entry.get("patch") or {}).get("patch_id", "")
            try:
                tx = self._review_one_held(entry, guard or contextlib.nullcontext)
            except Exception as e:
                logger.warning("[ConceptInhibitor] 單筆 held 重審失敗: %s", e)
                tx = None
            finally:
                self._release_claim(patch_id)
            if tx is not None:
                results.append(tx)
        return results

    def _claim_held(self, batch: int) -> list[dict[str, Any]]:
        """在臨界區內重讀磁碟、認領未被他人認領的 entry，回傳其深拷貝。"""
        with self._held_guard():
            registry = self._held_registry()
            if not registry:
                return []
            now = datetime.now(timezone.utc)
            claimable = [
                e for e in registry.values() if not self._is_claimed(e, now)
            ]
            claimable.sort(key=lambda e: e.get("held_at", ""))
            claimed = claimable[: max(batch, 0)]
            for entry in claimed:
                entry["claimed_at"] = now.isoformat()
            if claimed:
                self._save_held()
            return copy.deepcopy(claimed)

    @staticmethod
    def _is_claimed(entry: dict[str, Any], now: datetime) -> bool:
        """entry 是否仍被某個 reviewer 認領（認領逾時視為認領者已中斷）。"""
        claimed_at = entry.get("claimed_at")
        if not claimed_at:
            return False
        try:
            at = datetime.fromisoformat(claimed_at)
        except Exception:
            return False
        return (now - at).total_seconds() < _HELD_CLAIM_TTL_SECONDS

    def _release_claim(self, patch_id: str) -> None:
        """放回仍在登記簿的 held entry：清除 in-flight 標記（已 finalize 的 entry 不存在，略過）。"""
        if not patch_id:
            return
        try:
            with self._held_guard():
                live = self._held_registry().get(patch_id)
                if live is not None and "claimed_at" in live:
                    live.pop("claimed_at", None)
                    self._save_held()
        except OSError as e:
            logger.warning("[ConceptInhibitor] 放回 held 認領失敗（待認領逾時自動釋放）: %s", e)

    def _is_stale_patch(self, patch: ConceptPatch) -> bool:
        """patch 的前提（建立時的概念狀態）是否已失效。"""
        concept = self._semantic.get(patch.concept_id or "")
        if concept is None:
            return True
        if patch.action == "modify":
            return concept.description != patch.old_value
        if patch.action == "deprecate":
            return concept.status != patch.old_value
        if patch.action == "expand":
            # 冪等：同一筆 fact／來源 episode 已經在概念上，視為已套用（stale），不重複追加
            if patch.target_path == "source_episode_ids":
                return (
                    isinstance(patch.new_value, str)
                    and patch.new_value in concept.source_episode_ids
                )
            if patch.target_path == "metadata.facts":
                return patch.new_value in concept.facts
        return False

    def _review_one_held(
        self,
        entry: dict[str, Any],
        guard: Callable[[], Any],
    ) -> PatchTransaction | None:
        patch = ConceptPatch.from_dict(entry["patch"])
        ctx = entry.get("context", {})
        retries = int(entry.get("retries", 0))
        user_input = ctx.get("user_input", "")
        llm_response = ctx.get("llm_response", "")

        try:
            held_at = datetime.fromisoformat(entry["held_at"])
        except Exception:
            held_at = datetime.now(timezone.utc)
        if datetime.now(timezone.utc) - held_at > timedelta(days=self._held_max_age_days):
            patch.status = "rejected"
            patch.rejection_reason = f"held 逾期未能重審（>{self._held_max_age_days:g} 天）"
            return self._finalize_held(patch, ctx)

        if self._is_stale_patch(patch):
            patch.status = "rejected"
            patch.rejection_reason = "held 重審：概念已不存在或已被改動，patch 前提失效"
            return self._finalize_held(patch, ctx)

        if self._llm_fn is None:
            return None  # 沒有 LLM 無法重審，不計次

        retries += 1
        patch.status = "pending"
        self._evaluate_patch(patch, user_input, llm_response)  # LLM 在 guard 外

        failed_apply = False
        if patch.status == "approved":
            with guard():
                if self._is_stale_patch(patch):
                    patch.status = "rejected"
                    patch.rejection_reason = "held 重審：核准後概念已被改動，patch 前提失效"
                elif self._apply_patch(patch):
                    patch.status = "applied"
                else:
                    patch.status = "rejected"
                    patch.rejection_reason = "SemanticMemory apply 失敗（held 重審）"
                    failed_apply = True
            return self._finalize_held(patch, ctx, failed=failed_apply)

        if patch.status == "held":
            if retries >= self._held_max_retries:
                patch.status = "rejected"
                patch.rejection_reason = f"held 重審超過上限（{retries} 次）仍無法判定"
                return self._finalize_held(patch, ctx)
            try:
                with self._held_guard():
                    live = self._held_registry().get(patch.patch_id)
                    if live is not None:
                        live["retries"] = retries
                        live.pop("claimed_at", None)
                        self._save_held()
            except OSError as e:
                logger.warning("[ConceptInhibitor] 更新 held 重試次數失敗: %s", e)
            return self._write_held_audit(patch, ctx, status="committed")

        # rejected（LLM 不佐證 / 規則拒絕）-> 最終
        return self._finalize_held(patch, ctx)

    def _finalize_held(
        self,
        patch: ConceptPatch,
        ctx: dict[str, Any],
        failed: bool = False,
    ) -> PatchTransaction:
        """held patch 進入最終狀態：移出登記簿並寫 audit。"""
        try:
            with self._held_guard():
                if self._held_registry().pop(patch.patch_id, None) is not None:
                    self._save_held()
        except OSError as e:
            logger.warning("[ConceptInhibitor] 移出 held 登記簿失敗（仍寫 audit）: %s", e)
        return self._write_held_audit(
            patch, ctx, status="failed" if failed else "committed",
        )

    def _write_held_audit(
        self,
        patch: ConceptPatch,
        ctx: dict[str, Any],
        status: str,
    ) -> PatchTransaction:
        tx = PatchTransaction(
            patches=[patch],
            user_input=ctx.get("user_input", ""),
            llm_response=ctx.get("llm_response", ""),
            session_id=ctx.get("session_id", ""),
            turn_id=ctx.get("turn_id", ""),
            triggered_by=ctx.get("triggered_by", "conversation"),
            status=status,
        )
        self._write_audit(tx)
        logger.info("[ConceptInhibitor] held 重審: %s — %s", tx.summary(), patch.concept_name)
        return tx

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
        """執行單一 patch 到 SemanticMemory。成功時更新 Concept Health 指標。

        mutation+save 在 SemanticMemory._lock 內完成，確保原子性。
        """
        concept = self._semantic.get(patch.concept_id or "")
        if concept is None:
            return False

        try:
            with self._semantic._lock:
                if patch.action == "expand":
                    success = self._apply_expand(concept, patch)
                elif patch.action == "modify":
                    success = self._apply_modify(concept, patch)
                elif patch.action == "deprecate":
                    success = self._apply_deprecate(concept, patch)
                elif patch.action == "mark_conflict":
                    success = self._apply_mark_conflict(concept, patch)
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
        """執行 expand/ADD_FACT — 新增 fact 到 metadata["facts"]。

        注意：不呼叫 _save()。由外層 _apply_patch 統一持久化一次。
        """
        if "facts" not in concept.metadata:
            concept.metadata["facts"] = []

        concept.metadata["facts"].append(patch.new_value)
        self._apply_support_source(concept, patch)

        # 更新 confidence
        if patch.new_confidence is not None:
            concept.confidence = patch.new_confidence

        # 寫入 patch_history
        self._add_history_entry(concept, patch)

        concept.updated_at = datetime.now(timezone.utc)
        return True

    def _apply_modify(self, concept: Concept, patch: ConceptPatch) -> bool:
        """執行 modify — 更新 description + confidence。

        注意：不呼叫 _save()。由外層 _apply_patch 統一持久化一次。
        """
        new_description = (
            patch.new_value if isinstance(patch.new_value, str) else concept.description
        )

        # 先算新 embedding（description 變了）：失敗時整筆不套用，不留半套變更
        new_embedding = None
        if self._semantic._embed_fn is not None:
            new_vec = self._semantic._embed_fn(f"{concept.name}: {new_description}")
            new_embedding = self._semantic._normalize_vector(new_vec).tolist()
            # 先保存舊 embedding 供 rollback 用
            patch.rollback_payload = patch.rollback_payload or {}
            patch.rollback_payload["old_embedding"] = list(concept.embedding)

        concept.description = new_description
        if patch.new_confidence is not None:
            concept.confidence = patch.new_confidence
        if new_embedding is not None:
            concept.embedding = new_embedding
            self._semantic._update_embedding_index(concept, is_update=True)

        self._add_history_entry(concept, patch)

        concept.updated_at = datetime.now(timezone.utc)
        return True

    def _apply_deprecate(self, concept: Concept, patch: ConceptPatch) -> bool:
        """執行 deprecate — 標記狀態 + 降權。

        注意：不呼叫 _save()。由外層 _apply_patch 統一持久化一次。
        """
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
        return True

    def _apply_mark_conflict(self, concept: Concept, patch: ConceptPatch) -> bool:
        """執行 mark_conflict — 遞增 conflict_count 並設 has_contradiction。

        注意：不呼叫 _save()。由外層 _apply_patch 統一 record_mutation（version +1）後持久化。
        rollback 所需狀態在變更當下寫入 rollback_payload（存檔失敗時同樣可還原）。
        不新增 derivation：矛盾對手不是此概念的來源。
        """
        payload = patch.new_value if isinstance(patch.new_value, dict) else {}
        try:
            delta = int(payload.get("conflict_count_delta", 1))
        except (TypeError, ValueError):
            return False
        if delta <= 0:
            return False

        patch.old_value = concept.conflict_count
        patch.rollback_payload = {
            "conflict_count_delta": delta,
            "had_has_contradiction": "has_contradiction" in concept.metadata,
        }
        concept.conflict_count += delta
        concept.metadata["has_contradiction"] = True
        self._add_history_entry(concept, patch)
        concept.updated_at = datetime.now(timezone.utc)
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
        mutation+save 在 SemanticMemory._lock 內完成。
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
                redirected = (patch.rollback_payload or {}).get("redirected_to")
                if redirected:
                    # 同名 create 被導向主概念：還原主概念，不能移除（同名的）已合併次概念
                    self._rollback_redirect(patch, str(redirected))
                    return
                # create rollback → 移除概念（remove_concept 自帶 lock）
                name = (patch.rollback_payload or {}).get(
                    "created_concept_name", patch.concept_name
                )
                self._semantic.remove_concept(name)
                logger.info(
                    "[ConceptInhibitor] rollback create: 移除 %s", name,
                )
                return  # create rollback 不需要後續的 history/save 操作

            with self._semantic._lock:
                if patch.action == "expand":
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
                        concept.remove_derivations_by_patch(patch.patch_id)
                    self._restore_support_source(concept, patch)
                    # 還原 confidence
                    if patch.old_confidence is not None:
                        concept.confidence = patch.old_confidence
                    concept.record_mutation()  # rollback 本身也是一次變更，version 單調
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
                    concept.record_mutation()
                elif patch.action == "mark_conflict":
                    rb = patch.rollback_payload or {}
                    delta = int(rb.get("conflict_count_delta", 0) or 0)
                    if delta > 0:
                        # 以增量回退（而非還原絕對值），並行的其他標記不會被蓋掉
                        concept.conflict_count = max(0, concept.conflict_count - delta)
                        if not rb.get("had_has_contradiction") and concept.conflict_count <= 0:
                            concept.metadata.pop("has_contradiction", None)
                    concept.record_mutation()
                elif patch.action == "deprecate":
                    # 還原為 active
                    concept.status = "active"
                    concept.metadata.pop("deprecated_at", None)
                    concept.metadata.pop("deprecated_reason", None)
                    concept.metadata.pop("replaced_by", None)
                    concept.record_mutation()

                # 只移除屬於這個 patch 的 patch_history 條目（apply 可能是 no-op，沒寫過 history）
                history = concept.metadata.get("patch_history", [])
                if history and history[-1].get("patch_id") == patch.patch_id:
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

    def _rollback_redirect(self, patch: ConceptPatch, primary_id: str) -> None:
        """回滾「同名 create 導向主概念」：移除補進主概念的 fact／來源 episode／derivation。"""
        rb = patch.rollback_payload or {}
        with self._semantic._lock:
            primary = self._semantic.get(primary_id)
            if primary is None:
                logger.warning("[ConceptInhibitor] rollback 導向：找不到主概念 %s", primary_id)
                return
            fact = rb.get("added_fact")
            if fact:
                facts = primary.metadata.get("facts", [])
                if fact in facts:
                    facts.remove(fact)
            old_sources = rb.get("old_source_episode_ids")
            if old_sources is not None:
                primary.source_episode_ids = list(old_sources)
            primary.remove_derivations_by_patch(patch.patch_id)
            self._restore_support_source(primary, patch)
            if rb.get("old_confidence") is not None:
                primary.confidence = rb["old_confidence"]
            history = primary.metadata.get("patch_history", [])
            if history and history[-1].get("patch_id") == patch.patch_id:
                history.pop()
            primary.record_mutation()
            self._semantic._save()
        logger.info("[ConceptInhibitor] rollback 導向 create: 還原主概念 %s", primary_id)

    # ──────────────────────────────────────────────
    # Audit Log
    # ──────────────────────────────────────────────

    def _write_audit(self, tx: PatchTransaction) -> None:
        """寫入獨立 audit log（JSONL）。失敗只記 log。"""
        self._try_write_audit(tx)

    def _try_write_audit(self, tx: PatchTransaction) -> bool:
        """寫入獨立 audit log（JSONL），回傳是否成功（audit-before-mutate 用）。"""
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
            return True
        except Exception as e:
            logger.warning("[ConceptInhibitor] audit log 寫入失敗: %s", e)
            return False

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
