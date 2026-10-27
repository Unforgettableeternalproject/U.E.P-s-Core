"""
SemanticMemory - 語義記憶存儲

Phase 2: 完整持久化 + 向量索引

存儲結構：
  - concepts.json: 所有概念的元數據（不含 embedding）
  - concept_embeddings.npy: L2-normalized 向量矩陣
  - 與 EpisodicMemory 共用相同 embedding 模型

設計決策：
  - 維持 NumPy+JSON 檔案制，不引入資料庫
  - 向量搜索用 dot product（因為存儲時已 L2-normalize）
  - embedding 生成委託給外部（MemoryEngine 傳入 embedder），避免重複載入模型
"""

from __future__ import annotations

import copy
import json
import logging
import threading
from collections.abc import Callable
from datetime import datetime, timezone
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import numpy as np

from echo_memory.schemas.concept import Concept
from echo_memory.storage.locking import ReleasableRLock
from echo_memory.storage.atomic import atomic_write, atomic_write_json

logger = logging.getLogger("echo_memory.semantic")


class SemanticMemory:
    """
    語義記憶管理器 — 持久化 + 向量索引。

    存儲檔案：
      {storage_path}/concepts.json   — 概念元數據（JSON）
      {storage_path}/concept_embeddings.npy — L2-normalized 向量矩陣
    """

    def __init__(
        self,
        storage_path: str | Path,
        embed_fn: Callable[[str], list[float]] | None = None,
    ):
        """
        初始化語義記憶。

        Args:
            storage_path: 存儲目錄
            embed_fn: 文本 → embedding 的函式（由 MemoryEngine 注入，
                      確保與 EpisodicMemory 共用同一個模型實例）。
                      若為 None，upsert 時必須自行提供 embedding。
        """
        self.storage_path = Path(storage_path)
        self.storage_path.mkdir(parents=True, exist_ok=True)

        # 檔案路徑
        self.metadata_file = self.storage_path / "concepts.json"
        self.embeddings_file = self.storage_path / "concept_embeddings.npy"

        # Embedding 生成函式（外部注入）
        self._embed_fn = embed_fn

        # 記憶體快取
        self._concepts: dict[str, Concept] = {}       # name → Concept
        self._concepts_by_id: dict[str, Concept] = {}  # id → Concept
        self._embeddings: np.ndarray | None = None
        self._id_to_index: dict[str, int] = {}   # concept_id → row index
        self._idx_to_id: dict[int, str] = {}     # row index → concept_id（反向查詢，O(1)）

        # 執行緒鎖（RLock：upsert_concept 內部呼叫 _save()，需要可重入）
        self._lock = threading.RLock()
        # 讀者鎖：寫入端只在記憶體變更與快照時持有，存檔 I/O 前讓出（見 _write／_io_unlocked）；
        # 寫入端彼此仍由 _lock 序列化，因此存檔順序與 mark_merged 的失敗回滾語意不變
        self._mem = ReleasableRLock()

        # 圖譜（可選；由 MemoryEngine 注入，供 remove / merge 同步清理概念節點）
        self._graph: Any | None = None

        # 載入已存在的資料
        self._load()

        logger.info(
            f"[SemanticMemory] 初始化完成 — "
            f"已載入 {len(self._concepts)} 個概念"
        )

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 寫入 API
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def upsert_concept(
        self,
        name: str,
        description: str,
        concept_type: str = "entity",
        source_episode_id: str | None = None,
        embedding: list[float] | None = None,
        metadata: dict[str, Any] | None = None,
        producer: str = "unknown",
    ) -> str:
        """
        插入或更新概念。
        thread-safe: 由 self._lock（RLock）保護。

        若同名概念已存在：
          - 更新 description（取較新的）
          - 追加 source_episode_id（若有）
          - 重新生成 embedding（描述變了）

        Args:
            name: 概念名稱（唯一鍵）
            description: 概念描述
            concept_type: entity | rule | abstract | pattern
            source_episode_id: 蒸餾來源的 Episode ID
            embedding: 預先計算的向量（若為 None 且有 embed_fn 會自動生成）
            metadata: 額外元數據
            producer: 來源產生者（dream/reflection/extractor/conversation/unknown），
                寫入 source_episode_id 的 derivation 時使用

        Returns:
            concept_id
        """
        # embedding 是網路呼叫（輸入只取決於 name/description）：在 _lock 之外先算好，
        # 避免 search_by_vector 等讀者被慢呼叫擋住
        vec = embedding
        if vec is None and self._embed_fn is not None:
            vec = self._embed_fn(f"{name}: {description}")

        with self._write():
            is_update = name in self._concepts

            if is_update:
                concept = self._concepts[name]
                concept.description = description
                concept.concept_type = concept_type
                concept.updated_at = datetime.now(timezone.utc)
                concept.version += 1  # 先升版，後續 derivation 才落在新版本
                if metadata:
                    concept.metadata.update(metadata)
            else:
                concept = Concept(
                    name=name,
                    description=description,
                    concept_type=concept_type,
                    metadata=metadata or {},
                )
                self._concepts[name] = concept
                self._concepts_by_id[concept.id] = concept

            # 追加來源 Episode
            if source_episode_id:
                concept.add_source_episode(source_episode_id, producer=producer)

            # 更新 embedding 索引（向量已於鎖外算好）
            if vec is not None:
                concept.embedding = self._normalize_vector(vec).tolist()
                self._update_embedding_index(concept, is_update=is_update)

            # 持久化（RLock 可重入，此處遞迴呼叫 _save 不會死鎖）
            self._save()

            action = "更新" if is_update else "新增"
            logger.debug(
                f"[SemanticMemory] {action}概念: {name} "
                f"(id={concept.id}, sources={len(concept.source_episode_ids)})"
            )
            return concept.id

    def add_derivation(
        self,
        concept_id: str,
        kind: str,
        parent_id: str,
        producer: str = "unknown",
        parent_version: int | None = None,
        ref: dict[str, Any] | None = None,
    ) -> bool:
        """為既有概念追加一筆衍生紀錄並持久化。

        kind=concept 且未指定 parent_version 時，取 parent 目前的 version（衍生當下的版本）。
        不遞增本概念 version；若本次同時是 mutation，呼叫端應先 record_mutation()。

        Returns:
            True 若新增；概念不存在或紀錄已存在回傳 False
        """
        with self._write():
            concept = self._concepts_by_id.get(concept_id)
            if concept is None:
                return False
            if kind == "concept" and parent_version is None:
                parent = self._concepts_by_id.get(parent_id)
                if parent is not None:
                    parent_version = parent.version
            added = concept.add_derivation(
                kind, parent_id, producer, parent_version=parent_version, ref=ref,
            )
            if added:
                self._save()
            return added

    def remove_concept(self, name: str) -> bool:
        """
        移除概念。
        thread-safe: 由 self._lock（RLock）保護。

        Args:
            name: 概念名稱

        Returns:
            True 若成功移除，False 若不存在
        """
        with self._write():
            if name not in self._concepts:
                return False

            concept = self._concepts[name]
            del self._concepts[name]
            del self._concepts_by_id[concept.id]

            # 圖譜同步清理：不留指向已不存在 Concept 的孤兒節點與邊（先改記憶體，概念存檔成功後再存圖）
            self._drop_graph_node(concept.id, persist=False)

            # 重建向量索引
            self._rebuild_embeddings()

            # 持久化：概念檔先落地，再存圖譜
            self._save()
            self._persist_graph()

            logger.debug(f"[SemanticMemory] 移除概念: {name} (id={concept.id})")
            return True

    def attach_graph(self, graph: Any | None) -> None:
        """注入 MemoryGraph，讓 remove / merge 能同步清理概念節點。"""
        self._graph = graph

    def _drop_graph_node(
        self, concept_id: str, persist: bool = True,
    ) -> dict[str, Any] | None:
        """移除圖譜中的概念節點並回傳快照（供 merge 回滾還原）；無圖譜或無節點回傳 None。

        persist=False 時只改記憶體，由呼叫端在概念檔存檔成功後呼叫 _persist_graph()。
        """
        graph = self._graph
        if graph is None or not graph.has_node(concept_id):
            return None
        snapshot = graph.snapshot_concept_node(concept_id)
        graph.remove_concept_node(concept_id)
        if persist:
            with self._io_unlocked():
                graph.save()
        return snapshot

    def _persist_graph(self) -> None:
        """存圖譜（若有）。必須在概念檔 _save() 成功之後呼叫。"""
        if self._graph is not None:
            with self._io_unlocked():
                self._graph.save()

    @staticmethod
    def is_merged(concept: Concept) -> bool:
        """概念是否已被合併進其他概念（metadata 有 merged_into）。"""
        return bool((concept.metadata or {}).get("merged_into"))

    def mark_merged(self, name: str, merged_into_id: str, patch_id: str = "") -> bool:
        """將次概念標記為「已合併」：轉非 active、保留資料，並摘掉圖譜節點。

        不硬刪（deprecate != delete）：status 轉 deprecated，metadata 記
        merged_into / merged_by_patch；圖譜節點與邊的快照存入
        metadata["merge_graph_snapshot"]，供 restore_merged 還原。
        已合併的概念不參與召回（search_by_vector 略過）。

        順序：概念檔先存檔成功，才摘圖譜節點並存圖；任一步失敗還原記憶體並拋出，
        讓記憶體與磁碟不分歧。

        Returns:
            True 若成功；概念不存在回傳 False
        """
        with self._write():
            concept = self._concepts.get(name)
            if concept is None:
                return False
            prev = (
                concept.status, copy.deepcopy(concept.metadata),
                concept.updated_at, concept.version,
            )

            def _revert() -> None:
                concept.status, concept.metadata, concept.updated_at, concept.version = (
                    prev[0], copy.deepcopy(prev[1]), prev[2], prev[3],
                )

            graph = self._graph
            snapshot = None
            if graph is not None and graph.has_node(concept.id):
                snapshot = graph.snapshot_concept_node(concept.id)
            concept.status = "deprecated"
            concept.metadata["merged_into"] = merged_into_id
            concept.metadata["merged_by_patch"] = patch_id
            concept.metadata["merged_at"] = datetime.now(timezone.utc).isoformat()
            if snapshot is not None:
                concept.metadata["merge_graph_snapshot"] = snapshot
            concept.updated_at = datetime.now(timezone.utc)
            concept.version += 1
            try:
                self._save()
            except Exception:
                _revert()
                raise
            if snapshot is not None:
                try:
                    graph.remove_concept_node(concept.id)
                    with self._io_unlocked():
                        graph.save()
                except Exception:
                    _revert()
                    try:
                        if not graph.has_node(concept.id):
                            graph.restore_concept_node(snapshot)
                        self._save()
                    except Exception as comp_err:
                        logger.error("[SemanticMemory] mark_merged 補償失敗: %s", comp_err)
                    raise
            return True

    def restore_merged(self, name: str) -> bool:
        """merge 回滾：把已合併的次概念恢復為 active，並還原圖譜節點與邊。

        順序：概念檔先存檔成功，才還原圖譜節點並存圖；失敗則還原記憶體並拋出。

        Returns:
            True 若成功；概念不存在或不是「已合併」狀態回傳 False
        """
        with self._write():
            concept = self._concepts.get(name)
            if concept is None or not concept.metadata.get("merged_into"):
                return False
            prev = (
                concept.status, copy.deepcopy(concept.metadata),
                concept.updated_at, concept.version,
            )

            def _revert() -> None:
                concept.status, concept.metadata, concept.updated_at, concept.version = (
                    prev[0], copy.deepcopy(prev[1]), prev[2], prev[3],
                )

            snapshot = concept.metadata.pop("merge_graph_snapshot", None)
            for key in ("merged_into", "merged_by_patch", "merged_at"):
                concept.metadata.pop(key, None)
            concept.status = "active"
            concept.updated_at = datetime.now(timezone.utc)
            concept.version += 1
            try:
                self._save()
            except Exception:
                _revert()
                raise
            if snapshot and self._graph is not None:
                try:
                    self._graph.restore_concept_node(snapshot)
                    with self._io_unlocked():
                        self._graph.save()
                except Exception:
                    _revert()
                    try:
                        if self._graph.has_node(concept.id):
                            self._graph.remove_concept_node(concept.id)
                        self._save()
                    except Exception as comp_err:
                        logger.error("[SemanticMemory] restore_merged 補償失敗: %s", comp_err)
                    raise
            return True

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 查詢 API
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def get(self, concept_id: str) -> Concept | None:
        """根據 ID 獲取概念（不遞增 access_count，純查詢用）。"""
        return self._concepts_by_id.get(concept_id)

    def get_by_name(self, name: str) -> Concept | None:
        """根據名稱獲取概念（不遞增 access_count，純查詢/存在性檢查用）。"""
        return self._concepts.get(name)

    def search_by_vector(
        self,
        query_embedding: list[float],
        top_k: int = 5,
        min_similarity: float = 0.3,
    ) -> list[tuple[Concept, float]]:
        """
        向量相似度搜索概念。

        Args:
            query_embedding: 查詢向量（應為 query-prefix 版本）
            top_k: 返回數量
            min_similarity: 最小相似度閾值

        Returns:
            [(Concept, similarity_score), ...] 按相似度降序
        """
        query_vec = self._normalize_vector(query_embedding)
        if not np.any(query_vec):
            return []

        # 只拿讀者鎖做記憶體內運算；寫入端的 embedding 與存檔 I/O 都不持有它
        with self._mem:
            if self._embeddings is None or len(self._concepts) == 0:
                return []

            # dot product = cosine similarity（因為已 L2-normalize）
            similarities = np.dot(self._embeddings, query_vec)
            top_indices = np.argsort(similarities)[::-1][:top_k]

            results = []
            for idx in top_indices:
                sim = float(similarities[idx])
                if sim < min_similarity:
                    continue

                concept_id = self._index_to_id(idx)
                if concept_id and concept_id in self._concepts_by_id:
                    concept = self._concepts_by_id[concept_id]

                    # 已合併的次概念不參與召回
                    if concept.metadata.get("merged_into"):
                        continue

                    # 按生命週期狀態加權（deprecated ×0.15, archived ×0.03）
                    weighted_sim = sim * concept.retrieval_weight

                    # 加權後低於閾值則跳過
                    if weighted_sim < min_similarity:
                        continue

                    concept.access_count += 1
                    results.append((concept, weighted_sim))

        # 重新按加權分數排序
        results.sort(key=lambda x: x[1], reverse=True)

        logger.debug(
            f"[SemanticMemory] 向量搜索完成 — "
            f"找到 {len(results)}/{top_k} 個相似度 >= {min_similarity} 的概念"
        )
        return results

    @staticmethod
    def validate_subject(subject: str) -> None:
        """subject 必須是 user / agent / world，否則拋 ValueError。"""
        if subject not in ("user", "agent", "world"):
            raise ValueError(f"subject 必須是 user/agent/world，不是 {subject!r}")

    def set_subject(self, concept_id: str, subject: str) -> bool:
        """標記概念的主體（user / agent / world）並持久化。

        蒸餾寫入走 patch pipeline（coordinator → inhibitor → payload），
        subject 不穿那四層，寫入後在這裡補一次。
        """
        self.validate_subject(subject)
        with self._write():
            concept = self._concepts_by_id.get(concept_id)
            if concept is None:
                return False
            if concept.subject != subject:
                concept.subject = subject
                concept.version += 1  # 內容變更：版本單調遞增
                concept.updated_at = datetime.now(timezone.utc)
                self._save()
            return True

    def list_concepts(
        self,
        concept_type: str | None = None,
        limit: int | None = 50,
        offset: int = 0,
        sort_by: str = "updated_at",
        subject: str | None = None,
        status: str | None = None,
    ) -> list[Concept]:
        """
        列出概念，支援篩選和分頁。

        Args:
            concept_type: 若提供，只返回該類型的概念
            subject: 若提供，只返回該主體的概念（user / agent / world）
            status: 若提供，只返回該狀態的概念（active / deprecated / archived）
            limit: 每頁最多返回數量。None 表示不限制（回傳全部）。
            offset: 跳過的筆數
            sort_by: 排序欄位 ("updated_at" / "confidence" / "access_count")

        Returns:
            Concept 列表
        """
        with self._mem:
            concepts = list(self._concepts.values())

        if concept_type is not None:
            concepts = [c for c in concepts if c.concept_type == concept_type]
        if subject is not None:
            concepts = [c for c in concepts if c.subject == subject]
        if status is not None:
            concepts = [c for c in concepts if c.status == status]

        if sort_by == "confidence":
            concepts.sort(key=lambda c: c.confidence, reverse=True)
        elif sort_by == "access_count":
            concepts.sort(key=lambda c: c.access_count, reverse=True)
        else:
            concepts.sort(key=lambda c: c.updated_at, reverse=True)

        if limit is None:
            return concepts[offset:]
        return concepts[offset: offset + limit]

    def count(self) -> int:
        return len(self._concepts)

    def clear(self):
        """清空所有語義記憶。thread-safe: 由 self._lock（RLock）保護。"""
        with self._write():
            self._concepts.clear()
            self._concepts_by_id.clear()
            self._embeddings = None
            self._id_to_index.clear()
            self._idx_to_id.clear()
            self._save()
            logger.info("[SemanticMemory] 已清空所有概念")

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 內部：向量索引管理
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _update_embedding_index(self, concept: Concept, is_update: bool) -> None:
        """新增或更新單一概念的向量索引。"""
        if not concept.embedding:
            return

        new_vec = np.asarray(concept.embedding, dtype=np.float32)

        if is_update and concept.id in self._id_to_index:
            # 原地更新已有的行
            idx = self._id_to_index[concept.id]
            self._embeddings[idx] = new_vec
        else:
            # 追加新行
            if self._embeddings is None:
                self._embeddings = new_vec.reshape(1, -1)
                self._id_to_index[concept.id] = 0
                self._idx_to_id[0] = concept.id
            else:
                idx = len(self._embeddings)
                self._embeddings = np.vstack([self._embeddings, new_vec])
                self._id_to_index[concept.id] = idx
                self._idx_to_id[idx] = concept.id

    def _rebuild_embeddings(self) -> None:
        """重建向量索引（刪除操作後呼叫）。"""
        if not self._concepts_by_id:
            self._embeddings = None
            self._id_to_index = {}
            self._idx_to_id = {}
            return

        vectors = []
        new_id_to_index: dict[str, int] = {}
        new_idx_to_id: dict[int, str] = {}

        for concept in self._concepts.values():
            if concept.embedding:
                vec = np.asarray(concept.embedding, dtype=np.float32)
                idx = len(vectors)
                new_id_to_index[concept.id] = idx
                new_idx_to_id[idx] = concept.id
                vectors.append(vec)

        if vectors:
            self._embeddings = np.vstack(vectors).astype(np.float32)
        else:
            self._embeddings = None
        self._id_to_index = new_id_to_index
        self._idx_to_id = new_idx_to_id

    def _index_to_id(self, index: int) -> str | None:
        """索引 → Concept ID（O(1) 反向查詢）"""
        return self._idx_to_id.get(index)

    @staticmethod
    def _normalize_vector(vector: list[float] | np.ndarray) -> np.ndarray:
        """Return an L2-normalized float32 vector."""
        vec = np.asarray(vector, dtype=np.float32)
        norm = float(np.linalg.norm(vec))
        if norm == 0.0:
            return np.zeros_like(vec, dtype=np.float32)
        return vec / norm

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 持久化
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _load(self) -> None:
        """從磁盤載入概念和向量。"""
        # 載入元數據
        if self.metadata_file.exists():
            try:
                with open(self.metadata_file, encoding="utf-8") as f:
                    data = json.load(f)
                for c_dict in data:
                    concept = Concept.from_dict(c_dict)
                    self._concepts[concept.name] = concept
                    self._concepts_by_id[concept.id] = concept
            except Exception as e:
                logger.error(f"[SemanticMemory] 載入 concepts.json 失敗: {e}")

        # 載入向量
        if self.embeddings_file.exists():
            try:
                loaded = np.load(self.embeddings_file)
                if loaded.ndim == 2:
                    self._embeddings = loaded.astype(np.float32)
                else:
                    logger.warning(
                        "[SemanticMemory] concept_embeddings.npy 非 2D，已忽略"
                    )
                    self._embeddings = None
            except Exception as e:
                logger.error(
                    f"[SemanticMemory] 載入 concept_embeddings.npy 失敗: {e}"
                )
                self._embeddings = None

        # 重建 id_to_index 和 idx_to_id（根據 concepts 順序對齊 embeddings 的行）
        # 假設：_concepts 中的第 i 個概念對應 .npy 的第 i 行。
        # 當所有概念都有 embedding 時成立；不一致時觸發 _rebuild_embeddings 修復。
        if self._embeddings is not None:
            for i, concept in enumerate(self._concepts.values()):
                if i < len(self._embeddings):
                    self._id_to_index[concept.id] = i
                    self._idx_to_id[i] = concept.id
                else:
                    break

            # 如果概念數 > 向量行數（表示有些概念沒有 embedding），裁剪對齊
            if len(self._concepts) != len(self._embeddings):
                logger.warning(
                    f"[SemanticMemory] 概念數 ({len(self._concepts)}) "
                    f"與向量行數 ({len(self._embeddings)}) 不一致，重建索引"
                )
                self._rebuild_embeddings()

    @contextmanager
    def _write(self):
        """寫入區段：先取寫入鎖（序列化寫入者），再取讀者鎖（記憶體變更期間擋讀者）。"""
        with self._lock:
            with self._mem:
                yield

    @contextmanager
    def _io_unlocked(self):
        """做圖譜存檔等磁碟 I/O 時讓出讀者鎖（寫入鎖仍持有）。"""
        with self._mem.released():
            yield

    def _save(self) -> None:
        """
        保存概念元數據和向量到磁盤。
        thread-safe: 由 self._lock（RLock）保護（呼叫端持有鎖，RLock 允許同執行緒重入）。

        使用 temp file + rename 原子替換模式，避免寫入中斷導致資料損毀。

        embedding 同時存在 JSON 和 .npy 中：
          - .npy 是主要的向量存儲（NumPy 高效載入）
          - JSON 裡的 embedding 作為 fallback，在 .npy 損毀時可透過
            _rebuild_embeddings 從 JSON 重建
        """
        with self._lock:
            # 快照在讀者鎖內取（純記憶體），檔案寫入讓出讀者鎖後進行；
            # _lock 仍持有，其他寫入者進不來，所以存檔順序即寫入順序
            with self._mem:
                data = [c.to_dict() for c in self._concepts.values()]
                embeddings = self._embeddings
            with self._mem.released():
                self._write_files(data, embeddings)

    def _write_files(self, data: list[dict[str, Any]], embeddings: np.ndarray | None) -> None:
        # 保存元數據（唯一暫存檔 + 原子替換；失敗直接拋出，不再只 log）
        try:
            atomic_write_json(self.metadata_file, data)
        except Exception as e:
            logger.error(f"[SemanticMemory] 保存 concepts.json 失敗: {e}")
            raise

        # 保存向量（原子寫入；使用檔案物件，np.save 不會自動加副檔名）
        try:
            if embeddings is not None:
                atomic_write(
                    self.embeddings_file,
                    lambda f: np.save(f, embeddings),
                    binary=True,
                )
            elif self.embeddings_file.exists():
                self.embeddings_file.unlink()
        except Exception as e:
            logger.error(
                f"[SemanticMemory] 保存 concept_embeddings.npy 失敗: {e}"
            )
            raise

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 統計
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def get_stats(self) -> dict[str, Any]:
        """獲取語義記憶統計。"""
        concepts = list(self._concepts.values())
        type_counts: dict[str, int] = {}
        for c in concepts:
            type_counts[c.concept_type] = type_counts.get(c.concept_type, 0) + 1

        return {
            "total_concepts": len(concepts),
            "type_distribution": type_counts,
            "avg_confidence": (
                sum(c.confidence for c in concepts) / len(concepts)
                if concepts else 0.0
            ),
            "concepts_with_embeddings": (
                len(self._embeddings) if self._embeddings is not None else 0
            ),
        }
