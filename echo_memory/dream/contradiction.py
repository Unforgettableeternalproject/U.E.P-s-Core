"""
ContradictionDetector - 自動矛盾偵測模組

對應大腦分區：前額葉皮質（Prefrontal Cortex）- 知識一致性監控

職責：
  - 偵測新概念與既有概念間的語義矛盾
  - 兩階段策略：向量相似度篩選 → LLM 確認
  - 建立 CONTRADICTS 邊，供 Ecphory 懲罰計算使用
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import numpy as np

from echo_memory.dream.staleness import Guard, resolve_guard
from echo_memory.schemas.concept import Concept
from echo_memory.storage.graph import MemoryGraph
from echo_memory.storage.semantic import SemanticMemory

logger = logging.getLogger("echo_memory.dream.contradiction")

# LLM 矛盾判斷 Prompt 模板
_CONTRADICTION_PROMPT = """\
你是知識一致性檢驗員。判斷以下兩個概念是否相互矛盾。

概念 A: {name_a} — {desc_a}
概念 B: {name_b} — {desc_b}

輸出 JSON:
{{"is_contradiction": true/false, "strength": 0.0-1.0, "reason": "..."}}"""


@dataclass
class ContradictionResult:
    """單次矛盾偵測的結果"""

    concept_a_id: str
    concept_b_id: str
    is_contradiction: bool
    strength: float = 0.0
    reason: str = ""


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
        """
        self._guard = resolve_guard(guard)
        self.semantic = semantic
        self.graph = graph
        self.similarity_threshold = similarity_threshold
        self.llm_fn = llm_fn
        self._conflict_ledger = conflict_ledger

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
        偵測 concept 與其他概念的矛盾，並建立 CONTRADICTS 邊。

        策略（兩階段）：
          1. 向量高相似度篩選：cosine similarity >= threshold → 候選
          2. 若有 llm_fn → LLM 確認矛盾；若無 → 不建邊（降級跳過）

        Args:
            concept: 待偵測的新概念
            candidate_pool: 候選概念池；為 None 時使用 SemanticMemory 全部概念

        Returns:
            list of (concept_a_id, concept_b_id, weight) — 已建立的矛盾邊清單
        """
        # 無 LLM 時降級跳過（純向量無法判斷矛盾）
        if self.llm_fn is None:
            logger.debug(
                "[ContradictionDetector] 無 LLM，跳過矛盾偵測（降級模式）"
            )
            return []

        # 概念本身沒有向量時無法比較
        if not concept.embedding:
            logger.debug(
                f"[ContradictionDetector] 概念 {concept.id} 無向量，跳過偵測"
            )
            return []

        # 臨界區 1（短）：取候選池、篩選候選並記錄版本快照（不含 LLM）
        with self._guard():
            pool = candidate_pool if candidate_pool is not None else self._get_all_concepts()

            # 篩選候選（高相似度、排除自身、排除已有矛盾邊）
            candidates = self._filter_candidates(concept, pool)
            # 只記錄此刻確實存在於 SemanticMemory 的概念（未入庫者無從判斷過期）
            snapshot = {}
            for c in [concept, *candidates]:
                stored = self.semantic.get(c.id)
                if stored is not None:
                    snapshot[c.id] = stored.description

        if not candidates:
            logger.debug(
                f"[ContradictionDetector] 概念 {concept.name} 無高相似度候選，跳過"
            )
            return []

        logger.debug(
            f"[ContradictionDetector] 概念 {concept.name} 找到 {len(candidates)} 個候選"
        )

        # 鎖外：對每個候選呼叫 LLM 確認（慢）
        confirmed: list[ContradictionResult] = []
        for candidate in candidates:
            result = self._check_contradiction_with_llm(concept, candidate)
            if result.is_contradiction:
                confirmed.append(result)

        # 臨界區 2（短）：比對快照後登記邊；概念已刪除 / 已改動的結果丟棄
        created_edges: list[tuple[str, str, float]] = []
        with self._guard():
            for result in confirmed:
                if not self._pair_still_valid(result, snapshot):
                    continue
                self._register_edge(result)
                created_edges.append(
                    (result.concept_a_id, result.concept_b_id, result.strength)
                )

        logger.info(
            f"[ContradictionDetector] 概念 {concept.name} "
            f"偵測完成，建立 {len(created_edges)} 條矛盾邊"
        )

        return created_edges

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 內部方法
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _pair_still_valid(
        self, result: ContradictionResult, snapshot: dict[str, str],
    ) -> bool:
        """鎖內檢查：兩端概念仍存在、描述未變、且尚未被登記矛盾邊。"""
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
        if self.graph.has_contradicts_edge(
            concept_a=result.concept_a_id, concept_b=result.concept_b_id,
        ):
            return False
        return True

    def _get_all_concepts(self) -> list[Concept]:
        """從 SemanticMemory 取得全部概念"""
        return list(self.semantic._concepts.values())

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

            # 排除無向量的候選
            if not other.embedding:
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
                candidates.append(other)
                logger.debug(
                    f"[ContradictionDetector] 候選: {other.name} (sim={sim:.4f})"
                )

        return candidates

    def _check_contradiction_with_llm(
        self, concept_a: Concept, concept_b: Concept
    ) -> ContradictionResult:
        """
        呼叫 LLM 確認兩個概念是否矛盾。

        Args:
            concept_a: 概念 A
            concept_b: 概念 B

        Returns:
            ContradictionResult 結果物件
        """
        prompt = _CONTRADICTION_PROMPT.format(
            name_a=concept_a.name,
            desc_a=concept_a.description,
            name_b=concept_b.name,
            desc_b=concept_b.description,
        )

        default_result = ContradictionResult(
            concept_a_id=concept_a.id,
            concept_b_id=concept_b.id,
            is_contradiction=False,
        )

        try:
            response = self.llm_fn(prompt)  # type: ignore[misc]
            parsed = self._parse_llm_response(response)
            return ContradictionResult(
                concept_a_id=concept_a.id,
                concept_b_id=concept_b.id,
                is_contradiction=parsed.get("is_contradiction", False),
                strength=float(parsed.get("strength", 0.0)),
                reason=parsed.get("reason", ""),
            )
        except Exception as e:
            logger.warning(
                f"[ContradictionDetector] LLM 呼叫失敗，跳過: {e}"
            )
            return default_result

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

    def _register_edge(self, result: ContradictionResult) -> None:
        """
        在圖中建立 CONTRADICTS 邊 + 更新 Concept Health + 登記 Conflict Ledger。

        Args:
            result: ContradictionResult 結果物件
        """
        # 確保節點存在（若尚未加入圖中則先添加佔位節點）
        self._ensure_concept_node(result.concept_a_id)
        self._ensure_concept_node(result.concept_b_id)

        self.graph.add_contradicts_edge(
            concept_a=result.concept_a_id,
            concept_b=result.concept_b_id,
            weight=result.strength,
        )

        # 更新兩邊概念的 conflict_count 和 has_contradiction 標記
        for cid in (result.concept_a_id, result.concept_b_id):
            concept = self.semantic.get(cid)
            if concept:
                concept.conflict_count += 1
                concept.metadata["has_contradiction"] = True
        self.semantic._save()

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

        logger.info(
            f"[ContradictionDetector] 建立矛盾邊: "
            f"{result.concept_a_id[:8]} ↔ {result.concept_b_id[:8]} "
            f"(strength={result.strength:.2f}, reason={result.reason!r})"
        )

    def _ensure_concept_node(self, concept_id: str) -> None:
        """
        若節點尚未在圖中，查詢 SemanticMemory 並添加。

        Args:
            concept_id: 概念 ID
        """
        if not self.graph.has_node(concept_id):
            concept = self.semantic.get(concept_id)
            if concept:
                self.graph.add_concept_node(
                    concept_id=concept.id,
                    name=concept.name,
                    concept_type=concept.concept_type,
                )
            else:
                # 資料不完整，添加最小節點以確保邊可以建立
                self.graph.graph.add_node(
                    concept_id,
                    node_type="Concept",
                )
                logger.warning(
                    f"[ContradictionDetector] 概念 {concept_id} "
                    f"不在 SemanticMemory 中，添加最小節點"
                )
