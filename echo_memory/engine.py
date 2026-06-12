"""
MemoryEngine - 記憶系統主類

統一管理三層記憶的存儲、檢索與鞏固。

Architecture:
    MemoryEngine
    ├── EpisodicMemory   (對話事件)
    ├── SemanticMemory   (抽象知識)
    ├── WorkingMemory    (短期緩存)
    ├── EcphoryRetrieval (聯想檢索)
    └── DreamEngine      (離線鞏固)
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from echo_memory.schemas.episode import Episode
from echo_memory.schemas.memory_context import MemoryContext
from echo_memory.namespace import normalize_namespace_id, resolve_namespace_path
from echo_memory.storage.locking import storage_lock

logger = logging.getLogger("echo_memory.engine")


class MemoryEngine:
    """
    記憶引擎主類 - MVP 骨架

    Phase 1 實作目標：
    - 基本存儲功能
    - 單一情節記憶存取
    - 簡單的向量檢索

    後續擴展：
    - EcphoryRAG 多跳檢索
    - Dream Engine 鞏固
    - 神經化學調控
    """

    def __init__(
        self,
        storage_path: str | Path = "./data",
        embedding_model: str = "BAAI/bge-small-zh-v1.5",
        device: str = "cpu",
        query_prefix: str = "",
        max_episodes: int | None = None,
        llm_fn: Callable[[str], str] | None = None,
        namespace_id: str = "local-default",
    ):
        """
        初始化記憶引擎。

        Args:
            storage_path: 資料存儲路徑
            embedding_model: 嵌入模型名稱
            device: 運行設備 (cpu/cuda)
            query_prefix: retrieval 查詢時的 prefix（大多數模型不需要，留空即可）
            max_episodes: 情節記憶上限（None 代表無限制）
            llm_fn: LLM 呼叫函式 (prompt: str) -> str，用於知識蒸餾與反思重播。
                    若為 None，Dream Engine 的蒸餾和重播步驟會優雅跳過。
        """
        self.namespace_id = normalize_namespace_id(namespace_id)
        self.storage_root = Path(storage_path)
        self.storage_path = resolve_namespace_path(self.storage_root, self.namespace_id)
        self.storage_path.mkdir(parents=True, exist_ok=True)

        self.embedding_model = embedding_model
        self.device = device
        self.query_prefix = query_prefix
        self.max_episodes = max_episodes
        self._llm_fn = llm_fn

        # 延遲導入以加快啟動速度
        self._episodic = None
        self._semantic = None
        self._working = None
        self._procedural = None
        self._retrieval = None
        self._dream_engine = None
        self._entity_store = None
        self._entity_extractor = None

        logger.info(f"[MemoryEngine] 初始化完成 - 存儲路徑: {self.storage_path}")

    @property
    def episodic(self):
        """延遲加載情節記憶模組"""
        if self._episodic is None:
            from echo_memory.storage.episodic import EpisodicMemory
            self._episodic = EpisodicMemory(
                storage_path=self.storage_path / "episodic",
                embedding_model=self.embedding_model,
                device=self.device,
                query_prefix=self.query_prefix,
                max_episodes=self.max_episodes,
                lock_path=self.storage_path,
            )
        return self._episodic

    @property
    def semantic(self):
        """延遲加載語義記憶模組（共享 EpisodicMemory 的 embedding 模型）"""
        if self._semantic is None:
            from echo_memory.storage.semantic import SemanticMemory

            # 用 episodic 的 _generate_embedding 作為 embed_fn，
            # 這樣兩層記憶共用同一個 SentenceTransformer 實例
            def _shared_embed_fn(text: str) -> list[float]:
                return self.episodic._generate_embedding(text)

            self._semantic = SemanticMemory(
                storage_path=self.storage_path / "semantic",
                embed_fn=_shared_embed_fn,
            )
        return self._semantic

    @property
    def working(self):
        """延遲加載工作記憶模組"""
        if self._working is None:
            from echo_memory.storage.working import WorkingMemory
            self._working = WorkingMemory(ttl_seconds=300)
        return self._working

    @property
    def procedural(self):
        """延遲加載程序記憶模組（共享 EpisodicMemory 的 embedding 模型）"""
        if self._procedural is None:
            from echo_memory.storage.procedural import ProceduralMemory

            def _shared_embed_fn(text: str) -> list[float]:
                return self.episodic._generate_embedding(text)

            self._procedural = ProceduralMemory(
                storage_path=self.storage_path / "procedural",
                embed_fn=_shared_embed_fn,
            )
        return self._procedural

    @property
    def retrieval(self):
        """延遲加載檢索引擎（含查詢向量快取 + 程序搜索 + Entity 收集）"""
        if self._retrieval is None:
            from echo_memory.retrieval.ecphory import EcphoryRetrieval
            self._retrieval = EcphoryRetrieval(
                episodic=self.episodic,
                semantic=self.semantic,
                working=self.working,
                procedural=self.procedural,
                entity_store=self.entity_store,
            )
        return self._retrieval

    @property
    def entity_store(self):
        """延遲加載實體存儲模組"""
        if self._entity_store is None:
            from echo_memory.storage.entity import EntityStore
            self._entity_store = EntityStore(
                storage_path=self.storage_path / "entities",
            )
        return self._entity_store

    @property
    def entity_extractor(self):
        """延遲加載實體提取器"""
        if self._entity_extractor is None:
            from echo_memory.dream.entity_extractor import EntityExtractor
            self._entity_extractor = EntityExtractor(llm_fn=self._llm_fn)
        return self._entity_extractor

    def set_llm_fn(self, llm_fn: Callable[[str], str] | None) -> None:
        """Update the optional LLM callback used by lazy and loaded components."""
        self._llm_fn = llm_fn
        if self._entity_extractor is not None:
            self._entity_extractor._llm_fn = llm_fn

    # ─── 核心 API ────────────────────────────────────────────────────────────

    def store_episode(
        self,
        user_input: str,
        agent_response: str,
        session_id: str,
        content_summary: str | None = None,
        salience: float = 0.5,
        **metadata: Any,
    ) -> str:
        """
        存儲一個情節記憶。

        Args:
            user_input: 用戶輸入
            agent_response: Agent 回應
            session_id: 會話 ID
            content_summary: 對話摘要（若為 None 則自動生成）
            salience: 顯著性分數 (0-1)
            **metadata: 額外元數據

        Returns:
            episode_id: 新建的情節 ID
        """
        if content_summary is None:
            # 結合 user + agent 的核心內容作為摘要，供向量搜索使用
            combined = f"{user_input} {agent_response}"
            content_summary = combined[:150]

        episode = Episode(
            user_input=user_input,
            agent_response=agent_response,
            session_id=session_id,
            content_summary=content_summary,
            salience_score=salience,
            metadata=metadata,
        )

        with storage_lock(self.storage_path):
            episode_id = self.episodic.write(episode)
            logger.info(f"[MemoryEngine] 存儲情節: {episode_id}")

            # ── 實體提取（非關鍵路徑，失敗不影響主寫入）──────────────────
            try:
                entities = self.entity_extractor.extract(episode)
                graph = self.episodic.graph

                for ent in entities:
                    # 插入或更新 EntityStore
                    ent_id = self.entity_store.upsert(ent)
                    self.entity_store.increment_mention(ent_id, episode_id)

                    # 在記憶圖中維護 Entity 節點與 MENTIONS 邊
                    actual_ent = self.entity_store.get(ent_id)
                    if actual_ent is not None:
                        if not graph.has_node(ent_id):
                            graph.add_entity_node(
                                ent_id,
                                name=actual_ent.name,
                                entity_type=actual_ent.entity_type,
                            )
                        graph.add_mentions_edge(episode_id, ent_id)

                if entities:
                    graph.save()
                    logger.debug(
                        f"[MemoryEngine] 實體提取完成: "
                        f"{len(entities)} 個實體 (episode={episode_id[:8]}...)"
                    )
            except Exception as exc:
                logger.warning(f"[MemoryEngine] 實體提取失敗（不影響主寫入）: {exc}")

        return episode_id

    def retrieve(
        self,
        query: str,
        dopamine: float = 0.5,
        serotonin: float = 0.5,
        top_k: int = 10,
        min_similarity: float = 0.3,
    ) -> MemoryContext:
        """
        檢索相關記憶（EcphoryRAG）。

        Args:
            query: 查詢文本
            dopamine: 多巴胺濃度 (0-1)，影響探索廣度
            serotonin: 血清素濃度 (0-1)，影響謹慎程度
            top_k: 返回記憶數量

        Returns:
            MemoryContext: 檢索到的記憶脈絡
        """
        context = self.retrieval.retrieve(
            query=query,
            dopamine=dopamine,
            serotonin=serotonin,
            top_k=top_k,            min_similarity=min_similarity,        )

        logger.info(
            f"[MemoryEngine] 檢索完成 - "
            f"Episodes: {len(context.episodes)}, "
            f"Concepts: {len(context.concepts)}"
        )

        return context

    def dream(self, triggered_by: str = "manual") -> dict[str, Any]:
        """
        執行離線記憶鞏固（Dream Engine - 簡化版已實作）。

        Args:
            triggered_by: 觸發來源 (manual/sleep/idle/cron)

        Returns:
            DreamReport: 鞏固報告
        """
        if self._dream_engine is None:
            from echo_memory.config import get_config
            from echo_memory.dream.engine import DreamEngine

            config = get_config()
            self._dream_engine = DreamEngine(
                graph=self.episodic.graph,
                episodic=self.episodic,
                semantic=self.semantic,
                procedural=self.procedural,
                llm_fn=self._llm_fn,
                idle_minutes=config.dream_idle_minutes,
                cron_schedule=config.dream_cron,
                enable_idle=False,  # 手動調用時不啟用自動觸發
                enable_cron=False,
            )

        with storage_lock(self.storage_path):
            report = self._dream_engine.dream_now(triggered_by=triggered_by)
        logger.info(f"[MemoryEngine] Dream 完成 - {report.duration_seconds:.1f}s")

        result = {
            "triggered_by": report.triggered_by,
            "duration_seconds": report.duration_seconds,
            "edges_updated": report.edges_updated,
            "edges_pruned": report.pruning.edges_pruned if report.pruning else 0,
            "error": report.error,
        }

        # 重播報告
        if report.replay:
            r = report.replay
            result["replay"] = {
                "episodes_replayed": r.episodes_replayed,
                "edges_strengthened": r.edges_strengthened,
                "edges_created": r.edges_created,
                "avg_importance": r.avg_importance,
                "skipped_reason": r.skipped_reason,
            }

        # 蒸餾報告
        if report.distillation:
            d = report.distillation
            result["distillation"] = {
                "episodes_scanned": d.episodes_scanned,
                "concepts_created": d.concepts_created,
                "concepts_updated": d.concepts_updated,
                "edges_created": d.edges_created,
                "skipped_reason": d.skipped_reason,
            }

        return result

    # ─── 輔助方法 ────────────────────────────────────────────────────────────

    def record_procedure_success(self, procedure_id: str) -> bool:
        """記錄程序執行成功"""
        return self.procedural.record_success(procedure_id)

    def record_procedure_failure(self, procedure_id: str) -> bool:
        """記錄程序執行失敗"""
        return self.procedural.record_failure(procedure_id)

    def get_stats(self) -> dict[str, int]:
        """獲取記憶系統統計資訊"""
        return {
            "total_episodes": self.episodic.count(),
            "total_concepts": self.semantic.count(),
            "total_procedures": self.procedural.count(),
            "total_entities": self.entity_store.count(),
            "working_cache_size": len(self.working._cache),
        }

    def clear_all(self):
        """清空所有記憶（測試用）"""
        logger.warning("[MemoryEngine] 清空所有記憶")
        self.episodic.clear()
        self.semantic.clear()
        self.procedural.clear()
        self.entity_store.clear()
        self.working.clear()
