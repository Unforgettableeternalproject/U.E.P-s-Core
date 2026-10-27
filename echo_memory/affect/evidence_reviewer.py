"""
EvidenceReviewer - 概念證據審核器（M4 包 B）

對應設計 §3.2 三步（LLM 一律在鎖外）：
  1. 鎖內短臨界區：讀概念快照、provenance 取 evidence units、independent_support、
     計算 basis_digest。
  2. 鎖外：LLM 只依證據原文判 status／claim_kind／cited ids／scope／corroborating ids。
  3. 結構驗證（只看欄位，不看文字內容）→ 鎖內重算 digest → 經
     ConceptInhibitor.process_review_patch 寫回（audit-before-mutate、可 rollback）。

學習程度（§8）：support_strength 以 source_type=user 的 unit 的學習權重 w
做 noisy-OR 合成（同一 episode 取最大 w，不重複複利），達 SUPPORT_THRESHOLD 才可 supported；
intention 類主張（意圖／計畫／承諾）自述最高 insufficient，需交叉佐證。

架構禁令（CLAUDE.md）：
  - 主張類型、範圍、是否支持全由 LLM 結構化輸出；程式只做欄位層級的結構護欄，
    不以 keyword / regex 判斷內容。
  - salience 不是門檻，審核流程完全不讀 salience。

無 LLM 一律 skipped（維持 not_checked，不寫 patch、不落盤假審核）。
LLM 失敗／回傳非 JSON／引用不存在的 evidence id -> held：記進審核器自己的待審登記簿
(review_pending.json)，下次 Dream／review_batch 重跑，超過重試上限轉 abandoned 並留 audit。
（ConceptInhibitor.review_held_patches 無法重審「尚無內容」的 review，故由審核器自管。）
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from echo_memory.affect.concept_patch import (
    ConceptPatch,
    PatchEvidence,
    PatchTransaction,
)
from echo_memory.schemas.evidence import (
    EvidenceUnit,
    can_attest_agent_said,
    can_attest_user_claim,
    learning_weight,
)
from echo_memory.schemas.evidence_review import (
    CLAIM_KINDS,
    REVIEWED_STATUSES,
    EvidenceReview,
    compute_basis_digest,
    decide_review_status,
    noisy_or,
)
from echo_memory.schemas.sharing import (
    SHAREABLE_SENSITIVITIES,
    SENSITIVITIES,
    parse_guild_scope,
)
from echo_memory.storage.atomic import atomic_write_json
from echo_memory.utils.source_view import SOURCE_LEGEND, classify_unit, render_unit

logger = logging.getLogger("echo_memory.affect.evidence_reviewer")

PENDING_FILENAME = "review_pending.json"

_REVIEW_PROMPT = """\
你是記憶系統的證據審核員。判斷下列「概念」是否被「證據」支持。
只依證據原文判斷，不得引入外部知識，也不得因為概念聽起來合理就判為支持。

{legend}

概念名稱：{name}
概念描述：{description}
概念主體（subject）：{subject}
概念來源類別（support_source）：{support_source}

證據（每行格式：[id] 時間 | 來源標示: 內容）：
{evidence}

判斷規則：
- status：supported＝已引用的證據明確支持概念描述；contradicted＝證據明確與概念描述相反；
  insufficient＝證據不足、只有間接推測，或沒有證據直接支持。
- 助手的推測、建議、猜測不是使用者的事實或偏好；只有「使用者說（已確認）」可支持關於使用者的描述。
- claim_kind：fact＝客觀事實；preference＝偏好；state＝目前狀態；
  intention＝意圖、計畫、承諾（尚未發生的事）。
- 若 claim_kind 是 intention，且有其他證據交叉佐證（平台觀察、他人陳述、使用者事後陳述已完成），
  把那些證據 id 填入 corroborating_unit_ids；沒有就留空陣列。
- cited_unit_ids 與 corroborating_unit_ids 只能填上面列出的 id。
- scope：subject_ref 填這個主張是關於誰（自由文字），context 填適用情境或時間範圍（沒有就 null）。

以純 JSON 回覆：
{{"status": "supported|contradicted|insufficient", "claim_kind": "fact|preference|state|intention", \
"cited_unit_ids": ["..."], "corroborating_unit_ids": ["..."], \
"scope": {{"subject_ref": "...", "context": "..."}}, "reason": "..."}}"""


_SHARE_PROMPT = """你是記憶系統的分享審核員。下列「證據」都來自多人場合（群組語音或文字）。
你要從這些證據整理出一則可分享給同一場域其他成員的概念摘要。
只依證據判斷，不得引入外部知識；證據以外的任何資訊一律不得出現在摘要。
判斷依語意與語氣，不得依賴特定字詞。

{legend}

證據（每行格式：[id] 時間 | 來源標示: 內容）：
{evidence}

判斷規則：
- sensitivity：none＝客觀事實或公開資訊；personal＝一般個人偏好、習慣；
  sensitive＝健康、財務、感情、身分識別、位置、對他人的指控等不宜轉述的資訊。
- claim_kind：fact＝客觀事實；preference＝偏好；state＝目前狀態；intention＝意圖、計畫、承諾。
- cited_unit_ids：支撐這個概念的證據 id，只能填上面列出的 id。
- supports_summary_from_cited_only：shareable_summary 是否完全被 cited_unit_ids 的證據支撐（true／false）。
- shareable_summary：第三人稱、去逐字、不含任何人的名字與私密細節，{max_chars} 字以內；
  只能依 cited_unit_ids 的證據撰寫。
- privacy_restrict_unit_ids：說話者表達了「不希望被轉述、別說出去」意圖的證據 id（沒有就留空陣列）。

以純 JSON 回覆：
{{"sensitivity": "none|personal|sensitive", "claim_kind": "fact|preference|state|intention", "cited_unit_ids": ["..."], "supports_summary_from_cited_only": true, "shareable_summary": "...", "privacy_restrict_unit_ids": ["..."], "reason": "..."}}"""

_PRIVACY_SCAN_PROMPT = """你是記憶系統的隱私意圖掃描員。下列是群組場合中使用者說的話。
判斷哪些話的說話者表達了「不希望被轉述、別說出去」的意圖。依語意與語氣判斷，不得依賴特定字詞。

{legend}

證據（每行格式：[id] 時間 | 來源標示: 內容）：
{evidence}

privacy_restrict_unit_ids 只能填上面列出的 id；沒有就留空陣列。
以純 JSON 回覆：{{"privacy_restrict_unit_ids": ["..."], "reason": "..."}}"""

_CORROBORATION_PROMPT = """你是記憶系統的交叉佐證審核員。判斷下列「候選陳述」是否獨立佐證「主張」。
只依文字語意判斷；候選必須指涉同一個主張，且是該成員獨立得知，而非轉述主張本身。

主張：{claim}

候選陳述（每行格式：[projection_id] 摘要）：
{candidates}

corroborating_projection_ids 只能填上面列出的 id；沒有就留空陣列。
以純 JSON 回覆：{{"corroborating_projection_ids": ["..."], "reason": "..."}}"""

SHARE_SUMMARY_MAX_CHARS = 120


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 資料結構
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@dataclass
class ReviewOutcome:
    """一次審核的結果。outcome：reviewed｜held｜stale｜skipped。"""

    outcome: str
    reason: str = ""
    concept_id: str = ""
    status: str = ""  # reviewed 時寫入的審核狀態
    patch_id: str = ""


@dataclass
class ReviewBatchReport:
    """Dream 審核 phase 的批次統計。"""

    reviewed: int = 0
    held: int = 0
    stale: int = 0
    skipped: int = 0
    outcomes: list[ReviewOutcome] = field(default_factory=list)

    def add(self, outcome: ReviewOutcome) -> None:
        self.outcomes.append(outcome)
        if outcome.outcome == "reviewed":
            self.reviewed += 1
        elif outcome.outcome == "held":
            self.held += 1
        elif outcome.outcome == "stale":
            self.stale += 1
        else:
            self.skipped += 1

    def summary(self) -> str:
        return (
            f"證據審核: 完成 {self.reviewed}, 暫緩 {self.held}, "
            f"過期 {self.stale}, 略過 {self.skipped}"
        )


@dataclass
class EvidenceSnapshot:
    """鎖內取得的審核基礎快照（供 ContradictionDetector 重用）。"""

    concept_id: str
    name: str
    description: str
    subject: str
    support_source: str
    digest: str
    units: list[EvidenceUnit]  # provenance 解析到的全部、未塗銷的 unit
    support: Any  # IndependentSupport
    scope: dict[str, Any] = field(default_factory=dict)


@dataclass
class SharingJudgement:
    """一次分享判定（只讀判定，不寫任何東西）。outcome：judged｜skipped｜held。

    shareable=True 才可投影；cited_unit_ids ⊆ U_S（scope 內來源可見度為 scope_shared 的使用者 unit）。
    """

    outcome: str
    reason: str = ""
    concept_id: str = ""
    scope_id: str = ""
    digest: str = ""
    shareable: bool = False
    sensitivity: str = "none"
    claim_kind: str = "fact"
    cited_unit_ids: list[str] = field(default_factory=list)
    shareable_summary: str = ""
    privacy_restrict_unit_ids: list[str] = field(default_factory=list)
    review_status: str = "insufficient"
    support_strength: float = 0.0


def aware(dt: datetime | None) -> datetime | None:
    """把 naive datetime 視為 UTC，避免比較時拋 TypeError。"""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def collect_snapshot(
    semantic: Any,
    provenance: Any,
    concept_id: str,
) -> EvidenceSnapshot | None:
    """讀概念與其 evidence units（呼叫端負責持鎖）。概念不存在回傳 None。"""
    concept = semantic.get(concept_id)
    if concept is None:
        return None
    result = provenance.resolve(concept_id)
    support = provenance.independent_support([concept_id])
    units = [u for u in result.evidence if u.tombstoned_at is None]
    review = concept.evidence_review if isinstance(concept.evidence_review, dict) else {}
    scope = review.get("scope") if isinstance(review.get("scope"), dict) else {}
    return EvidenceSnapshot(
        concept_id=concept.id,
        name=concept.name,
        description=concept.description,
        subject=getattr(concept, "subject", "world") or "world",
        support_source=str(concept.metadata.get("support_source") or "legacy_unknown"),
        digest=compute_basis_digest(concept),
        units=units,
        support=support,
        scope=dict(scope),
    )


def render_review_unit(u: EvidenceUnit, max_chars: int = 400) -> str | None:
    """審核用的 unit 標示行。與 source_view.render_unit 相同，但保留 observation（交叉佐證用）。"""
    if classify_unit(u) == "observation":
        content = (u.content or "").strip()
        if not content:
            return None
        return f"平台觀察（不是任何人說的話）: {content[:max_chars]}"
    return render_unit(u, max_chars=max_chars)


def select_prompt_units(units: list[EvidenceUnit], limit: int) -> list[EvidenceUnit]:
    """送審 unit 的挑選：使用者陳述 > 平台觀察 > 助手說出口 > 其他，同類新的在前；上限 limit。"""
    order = {"user_claim": 0, "observation": 1, "agent_said": 2}

    def key(u: EvidenceUnit):
        ts = aware(u.occurred_at)
        return (order.get(classify_unit(u), 3), -(ts.timestamp() if ts else 0.0), u.id)

    return sorted(units, key=key)[: max(limit, 0)]


def format_evidence_lines(units: list[EvidenceUnit]) -> tuple[str, dict[str, EvidenceUnit]]:
    """渲染證據行；回傳 (文字, 實際送審的 id -> unit)。無法渲染的 unit 不送審、也不可被引用。"""
    lines: list[str] = []
    shown: dict[str, EvidenceUnit] = {}
    for u in units:
        text = render_review_unit(u)
        if text is None:
            continue
        ts = aware(u.occurred_at)
        stamp = ts.strftime("%Y-%m-%d %H:%M") if ts else "時間未知"
        lines.append(f"[{u.id}] {stamp} | {text}")
        shown[u.id] = u
    return "\n".join(lines), shown


def parse_llm_json(response: Any) -> dict[str, Any] | None:
    """從 LLM 回應取出第一個 JSON 物件；失敗回傳 None。"""
    if not isinstance(response, str):
        return None
    text = response.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


@dataclass
class _Decision:
    """LLM 輸出經結構驗證後的決定。held_reason 非空代表要進 held。"""

    review: EvidenceReview | None = None
    held_reason: str = ""
    skip_reason: str = ""
    notes: list[str] = field(default_factory=list)


def has_context(u: EvidenceUnit) -> bool:
    """unit 是否帶情境資訊（宿主附的 channel_kind／audience_size；缺代表舊資料或未接線宿主）。"""
    attrs = u.attrs or {}
    return attrs.get("channel_kind") is not None or attrs.get("audience_size") is not None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# EvidenceReviewer
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class EvidenceReviewer:
    """證據審核器。

    Args:
        llm_fn: LLM 呼叫函式；None 時所有審核 skipped（維持 not_checked）。
        guard: ConceptInhibitor（需有 process_review_patch）；審核結果只經它寫回。
        semantic: SemanticMemory。
        provenance: ProvenanceResolver（resolve／independent_support）。
        evidence: EvidenceStore（目前僅保留供呼叫端相容，unit 由 provenance 解析）。
        lock_factory: 取得 storage_lock 的工廠；None 時不加鎖（單元測試）。
        cfg: Config；None 取全域。
        max_attempts: 同一份內容審核失敗（held）幾次後放棄（None 取 config 的 held 重試上限）。
        pending_path: 待審登記簿路徑；None 用 semantic.storage_path / review_pending.json。
        triggered_by: 寫回 patch 的觸發來源標籤。
        privacy_ledger: PrivacyLedger（is_restricted）；分享判定必要，缺少一律不分享（fail-closed）。
        visibility_fn: ``(unit, ledger) -> "private"|"scope_shared"``；缺省用
            schemas.sharing.origin_visibility。
    """

    def __init__(
        self,
        llm_fn: Callable[[str], str] | None,
        guard: Any,
        semantic: Any,
        provenance: Any,
        evidence: Any = None,
        lock_factory: Callable[[], Any] | None = None,
        cfg: Any = None,
        max_attempts: int | None = None,
        pending_path: str | Path | None = None,
        triggered_by: str = "dream",
        privacy_ledger: Any = None,
        visibility_fn: Callable[[Any, Any], str] | None = None,
    ):
        if cfg is None:
            from echo_memory.config import get_config
            cfg = get_config()
        self._cfg = cfg
        self._llm_fn = llm_fn
        self._gate = guard
        self._semantic = semantic
        self._provenance = provenance
        self._evidence = evidence
        self._lock = lock_factory if lock_factory is not None else contextlib.nullcontext
        self._max_attempts = (
            max_attempts if max_attempts is not None
            else int(getattr(cfg, "concept_held_max_retries", 3))
        )
        self._pending_path = (
            Path(pending_path) if pending_path is not None
            else Path(semantic.storage_path) / PENDING_FILENAME
        )
        self._triggered_by = triggered_by
        self._privacy_ledger = privacy_ledger
        if visibility_fn is None:
            from echo_memory.schemas.sharing import origin_visibility as visibility_fn
        self._visibility_fn = visibility_fn

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 公共 API
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def review(self, concept_id: str) -> ReviewOutcome:
        """對單一概念做審核（手動入口／Dream 批次共用）。"""
        if self._llm_fn is None:
            return ReviewOutcome("skipped", "no_llm", concept_id)
        if not hasattr(self._gate, "process_review_patch"):
            return ReviewOutcome("skipped", "no_gate", concept_id)

        # 步驟 1：鎖內快照
        with self._lock(), self._semantic._lock:
            concept = self._semantic.get(concept_id)
            if concept is None:
                return ReviewOutcome("skipped", "concept_missing", concept_id)
            if concept.status != "active" or concept.metadata.get("merged_into"):
                return ReviewOutcome("skipped", "concept_inactive", concept_id)
            snap = collect_snapshot(self._semantic, self._provenance, concept_id)
        if snap is None:
            return ReviewOutcome("skipped", "concept_missing", concept_id)

        # 政策：可作證的使用者 unit 全部缺情境資訊（舊資料）-> 不寫審核（維持 not_checked），
        # 避免首次 Dream 把既有單來源偏好一律降成 insufficient
        independent = set(snap.support.independent_ids)
        attest_users = [
            u for u in snap.units if u.id in independent and can_attest_user_claim(u)
        ]
        if attest_users and not any(has_context(u) for u in attest_users):
            return self._skip_no_context(snap)

        prompt_units = select_prompt_units(snap.units, int(self._cfg.evidence_review_max_units))
        evidence_text, shown = format_evidence_lines(prompt_units)

        # 沒有任何可送審的證據：結構性 insufficient，不叫 LLM（reviewer="none"）
        if not shown:
            decision = _Decision(
                review=self._build_review(
                    snap, status="insufficient", strength=0.0, cited=[], claim_kind="fact",
                    corroborated=[], scope={}, reason="沒有可解析的證據來源", reviewer="none",
                ),
                notes=["no_resolvable_evidence"],
            )
        else:
            # 步驟 2：鎖外 LLM
            try:
                raw = self._llm_fn(self._build_prompt(snap, evidence_text))
            except Exception as e:  # noqa: BLE001 - LLM 任何失敗都進 held
                logger.warning("[EvidenceReviewer] LLM 呼叫失敗 %s: %s", concept_id[:12], e)
                return self._hold(snap, f"llm_error: {e}")
            parsed = parse_llm_json(raw)
            if parsed is None:
                return self._hold(snap, "llm_invalid_json")
            decision = self._decide(snap, parsed, shown)
            if decision.held_reason:
                return self._hold(snap, decision.held_reason)
            if decision.skip_reason:
                return self._skip_no_context(snap)

        # 步驟 3：鎖內重算 digest、經 Gate 寫回
        return self._commit(snap, decision)

    def review_batch(self, limit: int | None = None) -> ReviewBatchReport:
        """Dream 審核 phase：依優先序挑概念審核（LLM 不在 storage_lock 內）。

        優先序：新概念（從未審核）> digest 失效（有審核但基礎已變）> held 待審重跑；
        同類內以 access_count、conflict_count 高者優先。
        """
        report = ReviewBatchReport()
        batch = limit if limit is not None else int(self._cfg.evidence_review_batch)
        if batch <= 0:
            return report
        if self._llm_fn is None:
            report.add(ReviewOutcome("skipped", "no_llm"))
            return report

        counted = 0
        for cid in self._select_candidates(None):
            if counted >= batch:
                break
            try:
                outcome = self.review(cid)
            except Exception as e:  # noqa: BLE001 - 單筆失敗不拖垮整批
                logger.warning("[EvidenceReviewer] 審核 %s 失敗: %s", cid[:12], e)
                outcome = ReviewOutcome("skipped", f"error: {e}", cid)
            report.add(outcome)
            # no_context 不呼叫 LLM、不寫審核，不佔批次名額（且之後內容不變就不再選中）
            if not (outcome.outcome == "skipped" and outcome.reason == "no_context"):
                counted += 1
        return report

    def pending_ids(self) -> list[str]:
        """目前待重審（尚未放棄）的概念 id。"""
        with self._lock():
            return [
                cid for cid, rec in self._load_pending().items()
                if not rec.get("abandoned")
            ]

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 候選挑選
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _select_candidates(self, batch: int | None) -> list[str]:
        with self._lock(), self._semantic._lock:
            pending = self._load_pending()
            fresh: list[Any] = []
            invalidated: list[Any] = []
            retry: list[Any] = []
            for c in self._semantic.list_concepts(status="active", limit=None):
                if c.metadata.get("merged_into"):
                    continue
                if c.effective_review_status() != "not_checked":
                    continue
                rec = pending.get(c.id)
                digest = compute_basis_digest(c)
                if rec is not None and rec.get("digest") == digest:
                    # 同一份內容仍待審：放棄者略過，否則排進 held 重跑
                    if not rec.get("abandoned"):
                        retry.append(c)
                    continue
                if not c.evidence_review:
                    fresh.append(c)
                else:
                    invalidated.append(c)

        def rank(c: Any):
            return (-int(c.access_count), -int(c.conflict_count), c.created_at.timestamp())

        ordered: list[str] = []
        for group in (fresh, invalidated, retry):
            ordered.extend(c.id for c in sorted(group, key=rank))
        return ordered if batch is None else ordered[:batch]

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Prompt 與結構驗證
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    @staticmethod
    def _build_prompt(snap: EvidenceSnapshot, evidence_text: str) -> str:
        return _REVIEW_PROMPT.format(
            legend=SOURCE_LEGEND,
            name=snap.name,
            description=snap.description,
            subject=snap.subject,
            support_source=snap.support_source,
            evidence=evidence_text,
        )

    def _decide(
        self,
        snap: EvidenceSnapshot,
        parsed: dict[str, Any],
        shown: dict[str, EvidenceUnit],
    ) -> _Decision:
        """把 LLM 輸出轉成審核決定。只看欄位，不看文字內容。"""
        llm_status = parsed.get("status")
        if llm_status not in REVIEWED_STATUSES:
            return _Decision(held_reason=f"llm_invalid_status: {llm_status!r}")
        claim_kind = parsed.get("claim_kind")
        if claim_kind not in CLAIM_KINDS:
            return _Decision(held_reason=f"llm_invalid_claim_kind: {claim_kind!r}")

        cited_raw = parsed.get("cited_unit_ids", [])
        corro_raw = parsed.get("corroborating_unit_ids", [])
        if not isinstance(cited_raw, list) or not isinstance(corro_raw, list):
            return _Decision(held_reason="llm_invalid_id_list")
        cited_ids = [str(x) for x in cited_raw]
        corro_ids = [str(x) for x in corro_raw]
        unknown = [i for i in cited_ids + corro_ids if i not in shown]
        if unknown:
            # 引用不存在的 evidence id：LLM 失誤，不降級成 supported
            return _Decision(held_reason=f"unknown_evidence_id: {unknown[0]}")
        cited_ids = list(dict.fromkeys(cited_ids))
        cited_units = [shown[i] for i in cited_ids]
        cited_users = [u for u in cited_units if can_attest_user_claim(u)]
        if cited_users and not any(has_context(u) for u in cited_users):
            return _Decision(skip_reason="no_context")

        notes: list[str] = []
        scope = parsed.get("scope") if isinstance(parsed.get("scope"), dict) else {}
        strength, attestable = self._support_strength(snap, cited_units)

        status = llm_status
        # 支持／反駁都必須有被引用的證據
        if status in ("supported", "contradicted") and not cited_units:
            status = "insufficient"
            notes.append("no_cited_evidence")
        # supported 需至少一個可作證的被引用 unit（使用者陳述；subject=agent 時含助手已說出口）
        if status == "supported" and not attestable:
            status = "insufficient"
            notes.append("no_attestable_citation")
        # 助手推測不能自證；legacy 無可作證 unit 已由上一條擋下
        if status == "supported" and snap.support_source == "agent_only":
            status = "insufficient"
            notes.append("agent_only_cap")

        corroborated: list[str] = []
        if claim_kind == "intention":
            corroborated = self._eligible_corroborators(corro_ids, cited_units, shown)
            if corro_ids and not corroborated:
                notes.append("corroboration_not_eligible")

        before_gate = status
        status = decide_review_status(status, strength, claim_kind, corroborated, self._cfg)
        if before_gate == "supported" and status == "insufficient":
            if claim_kind == "intention":
                notes.append("intention_needs_corroboration")
            else:
                notes.append("below_support_threshold")

        reason = str(parsed.get("reason") or "").strip()
        if notes:
            reason = f"{reason} [{', '.join(notes)}]".strip()
        review = self._build_review(
            snap, status=status, strength=strength, cited=cited_ids, claim_kind=claim_kind,
            corroborated=corroborated, scope=scope, reason=reason or "LLM 證據審核",
            reviewer="llm",
        )
        return _Decision(review=review, notes=notes)

    def _support_strength(
        self,
        snap: EvidenceSnapshot,
        cited_units: list[EvidenceUnit],
    ) -> tuple[float, bool]:
        """被引用 unit 的 noisy-OR 支持度（§8.2）與是否至少一個可作證。

        只計 independent_support 去重後的 known unit；同一 episode 取最大 w，不重複複利。
        """
        independent = set(snap.support.independent_ids)
        by_group: dict[str, float] = {}
        attestable = False
        for u in cited_units:
            if u.id not in independent:
                continue
            if can_attest_user_claim(u):
                w = learning_weight(u, self._cfg)
            elif snap.subject == "agent" and can_attest_agent_said(u):
                w = 1.0
            else:
                continue
            attestable = True
            group = u.episode_id or u.id
            by_group[group] = max(by_group.get(group, 0.0), w)
        return noisy_or(by_group.values()), attestable

    @staticmethod
    def _eligible_corroborators(
        corro_ids: list[str],
        cited_units: list[EvidenceUnit],
        shown: dict[str, EvidenceUnit],
    ) -> list[str]:
        """intention 的交叉佐證結構資格（§8.3）；是否真的佐證由 LLM 判，這裡只擋資格不符者。

        (a) observation（平台觀察）；
        (b) 其他說話者的使用者陳述（speaker_id 與所有被引用的主張 unit 不同，且皆非 None）；
        (c) 使用者事後陳述：known 的 user unit，occurred_at 晚於所有被引用主張 unit。
        被引用的主張 unit 本身、助手內容、未確認來源都不能當佐證。
        """
        cited_ids = {u.id for u in cited_units}
        claim_speakers = {u.speaker_id for u in cited_units}
        claim_times = [aware(u.occurred_at) for u in cited_units if u.occurred_at]
        latest_claim = max(claim_times) if claim_times else None
        out: list[str] = []
        for cid in dict.fromkeys(corro_ids):
            u = shown[cid]
            if cid in cited_ids or u.tombstoned_at is not None:
                continue
            if u.source_type == "observation":
                ok = u.provenance_status == "known"
            elif can_attest_user_claim(u):
                other_speaker = (
                    u.speaker_id is not None
                    and None not in claim_speakers
                    and u.speaker_id not in claim_speakers
                )
                later = (
                    latest_claim is not None
                    and aware(u.occurred_at) is not None
                    and aware(u.occurred_at) > latest_claim
                )
                ok = other_speaker or later
            else:
                ok = False
            if ok:
                out.append(cid)
        return out

    def _build_review(
        self,
        snap: EvidenceSnapshot,
        *,
        status: str,
        strength: float,
        cited: list[str],
        claim_kind: str,
        corroborated: list[str],
        scope: dict[str, Any],
        reason: str,
        reviewer: str,
    ) -> EvidenceReview:
        return EvidenceReview(
            status=status,
            support_strength=strength,
            cited_evidence_ids=list(cited),
            scope=dict(scope),
            independent_count=int(snap.support.independent_episode_count),
            user_attested_count=int(snap.support.user_attested_count),
            legacy_unknown_count=int(snap.support.legacy_unknown_count),
            basis_digest=snap.digest,
            reviewed_at=datetime.now(timezone.utc),
            reviewer=reviewer,
            reason=reason,
            claim_kind=claim_kind,
            corroborated_by=list(corroborated),
        )

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 寫回與 held
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _commit(self, snap: EvidenceSnapshot, decision: _Decision) -> ReviewOutcome:
        review = decision.review
        assert review is not None
        with self._lock(), self._semantic._lock:
            current = self._semantic.get(snap.concept_id)
            if current is None:
                return ReviewOutcome("skipped", "concept_missing", snap.concept_id)
            if compute_basis_digest(current) != snap.digest:
                logger.info("[EvidenceReviewer] %s 在 LLM 期間內容已變動，丟棄審核", snap.concept_id[:12])
                self._drop_pending(snap.concept_id)
                return ReviewOutcome("stale", "basis_changed", snap.concept_id)
            ok, tx = self._gate.process_review_patch(
                snap.concept_id, review, user_input=f"[evidence_review] {snap.name}",
                triggered_by=self._triggered_by,
            )
        patch = tx.patches[0] if tx.patches else None
        patch_id = patch.patch_id if patch is not None else ""
        if ok:
            self._drop_pending(snap.concept_id)
            return ReviewOutcome("reviewed", "", snap.concept_id, review.status, patch_id)
        reason = (patch.rejection_reason if patch is not None else "") or "gate_rejected"
        if reason.startswith("stale"):
            self._drop_pending(snap.concept_id)
            return ReviewOutcome("stale", reason, snap.concept_id, patch_id=patch_id)
        return ReviewOutcome("skipped", f"gate_rejected: {reason}", snap.concept_id, patch_id=patch_id)

    def _skip_no_context(self, snap: EvidenceSnapshot) -> ReviewOutcome:
        """缺情境資訊：不寫審核。登記成 abandoned，讓批次在內容不變時不重複送審（手動審核仍可）。"""
        with self._lock():
            pending = self._load_pending()
            pending[snap.concept_id] = {
                "digest": snap.digest,
                "name": snap.name,
                "reason": "no_context",
                "attempts": 0,
                "held_at": datetime.now(timezone.utc).isoformat(),
                "last_attempt_at": datetime.now(timezone.utc).isoformat(),
                "abandoned": True,
            }
            self._save_pending(pending)
        return ReviewOutcome("skipped", "no_context", snap.concept_id)

    def _hold(self, snap: EvidenceSnapshot, reason: str) -> ReviewOutcome:
        """LLM 失敗／輸出無效：登記待審並留 audit；超過重試上限轉 abandoned。"""
        with self._lock():
            pending = self._load_pending()
            rec = pending.get(snap.concept_id)
            attempts = 1
            held_at = datetime.now(timezone.utc).isoformat()
            if rec is not None and rec.get("digest") == snap.digest:
                attempts = int(rec.get("attempts", 0)) + 1
                held_at = rec.get("held_at", held_at)
            abandoned = attempts >= self._max_attempts
            pending[snap.concept_id] = {
                "digest": snap.digest,
                "name": snap.name,
                "reason": reason,
                "attempts": attempts,
                "held_at": held_at,
                "last_attempt_at": datetime.now(timezone.utc).isoformat(),
                "abandoned": abandoned,
            }
            self._save_pending(pending)
        self._audit_hold(snap, reason, attempts, abandoned)
        return ReviewOutcome("held", reason, snap.concept_id)

    def _audit_hold(
        self, snap: EvidenceSnapshot, reason: str, attempts: int, abandoned: bool,
    ) -> None:
        """held／放棄各留一筆 audit（沿用 Gate 的 audit log；沒有就只記 log）。"""
        writer = getattr(self._gate, "_write_audit", None)
        if writer is None:
            return
        try:
            tx = PatchTransaction(
                patches=[],
                user_input=f"[evidence_review] {snap.name}",
                llm_response="",
                session_id="evidence-review",
                turn_id="",
                triggered_by=self._triggered_by,  # type: ignore[arg-type]
            )
            patch = ConceptPatch(
                transaction_id=tx.transaction_id,
                concept_id=snap.concept_id,
                concept_name=snap.name,
                action="review",
                target_path="evidence_review",
                new_value={"basis_digest": snap.digest, "attempts": attempts},
                evidence=PatchEvidence(
                    evidence_kind="evidence_review",
                    evidence_strength=0.7,
                    verifier_status="valid",
                    verifier_reason=reason,
                    retrieved_concept_ids=[snap.concept_id],
                ),
                risk_level="low",
            )
            patch.status = "rejected" if abandoned else "held"
            patch.rejection_reason = (
                f"review held 超過重試上限（{attempts} 次）: {reason}" if abandoned else reason
            )
            tx.patches.append(patch)
            tx.status = "committed"
            writer(tx)
        except Exception as e:  # noqa: BLE001 - audit 失敗不影響待審登記
            logger.warning("[EvidenceReviewer] held audit 寫入失敗: %s", e)

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 分享判定（M10）：輸入只含 U_S，LLM 一律在鎖外
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def sharing_ready(self) -> bool:
        """分享判定的前置條件：有 LLM、ledger、可見度函式。缺任一個一律不分享。"""
        return (
            self._llm_fn is not None
            and self._privacy_ledger is not None
            and self._visibility_fn is not None
        )

    def _shareable_scope(self, u: EvidenceUnit) -> str | None:
        """unit 的來源可見度為 scope_shared 時回傳其 ``guild:<id>``（只看結構欄位）。"""
        if self._visibility_fn is None or self._privacy_ledger is None:
            return None
        try:
            visible = self._visibility_fn(u, self._privacy_ledger)
        except Exception:  # noqa: BLE001 - 無法判斷 -> private
            return None
        if visible != "scope_shared":
            return None
        scope = getattr(u, "scope", None)
        return scope if parse_guild_scope(scope) is not None else None

    def cited_units_shareable(self, unit_ids: list[str], scope_id: str) -> bool | None:
        """投影引用的來源 unit 是否仍可分享（未被遮蔽、未被收緊、仍屬該場域）。

        任一不符回傳 False（forget／遮蔽／ledger 收緊後的撤回依據）；無法判斷回傳 None（不撤回）。
        """
        if self._evidence is None or self._privacy_ledger is None or self._visibility_fn is None:
            return None
        ids = [str(x) for x in unit_ids if x]
        if not ids:
            return False
        try:
            got = {u.id: u for u in self._evidence.get(ids)}
        except Exception:  # noqa: BLE001
            return None
        return all(
            i in got and self._shareable_scope(got[i]) == scope_id for i in ids
        )

    def _shareable_units(self, units: list[EvidenceUnit], scope_id: str) -> list[EvidenceUnit]:
        return [u for u in units if self._shareable_scope(u) == scope_id]

    def candidate_scopes(self, concept_id: str) -> list[str]:
        """概念目前有可分享來源（U_S 非空）的場域 scope id。分享條件不齊備回傳空。"""
        if not self.sharing_ready():
            return []
        with self._lock(), self._semantic._lock:
            concept = self._semantic.get(concept_id)
            if concept is None or concept.status != "active" or concept.metadata.get("merged_into"):
                return []
            snap = collect_snapshot(self._semantic, self._provenance, concept_id)
        if snap is None:
            return []
        return sorted({s for s in (self._shareable_scope(u) for u in snap.units) if s})

    def judge_sharing(self, concept_id: str, scope_id: str) -> SharingJudgement:
        """判定概念能否在 scope_id 分享（不寫入）。fail-closed：任何缺漏／例外都不分享。

        步驟 1 鎖內：快照、算 U_S 與 digest；步驟 2 鎖外：LLM 只看 U_S；
        步驟 3 結構驗證（欄位層級，不看文字內容）。
        """
        base = SharingJudgement(outcome="skipped", concept_id=concept_id, scope_id=scope_id)
        if self._llm_fn is None:
            base.reason = "no_llm"
            return base
        if self._privacy_ledger is None or self._visibility_fn is None:
            base.reason = "no_visibility"
            return base
        if parse_guild_scope(scope_id) is None:
            base.reason = "invalid_scope"
            return base

        with self._lock(), self._semantic._lock:
            concept = self._semantic.get(concept_id)
            if concept is None:
                base.reason = "concept_missing"
                return base
            if concept.status != "active" or concept.metadata.get("merged_into"):
                base.reason = "concept_inactive"
                return base
            snap = collect_snapshot(self._semantic, self._provenance, concept_id)
            review = EvidenceReview.from_dict(concept.evidence_review)
            eff_status = concept.effective_review_status()
        if snap is None:
            base.reason = "concept_missing"
            return base
        base.digest = snap.digest
        if eff_status == "contradicted":
            base.reason = "contradicted"
            return base
        shareable_units = self._shareable_units(snap.units, scope_id)
        if not shareable_units:
            base.reason = "no_shareable_units"
            return base
        prompt_units = select_prompt_units(shareable_units, int(self._cfg.evidence_review_max_units))
        evidence_text, shown = format_evidence_lines(prompt_units)
        if not shown:
            base.reason = "no_renderable_units"
            return base

        prompt = _SHARE_PROMPT.format(
            legend=SOURCE_LEGEND, evidence=evidence_text, max_chars=SHARE_SUMMARY_MAX_CHARS,
        )
        try:
            raw = self._llm_fn(prompt)
        except Exception as e:  # noqa: BLE001
            logger.warning("[EvidenceReviewer] 分享判定 LLM 失敗 %s: %s", concept_id[:12], e)
            return dataclasses.replace(base, outcome="held", reason=f"llm_error: {e}")
        parsed = parse_llm_json(raw)
        if parsed is None:
            return dataclasses.replace(base, outcome="held", reason="llm_invalid_json")
        out = self._decide_sharing(base, review, parsed, shown)
        if out.outcome == "judged" and out.shareable:
            # 只依 U_S 的被引用 unit 重算：不帶入全部來源（含 DM／其他場域）的審核結果
            cited_units = [shown[i] for i in out.cited_unit_ids]
            out.support_strength, _ = self._support_strength_of(cited_units)
            out.review_status = decide_review_status(
                "supported", out.support_strength, out.claim_kind, [], self._cfg,
            )
        return out

    def _support_strength_of(self, units: list[EvidenceUnit]) -> tuple[float, bool]:
        """被引用 unit 的 noisy-OR 學習權重（同一 episode 取最大）；只計使用者陳述。"""
        by_group: dict[str, float] = {}
        for u in units:
            if can_attest_user_claim(u):
                group = u.episode_id or u.id
                by_group[group] = max(by_group.get(group, 0.0), learning_weight(u, self._cfg))
        return noisy_or(by_group.values()), bool(by_group)

    @staticmethod
    def _decide_sharing(
        base: SharingJudgement,
        review: EvidenceReview,
        parsed: dict[str, Any],
        shown: dict[str, EvidenceUnit],
    ) -> SharingJudgement:
        """LLM 分享判定 -> 結構驗證。只看欄位，不看文字內容。"""

        def held(reason: str) -> SharingJudgement:
            return dataclasses.replace(base, outcome="held", reason=reason)

        sensitivity = parsed.get("sensitivity")
        if sensitivity not in SENSITIVITIES:
            return held(f"llm_invalid_sensitivity: {sensitivity!r}")
        claim_kind = parsed.get("claim_kind")
        if claim_kind not in CLAIM_KINDS:
            claim_kind = review.claim_kind if review.claim_kind in CLAIM_KINDS else "fact"
        cited_raw = parsed.get("cited_unit_ids", [])
        restrict_raw = parsed.get("privacy_restrict_unit_ids", [])
        if not isinstance(cited_raw, list) or not isinstance(restrict_raw, list):
            return held("llm_invalid_id_list")
        cited = list(dict.fromkeys(str(x) for x in cited_raw))
        restrict = list(dict.fromkeys(str(x) for x in restrict_raw))
        unknown = [i for i in cited + restrict if i not in shown]
        if unknown:
            return held(f"unknown_evidence_id: {unknown[0]}")
        summary = parsed.get("shareable_summary")
        summary = summary.strip() if isinstance(summary, str) else ""
        if len(summary) > SHARE_SUMMARY_MAX_CHARS:
            return held("summary_too_long")
        supports = parsed.get("supports_summary_from_cited_only") is True

        out = dataclasses.replace(
            base, outcome="judged", sensitivity=sensitivity, claim_kind=claim_kind,
            cited_unit_ids=cited, shareable_summary=summary,
            privacy_restrict_unit_ids=restrict,
        )
        if sensitivity not in SHAREABLE_SENSITIVITIES:
            out.reason = "sensitive"
        elif not cited:
            out.reason = "no_cited_units"
        elif not supports:
            out.reason = "not_supported_by_cited_only"
        elif not summary:
            out.reason = "empty_summary"
        elif set(cited) & set(restrict):
            out.reason = "privacy_restricted"
        else:
            out.shareable = True
        return out

    def scan_privacy_intent(self, units: list[EvidenceUnit]) -> list[str] | None:
        """熱路徑外的隱私意圖掃描（供包 C 的 engine.scan_privacy_intent 使用）。

        只掃來源可見度為 scope_shared 的 user unit；LLM 判斷，程式只驗證 ids ⊆ 送審 id。
        回傳需收緊的 unit id；無 LLM／失敗／輸出無效回傳 None（留待 Dream）。
        """
        if not self.sharing_ready():
            return None
        eligible = [u for u in units if self._shareable_scope(u) is not None]
        if not eligible:
            return []
        evidence_text, shown = format_evidence_lines(eligible)
        if not shown:
            return []
        prompt = _PRIVACY_SCAN_PROMPT.format(legend=SOURCE_LEGEND, evidence=evidence_text)
        try:
            raw = self._llm_fn(prompt)  # type: ignore[misc]
        except Exception as e:  # noqa: BLE001
            logger.warning("[EvidenceReviewer] 隱私意圖掃描失敗: %s", e)
            return None
        parsed = parse_llm_json(raw)
        if parsed is None:
            return None
        ids = parsed.get("privacy_restrict_unit_ids", [])
        if not isinstance(ids, list):
            return None
        ids = list(dict.fromkeys(str(x) for x in ids))
        if any(i not in shown for i in ids):
            return None
        return ids

    # ── 交叉佐證（§5）─────────────────────────────────────────────

    def judge_corroboration(self, claim_text: str, candidates: list[Any]) -> list[str] | None:
        """LLM 判斷哪些候選投影獨立佐證主張。輸入只有摘要；引用以結構驗證 ⊆ 候選。

        candidates：具 projection_id、summary 屬性的物件（ProjectionView）。
        回傳佐證的 projection_id；無 LLM／失敗／引用不在候選回傳 None。
        """
        if self._llm_fn is None or not candidates:
            return None
        allowed = {c.projection_id for c in candidates}
        lines = "\n".join(f"[{c.projection_id}] {c.summary}" for c in candidates)
        prompt = _CORROBORATION_PROMPT.format(claim=claim_text, candidates=lines)
        try:
            raw = self._llm_fn(prompt)
        except Exception as e:  # noqa: BLE001
            logger.warning("[EvidenceReviewer] 佐證判斷失敗: %s", e)
            return None
        parsed = parse_llm_json(raw)
        if parsed is None:
            return None
        ids = parsed.get("corroborating_projection_ids", [])
        if not isinstance(ids, list):
            return None
        ids = list(dict.fromkeys(str(x) for x in ids))
        if any(i not in allowed for i in ids):
            return None
        return ids

    def apply_corroboration(
        self, concept_id: str, scope_id: str, corroborators: list[Any],
    ) -> ReviewOutcome:
        """把共享層投影佐證寫成 ``sp:`` 參考（經 Gate 的 review patch）。

        佐證強度＝各擁有者 support_strength 的 noisy-OR（同擁有者取最大，不重複加分）；
        未達 SUPPORT_THRESHOLD 不寫。只處理 claim_kind=intention 且審核仍有效的概念。
        不複製任何人的文字進本 namespace，只存參考字串。
        """
        from echo_memory.schemas.evidence_review import make_sp_ref

        if not corroborators:
            return ReviewOutcome("skipped", "no_corroborators", concept_id)
        if not hasattr(self._gate, "process_review_patch"):
            return ReviewOutcome("skipped", "no_gate", concept_id)
        by_owner: dict[str, float] = {}
        for v in corroborators:
            by_owner[v.owner_uid] = max(by_owner.get(v.owner_uid, 0.0), float(v.support_strength))
        if noisy_or(by_owner.values()) < float(self._cfg.support_threshold):
            return ReviewOutcome("skipped", "corroboration_below_threshold", concept_id)
        refs = [make_sp_ref(scope_id, v.projection_id, v.source_digest) for v in corroborators]

        with self._lock(), self._semantic._lock:
            concept = self._semantic.get(concept_id)
            if concept is None:
                return ReviewOutcome("skipped", "concept_missing", concept_id)
            review = EvidenceReview.from_dict(concept.evidence_review)
            if concept.effective_review_status() not in ("supported", "insufficient"):
                return ReviewOutcome("skipped", "no_valid_review", concept_id)
            if review.claim_kind != "intention":
                return ReviewOutcome("skipped", "not_intention", concept_id)
            merged = list(review.corroborated_by)
            merged.extend(r for r in refs if r not in merged)
            if merged == review.corroborated_by:
                return ReviewOutcome("skipped", "already_corroborated", concept_id)
            new = dataclasses.replace(
                review,
                corroborated_by=merged,
                status=decide_review_status(
                    "supported", review.support_strength, "intention", merged, self._cfg,
                ),
                reviewed_at=datetime.now(timezone.utc),
                reviewer="llm",
                reason=f"{review.reason} [shared_corroboration]".strip(),
            )
            ok, tx = self._gate.process_review_patch(
                concept_id, new, user_input=f"[evidence_review] corroboration {concept.name}",
                triggered_by=self._triggered_by,
            )
        patch = tx.patches[0] if tx.patches else None
        patch_id = patch.patch_id if patch is not None else ""
        if ok:
            return ReviewOutcome("reviewed", "shared_corroboration", concept_id, new.status, patch_id)
        reason = (patch.rejection_reason if patch is not None else "") or "gate_rejected"
        return ReviewOutcome("skipped", f"gate_rejected: {reason}", concept_id, patch_id=patch_id)

    def revalidate_corroboration(
        self, concept_id: str, is_valid: Callable[[str], bool],
    ) -> ReviewOutcome:
        """移除已失效的 ``sp:`` 佐證參考（投影被撤回／擁有者退出／digest 改變），重算審核狀態。

        失效的佐證不需通知被佐證者；intention 失去全部佐證即回落 insufficient。
        """
        from echo_memory.schemas.evidence_review import SP_PREFIX

        if not hasattr(self._gate, "process_review_patch"):
            return ReviewOutcome("skipped", "no_gate", concept_id)
        with self._lock(), self._semantic._lock:
            concept = self._semantic.get(concept_id)
            if concept is None:
                return ReviewOutcome("skipped", "concept_missing", concept_id)
            review = EvidenceReview.from_dict(concept.evidence_review)
            bad = []
            for ref in review.corroborated_by:
                if not ref.startswith(SP_PREFIX):
                    continue
                try:
                    valid = bool(is_valid(ref))
                except Exception:  # noqa: BLE001 - 無法驗證 -> 視為失效
                    valid = False
                if not valid:
                    bad.append(ref)
            if not bad:
                return ReviewOutcome("skipped", "all_valid", concept_id)
            if concept.effective_review_status() not in REVIEWED_STATUSES:
                return ReviewOutcome("skipped", "review_not_effective", concept_id)
            remaining = [r for r in review.corroborated_by if r not in bad]
            status = review.status
            if status == "supported":
                status = decide_review_status(
                    "supported", review.support_strength, review.claim_kind, remaining, self._cfg,
                )
            new = dataclasses.replace(
                review,
                corroborated_by=remaining,
                status=status,
                reviewed_at=datetime.now(timezone.utc),
                reason=f"{review.reason} [corroboration_invalidated]".strip(),
            )
            ok, tx = self._gate.process_review_patch(
                concept_id, new, user_input=f"[evidence_review] revalidate {concept.name}",
                triggered_by=self._triggered_by,
            )
        patch = tx.patches[0] if tx.patches else None
        patch_id = patch.patch_id if patch is not None else ""
        if ok:
            return ReviewOutcome("reviewed", "corroboration_invalidated", concept_id, new.status, patch_id)
        reason = (patch.rejection_reason if patch is not None else "") or "gate_rejected"
        return ReviewOutcome("skipped", f"gate_rejected: {reason}", concept_id, patch_id=patch_id)

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 待審登記簿
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _load_pending(self) -> dict[str, dict[str, Any]]:
        try:
            if not self._pending_path.exists():
                return {}
            data = json.loads(self._pending_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception as e:  # noqa: BLE001 - 壞檔視為空，不拖垮審核
            logger.warning("[EvidenceReviewer] 讀取待審登記簿失敗，視為空: %s", e)
            return {}

    def _save_pending(self, pending: dict[str, dict[str, Any]]) -> None:
        try:
            self._pending_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_json(self._pending_path, pending)
        except Exception as e:  # noqa: BLE001
            logger.warning("[EvidenceReviewer] 寫入待審登記簿失敗: %s", e)

    def _drop_pending(self, concept_id: str) -> None:
        with self._lock():
            pending = self._load_pending()
            if pending.pop(concept_id, None) is not None:
                self._save_pending(pending)
