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

import json
import logging
import threading
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from echo_memory.schemas.concept import Concept

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

        Returns:
            concept_id
        """
        with self._lock:
            is_update = name in self._concepts

            if is_update:
                concept = self._concepts[name]
                concept.description = description
                concept.concept_type = concept_type
                concept.updated_at = datetime.now(timezone.utc)
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
                concept.add_source_episode(source_episode_id)

            # 生成/更新 embedding
            vec = embedding
            if vec is None and self._embed_fn is not None:
                vec = self._embed_fn(f"{name}: {description}")

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

    def remove_concept(self, name: str) -> bool:
        """
        移除概念。
        thread-safe: 由 self._lock（RLock）保護。

        Args:
            name: 概念名稱

        Returns:
            True 若成功移除，False 若不存在
        """
        with self._lock:
            if name not in self._concepts:
                return False

            concept = self._concepts[name]
            del self._concepts[name]
            del self._concepts_by_id[concept.id]

            # 重建向量索引
            self._rebuild_embeddings()

            # 持久化
            self._save()

            logger.debug(f"[SemanticMemory] 移除概念: {name} (id={concept.id})")
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
        if self._embeddings is None or len(self._concepts) == 0:
            return []

        query_vec = self._normalize_vector(query_embedding)
        if not np.any(query_vec):
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

    def list_concepts(
        self,
        concept_type: str | None = None,
        limit: int | None = 50,
        offset: int = 0,
        sort_by: str = "updated_at",
    ) -> list[Concept]:
        """
        列出概念，支援篩選和分頁。

        Args:
            concept_type: 若提供，只返回該類型的概念
            limit: 每頁最多返回數量。None 表示不限制（回傳全部）。
            offset: 跳過的筆數
            sort_by: 排序欄位 ("updated_at" / "confidence" / "access_count")

        Returns:
            Concept 列表
        """
        concepts = list(self._concepts.values())

        if concept_type is not None:
            concepts = [c for c in concepts if c.concept_type == concept_type]

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
        with self._lock:
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
            # 保存元數據（原子寫入：先寫 temp 檔再 rename）
            tmp_json = self.metadata_file.with_suffix(".json.tmp")
            try:
                data = [c.to_dict() for c in self._concepts.values()]
                with open(tmp_json, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                tmp_json.replace(self.metadata_file)
            except Exception as e:
                logger.error(f"[SemanticMemory] 保存 concepts.json 失敗: {e}")
                if tmp_json.exists():
                    tmp_json.unlink()

            # 保存向量（原子寫入）
            # np.save 會自動加 .npy 副檔名，所以 temp 檔名必須以 .npy 結尾
            tmp_npy = self.storage_path / "concept_embeddings_tmp.npy"
            try:
                if self._embeddings is not None:
                    np.save(tmp_npy, self._embeddings)
                    tmp_npy.replace(self.embeddings_file)
                elif self.embeddings_file.exists():
                    self.embeddings_file.unlink()
            except Exception as e:
                logger.error(
                    f"[SemanticMemory] 保存 concept_embeddings.npy 失敗: {e}"
                )
                if tmp_npy.exists():
                    tmp_npy.unlink()

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
