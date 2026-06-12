"""
EpisodicMemory - 情節記憶存儲

使用 NumPy 向量矩陣 + JSON 元數據存儲。

Phase 1 實作：
- 基本 CRUD 操作
- NumPy 精確餘弦相似度檢索
- JSON 元數據持久化
"""

from __future__ import annotations

import csv
import io
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
        max_episodes: int | None = None,
    ):
        self.storage_path = Path(storage_path)
        self.storage_path.mkdir(parents=True, exist_ok=True)

        self.embedding_model = embedding_model
        self.device = device
        self.query_prefix = query_prefix
        self.max_episodes = max_episodes  # None 代表無限制

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

        容量控制：若設定了 max_episodes 且已達上限，最舊的情節會被移除。

        Args:
            episode: Episode 對象

        Returns:
            episode_id: 新建的情節 ID
        """
        # 容量檢查：超過上限時刪除最舊的情節
        if self.max_episodes is not None and len(self._episodes) >= self.max_episodes:
            self._evict_oldest()

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

    def mark_as_dreamed(self, episode_ids: list[str]) -> int:
        """
        將指定的 Episode 標記為已處理（is_dreamed=True）並持久化。

        供 KnowledgeDistillation / LiDERReplay 等外部模組呼叫，
        取代直接存取 _save() 私有方法。

        Args:
            episode_ids: 要標記的 Episode ID 列表

        Returns:
            實際標記的數量（排除不存在的 ID）
        """
        marked = 0
        for ep_id in episode_ids:
            ep = self._episodes.get(ep_id)
            if ep is not None and not ep.is_dreamed:
                ep.is_dreamed = True
                marked += 1

        if marked > 0:
            self._save()
            logger.debug(
                f"[EpisodicMemory] 標記 {marked}/{len(episode_ids)} 個 Episode 為 is_dreamed"
            )

        return marked

    def update_salience(self, episode_id: str, new_salience: float) -> bool:
        """
        更新指定 Episode 的顯著性分數並持久化。

        供 LiDERReplay 等外部模組呼叫，取代直接修改物件屬性。

        Args:
            episode_id: Episode ID
            new_salience: 新的顯著性分數 (0-1)

        Returns:
            True 若成功更新，False 若 Episode 不存在
        """
        ep = self._episodes.get(episode_id)
        if ep is None:
            return False

        ep.salience_score = min(1.0, max(0.0, new_salience))
        self._save()
        return True

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
    # 公共 CRUD API（Issue #8）
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def list_episodes(
        self,
        session_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
        sort_by: str = "timestamp",
        ascending: bool = False,
    ) -> list[Episode]:
        """
        列出情節，支援分頁和 session 篩選。

        Args:
            session_id: 若提供，只返回該 session 的情節
            limit: 每頁最多返回數量（最大 200）
            offset: 跳過的筆數（分頁起始）
            sort_by: 排序欄位，"timestamp" 或 "salience"
            ascending: 是否升序排列（預設降序，最新/最高先）

        Returns:
            Episode 列表
        """
        limit = min(limit, 200)

        episodes = list(self._episodes.values())

        # session 篩選
        if session_id is not None:
            episodes = [ep for ep in episodes if ep.session_id == session_id]

        # 排序
        if sort_by == "salience":
            episodes.sort(key=lambda ep: ep.salience_score, reverse=not ascending)
        else:
            # 預設以 timestamp 排序
            episodes.sort(key=lambda ep: ep.timestamp, reverse=not ascending)

        # 分頁
        return episodes[offset: offset + limit]

    def delete_episode(self, episode_id: str) -> bool:
        """
        刪除單一情節，同步更新 vectors/graph/metadata。

        刪除後：
        - _episodes dict 移除條目
        - _embeddings ndarray 重建（移除對應行）
        - _id_to_index mapping 重建
        - graph 移除節點及其所有邊
        - _last_episode_id / _last_episode_by_session 回溯

        Args:
            episode_id: 要刪除的情節 ID

        Returns:
            True 若刪除成功，False 若情節不存在
        """
        if episode_id not in self._episodes:
            logger.warning(f"[EpisodicMemory] 情節不存在，無法刪除: {episode_id}")
            return False

        episode = self._episodes[episode_id]

        # 1. 從 _episodes 移除
        del self._episodes[episode_id]

        # 2. 重建向量索引
        self._rebuild_embeddings()

        # 3. 從圖移除節點（NetworkX 自動移除相連的邊）
        if self.graph.has_node(episode_id):
            self.graph.graph.remove_node(episode_id)

        # 4. 回溯 _last_episode_id
        if self._last_episode_id == episode_id:
            self._last_episode_id = self._find_latest_episode_id()

        # 5. 回溯 _last_episode_by_session
        session_id = episode.session_id
        if self._last_episode_by_session.get(session_id) == episode_id:
            latest_in_session = self._find_latest_episode_id(session_id=session_id)
            if latest_in_session is not None:
                self._last_episode_by_session[session_id] = latest_in_session
            else:
                del self._last_episode_by_session[session_id]

        # 持久化
        self._save()

        logger.info(f"[EpisodicMemory] 已刪除情節: {episode_id}")
        return True

    def delete_session(self, session_id: str) -> int:
        """
        刪除整個 session 的所有情節，回傳刪除數量。

        Args:
            session_id: 要刪除的 session ID

        Returns:
            刪除的情節數量
        """
        target_ids = [
            ep_id
            for ep_id, ep in self._episodes.items()
            if ep.session_id == session_id
        ]

        if not target_ids:
            logger.info(f"[EpisodicMemory] Session 無情節可刪除: {session_id}")
            return 0

        # 批次從 _episodes 移除
        for ep_id in target_ids:
            del self._episodes[ep_id]

        # 重建向量索引（只做一次，效率更好）
        self._rebuild_embeddings()

        # 從圖移除所有節點
        for ep_id in target_ids:
            if self.graph.has_node(ep_id):
                self.graph.graph.remove_node(ep_id)

        # 清除 session tracking
        if self._last_episode_by_session.get(session_id) in target_ids:
            del self._last_episode_by_session[session_id]

        # 回溯全域 _last_episode_id
        if self._last_episode_id in target_ids:
            self._last_episode_id = self._find_latest_episode_id()

        # 持久化
        self._save()

        logger.info(
            f"[EpisodicMemory] 已刪除 session {session_id} 的 {len(target_ids)} 個情節"
        )
        return len(target_ids)

    def export_episodes(
        self,
        session_id: str | None = None,
        format: str = "json",
    ) -> str:
        """
        匯出情節為字串。

        Args:
            session_id: 若提供，只匯出該 session 的情節
            format: "json" 或 "csv"

        Returns:
            匯出內容字串

        Raises:
            ValueError: 若 format 不支援
        """
        if format not in ("json", "csv"):
            raise ValueError(f"不支援的匯出格式: {format!r}，請使用 'json' 或 'csv'")

        # 直接從 _episodes 取得全部資料，不走分頁的 list_episodes 避免截斷
        episodes = list(self._episodes.values())
        if session_id:
            episodes = [ep for ep in episodes if ep.session_id == session_id]
        episodes.sort(key=lambda e: e.timestamp)

        if format == "json":
            return self._export_json(episodes)
        else:
            return self._export_csv(episodes)

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 內部方法
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _export_json(self, episodes: list[Episode]) -> str:
        """匯出為 JSON 字串（不含 embedding 向量，避免過大）"""
        records = []
        for ep in episodes:
            d = ep.to_dict()
            d.pop("embedding", None)  # 向量太大，匯出時省略
            records.append(d)
        return json.dumps(records, ensure_ascii=False, indent=2)

    def _export_csv(self, episodes: list[Episode]) -> str:
        """匯出為 CSV 字串"""
        output = io.StringIO()
        fieldnames = [
            "id", "session_id", "timestamp", "salience_score",
            "da_weight", "ht_weight", "is_dreamed",
            "content_summary", "user_input", "agent_response",
        ]
        writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for ep in episodes:
            d = ep.to_dict()
            d.pop("embedding", None)
            d.pop("metadata", None)
            writer.writerow(d)
        return output.getvalue()

    def _rebuild_embeddings(self) -> None:
        """
        重建 _embeddings 矩陣和 _id_to_index mapping。

        在刪除操作後呼叫。遍歷目前 _episodes 的順序，
        重新堆疊向量並重建索引。
        """
        if not self._episodes:
            self._embeddings = None
            self._id_to_index = {}
            return

        vectors = []
        new_id_to_index: dict[str, int] = {}

        for idx, (ep_id, ep) in enumerate(self._episodes.items()):
            if ep.embedding:
                vec = self._normalize_vector(ep.embedding)
                vectors.append(vec)
                new_id_to_index[ep_id] = idx
            else:
                # 若某筆沒有 embedding（極少發生），用零向量佔位
                dim = len(next(iter(self._episodes.values())).embedding) if self._episodes else 384
                vectors.append(np.zeros(dim, dtype=np.float32))
                new_id_to_index[ep_id] = idx

        self._embeddings = np.vstack(vectors).astype(np.float32)
        self._id_to_index = new_id_to_index

    def _evict_oldest(self) -> None:
        """
        移除最舊的一筆情節（容量滿時呼叫）。
        不呼叫 _save()，由上層 write() 統一持久化。
        """
        if not self._episodes:
            return

        oldest_id = min(self._episodes, key=lambda eid: self._episodes[eid].timestamp)
        oldest_ep = self._episodes[oldest_id]

        del self._episodes[oldest_id]
        self._rebuild_embeddings()

        if self.graph.has_node(oldest_id):
            self.graph.graph.remove_node(oldest_id)

        # 回溯追蹤欄位
        if self._last_episode_id == oldest_id:
            self._last_episode_id = self._find_latest_episode_id()

        session_id = oldest_ep.session_id
        if self._last_episode_by_session.get(session_id) == oldest_id:
            latest_in_session = self._find_latest_episode_id(session_id=session_id)
            if latest_in_session is not None:
                self._last_episode_by_session[session_id] = latest_in_session
            else:
                self._last_episode_by_session.pop(session_id, None)

        logger.info(f"[EpisodicMemory] 容量已滿，移除最舊情節: {oldest_id}")

    def _find_latest_episode_id(self, session_id: str | None = None) -> str | None:
        """
        在目前 _episodes 中尋找 timestamp 最新的那筆 ID。

        Args:
            session_id: 若提供，只在該 session 內尋找

        Returns:
            最新 episode ID，若無則 None
        """
        candidates = (
            {eid: ep for eid, ep in self._episodes.items() if ep.session_id == session_id}
            if session_id is not None
            else self._episodes
        )

        if not candidates:
            return None

        return max(candidates, key=lambda eid: candidates[eid].timestamp)

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

        # 重建 _last_episode_by_session（從已載入的情節中推算）
        self._rebuild_session_tracking()

    def _rebuild_session_tracking(self) -> None:
        """
        從 _episodes 重建 _last_episode_by_session 和 _last_episode_id。
        在 _load() 後呼叫，確保追蹤狀態與磁盤資料一致。
        """
        self._last_episode_by_session.clear()
        self._last_episode_id = None

        if not self._episodes:
            return

        # 找出每個 session 的最新情節
        session_latest: dict[str, tuple[str, object]] = {}  # session_id -> (ep_id, timestamp)
        for ep_id, ep in self._episodes.items():
            sid = ep.session_id
            if sid not in session_latest or ep.timestamp > session_latest[sid][1]:
                session_latest[sid] = (ep_id, ep.timestamp)

        self._last_episode_by_session = {sid: data[0] for sid, data in session_latest.items()}

        # 全域最新
        self._last_episode_id = self._find_latest_episode_id()

    def _save(self):
        """保存元數據、向量和圖到磁盤"""
        # 保存元數據
        with open(self.metadata_file, "w", encoding="utf-8") as f:
            data = [ep.to_dict() for ep in self._episodes.values()]
            json.dump(data, f, ensure_ascii=False, indent=2)

        # 保存向量（若為空則清除殘留檔案）
        if self._embeddings is not None:
            np.save(self.embeddings_file, self._embeddings)
        elif self.embeddings_file.exists():
            self.embeddings_file.unlink()

        # 保存圖結構
        self.graph.save()

    def _update_graph(self, episode: Episode):
        """
        更新圖結構(Phase 1.5)。

        自動維護記憶圖：
        1. 添加節點
        2. 創建 PRECEDES 邊（與前一個同 session 的 Episode 的時序關係）
        """
        # 添加節點
        if not self.graph.has_node(episode.id):
            self.graph.add_episode_node(episode)

        # 創建 PRECEDES 邊（與同 session 的前一個 Episode）
        prev_id = self._last_episode_by_session.get(episode.session_id)
        if prev_id is not None and prev_id != episode.id:
            last_ep = self._episodes.get(prev_id)
            if last_ep:
                time_delta = (episode.timestamp - last_ep.timestamp).total_seconds()
                self.graph.add_precedes_edge(prev_id, episode.id, time_delta)

        # 更新追蹤欄位
        self._last_episode_id = episode.id
        self._last_episode_by_session[episode.session_id] = episode.id
