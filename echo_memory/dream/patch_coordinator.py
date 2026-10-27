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

from echo_memory.affect.concept_patch import PatchTransaction

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

    @property
    def has_inhibitor(self) -> bool:
        return self._inhibitor is not None

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
            return len(self._inhibitor.review_held_patches(guard=guard))
        except Exception as e:
            logger.warning("[DreamPatchCoordinator] held 重審失敗（不影響 Dream）: %s", e)
            return 0

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
    ) -> bool:
        """記錄矛盾偵測結果的 audit。"""
        return self._write_dream_audit({
            "operation": "contradiction_detection",
            "concept_id": concept_id[:12],
            "contradicts_created": contradicts_count,
        })

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
