"""
HypothesisGateMixin - ConceptInhibitor 的 Hypothesis 寫入入口（M5-D）

假說的所有狀態轉移（提案、租約、審核結果、過期、清除、重開）與其圖譜副作用
（昇格 RELATES_TO、移除 LATENT_BRIDGE、撤回昇格邊）都只能經這裡：

    ConceptInhibitor.process_hypothesis_patch(op, payload)

規則層 approve（審核者本身就是 LLM，Gate 不再呼叫 LLM、不在 LLM 期間持鎖）。
流程：鎖內建 patch 並做結構驗證 -> audit-before-mutate（audit 失敗則什麼都不動）->
apply（圖譜邊 + 假說檔）-> 失敗則 rollback -> 終態 audit。
rollback 依 rollback_payload 完整還原（昇格邊、被移除的 bridge、假說紀錄）。

此 mixin 是 ConceptInhibitor 的一部分，是唯一持有 HypothesisWriter 的地方
（結構測試會檢查其他模組沒有）。

引擎只依結構欄位判斷，不從文字內容推測（不使用 keyword / regex）；salience 不是門檻。
"""

from __future__ import annotations

import copy
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from echo_memory.affect.concept_patch import ConceptPatch, PatchEvidence, PatchTransaction
from echo_memory.config import get_config
from echo_memory.schemas.evidence_review import CLAIM_KINDS, EvidenceReview
from echo_memory.schemas.hypothesis import (
    RETAINED_STATUSES,
    Hypothesis,
    HypothesisOrigin,
    evaluate_hypothesis_support,
    hypothesis_evidence_kind,
    make_pair_key,
    split_pair_key,
)
from echo_memory.storage.conflict_ledger import compute_pair_digest
from echo_memory.storage.graph import _json_safe
from echo_memory.storage.hypothesis import HypothesisStore, HypothesisWriter

logger = logging.getLogger("echo_memory.affect.hypothesis_gate")

HYPOTHESIS_OPS = ("propose", "claim", "release", "review", "expire", "purge", "reopen")
REVIEW_RESULTS = ("supported", "refuted", "insufficient", "llm_failed")
REOPEN_REASONS = ("digest_drift", "evidence_gone")
GATE_EXPIRE_REASONS = ("ttl", "endpoint_gone")
_MAX_BACKOFF_HOURS = 24.0 * 7


class _Reject(Exception):
    """規劃階段的拒絕原因（stale 開頭者代表前提已過期）。"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def backoff_delay(base_hours: float, count: int) -> timedelta:
    """指數退避：base * 2^(count-1)，上限一週。"""
    hours = min(_MAX_BACKOFF_HOURS, float(base_hours) * (2 ** max(0, int(count) - 1)))
    return timedelta(hours=hours)


class HypothesisGateMixin:
    """ConceptInhibitor 的 Hypothesis 子系統。依賴宿主的 _semantic／_lookup_units／_apply_patch 等。"""

    # ──────────────────────────────────────────────
    # 存儲 handle（writer 只存在於這個 mixin）
    # ──────────────────────────────────────────────

    def _namespace_root(self) -> Path:
        return Path(self._semantic.storage_path).parent  # type: ignore[attr-defined]

    def _hypothesis_store(self) -> HypothesisStore:
        store = getattr(self, "_hyp_store_obj", None)
        if store is None:
            store = HypothesisStore(self._namespace_root())
            self._hyp_store_obj = store
        return store

    def _hypothesis_writer(self) -> HypothesisWriter:
        writer = getattr(self, "_hyp_writer_obj", None)
        if writer is None:
            writer = HypothesisWriter(self._hypothesis_store())
            self._hyp_writer_obj = writer
        return writer

    @property
    def hypothesis_store(self) -> HypothesisStore:
        """唯讀視圖（給呼叫端查詢；沒有任何寫入方法）。"""
        return self._hypothesis_store()

    # ──────────────────────────────────────────────
    # 公開入口
    # ──────────────────────────────────────────────

    def process_hypothesis_patch(
        self,
        op: str,
        payload: dict[str, Any] | None = None,
        *,
        triggered_by: str = "dream",
    ) -> tuple[bool, PatchTransaction]:
        """以 hypothesis patch 改變假說狀態（含圖譜副作用）。規則層 approve，不呼叫 LLM。

        op / payload（結構驗證不通過一律拒絕，什麼都不動，仍留 audit）:
          - "propose": concept_a、concept_b、origin{bridge_id,cycle_id,confirm_count,similarity}、
            pair_digest。前提：兩端 active 未 merged、digest 相符、兩端間無 RELATES_TO／CONTRADICTS、
            bridge 為 active 且 confirm_count 達標、同 pair 無開放／已 supported／同 digest 被擋、
            開放總量未滿。
          - "claim": hypothesis_id、token（審核租約，過期回 proposed）、lease_seconds、manual。
          - "release": hypothesis_id、token：under_review 回 proposed（持有者主動釋放，或租約已過期）。
          - "review": hypothesis_id、token、result（supported／refuted／insufficient／llm_failed）、
            pair_digest、review、statement、claim_kind、coverage{a,b}。supported 時 Gate 以被引用
            unit 重算支持度與獨立佐證（使用者自述只是主觀事實），通過才昇格 RELATES_TO。
          - "expire": hypothesis_id、reason（ttl／endpoint_gone）。
          - "purge": hypothesis_ids（refuted／expired 且保留期限已過）。
          - "reopen": hypothesis_id、reason（digest_drift／evidence_gone）：supported 回 proposed 重審。

        Returns:
            (success, PatchTransaction)。success=False 時沒有任何假說或圖譜被改動。
        """
        payload = dict(payload) if isinstance(payload, dict) else {}
        hid_hint = str(payload.get("hypothesis_id") or "")
        tx = PatchTransaction(
            patches=[],
            user_input=f"[hypothesis:{op}] {hid_hint or payload.get('concept_a', '-')}",
            llm_response="",
            session_id="hypothesis",
            turn_id="",
            triggered_by=triggered_by,  # type: ignore[arg-type]
        )
        patch_id = f"patch_{uuid.uuid4().hex[:12]}"
        reject: str | None = None
        patch: ConceptPatch | None = None
        with self._semantic._lock:  # type: ignore[attr-defined]
            try:
                if op not in HYPOTHESIS_OPS:
                    raise _Reject(f"不支援的 hypothesis op: {op!r}")
                plan = self._plan_hypothesis(op, payload, patch_id)
            except _Reject as e:
                plan = {"new_value": {"op": op, "hypothesis_id": hid_hint}, "old_value": None,
                        "risk": "low", "name": hid_hint or op}
                reject = str(e)
            except Exception as e:  # noqa: BLE001 - 規劃失敗一律 fail-closed
                logger.warning("[HypothesisGate] 規劃 %s 失敗: %s", op, e, exc_info=True)
                plan = {"new_value": {"op": op, "hypothesis_id": hid_hint}, "old_value": None,
                        "risk": "low", "name": hid_hint or op}
                reject = f"plan_error: {e}"
            patch = ConceptPatch(
                transaction_id=tx.transaction_id,
                patch_id=patch_id,
                concept_id=None,
                concept_name=str(plan["name"]),
                action="hypothesis",
                target_path="hypotheses",
                old_value=plan["old_value"],
                new_value=plan["new_value"],
                patch_confidence=0.7,
                evidence=PatchEvidence(
                    evidence_kind="hypothesis_review",
                    evidence_strength=0.7,
                    verifier_status="valid",
                    verifier_reason=f"hypothesis:{op}",
                    retrieved_concept_ids=list(plan["new_value"].get("endpoints") or []),
                ),
                risk_level=plan["risk"],
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

        # audit-before-mutate：先寫 audit，失敗則假說與圖譜都不動
        if not self._try_write_audit(tx):  # type: ignore[attr-defined]
            patch.status = "rejected"
            patch.rejection_reason = "audit log 寫入失敗，阻止 mutation"
            tx.status = "failed"
            return False, tx

        if self._apply_patch(patch):  # type: ignore[attr-defined]
            patch.status = "applied"
            tx.status = "committed"
            self._write_audit(tx)  # type: ignore[attr-defined]
            return True, tx

        self._rollback_patch(patch)  # type: ignore[attr-defined]
        patch.status = "rejected"
        patch.rejection_reason = "hypothesis 套用失敗（前提已變動或存檔失敗）"
        tx.status = "failed"
        self._write_audit(tx)  # type: ignore[attr-defined]
        return False, tx

    def rollback_hypothesis_patch(self, tx: PatchTransaction) -> None:
        """人工回滾已套用的 hypothesis patch（經 audit）：還原昇格邊、被移除的 bridge、假說紀錄。"""
        for p in reversed(tx.applied_patches):
            if p.action == "hypothesis":
                self._rollback_patch(p)  # type: ignore[attr-defined]
                p.status = "rolled_back"
        tx.status = "rolled_back"
        self._write_audit(tx)  # type: ignore[attr-defined]

    # ──────────────────────────────────────────────
    # 規劃（鎖內；只讀，產生完整計畫供 audit）
    # ──────────────────────────────────────────────

    def _plan_hypothesis(self, op: str, payload: dict[str, Any], patch_id: str) -> dict[str, Any]:
        cfg = get_config()
        now = _now()
        store = self._hypothesis_store()
        if store.is_corrupt:
            raise _Reject("假說檔損毀，拒絕寫入")
        planner = getattr(self, f"_plan_hyp_{op}")
        return planner(payload, patch_id, cfg, now, store)

    # ── 共用查驗 ────────────────────────────────

    def _hyp_endpoint_concepts(self, pair_key: str) -> tuple[Any, Any]:
        """兩端皆 active 且未 merged 才回傳；否則拋 _Reject("endpoint_gone: ...")。"""
        ends = split_pair_key(pair_key)
        if ends is None:
            raise _Reject("pair_key 無效")
        concepts = []
        for cid in ends:
            c = self._semantic.get(cid)  # type: ignore[attr-defined]
            if c is None or c.status != "active" or c.metadata.get("merged_into"):
                raise _Reject(f"endpoint_gone: {cid}")
            concepts.append(c)
        return concepts[0], concepts[1]

    def _hyp_graph(self) -> Any:
        graph = getattr(self._semantic, "_graph", None)  # type: ignore[attr-defined]
        if graph is None:
            raise _Reject("no_graph: SemanticMemory 未 attach 圖譜")
        return graph

    @staticmethod
    def _pair_edges(graph: Any, a: str, b: str) -> dict[str, dict[str, Any]]:
        """a,b 之間現有邊（兩方向）：{"a>b": attrs, ...}（複本）。"""
        out: dict[str, dict[str, Any]] = {}
        with graph.lock:
            for src, dst in ((a, b), (b, a)):
                if graph.graph.has_edge(src, dst):
                    out[f"{src}>{dst}"] = dict(graph.graph[src][dst])
        return out

    def _hyp_record(self, store: HypothesisStore, payload: dict[str, Any]) -> Hypothesis:
        hid = str(payload.get("hypothesis_id") or "")
        rec = store.get(hid) if hid else None
        if rec is None:
            raise _Reject(f"hypothesis_missing: {hid!r}")
        return rec

    @staticmethod
    def _plan_result(
        op: str, rec: Hypothesis | None, *, put: list[Hypothesis] = (),  # type: ignore[assignment]
        remove: list[str] = (), edges: dict[str, Any] | None = None,
        old_records: dict[str, Any] | None = None, risk: str = "low", extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        ref = rec or (put[0] if put else None)
        new_value: dict[str, Any] = {
            "op": op,
            "hypothesis_id": ref.id if ref else "",
            "pair_key": ref.pair_key if ref else "",
            "endpoints": list(ref.endpoints or ()) if ref else [],
            "records_put": [h.to_dict() for h in put],
            "records_remove": list(remove),
            "expected_versions": {
                h.id: (old_records or {}).get(h.id, {}).get("version") for h in put
            },
            "edges": _json_safe(edges or {}),
        }
        if extra:
            new_value.update(extra)
        return {
            "new_value": new_value,
            "old_value": old_records or None,
            "risk": risk,
            "name": ref.pair_key if ref else op,
        }

    # ── propose ────────────────────────────────

    def _plan_hyp_propose(self, payload, patch_id, cfg, now, store) -> dict[str, Any]:
        if not cfg.hypothesis_enabled:
            raise _Reject("hypothesis 已停用")
        from echo_memory.schemas.sharing import is_scope_namespace_id

        if is_scope_namespace_id(self._namespace_root().name):
            raise _Reject("scope_namespace: 共享 namespace 不產生假說")
        a, b = str(payload.get("concept_a") or ""), str(payload.get("concept_b") or "")
        if not a or not b or a == b:
            raise _Reject("concept_a／concept_b 無效")
        pair_key = make_pair_key(a, b)
        ca, cb = self._hyp_endpoint_concepts(pair_key)
        digest = compute_pair_digest(ca, cb)
        if payload.get("pair_digest") != digest:
            raise _Reject("stale: pair_digest 與目前證據基礎不符")

        graph = self._hyp_graph()
        types = {e.get("edge_type") for e in self._pair_edges(graph, a, b).values()}
        if types & {"RELATES_TO", "CONTRADICTS"}:
            raise _Reject("pair_has_relation: 端點間已有 RELATES_TO／CONTRADICTS")
        bridge = next(
            (br for br in graph.iter_latent_bridges(("active",)) if br.bridge_id == pair_key), None,
        )
        if bridge is None:
            raise _Reject("bridge_not_active: 該對沒有 active 的 LATENT_BRIDGE")
        if bridge.confirm_count < int(cfg.hypothesis_min_confirm_count):
            raise _Reject(
                f"bridge_unconfirmed: confirm_count={bridge.confirm_count} < "
                f"{cfg.hypothesis_min_confirm_count}"
            )

        existing = store.get_by_pair(a, b)
        old_records: dict[str, Any] = {}
        remove: list[str] = []
        if existing is not None:
            if existing.is_open:
                raise _Reject("duplicate_open: 該對已有開放假說")
            if existing.status == "supported":
                raise _Reject("already_supported: 該對已有 supported 假說")
            if store.is_blocked(a, b, digest):
                raise _Reject("blocked: 同 pair_digest 已被否決或審核耗盡")
            old_records[existing.id] = existing.to_dict()
            remove.append(existing.id)
        if store.open_count() >= int(cfg.hypothesis_max_open):
            raise _Reject("open_cap: 開放假說總量已達上限")

        origin_raw = payload.get("origin") if isinstance(payload.get("origin"), dict) else {}
        origin = HypothesisOrigin.from_dict({
            "type": "latent_bridge",
            "bridge_id": pair_key,
            "cycle_id": origin_raw.get("cycle_id", bridge.last_confirmed_cycle_id),
            "confirm_count": bridge.confirm_count,
            "similarity": bridge.similarity,
        })
        rec = Hypothesis(
            pair_key=pair_key, origin=origin, pair_digest=digest, status="proposed",
            expires_at=now + timedelta(days=float(cfg.hypothesis_proposed_ttl_days)),
            created_at=now, updated_at=now,
        )
        return self._plan_result("propose", None, put=[rec], remove=remove, old_records=old_records)

    # ── claim ──────────────────────────────────

    def _plan_hyp_claim(self, payload, patch_id, cfg, now, store) -> dict[str, Any]:
        rec = self._hyp_record(store, payload)
        token = str(payload.get("token") or "")
        if not token:
            raise _Reject("token 必填")
        if rec.expires_at is not None and rec.expires_at <= now and rec.is_open:
            raise _Reject("ttl_elapsed: 假說已逾期，待 expire")
        if rec.status == "proposed":
            if not payload.get("manual") and rec.next_review_at is not None and rec.next_review_at > now:
                raise _Reject("backoff: 尚在退避期")
        elif rec.status == "under_review":
            if not rec.lease_expired(now):
                raise _Reject("leased: 審核租約未過期")
        else:
            raise _Reject(f"not_claimable: status={rec.status}")
        self._hyp_endpoint_concepts(rec.pair_key)
        lease_raw = payload.get("lease_seconds")
        lease = float(cfg.hypothesis_lease_seconds if lease_raw is None else lease_raw)
        new = Hypothesis.from_dict(rec.to_dict())
        new.status = "under_review"
        new.claim_token = token
        new.claimed_until = now + timedelta(seconds=max(0.0, lease))
        return self._plan_result(
            "claim", new, put=[new], old_records={rec.id: rec.to_dict()},
        )

    # ── release ────────────────────────────────

    def _plan_hyp_release(self, payload, patch_id, cfg, now, store) -> dict[str, Any]:
        rec = self._hyp_record(store, payload)
        if rec.status != "under_review":
            raise _Reject(f"not_under_review: status={rec.status}")
        token = str(payload.get("token") or "")
        if not rec.lease_expired(now) and (not token or token != rec.claim_token):
            raise _Reject("leased: 租約未過期且 token 不符")
        new = Hypothesis.from_dict(rec.to_dict())
        new.status = "proposed"
        new.claim_token, new.claimed_until = "", None
        return self._plan_result("release", new, put=[new], old_records={rec.id: rec.to_dict()})

    # ── review ─────────────────────────────────

    def _plan_hyp_review(self, payload, patch_id, cfg, now, store) -> dict[str, Any]:
        rec = self._hyp_record(store, payload)
        result = payload.get("result")
        if result not in REVIEW_RESULTS:
            raise _Reject(f"result 無效: {result!r}")
        if rec.status != "under_review":
            raise _Reject(f"stale: 假說不在審核中 (status={rec.status})")
        if not rec.claim_token or rec.claim_token != str(payload.get("token") or ""):
            raise _Reject("stale: 審核租約屬於他人")
        if rec.lease_expired(now):
            raise _Reject("stale: 審核租約已過期")

        old_records = {rec.id: rec.to_dict()}
        new = Hypothesis.from_dict(rec.to_dict())
        new.claim_token, new.claimed_until = "", None

        if result == "llm_failed":
            new.status = "proposed"
            new.fail_count = rec.fail_count + 1
            new.next_review_at = now + backoff_delay(cfg.hypothesis_backoff_base_hours, new.fail_count)
            return self._plan_result("review", new, put=[new], old_records=old_records,
                                     extra={"result": result})

        # 其餘結果都綁當下 pair_digest；端點失效 / digest 漂移一律 stale
        try:
            ca, cb = self._hyp_endpoint_concepts(rec.pair_key)
        except _Reject as e:
            raise _Reject(f"stale: {e}") from e
        digest = compute_pair_digest(ca, cb)
        if payload.get("pair_digest") != digest:
            raise _Reject("stale: pair_digest 在審核期間已變動")

        review = EvidenceReview.from_dict(payload.get("review"))
        review.basis_digest = digest
        review.reviewer = "llm"
        review.patch_id = patch_id
        review.reviewed_at = now
        claim_kind = payload.get("claim_kind")
        if claim_kind not in CLAIM_KINDS:
            raise _Reject(f"claim_kind 無效: {claim_kind!r}")
        new.claim_kind, new.pair_digest = claim_kind, digest

        if result == "insufficient":
            review.status = "insufficient"
            new.review, new.statement = review, None
            new.attempts = rec.attempts + 1
            if new.attempts >= int(cfg.hypothesis_max_attempts):
                new.status = "expired"
                new.outcome.expire_reason = "insufficient_exhausted"
                new.expires_at = now + timedelta(days=float(cfg.hypothesis_refuted_retention_days))
            else:
                new.status = "proposed"
                new.next_review_at = now + backoff_delay(
                    cfg.hypothesis_backoff_base_hours, new.attempts,
                )
            edges = self._plan_unlink_if_promoted(rec)
            if edges:
                new.outcome.promoted_edge = None  # 昇格邊隨本次撤回一併清除
            return self._plan_result("review", new, put=[new], old_records=old_records,
                                     edges=edges, risk="medium" if edges else "low",
                                     extra={"result": result})

        # refuted／supported：被引用證據必須存在且具證據資格
        cited = [str(x) for x in (review.cited_evidence_ids or [])]
        cited = list(dict.fromkeys(cited))
        if not cited:
            raise _Reject("no_cited_evidence: 缺引用證據不得 supported／refuted")
        units = self._lookup_units(cited)  # type: ignore[attr-defined]
        missing = [i for i in cited if i not in units]
        if missing:
            raise _Reject(f"cited_unit_missing: {missing[0]}")
        if any(hypothesis_evidence_kind(units[i]) is None for i in cited):
            raise _Reject("cited_unit_ineligible: 引用了不具證據資格的 unit（agent／dream／未確認來源）")
        review.cited_evidence_ids = cited

        if result == "refuted":
            review.status = "contradicted"
            new.review, new.statement = review, None
            new.status = "refuted"
            new.outcome.expire_reason = ""
            new.expires_at = now + timedelta(days=float(cfg.hypothesis_refuted_retention_days))
            graph = self._hyp_graph()
            edges = self._plan_unlink_if_promoted(rec)
            edges["remove_bridge"] = bool(
                {e.get("edge_type") for e in self._pair_edges(graph, *rec.endpoints).values()}
                & {"LATENT_BRIDGE"}
            )
            new.outcome.promoted_edge = None
            return self._plan_result("review", new, put=[new], old_records=old_records,
                                     edges=edges, risk="medium", extra={"result": result})

        # supported：Gate 以被引用 unit 重算支持度與獨立佐證
        statement = str(payload.get("statement") or "").strip()
        if not statement or len(statement) > int(cfg.hypothesis_statement_max_chars):
            raise _Reject("statement 無效: supported 必須有長度合法的關聯描述")
        coverage = payload.get("coverage") if isinstance(payload.get("coverage"), dict) else {}
        cov_a = [str(x) for x in coverage.get("a", []) if str(x) in cited]
        cov_b = [str(x) for x in coverage.get("b", []) if str(x) in cited]
        if not cov_a or not cov_b:
            raise _Reject("coverage: supported 的引用證據必須同時覆蓋兩端概念")
        support = evaluate_hypothesis_support([units[i] for i in cited], claim_kind, cfg)
        if support.status != "supported":
            raise _Reject(f"not_supported: {','.join(support.notes) or 'insufficient'}")
        review.status = "supported"
        review.support_strength = support.strength
        review.corroborated_by = list(support.independent_ids)
        review.claim_kind = claim_kind
        new.review, new.statement = review, statement
        new.status = "supported"
        new.expires_at = None
        new.next_review_at = None

        graph = self._hyp_graph()
        a, b = rec.endpoints  # type: ignore[misc]
        existing_edges = self._pair_edges(graph, a, b)
        types = {e.get("edge_type") for e in existing_edges.values()}
        if "CONTRADICTS" in types:
            raise _Reject("pair_contradicts: 端點間有 CONTRADICTS，不昇格")
        ours = [
            e for e in existing_edges.values()
            if e.get("edge_type") == "RELATES_TO" and e.get("hypothesis_id") == rec.id
        ]
        edges: dict[str, Any] = {"a": a, "b": b, "cited_unit_ids": cited}
        if ours:
            edges["refresh_promoted"] = True  # 重審仍 supported：沿用昇格邊，更新引用
            new.outcome.promoted_edge = {"a": a, "b": b, "hypothesis_id": rec.id}
        elif "RELATES_TO" in types:
            new.outcome.promoted_edge = None  # 既有（非本假說）的 RELATES_TO：不動邊
        else:
            edges["promote"] = True
            edges["weight"] = float(cfg.hypothesis_promoted_weight)
            edges["remove_bridge"] = "LATENT_BRIDGE" in types
            new.outcome.promoted_edge = {"a": a, "b": b, "hypothesis_id": rec.id}
            new.outcome.promote_patch_id = patch_id
        return self._plan_result("review", new, put=[new], old_records=old_records,
                                 edges=edges, risk="medium", extra={"result": result})

    def _plan_unlink_if_promoted(self, rec: Hypothesis) -> dict[str, Any]:
        """重審結果不再是 supported：撤回先前昇格的邊。"""
        if rec.outcome.promoted_edge:
            return {"unlink_promoted": True, "a": rec.endpoints[0], "b": rec.endpoints[1]}  # type: ignore[index]
        return {}

    # ── expire ─────────────────────────────────

    def _plan_hyp_expire(self, payload, patch_id, cfg, now, store) -> dict[str, Any]:
        rec = self._hyp_record(store, payload)
        reason = payload.get("reason")
        if reason not in GATE_EXPIRE_REASONS:
            raise _Reject(f"expire reason 無效: {reason!r}")
        if rec.status in RETAINED_STATUSES:
            raise _Reject(f"already_closed: status={rec.status}")
        if reason == "ttl":
            if not rec.is_open or rec.expires_at is None or rec.expires_at > now:
                raise _Reject("ttl_not_elapsed")
        else:  # endpoint_gone：Gate 自行確認端點確實失效
            try:
                self._hyp_endpoint_concepts(rec.pair_key)
            except _Reject:
                pass
            else:
                raise _Reject("endpoint_alive: 端點仍 active")
        old_records = {rec.id: rec.to_dict()}
        new = Hypothesis.from_dict(rec.to_dict())
        new.status = "expired"
        new.statement = None
        new.claim_token, new.claimed_until = "", None
        new.outcome.expire_reason = reason
        new.expires_at = now + timedelta(days=float(cfg.hypothesis_refuted_retention_days))
        return self._plan_result("expire", new, put=[new], old_records=old_records,
                                 extra={"reason": reason})

    # ── purge ──────────────────────────────────

    def _plan_hyp_purge(self, payload, patch_id, cfg, now, store) -> dict[str, Any]:
        raw = payload.get("hypothesis_ids")
        ids = [str(x) for x in raw] if isinstance(raw, list) else []
        if not ids:
            raise _Reject("hypothesis_ids 必填")
        old_records: dict[str, Any] = {}
        for hid in dict.fromkeys(ids):
            rec = store.get(hid)
            if rec is None:
                raise _Reject(f"hypothesis_missing: {hid}")
            if rec.status not in RETAINED_STATUSES or rec.expires_at is None or rec.expires_at > now:
                raise _Reject(f"retention_not_elapsed: {hid}")
            old_records[hid] = rec.to_dict()
        first = Hypothesis.from_dict(next(iter(old_records.values())))
        return self._plan_result("purge", first, remove=list(old_records), old_records=old_records)

    # ── reopen ─────────────────────────────────

    def _plan_hyp_reopen(self, payload, patch_id, cfg, now, store) -> dict[str, Any]:
        rec = self._hyp_record(store, payload)
        reason = payload.get("reason")
        if reason not in REOPEN_REASONS:
            raise _Reject(f"reopen reason 無效: {reason!r}")
        if rec.status != "supported":
            raise _Reject(f"not_supported: status={rec.status}")
        if reason == "digest_drift":
            ca, cb = self._hyp_endpoint_concepts(rec.pair_key)
            if compute_pair_digest(ca, cb) == rec.pair_digest:
                raise _Reject("no_drift: pair_digest 未變動")
        else:
            cited = list(rec.review.cited_evidence_ids)
            units = self._lookup_units(cited) if cited else {}  # type: ignore[attr-defined]
            if cited and all(
                i in units and hypothesis_evidence_kind(units[i]) is not None for i in cited
            ):
                raise _Reject("evidence_intact: 引用證據仍有效")
        old_records = {rec.id: rec.to_dict()}
        new = Hypothesis.from_dict(rec.to_dict())
        new.status = "proposed"
        new.statement = None  # proposed 不存斷言文字
        new.attempts = 0
        new.next_review_at = None
        new.claim_token, new.claimed_until = "", None
        new.expires_at = now + timedelta(days=float(cfg.hypothesis_proposed_ttl_days))
        return self._plan_result("reopen", new, put=[new], old_records=old_records,
                                 extra={"reason": reason})

    # ──────────────────────────────────────────────
    # 套用 / 回滾（由 ConceptInhibitor._apply_patch／_rollback_patch_impl 呼叫）
    # ──────────────────────────────────────────────

    def _apply_hypothesis(self, patch: ConceptPatch) -> bool:
        """執行 hypothesis patch：先圖譜邊、再假說檔；任何一步失敗就地回滾已完成的步驟。"""
        nv = patch.new_value if isinstance(patch.new_value, dict) else {}
        rb = patch.rollback_payload = patch.rollback_payload or {}
        edges = nv.get("edges") if isinstance(nv.get("edges"), dict) else {}
        try:
            if edges:
                if not self._apply_hypothesis_edges(patch, edges, nv, rb):
                    self._rollback_hypothesis(patch)
                    return False
            writer = self._hypothesis_writer()
            priors: dict[str, Any] = rb.setdefault("records_prior", {})
            for hid in nv.get("records_remove") or []:
                prior = writer.store.get(hid)
                priors[hid] = prior.to_dict() if prior is not None else None
                writer.remove([hid])
            for rec_dict in nv.get("records_put") or []:
                rec = Hypothesis.from_dict(rec_dict)
                if rec is None:
                    raise ValueError("records_put 含無法解析的紀錄")
                prior = writer.store.get(rec.id)
                priors.setdefault(rec.id, prior.to_dict() if prior is not None else None)
                expected = (nv.get("expected_versions") or {}).get(rec.id)
                writer.put(rec, expected_version=expected)
            return True
        except Exception as e:  # noqa: BLE001
            logger.error("[HypothesisGate] apply 失敗 (%s): %s", patch.patch_id, e)
            try:
                self._rollback_hypothesis(patch)
            except Exception as e2:  # noqa: BLE001
                logger.error("[HypothesisGate] apply 失敗後的回滾也失敗: %s", e2)
            return False

    def _apply_hypothesis_edges(
        self, patch: ConceptPatch, edges: dict[str, Any], nv: dict[str, Any], rb: dict[str, Any],
    ) -> bool:
        graph = getattr(self._semantic, "_graph", None)  # type: ignore[attr-defined]
        if graph is None:
            return False
        ends = split_pair_key(str(nv.get("pair_key") or ""))
        if ends is None:
            return False
        a, b = ends
        hid = str(nv.get("hypothesis_id") or "")
        changed = False
        with graph.lock:
            if not (graph.graph.has_node(a) and graph.graph.has_node(b)):
                return False
            pair = [(a, b), (b, a)]
            if edges.get("unlink_promoted"):
                removed = rb.setdefault("promoted_removed", {})
                for src, dst in pair:
                    data = graph.graph.get_edge_data(src, dst)
                    if (
                        data and data.get("edge_type") == "RELATES_TO"
                        and data.get("origin") == "hypothesis" and data.get("hypothesis_id") == hid
                    ):
                        removed[f"{src}>{dst}"] = dict(data)
                        graph.graph.remove_edge(src, dst)
                        changed = True
            if edges.get("remove_bridge"):
                removed = rb.setdefault("bridge_removed", {})
                for src, dst in pair:
                    data = graph.graph.get_edge_data(src, dst)
                    if data and data.get("edge_type") == "LATENT_BRIDGE":
                        removed[f"{src}>{dst}"] = dict(data)
                        graph.graph.remove_edge(src, dst)
                        changed = True
            if edges.get("promote"):
                for src, dst in pair:
                    data = graph.graph.get_edge_data(src, dst)
                    if data and data.get("edge_type") == "CONTRADICTS":
                        return False  # 前提在鎖內再驗一次
                    if data is not None:
                        return False  # 預期已無邊（bridge 已於上一步移除）
                added = rb.setdefault("relates_added", [])
                for src, dst in pair:
                    graph.graph.add_edge(
                        src, dst,
                        edge_type="RELATES_TO",
                        weight=max(0.0, min(1.0, float(edges.get("weight", 0.5)))),
                        frequency=1,
                        avg_salience=0.5,
                        origin="hypothesis",
                        hypothesis_id=hid,
                        cited_unit_ids=list(edges.get("cited_unit_ids") or []),
                        patch_id=patch.patch_id,
                        created_at=time.time(),
                    )
                    added.append(f"{src}>{dst}")
                changed = True
            if edges.get("refresh_promoted"):
                refreshed = rb.setdefault("promoted_refreshed", {})
                for src, dst in pair:
                    data = graph.graph.get_edge_data(src, dst)
                    if (
                        data and data.get("edge_type") == "RELATES_TO"
                        and data.get("hypothesis_id") == hid
                    ):
                        refreshed[f"{src}>{dst}"] = {
                            "cited_unit_ids": copy.deepcopy(data.get("cited_unit_ids")),
                            "patch_id": data.get("patch_id"),
                        }
                        data["cited_unit_ids"] = list(edges.get("cited_unit_ids") or [])
                        data["patch_id"] = patch.patch_id
                        changed = True
        if changed:
            self._save_graph(graph)  # type: ignore[attr-defined]
        return True

    def _rollback_hypothesis(self, patch: ConceptPatch) -> None:
        """依 rollback_payload 還原：假說紀錄、昇格邊、被移除的 bridge／昇格邊（原屬性）。"""
        rb = patch.rollback_payload or {}
        nv = patch.new_value if isinstance(patch.new_value, dict) else {}
        hid = str(nv.get("hypothesis_id") or "")
        writer = self._hypothesis_writer()
        # 紀錄：反向還原（propose 取代舊紀錄時，先移除新的再放回舊的，避免 pair_key 衝突）
        for rid, prior in reversed(list((rb.get("records_prior") or {}).items())):
            try:
                writer.restore(rid, prior)
            except Exception as e:  # noqa: BLE001
                logger.error("[HypothesisGate] 回滾假說紀錄失敗 (%s): %s", rid, e)

        graph = getattr(self._semantic, "_graph", None)  # type: ignore[attr-defined]
        if graph is None:
            return
        changed = False
        with graph.lock:
            for key in rb.get("relates_added") or []:
                src, dst = key.split(">", 1)
                data = graph.graph.get_edge_data(src, dst)
                if (
                    data and data.get("edge_type") == "RELATES_TO"
                    and data.get("origin") == "hypothesis" and data.get("hypothesis_id") == hid
                ):
                    graph.graph.remove_edge(src, dst)
                    changed = True
            for section in ("bridge_removed", "promoted_removed"):
                for key, attrs in (rb.get(section) or {}).items():
                    src, dst = key.split(">", 1)
                    if (
                        graph.graph.has_node(src) and graph.graph.has_node(dst)
                        and not graph.graph.has_edge(src, dst)
                    ):
                        graph.graph.add_edge(src, dst, **attrs)
                        changed = True
            for key, prior in (rb.get("promoted_refreshed") or {}).items():
                src, dst = key.split(">", 1)
                data = graph.graph.get_edge_data(src, dst)
                if data is not None:
                    data["cited_unit_ids"] = prior.get("cited_unit_ids")
                    data["patch_id"] = prior.get("patch_id")
                    changed = True
        if changed:
            self._save_graph(graph)  # type: ignore[attr-defined]
