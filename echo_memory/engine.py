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
from echo_memory.neurochem.state import NeurochemState
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
        embedding_backend: str = "sentence-transformers",
        ollama_base_url: str = "http://localhost:11434",
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
            embedding_backend: "ollama" 或 "sentence-transformers"
            ollama_base_url: Ollama server URL（僅 backend=ollama 時使用）
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
        self.embedding_backend = embedding_backend
        self.ollama_base_url = ollama_base_url

        # 延遲導入以加快啟動速度
        self._episodic = None
        self._semantic = None
        self._working = None
        self._procedural = None
        self._retrieval = None
        self._dream_engine = None
        self._concept_inhibitor = None  # 由外部注入（Entry.py），供 DreamEngine 使用
        self._entity_store = None
        self._entity_extractor = None

        # ── 神經化學狀態（session 級別，跨對話持久化）──────────────────
        self._neurochem: NeurochemState = self._load_neurochem()

        logger.info(f"[MemoryEngine] 初始化完成 - 存儲路徑: {self.storage_path}, "
                    f"neurochem: DA={self._neurochem.dopamine:.3f}, "
                    f"5-HT={self._neurochem.serotonin:.3f}")

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
                embedding_backend=self.embedding_backend,
                ollama_base_url=self.ollama_base_url,
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

    @property
    def neurochem(self) -> NeurochemState:
        """取得當前神經化學狀態。"""
        return self._neurochem

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
        da_weight: float | None = None,
        ht_weight: float | None = None,
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
            da_weight: 多巴胺烙印強度（None 則繼承當前 neurochem.dopamine）
            ht_weight: 血清素穩定性標記（None 則繼承當前 neurochem.serotonin）
            **metadata: 額外元數據

        Returns:
            episode_id: 新建的情節 ID
        """
        if content_summary is None:
            # 結合 user + agent 的核心內容作為摘要，供向量搜索使用
            combined = f"{user_input} {agent_response}"
            content_summary = combined[:150]

        # 若未明確指定 da/ht，繼承當前神經化學狀態
        if da_weight is None:
            da_weight = self._neurochem.dopamine
        if ht_weight is None:
            ht_weight = self._neurochem.serotonin

        episode = Episode(
            user_input=user_input,
            agent_response=agent_response,
            session_id=session_id,
            content_summary=content_summary,
            salience_score=salience,
            da_weight=da_weight,
            ht_weight=ht_weight,
            metadata=metadata,
        )

        with storage_lock(self.storage_path):
            episode_id = self.episodic.write(episode)
            logger.info(f"[MemoryEngine] 存儲情節: {episode_id}")

            # ── 實體提取（非關鍵路徑，失敗不影響主寫入）──────────────────
            # 低 salience 時跳過以節省資源，除非 entity_triggers=True 強制執行
            entity_triggers = metadata.get("entity_triggers", False)
            should_extract = salience >= 0.4 or entity_triggers
            if should_extract:
                try:
                    entities = self.entity_extractor.extract(episode)
                    graph = self.episodic.graph

                    for ent in entities:
                        ent_id = self.entity_store.upsert(ent)
                        self.entity_store.increment_mention(ent_id, episode_id)

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
            else:
                logger.debug(
                    "[MemoryEngine] 跳過實體提取（salience=%.2f, "
                    "entity_triggers=%s）", salience, entity_triggers,
                )

        # ── 神經化學衰減 + 持久化 ───────────────────────────────────
        self._neurochem.tick()
        self._save_neurochem()

        return episode_id

    def retrieve(
        self,
        query: str,
        dopamine: float | None = None,
        serotonin: float | None = None,
        top_k: int = 10,
        min_similarity: float | None = None,
        exclude_session_id: str | None = None,
    ) -> MemoryContext:
        """
        檢索相關記憶（EcphoryRAG）。

        Args:
            query: 查詢文本
            dopamine: 多巴胺濃度 (0-1)，None 則從 neurochem 取值
            serotonin: 血清素濃度 (0-1)，None 則從 neurochem 取值
            top_k: 返回記憶數量
            exclude_session_id: 排除指定 session（避免與 LLM history 重複）

        Returns:
            MemoryContext: 檢索到的記憶脈絡
        """
        # 使用 neurochem 狀態作為預設值（取代硬編碼 0.5）
        if dopamine is None:
            dopamine = self._neurochem.dopamine
        if serotonin is None:
            serotonin = self._neurochem.serotonin
        if min_similarity is None:
            min_similarity = self._config.retrieval_min_similarity

        context = self.retrieval.retrieve(
            query=query,
            dopamine=dopamine,
            serotonin=serotonin,
            top_k=top_k,
            min_similarity=min_similarity,
            exclude_session_id=exclude_session_id,
        )

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
                concept_inhibitor=self._concept_inhibitor,
                idle_minutes=config.dream_idle_minutes,
                cron_schedule=config.dream_cron,
                enable_idle=config.dream_enable_idle,
                enable_cron=config.dream_enable_cron,
            )

            # 啟動自動觸發排程器（閒置 + cron）
            if config.dream_enable_idle or config.dream_enable_cron:
                try:
                    self._dream_engine.start()
                    logger.info(
                        f"[MemoryEngine] Dream 排程器已啟動 "
                        f"(idle={config.dream_enable_idle}, "
                        f"cron={config.dream_enable_cron})"
                    )
                except Exception as e:
                    logger.warning(f"[MemoryEngine] Dream 排程器啟動失敗: {e}")

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

        # 程序提取報告
        if report.procedure_extraction:
            p = report.procedure_extraction
            result["procedure_extraction"] = {
                "procedures_created": getattr(p, "procedures_created", 0),
                "procedures_updated": getattr(p, "procedures_updated", 0),
                "skipped_reason": getattr(p, "skipped_reason", None),
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
        # 重置 neurochem 到預設值
        self._neurochem = NeurochemState()
        self._save_neurochem()

    # ─── 神經化學持久化 ─────────────────────────────────────────────

    def _neurochem_file(self) -> Path:
        """Neurochem 狀態檔案路徑。"""
        return self.storage_path / "neurochem_state.json"

    def _load_neurochem(self) -> NeurochemState:
        """
        從磁碟載入神經化學狀態。

        若檔案不存在或損壞，回傳預設狀態。
        載入後自動套用時間衰減（根據上次存檔距今的小時數）。
        """
        import json

        nc_file = self._neurochem_file()
        if not nc_file.exists():
            logger.info("[MemoryEngine] 未找到 neurochem 狀態檔，使用預設值")
            return NeurochemState()

        try:
            with open(nc_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            state = NeurochemState.from_dict(data)

            # 根據上次存檔時間套用自然衰減
            saved_at = data.get("saved_at")
            if saved_at:
                from datetime import datetime, timezone
                try:
                    last_save = datetime.fromisoformat(saved_at)
                    now = datetime.now(timezone.utc)
                    if last_save.tzinfo is None:
                        last_save = last_save.replace(tzinfo=timezone.utc)
                    elapsed_hours = (now - last_save).total_seconds() / 3600.0
                    if elapsed_hours > 0.1:  # 超過 6 分鐘才衰減
                        state.natural_decay(hours=elapsed_hours)
                        logger.info(
                            f"[MemoryEngine] 載入 neurochem，"
                            f"距上次 {elapsed_hours:.1f}hr，"
                            f"衰減後: DA={state.dopamine:.3f}, 5-HT={state.serotonin:.3f}"
                        )
                except (ValueError, TypeError):
                    pass

            return state
        except Exception as e:
            logger.warning(f"[MemoryEngine] 載入 neurochem 狀態失敗: {e}，使用預設值")
            return NeurochemState()

    def _save_neurochem(self) -> None:
        """
        將神經化學狀態寫入磁碟（原子寫入）。
        """
        import json
        import os
        from datetime import datetime, timezone
        from echo_memory.storage.locking import temporary_sibling

        nc_file = self._neurochem_file()
        nc_file.parent.mkdir(parents=True, exist_ok=True)

        data = self._neurochem.to_dict()
        data["saved_at"] = datetime.now(timezone.utc).isoformat()

        tmp_file = temporary_sibling(nc_file)
        try:
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_file, nc_file)
        except Exception as e:
            logger.error(f"[MemoryEngine] 保存 neurochem 狀態失敗: {e}")
            if tmp_file.exists():
                tmp_file.unlink()
