"""
ConceptExtractor — 即時概念提取（對話後背景 Thread）

對應 OpenSoul soul/core/agent.py::_extract_concepts_bg()。

在 store_episode() 後以 daemon Thread 執行：
  1. LLM 提取 3-5 個關鍵概念（名詞 + 描述）
  2. 同義詞/多義詞判斷（三層閾值分流）
  3. 建立 Concept 節點 + RELATES_TO 邊 + DERIVED_FROM 邊

設計原則：
  - 非阻塞（daemon Thread，失敗不影響主流程）
  - llm_fn 為 None 時靜默跳過
  - 同義詞閾值對齊 OpenSoul：>=0.92 同義, 0.85-0.92 多義, <0.85 新概念
"""

from __future__ import annotations

import json
import logging
import re
import threading
from collections.abc import Callable
import numpy as np

from echo_memory.schemas.episode import Episode

logger = logging.getLogger("echo_memory.affect.concept_extractor")

# 概念提取 prompt
_CONCEPT_PROMPT = """分析以下對話，提取 3-5 個關鍵概念。

使用者：{user_input}
回覆：{agent_response}

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
- 不要提取過於泛化的概念（如「問題」「回答」）"""


# 同義詞/多義詞閾值（對齊 OpenSoul）
_SYNONYM_THRESHOLD = 0.92   # >= 0.92 → 同義詞（合併）
_POLYSEMY_THRESHOLD = 0.85  # >= 0.85 → 多義詞（建 RELATES_TO）
# < 0.85 → 全新概念


class ConceptExtractor:
    """
    即時概念提取器。

    設計為在 daemon Thread 中運行。
    失敗時靜默記錄日誌，不拋異常。
    """

    def __init__(
        self,
        llm_fn: Callable[[str], str] | None,
        semantic,       # SemanticMemory
        graph,          # MemoryGraph
        embed_fn: Callable[[str], list[float]] | None = None,
    ):
        """
        Args:
            llm_fn: LLM 呼叫函式
            semantic: SemanticMemory 實例
            graph: MemoryGraph 實例
            embed_fn: 文本嵌入函式（用於同義詞比對）
        """
        self._llm_fn = llm_fn
        self._semantic = semantic
        self._graph = graph
        self._embed_fn = embed_fn

    def extract_in_background(
        self,
        episode: Episode,
        salience: float = 0.5,
    ) -> threading.Thread | None:
        """
        在背景 Thread 中執行概念提取。

        Args:
            episode: 剛存儲的 Episode
            salience: 當前顯著性

        Returns:
            啟動的 Thread（None 表示跳過）
        """
        if self._llm_fn is None:
            return None

        t = threading.Thread(
            target=self._extract_concepts_bg,
            args=(episode, salience),
            daemon=True,
            name=f"concept-extract-{episode.id[:8]}",
        )
        t.start()
        return t

    def _extract_concepts_bg(
        self,
        episode: Episode,
        salience: float,
    ) -> None:
        """背景執行的概念提取流程。"""
        try:
            # Step 1: LLM 提取概念
            concepts = self._llm_extract(episode)
            if not concepts:
                return

            concept_ids: list[str] = []

            for item in concepts:
                noun = item.get("noun", "").strip()
                desc = item.get("desc", "").strip()
                if not noun:
                    continue

                # Step 2: 同義詞/多義詞判斷
                concept_id = self._upsert_with_similarity(
                    noun, desc, episode.id
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

    def _llm_extract(self, episode: Episode) -> list[dict[str, str]]:
        """透過 LLM 提取概念列表。"""
        prompt = _CONCEPT_PROMPT.format(
            user_input=episode.user_input[:300],
            agent_response=episode.agent_response[:300],
        )

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
    ) -> str | None:
        """
        根據同義詞/多義詞閾值決定：合併、建邊、或建新概念。

        Returns:
            concept_id（合併或新建的 ID）
        """
        # 生成 embedding
        if self._embed_fn is None:
            # 無 embed_fn，直接 upsert 新概念
            return self._semantic.upsert_concept(
                name=noun,
                description=desc,
                source_episode_id=episode_id,
            )

        try:
            embedding = self._embed_fn(noun + " " + desc)
        except Exception:
            return self._semantic.upsert_concept(
                name=noun,
                description=desc,
                source_episode_id=episode_id,
            )

        # 找最近鄰
        existing_concepts = self._semantic.list_concepts()
        best_match = None
        best_sim = 0.0

        vec = np.array(embedding, dtype=np.float32)
        norm = np.linalg.norm(vec)
        if norm == 0:
            return self._semantic.upsert_concept(
                name=noun,
                description=desc,
                source_episode_id=episode_id,
            )

        for concept in existing_concepts:
            if not concept.embedding or len(concept.embedding) == 0:
                continue
            c_vec = np.array(concept.embedding, dtype=np.float32)
            c_norm = np.linalg.norm(c_vec)
            if c_norm == 0:
                continue

            sim = float(np.dot(vec, c_vec) / (norm * c_norm))
            if sim > best_sim:
                best_sim = sim
                best_match = concept

        if best_match is None or best_sim < _POLYSEMY_THRESHOLD:
            # < 0.85 → 全新概念
            concept_id = self._semantic.upsert_concept(
                name=noun,
                description=desc,
                source_episode_id=episode_id,
                embedding=embedding,
            )
            # 建立 DERIVED_FROM 邊
            if concept_id and self._graph.has_node(episode_id):
                if not self._graph.has_node(concept_id):
                    self._graph.add_concept_node(concept_id, noun)
                self._graph.add_derived_from_edge(concept_id, episode_id)
            return concept_id

        elif best_sim >= _SYNONYM_THRESHOLD:
            # >= 0.92 → 同義詞（合併到現有概念）
            logger.debug(
                f"[ConceptExtractor] 同義詞合併: '{noun}' → '{best_match.name}' "
                f"(sim={best_sim:.3f})"
            )
            # 更新現有概念的 source_episode_ids
            if episode_id not in best_match.source_episode_ids:
                best_match.source_episode_ids.append(episode_id)
                best_match.access_count += 1
                self._semantic._save()
            return best_match.id

        else:
            # 0.85 ~ 0.92 → 多義詞（建新概念 + RELATES_TO 邊）
            logger.debug(
                f"[ConceptExtractor] 多義詞關聯: '{noun}' ~ '{best_match.name}' "
                f"(sim={best_sim:.3f})"
            )
            concept_id = self._semantic.upsert_concept(
                name=noun,
                description=desc,
                source_episode_id=episode_id,
                embedding=embedding,
            )
            if concept_id and best_match.id:
                self._graph.add_relates_to_edge(
                    concept_id, best_match.id,
                    weight=best_sim,
                    salience=0.6,
                )
            return concept_id
