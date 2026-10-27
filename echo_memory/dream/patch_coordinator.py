"""
DreamPatchCoordinator — Dream Engine 的 Patch 協調層

所有 Dream Engine 對 SemanticMemory 和 MemoryGraph 的變更都透過此協調器：

1. SemanticMemory mutation（蒸餾產生的 Concept）→ 走 ConceptInhibitor patch pipeline
   - 但 Dream 來源直接 approve + audit only（不走 LLM corroboration）
2. Graph mutation（pruning/replay 的邊操作）→ 不走 ConceptPatch（不是 concept），
   但寫入獨立的 dream_audit_log.jsonl

設計原則：
  - Dream 來源的 concept 可信度足夠，直接 approve
  - Graph edge 操作不需要 Inhibitor 審核，但需要 audit trail
  - Batch 操作（如 update_edge_weights）用摘要 audit，不逐條記錄
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from echo_memory.affect.concept_patch import PatchTransaction, SupersedeProposal

logger = logging.getLogger("echo_memory.dream.patch_coordinator")


class DreamPatchCoordinator:
    """
    Dream Engine 內部的 Patch 協調層。

    協調 Dream 各階段的寫入操作：
      - 蒸餾的 Concept 寫入走 ConceptInhibitor
      - Graph 操作走獨立 audit log
    """

    def __init__(
        self,
        inhibitor=None,  # ConceptInhibitor（避免循環 import）
        audit_log_path: str | Path | None = None,
        semantic=None,  # SemanticMemory；inhibitor 缺省時用來建立降級 Gate
    ):
        """
        Args:
            inhibitor: ConceptInhibitor 實例。None 且提供 semantic 時，建立無 LLM 的
                降級 Inhibitor（仍經 Patch Gate、留 patch trail）。
            audit_log_path: Dream audit log 路徑。None 時使用預設位置。
            semantic: SemanticMemory 實例，僅在 inhibitor 缺省時使用。
        """
        if inhibitor is None and semantic is not None:
            from echo_memory.affect.concept_inhibitor import ConceptInhibitor
            inhibitor = ConceptInhibitor(semantic=semantic, llm_fn=None)
        self._inhibitor = inhibitor
        self._audit_log_path = Path(audit_log_path) if audit_log_path else None
        self._cycle_id: str = ""
        # ConflictLedger（可選）：held 的 supersede 經通用重審套用後，在這裡補標 resolved
        self.conflict_ledger: Any = None

    @property
    def has_inhibitor(self) -> bool:
        return self._inhibitor is not None

    @property
    def inhibitor(self):
        """底層 ConceptInhibitor（審核器等需要直接走 Gate 的模組使用）。"""
        return self._inhibitor

    def ensure_gate(self, semantic) -> None:
        """沒有 inhibitor 時補上無 LLM 的降級 Gate（仍經 Patch Gate、留 patch trail）。"""
        if self._inhibitor is None and semantic is not None:
            from echo_memory.affect.concept_inhibitor import ConceptInhibitor
            self._inhibitor = ConceptInhibitor(semantic=semantic, llm_fn=None)

    def begin_cycle(self, triggered_by: str) -> None:
        """開始新的 Dream 週期，設定 cycle_id。"""
        self._cycle_id = (
            f"dream_{triggered_by}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
        )

    # ──────────────────────────────────────────────
    # SemanticMemory mutation（走 Inhibitor pipeline）
    # ──────────────────────────────────────────────

    def submit_distillation_concept(
        self,
        name: str,
        description: str,
        concept_type: str,
        source_episode_ids: list[str],
        embedding: list[float] | None = None,
        subject: str | None = None,
        support_source: str | None = None,
        similar_hint: Any = None,
    ) -> tuple[str | None, PatchTransaction | None]:
        """
        提交蒸餾產生的 Concept 到 ConceptInhibitor。

        Dream distillation 來源直接 approve + audit only。

        Args:
            name: 概念名稱
            description: 概念描述
            concept_type: 概念類型
            source_episode_ids: 來源 Episode ID 列表
            embedding: 概念嵌入向量
            subject: 概念主體（user / agent / world），隨 create patch 寫入 patch trail
            support_source: 支撐來源（user / legacy_unknown / agent_only），隨 create／expand
                patch 寫入 patch trail（只升不降、可 rollback）
            similar_hint: precheck_similar() 在鎖外算好的語意相近提示（呼叫端持鎖時必傳）

        Returns:
            (concept_id, PatchTransaction) — inhibitor 為 None 時回傳 (None, None)
        """
        if self._inhibitor is None:
            return None, None

        # 用第一個 source episode 作為主 evidence
        episode_id = source_episode_ids[0] if source_episode_ids else self._cycle_id

        concept_id, tx = self._inhibitor.process_extractor_patch(
            noun=name,
            desc=description,
            episode_id=episode_id,
            evidence_kind="distillation",
            embedding=embedding,
            triggered_by="dream",
            concept_type=concept_type,
            source_episode_ids=source_episode_ids,
            subject=subject,
            support_source=support_source,
            similar_hint=similar_hint,
        )

        return concept_id, tx

    def precheck_similar(self, name: str, description: str) -> Any:
        """蒸餾寫入前的語意相近提示（embedding + 可選 LLM），須在持有 storage_lock 之前呼叫。

        inhibitor 缺省或評估失敗回傳 None（寫入端會退回自行評估或不帶提示）。
        """
        evaluate = getattr(self._inhibitor, "evaluate_similar", None)
        if evaluate is None:
            return None
        try:
            return evaluate(name, description)
        except Exception as e:
            logger.warning("[DreamPatchCoordinator] 語意相近預檢失敗（不影響寫入）: %s", e)
            return None

    def submit_conflict_mark(
        self,
        concept_a_id: str,
        concept_b_id: str,
        strength: float = 0.0,
        reason: str = "",
    ) -> tuple[bool, PatchTransaction | None]:
        """
        提交機器偵測的矛盾標記（mark_conflict patch）到 ConceptInhibitor。

        走 Gate 的 audit-before-mutate、可 rollback；不需 LLM 佐證。

        Returns:
            (success, PatchTransaction) — inhibitor 缺省回傳 (False, None)；
            success=False 時呼叫方應阻止後續的圖邊建立。
        """
        if self._inhibitor is None or not hasattr(self._inhibitor, "process_conflict_mark"):
            return False, None
        return self._inhibitor.process_conflict_mark(
            concept_a_id, concept_b_id,
            strength=strength, reason=reason, triggered_by="dream",
        )

    def rollback_conflict_mark(self, tx: PatchTransaction | None) -> None:
        """回滾先前 submit_conflict_mark 已套用的標記（圖邊建立失敗時使用）。"""
        if tx is None or self._inhibitor is None:
            return
        self._inhibitor.rollback_conflict_mark(tx)

    # ──────────────────────────────────────────────
    # 取代式矛盾（supersede = deprecate + replaced_by，經 Gate；薄包裝）
    # ──────────────────────────────────────────────

    def prepare_supersede(
        self,
        older_id: str,
        newer_id: str,
        reason: str = "",
        basis: dict[str, Any] | None = None,
        graph: Any = None,
    ) -> tuple[str, SupersedeProposal | None]:
        """Gate 判斷「newer 取代 older」（含 LLM 佐證，不得在 storage_lock 內呼叫）。

        graph：偵測 CONTRADICTS 邊用的 MemoryGraph（缺省用 semantic 已 attach 的圖）。

        Returns:
            (state, proposal)：approved｜held｜rejected｜unavailable（無 Gate）
        """
        if self._inhibitor is None or not hasattr(self._inhibitor, "prepare_supersede_patch"):
            return "unavailable", None
        return self._inhibitor.prepare_supersede_patch(
            older_id, newer_id, reason, basis, graph=graph,
        )

    def commit_supersede(
        self,
        proposal: SupersedeProposal,
        clear_conflict_marks: bool = False,
        graph: Any = None,
        verify_fn: Any = None,
    ) -> tuple[bool, PatchTransaction | None]:
        """套用已通過 Gate 的 supersede 提案（呼叫端應持 storage_lock）。"""
        if self._inhibitor is None:
            return False, None
        return self._inhibitor.process_supersede_patch(
            proposal, clear_conflict_marks=clear_conflict_marks,
            graph=graph, verify_fn=verify_fn,
        )

    def submit_supersede(
        self,
        older_id: str,
        newer_id: str,
        reason: str = "",
        basis: dict[str, Any] | None = None,
        clear_conflict_marks: bool = False,
    ) -> tuple[bool, PatchTransaction | None]:
        """prepare + commit 的便利入口（測試或無鎖情境用；Dream 內請分開呼叫以保持 LLM 在鎖外）。"""
        state, proposal = self.prepare_supersede(older_id, newer_id, reason, basis)
        if state != "approved" or proposal is None:
            return False, None
        return self.commit_supersede(proposal, clear_conflict_marks=clear_conflict_marks)

    def rollback_supersede(self, tx: PatchTransaction | None, graph: Any = None) -> list[str]:
        """回滾 commit_supersede（含補回互相標記與 CONTRADICTS 邊）；回傳被補回標記的概念 id。"""
        if tx is None or self._inhibitor is None:
            return []
        return self._inhibitor.rollback_supersede_patch(tx, graph=graph)

    # ──────────────────────────────────────────────
    # 跨使用者分享（M10）：share action 薄包裝
    # ──────────────────────────────────────────────

    def submit_share(
        self,
        concept_id: str,
        op: str,
        scope_id: str | None,
        payload: dict[str, Any] | None = None,
    ) -> tuple[bool, PatchTransaction | None]:
        """提交 share patch（project／retract／restrict）到 ConceptInhibitor。

        走 Gate 的 audit-before-mutate、可 rollback；規則層 approve，不呼叫 LLM。
        fail-closed：沒有 Gate、Gate 不支援 share、或任何例外 -> (False, None)，視為不分享。
        呼叫端應持擁有者鎖；LLM 與共享層寫入都不得在這把鎖內。
        """
        if self._inhibitor is None or not hasattr(self._inhibitor, "process_share_patch"):
            return False, None
        try:
            return self._inhibitor.process_share_patch(
                concept_id, op, scope_id, payload or {}, triggered_by="dream",
            )
        except Exception as e:
            logger.warning("[DreamPatchCoordinator] share patch(%s) 失敗（視為不分享）: %s", op, e)
            return False, None

    def submit_restrict_units(
        self,
        unit_ids: list[str],
        reason_code: str = "",
    ) -> list[tuple[bool, PatchTransaction | None]]:
        """使用者／LLM 判定的隱私收緊：寫 ledger，並把受影響場域改為 private＋pinned。

        各步驟為獨立 share patch（各自 audit）。共享層撤回由呼叫端依 projection_id 觸發。
        """
        if self._inhibitor is None or not hasattr(self._inhibitor, "restrict_units"):
            return []
        try:
            return self._inhibitor.restrict_units(
                list(unit_ids), reason_code=reason_code, triggered_by="dream",
            )
        except Exception as e:
            logger.warning("[DreamPatchCoordinator] restrict_units 失敗: %s", e)
            return []

    def review_held(self, guard=None) -> int:
        """
        Dream 週期開始時重審先前 held 的 concept patch（觸發點之一）。

        LLM corroboration 在 guard 外，apply 在 guard 內（由 inhibitor 處理）。

        Returns:
            本次重審產生的 audit transaction 數
        """
        if self._inhibitor is None or not hasattr(self._inhibitor, "review_held_patches"):
            return 0
        try:
            txs = self._inhibitor.review_held_patches(guard=guard)
            for tx in txs:
                self._ledger_resolve_applied_supersede(tx)
            return len(txs)
        except Exception as e:
            logger.warning("[DreamPatchCoordinator] held 重審失敗（不影響 Dream）: %s", e)
            return 0

    def _ledger_resolve_applied_supersede(self, tx: PatchTransaction) -> None:
        """held 的 supersede 由通用重審套用後，把配對在 ledger 標為 temporal_evolution。"""
        ledger = self.conflict_ledger
        if ledger is None or self._inhibitor is None:
            return
        from echo_memory.storage.conflict_ledger import compute_pair_digest

        semantic = self._inhibitor.semantic
        for p in tx.applied_patches:
            nv = p.new_value if isinstance(p.new_value, dict) else {}
            if p.action != "deprecate" or not isinstance(nv.get("supersession"), dict):
                continue
            older = semantic.get(p.concept_id or "")
            newer = semantic.get(str(nv.get("replaced_by") or ""))
            if older is None or newer is None:
                continue
            ledger.record_resolved(
                older.id, newer.id, "temporal_evolution",
                reason=p.evidence.verifier_reason,
                pair_digest=compute_pair_digest(older, newer),
                relation="supersedes",
                concept_a_name=older.name, concept_b_name=newer.name,
                patch_id=tx.transaction_id,
            )

    # ──────────────────────────────────────────────
    # Graph mutation audit（不走 Inhibitor）
    # ──────────────────────────────────────────────

    def audit_graph_edge_removal(
        self,
        edges: list[tuple[str, str]],
        reason: str = "pruning",
    ) -> bool:
        """記錄 graph 邊移除的 audit。回傳 False 時呼叫方應阻止 mutation。"""
        return self._write_dream_audit({
            "operation": "remove_edges",
            "count": len(edges),
            "reason": reason,
            "sample_edges": [
                {"from": e[0][:12], "to": e[1][:12]} for e in edges[:5]
            ],
        })

    def audit_graph_node_archive(
        self,
        node_ids: list[str],
        reason: str = "stale_node_archive",
    ) -> bool:
        """記錄 graph 節點歸檔的 audit。回傳 False 時呼叫方應阻止 mutation。"""
        return self._write_dream_audit({
            "operation": "archive_nodes",
            "count": len(node_ids),
            "reason": reason,
            "sample_nodes": [nid[:12] for nid in node_ids[:5]],
        })

    def audit_graph_latent_bridge(
        self,
        bridges_created: int,
        sample_bridges: list[dict[str, str]] | None = None,
    ) -> bool:
        """記錄 LATENT_BRIDGE 建立的 audit。回傳 False 時呼叫方應阻止 mutation。"""
        return self._write_dream_audit({
            "operation": "create_latent_bridges",
            "count": bridges_created,
            "sample_bridges": sample_bridges or [],
        })

    def audit_graph_relates_to(
        self,
        concept_id: str,
        edges: list[dict[str, Any]],
    ) -> bool:
        """
        記錄蒸餾依來源重疊建立 RELATES_TO 邊的 audit。

        回傳 False 時呼叫方應阻止 mutation。
        edges 每項至少含 target（概念 ID）與 jaccard / weight。
        """
        return self._write_dream_audit({
            "operation": "add_relates_to_edges",
            "concept_id": concept_id[:12],
            "count": len(edges),
            "sample_edges": [
                {
                    "to": str(e.get("target", ""))[:12],
                    "jaccard": round(float(e.get("jaccard", 0.0)), 4),
                    "weight": round(float(e.get("weight", 0.0)), 4),
                }
                for e in edges[:5]
            ],
        })

    def audit_latent_bridge_maintenance(
        self,
        operation: str,
        count: int,
    ) -> bool:
        """記錄 LATENT_BRIDGE 維護（重新確認等）的 audit。回傳 False 時呼叫方應阻止 mutation。"""
        return self._write_dream_audit({
            "operation": operation,
            "count": count,
        })

    def audit_batch_weight_update(
        self,
        edges_updated: int,
    ) -> bool:
        """記錄 batch edge weight 更新的 audit（摘要級）。回傳 False 時呼叫方應阻止 mutation。"""
        return self._write_dream_audit({
            "operation": "batch_weight_update",
            "count": edges_updated,
        })

    def audit_replay_edge_change(
        self,
        operation: str,
        from_id: str,
        to_id: str,
        edge_type: str = "",
        weight_delta: float = 0.0,
    ) -> bool:
        """記錄 replay 的單條邊變更 audit。回傳 False 時呼叫方應阻止 mutation。"""
        return self._write_dream_audit({
            "operation": f"replay_{operation}",
            "from": from_id[:12],
            "to": to_id[:12],
            "edge_type": edge_type,
            "weight_delta": round(weight_delta, 4),
        })

    def audit_replay_batch(
        self,
        edges_reinforced: int,
        edges_created: int,
        salience_updates: int,
    ) -> bool:
        """記錄 replay 的 batch 摘要 audit。回傳 False 時呼叫方應阻止 mutation。"""
        return self._write_dream_audit({
            "operation": "replay_batch_summary",
            "edges_reinforced": edges_reinforced,
            "edges_created": edges_created,
            "salience_updates": salience_updates,
        })

    def audit_graph_derived_from(
        self,
        concept_id: str,
        episode_ids: list[str],
    ) -> bool:
        """記錄 DERIVED_FROM 邊建立的 audit。"""
        return self._write_dream_audit({
            "operation": "add_derived_from_edges",
            "concept_id": concept_id[:12],
            "episode_count": len(episode_ids),
        })

    def audit_contradiction_detection(
        self,
        concept_id: str,
        contradicts_count: int,
        relation: str | None = None,
        peer_id: str | None = None,
        reason: str = "",
    ) -> bool:
        """記錄矛盾偵測結果的 audit。

        relation（supersedes／different_scope 等）與 peer_id 給定時一併留痕，
        便於追查「為什麼這一對沒有建矛盾邊」。
        """
        entry: dict[str, Any] = {
            "operation": "contradiction_detection",
            "concept_id": concept_id[:12],
            "contradicts_created": contradicts_count,
        }
        if relation is not None:
            entry["relation"] = relation
        if peer_id is not None:
            entry["peer_id"] = peer_id[:12]
        if reason:
            entry["reason"] = reason[:200]
        return self._write_dream_audit(entry)

    def audit_procedure_extraction(
        self,
        procedures_created: int,
        procedures_updated: int,
    ) -> bool:
        """記錄程序提取結果的 audit。"""
        return self._write_dream_audit({
            "operation": "procedure_extraction",
            "procedures_created": procedures_created,
            "procedures_updated": procedures_updated,
        })

    # ──────────────────────────────────────────────
    # Audit Log 寫入
    # ──────────────────────────────────────────────

    def _get_audit_path(self) -> Path:
        """取得 dream audit log 路徑。"""
        if self._audit_log_path:
            return self._audit_log_path

        # 嘗試從 inhibitor 取 semantic 的 storage_path
        if self._inhibitor is not None and hasattr(self._inhibitor, '_semantic'):
            return self._inhibitor._semantic.storage_path / "dream_audit_log.jsonl"

        # Fallback
        return Path("dream_audit_log.jsonl")

    def _write_dream_audit(self, entry: dict[str, Any]) -> bool:
        """
        寫入 dream audit log（JSONL）。

        Returns:
            True 寫入成功，False 失敗。
            失敗時呼叫方應阻止對應的 mutation（audit-before-mutate 契約）。
        """
        entry["timestamp"] = datetime.now(timezone.utc).isoformat()
        entry["cycle_id"] = self._cycle_id

        log_path = self._get_audit_path()
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            return True
        except Exception as e:
            logger.error(
                "[DreamPatchCoordinator] audit log 寫入失敗，阻止對應 mutation: %s", e,
            )
            return False
