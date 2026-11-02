"""
ContradictionDetector - 自動矛盾偵測模組

對應大腦分區：前額葉皮質（Prefrontal Cortex）- 知識一致性監控

職責：
  - 偵測新概念與既有概念間的語義矛盾
  - 兩階段策略：向量相似度篩選 → LLM 判定關係（relation）
  - 不同 subject 的概念對不送判（結構欄位過濾）
  - 關係分派：
      contradicts     -> mark_conflict + CONTRADICTS 邊（真矛盾）
      supersedes      -> 舊者 deprecate + replaced_by（經 Gate），不建邊、不加 conflict_count
      different_scope -> 不建邊，ConflictLedger 記 resolved 去重
      compatible / unrelated -> 不處理

架構禁令（CLAUDE.md）：relation、newer、scope 全由 LLM 結構化輸出；
程式只做欄位層級的結構護欄（subject、時間先後、user_claim 證據），
不以 keyword / regex 判斷內容。
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import numpy as np

from echo_memory.affect.evidence_reviewer import (
    EvidenceSnapshot,
    aware,
    collect_snapshot,
    format_evidence_lines,
    select_prompt_units,
)
from echo_memory.dream.staleness import Guard, resolve_guard
from echo_memory.schemas.concept import Concept
from echo_memory.schemas.evidence import can_attest_user_claim
from echo_memory.storage.conflict_ledger import compute_pair_digest
from echo_memory.storage.graph import MemoryGraph
from echo_memory.storage.semantic import SemanticMemory

logger = logging.getLogger("echo_memory.dream.contradiction")

# LLM 回傳的 relation 合法值
RELATIONS = ("contradicts", "supersedes", "different_scope", "compatible", "unrelated")
# 每個概念送進矛盾判斷 prompt 的證據行數上限
_PROMPT_EVIDENCE_UNITS = 4

# LLM 矛盾判斷 Prompt 模板
_CONTRADICTION_PROMPT = """\
你是知識一致性檢驗員。判斷以下兩個概念的關係。只依概念描述與證據原文判斷，不得引入外部知識。

概念 A（主體 {subject_a}；來源類別 {source_a}；範圍 {scope_a}）: {name_a} — {desc_a}
證據 A（含時間）:
{evidence_a}

概念 B（主體 {subject_b}；來源類別 {source_b}；範圍 {scope_b}）: {name_b} — {desc_b}
證據 B（含時間）:
{evidence_b}

relation 的類型：
- contradicts：同一主體、同一情境與時間範圍下互斥，且看不出誰先誰後（兩者同時被主張）。
- supersedes：兩者互斥，但使用者隨時間更正了偏好或狀況，較新的取代較舊的；newer 填 "a" 或 "b"。
- different_scope：表面互斥，實際是不同主體、不同情境或不同時間範圍，可以並存。
- compatible：不互斥，可同時成立。
- unrelated：彼此無關。

輸出純 JSON:
{{"relation": "contradicts|supersedes|different_scope|compatible|unrelated", \
"newer": "a|b|null", "scope_note": "...", "strength": 0.0-1.0, "reason": "..."}}"""


@dataclass
class ContradictionResult:
    """單次矛盾偵測的結果。

    relation 缺省時由 is_contradiction 推導（相容舊呼叫與舊格式 LLM 回應）：
    True -> contradicts，False -> compatible。給定 relation 時 is_contradiction 與它同步
    （僅 contradicts 為 True）。action 記錄 Dream 實際做了什麼。
    """

    concept_a_id: str
    concept_b_id: str
    is_contradiction: bool
    strength: float = 0.0
    reason: str = ""
    relation: str = ""
    newer_id: str | None = None  # 僅 supersedes：LLM 指出較新的一方
    scope_note: str = ""
    action: str = ""  # edge_created｜superseded｜scope_recorded｜supersede_held｜supersede_rejected｜skipped｜...
    transaction: Any = None  # superseded 時的 PatchTransaction（rollback_supersede 用）
    llm_failed: bool = False  # LLM 例外或回傳無法解析：結果是預設值，不代表真的判過

    def __post_init__(self) -> None:
        if not self.relation:
            self.relation = "contradicts" if self.is_contradiction else "compatible"
        else:
            self.is_contradiction = self.relation == "contradicts"


class ContradictionDetector:
    """
    自動矛盾偵測器。

    採用兩階段策略：
      1. 向量高相似度篩選：cosine similarity >= threshold → 候選（同主題但可能矛盾）
      2. 若有 llm_fn → LLM 確認矛盾；若無 → 降級跳過（不建邊）

    降級策略說明：
      純向量相似度無法區分「相似（相同主張）」和「矛盾（對立主張）」，
      因此在無 LLM 的情況下，為避免誤報，選擇跳過而非猜測。
    """

    def __init__(
        self,
        semantic: SemanticMemory,
        graph: MemoryGraph,
        similarity_threshold: float = 0.82,
        llm_fn: Callable[[str], str] | None = None,
        conflict_ledger: Any | None = None,
        guard: Guard | None = None,
        patch_coordinator: Any | None = None,
        provenance: Any | None = None,
    ):
        """
        初始化矛盾偵測器。

        Args:
            semantic: SemanticMemory 實例，提供概念向量
            graph: MemoryGraph 實例，用於建立 CONTRADICTS 邊
            similarity_threshold: 向量相似度門檻，高於此值才進入 LLM 確認階段
            llm_fn: LLM 呼叫函式 Callable[[prompt_str], response_str]；
                    為 None 時降級跳過矛盾偵測
            conflict_ledger: ConflictLedger 實例（可選）。
                    不為 None 時，每次建立 CONTRADICTS 邊會同步登記帳本。
            guard: 鎖 context manager 工廠（通常為 storage_lock）。
                    候選篩選與邊登記在短臨界區，LLM 確認在鎖外；None 時不加鎖。
            patch_coordinator: DreamPatchCoordinator 實例（可選）。概念的 conflict_count／
                has_contradiction 標記一律經它提交 mark_conflict patch（Gate audit、可 rollback）；
                None 時以 semantic 建立無 LLM 的降級 Gate。
            provenance: ProvenanceResolver（可選）。提供時矛盾判斷 prompt 帶證據原文與時間，
                supersedes 的結構護欄（較新者有 user_claim 證據、時間較晚）才有依據；
                None 時 supersedes 一律因「無時間」降為 contradicts。
        """
        self._guard = resolve_guard(guard)
        self.semantic = semantic
        self.graph = graph
        self.similarity_threshold = similarity_threshold
        self.llm_fn = llm_fn
        self._conflict_ledger = conflict_ledger
        self._patch_coordinator = patch_coordinator
        self._provenance = provenance
        # 最近一次 detect_and_register／reevaluate_pair 每個候選的處置結果（含 relation／action）
        self.last_results: list[ContradictionResult] = []
        # M6：事件發射器（由 DreamEngine 注入；None＝不發）
        self.event_emitter: Any | None = None

        logger.info(
            f"[ContradictionDetector] 初始化完成 - "
            f"相似度門檻: {similarity_threshold}, "
            f"LLM: {'啟用' if llm_fn else '停用（降級模式）'}"
        )

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 公共 API
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def detect_and_register(
        self,
        concept: Concept,
        candidate_pool: list[Concept] | None = None,
    ) -> list[tuple[str, str, float]]:
        """
        偵測 concept 與其他概念的關係，並依 relation 分派。

        策略（兩階段）：
          1. 向量高相似度篩選：cosine similarity >= threshold → 候選；
             不同 subject、或已有同內容 resolved 判決的配對不送判
          2. 若有 llm_fn → LLM 判 relation；若無 → 不處理（降級跳過）

        每個候選的 relation／處置結果放在 self.last_results。

        Args:
            concept: 待偵測的新概念
            candidate_pool: 候選概念池；為 None 時使用 SemanticMemory 全部概念

        Returns:
            list of (concept_a_id, concept_b_id, weight) — 已建立的矛盾邊清單
            （supersedes／different_scope 不建邊，不在此清單內）
        """
        self.last_results = []
        # 無 LLM 時降級跳過（純向量無法判斷矛盾）
        if self.llm_fn is None:
            logger.debug(
                "[ContradictionDetector] 無 LLM，跳過矛盾偵測（降級模式）"
            )
            return []

        # 已合併 / 非 active 的概念不參與偵測
        if not self._is_active(concept):
            return []

        # 概念本身沒有向量時無法比較
        if not concept.embedding:
            logger.debug(
                f"[ContradictionDetector] 概念 {concept.id} 無向量，跳過偵測"
            )
            return []

        # 臨界區 1（短）：取候選池、篩選候選並記錄版本快照、證據視圖（不含 LLM）
        views: dict[str, EvidenceSnapshot] = {}
        with self._guard():
            pool = candidate_pool if candidate_pool is not None else self._get_all_concepts()

            # 篩選候選（高相似度、同 subject、排除自身、排除已有矛盾邊、排除已判決配對）
            candidates = self._filter_candidates(concept, pool)
            # 只記錄此刻確實存在於 SemanticMemory 的概念（未入庫者無從判斷過期）
            snapshot = {}
            for c in [concept, *candidates]:
                stored = self.semantic.get(c.id)
                if stored is not None:
                    snapshot[c.id] = stored.description
            if candidates:
                for c in [concept, *candidates]:
                    view = self._view_for(c.id)
                    if view is not None:
                        views[c.id] = view

        if not candidates:
            logger.debug(
                f"[ContradictionDetector] 概念 {concept.name} 無高相似度候選，跳過"
            )
            return []

        logger.debug(
            f"[ContradictionDetector] 概念 {concept.name} 找到 {len(candidates)} 個候選"
        )

        # 鎖外：對每個候選呼叫 LLM 判 relation（慢），supersedes 另過 Gate 的 LLM 佐證
        confirmed: list[ContradictionResult] = []
        superseding: list[tuple[ContradictionResult, Any, dict[str, Any]]] = []
        scoped: list[ContradictionResult] = []
        for candidate in candidates:
            result = self._check_contradiction_with_llm(concept, candidate, views)
            self.last_results.append(result)
            if result.relation == "contradicts":
                confirmed.append(result)
            elif result.relation == "different_scope":
                scoped.append(result)
            elif result.relation == "supersedes":
                routed = self._route_supersede(result, views)
                if routed is None:
                    # 護欄不過 -> 降為 contradicts（無時間者只能走 contradicts）
                    confirmed.append(result)
                elif routed[0] is not None:
                    superseding.append((result, routed[0], routed[1]))

        # 臨界區 2（短）：比對快照後套用；概念已刪除 / 已改動的結果丟棄
        created_edges: list[tuple[str, str, float]] = []
        with self._guard():
            for result in confirmed:
                if not self._pair_still_valid(result, snapshot):
                    result.action = "skipped_stale"
                    continue
                if not self._register_edge(result):
                    result.action = "edge_rejected"
                    continue
                result.action = "edge_created"
                created_edges.append(
                    (result.concept_a_id, result.concept_b_id, result.strength)
                )
            for result, proposal, _basis in superseding:
                if not self._pair_still_valid(result, snapshot, check_edge=False):
                    result.action = "skipped_stale"
                    continue
                self._apply_supersede(result, proposal)
            for result in scoped:
                if not self._pair_still_valid(result, snapshot, check_edge=False):
                    result.action = "skipped_stale"
                    continue
                self._record_scope(result)

        logger.info(
            f"[ContradictionDetector] 概念 {concept.name} "
            f"偵測完成，建立 {len(created_edges)} 條矛盾邊"
        )

        return created_edges

    def reevaluate_batch(self, limit: int | None = None) -> list[ContradictionResult]:
        """Dream 用：對既有 CONTRADICTS 邊的配對批次重判（每對 LLM 在鎖外）。

        以 ledger 的 reevaluated_digest 去重：兩概念證據基礎不變就不重判；
        重判後未被取代的配對也會記下 digest，避免每個週期重複送審。
        單次上限 limit（None 取 config contradiction_reevaluate_batch）。
        """
        if self.llm_fn is None:
            return []
        if limit is None:
            from echo_memory.config import get_config
            limit = int(get_config().contradiction_reevaluate_batch)
        if limit <= 0:
            return []
        pairs: list[tuple[str, str]] = []
        with self._guard():
            with self.graph.lock:
                seen: set[frozenset[str]] = set()
                for src, dst, data in self.graph.graph.edges(data=True):
                    key = frozenset((src, dst))
                    if data.get("edge_type") != "CONTRADICTS" or key in seen:
                        continue
                    seen.add(key)
                    pairs.append((src, dst))
            todo: list[tuple[str, str]] = []
            for a_id, b_id in pairs:
                a, b = self.semantic.get(a_id), self.semantic.get(b_id)
                if a is None or b is None or not self._is_active(a) or not self._is_active(b):
                    continue
                if (
                    self._conflict_ledger is not None
                    and self._conflict_ledger.is_pair_reevaluated(
                        a_id, b_id, self._pair_digest(a, b),
                    )
                ):
                    continue
                todo.append((a_id, b_id))
                if len(todo) >= limit:
                    break
        results: list[ContradictionResult] = []
        for a_id, b_id in todo:
            result = self.reevaluate_pair(a_id, b_id)
            if result is None:
                continue
            results.append(result)
            # LLM 失敗（預設值）或取代被 held／執行失敗：不代表真的判過，不去重，下個週期再來
            judged = not result.llm_failed and result.action not in (
                "superseded", "supersede_held", "supersede_failed", "skipped_stale",
            )
            if judged and self._conflict_ledger is not None:
                with self._guard():
                    a, b = self.semantic.get(a_id), self.semantic.get(b_id)
                    if a is not None and b is not None:
                        self._conflict_ledger.mark_reevaluated(a_id, b_id, self._pair_digest(a, b))
        self.last_results = results
        return results

    def reevaluate_pair(self, concept_a_id: str, concept_b_id: str) -> ContradictionResult | None:
        """重新判斷「已有 CONTRADICTS 邊」的配對（例如後來出現了時間先後的證據）。

        若 LLM 判 supersedes 且通過護欄與 Gate：舊者 deprecate + replaced_by，
        扣回雙方先前的互相標記並移除 CONTRADICTS 邊（可 rollback_supersede 還原）。
        其他 relation 不動既有邊。LLM 在鎖外。

        Returns:
            判定結果（含 action）；無 LLM、概念不存在或非 active 回傳 None
        """
        if self.llm_fn is None:
            return None
        views: dict[str, EvidenceSnapshot] = {}
        with self._guard():
            a = self.semantic.get(concept_a_id)
            b = self.semantic.get(concept_b_id)
            if a is None or b is None or not self._is_active(a) or not self._is_active(b):
                return None
            if a.subject != b.subject:
                return None
            snapshot = {a.id: a.description, b.id: b.description}
            for cid in (a.id, b.id):
                view = self._view_for(cid)
                if view is not None:
                    views[cid] = view
        result = self._check_contradiction_with_llm(a, b, views)
        self.last_results = [result]
        if result.relation != "supersedes":
            result.action = "unchanged"
            return result
        routed = self._route_supersede(result, views)
        if routed is None or routed[0] is None:
            return result
        with self._guard():
            if not self._pair_still_valid(result, snapshot, check_edge=False):
                result.action = "skipped_stale"
                return result
            self._apply_supersede(result, routed[0])
        return result

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 內部方法
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _pair_still_valid(
        self, result: ContradictionResult, snapshot: dict[str, str],
        check_edge: bool = True,
    ) -> bool:
        """鎖內檢查：兩端概念仍存在、描述未變、且（check_edge 時）尚未被登記矛盾邊。"""
        for cid in (result.concept_a_id, result.concept_b_id):
            if cid not in snapshot:
                continue
            current = self.semantic.get(cid)
            if current is None or current.description != snapshot[cid]:
                logger.info(
                    "[ContradictionDetector] 概念 %s 在 LLM 期間已刪除或變動，丟棄矛盾結果",
                    cid[:8],
                )
                return False
        if check_edge and self.graph.has_contradicts_edge(
            concept_a=result.concept_a_id, concept_b=result.concept_b_id,
        ):
            return False
        return True

    def _get_all_concepts(self) -> list[Concept]:
        """從 SemanticMemory 取得全部概念"""
        return [c for c in self.semantic._concepts.values() if self._is_active(c)]

    @staticmethod
    def _is_active(concept: Concept) -> bool:
        """active 且未被合併（merged_into）的概念才參與矛盾偵測"""
        return concept.status == "active" and not concept.metadata.get("merged_into")

    def _cosine_similarity(self, vec_a: list[float], vec_b: list[float]) -> float:
        """
        計算兩向量的餘弦相似度。

        Args:
            vec_a: 向量 A
            vec_b: 向量 B

        Returns:
            相似度分數 [-1.0, 1.0]
        """
        a = np.array(vec_a, dtype=np.float32)
        b = np.array(vec_b, dtype=np.float32)
        norm_a = np.linalg.norm(a)
        norm_b = np.linalg.norm(b)
        if norm_a == 0.0 or norm_b == 0.0:
            return 0.0
        return float(np.dot(a, b) / (norm_a * norm_b))

    def _filter_candidates(
        self, concept: Concept, pool: list[Concept]
    ) -> list[Concept]:
        """
        篩選高相似度候選概念（排除自身、排除已有矛盾邊）。

        Args:
            concept: 目標概念
            pool: 全部候選池

        Returns:
            符合條件的候選概念清單
        """
        candidates: list[Concept] = []

        for other in pool:
            # 排除自身
            if other.id == concept.id:
                continue

            # 排除已合併 / 非 active 的候選（candidate_pool 可由外部傳入）
            if not self._is_active(other):
                continue

            # 排除無向量的候選
            if not other.embedding:
                continue

            # 不同 subject（user／agent／world）的概念對不送判（結構欄位過濾，省 LLM 成本）
            if other.subject != concept.subject:
                continue

            # 排除已有 CONTRADICTS 邊的配對
            if self.graph.has_contradicts_edge(concept_a=concept.id, concept_b=other.id):
                logger.debug(
                    f"[ContradictionDetector] 已存在矛盾邊，跳過: "
                    f"{concept.id[:8]} ↔ {other.id[:8]}"
                )
                continue

            # 計算相似度
            sim = self._cosine_similarity(concept.embedding, other.embedding)
            if sim >= self.similarity_threshold:
                # 同內容的配對已有 resolved 判決（different_scope／supersedes）-> 不重複送審
                if self._pair_already_resolved(concept, other):
                    logger.debug(
                        f"[ContradictionDetector] 配對已判決，跳過: "
                        f"{concept.id[:8]} ↔ {other.id[:8]}"
                    )
                    continue
                candidates.append(other)
                logger.debug(
                    f"[ContradictionDetector] 候選: {other.name} (sim={sim:.4f})"
                )

        return candidates

    def _check_contradiction_with_llm(
        self,
        concept_a: Concept,
        concept_b: Concept,
        views: dict[str, EvidenceSnapshot] | None = None,
    ) -> ContradictionResult:
        """
        呼叫 LLM 判斷兩個概念的關係（relation）。

        舊格式回應（只有 is_contradiction）仍可解析：True -> contradicts，False -> compatible。

        Args:
            concept_a: 概念 A
            concept_b: 概念 B
            views: 概念 id -> 證據視圖（可選）；提供時 prompt 帶證據原文與時間

        Returns:
            ContradictionResult 結果物件
        """
        views = views or {}
        prompt = _CONTRADICTION_PROMPT.format(
            name_a=concept_a.name,
            desc_a=concept_a.description,
            subject_a=concept_a.subject,
            source_a=concept_a.metadata.get("support_source", "未知"),
            scope_a=self._scope_text(concept_a),
            evidence_a=self._evidence_text(views.get(concept_a.id)),
            name_b=concept_b.name,
            desc_b=concept_b.description,
            subject_b=concept_b.subject,
            source_b=concept_b.metadata.get("support_source", "未知"),
            scope_b=self._scope_text(concept_b),
            evidence_b=self._evidence_text(views.get(concept_b.id)),
        )

        default_result = ContradictionResult(
            concept_a_id=concept_a.id,
            concept_b_id=concept_b.id,
            is_contradiction=False,
        )

        try:
            response = self.llm_fn(prompt)  # type: ignore[misc]
            parsed = self._parse_llm_response(response)
            if not parsed:
                default_result.llm_failed = True
                return default_result
            relation = parsed.get("relation")
            newer_id = None
            if relation in RELATIONS:
                if relation == "supersedes":
                    newer_id = {"a": concept_a.id, "b": concept_b.id}.get(
                        str(parsed.get("newer") or "").lower()
                    )
                    if newer_id is None:
                        # 沒指出誰較新：無法取代，保守降為 contradicts
                        relation = "contradicts"
            else:
                relation = "contradicts" if parsed.get("is_contradiction", False) else "compatible"
            return ContradictionResult(
                concept_a_id=concept_a.id,
                concept_b_id=concept_b.id,
                is_contradiction=relation == "contradicts",
                strength=float(parsed.get("strength", 0.0)),
                reason=parsed.get("reason", ""),
                relation=relation,
                newer_id=newer_id,
                scope_note=str(parsed.get("scope_note") or ""),
            )
        except Exception as e:
            logger.warning(
                f"[ContradictionDetector] LLM 呼叫失敗，跳過: {e}"
            )
            default_result.llm_failed = True
            return default_result

    @staticmethod
    def _scope_text(concept: Concept) -> str:
        """概念的審核範圍標籤（只餵回 LLM，不做字串比對）。"""
        review = concept.evidence_review if isinstance(concept.evidence_review, dict) else {}
        scope = review.get("scope")
        if not isinstance(scope, dict) or not scope:
            return "未標註"
        return json.dumps(scope, ensure_ascii=False)

    @staticmethod
    def _evidence_text(view: EvidenceSnapshot | None) -> str:
        if view is None or not view.units:
            return "（無證據資料）"
        text, _shown = format_evidence_lines(
            select_prompt_units(view.units, _PROMPT_EVIDENCE_UNITS)
        )
        return text or "（無證據資料）"

    def _view_for(self, concept_id: str) -> EvidenceSnapshot | None:
        """讀概念的證據視圖（須在鎖內呼叫）；沒有 provenance 或解析失敗回傳 None。"""
        if self._provenance is None:
            return None
        try:
            return collect_snapshot(self.semantic, self._provenance, concept_id)
        except Exception as e:
            logger.warning("[ContradictionDetector] 讀取證據視圖失敗 %s: %s", concept_id[:8], e)
            return None

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # supersedes / different_scope
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    @staticmethod
    def check_supersede_guard(
        newer: EvidenceSnapshot | None,
        older: EvidenceSnapshot | None,
    ) -> tuple[bool, str, dict[str, Any]]:
        """supersedes 的結構護欄（只看欄位）。

        - 較新者必須有 user_claim 證據（助手推測不能取代使用者陳述）；
        - 較舊者必須有時間，且較新者最早的使用者陳述晚於較舊者最後的證據；
          無時間資訊者不得 supersede。
        """
        if newer is None or older is None:
            return False, "no_evidence_view", {}
        claims = [u for u in newer.units if can_attest_user_claim(u)]
        if not claims:
            return False, "newer_has_no_user_claim", {}
        older_times = [aware(u.occurred_at) for u in older.units if u.occurred_at]
        claim_times = [aware(u.occurred_at) for u in claims if u.occurred_at]
        if not older_times or not claim_times:
            return False, "no_time_information", {}
        if min(claim_times) <= max(older_times):
            return False, "newer_not_later", {}
        return True, "", {
            "older_evidence_ids": [u.id for u in older.units],
            "newer_evidence_ids": [u.id for u in claims],
        }

    def _route_supersede(
        self,
        result: ContradictionResult,
        views: dict[str, EvidenceSnapshot],
    ) -> tuple[Any, dict[str, Any]] | None:
        """supersedes 的鎖外處理：結構護欄 -> Gate 佐證（含 LLM）。

        Returns:
            None：護欄不過（呼叫端降為 contradicts，reason 已註記）；
            (proposal, basis)：Gate 通過，待鎖內 commit；
            (None, {})：Gate held／rejected（result.action 已註記，不建邊也不取代）。
        """
        newer_id = result.newer_id
        older_id = (
            result.concept_b_id if newer_id == result.concept_a_id else result.concept_a_id
        )
        ok, why, basis = self.check_supersede_guard(views.get(newer_id), views.get(older_id))
        if not ok:
            result.relation = "contradicts"
            result.is_contradiction = True
            result.newer_id = None
            result.reason = f"{result.reason} [supersede_guard: {why}]".strip()
            return None
        coordinator = self._get_patch_coordinator()
        state, proposal = coordinator.prepare_supersede(
            older_id, newer_id, reason=result.reason, basis=basis, graph=self.graph,
        )
        if state == "approved" and proposal is not None:
            return proposal, basis
        result.action = "supersede_held" if state == "held" else "supersede_rejected"
        logger.info(
            "[ContradictionDetector] 取代未套用（%s）: %s <- %s",
            state, older_id[:8], newer_id[:8],
        )
        return None, {}

    def _apply_supersede(self, result: ContradictionResult, proposal: Any) -> bool:
        """鎖內：套用取代（deprecate + replaced_by），清既有互相標記與邊，登記 ledger。"""
        coordinator = self._get_patch_coordinator()
        had_edge = self.graph.has_contradicts_edge(
            concept_a=result.concept_a_id, concept_b=result.concept_b_id,
        )
        newer_id = proposal.newer_id
        ok, tx = coordinator.commit_supersede(
            proposal, clear_conflict_marks=had_edge, graph=self.graph,
            verify_fn=lambda: self._verify_newer_claims(newer_id),
        )
        if not ok:
            result.action = "supersede_failed"
            return False
        result.action = "superseded"
        result.transaction = tx
        self._record_resolved(result, "temporal_evolution", patch_id=getattr(tx, "transaction_id", None))
        coordinator.audit_contradiction_detection(
            proposal.newer_id, 0, relation="supersedes",
            peer_id=proposal.older_id, reason=result.reason,
        )
        logger.info(
            "[ContradictionDetector] 取代: %s 被 %s 取代",
            proposal.older_id[:8], proposal.newer_id[:8],
        )
        return True

    def _verify_newer_claims(self, newer_id: str) -> tuple[bool, str]:
        """鎖內：較新者仍有可作證的使用者陳述（供 process_supersede_patch 的 verify_fn）。"""
        view = self._view_for(newer_id)
        if view is None or not any(can_attest_user_claim(u) for u in view.units):
            return False, "newer_lost_user_claim"
        return True, ""

    def rollback_supersede(self, result: ContradictionResult) -> list[str]:
        """完整回滾一次 supersede：舊者回 active、補回互相標記與 CONTRADICTS 邊（原 weight），
        ledger 判決還原（先前是未解矛盾就還原成未解，否則移除該筆）。

        Returns:
            被補回標記的概念 id
        """
        if result.transaction is None:
            return []
        coordinator = self._get_patch_coordinator()
        with self._guard():
            cleared = coordinator.rollback_supersede(result.transaction, graph=self.graph)
            if self._conflict_ledger is not None:
                self._conflict_ledger.unresolve(result.concept_a_id, result.concept_b_id)
        result.action = "supersede_rolled_back"
        return cleared

    def _record_scope(self, result: ContradictionResult) -> None:
        """different_scope：不建邊、不標矛盾；ledger 記 resolved 去重，audit 留痕。"""
        result.action = "scope_recorded"
        self._record_resolved(result, "accepted_dual_truth")
        coordinator = self._get_patch_coordinator()
        coordinator.audit_contradiction_detection(
            result.concept_a_id, 0, relation="different_scope",
            peer_id=result.concept_b_id, reason=result.scope_note or result.reason,
        )

    @staticmethod
    def _pair_digest(concept_a: Concept, concept_b: Concept) -> str:
        return compute_pair_digest(concept_a, concept_b)

    def _pair_already_resolved(self, concept_a: Concept, concept_b: Concept) -> bool:
        if self._conflict_ledger is None:
            return False
        return self._conflict_ledger.is_pair_resolved(
            concept_a.id, concept_b.id, self._pair_digest(concept_a, concept_b),
        )

    def _record_resolved(
        self, result: ContradictionResult, status: str, patch_id: str | None = None,
    ) -> None:
        if self._conflict_ledger is None:
            return
        a = self.semantic.get(result.concept_a_id)
        b = self.semantic.get(result.concept_b_id)
        if a is None or b is None:
            return
        self._conflict_ledger.record_resolved(
            result.concept_a_id, result.concept_b_id,
            status=status,  # type: ignore[arg-type]
            reason=result.scope_note or result.reason,
            pair_digest=self._pair_digest(a, b),
            relation=result.relation,
            concept_a_name=a.name,
            concept_b_name=b.name,
            patch_id=patch_id,
        )

    def _parse_llm_response(self, response: str) -> dict:
        """
        解析 LLM 回應的 JSON，容錯處理 markdown fences 等格式。

        Args:
            response: LLM 原始回應字串

        Returns:
            解析後的 dict，解析失敗時回傳空 dict
        """
        # 移除 markdown fences
        cleaned = re.sub(r"```(?:json)?\s*", "", response).strip()
        cleaned = re.sub(r"```\s*$", "", cleaned).strip()

        # 嘗試解析 JSON
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            # 嘗試找出第一個 JSON 物件
            match = re.search(r"\{.*?\}", cleaned, re.DOTALL)
            if match:
                try:
                    return json.loads(match.group(0))
                except json.JSONDecodeError:
                    pass

        logger.warning(
            f"[ContradictionDetector] 無法解析 LLM 回應: {response[:100]!r}"
        )
        return {}

    def _get_patch_coordinator(self) -> Any:
        """取得 DreamPatchCoordinator；未注入時以 semantic 建立降級 Gate（延遲 import）。"""
        if self._patch_coordinator is None:
            from echo_memory.dream.patch_coordinator import DreamPatchCoordinator
            self._patch_coordinator = DreamPatchCoordinator(semantic=self.semantic)
        elif hasattr(self._patch_coordinator, "ensure_gate"):
            self._patch_coordinator.ensure_gate(self.semantic)
        return self._patch_coordinator

    def _register_edge(self, result: ContradictionResult) -> bool:
        """
        經 Patch Gate 標記 Concept Health + 在圖中建立 CONTRADICTS 邊 + 登記 Conflict Ledger。

        順序：mark_conflict patch（audit-before-mutate）→ 建邊；邊沒建成就回滾標記。
        圖邊不是概念欄位，不走 ConceptPatch；寫圖只用 MemoryGraph 的加鎖公開方法。

        Args:
            result: ContradictionResult 結果物件

        Returns:
            True 表示標記與邊都已建立；False 表示被跳過或 Gate 拒絕（沒有任何殘留變更）
        """
        # 任一端已合併 / 非 active 則不建邊（避免 add_edge 自動生出裸節點）
        for cid in (result.concept_a_id, result.concept_b_id):
            stored = self.semantic.get(cid)
            if stored is not None and not self._is_active(stored):
                logger.debug(f"[ContradictionDetector] 概念 {cid[:8]} 非 active，跳過建邊")
                return False

        # 確保節點存在（若尚未加入圖中則先添加佔位節點）
        self._ensure_concept_node(result.concept_a_id)
        self._ensure_concept_node(result.concept_b_id)

        # 先過 Gate：更新兩邊概念的 conflict_count 和 has_contradiction 標記
        coordinator = self._get_patch_coordinator()
        ok, tx = coordinator.submit_conflict_mark(
            result.concept_a_id, result.concept_b_id,
            strength=result.strength, reason=result.reason,
        )
        if not ok:
            logger.warning(
                "[ContradictionDetector] 矛盾標記被 Gate 拒絕或失敗，不建邊: %s ↔ %s",
                result.concept_a_id[:8], result.concept_b_id[:8],
            )
            return False

        try:
            added = self.graph.add_contradicts_edge(
                concept_a=result.concept_a_id,
                concept_b=result.concept_b_id,
                weight=result.strength,
                on_bridge_displaced=coordinator.record_bridge_displaced,
            )
            if added is False:
                # bridge 降級紀錄寫入失敗：不建邊，並撤回剛套用的標記
                coordinator.rollback_conflict_mark(tx)
                return False
        except Exception as e:
            # 建邊拋例外：同樣撤回剛套用的標記，不留只有標記沒有邊的狀態
            coordinator.rollback_conflict_mark(tx)
            logger.warning(
                "[ContradictionDetector] CONTRADICTS 建邊失敗，已回滾矛盾標記: %s ↔ %s (%s)",
                result.concept_a_id[:8], result.concept_b_id[:8], e,
            )
            return False
        if not self.graph.has_contradicts_edge(
            concept_a=result.concept_a_id, concept_b=result.concept_b_id,
        ):
            # 邊沒建成（例如節點被略過）：撤回剛套用的標記，不留只有標記沒有邊的狀態
            coordinator.rollback_conflict_mark(tx)
            logger.warning(
                "[ContradictionDetector] CONTRADICTS 邊未建立，已回滾矛盾標記: %s ↔ %s",
                result.concept_a_id[:8], result.concept_b_id[:8],
            )
            return False

        # 若有 ConflictLedger，登記矛盾帳本
        if self._conflict_ledger is not None:
            concept_a = self.semantic.get(result.concept_a_id)
            concept_b = self.semantic.get(result.concept_b_id)
            self._conflict_ledger.register(
                concept_a_id=result.concept_a_id,
                concept_b_id=result.concept_b_id,
                strength=result.strength,
                concept_a_name=concept_a.name if concept_a else "",
                concept_b_name=concept_b.name if concept_b else "",
                reason=result.reason,
            )

        if self.event_emitter is not None:
            self.event_emitter.emit_contradiction_found(
                result.concept_a_id, result.concept_b_id, result.strength,
            )

        logger.info(
            f"[ContradictionDetector] 建立矛盾邊: "
            f"{result.concept_a_id[:8]} ↔ {result.concept_b_id[:8]} "
            f"(strength={result.strength:.2f}, reason={result.reason!r})"
        )
        return True

    def _ensure_concept_node(self, concept_id: str) -> None:
        """
        若節點尚未在圖中，查詢 SemanticMemory 並添加。

        Args:
            concept_id: 概念 ID
        """
        if not self.graph.has_node(concept_id):
            concept = self.semantic.get(concept_id)
            if concept and not self._is_active(concept):
                # 已合併 / 非 active 的概念不得被補回圖譜
                return
            if concept:
                self.graph.add_concept_node(
                    concept_id=concept.id,
                    name=concept.name,
                    concept_type=concept.concept_type,
                )
            else:
                # 資料不完整，添加最小節點以確保邊可以建立（加鎖公開方法）
                self.graph.ensure_placeholder_concept_node(concept_id)
                logger.warning(
                    f"[ContradictionDetector] 概念 {concept_id} "
                    f"不在 SemanticMemory 中，添加最小節點"
                )
