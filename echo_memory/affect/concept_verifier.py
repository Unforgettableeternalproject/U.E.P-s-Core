"""
ConceptVerifier — 概念前驗證器

在 EcphoryRAG 檢索後、LLM 生成前執行。
根據用戶最新輸入，判斷已召回概念的狀態是否需要變更。

雙層驗證：
  1. Embedding 層：快速過濾與用戶輸入語意相關的概念（便宜、快）
  2. LLM 層：對相關概念進行狀態判斷——valid/expand/modify/deprecate（精準、慢）

降級策略：
  - llm_fn=None → 跳過 LLM 層，只做 embedding 相關性過濾，所有概念標記 valid
  - embed_fn=None → 跳過 embedding 過濾，直接送全部概念給 LLM（或全標 valid）
  - 兩者都 None → 所有概念標記 valid（純透傳）

輸出的 ConceptVerification 會被傳遞給：
  - LLM（作為增強上下文：哪些概念可能需要注意）
  - Inhibitor（後處理：根據 LLM 回覆決定是否真正執行 patch）
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

import numpy as np

from echo_memory.schemas.concept import Concept
from echo_memory.schemas.memory_context import MemoryContext

logger = logging.getLogger("echo_memory.affect.concept_verifier")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 資料結構
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


ConceptStatus = Literal["valid", "expand", "modify", "deprecate"]


@dataclass
class ConceptAssessment:
    """
    單個概念的驗證結果。

    Attributes:
        concept_id: 概念 ID
        concept_name: 概念名稱
        status: 狀態判定
          - valid: 不需改變，與用戶輸入一致或無關
          - expand: 用戶提供了新的相關資訊，概念需要補充
          - modify: 用戶修正了概念的含義或對照關係
          - deprecate: 概念已不再適用（用戶明確否認或矛盾）
        reason: 判定理由（LLM 生成或啟發式規則描述）
        suggested_update: 建議的新描述（expand/modify 時由 LLM 提供）
        relevance: 與用戶輸入的語意相關度 [0, 1]（embedding cosine sim）
    """
    concept_id: str
    concept_name: str
    status: ConceptStatus = "valid"
    reason: str = ""
    suggested_update: str | None = None
    relevance: float = 0.0


@dataclass
class ConceptVerification:
    """
    概念驗證結果集合。

    ConceptVerifier.verify() 的輸出，傳遞給後續流程：
    - Entry.py 將變更摘要注入 LLM 上下文
    - Inhibitor 根據 LLM 回覆決定是否執行 patch
    """
    assessments: list[ConceptAssessment] = field(default_factory=list)
    used_llm: bool = False  # 是否使用了 LLM 進行判斷

    @property
    def has_changes(self) -> bool:
        """是否有任何非 valid 的判定。"""
        return any(a.status != "valid" for a in self.assessments)

    @property
    def changes(self) -> list[ConceptAssessment]:
        """僅回傳有變更的判定。"""
        return [a for a in self.assessments if a.status != "valid"]

    def to_context_text(self) -> str:
        """
        生成可注入 LLM 上下文的文字摘要。

        只列出有變更的概念，讓 LLM 知道哪些記憶可能需要注意。
        沒有變更時回傳空字串。
        """
        if not self.has_changes:
            return ""

        lines = ["## 概念狀態變更提示"]
        for a in self.changes:
            status_label = {
                "expand": "補充",
                "modify": "修正",
                "deprecate": "失效",
            }.get(a.status, a.status)
            lines.append(f"- [{status_label}] {a.concept_name}: {a.reason}")
            if a.suggested_update:
                lines.append(f"  建議更新: {a.suggested_update}")

        return "\n".join(lines)

    def summary(self) -> str:
        """簡短摘要（用於日誌/儀表板）。"""
        total = len(self.assessments)
        changes = len(self.changes)
        if changes == 0:
            return f"概念驗證: {total} 個概念，無變更"
        statuses = {}
        for a in self.changes:
            statuses[a.status] = statuses.get(a.status, 0) + 1
        parts = [f"{v} {k}" for k, v in statuses.items()]
        return f"概念驗證: {total} 個概念，{changes} 個變更 ({', '.join(parts)})"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ConceptVerifier
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 結構化輸出 Schema（供 structured_llm_fn 使用）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# concept_verifier 的 LLM 回傳陣列，每個元素對應一個概念的狀態判定
CONCEPT_VERIFY_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "concept": {"type": "string"},
        "status": {"type": "string", "enum": ["valid", "expand", "modify", "deprecate"]},
        "reason": {"type": "string"},
        "update": {"type": ["string", "null"]},
    },
    "required": ["concept", "status", "reason"],
}

CONCEPT_VERIFY_RESPONSE_SCHEMA = {
    "type": "array",
    "items": CONCEPT_VERIFY_ITEM_SCHEMA,
}

# embedding 相關性閾值：低於此值的概念不送 LLM 驗證
_RELEVANCE_THRESHOLD = 0.3

# 最多送幾個概念給 LLM（控制 token 成本）
_MAX_CONCEPTS_TO_LLM = 5

_VERIFY_PROMPT = """\
你是記憶概念驗證器。根據用戶最新輸入，判斷以下已知概念的狀態。

用戶輸入：
{user_input}

已知概念：
{concepts_block}

以純 JSON 陣列回覆，每個概念一個物件：
[{{"concept": "概念名稱", "status": "valid|expand|modify|deprecate", "reason": "判定理由", "update": "若需更新的新描述，不需要則為 null"}}]

狀態說明：
- valid: 概念與用戶輸入一致或無關，不需改變
- expand: 用戶提供了新的相關資訊，概念需要補充更多細節
- modify: 用戶修正了概念的含義、對照關係或事實
- deprecate: 概念已不再適用（用戶明確否認、矛盾、或情況已變）

注意：
- 僅根據用戶輸入判斷，不要猜測
- 大多數概念應該是 valid
- 只有用戶輸入明確涉及的概念才可能是 expand/modify/deprecate
- 回覆必須是純 JSON，不要包含其他文字"""


class ConceptVerifier:
    """
    概念前驗證器。

    在 EcphoryRAG 檢索後、LLM 生成前執行。
    雙層驗證：embedding 相關性過濾 → LLM 狀態判斷。

    使用範例::

        verifier = ConceptVerifier(llm_fn=eval_fn, embed_fn=embed_fn)
        verification = verifier.verify(user_input, memory_ctx)

        if verification.has_changes:
            # 將變更摘要注入 LLM 上下文
            context_hint = verification.to_context_text()
    """

    def __init__(
        self,
        llm_fn: Callable[[str], str] | None = None,
        embed_fn: Callable[[str], list[float]] | None = None,
        relevance_threshold: float = _RELEVANCE_THRESHOLD,
        max_concepts: int = _MAX_CONCEPTS_TO_LLM,
        structured_llm_fn: Callable[[str], dict] | None = None,
    ):
        """
        Args:
            llm_fn: LLM 呼叫函式（EVAL 角色）。None 時跳過 LLM 層。
            embed_fn: 文本嵌入函式。None 時跳過相關性過濾。
            relevance_threshold: embedding cosine sim 閾值，低於此值不送 LLM。
            max_concepts: 最多送幾個概念給 LLM。
            structured_llm_fn: 結構化 LLM 呼叫函式，直接回傳 dict（省去 JSON parse）。
                               存在時優先於 llm_fn 使用；不存在時 fallback 至 llm_fn + JSON parse。
                               注意：此模組 LLM 回傳的是 JSON 陣列，structured_llm_fn
                               應回傳包含 "items" 鍵的 dict，或 callable 內部處理包裝。
        """
        self._llm_fn = llm_fn
        self._structured_llm_fn = structured_llm_fn
        self._embed_fn = embed_fn
        self._relevance_threshold = relevance_threshold
        self._max_concepts = max_concepts

    def verify(
        self,
        user_input: str,
        memory_ctx: MemoryContext | None = None,
    ) -> ConceptVerification:
        """
        驗證檢索到的概念狀態。

        Args:
            user_input: 用戶最新輸入
            memory_ctx: EcphoryRAG 檢索結果

        Returns:
            ConceptVerification — 所有概念的驗證結果
        """
        # 無概念 → 空結果
        if not memory_ctx or not memory_ctx.concepts:
            return ConceptVerification()

        concepts = memory_ctx.concepts

        # ── 第一層：Embedding 相關性過濾 ──
        scored = self._score_relevance(user_input, concepts, memory_ctx)

        # 分為「相關」和「不相關」
        relevant = []
        irrelevant_assessments = []
        for concept, relevance in scored:
            if relevance >= self._relevance_threshold:
                relevant.append((concept, relevance))
            else:
                irrelevant_assessments.append(ConceptAssessment(
                    concept_id=concept.id,
                    concept_name=concept.name,
                    status="valid",
                    reason="與用戶輸入語意不相關",
                    relevance=relevance,
                ))

        # 限制送 LLM 的數量（按相關度排序取前 N）
        relevant.sort(key=lambda x: x[1], reverse=True)
        relevant = relevant[:self._max_concepts]

        # ── 第二層：LLM 狀態判斷 ──
        has_any_llm = self._llm_fn is not None or self._structured_llm_fn is not None
        if relevant and has_any_llm:
            llm_assessments, used_llm = self._llm_verify(user_input, relevant)
        else:
            # 降級：所有相關概念標記 valid
            no_llm_available = (
                self._llm_fn is None and self._structured_llm_fn is None
            )
            llm_assessments = [
                ConceptAssessment(
                    concept_id=c.id,
                    concept_name=c.name,
                    status="valid",
                    reason="LLM 不可用，預設為 valid" if no_llm_available
                           else "語意相關但無需 LLM 驗證",
                    relevance=rel,
                )
                for c, rel in relevant
            ]
            used_llm = False

        # 合併結果
        all_assessments = llm_assessments + irrelevant_assessments

        # 更新 Concept Health：記錄所有被驗證概念的 last_verified_at
        for concept, _rel in relevant:
            concept.record_verification()

        verification = ConceptVerification(
            assessments=all_assessments,
            used_llm=used_llm,
        )

        if verification.has_changes:
            logger.info("[ConceptVerifier] %s", verification.summary())
        else:
            logger.debug("[ConceptVerifier] %s", verification.summary())

        return verification

    # ──────────────────────────────────────────────
    # 第一層：Embedding 相關性
    # ──────────────────────────────────────────────

    def _score_relevance(
        self,
        user_input: str,
        concepts: list[Concept],
        memory_ctx: MemoryContext,
    ) -> list[tuple[Concept, float]]:
        """
        計算每個概念與用戶輸入的語意相關度。

        優先使用 MemoryContext 中已有的 concept_similarities；
        沒有時使用 embed_fn 計算；都沒有時用預設中性值。
        """
        results = []

        for concept in concepts:
            # 優先用 MemoryContext 已有的相似度
            existing_sim = memory_ctx.concept_similarities.get(concept.id, -1.0)
            if existing_sim >= 0:
                results.append((concept, existing_sim))
                continue

            # 用 embed_fn 計算
            if self._embed_fn is not None and concept.embedding:
                try:
                    input_vec = np.array(
                        self._embed_fn(user_input), dtype=np.float32
                    )
                    concept_vec = np.array(concept.embedding, dtype=np.float32)

                    input_norm = np.linalg.norm(input_vec)
                    concept_norm = np.linalg.norm(concept_vec)

                    if input_norm > 0 and concept_norm > 0:
                        sim = float(
                            np.dot(input_vec, concept_vec)
                            / (input_norm * concept_norm)
                        )
                        results.append((concept, max(0.0, sim)))
                        continue
                except Exception as e:
                    logger.debug(
                        "[ConceptVerifier] embedding 計算失敗 (%s): %s",
                        concept.name, e,
                    )

            # 都不行 → 中性值（會被送去 LLM 驗證）
            results.append((concept, 0.5))

        return results

    # ──────────────────────────────────────────────
    # 第二層：LLM 狀態判斷
    # ──────────────────────────────────────────────

    def _llm_verify(
        self,
        user_input: str,
        relevant_concepts: list[tuple[Concept, float]],
    ) -> tuple[list[ConceptAssessment], bool]:
        """
        使用 LLM 判斷概念狀態。優先使用 structured_llm_fn，fallback 至 llm_fn + JSON parse。

        失敗時降級為全部 valid，並回傳 used_llm=False。

        Returns:
            (assessments, used_llm) — used_llm 為 False 表示 LLM 呼叫失敗已降級
        """
        # 組裝概念區塊
        concept_lines = []
        for concept, _rel in relevant_concepts:
            sources = (
                f"（{len(concept.source_episode_ids)} 個來源）"
                if concept.source_episode_ids else ""
            )
            concept_lines.append(
                f"- {concept.name}: {concept.description} "
                f"[信心度: {concept.confidence:.2f}]{sources}"
            )
        concepts_block = "\n".join(concept_lines)

        prompt = _VERIFY_PROMPT.format(
            user_input=user_input,
            concepts_block=concepts_block,
        )

        try:
            # ── 結構化路徑：直接拿到 dict，省去 JSON parse ──
            # structured_llm_fn 應回傳包含 "items" 鍵的 dict，
            # items 的值為與 CONCEPT_VERIFY_RESPONSE_SCHEMA 對應的 list
            if self._structured_llm_fn is not None:
                data = self._structured_llm_fn(prompt)
                items = data.get("items", data) if isinstance(data, dict) else data
                if not isinstance(items, list):
                    items = []
                return self._parse_llm_items(items, relevant_concepts), True

            # ── Fallback 路徑：llm_fn + 手動 JSON parse ──
            assert self._llm_fn is not None
            raw = self._llm_fn(prompt)
            return self._parse_llm_response(raw, relevant_concepts), True
        except Exception as e:
            logger.warning(
                "[ConceptVerifier] LLM 驗證失敗，降級為全部 valid: %s", e
            )
            return [
                ConceptAssessment(
                    concept_id=c.id,
                    concept_name=c.name,
                    status="valid",
                    reason=f"LLM 驗證失敗: {e}",
                    relevance=rel,
                )
                for c, rel in relevant_concepts
            ], False

    def _parse_llm_response(
        self,
        raw: str,
        relevant_concepts: list[tuple[Concept, float]],
    ) -> list[ConceptAssessment]:
        """
        解析 LLM 的原始文字回應（llm_fn 路徑使用）。

        對解析失敗或缺少的概念，降級為 valid。
        """
        # 嘗試提取 JSON（LLM 可能在 JSON 前後加文字）
        text = raw.strip()

        # 嘗試找到 JSON 陣列
        start = text.find("[")
        end = text.rfind("]")
        if start >= 0 and end > start:
            text = text[start:end + 1]

        try:
            items = json.loads(text)
        except json.JSONDecodeError as e:
            logger.warning(
                "[ConceptVerifier] JSON 解析失敗: %s\n原始回應: %s",
                e, raw[:200],
            )
            return [
                ConceptAssessment(
                    concept_id=c.id,
                    concept_name=c.name,
                    status="valid",
                    reason="LLM 回應格式錯誤",
                    relevance=rel,
                )
                for c, rel in relevant_concepts
            ]

        if not isinstance(items, list):
            items = [items]

        return self._parse_llm_items(items, relevant_concepts)

    def _parse_llm_items(
        self,
        items: list,
        relevant_concepts: list[tuple[Concept, float]],
    ) -> list[ConceptAssessment]:
        """
        從已解析的 list 組裝 ConceptAssessment（兩條路徑共用）。

        structured_llm_fn 路徑直接傳入已解析的 list；
        llm_fn 路徑由 _parse_llm_response 解析後再呼叫本方法。
        """
        # 建立名稱 → (concept, relevance) 的查詢表
        name_map = {c.name: (c, rel) for c, rel in relevant_concepts}

        assessments = []
        matched_names = set()

        for item in items:
            if not isinstance(item, dict):
                continue

            name = item.get("concept", "")
            status = item.get("status", "valid")
            reason = item.get("reason", "")
            update = item.get("update")

            # 驗證 status 值
            if status not in ("valid", "expand", "modify", "deprecate"):
                status = "valid"

            # 匹配概念
            if name in name_map:
                concept, rel = name_map[name]
                matched_names.add(name)
                assessments.append(ConceptAssessment(
                    concept_id=concept.id,
                    concept_name=concept.name,
                    status=status,
                    reason=reason,
                    suggested_update=update if update else None,
                    relevance=rel,
                ))

        # LLM 沒有回覆的概念 → valid
        for name, (concept, rel) in name_map.items():
            if name not in matched_names:
                assessments.append(ConceptAssessment(
                    concept_id=concept.id,
                    concept_name=concept.name,
                    status="valid",
                    reason="LLM 未提及，預設為 valid",
                    relevance=rel,
                ))

        return assessments
