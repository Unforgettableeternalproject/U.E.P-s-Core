"""
ConceptExtractor — 即時概念提取（對話後背景執行緒池）

對應 OpenSoul soul/core/agent.py::_extract_concepts_bg()。

在 store_episode() 後以 ThreadPoolExecutor 背景執行：
  1. LLM 提取 3-5 個關鍵概念（名詞 + 描述）
  2. 同義詞/多義詞判斷（三層閾值分流）
  3. 建立 Concept 節點 + RELATES_TO 邊 + DERIVED_FROM 邊

設計原則：
  - 非阻塞（ThreadPoolExecutor，有界並發，失敗不影響主流程）
  - llm_fn 為 None 時靜默跳過
  - 同義詞閾值對齊 OpenSoul：>=0.92 同義, 0.85-0.92 多義, <0.85 新概念
  - 提供 flush()（等待所有待處理任務）和 shutdown()（關閉執行緒池）
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
import numpy as np

from echo_memory.schemas.episode import Episode

logger = logging.getLogger("echo_memory.affect.concept_extractor")

# 概念提取 prompt
_CONCEPT_PROMPT = """分析以下對話，提取 3-5 個關鍵概念。

（每行開頭標示內容來源：「使用者說」是使用者確實說過的話；「來源未確認」「助手」「工具記錄」不是使用者陳述）
{conversation}

請以純 JSON 陣列格式回覆（不要包含 markdown 標記）：
[
  {{"noun": "概念名稱", "desc": "一句話描述"}},
  ...
]

規則：
- 提取對話中最重要的名詞概念（人物、地點、技術、事件等）
- 每個概念的 noun 應簡潔（2-6 個字）
- desc 應是一句話說明這個概念在對話中的角色
- 最少 1 個，最多 5 個
- 不要提取過於泛化的概念（如「問題」「回答」）
- desc 不得把助手的推測、建議或工具記錄寫成使用者的事實或偏好"""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 結構化輸出 Schema（供 structured_llm_fn 使用）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

CONCEPT_EXTRACT_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "noun": {"type": "string"},
        "desc": {"type": "string"},
    },
    "required": ["noun", "desc"],
}

# concept_extractor 的 LLM 回傳陣列，每個元素代表一個提取到的概念
CONCEPT_EXTRACT_RESPONSE_SCHEMA = {
    "type": "array",
    "items": CONCEPT_EXTRACT_ITEM_SCHEMA,
    "minItems": 1,
    "maxItems": 5,
}

# 同義詞/多義詞閾值（對齊 OpenSoul）
_SYNONYM_THRESHOLD = 0.92   # >= 0.92 → 同義詞（合併）
_POLYSEMY_THRESHOLD = 0.85  # >= 0.85 → 多義詞（建 RELATES_TO）
# < 0.85 → 全新概念


class ConceptExtractor:
    """
    即時概念提取器。

    使用 ThreadPoolExecutor 在背景執行緒池中運行，提供：
      - 有界並發（max_workers=2，I/O-bound LLM 呼叫）
      - flush()：等待所有待處理任務完成
      - shutdown()：關閉執行緒池（應在程式結束前呼叫）

    失敗時靜默記錄日誌，不拋異常。

    若提供 inhibitor，所有 SemanticMemory 寫入走 patch pipeline（GPT #3）。
    否則使用無 LLM 的降級 Gate，寫入仍經 patch pipeline。
    """

    # 執行緒池預設配置
    _DEFAULT_MAX_WORKERS = 2

    def __init__(
        self,
        llm_fn: Callable[[str], str] | None,
        semantic,       # SemanticMemory
        graph,          # MemoryGraph
        embed_fn: Callable[[str], list[float]] | None = None,
        inhibitor=None,  # ConceptInhibitor | None（避免循環 import）
        max_workers: int | None = None,
        structured_llm_fn: Callable[[str], dict] | None = None,
        evidence_store=None,  # EvidenceStore | None
    ):
        """
        Args:
            llm_fn: LLM 呼叫函式，回傳原始文字字串
            semantic: SemanticMemory 實例
            graph: MemoryGraph 實例
            embed_fn: 文本嵌入函式（用於同義詞比對）
            inhibitor: ConceptInhibitor 實例。提供時走 patch pipeline。
            max_workers: 執行緒池最大工作者數量（預設 2）
            structured_llm_fn: 結構化 LLM 呼叫函式，直接回傳 dict（省去 JSON parse）。
                               存在時優先於 llm_fn 使用；不存在時 fallback 至 llm_fn + JSON parse。
                               注意：LLM 回傳陣列，structured_llm_fn 應回傳包含
                               "items" 鍵的 dict，items 值為概念 list。
            evidence_store: EvidenceStore 實例（可選）。提供時 prompt 依 evidence 標示來源；
                            None 時視為 legacy_unknown（保守）。
        """
        self._evidence_store = evidence_store
        self._llm_fn = llm_fn
        self._structured_llm_fn = structured_llm_fn
        self._semantic = semantic
        self._graph = graph
        self._embed_fn = embed_fn
        if inhibitor is None and semantic is not None:
            # 未注入 inhibitor 時用無 LLM 的降級 Gate，寫入仍經 patch pipeline 並留 audit
            from echo_memory.affect.concept_inhibitor import ConceptInhibitor
            inhibitor = ConceptInhibitor(semantic=semantic, llm_fn=None)
            logger.info("[ConceptExtractor] 未注入 inhibitor，使用降級 Patch Gate（無 LLM）")
        self._inhibitor = inhibitor

        # ThreadPoolExecutor（有界並發 + 可控生命週期）
        self._max_workers = max_workers or self._DEFAULT_MAX_WORKERS
        self._executor = ThreadPoolExecutor(
            max_workers=self._max_workers,
            thread_name_prefix="concept-extract",
        )
        self._pending: list[Future] = []
        self._shutdown = False

    def extract_in_background(
        self,
        episode: Episode,
        salience: float = 0.5,
        concept_seeds: list | None = None,
    ) -> Future | None:
        """
        在背景執行緒池中執行概念提取。

        Args:
            episode: 剛存儲的 Episode
            salience: 當前顯著性
            concept_seeds: StorageJudge 提議的優先概念候選（可選）。
                          不為 None 時，LLM prompt 中優先考慮這些概念。

        Returns:
            Future（None 表示跳過）
        """
        # llm_fn 和 structured_llm_fn 都沒有時靜默跳過
        if self._llm_fn is None and self._structured_llm_fn is None:
            return None
        if self._shutdown:
            logger.warning("[ConceptExtractor] 已關閉，跳過提取")
            return None

        future = self._executor.submit(
            self._extract_concepts_bg,
            episode, salience, concept_seeds,
        )
        # 清理已完成的 futures（避免記憶體洩漏）
        self._pending = [f for f in self._pending if not f.done()]
        self._pending.append(future)
        return future

    def flush(self, timeout: float | None = None) -> int:
        """
        等待所有待處理的概念提取任務完成。

        適用於 session 結束前確保所有背景提取完成，
        或測試中需要同步等待結果。

        Args:
            timeout: 每個 future 的最大等待秒數。None 表示無限等待。

        Returns:
            成功完成的任務數
        """
        completed = 0
        for f in self._pending:
            try:
                f.result(timeout=timeout)
                completed += 1
            except Exception as e:
                logger.warning(
                    "[ConceptExtractor] flush 時任務失敗: %s", e,
                )
        self._pending.clear()
        return completed

    def shutdown(self, wait: bool = True) -> None:
        """
        關閉執行緒池。

        應在程式結束前呼叫。wait=True 會等待所有進行中的任務完成。

        Args:
            wait: 是否等待現有任務完成（預設 True）
        """
        self._shutdown = True
        self._executor.shutdown(wait=wait)
        logger.info("[ConceptExtractor] 執行緒池已關閉")

    def _extract_concepts_bg(
        self,
        episode: Episode,
        salience: float,
        concept_seeds: list | None = None,
    ) -> None:
        """背景執行的概念提取流程。"""
        try:
            # Step 1: LLM 提取概念（若有 seeds 則優先考慮）
            concepts = self._llm_extract(episode, concept_seeds=concept_seeds)
            if not concepts:
                return

            concept_ids: list[str] = []

            # 支撐來源由來源 Episode 的證據角色推得（M-9 規則：user > legacy_unknown > agent_only）
            from echo_memory.utils.source_view import load_units, support_source

            support = support_source(
                [episode], load_units(self._evidence_store, [episode]),
            )

            for item in concepts:
                noun = item.get("noun", "").strip()
                desc = item.get("desc", "").strip()
                if not noun:
                    continue

                # Step 2: 同義詞/多義詞判斷
                concept_id = self._upsert_with_similarity(
                    noun, desc, episode.id, support_source=support,
                )
                if concept_id:
                    concept_ids.append(concept_id)

            # Step 3: 相鄰概念建立 RELATES_TO 邊
            for i in range(len(concept_ids) - 1):
                self._graph.add_relates_to_edge(
                    concept_ids[i],
                    concept_ids[i + 1],
                    weight=0.5,
                    salience=salience,
                )

            if concept_ids:
                self._graph.save()
                logger.debug(
                    f"[ConceptExtractor] 提取 {len(concept_ids)} 個概念 "
                    f"(episode={episode.id[:8]})"
                )

        except Exception as e:
            logger.warning(
                "[ConceptExtractor] 背景提取失敗 (episode=%s): %s",
                episode.id[:8],
                e,
                exc_info=True,
            )

    def _llm_extract(
        self,
        episode: Episode,
        concept_seeds: list | None = None,
    ) -> list[dict[str, str]]:
        """透過 LLM 提取概念列表。優先使用 structured_llm_fn，fallback 至 llm_fn + JSON parse。"""
        from echo_memory.utils.source_view import load_units, render_episode_lines, units_for

        units_by_episode = load_units(self._evidence_store, [episode])
        conversation = "\n".join(render_episode_lines(
            episode, units_for(episode, units_by_episode), max_chars=300,
        ))
        prompt = _CONCEPT_PROMPT.format(conversation=conversation or "（無可確認內容）")

        # 若有 StorageJudge 的 concept_seeds，注入優先提取提示
        if concept_seeds:
            seed_names = []
            for s in concept_seeds:
                name = s.name if hasattr(s, "name") else str(s.get("name", ""))
                if name:
                    seed_names.append(name)
            if seed_names:
                prompt += (
                    f"\n\n提示：優先考慮以下概念（若與對話相關）："
                    f"{', '.join(seed_names)}"
                )

        # ── 結構化路徑：直接拿到 dict，省去 JSON parse ──
        # structured_llm_fn 應回傳包含 "items" 鍵的 dict，
        # items 的值為符合 CONCEPT_EXTRACT_RESPONSE_SCHEMA 的概念 list
        if self._structured_llm_fn is not None:
            data = self._structured_llm_fn(prompt)
            items = data.get("items", data) if isinstance(data, dict) else data
            if isinstance(items, list):
                return items[:5]
            logger.warning(
                "[ConceptExtractor] structured_llm_fn 回傳格式非預期: %s",
                type(items),
            )
            return []

        # ── Fallback 路徑：llm_fn + 手動 JSON parse ──
        assert self._llm_fn is not None
        raw = self._llm_fn(prompt)

        # 解析 JSON
        try:
            data = json.loads(raw.strip())
            if isinstance(data, list):
                return data[:5]
        except json.JSONDecodeError:
            pass

        # 嘗試從 markdown code block 提取
        match = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", raw, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group(1))
                if isinstance(data, list):
                    return data[:5]
            except json.JSONDecodeError:
                pass

        logger.warning(
            "[ConceptExtractor] LLM 回應無法解析為 JSON: %s",
            raw[:200],
        )
        return []

    def _upsert_with_similarity(
        self,
        noun: str,
        desc: str,
        episode_id: str,
        support_source: str | None = None,
    ) -> str | None:
        """
        根據同義詞/多義詞閾值決定：合併、建邊、或建新概念。

        一律走 patch pipeline（未注入 inhibitor 時為無 LLM 的降級 Gate）。

        Returns:
            concept_id（合併或新建的 ID）
        """
        # 生成 embedding
        embedding: list[float] | None = None
        if self._embed_fn is not None:
            try:
                embedding = self._embed_fn(noun + " " + desc)
                vec = np.array(embedding, dtype=np.float32)
                if np.linalg.norm(vec) == 0:
                    embedding = None
            except Exception:
                embedding = None

        # 找最近鄰
        best_match = None
        best_sim = 0.0

        if embedding is not None:
            # 只比對 active（已合併／deprecated 的概念不當同義詞目標）
            existing_concepts = self._semantic.list_concepts(limit=None, status="active")
            vec = np.array(embedding, dtype=np.float32)
            norm = float(np.linalg.norm(vec))

            for concept in existing_concepts:
                if not concept.embedding or len(concept.embedding) == 0:
                    continue
                c_vec = np.array(concept.embedding, dtype=np.float32)
                c_norm = float(np.linalg.norm(c_vec))
                if c_norm == 0:
                    continue
                sim = float(np.dot(vec, c_vec) / (norm * c_norm))
                if sim > best_sim:
                    best_sim = sim
                    best_match = concept

        # ── 一律走 Patch Pipeline（未注入 inhibitor 時為降級 Gate）──
        return self._upsert_via_pipeline(
            noun, desc, episode_id, embedding, best_match, best_sim,
            support_source=support_source,
        )

    def _upsert_via_pipeline(
        self,
        noun: str,
        desc: str,
        episode_id: str,
        embedding: list[float] | None,
        best_match,
        best_sim: float,
        support_source: str | None = None,
    ) -> str | None:
        """透過 ConceptInhibitor patch pipeline 寫入。"""
        if best_match is None or best_sim < _POLYSEMY_THRESHOLD:
            # < 0.85 → 全新概念
            evidence_kind = (
                "extractor_fallback" if embedding is None
                else "extractor_new_concept"
            )
            concept_id, _tx = self._inhibitor.process_extractor_patch(
                noun=noun,
                desc=desc,
                episode_id=episode_id,
                evidence_kind=evidence_kind,
                similarity_score=best_sim,
                embedding=embedding,
                support_source=support_source,
            )
            # Graph 邊由 extractor 自己管（inhibitor 不管 graph）
            if concept_id and embedding is not None:
                if self._graph.has_node(episode_id):
                    if not self._graph.has_node(concept_id):
                        # Gate 可能把它改以 expand 補進相近的既有概念，節點名稱以實際概念為準
                        stored = self._semantic.get(concept_id)
                        self._graph.add_concept_node(
                            concept_id, stored.name if stored is not None else noun,
                        )
                    self._graph.add_derived_from_edge(concept_id, episode_id)
            return concept_id

        elif best_sim >= _SYNONYM_THRESHOLD:
            # >= 0.92 → 同義詞合併
            logger.debug(
                f"[ConceptExtractor] 同義詞合併: '{noun}' → '{best_match.name}' "
                f"(sim={best_sim:.3f})"
            )
            concept_id, _tx = self._inhibitor.process_extractor_patch(
                noun=noun,
                desc=desc,
                episode_id=episode_id,
                evidence_kind="extractor_synonym_merge",
                similarity_score=best_sim,
                existing_concept_id=best_match.id,
                support_source=support_source,
            )
            return concept_id

        else:
            # 0.85 ~ 0.92 → 多義詞
            logger.debug(
                f"[ConceptExtractor] 多義詞關聯: '{noun}' ~ '{best_match.name}' "
                f"(sim={best_sim:.3f})"
            )
            concept_id, _tx = self._inhibitor.process_extractor_patch(
                noun=noun,
                desc=desc,
                episode_id=episode_id,
                evidence_kind="extractor_polysemy",
                similarity_score=best_sim,
                embedding=embedding,
                support_source=support_source,
            )
            # concept_id == best_match.id：Gate 判定為同一件事而 expand 既有概念，不建自環邊
            if concept_id and best_match.id and concept_id != best_match.id:
                # 確保新概念的 graph node 存在再建邊
                if not self._graph.has_node(concept_id):
                    self._graph.add_concept_node(concept_id, noun)
                if not self._graph.has_node(best_match.id):
                    self._graph.add_concept_node(best_match.id, best_match.name)
                self._graph.add_relates_to_edge(
                    concept_id, best_match.id,
                    weight=best_sim,
                    salience=0.6,
                )
            return concept_id
