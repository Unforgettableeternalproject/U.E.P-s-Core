"""
EpisodicMemory - 情節記憶存儲

使用 NumPy 向量矩陣 + JSON 元數據存儲。

Phase 1 實作：
- 基本 CRUD 操作
- NumPy 精確餘弦相似度檢索
- JSON 元數據持久化
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import numpy as np

from echo_memory.schemas.episode import Episode
from echo_memory.storage.graph import MemoryGraph

logger = logging.getLogger("echo_memory.episodic")


class EpisodicMemory:
    """
    情節記憶管理器 - MVP 實作。
    
    存儲結構：
    - episodes.json: 所有情節的元數據
    - embeddings.npy: L2-normalized 向量矩陣
    """

    def __init__(
        self,
        storage_path: str | Path,
        embedding_model: str = "BAAI/bge-small-zh-v1.5",
        device: str = "cpu",
        query_prefix: str = "为这个句子生成表示以用于检索相关文章：",
    ):
        self.storage_path = Path(storage_path)
        self.storage_path.mkdir(parents=True, exist_ok=True)

        self.embedding_model = embedding_model
        self.device = device
        self.query_prefix = query_prefix

        # 數據文件路徑
        self.metadata_file = self.storage_path / "episodes.json"
        self.embeddings_file = self.storage_path / "embeddings.npy"

        # 內存緩存
        self._episodes: dict[str, Episode] = {}
        self._embeddings: np.ndarray | None = None
        self._id_to_index: dict[str, int] = {}

        # 記憶圖（Phase 1.5）
        self.graph = MemoryGraph(storage_path)

        # 追蹤最後一個 Episode（全域 + 每個 session）
        self._last_episode_id: str | None = None
        self._last_episode_by_session: dict[str, str] = {}

        # 延遲加載 embedding 生成器
        self._embedder = None
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 公共 API
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


        # 載入現有數據
        self._load()

        logger.info(f"[EpisodicMemory] 初始化完成 - 已載入 {len(self._episodes)} 個情節")

    def write(self, episode: Episode) -> str:
        """
        寫入新的情節記憶。
        
        Args:
            episode: Episode 對象
        
        Returns:
            episode_id: 新建的情節 ID
        """
        # 生成嵌入向量（如果未提供）
        if not episode.embedding:
            episode.embedding = self._generate_embedding(episode.content_summary)

        # 存儲到內存
        self._episodes[episode.id] = episode

        # 更新向量索引
        self._update_embeddings(episode)

        # 更新圖結構
        self._update_graph(episode)

        # 持久化
        self._save()

        logger.debug(f"[EpisodicMemory] 寫入情節: {episode.id}")
        return episode.id

    def get(self, episode_id: str) -> Episode | None:
        """根據 ID 獲取情節"""
        return self._episodes.get(episode_id)

    def search(
        self,
        query_embedding: list[float],
        top_k: int = 10,
        min_similarity: float = 0.3,
    ) -> list[tuple[Episode, float]]:
        """
        向量相似度搜索。
        
        Args:
            query_embedding: 查詢向量
            top_k: 返回數量
            min_similarity: 最小相似度閾值 (0-1)，低於此值的結果會被過濾
        
        Returns:
            (Episode, similarity_score) 元組列表
        """
        if self._embeddings is None or len(self._episodes) == 0:
            return []

        # Stored vectors are normalized on write/load, so the dot product is
        # cosine similarity. A zero query has no defined direction.
        query_vec = self._normalize_vector(query_embedding)
        if not np.any(query_vec):
            return []

        similarities = np.dot(self._embeddings, query_vec)
        top_indices = np.argsort(similarities)[::-1][:top_k]

        # 轉換為 (Episode, similarity) 元組，並過濾低相似度結果
        results = []
        for idx in top_indices:
            similarity = float(similarities[idx])

            # 過濾低相似度結果
            if similarity < min_similarity:
                continue

            ep_id = self._index_to_id(idx)
            if ep_id and ep_id in self._episodes:
                results.append((self._episodes[ep_id], similarity))

        logger.debug(
            f"[EpisodicMemory] 檢索完成 - "
            f"找到 {len(results)}/{top_k} 個相似度 >= {min_similarity} 的結果"
        )
        return results

    def count(self) -> int:
        """返回情節總數"""
        return len(self._episodes)

    def clear(self):
        """清空所有情節記憶（測試用）"""
        self._episodes.clear()
        self._embeddings = None
        self._id_to_index.clear()
        self._last_episode_id = None
        self._last_episode_by_session.clear()

        # 清空圖結構
        self.graph.clear()

        self._save()

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 內部方法
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _generate_embedding(self, text: str) -> list[float]:
        """生成文本嵌入向量（存儲用，不加 query prefix）"""
        if self._embedder is None:
            # 設置 Hugging Face Token（如果存在）
            from dotenv import load_dotenv
            load_dotenv()
            hf_token = os.getenv('HF_TOKEN')
            if hf_token:
                os.environ['HF_TOKEN'] = hf_token

            from sentence_transformers import SentenceTransformer
            self._embedder = SentenceTransformer(
                self.embedding_model,
                device=self.device,
            )

        embedding = self._embedder.encode(text, convert_to_numpy=True)
        return self._normalize_vector(embedding).tolist()

    def generate_query_embedding(self, text: str) -> list[float]:
        """生成查詢嵌入向量（加 query prefix，用於 retrieval 場景）"""
        prefixed_text = self.query_prefix + text if self.query_prefix else text
        return self._generate_embedding(prefixed_text)

    def _update_embeddings(self, episode: Episode):
        """更新向量索引"""
        new_vec = self._normalize_vector(episode.embedding)
        episode.embedding = new_vec.tolist()

        if self._embeddings is None:
            self._embeddings = new_vec.reshape(1, -1)
            self._id_to_index[episode.id] = 0
        else:
            idx = len(self._embeddings)
            self._embeddings = np.vstack([self._embeddings, new_vec])
            self._id_to_index[episode.id] = idx

    @staticmethod
    def _normalize_vector(vector: list[float] | np.ndarray) -> np.ndarray:
        """Return an L2-normalized float32 vector, preserving zero vectors."""
        normalized = np.asarray(vector, dtype=np.float32)
        norm = float(np.linalg.norm(normalized))
        if norm == 0.0:
            return np.zeros_like(normalized, dtype=np.float32)
        return normalized / norm

    @classmethod
    def _normalize_matrix(cls, matrix: np.ndarray) -> tuple[np.ndarray, bool]:
        """Normalize persisted rows and report whether migration was needed."""
        normalized = np.asarray(matrix, dtype=np.float32)
        if normalized.ndim != 2:
            raise ValueError("embeddings.npy must contain a 2D matrix")

        norms = np.linalg.norm(normalized, axis=1, keepdims=True)
        nonzero_rows = norms[:, 0] > 0.0
        migrated = bool(
            np.any(nonzero_rows)
            and not np.allclose(norms[nonzero_rows], 1.0, rtol=1e-5, atol=1e-6)
        )
        normalized = normalized.copy()
        normalized[nonzero_rows] /= norms[nonzero_rows]
        normalized[~nonzero_rows] = 0.0
        return normalized, migrated

    def _index_to_id(self, index: int) -> str | None:
        """索引 -> Episode ID"""
        for ep_id, idx in self._id_to_index.items():
            if idx == index:
                return ep_id
        return None

    def _load(self):
        """從磁盤載入數據"""
        if self.metadata_file.exists():
            with open(self.metadata_file, encoding="utf-8") as f:
                data = json.load(f)
                for ep_dict in data:
                    ep = Episode.from_dict(ep_dict)
                    self._episodes[ep.id] = ep

        if self.embeddings_file.exists():
            loaded_embeddings = np.load(self.embeddings_file)

            # 維度相容性檢查：若已知模型維度與載入資料不符，清除向量索引
            _known_dimensions = {
                "all-MiniLM-L6-v2": 384,
                "BAAI/bge-small-zh-v1.5": 512,
            }
            expected_dim = _known_dimensions.get(self.embedding_model)
            if (
                expected_dim is not None
                and loaded_embeddings.ndim == 2
                and loaded_embeddings.shape[1] != expected_dim
            ):
                logger.warning(
                    f"[EpisodicMemory] embeddings.npy 維度 {loaded_embeddings.shape[1]} "
                    f"與目前模型 '{self.embedding_model}' 期望的 {expected_dim} 不符。"
                    "向量索引已清除，需重新對語料生成嵌入。"
                )
                # 清除不相容的向量索引和磁碟檔案，保留元數據
                self._embeddings = None
                self._id_to_index.clear()
                if self.embeddings_file.exists():
                    self.embeddings_file.unlink()
            else:
                self._embeddings, migrated = self._normalize_matrix(loaded_embeddings)
                if migrated:
                    np.save(self.embeddings_file, self._embeddings)
                    logger.info(
                        "[EpisodicMemory] Migrated embeddings.npy to L2-normalized vectors"
                    )

                metadata_migrated = False
                # 重建索引映射
                for i, ep in enumerate(self._episodes.values()):
                    self._id_to_index[ep.id] = i
                    if i >= len(self._embeddings):
                        break

                    normalized_embedding = self._embeddings[i].tolist()
                    metadata_embedding = np.asarray(ep.embedding, dtype=np.float32)
                    embeddings_match = (
                        metadata_embedding.shape == self._embeddings[i].shape
                        and np.allclose(
                            metadata_embedding,
                            self._embeddings[i],
                            rtol=1e-5,
                            atol=1e-6,
                        )
                    )
                    if not embeddings_match:
                        ep.embedding = normalized_embedding
                        metadata_migrated = True

                if metadata_migrated:
                    with open(self.metadata_file, "w", encoding="utf-8") as f:
                        data = [ep.to_dict() for ep in self._episodes.values()]
                        json.dump(data, f, ensure_ascii=False, indent=2)
                    logger.info(
                        "[EpisodicMemory] Migrated episode metadata to L2-normalized vectors"
                    )

        # 重建 per-session 最後 episode 追蹤
        from collections import defaultdict
        session_episodes: dict[str, list[Episode]] = defaultdict(list)
        for ep in self._episodes.values():
            session_episodes[ep.session_id].append(ep)
        for session_id, eps in session_episodes.items():
            eps.sort(key=lambda e: e.timestamp)
            self._last_episode_by_session[session_id] = eps[-1].id

    def _save(self):
        """持久化到磁盤"""
        # 保存元數據
        with open(self.metadata_file, "w", encoding="utf-8") as f:
            data = [ep.to_dict() for ep in self._episodes.values()]
            json.dump(data, f, ensure_ascii=False, indent=2)

        # 保存向量
        if self._embeddings is not None:
            np.save(self.embeddings_file, self._embeddings)

        # 保存圖結構
        self.graph.save()

    def _update_graph(self, episode: Episode):
        """
        更新圖結構(Phase 1.5)。

        自動維護記憶圖：
        1. 添加節點
        2. 創建 PRECEDES 邊（只連接同一 session 的前一個 Episode）
        """
        # 添加節點
        if not self.graph.has_node(episode.id):
            self.graph.add_episode_node(episode)

        # 創建 PRECEDES 邊（使用 per-session 追蹤）
        last_id = self._last_episode_by_session.get(episode.session_id)
        if last_id is not None:
            last_ep = self._episodes.get(last_id)
            if last_ep:
                time_delta = (episode.timestamp - last_ep.timestamp).total_seconds()
                self.graph.add_precedes_edge(last_id, episode.id, time_delta)

        # 更新此 session 最後一個 Episode
        self._last_episode_id = episode.id
        self._last_episode_by_session[episode.session_id] = episode.id
