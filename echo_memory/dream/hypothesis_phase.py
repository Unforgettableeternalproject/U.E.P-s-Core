"""
HypothesisPhase - Dream 的假說產生與維護階段（M5-D）

觸發方案 A：純結構自動提案，LLM 只在審核時介入（見 affect/hypothesis_reviewer.py）。

產生（propose）：
  - 透過 graph.iter_latent_bridges() 取 BridgeInfo（不碰 networkx 邊屬性）。
  - 前置：bridge state=active；兩端 active 且未 merged；端點間無 RELATES_TO／CONTRADICTS；
    同 pair 無開放 Hypothesis；同 pair_digest 未被 refuted（或審核耗盡）。
  - 條件：confirm_count >= min_confirm_count（跨 Dream 週期，M5-B 已實作）＋兩端各至少 1 筆
    source_type=user 的證據（provenance 追溯，不是 embedding 相似度）。
  - 排序：圖結構（兩端無共同鄰居加分）> 共同來源 episode 數 > 確認次數 > 相似度。
    salience 不參與（不是門檻，也不用來排序）。
  - 每週期新提案上限、開放總量上限由 config 決定。不使用 keyword／regex。
  - 共享 namespace（scope-g*）不產生假說。

維護（maintain，不需 LLM）：租約過期釋放、TTL 過期、端點失效 expire、supported 的
digest 漂移／證據消失回 proposed 重審、refuted／expired 保留期滿清除。

所有寫入都經 ConceptInhibitor.process_hypothesis_patch；本模組只持有唯讀 HypothesisStore。
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from echo_memory.affect.hypothesis_reviewer import endpoint_units, verify_support
from echo_memory.schemas.hypothesis import KIND_USER, hypothesis_evidence_kind
from echo_memory.storage.conflict_ledger import compute_pair_digest

logger = logging.getLogger("echo_memory.dream.hypothesis_phase")


@dataclass
class HypothesisReport:
    """假說 phase 的統計。"""

    proposed: int = 0
    rejected: int = 0
    expired: int = 0
    reopened: int = 0
    released: int = 0
    purged: int = 0
    candidates: int = 0
    skipped_reason: str | None = None
    proposed_ids: list[str] = field(default_factory=list)

    def summary(self) -> str:
        if self.skipped_reason:
            return f"假說: 略過（{self.skipped_reason}）"
        return (
            f"假說: 新提案 {self.proposed}（候選 {self.candidates}, 被 Gate 拒絕 {self.rejected}）, "
            f"過期 {self.expired}, 重開 {self.reopened}, 釋放 {self.released}, 清除 {self.purged}"
        )


class HypothesisPhase:
    """Dream 假說 phase。

    Args:
        semantic: SemanticMemory。
        graph: MemoryGraph。
        store: HypothesisStore（唯讀）。
        gate: ConceptInhibitor（寫入唯一入口）。
        provenance: ProvenanceResolver；None 時不產生（無法確認使用者證據，fail-closed）。
        lock_factory: 取得 storage_lock 的工廠；None 時不加鎖。
        patch_coordinator: 取 cycle_id 用（可選）。
        cfg: Config；None 取全域。
        enabled: False 時整個 phase 略過（例如共享 namespace）。
    """

    def __init__(
        self,
        semantic: Any,
        graph: Any,
        store: Any,
        gate: Any,
        provenance: Any = None,
        lock_factory: Callable[[], Any] | None = None,
        patch_coordinator: Any = None,
        cfg: Any = None,
        enabled: bool = True,
    ):
        if cfg is None:
            from echo_memory.config import get_config
            cfg = get_config()
        self._cfg = cfg
        self._semantic = semantic
        self._graph = graph
        self._store = store
        self._gate = gate
        self._provenance = provenance
        self._lock = lock_factory if lock_factory is not None else contextlib.nullcontext
        self._coordinator = patch_coordinator
        self._enabled = enabled

    def run(self) -> HypothesisReport:
        report = HypothesisReport()
        if not self._enabled or not self._cfg.hypothesis_enabled:
            report.skipped_reason = "disabled"
            return report
        if self._is_scope_namespace():
            report.skipped_reason = "shared_namespace"
            return report
        if not hasattr(self._gate, "process_hypothesis_patch"):
            report.skipped_reason = "no_gate"
            return report
        self.maintain(report)
        self.propose(report)
        return report

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 維護
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _submit(self, op: str, payload: dict[str, Any]) -> tuple[bool, Any]:
        with self._lock(), self._semantic._lock:
            return self._gate.process_hypothesis_patch(op, payload, triggered_by="dream")

    def _endpoints_active(self, pair_key: str) -> bool:
        ends = pair_key.split("|")
        for cid in ends:
            c = self._semantic.get(cid)
            if c is None or c.status != "active" or c.metadata.get("merged_into"):
                return False
        return len(ends) == 2

    def maintain(self, report: HypothesisReport | None = None) -> HypothesisReport:
        report = report or HypothesisReport()
        now = datetime.now(timezone.utc)
        purge_ids: list[str] = []
        for h in self._store.list():
            try:
                if h.status == "under_review" and h.lease_expired(now):
                    ok, _ = self._submit("release", {"hypothesis_id": h.id, "token": h.claim_token})
                    report.released += int(ok)
                    h = self._store.get(h.id) or h
                if h.is_open:
                    if not self._endpoints_active(h.pair_key):
                        ok, _ = self._submit("expire", {"hypothesis_id": h.id, "reason": "endpoint_gone"})
                        report.expired += int(ok)
                    elif h.expires_at is not None and h.expires_at <= now:
                        ok, _ = self._submit("expire", {"hypothesis_id": h.id, "reason": "ttl"})
                        report.expired += int(ok)
                elif h.status == "supported":
                    reason = self._verify(h)
                    if reason == "endpoint_gone":
                        ok, _ = self._submit("expire", {"hypothesis_id": h.id, "reason": reason})
                        report.expired += int(ok)
                    elif reason is not None:
                        ok, _ = self._submit("reopen", {"hypothesis_id": h.id, "reason": reason})
                        report.reopened += int(ok)
                elif h.expires_at is not None and h.expires_at <= now:
                    purge_ids.append(h.id)
            except Exception as e:  # noqa: BLE001 - 單筆失敗不拖垮整批
                logger.warning("[HypothesisPhase] 維護 %s 失敗: %s", h.id[:12], e)
        if purge_ids:
            ok, _ = self._submit("purge", {"hypothesis_ids": purge_ids})
            report.purged += len(purge_ids) if ok else 0
        return report

    def _verify(self, h: Any) -> str | None:
        """supported 假說是否仍成立；沒有 provenance 無法重解析時 fail-closed 視為證據消失。"""
        if self._provenance is None:
            return "evidence_gone"
        with self._lock(), self._semantic._lock:
            return verify_support(h, self._semantic, self._provenance)

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 產生
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def propose(self, report: HypothesisReport | None = None) -> HypothesisReport:
        report = report or HypothesisReport()
        if self._provenance is None:
            report.skipped_reason = "no_provenance"
            return report
        room = int(self._cfg.hypothesis_max_open) - self._store.open_count()
        budget = min(int(self._cfg.hypothesis_max_new_per_cycle), room)
        if budget <= 0:
            return report

        ranked = self._rank_candidates()
        report.candidates = len(ranked)
        for cand in ranked:
            if report.proposed >= budget:
                break
            ok, tx = self._submit("propose", {
                "concept_a": cand["a"],
                "concept_b": cand["b"],
                "pair_digest": cand["digest"],
                "origin": {"cycle_id": self._cycle_id()},
            })
            if ok:
                report.proposed += 1
                rec = self._store.get_by_pair(cand["a"], cand["b"])
                if rec is not None:
                    report.proposed_ids.append(rec.id)
            else:
                report.rejected += 1
                patch = tx.patches[0] if tx.patches else None
                logger.info(
                    "[HypothesisPhase] Gate 拒絕提案 %s: %s", cand["pair_key"],
                    patch.rejection_reason if patch else "",
                )
        return report

    def _cycle_id(self) -> str:
        return self._coordinator.cycle_id if self._coordinator is not None else ""

    def _rank_candidates(self) -> list[dict[str, Any]]:
        """通過前置與條件的候選 bridge，依圖結構排序（高→低）。"""
        min_confirm = int(self._cfg.hypothesis_min_confirm_count)
        ranked: list[tuple[tuple, dict[str, Any]]] = []
        for br in self._graph.iter_latent_bridges(("active",)):
            if br.confirm_count < min_confirm:
                continue
            a, b = br.concept_a, br.concept_b
            if self._store.has_open_for_pair(a, b):
                continue
            existing = self._store.get_by_pair(a, b)
            if existing is not None and existing.status == "supported":
                continue
            with self._lock(), self._semantic._lock:
                ca, cb = self._semantic.get(a), self._semantic.get(b)
                if any(
                    c is None or c.status != "active" or c.metadata.get("merged_into")
                    for c in (ca, cb)
                ):
                    continue
                digest = compute_pair_digest(ca, cb)
                if self._store.is_blocked(a, b, digest):
                    continue
                if self._graph.edge_types_between(a, b) & {"RELATES_TO", "CONTRADICTS"}:
                    continue
                if not (self._has_user_evidence(a) and self._has_user_evidence(b)):
                    continue
                shared_sources = len(set(ca.source_episode_ids) & set(cb.source_episode_ids))
                no_common = int(
                    not (self._graph.concept_neighbor_ids(a) & self._graph.concept_neighbor_ids(b))
                )
            score = (no_common, shared_sources, br.confirm_count, br.similarity)
            ranked.append((score, {"a": a, "b": b, "pair_key": br.bridge_id, "digest": digest}))
        ranked.sort(key=lambda item: item[0], reverse=True)
        return [c for _, c in ranked]

    def _has_user_evidence(self, concept_id: str) -> bool:
        """端點是否至少有一筆 source_type=user（可作證的使用者陳述）證據。"""
        return any(
            hypothesis_evidence_kind(u) == KIND_USER
            for u in endpoint_units(self._provenance, concept_id)
        )

    def _is_scope_namespace(self) -> bool:
        from echo_memory.schemas.sharing import is_scope_namespace_id

        return is_scope_namespace_id(Path(self._semantic.storage_path).parent.name)
