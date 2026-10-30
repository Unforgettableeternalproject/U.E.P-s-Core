"""
SharingPhase - Dream 的分享 phase 與共享層對帳（M10 包 B）

流程（對應設計 §3.3／§3.4／§5，鎖序先擁有者後 scope、不巢狀、LLM 全在鎖外）：

  reconcile()（Dream 開頭，不需 LLM）
    (a) 共享層端：來源概念不存在／projection_id 不符／level 非 scope_shared／digest 已變 -> 撤回；
        已撤回但本體仍在 -> 補刪；重送待辦級聯。
    (b) 擁有者端：pinned 但仍帶 projection_id、projection_id 指向已不存在的投影 -> 先撤回、再 retract。
    (c) 失效的 ``sp:`` 佐證參考 -> 移除並重算審核狀態（B 撤回／退出／forget 後 A 的佐證消失）。

  run()（審核之後）
    對每個 (概念, 場域)：
      1. 鎖內快照（EvidenceReviewer.judge_sharing 內部）-> 2. 鎖外 LLM（輸入只含 U_S）->
      3. 結構驗證 -> 4. 鎖外算摘要 embedding -> 5. 持 scope 鎖寫投影 ->
      6. 持擁有者鎖經 Gate（share/project）寫回 projection_id。第 6 步被拒則撤回剛寫的投影。
    privacy_restrict_unit_ids 非空 -> 經 Gate restrict（ledger＋pinned），並立刻撤回受影響投影。
    之後對 intention 概念做交叉佐證。

fail-closed：沒有 LLM、ledger、可見度函式、共享層、Gate、owner uid -> 完全不投影（reconcile 仍可只撤回）。
架構禁令：敏感度、隱私意圖、佐證全由 LLM 判斷；本模組只做結構驗證，不使用 keyword／regex，
salience 不是分享門檻。
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from echo_memory.schemas.evidence_review import (
    SP_PREFIX,
    compute_basis_digest,
)
from echo_memory.schemas.sharing import LEVEL_SCOPE_SHARED, SharingState
from echo_memory.storage.atomic import atomic_write_json

logger = logging.getLogger("echo_memory.dream.sharing_phase")

JUDGED_FILENAME = "share_judged.json"
HELD_FILENAME = "share_held.json"


@dataclass
class SharingReport:
    """Dream 分享 phase 的批次統計。"""

    judged: int = 0
    projected: int = 0
    retracted: int = 0
    restricted: int = 0
    held: int = 0
    skipped: int = 0
    corroborated: int = 0
    revoked: int = 0
    completed_deletes: int = 0
    corroborations_invalidated: int = 0
    unhealthy_scopes: list[str] = field(default_factory=list)
    skip_reason: str = ""

    def summary(self) -> str:
        if self.skip_reason and not (self.revoked or self.corroborations_invalidated):
            return f"分享: 略過（{self.skip_reason}）"
        return (
            f"分享: 判定 {self.judged}, 投影 {self.projected}, 撤回 {self.revoked + self.retracted}, "
            f"收緊 {self.restricted}, 暫緩 {self.held}, 佐證 {self.corroborated}, "
            f"佐證失效 {self.corroborations_invalidated}"
        )


class SharingPhase:
    """Dream 的分享 phase。

    Args:
        owner_uid: 本 namespace 擁有者的 uid（投影的 owner_uid；缺少則不投影）。
        semantic: 擁有者的 SemanticMemory。
        reviewer: EvidenceReviewer（需帶 privacy_ledger，才能做分享判定）。
        share_layer: ScopeShareLayer。
        patch_coordinator: DreamPatchCoordinator（submit_share／submit_restrict_units）。
        lock_factory: 擁有者 storage_lock 工廠（回傳 context manager）。
        embed_fn: 摘要 -> 向量（缺省取 semantic 的 embed_fn）。
        cfg: Config；None 取全域。
    """

    def __init__(
        self,
        *,
        owner_uid: str | None,
        semantic: Any,
        reviewer: Any,
        share_layer: Any,
        patch_coordinator: Any,
        lock_factory: Callable[[], Any],
        embed_fn: Callable[[str], list[float]] | None = None,
        cfg: Any = None,
        judged_path: str | Path | None = None,
    ):
        if cfg is None:
            from echo_memory.config import get_config
            cfg = get_config()
        self._cfg = cfg
        self._owner = str(owner_uid).strip() if owner_uid is not None else ""
        self._semantic = semantic
        self._reviewer = reviewer
        self._layer = share_layer
        self._coordinator = patch_coordinator
        self._lock = lock_factory
        self._embed_fn = embed_fn if embed_fn is not None else getattr(semantic, "_embed_fn", None)
        self._judged_path = (
            Path(judged_path) if judged_path is not None
            else Path(semantic.storage_path) / JUDGED_FILENAME
        )
        self._held_path = Path(semantic.storage_path) / HELD_FILENAME
        self._max_held = int(getattr(cfg, "concept_held_max_retries", 3))

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 前置條件
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def ready(self) -> str:
        """分享（投影）前置條件；齊備回傳空字串，否則回傳缺少什麼（fail-closed）。"""
        if not self._owner:
            return "no_owner_uid"
        if self._layer is None:
            return "no_share_layer"
        if not getattr(self._cfg, "sharing_enabled", True):
            return "sharing_disabled"
        if not getattr(self._coordinator, "has_inhibitor", False):
            return "no_gate"
        if not hasattr(self._coordinator.inhibitor, "process_share_patch"):
            return "gate_without_share"
        if not self._reviewer.sharing_ready():
            return "no_llm_or_ledger"
        if self._embed_fn is None:
            return "no_embedding"
        return ""

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 對帳（Dream 開頭）
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def reconcile(self) -> SharingReport:
        """共享層／擁有者端對帳與佐證重驗。不需要 LLM；任何錯誤都只記 log，不拖垮 Dream。"""
        rep = SharingReport()
        if self._layer is None or not self._owner:
            rep.skip_reason = "no_share_layer" if self._layer is None else "no_owner_uid"
            return rep
        try:
            lay = self._layer.reconcile_owner(self._owner, self._owner_lookup)
            rep.revoked += len(lay.revoked)
            rep.completed_deletes += lay.completed_deletes
            rep.unhealthy_scopes = list(lay.unhealthy_scopes)
        except Exception as e:  # noqa: BLE001
            logger.warning("[SharingPhase] 共享層對帳失敗: %s", e)
        try:
            self._reconcile_owner_side(rep)
        except Exception as e:  # noqa: BLE001
            logger.warning("[SharingPhase] 擁有者端對帳失敗: %s", e)
        try:
            self._revalidate_corroborations(rep)
        except Exception as e:  # noqa: BLE001
            logger.warning("[SharingPhase] 佐證重驗失敗: %s", e)
        return rep

    def _owner_lookup(self, scope_id: str, source_concept_id: str) -> dict[str, Any] | None:
        """共享層對帳用：擁有者端對應概念的現況（鎖內短讀）。"""
        with self._lock(), self._semantic._lock:
            concept = self._semantic.get(source_concept_id)
            if concept is None:
                return {"exists": False}
            entry = concept.sharing_state().get(scope_id)
            return {
                "exists": True,
                "projection_id": entry.projection_id if entry is not None else "",
                "level": concept.effective_sharing_level(scope_id),
                "digest": compute_basis_digest(concept),
            }

    def _snapshot_states(self) -> list[tuple[str, SharingState, bool]]:
        """(concept_id, sharing_state, active)；鎖內短讀。"""
        out: list[tuple[str, SharingState, bool]] = []
        with self._lock(), self._semantic._lock:
            for c in self._semantic.list_concepts(limit=None):
                state = c.sharing_state()
                if state.by_scope:
                    out.append((c.id, state, c.status == "active"))
        return out

    def _reconcile_owner_side(self, rep: SharingReport) -> None:
        """(b) pinned 仍帶 projection_id／projection_id 指向已不存在的投影 -> 先撤回、再 retract。"""
        for cid, state, _active in self._snapshot_states():
            for scope_id, entry in state.by_scope.items():
                pid = entry.projection_id
                if not pid:
                    continue
                if entry.level != LEVEL_SCOPE_SHARED and entry.pinned:
                    self._revoke_then_retract(cid, scope_id, pid, "pinned_restricted", rep)
                    continue
                if not self._layer.is_healthy(scope_id):
                    continue  # 索引異常：不動擁有者端狀態，等人工復原
                if not self._layer.is_live(scope_id, pid):
                    self._revoke_then_retract(cid, scope_id, pid, "projection_missing", rep)
                    continue
                # forget／遮蔽／ledger 收緊：引用的來源 unit 不再可分享 -> 撤回，等重判
                if entry.level == LEVEL_SCOPE_SHARED and entry.cited_unit_ids:
                    if self._reviewer.cited_units_shareable(entry.cited_unit_ids, scope_id) is False:
                        self._revoke_then_retract(cid, scope_id, pid, "cited_unit_gone", rep)

    def _revalidate_corroborations(self, rep: SharingReport) -> None:
        with self._lock(), self._semantic._lock:
            targets = [
                c.id for c in self._semantic.list_concepts(limit=None)
                if any(
                    isinstance(r, str) and r.startswith(SP_PREFIX)
                    for r in ((c.evidence_review or {}).get("corroborated_by") or [])
                )
            ]
        for cid in targets:
            outcome = self._reviewer.revalidate_corroboration(cid, self._layer.validate_sp_ref)
            if outcome.outcome == "reviewed":
                rep.corroborations_invalidated += 1

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 分享 phase
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def run(self, limit: int | None = None) -> SharingReport:
        """判定並投影可分享概念；之後做交叉佐證。條件不齊備時完全不投影。"""
        rep = SharingReport()
        missing = self.ready()
        if missing:
            rep.skip_reason = missing
            return rep
        batch = limit if limit is not None else int(getattr(self._cfg, "sharing_batch", 8))
        if batch <= 0:
            rep.skip_reason = "batch_zero"
            return rep

        spent = 0
        for cid in self._candidate_ids():
            if spent >= batch or rep.held >= batch * 3:
                break
            try:
                spent += self._process_concept(cid, rep)
            except Exception as e:  # noqa: BLE001 - 單筆失敗不拖垮整批（fail-closed：不投影）
                logger.warning("[SharingPhase] 概念 %s 分享處理失敗: %s", cid[:12], e)
                rep.skipped += 1
        try:
            self._corroborate(rep, batch)
        except Exception as e:  # noqa: BLE001
            logger.warning("[SharingPhase] 交叉佐證失敗: %s", e)
        return rep

    def _candidate_ids(self) -> list[str]:
        """active、未合併、且 digest 與上次判定不同（或從未判定）的概念。"""
        judged = self._load_judged()
        held = self._load_held()
        with self._lock(), self._semantic._lock:
            out = []
            for c in self._semantic.list_concepts(status="active", limit=None):
                if c.metadata.get("merged_into"):
                    continue
                digest = compute_basis_digest(c)
                rec = judged.get(c.id)
                if rec is not None and rec.get("digest") == digest:
                    continue
                h = held.get(c.id)
                if h is not None and h.get("digest") == digest and h.get("abandoned"):
                    continue  # held 超過上限：內容不變就不再送審
                out.append(c.id)
        return out

    def _process_concept(self, cid: str, rep: SharingReport) -> int:
        """處理單一概念的所有場域；回傳花掉的批次名額（LLM 判定次數）。"""
        scopes = self._reviewer.candidate_scopes(cid)
        with self._lock(), self._semantic._lock:
            concept = self._semantic.get(cid)
            if concept is None:
                return 0
            state = concept.sharing_state()
            digest = compute_basis_digest(concept)
        # 曾分享、現在沒有可分享來源的場域 -> 先撤回（fail-closed）
        for scope_id, entry in state.by_scope.items():
            if entry.projection_id and scope_id not in scopes:
                self._revoke_then_retract(cid, scope_id, entry.projection_id, "no_shareable_units", rep)

        spent = 0
        all_final = True
        for scope_id in scopes:
            entry = state.get(scope_id)
            if entry is not None and entry.pinned:
                continue  # 使用者收緊：Dream 不得放寬
            if self._layer.is_owner_blocked(scope_id, self._owner):
                all_final = False
                continue
            j = self._reviewer.judge_sharing(cid, scope_id, owner_uid=self._owner)  # LLM 在鎖外
            if j.outcome == "held" and j.reason == "summary_too_long":
                j = self._reviewer.judge_sharing(cid, scope_id, owner_uid=self._owner)  # 摘要過長：重試一次
            if j.outcome == "held":
                rep.held += 1  # held 不佔批次名額，但計次，超過上限放棄（內容改變才重來）
                self._note_held(cid, digest)
                all_final = False
                continue
            if j.outcome != "judged":
                if j.reason == "contradicted" and entry is not None and entry.projection_id:
                    self._revoke_then_retract(cid, scope_id, entry.projection_id, "contradicted", rep)
                else:
                    all_final = False
                rep.skipped += 1
                continue
            spent += 1
            rep.judged += 1
            if not self._apply_judgement(cid, scope_id, entry, j, rep):
                all_final = False
        if all_final:
            self._record_judged(cid, digest, scopes)
            self._clear_held(cid)
        return spent

    def _apply_judgement(
        self, cid: str, scope_id: str, entry: Any, j: Any, rep: SharingReport,
    ) -> bool:
        """套用一次分享判定；回傳 False 代表結果不是終局（下次 Dream 重判）。"""
        existing_pid = entry.projection_id if entry is not None else ""
        if j.privacy_restrict_unit_ids:
            restricted = self._restrict_units(j.privacy_restrict_unit_ids, rep)
            if existing_pid:
                self._revoke_then_retract(cid, scope_id, existing_pid, "privacy_intent", rep)
            return restricted  # 收緊失敗不是終局：下次 Dream 重判（本次一律不投影）
        if not j.shareable:
            if existing_pid:
                self._revoke_then_retract(cid, scope_id, existing_pid, j.reason or "not_shareable", rep)
            return True
        return self._project(cid, scope_id, j, rep)

    def _project(self, cid: str, scope_id: str, j: Any, rep: SharingReport) -> bool:
        # 步驟 1：確認 digest 沒變（鎖內短讀；LLM 期間概念可能被改）
        with self._lock(), self._semantic._lock:
            concept = self._semantic.get(cid)
            if concept is None or compute_basis_digest(concept) != j.digest:
                rep.skipped += 1
                return False
        # 步驟 2：摘要 embedding（鎖外網路呼叫）
        try:
            vec = self._embed_fn(j.shareable_summary)  # type: ignore[misc]
        except Exception as e:  # noqa: BLE001
            logger.warning("[SharingPhase] 摘要 embedding 失敗: %s", e)
            rep.skipped += 1
            return False
        pid = f"proj_{uuid.uuid4().hex[:12]}"
        # 步驟 3：持 scope 鎖寫投影（只存摘要與 provenance）。about_members 非空才帶 subject_uids
        extra = {"subject_uids": list(j.about_members)} if getattr(j, "about_members", None) else {}
        res = self._layer.project(
            scope_id,
            owner_uid=self._owner,
            source_concept_id=cid,
            source_digest=j.digest,
            summary=j.shareable_summary,
            claim_kind=j.claim_kind,
            support_strength=j.support_strength,
            review_status=j.review_status,
            sensitivity=j.sensitivity,
            cited_unit_count=len(j.cited_unit_ids),
            embedding=vec,
            projection_id=pid,
            **extra,
        )
        if not res.ok:
            rep.skipped += 1
            return False
        # 步驟 4：持擁有者鎖經 Gate 寫回 projection_id（被拒則撤回剛寫的投影）
        payload = {
            "cited_unit_ids": list(j.cited_unit_ids),
            "basis_digest": j.digest,
            "sensitivity": j.sensitivity,
            "summary_digest": hashlib.sha256(j.shareable_summary.encode("utf-8")).hexdigest(),
            "projection_id": pid,
            "supports_summary_from_cited_only": True,
            "decided_by": "llm",
            "reason_code": "dream_project",
        }
        ok = False
        try:
            with self._lock(), self._semantic._lock:
                ok, _tx = self._coordinator.submit_share(cid, "project", scope_id, payload)
        except Exception as e:  # noqa: BLE001
            logger.warning("[SharingPhase] Gate project 失敗: %s", e)
        if not ok:
            self._layer.revoke(
                scope_id, [pid], reason_code="gate_rejected",
                owner_uid=self._owner, source_concept_id=cid,
            )
            rep.skipped += 1
            return False
        rep.projected += 1
        return True

    def _restrict_units(self, unit_ids: list[str], rep: SharingReport) -> bool:
        """使用者表達不想被轉述 -> Gate restrict（ledger＋pinned），並立刻撤回所有受影響投影。"""
        with self._lock(), self._semantic._lock:
            results = self._coordinator.submit_restrict_units(
                unit_ids, reason_code="llm_privacy_intent",
            )
        succeeded = bool(results) and all(ok for ok, _tx in results)
        if succeeded:
            rep.restricted += 1
        for cid, state, _active in self._snapshot_states():
            for scope_id, entry in state.by_scope.items():
                if entry.projection_id and entry.level != LEVEL_SCOPE_SHARED and entry.pinned:
                    self._revoke_then_retract(cid, scope_id, entry.projection_id, "privacy_intent", rep)
        return succeeded

    def _revoke_then_retract(
        self, cid: str, scope_id: str, pid: str, reason: str, rep: SharingReport,
    ) -> None:
        """撤回順序固定：先共享層（即時消失），再擁有者端 retract（清 projection_id）。"""
        res = self._layer.revoke(
            scope_id, [pid], reason_code=reason, owner_uid=self._owner, source_concept_id=cid,
        )
        rep.revoked += len(res.revoked_ids)
        try:
            with self._lock(), self._semantic._lock:
                ok, _tx = self._coordinator.submit_share(
                    cid, "retract", scope_id, {"reason_code": reason, "decided_by": "llm"},
                )
            if ok:
                rep.retracted += 1
        except Exception as e:  # noqa: BLE001
            logger.warning("[SharingPhase] Gate retract 失敗: %s", e)
        self._forget_judged(cid)

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 交叉佐證（§5）
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _corroborate(self, rep: SharingReport, batch: int) -> None:
        """intention 概念（審核有效且 insufficient、在該場域有投影）-> 找其他成員的投影佐證。"""
        top_k = int(getattr(self._cfg, "sharing_corroboration_top_k", 5))
        min_sim = float(getattr(self._cfg, "sharing_corroboration_min_similarity", 0.5))
        with self._lock(), self._semantic._lock:
            targets: list[tuple[str, str, str]] = []
            for c in self._semantic.list_concepts(status="active", limit=None):
                review = c.evidence_review or {}
                if review.get("claim_kind") != "intention":
                    continue
                if c.effective_review_status() != "insufficient":
                    continue
                for scope_id, entry in c.sharing_state().by_scope.items():
                    if entry.level == LEVEL_SCOPE_SHARED and entry.projection_id:
                        targets.append((c.id, scope_id, entry.projection_id))
        done = 0
        for cid, scope_id, pid in targets:
            if done >= batch:
                break
            view = self._layer.get_projection(scope_id, pid)
            if view is None:
                continue
            try:
                vec = self._embed_fn(view.summary)  # type: ignore[misc]
            except Exception as e:  # noqa: BLE001
                logger.warning("[SharingPhase] 佐證查詢 embedding 失敗: %s", e)
                continue
            cands = self._layer.search_projections(
                scope_id, vec, exclude_owner=self._owner, top_k=top_k, min_similarity=min_sim,
            )
            # 循環防護：曾被帶入本投影的成員，其陳述不算獨立來源（結構判斷）
            consumers = self._layer.consumers_of(scope_id, pid)
            cands = [c for c in cands if c.owner_uid not in consumers]
            # D87：關於本人的旁證（他人轉述）不是本人自述的獨立佐證
            cands = [c for c in cands if self._owner not in (getattr(c, "subject_uids", ()) or ())]
            if not cands:
                continue
            done += 1
            ids = self._reviewer.judge_corroboration(view.summary, cands)  # LLM 在鎖外
            if not ids:
                continue
            chosen = [c for c in cands if c.projection_id in ids]
            outcome = self._reviewer.apply_corroboration(cid, scope_id, chosen)
            if outcome.outcome == "reviewed":
                rep.corroborated += 1

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 已判定登記簿（同一份內容不重複送審）
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _load_judged(self) -> dict[str, dict[str, Any]]:
        try:
            with self._lock():
                if not self._judged_path.exists():
                    return {}
                data = json.loads(self._judged_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception as e:  # noqa: BLE001 - 壞檔視為空（只會多判定一次，不會多分享）
            logger.warning("[SharingPhase] 讀取已判定登記簿失敗，視為空: %s", e)
            return {}

    def _record_judged(self, cid: str, digest: str, scopes: list[str]) -> None:
        try:
            with self._lock():
                data = self._load_judged_unlocked()
                data[cid] = {"digest": digest, "scopes": list(scopes)}
                atomic_write_json(self._judged_path, data)
        except Exception as e:  # noqa: BLE001
            logger.warning("[SharingPhase] 寫入已判定登記簿失敗: %s", e)

    def _forget_judged(self, cid: str) -> None:
        try:
            with self._lock():
                data = self._load_judged_unlocked()
                if data.pop(cid, None) is not None:
                    atomic_write_json(self._judged_path, data)
        except Exception as e:  # noqa: BLE001
            logger.warning("[SharingPhase] 清除已判定登記簿失敗: %s", e)

    def _load_held(self) -> dict[str, dict[str, Any]]:
        try:
            with self._lock():
                if not self._held_path.exists():
                    return {}
                data = json.loads(self._held_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:  # noqa: BLE001 - 壞檔視為空（只會多試幾次，不會多分享）
            return {}

    def _note_held(self, cid: str, digest: str) -> None:
        """held（LLM 例外／非 JSON／摘要過長…）計次；同一份內容超過上限即放棄。"""
        try:
            with self._lock():
                data = self._load_held_unlocked()
                rec = data.get(cid)
                attempts = int(rec.get("attempts", 0)) + 1 if rec and rec.get("digest") == digest else 1
                data[cid] = {
                    "digest": digest, "attempts": attempts,
                    "abandoned": attempts >= self._max_held,
                }
                atomic_write_json(self._held_path, data)
        except Exception as e:  # noqa: BLE001
            logger.warning("[SharingPhase] 寫入 held 登記簿失敗: %s", e)

    def _clear_held(self, cid: str) -> None:
        try:
            with self._lock():
                data = self._load_held_unlocked()
                if data.pop(cid, None) is not None:
                    atomic_write_json(self._held_path, data)
        except Exception as e:  # noqa: BLE001
            logger.warning("[SharingPhase] 清除 held 登記簿失敗: %s", e)

    def _load_held_unlocked(self) -> dict[str, dict[str, Any]]:
        try:
            if not self._held_path.exists():
                return {}
            data = json.loads(self._held_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:  # noqa: BLE001
            return {}

    def _load_judged_unlocked(self) -> dict[str, dict[str, Any]]:
        try:
            if not self._judged_path.exists():
                return {}
            data = json.loads(self._judged_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:  # noqa: BLE001
            return {}
