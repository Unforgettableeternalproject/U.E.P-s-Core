"""
HypothesisReviewer - 假說審核器（M5-D）

沿用 EvidenceReviewer 的三步（LLM 一律在鎖外）：
  1. 鎖內短臨界區：經 Gate 認領（租約）、讀兩端概念快照與證據 unit、計算 pair_digest。
  2. 鎖外：LLM 只看證據原文，判斷「兩個概念是否真的有關」，輸出 status／cited ids／statement。
  3. 結構驗證（只看欄位，不看文字內容）-> 鎖內經 ConceptInhibitor.process_hypothesis_patch 寫回
     （Gate 會重算 pair_digest 與支持度，audit-before-mutate、可 rollback）。

護欄（全為結構檢查，不使用 keyword／regex）：
  - cited ids 必須屬於送審證據；缺引用的 supported／refuted 一律降成 insufficient。
  - supported 的引用必須同時覆蓋兩端概念。
  - 只有 user（主觀事實）與 observation（可觀測操作紀錄）算證據；dream／agent 衍生單位與
    embedding 相似度不算證據，根本不會送審、也不可被引用。
  - 使用者本人的陳述只是主觀事實：沒有獨立佐證（其他說話者或 observation）最高 insufficient。
  - support_strength／門檻／intention 佐證沿用 M4（decide_review_status、learning_weight、noisy-OR）。

失敗語意：llm_fn=None -> skipped（不認領、不寫任何東西）。LLM 失敗／輸出無效 -> 經 Gate 記
llm_failed（維持 proposed、指數退避），不寫假審核。salience 完全不參與。
"""

from __future__ import annotations

import contextlib
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from echo_memory.affect.evidence_reviewer import (
    format_evidence_lines,
    parse_llm_json,
    select_prompt_units,
)
from echo_memory.schemas.evidence_review import CLAIM_KINDS, EvidenceReview
from echo_memory.schemas.hypothesis import (
    OPEN_STATUSES,
    Hypothesis,
    evaluate_hypothesis_support,
    hypothesis_evidence_kind,
)
from echo_memory.storage.conflict_ledger import compute_pair_digest
from echo_memory.utils.source_view import SOURCE_LEGEND

logger = logging.getLogger("echo_memory.affect.hypothesis_reviewer")

_REVIEW_PROMPT = """\
你是記憶系統的假說審核員。系統結構性地發現下列兩個概念可能有關，但這只是「待驗證」的猜測。
你的工作是只依下方「證據原文」判斷兩者是否真的有關；不得引入外部知識，
也不得因為兩個概念名稱或描述聽起來相近、聽起來合理就判為有關。

{legend}

概念 A：{name_a}
描述：{desc_a}
概念 A 的證據（每行格式：[id] 時間 | 來源標示: 內容）：
{evidence_a}

概念 B：{name_b}
描述：{desc_b}
概念 B 的證據（每行格式：[id] 時間 | 來源標示: 內容）：
{evidence_b}

判斷規則：
- status：supported＝已引用的證據原文明確顯示 A 與 B 有關；refuted＝證據明確顯示兩者無關或相反；
  insufficient＝證據不足、只有間接推測，或沒有證據直接支持。
- 只有「使用者說」與「平台觀察」是證據；助手的推測、建議不是證據。
- 同一個使用者自己的說法只是主觀陳述；若沒有其他說話者的陳述或平台觀察佐證，不要判 supported。
- claim_kind：fact＝客觀事實；preference＝偏好；state＝目前狀態；intention＝意圖、計畫、承諾。
- cited_unit_ids：支持你判斷的證據 id，必須同時涵蓋 A 與 B 兩端的證據；只能填上面列出的 id。
- statement：supported 時，用一句話描述兩者的關聯，只能依 cited_unit_ids 的證據撰寫；其他狀態填 null。

以純 JSON 回覆：
{{"status": "supported|refuted|insufficient", "claim_kind": "fact|preference|state|intention", \
"cited_unit_ids": ["..."], "statement": "...", "reason": "..."}}"""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 資料結構
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@dataclass
class HypothesisReviewOutcome:
    """一次假說審核的結果。outcome：reviewed｜held｜stale｜skipped。"""

    outcome: str
    reason: str = ""
    hypothesis_id: str = ""
    status: str = ""  # reviewed 時寫入的結果：supported｜refuted｜insufficient
    patch_id: str = ""


@dataclass
class HypothesisReviewReport:
    """Dream 假說審核 phase 的批次統計。"""

    reviewed: int = 0
    supported: int = 0
    refuted: int = 0
    held: int = 0
    stale: int = 0
    skipped: int = 0
    outcomes: list[HypothesisReviewOutcome] = field(default_factory=list)

    def add(self, outcome: HypothesisReviewOutcome) -> None:
        self.outcomes.append(outcome)
        if outcome.outcome == "reviewed":
            self.reviewed += 1
            if outcome.status == "supported":
                self.supported += 1
            elif outcome.status == "refuted":
                self.refuted += 1
        elif outcome.outcome == "held":
            self.held += 1
        elif outcome.outcome == "stale":
            self.stale += 1
        else:
            self.skipped += 1

    def summary(self) -> str:
        return (
            f"假說審核: 完成 {self.reviewed}（支持 {self.supported}, 否決 {self.refuted}）, "
            f"暫緩 {self.held}, 過期 {self.stale}, 略過 {self.skipped}"
        )


@dataclass
class _Snapshot:
    pair_key: str
    digest: str
    name_a: str
    desc_a: str
    name_b: str
    desc_b: str
    units_a: list[Any]
    units_b: list[Any]


def endpoint_units(provenance: Any, concept_id: str) -> list[Any]:
    """端點概念可追溯到的、具證據資格的 unit（user／observation；其餘一律排除）。"""
    result = provenance.resolve(concept_id)
    return [u for u in result.evidence if hypothesis_evidence_kind(u) is not None]


def verify_support(
    hypothesis: Hypothesis, semantic: Any, provenance: Any,
) -> str | None:
    """supported 假說是否仍成立（讀取時重解析，fail-closed）。

    回傳 None＝仍成立；"endpoint_gone"／"digest_drift"／"evidence_gone"＝失效原因。
    引用證據消失（forget、M-8 遮蔽／塗銷、共享撤回使來源不再可追溯）一律視為 evidence_gone。
    """
    ends = hypothesis.endpoints
    if ends is None:
        return "endpoint_gone"
    concepts = []
    for cid in ends:
        c = semantic.get(cid)
        if c is None or c.status != "active" or c.metadata.get("merged_into"):
            return "endpoint_gone"
        concepts.append(c)
    if compute_pair_digest(concepts[0], concepts[1]) != hypothesis.pair_digest:
        return "digest_drift"
    cited = list(hypothesis.review.cited_evidence_ids)
    if not cited:
        return "evidence_gone"
    live = {u.id for cid in ends for u in endpoint_units(provenance, cid)}
    if any(i not in live for i in cited):
        return "evidence_gone"
    return None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# HypothesisReviewer
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class HypothesisReviewer:
    """假說審核器。只持有唯讀 store；所有寫入經 guard（ConceptInhibitor）。

    Args:
        llm_fn: LLM 呼叫函式；None 時所有審核 skipped（不認領、不寫任何東西）。
        guard: ConceptInhibitor（需有 process_hypothesis_patch）。
        semantic: SemanticMemory。
        provenance: ProvenanceResolver。
        store: HypothesisStore（唯讀）。
        lock_factory: 取得 storage_lock 的工廠；None 時不加鎖（單元測試）。
        cfg: Config；None 取全域。
        triggered_by: 寫回 patch 的觸發來源標籤。
    """

    def __init__(
        self,
        llm_fn: Callable[[str], str] | None,
        guard: Any,
        semantic: Any,
        provenance: Any,
        store: Any,
        lock_factory: Callable[[], Any] | None = None,
        cfg: Any = None,
        triggered_by: str = "dream",
        namespace_id: str | None = None,
    ):
        if cfg is None:
            from echo_memory.config import get_config
            cfg = get_config()
        self._cfg = cfg
        self._llm_fn = llm_fn
        self._gate = guard
        self._semantic = semantic
        self._provenance = provenance
        self._store = store
        self._lock = lock_factory if lock_factory is not None else contextlib.nullcontext
        self._triggered_by = triggered_by
        self._namespace_id = namespace_id  # 明確注入；None 時才由儲存路徑推得

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 公共 API
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def verify_support(self, hypothesis: Hypothesis) -> str | None:
        """見模組層 verify_support；鎖內重解析。"""
        with self._lock(), self._semantic._lock:
            return verify_support(hypothesis, self._semantic, self._provenance)

    def review(self, hypothesis_id: str, *, manual: bool = False) -> HypothesisReviewOutcome:
        """對單一假說做審核（手動入口／Dream 批次共用）。manual=True 時忽略退避。"""
        if self._llm_fn is None:
            return HypothesisReviewOutcome("skipped", "no_llm", hypothesis_id)
        if not hasattr(self._gate, "process_hypothesis_patch"):
            return HypothesisReviewOutcome("skipped", "no_gate", hypothesis_id)
        if self._is_scope_namespace():
            return HypothesisReviewOutcome("skipped", "shared_namespace", hypothesis_id)
        rec = self._store.get(hypothesis_id)
        if rec is None:
            return HypothesisReviewOutcome("skipped", "hypothesis_missing", hypothesis_id)
        if rec.status not in OPEN_STATUSES:
            return HypothesisReviewOutcome("skipped", f"not_open: {rec.status}", hypothesis_id)

        # 步驟 1：經 Gate 認領（租約）；被他人持有、退避中、端點失效一律略過
        token = uuid.uuid4().hex
        with self._lock(), self._semantic._lock:
            ok, tx = self._gate.process_hypothesis_patch(
                "claim",
                {"hypothesis_id": hypothesis_id, "token": token, "manual": manual,
                 "lease_seconds": float(self._cfg.hypothesis_lease_seconds)},
                triggered_by=self._triggered_by,
            )
        if not ok:
            return HypothesisReviewOutcome(
                "skipped", f"claim_rejected: {self._reason(tx)}", hypothesis_id,
            )
        try:
            return self._review_claimed(hypothesis_id, token)
        except Exception as e:  # noqa: BLE001 - 任何失敗都記 llm_failed（退避），不寫假審核
            logger.warning("[HypothesisReviewer] 審核 %s 失敗: %s", hypothesis_id[:12], e, exc_info=True)
            return self._backoff(hypothesis_id, token, f"error: {e}")

    def review_batch(self, limit: int | None = None) -> HypothesisReviewReport:
        """Dream 審核 phase：依建立先後挑可審核的假說（LLM 不在 storage_lock 內）。"""
        report = HypothesisReviewReport()
        batch = limit if limit is not None else int(self._cfg.hypothesis_review_batch)
        if batch <= 0:
            return report
        if self._llm_fn is None:
            report.add(HypothesisReviewOutcome("skipped", "no_llm"))
            return report
        counted = 0
        for hid in self.reviewable_ids():
            if counted >= batch:
                break
            try:
                outcome = self.review(hid)
            except Exception as e:  # noqa: BLE001 - 單筆失敗不拖垮整批
                logger.warning("[HypothesisReviewer] 審核 %s 失敗: %s", hid[:12], e)
                outcome = HypothesisReviewOutcome("skipped", f"error: {e}", hid)
            report.add(outcome)
            counted += 1
        return report

    def reviewable_ids(self) -> list[str]:
        """目前可審核（proposed 且不在退避期、或 under_review 但租約已過期、且未逾 TTL）的假說 id。"""
        now = datetime.now(timezone.utc)
        out: list[str] = []
        for h in self._store.list(OPEN_STATUSES):
            if h.expires_at is not None and h.expires_at <= now:
                continue
            if h.status == "proposed":
                if h.next_review_at is None or h.next_review_at <= now:
                    out.append(h.id)
            elif h.lease_expired(now):
                out.append(h.id)
        return out

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 審核流程
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _review_claimed(self, hid: str, token: str) -> HypothesisReviewOutcome:
        # 步驟 1：鎖內快照
        with self._lock(), self._semantic._lock:
            rec = self._store.get(hid)
            if rec is None or rec.claim_token != token:
                return HypothesisReviewOutcome("skipped", "claim_lost", hid)
            ends = rec.endpoints
            concepts = [self._semantic.get(cid) for cid in (ends or ())]
            if (
                ends is None or any(
                    c is None or c.status != "active" or c.metadata.get("merged_into")
                    for c in concepts
                )
            ):
                gone = True
            else:
                gone = False
                snap = _Snapshot(
                    pair_key=rec.pair_key,
                    digest=compute_pair_digest(concepts[0], concepts[1]),
                    name_a=concepts[0].name, desc_a=concepts[0].description,
                    name_b=concepts[1].name, desc_b=concepts[1].description,
                    units_a=endpoint_units(self._provenance, ends[0]),
                    units_b=endpoint_units(self._provenance, ends[1]),
                )
        if gone:  # 端點失效由維護階段 expire；這裡釋放租約即可
            self._release(hid, token)
            return HypothesisReviewOutcome("skipped", "endpoint_gone", hid)

        side_limit = max(1, int(self._cfg.evidence_review_max_units) // 2)
        prompt_a = select_prompt_units(snap.units_a, side_limit)
        prompt_b = select_prompt_units(snap.units_b, side_limit)
        text_a, shown_a = format_evidence_lines(prompt_a)
        text_b, shown_b = format_evidence_lines(prompt_b)

        if not shown_a or not shown_b:
            # 某一端沒有任何具資格的證據：結構性 insufficient，不叫 LLM（缺來源不得 verified）
            decision = _Decision(
                result="insufficient", claim_kind="fact",
                review=self._build_review(
                    snap, status="insufficient", cited=[], claim_kind="fact", support=None,
                    reason="至少一端概念沒有可解析的證據來源（user／observation）", reviewer="none",
                ),
            )
        else:
            # 步驟 2：鎖外 LLM
            try:
                raw = self._llm_fn(self._build_prompt(snap, text_a, text_b))  # type: ignore[misc]
            except Exception as e:  # noqa: BLE001
                logger.warning("[HypothesisReviewer] LLM 呼叫失敗 %s: %s", hid[:12], e)
                return self._backoff(hid, token, f"llm_error: {e}")
            parsed = parse_llm_json(raw)
            if parsed is None:
                return self._backoff(hid, token, "llm_invalid_json")
            shown = {**shown_a, **shown_b}
            decision = self._decide(snap, parsed, shown, set(shown_a), set(shown_b))
            if decision.held_reason:
                return self._backoff(hid, token, decision.held_reason)

        # 步驟 3：鎖內經 Gate 寫回（Gate 重算 digest 與支持度）
        payload = {
            "hypothesis_id": hid,
            "token": token,
            "result": decision.result,
            "pair_digest": snap.digest,
            "review": decision.review.to_dict() if decision.review else {},
            "statement": decision.statement,
            "claim_kind": decision.claim_kind,
            "coverage": {"a": decision.cover_a, "b": decision.cover_b},
        }
        with self._lock(), self._semantic._lock:
            ok, tx = self._gate.process_hypothesis_patch(
                "review", payload, triggered_by=self._triggered_by,
            )
        patch_id = tx.patches[0].patch_id if tx.patches else ""
        if ok:
            return HypothesisReviewOutcome("reviewed", "", hid, decision.result, patch_id)
        reason = self._reason(tx)
        if reason.startswith("stale"):
            self._release(hid, token)
            return HypothesisReviewOutcome("stale", reason, hid, patch_id=patch_id)
        # Gate 結構驗證不通過（例如引用證據已失效）：不寫假審核，退避後再試
        outcome = self._backoff(hid, token, f"gate_rejected: {reason}")
        outcome.patch_id = patch_id
        return outcome

    def _decide(
        self,
        snap: _Snapshot,
        parsed: dict[str, Any],
        shown: dict[str, Any],
        ids_a: set[str],
        ids_b: set[str],
    ) -> _Decision:
        """把 LLM 輸出轉成審核決定。只看欄位，不看文字內容。"""
        llm_status = parsed.get("status")
        if llm_status not in ("supported", "refuted", "insufficient"):
            return _Decision(held_reason=f"llm_invalid_status: {llm_status!r}")
        claim_kind = parsed.get("claim_kind")
        if claim_kind not in CLAIM_KINDS:
            return _Decision(held_reason=f"llm_invalid_claim_kind: {claim_kind!r}")
        cited_raw = parsed.get("cited_unit_ids", [])
        if not isinstance(cited_raw, list):
            return _Decision(held_reason="llm_invalid_id_list")
        cited = list(dict.fromkeys(str(x) for x in cited_raw))
        unknown = [i for i in cited if i not in shown]
        if unknown:
            # 引用不存在（或未送審）的 evidence id：LLM 失誤，不降級成 supported
            return _Decision(held_reason=f"unknown_evidence_id: {unknown[0]}")

        notes: list[str] = []
        status = llm_status
        cited_units = [shown[i] for i in cited]
        cover_a = [i for i in cited if i in ids_a]
        cover_b = [i for i in cited if i in ids_b]
        if status in ("supported", "refuted") and not cited:
            status = "insufficient"
            notes.append("no_cited_evidence")
        statement = parsed.get("statement")
        statement = statement.strip() if isinstance(statement, str) else ""
        support = evaluate_hypothesis_support(cited_units, claim_kind, self._cfg) if cited else None
        if status == "supported":
            if not statement or len(statement) > int(self._cfg.hypothesis_statement_max_chars):
                status = "insufficient"
                notes.append("statement_missing_or_too_long")
            elif not cover_a or not cover_b:
                status = "insufficient"
                notes.append("citation_not_covering_both_endpoints")
            elif support is None or support.status != "supported":
                status = "insufficient"
                notes.extend(support.notes if support is not None else ["no_support"])

        reason = str(parsed.get("reason") or "").strip()
        if notes:
            reason = f"{reason} [{', '.join(notes)}]".strip()
        review = self._build_review(
            snap, status={"supported": "supported", "refuted": "contradicted",
                          "insufficient": "insufficient"}[status],
            cited=cited, claim_kind=claim_kind, support=support,
            reason=reason or "LLM 假說審核", reviewer="llm",
        )
        return _Decision(
            result=status, review=review, claim_kind=claim_kind,
            statement=statement if status == "supported" else None,
            cover_a=cover_a, cover_b=cover_b,
        )

    def _build_review(
        self,
        snap: _Snapshot,
        *,
        status: str,
        cited: list[str],
        claim_kind: str,
        support: Any,
        reason: str,
        reviewer: str,
    ) -> EvidenceReview:
        return EvidenceReview(
            status=status,
            support_strength=float(support.strength) if support is not None else 0.0,
            cited_evidence_ids=list(cited),
            independent_count=len(support.independent_ids) if support is not None else 0,
            basis_digest=snap.digest,
            reviewed_at=datetime.now(timezone.utc),
            reviewer=reviewer,
            reason=reason,
            claim_kind=claim_kind,
            corroborated_by=list(support.independent_ids) if support is not None else [],
            corroboration_strength=support.corroboration_strength if support is not None else None,
        )

    @staticmethod
    def _build_prompt(snap: _Snapshot, text_a: str, text_b: str) -> str:
        return _REVIEW_PROMPT.format(
            legend=SOURCE_LEGEND,
            name_a=snap.name_a, desc_a=snap.desc_a, evidence_a=text_a,
            name_b=snap.name_b, desc_b=snap.desc_b, evidence_b=text_b,
        )

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 輔助
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _is_scope_namespace(self) -> bool:
        from echo_memory.schemas.sharing import is_scope_namespace_id

        ns = self._namespace_id or Path(self._semantic.storage_path).parent.name
        return is_scope_namespace_id(ns)

    @staticmethod
    def _reason(tx: Any) -> str:
        patch = tx.patches[0] if tx.patches else None
        return (patch.rejection_reason if patch is not None else "") or "gate_rejected"

    def _release(self, hid: str, token: str) -> None:
        """主動釋放租約（stale／端點失效時，讓假說立刻回到 proposed）。失敗只記 log，租約到期也會自動釋放。"""
        try:
            with self._lock(), self._semantic._lock:
                self._gate.process_hypothesis_patch(
                    "release", {"hypothesis_id": hid, "token": token},
                    triggered_by=self._triggered_by,
                )
        except Exception as e:  # noqa: BLE001
            logger.warning("[HypothesisReviewer] 釋放租約失敗 %s: %s", hid[:12], e)

    def _backoff(self, hid: str, token: str, reason: str) -> HypothesisReviewOutcome:
        """LLM 失敗／輸出無效：經 Gate 記 llm_failed（維持 proposed、指數退避），不寫假審核。"""
        try:
            with self._lock(), self._semantic._lock:
                self._gate.process_hypothesis_patch(
                    "review", {"hypothesis_id": hid, "token": token, "result": "llm_failed"},
                    triggered_by=self._triggered_by,
                )
        except Exception as e:  # noqa: BLE001
            logger.warning("[HypothesisReviewer] 記錄退避失敗 %s: %s", hid[:12], e)
        return HypothesisReviewOutcome("held", reason, hid)


@dataclass
class _Decision:
    """LLM 輸出經結構驗證後的決定。held_reason 非空代表要退避。"""

    result: str = "insufficient"
    review: EvidenceReview | None = None
    claim_kind: str = "fact"
    statement: str | None = None
    cover_a: list[str] = field(default_factory=list)
    cover_b: list[str] = field(default_factory=list)
    held_reason: str = ""
