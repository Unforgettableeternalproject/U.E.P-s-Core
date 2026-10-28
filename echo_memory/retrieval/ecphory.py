"""
EcphoryRetrieval - 聯想檢索引擎

Phase 2: 整合 SemanticMemory 概念檢索 + RELATES_TO 展開 + CONTRADICTS penalty

檢索流程：
  1. NumPy 餘弦相似度 → 定位 Episode 種子節點
  2. NetworkX 圖譜展開 → 沿 PRECEDES/RELATES_TO 邊多跳擴展
  3. 語義概念搜索 → SemanticMemory 向量檢索 + RELATES_TO 1-hop 展開
  4. 矛盾檢測 → CONTRADICTS 邊 penalty 降權
  5. 整合返回 MemoryContext（episodes + concepts）
"""

from __future__ import annotations

import logging
import math
import time
from time import perf_counter as _perf_counter  # 分段計時用；與 time.time 分開以便測試凍結時鐘
from typing import Any

from echo_memory.config import get_config
from echo_memory.neurochem.state import NeurochemState, RetrievalParams
from echo_memory.schemas.concept import Concept
from echo_memory.retrieval import standing as standing_mod
from echo_memory.schemas.memory_context import MemoryContext
from echo_memory.storage.episodic import EpisodicMemory
from echo_memory.storage.semantic import SemanticMemory
from echo_memory.storage.working import WorkingMemory

logger = logging.getLogger("echo_memory.retrieval")


class EcphoryRetrieval:
    """
    EcphoryRAG 檢索引擎 - 混合架構。

    整合三層記憶的聯想檢索：
      - Episodic: 向量種子 + 圖譜多跳展開
      - Semantic: 概念向量搜索 + RELATES_TO 展開 + CONTRADICTS 側向抑制
      - Working: (Phase 2 Branch 6) 查詢快取
    """

    # 歷史（已被取代）概念的分數門檻縮放：對應 Concept.retrieval_weight 的 deprecated 權重
    _HISTORY_BAR_SCALE = 0.15

    # 查詢向量快取的 key prefix
    _CACHE_PREFIX = "qemb:"

    def __init__(
        self,
        episodic: EpisodicMemory,
        semantic: SemanticMemory,
        working: WorkingMemory | None = None,
        procedural: Any | None = None,  # ProceduralMemory（延遲導入）
        entity_store: Any | None = None,  # EntityStore（延遲導入）
        evidence: Any | None = None,  # EvidenceStore；用於過濾 lexical 路的未交付 agent 內容
    ):
        self.episodic = episodic
        self.semantic = semantic
        self.working = working  # 若提供，啟用查詢向量快取
        self.procedural = procedural  # 若提供，檢索時一併搜索程序
        self.entity_store = entity_store  # 若提供，沿 MENTIONS 邊收集 Entity
        self.evidence = evidence

    def retrieve(
        self,
        query: str,
        dopamine: float = 0.5,
        serotonin: float = 0.5,
        top_k: int = 10,
        min_similarity: float = 0.3,
        enable_graph_expansion: bool = True,
        enable_semantic: bool = True,
        concept_top_k: int = 5,
        concept_min_similarity: float = 0.3,
        neurochem: NeurochemState | None = None,
        exclude_session_id: str | None = None,
        exclude_episode_ids: set[str] | None = None,
        include_history: bool = False,
    ) -> MemoryContext:
        """
        執行 EcphoryRAG 檢索。

        Args:
            query: 查詢文本
            dopamine: 多巴胺（影響探索廣度，neurochem=None 時使用）
            serotonin: 血清素（影響謹慎程度，neurochem=None 時使用）
            top_k: Episode 返回數量
            min_similarity: Episode 最小相似度閾值
            enable_graph_expansion: 是否啟用圖譜展開
            enable_semantic: 是否啟用語義概念檢索
            concept_top_k: Concept 返回數量
            concept_min_similarity: Concept 最小相似度閾值
            neurochem: NeurochemState 實例（若提供，覆寫 dopamine/serotonin
                       並用衍生參數驅動檢索行為）
            exclude_session_id: 排除指定 session 的全部 Episodes（舊方式，
                                向後相容）
            exclude_episode_ids: 排除指定 ID 的 Episodes（精準排除，
                                 用於三層混合策略：只排除 chat history
                                 中仍活著的 episodes）
            include_history: True 時才帶出已被取代（deprecated + replaced_by）的
                             歷史概念，並由 prompt 標註；預設不召回

        Returns:
            MemoryContext 記憶脈絡（包含 episodes + concepts）
        """
        # 若有 NeurochemState，用它的衍生參數覆寫預設值（只呼叫一次，快取結果）
        _ncparams: RetrievalParams | None = None
        if neurochem is not None:
            _ncparams = neurochem.get_retrieval_params()
            dopamine = neurochem.dopamine
            serotonin = neurochem.serotonin
            concept_top_k = _ncparams.concept_top_k
            concept_min_similarity = _ncparams.concept_min_similarity

        # 分段計時：每段結束時記一次，供宿主判斷逾時卡在哪
        timings: dict[str, float] = {}
        _mark = [_perf_counter()]

        def _lap(name: str) -> None:
            now = _perf_counter()
            timings[name] = round((now - _mark[0]) * 1000, 1)
            _mark[0] = now

        # Step 1: Episode 向量檢索（種子節點）
        # 查詢向量快取：相同查詢不重複生成 embedding（省 ~50ms）
        cache_key = f"{self._CACHE_PREFIX}{query.strip()}"
        query_embedding = None

        if self.working is not None:
            cached = self.working.get(cache_key)
            if cached is not None:
                query_embedding = cached
                logger.debug("[EcphoryRetrieval] 查詢向量快取命中")

        if query_embedding is None:
            query_embedding = self.episodic.try_generate_query_embedding(query)
            if not query_embedding:
                # embedding 不可用（逾時/連線失敗）：回空脈絡並標記降級，不崩潰
                reason = self.episodic.last_embedding_error or "embedding unavailable"
                _lap("embed")
                logger.warning(
                    f"[EcphoryRetrieval] 查詢 embedding 降級: {reason} "
                    f"(embed={timings['embed']}ms)"
                )
                return self._degraded_lexical_context(
                    query, reason, timings, _lap, top_k,
                    exclude_session_id, exclude_episode_ids,
                )
            if self.working is not None:
                self.working.set(cache_key, query_embedding)
        _lap("embed")

        # NeurochemState 影響種子數
        seed_k = max(3, top_k // 2)
        if _ncparams is not None:
            seed_k = _ncparams.seed_k

        # 排除時多撈一些，過濾後再截斷
        has_exclusion = exclude_session_id or exclude_episode_ids
        search_k = seed_k * 3 if has_exclusion else seed_k
        lexical_on = self._lexical_enabled()
        cfg = get_config() if lexical_on else None
        # lexical 啟用時向量路取 top N 再融合；關閉時維持舊行為
        seed_results = self.episodic.search(
            query_embedding,
            top_k=max(search_k, cfg.retrieval_lexical_top_n) if lexical_on else search_k,
            min_similarity=min_similarity,
        )
        rank_sim: dict[str, float] = {}
        lexical_only_ids: list[str] = []

        # 過濾排除的 episodes（避免與 LLM 的 in-context history 重複）
        if lexical_on:
            _lap("seed_search")
            seed_results = self._exclude(seed_results, exclude_session_id, exclude_episode_ids)
            lex_hits = self._lexical_hits(
                query, cfg, exclude_session_id, exclude_episode_ids,
            )
            seed_results, lexical_only_ids, rank_sim = self._rrf_seeds(
                seed_results, lex_hits, seed_k, cfg.retrieval_rrf_k,
                query_embedding, min_similarity, cfg.retrieval_lexical_min_cosine,
            )
            _lap("lexical")
        elif has_exclusion:
            filtered = []
            for ep, score in seed_results:
                # exclude_episode_ids 精準排除（三層混合策略）
                if exclude_episode_ids and ep.id in exclude_episode_ids:
                    continue
                # exclude_session_id 全 session 排除（向後相容）
                if exclude_session_id and ep.session_id == exclude_session_id:
                    continue
                filtered.append((ep, score))
            seed_results = filtered[:seed_k]
        if not lexical_on:
            _lap("seed_search")

        if not seed_results:
            # 沒有 Episode 種子，但仍可能有 Concept 命中
            episodes: list = []
            similarities: dict[str, float] = {}
            seed_ids: list[str] = []
            logger.debug("[EcphoryRetrieval] 未找到相似度達標的 Episode 種子")
        else:
            episodes = [ep for ep, _ in seed_results]
            seed_ids = [ep.id for ep in episodes]
            similarities = {ep.id: score for ep, score in seed_results}

            logger.debug(
                f"[EcphoryRetrieval] Episode 種子檢索完成 - "
                f"找到 {len(episodes)} 個種子 "
                f"(相似度範圍: {min(similarities.values()):.2f}-{max(similarities.values()):.2f})"
            )

        # Step 2: 圖譜多跳展開（Episode PRECEDES 邊）
        if enable_graph_expansion and seed_ids and self.episodic.graph.get_node_count() > 0:
            if _ncparams is not None:
                max_hops = _ncparams.max_hops
                min_weight = _ncparams.min_weight
            else:
                max_hops = 2 if serotonin > 0.6 else 1
                min_weight = max(0.15, 0.35 - dopamine * 0.3)

            expanded_nodes = self.episodic.graph.expand_neighbors(
                seed_ids=seed_ids,
                max_hops=max_hops,
                min_weight=min_weight,
                max_results=top_k * 2,
                node_types={"Episode"},  # 只展開 Episode 節點，跳過 Entity/Concept/Procedure
            )

            logger.debug(
                f"[EcphoryRetrieval] 圖譜展開完成 - "
                f"發現 {len(expanded_nodes)} 個關聯節點"
            )

            for node_id, weight, hops in expanded_nodes:
                if node_id not in similarities:
                    ep = self.episodic.get(node_id)
                    if ep:
                        # graph expansion 也要排除（與 seed 過濾相同邏輯）
                        if exclude_episode_ids and ep.id in exclude_episode_ids:
                            continue
                        if exclude_session_id and ep.session_id == exclude_session_id:
                            continue
                        # 圖譜展開的候選也要做語意相似度驗證，
                        # 避免不相關的 episode 僅靠 PRECEDES 邊被拉入。
                        ep_sim = self.episodic.similarity_to_query(
                            query_embedding, ep.id
                        )
                        if ep_sim is not None and ep_sim < min_similarity:
                            continue
                        episodes.append(ep)
                        sim_val = ep_sim if ep_sim is not None else 0.0
                        similarities[node_id] = max(sim_val, weight * 0.8)

        _lap("graph_expand")

        # Step 3: 語義概念檢索
        concepts: list[Concept] = []
        concept_similarities: dict[str, float] = {}

        if enable_semantic and self.semantic.count() > 0:
            concepts, concept_similarities = self._retrieve_concepts(
                query_embedding=query_embedding,
                top_k=concept_top_k,
                min_similarity=concept_min_similarity,
                dopamine=dopamine,
                include_history=include_history,
            )

            logger.debug(
                f"[EcphoryRetrieval] 語義概念檢索完成 - "
                f"找到 {len(concepts)} 個概念"
            )

        _lap("concepts")

        # Step 4: 程序記憶搜索（若有 ProceduralMemory）
        procedures = []
        if self.procedural is not None and self.procedural.count() > 0:
            try:
                proc_results = self.procedural.search_by_vector(
                    query_embedding=query_embedding,
                    top_k=3,
                    min_similarity=0.3,
                )
                procedures = [proc for proc, _ in proc_results]
                if procedures:
                    logger.debug(
                        f"[EcphoryRetrieval] 程序搜索完成 - "
                        f"找到 {len(procedures)} 個相關程序"
                    )
            except Exception as e:
                logger.warning(f"[EcphoryRetrieval] 程序搜索失敗（不影響檢索）: {e}")

        _lap("procedures")

        # Step 5: 融合排序 + 截斷
        # 融合四分量：語意相似度 + 時間衰減 + 顯著性 + 頻率
        # 權重對齊 OpenSoul 邊緣公式 W = α·Recency + β·Frequency + γ·Salience
        # 並額外加入向量相似度作為第四分量（OpenSoul 缺少這個）
        _SIM_WEIGHT = 0.35     # 向量相似度
        _RECENCY_WEIGHT = 0.25  # 時間衰減
        _SALIENCE_WEIGHT = 0.25  # 顯著性
        _FREQ_WEIGHT = 0.15     # 存取頻率

        now_ts = time.time()

        def _fusion_score(ep) -> float:
            # lexical-only 命中的排序用相似度不低於向量門檻（它已通過 lexical 自己的門檻）；
            # similarities 欄位仍保持原始 cosine
            sim = rank_sim.get(ep.id, similarities.get(ep.id, 0.0))

            # Recency = exp(-0.01 * hours_ago)（對齊 OpenSoul 公式）
            hours_ago = (now_ts - ep.timestamp.timestamp()) / 3600.0
            recency = math.exp(-0.01 * max(0.0, hours_ago))

            salience = getattr(ep, "salience_score", 0.5)

            # Frequency = log1p(access_count) / log1p(max_reasonable)
            access = getattr(ep, "access_count", 0)
            frequency = math.log1p(access) / math.log1p(50)

            return (
                _SIM_WEIGHT * sim
                + _RECENCY_WEIGHT * recency
                + _SALIENCE_WEIGHT * salience
                + _FREQ_WEIGHT * frequency
            )

        episodes.sort(key=_fusion_score, reverse=True)

        # similarities dict 保留原始 cosine sim，不被融合分數覆寫。
        # MemoryContext 消費者（如 Verifier）需要原始語意相似度做判斷，
        # 若需要融合分數，可在外部自行計算。

        final_episodes = episodes[:top_k]

        # Step 6: 沿 MENTIONS 邊收集 Entity（若有 EntityStore）
        entities = []
        if self.entity_store is not None:
            entities = self._collect_entities(final_episodes)
        _lap("rank")

        # 構建 MemoryContext
        context = MemoryContext(
            episodes=final_episodes,
            concepts=concepts,
            entities=entities,
            procedures=procedures,
            similarities=similarities,
            concept_similarities=concept_similarities,
            timings_ms=timings,
            lexical_only_ids=lexical_only_ids,
        )

        logger.debug(
            f"[EcphoryRetrieval] 檢索完成 - "
            f"Episodes: {len(final_episodes)}, Concepts: {len(concepts)}, "
            f"Entities: {len(entities)}, Procedures: {len(procedures)}"
        )

        return context

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Lexical 路 + RRF 融合（只影響種子排序，不涉及存儲決策）
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _lexical_enabled(self) -> bool:
        return bool(get_config().retrieval_lexical_enabled) and callable(
            getattr(self.episodic, "lexical_search", None)
        )

    @staticmethod
    def _exclude(results, exclude_session_id, exclude_episode_ids):
        """依排除條件過濾 (Episode, score) 列表。"""
        if not (exclude_session_id or exclude_episode_ids):
            return list(results)
        out = []
        for ep, score in results:
            if exclude_episode_ids and ep.id in exclude_episode_ids:
                continue
            if exclude_session_id and ep.session_id == exclude_session_id:
                continue
            out.append((ep, score))
        return out

    def _lexical_hits(
        self, query, cfg, exclude_session_id, exclude_episode_ids,
    ) -> list:
        """取 lexical 候選（已排除、已濾掉未交付 agent 內容）；任何失敗回空，不影響向量路。"""
        try:
            hits = self.episodic.lexical_search(
                query,
                top_n=cfg.retrieval_lexical_top_n,
                min_score=cfg.retrieval_lexical_min_score,
                rel_min=cfg.retrieval_lexical_rel_min,
                max_df=cfg.retrieval_lexical_max_df,
                min_coverage=cfg.retrieval_lexical_min_coverage,
            )
            hits = self._exclude(list(hits), exclude_session_id, exclude_episode_ids)
            return self._drop_undelivered(query, hits)
        except Exception as e:
            logger.warning(f"[EcphoryRetrieval] lexical 檢索失敗（不影響向量路）: {e}")
            return []

    def _drop_undelivered(self, query: str, hits: list) -> list:
        """agent 未交付內容不得被 lexical 命中。

        索引文字取自 content_summary／user_input／agent_response；但 agent_response
        在「被打斷且無進度」時仍保留完整預定回覆，content_summary 也可由呼叫端自訂。
        因此帶有未交付 agent unit 的 episode 以證據核對：只允許用其「非未交付 unit」
        的內容（user 與已交付 agent）命中。
        證據不可讀時保守丟棄這些帶證據的候選。
        """
        from echo_memory.schemas.evidence import is_undelivered_agent
        from echo_memory.storage.lexical import has_term_overlap

        if self.evidence is None or not hits:
            return hits
        eps = [ep for ep, _ in hits if getattr(ep, "evidence_ids", None)]
        if not eps:
            return hits
        try:
            units = self.evidence.units_for_episodes(eps)
        except Exception as e:
            logger.warning(f"[EcphoryRetrieval] 讀取證據失敗，略過帶證據的 lexical 候選: {e}")
            return [(ep, s) for ep, s in hits if not getattr(ep, "evidence_ids", None)]
        kept = []
        for ep, score in hits:
            if any(is_undelivered_agent(u) for u in units.get(ep.id, ())):
                allowed = "\n".join(
                    u.content for u in units.get(ep.id, ()) if not is_undelivered_agent(u)
                )
                if not has_term_overlap(query, allowed):
                    continue
            kept.append((ep, score))
        return kept

    def _rrf_seeds(
        self, vector_results, lex_hits, seed_k, rrf_k, query_embedding, min_similarity,
        min_cosine=-1.0,
    ):
        """RRF 融合兩路，回傳 (種子 [(Episode, 原始 cosine)], lexical_only ids, 排序用相似度)。"""
        rrf: dict[str, float] = {}
        eps: dict[str, Any] = {}
        cosine: dict[str, float] = {}
        for rank, (ep, score) in enumerate(vector_results, start=1):
            rrf[ep.id] = rrf.get(ep.id, 0.0) + 1.0 / (rrf_k + rank)
            eps[ep.id] = ep
            cosine[ep.id] = score
        for rank, (ep, _bm25) in enumerate(lex_hits, start=1):
            rrf[ep.id] = rrf.get(ep.id, 0.0) + 1.0 / (rrf_k + rank)
            eps.setdefault(ep.id, ep)
        # lexical-only 候選須有最低語意相似度（向量可用時）：字面巧合不足以成為種子
        lex_only_sim: dict[str, float] = {}
        for eid in rrf:
            if eid in cosine:
                continue
            sim = self.episodic.similarity_to_query(query_embedding, eid)
            sim = 0.0 if sim is None else float(sim)
            if sim < min_cosine:
                continue
            lex_only_sim[eid] = sim
        candidates = [i for i in rrf if i in cosine or i in lex_only_sim]
        # 同分以先到者（向量路優先）為準：sorted 為穩定排序
        order = sorted(candidates, key=lambda i: rrf[i], reverse=True)[:seed_k]

        seeds: list = []
        lexical_only: list[str] = []
        rank_sim: dict[str, float] = {}
        for eid in order:
            if eid in cosine:
                seeds.append((eps[eid], cosine[eid]))
                continue
            # lexical-only：補算原始 cosine（向量路門檻外的候選）
            sim = lex_only_sim[eid]
            seeds.append((eps[eid], sim))
            lexical_only.append(eid)
            rank_sim[eid] = max(sim, min_similarity) if min_similarity > 0 else sim
        return seeds, lexical_only, rank_sim

    def _degraded_lexical_context(
        self, query, reason, timings, lap, top_k, exclude_session_id, exclude_episode_ids,
    ) -> MemoryContext:
        """embedding 不可用時：lexical 仍可提供 Episode；無命中則維持舊的空脈絡。"""
        hits: list = []
        if self._lexical_enabled():
            cfg = get_config()
            hits = self._lexical_hits(
                query, cfg, exclude_session_id, exclude_episode_ids,
            )
            lap("lexical")
        if not hits:
            return MemoryContext(degraded=True, degraded_reason=reason, timings_ms=timings)
        final = [ep for ep, _ in hits[:top_k]]
        entities = self._collect_entities(final) if self.entity_store is not None else []
        lap("rank")
        logger.warning(f"[EcphoryRetrieval] 降級為僅 lexical，命中 {len(final)} 筆")
        return MemoryContext(
            episodes=final,
            entities=entities,
            similarities={ep.id: 0.0 for ep in final},
            degraded=True,
            degraded_reason=f"{reason}；僅 lexical",
            timings_ms=timings,
            lexical_only_ids=[ep.id for ep in final],
        )

    def _collect_entities(self, episodes: list) -> list:
        """
        從檢索到的 Episode 沿 MENTIONS 邊收集關聯的 Entity。

        去重：同一 Entity 被多個 Episode 提及時只出現一次。

        Returns:
            Entity 列表（按 mention_count 降序）
        """
        graph = self.episodic.graph
        seen_ids: set[str] = set()
        entities = []

        for ep in episodes:
            try:
                mentioned = graph.get_episode_entities(ep.id)
            except Exception:
                continue

            for ent_id, _weight in mentioned:
                if ent_id in seen_ids:
                    continue
                seen_ids.add(ent_id)

                ent = self.entity_store.get(ent_id)
                if ent is not None:
                    entities.append(ent)

        # 按 mention_count 降序（最常被提及的排前面）
        entities.sort(key=lambda e: e.mention_count, reverse=True)
        return entities[:10]  # 上限 10 個，避免噪音過多

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 語義概念檢索
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _retrieve_concepts(
        self,
        query_embedding: list[float],
        top_k: int = 5,
        min_similarity: float = 0.3,
        dopamine: float = 0.5,
        include_history: bool = False,
    ) -> tuple[list[Concept], dict[str, float]]:
        """
        從 SemanticMemory 檢索概念，並進行圖譜展開和側向抑制。

        流程：
          1. 向量搜索 SemanticMemory → 初始概念集合
          2. RELATES_TO + LATENT_BRIDGE 1-hop 展開 → 擴展相關概念
             （bridge 為探索式關聯：權重乘折扣與時間衰減，不是事實支持；
             展開對象 deprecated/archived 依生命週期降權，有 CONTRADICTS 者再降級）
          3. 乘上審核狀態因子（relevance 與 support 分開相乘，confidence 不計入）
          4. CONTRADICTS 側向抑制 → 只壓 standing 較低的一方
          5. 按分數排序返回（同分以 confidence 為 tie-break）

        已被取代（deprecated + replaced_by）的概念預設不召回；include_history=True
        才以原本的歷史權重帶出。

        Args:
            query_embedding: 查詢向量（已加 query prefix）
            top_k: 最終返回數量
            min_similarity: 最低相似度閾值
            dopamine: 多巴胺（影響展開閾值）
            include_history: 是否帶出已被取代的歷史概念

        Returns:
            (concepts, concept_similarities) — 概念列表和對應分數
        """
        params = standing_mod.params_from_config(get_config())

        def _bar(concept: Concept) -> float:
            """該概念的分數門檻：歷史概念已被 retrieval_weight 降權，門檻同比例縮放。"""
            if standing_mod.is_superseded(concept):
                return min_similarity * self._HISTORY_BAR_SCALE
            return min_similarity

        # Step 1: 向量搜索
        # 歷史模式：search_by_vector 以加權後分數比門檻，需放寬才看得到歷史概念，
        # 之後再依各概念自己的門檻過濾
        vector_results = self.semantic.search_by_vector(
            query_embedding,
            top_k=top_k,
            min_similarity=(
                min_similarity * self._HISTORY_BAR_SCALE if include_history
                else min_similarity
            ),
        )
        vector_results = [
            (c, s) for c, s in vector_results
            if s >= _bar(c)
            and (include_history or not standing_mod.is_superseded(c))
        ]

        if not vector_results:
            return [], {}

        concept_scores: dict[str, float] = {}
        concepts_by_id: dict[str, Concept] = {}

        for concept, sim in vector_results:
            concept_scores[concept.id] = sim
            concepts_by_id[concept.id] = concept

        # Step 2: RELATES_TO + LATENT_BRIDGE 1-hop 展開
        graph = self.episodic.graph
        seed_concept_ids = list(concept_scores.keys())
        cfg = get_config()
        now_ts = time.time()

        # 多巴胺影響展開閾值：DA 高 → 更願意展開弱關聯
        expand_threshold = max(0.2, 0.5 - dopamine * 0.3)

        for concept_id in seed_concept_ids:
            if not graph.has_node(concept_id):
                continue

            # (neighbor_id, effective_weight, is_bridge)；RELATES_TO 先於 bridge，
            # 同一鄰居有實證關聯時不會被探索式橋接取代
            expansions: list[tuple[str, float, bool]] = [
                (nid, w, False)
                for nid, _et, w in graph.get_concept_relations(
                    concept_id, edge_type="RELATES_TO"
                )
            ]
            for nid, _et, raw_w in graph.get_concept_relations(
                concept_id, edge_type="LATENT_BRIDGE"
            ):
                edge = graph.graph.get_edge_data(concept_id, nid) or {}
                if edge.get("edge_type") != "LATENT_BRIDGE":
                    edge = graph.graph.get_edge_data(nid, concept_id) or {}
                # 降級／過期的 bridge 不參與檢索擴散（舊邊缺 state 視為 active）
                if (edge.get("state") or "active") != "active":
                    continue
                decay = graph.bridge_decay_factor(
                    edge, cfg.latent_bridge_half_life_days, now_ts,
                )
                expansions.append(
                    (nid, raw_w * cfg.latent_bridge_discount * decay, True)
                )

            for neighbor_id, weight, is_bridge in expansions:
                if neighbor_id in concept_scores:
                    continue  # 已經在結果中
                if weight < expand_threshold:
                    continue

                # 從 SemanticMemory 取得 Concept 物件
                neighbor_concept = self.semantic.get(neighbor_id)
                if neighbor_concept is None:
                    continue
                # 已合併概念不得被召回（直接略過，不是降權）
                if neighbor_concept.metadata.get("merged_into"):
                    continue
                # 已被取代的歷史概念：預設不召回，include_history 才展開
                if not include_history and standing_mod.is_superseded(neighbor_concept):
                    continue

                # 展開的概念分數 = 來源分數 × 邊權重 × 衰減因子 × 生命週期權重
                # （deprecated ×0.15 / archived ×0.03，與向量命中一致）
                derived_score = (
                    concept_scores[concept_id]
                    * weight
                    * 0.7
                    * neighbor_concept.retrieval_weight
                )
                if is_bridge and self._has_contradicts(graph, neighbor_id):
                    # 探索式橋接帶出有未解矛盾的概念時降權（×bridge_conflict_factor）
                    derived_score *= params["bridge_conflict_factor"]
                if derived_score >= _bar(neighbor_concept):
                    concept_scores[neighbor_id] = derived_score
                    concepts_by_id[neighbor_id] = neighbor_concept

        # 門檻只看 relevance（含側向抑制）；審核狀態因子只影響排序，
        # 不讓「未審核」變相成「更容易被門檻擋掉」
        gate_scores = dict(concept_scores)

        # Step 3: 審核狀態因子（support；與 relevance 分開相乘，confidence 不計入）
        for cid, concept in concepts_by_id.items():
            concept_scores[cid] *= standing_mod.status_factor(
                concept, params["status_table"],
            )

        # Step 4: CONTRADICTS 側向抑制
        multipliers = self._apply_lateral_inhibition(
            concept_scores, concepts_by_id, graph,
            gamma=params["gamma"], floor=params["floor"],
            status_table=params["status_table"], rank_table=params["rank_table"],
        )
        for cid, m in multipliers.items():
            gate_scores[cid] *= m

        # Step 5: 排序 + 截斷（同分以 confidence 為 tie-break）
        sorted_ids = sorted(
            concept_scores.keys(),
            key=lambda cid: (concept_scores[cid], concepts_by_id[cid].confidence),
            reverse=True,
        )[:top_k]

        # 過濾掉被抑制壓到門檻以下的
        final_concepts = []
        final_scores: dict[str, float] = {}
        for cid in sorted_ids:
            if gate_scores[cid] >= _bar(concepts_by_id[cid]):
                final_concepts.append(concepts_by_id[cid])
                final_scores[cid] = concept_scores[cid]

        return final_concepts, final_scores

    @staticmethod
    def _has_contradicts(graph, concept_id: str) -> bool:
        """概念是否帶有任何 CONTRADICTS 邊。"""
        if not graph.has_node(concept_id):
            return False
        return bool(graph.get_concept_relations(concept_id, edge_type="CONTRADICTS"))

    @staticmethod
    def _apply_lateral_inhibition(
        concept_scores: dict[str, float],
        concepts_by_id: dict[str, Concept],
        graph,
        gamma: float = standing_mod.DEFAULT_GAMMA,
        floor: float = standing_mod.DEFAULT_FLOOR,
        status_table: dict[str, float] | None = None,
        rank_table: dict[str, float] | None = None,
    ) -> dict[str, float]:
        """
        CONTRADICTS 側向抑制：只壓 standing 較低的一方。

        對結果集合內每條 CONTRADICTS 邊 (A, B, w)：
          share_A = standing(A) / (standing(A) + standing(B))
          m_A = max(floor, 1 - gamma × w × (1 - share_A))
        standing 較高者乘數接近 1，較低者被壓；多條邊取最小乘數。
        w 是矛盾強度（越強越該壓），不是乘數。

        Args:
            concept_scores: concept_id → score（會被直接修改）
            concepts_by_id: concept_id → Concept（用來算 standing）
            graph: MemoryGraph 實例

        Returns:
            實際套用的乘數 {concept_id: m}（只含被抑制者）
        """
        def _edges_of(cid: str) -> list[tuple[str, float]]:
            if not graph.has_node(cid):
                return []
            return [
                (nid, w)
                for nid, _et, w in graph.get_concept_relations(
                    cid, edge_type="CONTRADICTS"
                )
            ]

        in_set = {cid: concepts_by_id[cid] for cid in concept_scores if cid in concepts_by_id}
        multipliers = standing_mod.inhibition_multipliers(
            in_set, _edges_of, gamma=gamma, floor=floor,
            table=status_table, rank_table=rank_table,
        )
        for cid, m in multipliers.items():
            original = concept_scores[cid]
            concept_scores[cid] = original * m
            logger.debug(
                f"[EcphoryRetrieval] CONTRADICTS 側向抑制: "
                f"{cid[:12]} {original:.3f} → {concept_scores[cid]:.3f} (×{m:.2f})"
            )
        return multipliers
