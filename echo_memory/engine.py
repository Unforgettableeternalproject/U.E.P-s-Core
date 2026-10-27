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

import hashlib
import logging
import os
import time
import uuid
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from echo_memory.schemas.episode import Episode
from echo_memory.schemas.evidence import (
    SOURCE_TYPES,
    EvidenceInput,
    EvidenceUnit,
    SourceRef,
    is_delivered,
)
from echo_memory.schemas.memory_context import MemoryContext
from echo_memory.namespace import normalize_namespace_id, resolve_namespace_path
from echo_memory.neurochem.state import NeurochemState
from echo_memory.storage.evidence import EvidenceStore, build_legacy_units
from echo_memory.storage.locking import storage_lock

logger = logging.getLogger("echo_memory.engine")

# record_observation 支援的 kind；新增 kind 時一併決定它的彙整規則
OBSERVATION_KINDS = ("game_activity",)


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
        self._evidence = None
        self._idempotency = None
        self._activity = None

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
            # 背景暖機 embedding 後端；失敗只標記 degraded，不影響使用。
            # 測試或離線環境可設 ECHO_DISABLE_EMBEDDING_WARMUP=1 關閉。
            if os.getenv("ECHO_DISABLE_EMBEDDING_WARMUP", "").lower() not in ("1", "true", "yes"):
                self._episodic.warmup(background=True)
        return self._episodic

    def warmup(self, background: bool = True) -> Any:
        """
        主動暖機 embedding 後端，供宿主在啟動時呼叫，避免第一次檢索撞上冷啟動。

        存取 episodic 時已在背景起過一次暖機的話，這裡會再確認一次（已載入時很快）。
        background=True 回傳 daemon thread；False 時同步執行並回傳是否成功。失敗不拋出。
        """
        return self.episodic.warmup(background=background)

    @property
    def evidence(self) -> EvidenceStore:
        """延遲加載證據存儲（append-only evidence.jsonl）。"""
        if self._evidence is None:
            self._evidence = EvidenceStore(
                self.storage_path / "evidence", lock_path=self.storage_path,
            )
        return self._evidence

    @property
    def idempotency(self):
        """延遲加載寫入冪等索引（namespace 目錄下的 idempotency.json）。"""
        if self._idempotency is None:
            from echo_memory.config import get_config
            from echo_memory.storage.idempotency import IdempotencyIndex
            config = get_config()
            self._idempotency = IdempotencyIndex(
                self.storage_path,
                max_keys=config.episode_idempotency_max_keys,
                retention_days=config.episode_idempotency_retention_days,
            )
        return self._idempotency

    @property
    def activity(self):
        """延遲加載活動彙整（namespace 目錄下的 activity.json）。"""
        if self._activity is None:
            from echo_memory.storage.activity import ActivityStore
            self._activity = ActivityStore(self.storage_path)
        return self._activity

    def get_episode_evidence(self, episode: Episode | str) -> list[EvidenceUnit]:
        """取得 Episode 的證據；舊 Episode 回傳即時合成的 legacy_unknown unit（不寫入）。"""
        if isinstance(episode, str):
            found = self.episodic.get(episode)
            if found is None:
                return []
            episode = found
        return self.evidence.units_for_episode(episode)

    def resolve_provenance(self, concept_id: str, at_version: int | None = None):
        """沿 C→B→A 回溯 Concept 的原始 Evidence（含路徑、缺失、循環、版本綁定）。

        at_version 省略時用目前版本；指定則重現該版本當時的來源。
        回傳 ProvenanceResult；不存在的 Concept 回傳 found=False，不拋例外。
        """
        return self._provenance_resolver().resolve(concept_id, at_version=at_version)

    def independent_support(self, concept_ids):
        """以原始 Evidence 去重，計算一組 Concept 的獨立來源（legacy_unknown 單獨計數）。"""
        return self._provenance_resolver().independent_support(concept_ids)

    def _provenance_resolver(self):
        from echo_memory.storage.provenance import ProvenanceResolver

        return ProvenanceResolver(
            semantic=self.semantic,
            get_episode=lambda eid: self.episodic.get(eid),
            evidence=self.evidence,
        )

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
            # 注入圖譜，remove / merge 才能同步清理概念節點
            self._semantic.attach_graph(self.episodic.graph)
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
        *,
        evidence: Sequence[EvidenceInput] | None = None,
        episode_kind: str = "turn",
        idempotency_key: str | None = None,
        **metadata: Any,
    ) -> str:
        """
        存儲一個情節記憶。

        來源契約：
        - 帶 evidence：每個 EvidenceInput 轉成 provenance_status="known" 的 EvidenceUnit
          （意義為「呼叫端以結構欄位宣告了來源」，不是引擎驗證）。
        - 不帶 evidence（舊介面）：自動產生 provenance_status="legacy_unknown" 的 user／agent
          unit，agent 的 delivery_state 為 unknown；不會取得使用者確認的證據地位。

        Args:
            user_input: 用戶輸入（保留原文供對話回憶）
            agent_response: Agent 回應（保留原文；只含已交付部分）
            session_id: 會話 ID
            content_summary: 對話摘要（若為 None 則自動生成）
            salience: 顯著性分數 (0-1)
            da_weight: 多巴胺烙印強度（None 則繼承當前 neurochem.dopamine）
            ht_weight: 血清素穩定性標記（None 則繼承當前 neurochem.serotonin）
            evidence: 來源明確的證據宣告
            episode_kind: "turn"（對話輪）或 "tool_note"（工具操作記憶，須帶 evidence）
            idempotency_key: 寫入冪等鍵（同 namespace 內有效）。同 key 再次呼叫時回傳第一次
                的 episode_id，不再寫 episode／evidence、不提取實體、不推進神經化學；
                僅去重「同一次請求的重送」，不過濾內容。key 對應的 episode 已被刪除時
                視為新寫入。None（預設）時行為完全不變。
            **metadata: 額外元數據

        Returns:
            episode_id: 新建的情節 ID（拒收時為空字串）

        Raises:
            ValueError: episode_kind 無效、tool_note 未帶 evidence，evidence 與
                user_input／agent_response 不一致，或 idempotency_key 不是非空字串。
        """
        if idempotency_key is not None and (
            not isinstance(idempotency_key, str) or not idempotency_key.strip()
        ):
            raise ValueError("idempotency_key 必須是非空字串或 None")
        if episode_kind not in ("turn", "tool_note"):
            raise ValueError(f"episode_kind 必須是 'turn' 或 'tool_note'，收到 {episode_kind!r}")
        if episode_kind == "tool_note" and not evidence:
            raise ValueError("tool_note 必須帶 evidence 宣告來源")
        if evidence:
            self._check_evidence_consistency(user_input, agent_response, evidence)
            has_content = any(e.content and e.content.strip() for e in evidence)
            if not has_content:
                logger.warning("[MemoryEngine] 拒絕存儲全空白的 evidence，跳過 store_episode")
                return ""
        elif not user_input or not user_input.strip():
            # 空白輸入防護：純空白的 user_input 不應產生 episode
            logger.warning(
                "[MemoryEngine] 拒絕存儲空白 user_input，跳過 store_episode"
            )
            return ""

        if content_summary is None:
            # 結合 user + agent 的核心內容作為摘要，供向量搜索使用
            combined = f"{user_input} {agent_response}".strip()
            if not combined and evidence:
                combined = next((e.content for e in evidence if e.content.strip()), "")
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
            episode_kind=episode_kind,
        )

        recorded_at = datetime.now(timezone.utc)
        if evidence:
            units = [
                inp.to_unit(
                    unit_id=f"ev_{uuid.uuid4().hex[:12]}",
                    episode_id=episode.id,
                    recorded_at=recorded_at,
                )
                for inp in evidence
            ]
        else:
            units = build_legacy_units(
                episode_id=episode.id,
                user_input=user_input,
                agent_response=agent_response,
                occurred_at=episode.timestamp,
                metadata=metadata,
                via="legacy_signature",
                recorded_at=recorded_at,
            )
        episode.evidence_ids = [u.id for u in units]

        # 鎖語意：寫入與實體寫回各自是短臨界區，實體提取（LLM）在鎖外，
        # 避免同 namespace 的其他寫入被 LLM 延遲拖到 TimeoutError。
        with storage_lock(self.storage_path):
            # 冪等判定與登記和實際寫入在同一臨界區，並行同 key 請求只會有一條通過。
            # 先登記再寫入：寫入失敗或中途崩潰時 key 指向不存在的 episode，
            # 重試會被視為新寫入；不會出現「已寫入卻未登記」的重複窗口。
            if idempotency_key is not None:
                existing_id = self.idempotency.get(idempotency_key)
                if existing_id is not None and self.episodic.get(existing_id) is not None:
                    logger.info(
                        "[MemoryEngine] 冪等命中，略過寫入: key=%s -> %s",
                        idempotency_key, existing_id,
                    )
                    return existing_id
                self.idempotency.record(idempotency_key, episode.id)
            # 先追加證據再寫 Episode：中途失敗只會留下無害的孤兒 evidence，
            # 不會有「沒有證據的 Episode」。
            self.evidence.append(units)
            episode_id = self.episodic.write(episode)
            logger.info(f"[MemoryEngine] 存儲情節: {episode_id}")

        # ── 實體提取（非關鍵路徑，失敗不影響主寫入）──────────────────
        # 低 salience 時跳過以節省資源，除非 entity_triggers=True 強制執行
        entity_triggers = metadata.get("entity_triggers", False)
        should_extract = salience >= 0.4 or entity_triggers
        # tool_note 的 user_input／agent_response 為空，實體提取讀不到內容（待下游改讀 evidence）
        if not (user_input.strip() or agent_response.strip()):
            should_extract = False
        if should_extract:
            try:
                # 鎖外：LLM 呼叫（慢）
                entities = self.entity_extractor.extract(episode)

                if entities:
                    # 鎖內短臨界區：確認 episode 仍存在才寫回，過期則丟棄
                    with storage_lock(self.storage_path):
                        if self.episodic.get(episode_id) is None:
                            logger.info(
                                "[MemoryEngine] episode %s 在實體提取期間已被刪除，"
                                "丟棄 %d 個實體",
                                episode_id[:8], len(entities),
                            )
                        else:
                            self._apply_entities(episode_id, entities)
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

    @staticmethod
    def _check_evidence_consistency(
        user_input: str, agent_response: str, evidence: Sequence[EvidenceInput],
    ) -> None:
        """user_input／agent_response 必須與 evidence 一致（早期暴露呼叫端 bug）。"""
        user_text = "\n".join(e.content for e in evidence if e.source_type == "user")
        agent_text = "\n".join(
            e.content for e in evidence
            if e.source_type == "agent" and e.delivery_state in ("delivered", "partial")
        )
        if user_text.strip() != (user_input or "").strip():
            raise ValueError("user_input 與 evidence 中 user unit 的內容不一致")
        if agent_text.strip() != (agent_response or "").strip():
            raise ValueError("agent_response 與 evidence 中已交付 agent unit 的內容不一致")

    def _apply_entities(self, episode_id: str, entities: list[Any]) -> None:
        """寫入實體與 MENTIONS 邊（呼叫端須已持有 storage_lock）。"""
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

        graph.save()
        logger.debug(
            f"[MemoryEngine] 實體提取完成: "
            f"{len(entities)} 個實體 (episode={episode_id[:8]}...)"
        )

    def retrieve(
        self,
        query: str,
        dopamine: float | None = None,
        serotonin: float | None = None,
        top_k: int = 10,
        min_similarity: float | None = None,
        exclude_session_id: str | None = None,
        exclude_episode_ids: set[str] | None = None,
    ) -> MemoryContext:
        """
        檢索相關記憶（EcphoryRAG）。

        Args:
            query: 查詢文本
            dopamine: 多巴胺濃度 (0-1)，None 則從 neurochem 取值
            serotonin: 血清素濃度 (0-1)，None 則從 neurochem 取值
            top_k: 返回記憶數量
            exclude_session_id: 排除指定 session 的全部 Episodes（向後相容）
            exclude_episode_ids: 排除指定 ID 的 Episodes（精準排除，
                                 三層混合策略用）

        Returns:
            MemoryContext: 檢索到的記憶脈絡
        """
        # 使用 neurochem 狀態作為預設值（取代硬編碼 0.5）
        if dopamine is None:
            dopamine = self._neurochem.dopamine
        if serotonin is None:
            serotonin = self._neurochem.serotonin
        if min_similarity is None:
            from echo_memory.config import get_config
            min_similarity = get_config().retrieval_min_similarity

        # setup：首次存取時延遲建立 retrieval／episodic／semantic 並載入資料
        t_start = time.perf_counter()
        retrieval = self.retrieval
        t_setup = time.perf_counter()

        context = retrieval.retrieve(
            query=query,
            dopamine=dopamine,
            serotonin=serotonin,
            top_k=top_k,
            min_similarity=min_similarity,
            exclude_session_id=exclude_session_id,
            exclude_episode_ids=exclude_episode_ids,
        )

        # 附上證據供 prompt 組裝判斷交付狀態（未說出口的 agent 內容不得當成「說過」）
        t_evidence = time.perf_counter()
        if context.episodes:
            context.evidence_by_episode = self.evidence.units_for_episodes(context.episodes)
        t_end = time.perf_counter()

        timings = {"setup": round((t_setup - t_start) * 1000, 1)}
        timings.update(context.timings_ms)
        timings["evidence"] = round((t_end - t_evidence) * 1000, 1)
        timings["total"] = round((t_end - t_start) * 1000, 1)
        context.timings_ms = timings

        logger.info(
            f"[MemoryEngine] 檢索完成 - "
            f"Episodes: {len(context.episodes)}, "
            f"Concepts: {len(context.concepts)}, "
            f"degraded={context.degraded}, timings_ms={timings}"
        )

        return context

    def retrieve_evidence(
        self,
        query: str | None = None,
        *,
        episode_ids: Sequence[str] | None = None,
        source_types: Sequence[str] | None = None,
        require_delivered: bool = False,
        include_legacy: bool = True,
        top_k: int = 10,
        min_similarity: float | None = None,
    ) -> list[EvidenceUnit]:
        """
        證據檢索：回傳 EvidenceUnit（逐字原文、speaker_id、delivery_state、provenance_status）。

        與 retrieve() 分開：retrieve 以 Episode 為單位回憶對話並供 prompt 使用；
        本方法供 Verifier／Patch Gate 取證。不新建向量索引：query 走 Episode 向量檢索，
        依排名展開成 unit（同 Episode 內維持寫入順序）。

        Args:
            query: 語意查詢；與 episode_ids 至少給一個。
            episode_ids: 指定 Episode。只給它時依傳入順序展開；與 query 並用時，
                僅保留檢索結果中屬於這些 id 者。
            source_types: 只保留這些 source_type（None＝不限）。僅給 ["observation"] 時
                改走觀察紀錄專用路徑，可不帶 query／episode_ids。
            require_delivered: True 則只留已交付者（agent partial 須有進度；
                not_delivered／unknown／tombstoned 排除）。
            include_legacy: False 則排除 provenance_status="legacy_unknown"。
            top_k / min_similarity: 僅 query 路徑使用，語意同 retrieve()。

        Raises:
            ValueError: query 與 episode_ids 皆未提供，或 source_types 含無效值。
        """
        if source_types is not None:
            bad = [s for s in source_types if s not in SOURCE_TYPES]
            if bad:
                raise ValueError(f"source_types 含無效值 {bad}，須為 {SOURCE_TYPES} 之一")
        wanted = set(source_types) if source_types is not None else None

        # observation 沒有所屬 Episode，只能以 source_types=["observation"] 專門取回
        # （最新在前、最多 top_k 筆；無向量索引，query／episode_ids 不參與篩選）。
        # 一般的 query／episode_ids 路徑只展開 Episode 的證據，不會混入 observation。
        if wanted == {"observation"}:
            units = [
                u for u in self.evidence.list(source_type="observation")
                if u.tombstoned_at is None and (include_legacy or u.provenance_status == "known")
            ]
            units.sort(key=lambda u: u.occurred_at, reverse=True)
            return units[:top_k]

        if not query and not episode_ids:
            raise ValueError("retrieve_evidence 需要 query 或 episode_ids")

        if query:
            episodes = self.retrieve(query, top_k=top_k, min_similarity=min_similarity).episodes
            if episode_ids:
                allow = set(episode_ids)
                episodes = [ep for ep in episodes if ep.id in allow]
        else:
            episodes = []
            for eid in dict.fromkeys(episode_ids or ()):
                ep = self.episodic.get(eid)
                if ep is not None:
                    episodes.append(ep)

        per_episode = self.evidence.units_for_episodes(episodes)
        out: list[EvidenceUnit] = []
        for ep in episodes:
            for u in per_episode.get(ep.id, ()):
                if u.tombstoned_at is not None:
                    continue
                if wanted is not None and u.source_type not in wanted:
                    continue
                if not include_legacy and u.provenance_status != "known":
                    continue
                if require_delivered and not is_delivered(u):
                    continue
                out.append(u)
        return out

    # ─── 平台觀察（observation）與活動彙整 ─────────────────────────

    def record_observation(
        self,
        *,
        kind: str,
        subject: str,
        started_at: datetime,
        ended_at: datetime,
        attrs: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        source_ref: SourceRef | None = None,
    ) -> str | None:
        """
        記錄平台被動觀察（如 Discord presence 的遊戲時段）。

        只追加一筆 source_type="observation" 的 EvidenceUnit（episode_id=None），
        不建 turn episode、不抽實體、不推進 neurochem。observation 不是任何人說的話，
        can_attest_* 一律為 False，也不會進入蒸餾或 prompt 的對話記憶。
        同時把時長累加進活動彙整（activity_profile 的資料來源）。

        Args:
            kind: 觀察類型，目前僅支援 "game_activity"（subject＝遊戲名）。
            subject: 觀察對象；前後空白會被去除。
            started_at / ended_at: 起訖時間（無時區視為 UTC）；ended_at 不得早於 started_at。
            attrs: 額外屬性（須可 JSON 序列化）；與核心欄位同名時以核心欄位為準。
            idempotency_key: 冪等鍵（建議 user+game+start）。同 key 重送回第一次的
                evidence id，不再寫入也不重複累加。證據 id 由 key 決定，所以即使冪等索引
                過期，重送仍不會重複。
            source_ref: 來源指標；預設 SourceRef(kind="discord_presence")。

        Returns:
            evidence id。

        Raises:
            ValueError: kind 不支援、subject 為空、時間順序錯誤或 idempotency_key 非空字串。
        """
        if kind not in OBSERVATION_KINDS:
            raise ValueError(f"kind 必須是 {OBSERVATION_KINDS} 之一，收到 {kind!r}")
        if not isinstance(subject, str) or not subject.strip():
            raise ValueError("subject 必須是非空字串")
        subject = subject.strip()
        if idempotency_key is not None and (
            not isinstance(idempotency_key, str) or not idempotency_key.strip()
        ):
            raise ValueError("idempotency_key 必須是非空字串或 None")
        started = self._as_utc(started_at)
        ended = self._as_utc(ended_at)
        if ended < started:
            raise ValueError("ended_at 不得早於 started_at")
        minutes = (ended - started).total_seconds() / 60.0

        if idempotency_key is not None:
            digest = hashlib.sha1(f"obs:{idempotency_key}".encode("utf-8")).hexdigest()[:12]
            evidence_id = f"ev_obs_{digest}"
            index_key = f"obs:{idempotency_key}"
        else:
            evidence_id = f"ev_obs_{uuid.uuid4().hex[:12]}"
            index_key = None

        inp = EvidenceInput(
            source_type="observation",
            content=f"{kind}: {subject}",
            occurred_at=started,
            source_ref=source_ref or SourceRef(kind="discord_presence"),
            attrs={
                **(attrs or {}),
                "kind": kind,
                "subject": subject,
                "started_at": started.isoformat(),
                "ended_at": ended.isoformat(),
                "duration_minutes": round(minutes, 4),
            },
        )
        # 順序：證據 -> 彙整 -> 冪等索引。彙整以 evidence id 去重，
        # 任一步中斷後以同 key 重試都會補齊缺的部分，不會重複累加。
        with storage_lock(self.storage_path):
            if index_key is not None:
                hit = self.idempotency.get(index_key)
                if hit is not None:
                    logger.info("[MemoryEngine] 觀察冪等命中，略過: key=%s -> %s", idempotency_key, hit)
                    return hit
            existing = self.evidence.get([evidence_id]) if index_key is not None else []
            if existing:
                # 前次寫入中斷：證據已在。已被遮蔽（使用者退出）者不得復活。
                if existing[0].tombstoned_at is None:
                    self.activity.apply(
                        kind, subject, minutes=minutes, started_at=started,
                        ended_at=ended, evidence_id=evidence_id,
                    )
            else:
                self.evidence.append([
                    inp.to_unit(
                        unit_id=evidence_id, episode_id=None,
                        recorded_at=datetime.now(timezone.utc),
                    )
                ])
                self.activity.apply(
                    kind, subject, minutes=minutes, started_at=started,
                    ended_at=ended, evidence_id=evidence_id,
                )
            if index_key is not None:
                self.idempotency.record(index_key, evidence_id)
        logger.info("[MemoryEngine] 記錄觀察: %s %s (%.1f 分鐘)", kind, subject, minutes)
        return evidence_id

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if not isinstance(value, datetime):
            raise ValueError(f"需要 datetime，收到 {type(value).__name__}")
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)

    def activity_profile(
        self, top_n: int = 3, *, kind: str = "game_activity",
    ) -> list[dict[str, Any]]:
        """
        活動摘要：依 total_minutes 由大到小（同分依 last_played 新到舊）取前 top_n。

        回傳 [{name, play_count, total_minutes, last_played}]；沒有資料或已退出時為空。
        """
        if kind not in OBSERVATION_KINDS:
            raise ValueError(f"kind 必須是 {OBSERVATION_KINDS} 之一，收到 {kind!r}")
        if top_n <= 0:
            return []
        entries = self.activity.list(kind)
        entries.sort(key=lambda e: e["last_played"], reverse=True)
        entries.sort(key=lambda e: e["total_minutes"], reverse=True)
        return [
            {
                "name": e["name"],
                "play_count": e["play_count"],
                "total_minutes": round(e["total_minutes"], 2),
                "last_played": e["last_played"],
            }
            for e in entries[:top_n]
        ]

    def forget_activity(self, kind: str = "game_activity") -> int:
        """
        退出用：清除該 kind 的活動彙整，並遮蔽對應的 observation 證據。

        evidence.jsonl 為 append-only，改以 redactions.jsonl 追加遮蔽紀錄；遮蔽後
        retrieve_evidence 不再回傳、內容與 attrs 讀取時為空。冪等索引保留，
        所以同 key 重送不會讓資料復活。回傳本次遮蔽的證據筆數。
        """
        if kind not in OBSERVATION_KINDS:
            raise ValueError(f"kind 必須是 {OBSERVATION_KINDS} 之一，收到 {kind!r}")
        with storage_lock(self.storage_path):
            ids = [
                u.id for u in self.evidence.list(source_type="observation")
                if u.tombstoned_at is None and u.attrs.get("kind") == kind
            ]
            redacted = self.evidence.redact(ids)
            self.activity.clear(kind)
        logger.info("[MemoryEngine] 已清除活動 %s：遮蔽 %d 筆證據", kind, redacted)
        return redacted

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
                lock_factory=lambda: storage_lock(self.storage_path),
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

        # 不在此持鎖：DreamEngine 內部各階段自行以短臨界區取 storage_lock，
        # LLM 階段在鎖外（手動與排程觸發共用同一套鎖語意）
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
                "errors": r.errors if r.errors else None,
            }

        # 蒸餾報告
        if report.distillation:
            d = report.distillation
            result["distillation"] = {
                "episodes_scanned": d.episodes_scanned,
                "concepts_created": d.concepts_created,
                "concepts_updated": d.concepts_updated,
                "concepts_rejected": d.concepts_rejected,
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

    def get_profile(self, max_chars: int = 800, limit: int = 30) -> str:
        """使用者 profile：所有 subject=user 的 active concept，不靠相似度。

        對應 U.E.P Core mem_module 的 profile_memories_provider
        （similarity_threshold=0、全撈）——profile 是常駐脈絡，不是檢索結果。
        「你還記得我什麼」這種開放式問題向量檢索答不了，靠這個。
        按 confidence 排，截到 max_chars。沒有就回空字串。
        """
        concepts = self.semantic.list_concepts(
            subject="user", status="active", limit=limit, sort_by="confidence",
        )
        if not concepts:
            return ""
        lines = []
        for c in concepts:
            desc = (c.description or "").strip().replace("\n", " ")
            lines.append(f"- {c.name}：{desc}" if desc else f"- {c.name}")
        text = "\n".join(lines)
        if len(text) > max_chars:
            text = text[:max_chars].rstrip() + "…"
        return text

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
