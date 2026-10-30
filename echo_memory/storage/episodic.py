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
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from echo_memory.schemas.episode import Episode
from echo_memory.storage.graph import MemoryGraph
from echo_memory.storage.lexical import LexicalIndex, build_docs
from echo_memory.storage.atomic import replace_with_retry
from echo_memory.storage.locking import storage_lock, temporary_sibling

logger = logging.getLogger("echo_memory.episodic")


class EmbeddingUnavailable(RuntimeError):
    """Embedding 後端暫時不可用（逾時 / 連線失敗 / 回應異常）。"""


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
        query_prefix: str = "",
        max_episodes: int | None = None,
        lock_path: str | Path | None = None,
        embedding_backend: str = "sentence-transformers",
        ollama_base_url: str = "http://localhost:11434",
        query_timeout: float | None = None,
        write_timeout: float | None = None,
        warmup_timeout: float | None = None,
    ):
        from echo_memory.config import get_config

        cfg = get_config()
        self.query_timeout = cfg.embedding_query_timeout if query_timeout is None else query_timeout
        self.write_timeout = cfg.embedding_write_timeout if write_timeout is None else write_timeout
        self.warmup_timeout = cfg.embedding_warmup_timeout if warmup_timeout is None else warmup_timeout
        self.keep_alive = cfg.embedding_keep_alive
        # 降級狀態：最近一次查詢 embedding 是否失敗（成功即自動恢復）
        self.embedding_degraded = False
        self.last_embedding_error = ""
        self._warmup_thread: threading.Thread | None = None
        self._timeout_local = threading.local()
        self.storage_path = Path(storage_path)
        self.storage_path.mkdir(parents=True, exist_ok=True)

        self.embedding_model = embedding_model
        self.device = device
        self.query_prefix = query_prefix
        self.max_episodes = max_episodes  # None 代表無限制
        self.lock_path = Path(lock_path) if lock_path is not None else self.storage_path
        self.embedding_backend = embedding_backend
        self.ollama_base_url = ollama_base_url

        # 數據文件路徑
        self.metadata_file = self.storage_path / "episodes.json"
        self.embeddings_file = self.storage_path / "embeddings.npy"
        self.index_manifest_file = self.storage_path / "embedding_index.json"

        # 同程序讀寫保護：只保護記憶體內狀態（CPU 操作），絕不跨網路呼叫或磁碟讀取持有。
        # 鎖序固定為 storage_lock -> _mem_lock；讀者只拿 _mem_lock。
        self._mem_lock = threading.RLock()

        # 內存緩存
        self._episodes: dict[str, Episode] = {}
        self._embeddings: np.ndarray | None = None
        self._id_to_index: dict[str, int] = {}

        # Lexical（BM25）索引：僅存記憶體，由 episodes 內容簽章懶重建，不持久化
        self._lexical = LexicalIndex()

        # 記憶圖（Phase 1.5）
        self.graph = MemoryGraph(storage_path)

        # 追蹤最後一個 Episode（全域 + 每個 session）
        self._last_episode_id: str | None = None
        self._last_episode_by_session: dict[str, str] = {}

        # 延遲加載 embedding 生成器
        self._embedder = None
        self._index_rebuild_required = False
        self._snapshot_upgrade_required = False

        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # 公共 API
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

        # 載入現有數據
        with storage_lock(self.lock_path):
            self._load(reset=True)
            if self._snapshot_upgrade_required:
                self._save_unlocked()

        logger.info(f"[EpisodicMemory] 初始化完成 - 已載入 {len(self._episodes)} 個情節")

    def prepare_embedding(self, episode: Episode) -> None:
        """
        在鎖外為 episode 生成 embedding（網路呼叫）。

        成功則填入 episode.embedding；後端不可用時保持空向量並標記
        metadata["embedding_pending"]，由 write() 在鎖內以現有索引維度補零向量佔位，
        之後由 backfill_pending_embeddings 補齊。已有 embedding 或已標記待補則不動作。
        """
        if episode.embedding or episode.metadata.get("embedding_pending"):
            return
        try:
            with self._embedding_timeout(self.write_timeout):
                episode.embedding = self._generate_embedding(episode.content_summary)
        except EmbeddingUnavailable as e:
            logger.warning(f"[EpisodicMemory] embedding 不可用，先存 episode 待補: {e}")
            episode.metadata["embedding_pending"] = True

    def write(self, episode: Episode) -> str:
        """
        寫入新的情節記憶。

        容量控制：若設定了 max_episodes 且已達上限，最舊的情節會被移除。

        Args:
            episode: Episode 對象

        Returns:
            episode_id: 新建的情節 ID
        """
        # embedding 內容不依賴索引狀態：網路呼叫放在任何鎖之外
        # （engine.store_episode 會先呼叫過，這裡對已備妥的 episode 為 no-op）
        self.prepare_embedding(episode)
        reindex_cache = self._precompute_reindex_cache()

        with storage_lock(self.lock_path):
            self._load(reset=True)
            self._reindex_if_required(reindex_cache)

            with self._mem_lock:
                # 容量檢查：超過上限時刪除最舊的情節
                if self.max_episodes is not None and len(self._episodes) >= self.max_episodes:
                    self._evict_oldest()

                # 鎖外沒算出 embedding（後端不可用）：每輪對話都必須存成 episode，
                # 以零向量佔位（相似度恆為 0，不會被召回），之後由
                # backfill_pending_embeddings 補齊
                if not episode.embedding:
                    placeholder = self._placeholder_embedding()
                    if placeholder is None:
                        raise EmbeddingUnavailable(
                            "embedding 不可用且無法決定佔位向量維度"
                        )
                    episode.embedding = placeholder
                    episode.metadata["embedding_pending"] = True

                # 存儲到內存
                self._episodes[episode.id] = episode

                # 更新向量索引
                self._update_embeddings(episode)

                # 更新圖結構
                self._update_graph(episode)

            # 持久化：episode 檔案已落地後圖存檔失敗不得讓 write 看起來失敗
            # （重試會重複 episode、store_episode 後續步驟會被跳過）
            self._save_unlocked(tolerate_graph_failure=True)

        logger.debug(f"[EpisodicMemory] 寫入情節: {episode.id}")
        return episode.id

    def get(self, episode_id: str) -> Episode | None:
        """根據 ID 獲取情節"""
        with self._mem_lock:
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
        # 先確保索引就緒（可能取 storage_lock），再拿 _mem_lock，維持鎖序
        self._ensure_index_ready()
        # Stored vectors are normalized on write/load, so the dot product is
        # cosine similarity. A zero query has no defined direction.
        query_vec = self._normalize_vector(query_embedding)
        if not np.any(query_vec):
            return []

        with self._mem_lock:
            if self._embeddings is None or len(self._episodes) == 0:
                return []
            # 索引仍待重建（檢查與取鎖之間被其他來源重載）或查詢維度不合：回空而非拋例外
            if self._index_rebuild_required or query_vec.shape[0] != self._embeddings.shape[1]:
                return []

            similarities = np.dot(self._embeddings, query_vec)
            top_indices = np.argsort(similarities)[::-1][:top_k]
            idx_to_id = {idx: ep_id for ep_id, idx in self._id_to_index.items()}

            # 轉換為 (Episode, similarity) 元組，並過濾低相似度結果
            results = []
            for idx in top_indices:
                similarity = float(similarities[idx])

                # 過濾低相似度結果
                if similarity < min_similarity:
                    continue

                ep_id = idx_to_id.get(int(idx))
                if ep_id and ep_id in self._episodes:
                    results.append((self._episodes[ep_id], similarity))

        logger.debug(
            f"[EpisodicMemory] 檢索完成 - "
            f"找到 {len(results)}/{top_k} 個相似度 >= {min_similarity} 的結果"
        )
        return results

    def lexical_search(
        self,
        query: str,
        top_n: int = 20,
        min_score: float = 0.0,
        rel_min: float = 0.0,
        max_df: float = 1.0,
        min_coverage: float = 0.0,
    ) -> list[tuple[Episode, float]]:
        """
        Lexical（BM25）檢索，回傳 (Episode, bm25_score)，分數遞減。

        並行模型：只在 _mem_lock 內取文件文字快照（純記憶體 CPU 操作）；
        索引同步與搜尋在 LexicalIndex 自己的鎖內以該快照進行，不做網路呼叫或磁碟 I/O。
        """
        with self._mem_lock:
            episodes = dict(self._episodes)
        docs = build_docs(episodes.values())
        hits = self._lexical.query(
            docs, query, top_n, min_score, rel_min, max_df, min_coverage,
        )
        return [(episodes[eid], score) for eid, score in hits if eid in episodes]

    def similarity_to_query(
        self,
        query_embedding: list[float],
        episode_id: str,
    ) -> float | None:
        """
        計算單一 episode 與查詢向量的 cosine similarity。

        用於圖譜展開時驗證候選 episode 的語意相關度。

        Args:
            query_embedding: 查詢向量
            episode_id: 目標 episode ID

        Returns:
            cosine similarity (0-1)，找不到 episode 時回傳 None
        """
        self._ensure_index_ready()
        query_vec = self._normalize_vector(query_embedding)
        if not np.any(query_vec):
            return None

        with self._mem_lock:
            if self._embeddings is None or episode_id not in self._episodes:
                return None
            if self._index_rebuild_required or query_vec.shape[0] != self._embeddings.shape[1]:
                return None

            idx = self._id_to_index.get(episode_id)
            if idx is None:
                return None

            return float(np.dot(self._embeddings[idx], query_vec))

    def count(self) -> int:
        """返回情節總數"""
        with self._mem_lock:
            return len(self._episodes)

    def count_pending(self, min_salience: float) -> int:
        """
        統計待做夢的情節數：未 is_dreamed 且 salience_score >= min_salience。

        只在 _mem_lock 內掃記憶體字典，不取 storage_lock、不碰磁碟，可被排程每分鐘呼叫。
        """
        with self._mem_lock:
            return sum(
                1 for ep in self._episodes.values()
                if not ep.is_dreamed and ep.salience_score >= min_salience
            )

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
        with storage_lock(self.lock_path):
            self._load(reset=True)
            marked = 0
            with self._mem_lock:
                for ep_id in episode_ids:
                    ep = self._episodes.get(ep_id)
                    if ep is not None and not ep.is_dreamed:
                        ep.is_dreamed = True
                        marked += 1

            if marked > 0:
                self._save_unlocked()
                logger.debug(
                    f"[EpisodicMemory] 標記 {marked}/{len(episode_ids)} 個 Episode 為 is_dreamed"
                )

            return marked

    def unscanned_ids_below(self, max_salience: float) -> list[str]:
        """
        取得從未被 dream 週期評估、未 is_dreamed 且 salience_score < max_salience 的 Episode ID。

        只在 _mem_lock 內掃記憶體字典，不取 storage_lock、不碰磁碟。
        """
        with self._mem_lock:
            return [
                ep.id for ep in self._episodes.values()
                if not ep.is_dreamed
                and ep.dream_scanned_at is None
                and ep.salience_score < max_salience
            ]

    def mark_as_scanned(
        self,
        episode_ids: list[str],
        at: datetime | None = None,
        overwrite: bool = True,
    ) -> int:
        """
        將指定的 Episode 標記為 dream 週期已評估（寫入 dream_scanned_at）並持久化。

        不改動 is_dreamed，因此不影響蒸餾候選。

        Args:
            episode_ids: 要標記的 Episode ID 列表
            at: 評估時間（預設現在，UTC）
            overwrite: False 時只標記尚無 dream_scanned_at 的 Episode

        Returns:
            實際標記的數量（排除不存在的 ID）
        """
        if not episode_ids:
            return 0
        stamp = at or datetime.now(timezone.utc)
        with storage_lock(self.lock_path):
            self._load(reset=True)
            marked = 0
            with self._mem_lock:
                for ep_id in episode_ids:
                    ep = self._episodes.get(ep_id)
                    if ep is not None and (overwrite or ep.dream_scanned_at is None):
                        ep.dream_scanned_at = stamp
                        marked += 1

            if marked > 0:
                self._save_unlocked()
                logger.debug(
                    f"[EpisodicMemory] 標記 {marked}/{len(episode_ids)} 個 Episode 為 dream_scanned"
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
        with storage_lock(self.lock_path):
            self._load(reset=True)
            with self._mem_lock:
                ep = self._episodes.get(episode_id)
                if ep is None:
                    return False
                ep.salience_score = min(1.0, max(0.0, new_salience))
            self._save_unlocked()
            return True

    def update_summary(self, episode_id: str, new_summary: str) -> bool:
        """Update an episode summary and its embedding against the latest snapshot."""
        # embedding 只取決於新摘要：網路呼叫在鎖外；episode 不存在時白算一次可接受
        new_embedding = self._generate_embedding(new_summary)
        reindex_cache = self._precompute_reindex_cache()
        with storage_lock(self.lock_path):
            self._load(reset=True)
            self._reindex_if_required(reindex_cache)
            with self._mem_lock:
                ep = self._episodes.get(episode_id)
                if ep is None:
                    return False

                ep.content_summary = new_summary
                ep.embedding = new_embedding
                self._rebuild_embeddings()
            self._save_unlocked()
            return True

    def mask_agent_content(self, episode_id: str, new_summary: str) -> bool:
        """遮蔽 Episode 的 agent 回覆原文（M10 級聯遮蔽用）。

        agent_response 清空、content_summary 換成不含 agent 內容的摘要並重算 embedding，
        metadata 記 agent_masked；user_input 與其餘欄位保留（保留使用者自己的話）。
        回傳 False 代表 Episode 不存在。
        """
        new_embedding = self._generate_embedding(new_summary) if new_summary else None
        reindex_cache = self._precompute_reindex_cache()
        with storage_lock(self.lock_path):
            self._load(reset=True)
            self._reindex_if_required(reindex_cache)
            with self._mem_lock:
                ep = self._episodes.get(episode_id)
                if ep is None:
                    return False
                ep.agent_response = ""
                ep.content_summary = new_summary
                if new_embedding is not None:
                    ep.embedding = new_embedding
                    self._rebuild_embeddings()
                ep.metadata["agent_masked"] = True
            self._save_unlocked()
            return True

    def update_episode_metadata(
        self, episode_id: str, updates: dict[str, Any]
    ) -> bool:
        """
        合併更新指定 Episode 的 metadata 並持久化。

        不影響 embedding 和索引，僅更新 metadata dict。

        Args:
            episode_id: Episode ID
            updates: 要合併進 metadata 的 key-value 對

        Returns:
            True 若成功更新，False 若 Episode 不存在
        """
        with storage_lock(self.lock_path):
            self._load(reset=True)
            with self._mem_lock:
                ep = self._episodes.get(episode_id)
                if ep is None:
                    return False
                ep.metadata.update(updates)
            self._save_unlocked()
            return True

    def archive_episode(
        self, episode_id: str, archive_salience: float = 0.05
    ) -> bool:
        """
        原子性歸檔 episode：降低 salience + 標記 metadata。

        在單一 storage_lock 內完成，避免 race condition。

        Args:
            episode_id: Episode ID
            archive_salience: 歸檔後的 salience（預設 0.05）

        Returns:
            True 若成功，False 若 Episode 不存在
        """
        with storage_lock(self.lock_path):
            self._load(reset=True)
            with self._mem_lock:
                ep = self._episodes.get(episode_id)
                if ep is None:
                    return False

                ep.salience_score = min(1.0, max(0.0, archive_salience))
                ep.metadata["archived"] = True
                ep.metadata["archived_by"] = "reflection"
            self._save_unlocked()
            return True

    def clear(self):
        """清空所有情節記憶（測試用）"""
        with storage_lock(self.lock_path):
            with self._mem_lock:
                # 以新物件替換而非原地清空，持有舊參照的讀者仍看到完整舊狀態
                self._episodes = {}
                self._embeddings = None
                self._id_to_index = {}
                self._last_episode_id = None
                self._last_episode_by_session = {}

                # 清空圖結構（只清記憶體；檔案由下方 _save_unlocked 以空圖覆寫）
                self.graph.clear_in_memory()

            self._save_unlocked()

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

        with self._mem_lock:
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
        with storage_lock(self.lock_path):
            self._load(reset=True)
            with self._mem_lock:
                deleted = self._delete_episode_unlocked(episode_id)
            # 存檔（磁碟 I/O）在 _mem_lock 之外，仍在 storage_lock 內
            if deleted:
                self._save_unlocked()
            return deleted

    def _delete_episode_unlocked(self, episode_id: str) -> bool:
        if episode_id not in self._episodes:
            logger.warning(f"[EpisodicMemory] 情節不存在，無法刪除: {episode_id}")
            return False

        episode = self._episodes[episode_id]

        # 1. 從 _episodes 移除
        del self._episodes[episode_id]

        # 2. 重建向量索引
        self._rebuild_embeddings()

        # 3. 從圖移除節點（NetworkX 自動移除相連的邊）
        self.graph.remove_episode_node(episode_id)

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

        # 持久化由呼叫端在 _mem_lock 之外執行
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
        with storage_lock(self.lock_path):
            self._load(reset=True)
            with self._mem_lock:
                count = self._delete_session_unlocked(session_id)
            if count:
                self._save_unlocked()
            return count

    def _delete_session_unlocked(self, session_id: str) -> int:
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
            self.graph.remove_episode_node(ep_id)

        # 清除 session tracking
        if self._last_episode_by_session.get(session_id) in target_ids:
            del self._last_episode_by_session[session_id]

        # 回溯全域 _last_episode_id
        if self._last_episode_id in target_ids:
            self._last_episode_id = self._find_latest_episode_id()

        # 持久化由呼叫端在 _mem_lock 之外執行
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
        with self._mem_lock:
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

        self.graph.remove_episode_node(oldest_id)

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

    def _find_latest_episode_id(
        self,
        session_id: str | None = None,
        episodes: dict[str, Episode] | None = None,
    ) -> str | None:
        """
        在目前 _episodes（或指定的 episodes）中尋找 timestamp 最新的那筆 ID。

        Args:
            session_id: 若提供，只在該 session 內尋找
            episodes: 指定來源（載入中尚未發布的快照）；None 用 self._episodes

        Returns:
            最新 episode ID，若無則 None
        """
        source = self._episodes if episodes is None else episodes
        candidates = (
            {eid: ep for eid, ep in source.items() if ep.session_id == session_id}
            if session_id is not None
            else source
        )

        if not candidates:
            return None

        def _aware_ts(eid):
            """確保 timestamp 是 aware datetime（相容舊資料）"""
            ts = candidates[eid].timestamp
            if ts.tzinfo is None:
                from datetime import timezone
                ts = ts.replace(tzinfo=timezone.utc)
            return ts

        return max(candidates, key=_aware_ts)

    @contextmanager
    def _embedding_timeout(self, timeout: float | None):
        """以 thread-local 方式暫時指定 embedding 逾時，不改 _generate_embedding 簽章。"""
        previous = getattr(self._timeout_local, "value", None)
        self._timeout_local.value = timeout
        try:
            yield
        finally:
            self._timeout_local.value = previous

    def _generate_embedding(self, text: str) -> list[float]:
        """生成文本嵌入向量（存儲用，不加 query prefix）"""
        if self.embedding_backend == "ollama":
            return self._generate_embedding_ollama(text)
        return self._generate_embedding_st(text)

    def _ollama_embed_payload(self, text: str) -> dict:
        """組 /api/embed 請求內容；keep_alive 為空字串時不帶，交由 Ollama 預設。"""
        payload: dict = {"model": self.embedding_model, "input": text}
        keep_alive = str(self.keep_alive).strip()
        if keep_alive:
            # 純數字（如 -1、0）以整數傳遞，其餘（如 30m）以字串傳遞
            try:
                payload["keep_alive"] = int(keep_alive)
            except ValueError:
                payload["keep_alive"] = keep_alive
        return payload

    def _generate_embedding_ollama(self, text: str) -> list[float]:
        """透過 Ollama /api/embed 端點生成嵌入向量；失敗拋 EmbeddingUnavailable"""
        import requests

        override = getattr(self._timeout_local, "value", None)
        effective_timeout = self.write_timeout if override is None else override
        try:
            resp = requests.post(
                f"{self.ollama_base_url}/api/embed",
                json=self._ollama_embed_payload(text),
                timeout=effective_timeout,
            )
            resp.raise_for_status()
            embedding = resp.json()["embeddings"][0]
            return self._normalize_vector(embedding).tolist()
        except requests.exceptions.ConnectionError as e:
            raise EmbeddingUnavailable(
                f"無法連線 Ollama ({self.ollama_base_url})。"
                f"請確認 Ollama 已啟動，或將 EMBEDDING_BACKEND 改為 sentence-transformers。"
            ) from e
        except requests.exceptions.Timeout as e:
            raise EmbeddingUnavailable(
                f"Ollama 嵌入請求超時 (model={self.embedding_model}, timeout={effective_timeout}s)。"
            ) from e
        except (requests.exceptions.RequestException, KeyError, IndexError, ValueError) as e:
            raise EmbeddingUnavailable(
                f"Ollama 嵌入失敗 (model={self.embedding_model}): {e}"
            ) from e

    def _placeholder_embedding(self) -> list[float] | None:
        """取得與現有索引同維度的零向量；維度未知時回 None。"""
        if self._embeddings is not None and self._embeddings.ndim == 2:
            return [0.0] * int(self._embeddings.shape[1])
        dims = {
            "all-MiniLM-L6-v2": 384,
            "BAAI/bge-small-zh-v1.5": 512,
            "BAAI/bge-m3": 1024,
            "bge-m3": 1024,
        }
        dim = dims.get(self.embedding_model)
        return [0.0] * dim if dim else None

    def _generate_embedding_st(self, text: str) -> list[float]:
        """透過 sentence-transformers 生成嵌入向量（fallback）"""
        if self._embedder is None:
            try:
                from dotenv import load_dotenv
                load_dotenv()
                hf_token = os.getenv('HF_TOKEN')
                if hf_token:
                    os.environ['HF_TOKEN'] = hf_token
            except ImportError:
                pass

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
        with self._embedding_timeout(self.query_timeout):
            return self._generate_embedding(prefixed_text)

    def try_generate_query_embedding(self, text: str) -> list[float]:
        """
        降級版查詢嵌入：用短逾時，失敗回空 list 並標記 embedding_degraded，
        不拋例外。成功時自動清除降級標記。
        """
        try:
            vec = self.generate_query_embedding(text)
        except Exception as e:  # 檢索路徑不可崩潰
            self.embedding_degraded = True
            self.last_embedding_error = str(e)
            return []
        self.embedding_degraded = False
        self.last_embedding_error = ""
        return vec

    def warmup(self, background: bool = True) -> threading.Thread | bool:
        """
        暖機 embedding 後端（Ollama 載入模型 / sentence-transformers 載入權重）。

        background=True 時於 daemon thread 執行並回傳該 thread；
        否則同步執行並回傳是否成功。失敗只記 log 並標記 degraded，不拋出。
        """
        def _run() -> bool:
            try:
                with self._embedding_timeout(self.warmup_timeout):
                    self._generate_embedding("warmup")
            except Exception as e:
                self.embedding_degraded = True
                self.last_embedding_error = str(e)
                logger.warning(f"[EpisodicMemory] embedding 暖機失敗: {e}")
                return False
            self.embedding_degraded = False
            self.last_embedding_error = ""
            logger.info("[EpisodicMemory] embedding 暖機完成")
            return True

        if not background:
            return _run()
        thread = threading.Thread(target=_run, name="embedding-warmup", daemon=True)
        thread.start()
        self._warmup_thread = thread
        return thread

    def backfill_pending_embeddings(self, limit: int | None = None) -> int:
        """補齊 embedding_pending 的 episode；後端仍不可用就停止。回傳補齊數量。"""
        # 階段 1：鎖內只讀出待補清單（含摘要快照）
        with self._mem_lock:
            pending = [
                (ep.id, ep.content_summary) for ep in self._episodes.values()
                if ep.metadata.get("embedding_pending")
            ]
        if limit is not None:
            pending = pending[:limit]
        if not pending:
            return 0

        # 階段 2：鎖外逐筆算 embedding，後端不可用就停止
        computed: dict[str, tuple[str, list[float]]] = {}
        for ep_id, summary in pending:
            try:
                with self._embedding_timeout(self.write_timeout):
                    computed[ep_id] = (summary, self._generate_embedding(summary))
            except EmbeddingUnavailable as e:
                logger.warning(f"[EpisodicMemory] 補 embedding 中止: {e}")
                break
        if not computed:
            return 0

        # 階段 3：鎖內載入最新快照，只套用「仍存在、仍待補、摘要未變」的 episode
        done = 0
        reindex_cache = self._precompute_reindex_cache()
        with storage_lock(self.lock_path):
            self._load(reset=True)
            self._reindex_if_required(reindex_cache)
            with self._mem_lock:
                for ep_id, (summary, vector) in computed.items():
                    ep = self._episodes.get(ep_id)
                    if (
                        ep is None
                        or not ep.metadata.get("embedding_pending")
                        or ep.content_summary != summary
                    ):
                        continue
                    ep.embedding = vector
                    ep.metadata.pop("embedding_pending", None)
                    done += 1
                if done:
                    self._rebuild_embeddings()
            if done:
                self._save_unlocked()
        return done

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

    def _load(self, reset: bool = False):
        """
        從磁盤載入數據。

        磁碟讀取與解析全部在區域變數完成（不持 _mem_lock），最後以單次替換發布，
        並行讀者只會看到完整的舊狀態或完整的新狀態。
        """
        if reset:
            episodes: dict[str, Episode] = {}
            embeddings: np.ndarray | None = None
            id_to_index: dict[str, int] = {}
            index_rebuild_required = False
            snapshot_upgrade_required = False
            new_graph = self.graph.load_snapshot()
        else:
            with self._mem_lock:
                episodes = dict(self._episodes)
                embeddings = self._embeddings
                id_to_index = dict(self._id_to_index)
                index_rebuild_required = self._index_rebuild_required
                snapshot_upgrade_required = self._snapshot_upgrade_required
            new_graph = None

        if self.metadata_file.exists():
            with open(self.metadata_file, encoding="utf-8") as f:
                data = json.load(f)
                for ep_dict in data:
                    ep = Episode.from_dict(ep_dict)
                    episodes[ep.id] = ep

        if self.embeddings_file.exists():
            loaded_embeddings = np.load(self.embeddings_file)

            embeddings, migrated = self._normalize_matrix(loaded_embeddings)
            manifest_exists = self.index_manifest_file.exists()
            expected_dimensions = {
                "all-MiniLM-L6-v2": 384,
                "BAAI/bge-small-zh-v1.5": 512,
                "BAAI/bge-m3": 1024,
                "bge-m3": 1024,  # Ollama model name
            }
            expected_dim = expected_dimensions.get(self.embedding_model)
            legacy_compatible = (
                len(embeddings) == len(episodes)
                and (expected_dim is None or embeddings.shape[1] == expected_dim)
            )

            if not manifest_exists and legacy_compatible:
                metadata_changed = migrated
                for i, ep in enumerate(episodes.values()):
                    id_to_index[ep.id] = i
                    normalized = embeddings[i].tolist()
                    current = np.asarray(ep.embedding, dtype=np.float32)
                    if current.shape != embeddings[i].shape or not np.allclose(
                        current, embeddings[i]
                    ):
                        ep.embedding = normalized
                        metadata_changed = True
                snapshot_upgrade_required = True
                if metadata_changed:
                    logger.info("[EpisodicMemory] 修復 legacy embedding metadata")
            else:
                for i, ep in enumerate(episodes.values()):
                    if i >= len(embeddings):
                        break
                    id_to_index[ep.id] = i
                index_rebuild_required = not self._manifest_matches_current_index(
                    episodes, embeddings
                )
        elif episodes:
            index_rebuild_required = True

        # 重建 _last_episode_by_session（從已載入的情節中推算）
        last_by_session, last_id = self._compute_session_tracking(episodes)

        # 單次發布：讀者持 _mem_lock 取參照，不會看到半成品
        with self._mem_lock:
            self._episodes = episodes
            self._embeddings = embeddings
            self._id_to_index = id_to_index
            self._index_rebuild_required = index_rebuild_required
            self._snapshot_upgrade_required = snapshot_upgrade_required
            self._last_episode_by_session = last_by_session
            self._last_episode_id = last_id
            if new_graph is not None:
                self.graph.install(new_graph)

        if new_graph is not None:
            self._repair_missing_graph_nodes(episodes)

    _graph_dirty = False

    def _repair_missing_graph_nodes(self, episodes: dict[str, Episode]) -> None:
        """冪等修復：episode 已落地但圖快照缺節點（圖存檔曾失敗）時，補節點與 PRECEDES。

        前驅取同 session 內時間較早的最近一筆。只補缺的，已存在的節點與邊不動。
        修復後標 graph dirty，下一次成功存檔寫回磁碟。
        """
        missing = [ep for ep in episodes.values() if not self.graph.has_node(ep.id)]
        if not missing:
            return
        by_session: dict[str, list[Episode]] = {}
        for ep in episodes.values():
            by_session.setdefault(ep.session_id, []).append(ep)
        for items in by_session.values():
            items.sort(key=lambda e: e.timestamp)
        for ep in sorted(missing, key=lambda e: e.timestamp):
            self.graph.add_episode_node(ep)
        for ep in sorted(missing, key=lambda e: e.timestamp):
            siblings = by_session[ep.session_id]
            idx = next(i for i, e in enumerate(siblings) if e.id == ep.id)
            if idx > 0:
                prev = siblings[idx - 1]
                if self.graph.has_node(prev.id) and not self.graph.has_edge(prev.id, ep.id):
                    self.graph.add_precedes_edge(
                        prev.id, ep.id, (ep.timestamp - prev.timestamp).total_seconds(),
                    )
            if idx + 1 < len(siblings):
                nxt = siblings[idx + 1]
                if self.graph.has_node(nxt.id) and not self.graph.has_edge(ep.id, nxt.id):
                    self.graph.add_precedes_edge(
                        ep.id, nxt.id, (nxt.timestamp - ep.timestamp).total_seconds(),
                    )
        self._graph_dirty = True
        logger.info("[EpisodicMemory] 修復 %d 個缺圖節點的 episode", len(missing))

    def _rebuild_session_tracking(self) -> None:
        """
        從 _episodes 重建 _last_episode_by_session 和 _last_episode_id。
        在 _load() 後呼叫，確保追蹤狀態與磁盤資料一致。
        """
        with self._mem_lock:
            self._last_episode_by_session, self._last_episode_id = (
                self._compute_session_tracking(self._episodes)
            )

    def _compute_session_tracking(
        self, episodes: dict[str, Episode]
    ) -> tuple[dict[str, str], str | None]:
        """由給定的 episodes 推算（每 session 最新 ID, 全域最新 ID），不改動實例狀態。"""
        if not episodes:
            return {}, None

        # 找出每個 session 的最新情節
        session_latest: dict[str, tuple[str, object]] = {}  # session_id -> (ep_id, timestamp)
        for ep_id, ep in episodes.items():
            sid = ep.session_id
            if sid not in session_latest or ep.timestamp > session_latest[sid][1]:
                session_latest[sid] = (ep_id, ep.timestamp)

        by_session = {sid: data[0] for sid, data in session_latest.items()}

        # 全域最新
        return by_session, self._find_latest_episode_id(episodes=episodes)

    def _save(self):
        """保存元數據、向量和圖到磁盤"""
        with storage_lock(self.lock_path):
            self._save_unlocked()

    def _save_unlocked(self, tolerate_graph_failure: bool = False) -> None:
        """在持有 namespace storage lock 時發布完整快照。

        tolerate_graph_failure=True（write 專用）：episode 檔案與 manifest 已寫成功後，
        圖存檔失敗只記 warning 並標 graph dirty，不拋出；圖是整份快照，下一次成功存檔
        自然補上，_load 也會對缺節點的 episode 做冪等修復。
        """
        self.storage_path.mkdir(parents=True, exist_ok=True)
        # 快照在 _mem_lock 內取（純記憶體），檔案寫入在鎖外
        with self._mem_lock:
            data = [ep.to_dict() for ep in self._episodes.values()]
            embeddings = self._embeddings
            ordered_ids = list(self._episodes)

        metadata_tmp = temporary_sibling(self.metadata_file)
        with open(metadata_tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        replace_with_retry(metadata_tmp, self.metadata_file)

        if embeddings is not None:
            embeddings_tmp = temporary_sibling(self.embeddings_file)
            with open(embeddings_tmp, "wb") as f:
                np.save(f, embeddings)
                f.flush()
                os.fsync(f.fileno())
            replace_with_retry(embeddings_tmp, self.embeddings_file)
        elif self.embeddings_file.exists():
            self.embeddings_file.unlink()

        # manifest 先寫：episode 檔案與 manifest 自洽後才存圖；圖存檔失敗會拋出給呼叫端
        self._write_manifest(embeddings, ordered_ids)
        if not tolerate_graph_failure:
            self.graph.save()
            self._graph_dirty = False
            return
        try:
            self.graph.save()
            self._graph_dirty = False
        except Exception as e:
            self._graph_dirty = True
            logger.warning(
                "[EpisodicMemory] episode 已寫入但圖存檔失敗（graph dirty，下次存檔補上）: %s", e,
            )

    def _manifest_matches_current_index(
        self,
        episodes: dict[str, Episode] | None = None,
        embeddings: np.ndarray | None = None,
    ) -> bool:
        if episodes is None:
            episodes, embeddings = self._episodes, self._embeddings
        if not episodes:
            return True
        if embeddings is None or embeddings.ndim != 2:
            return False
        ordered_ids = list(episodes)
        if len(embeddings) != len(ordered_ids):
            return False
        try:
            manifest = json.loads(self.index_manifest_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # Legacy snapshots can be trusted only when every metadata vector
            # still agrees with the persisted matrix row.
            return all(
                len(ep.embedding) == embeddings.shape[1]
                and np.allclose(ep.embedding, embeddings[index])
                for index, ep in enumerate(episodes.values())
            )
        return (
            manifest.get("version") == 1
            and manifest.get("model") == self.embedding_model
            and manifest.get("dimension") == embeddings.shape[1]
            and manifest.get("ordered_ids") == ordered_ids
        )

    def _write_manifest(
        self,
        embeddings: np.ndarray | None,
        ordered_ids: list[str],
    ) -> None:
        if embeddings is None:
            if self.index_manifest_file.exists():
                self.index_manifest_file.unlink()
            return
        manifest = {
            "version": 1,
            "model": self.embedding_model,
            "dimension": int(embeddings.shape[1]),
            "ordered_ids": ordered_ids,
        }
        manifest_tmp = temporary_sibling(self.index_manifest_file)
        with open(manifest_tmp, "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        replace_with_retry(manifest_tmp, self.index_manifest_file)
        self._index_rebuild_required = False

    def _precompute_reindex_cache(self) -> dict[tuple[str, str], list[float]] | None:
        """
        索引需重建（換模型遷移）時，先在鎖外算好各 episode 的新 embedding。

        以 (id, 摘要) 為鍵，鎖內 _reindex_if_required 直接取用；鎖內才發現需要重建
        （其他程序造成）時快取缺漏，退回鎖內逐筆生成（罕見路徑）。
        """
        if not self._index_rebuild_required:
            return None
        with self._mem_lock:
            targets = [(eid, ep.content_summary) for eid, ep in self._episodes.items()]
        return {
            (eid, summary): self._generate_embedding(summary)
            for eid, summary in targets
        }

    def _reindex_if_required(
        self, cache: dict[tuple[str, str], list[float]] | None = None
    ) -> None:
        """重建向量索引（須在 storage_lock 內、_load 之後呼叫）。"""
        if not self._index_rebuild_required:
            return
        vectors = []
        raw_embeddings = []
        id_to_index: dict[str, int] = {}
        for index, (episode_id, episode) in enumerate(self._episodes.items()):
            key = (episode_id, episode.content_summary)
            if cache is not None and key in cache:
                embedding = cache[key]
            else:
                embedding = self._generate_embedding(episode.content_summary)
            raw_embeddings.append(embedding)
            vectors.append(self._normalize_vector(embedding))
            id_to_index[episode_id] = index
        with self._mem_lock:
            for index, episode in enumerate(self._episodes.values()):
                episode.embedding = raw_embeddings[index]
            self._id_to_index = id_to_index
            self._embeddings = np.vstack(vectors) if vectors else None
            self._index_rebuild_required = False

    def _ensure_index_ready(self) -> None:
        if not self._index_rebuild_required:
            return
        reindex_cache = self._precompute_reindex_cache()
        with storage_lock(self.lock_path):
            self._load(reset=True)
            self._reindex_if_required(reindex_cache)
            self._save_unlocked()

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

        # 創建或強化 PRECEDES 邊（與同 session 的前一個 Episode）
        prev_id = self._last_episode_by_session.get(episode.session_id)
        if prev_id is not None and prev_id != episode.id:
            last_ep = self._episodes.get(prev_id)
            if last_ep:
                if self.graph.has_edge(prev_id, episode.id):
                    # 邊已存在（如跨 session 重啟恢復）→ 強化頻率
                    self.graph.update_precedes_edge(prev_id, episode.id)
                else:
                    # 新邊
                    time_delta = (episode.timestamp - last_ep.timestamp).total_seconds()
                    self.graph.add_precedes_edge(prev_id, episode.id, time_delta)

        # 更新追蹤欄位
        self._last_episode_id = episode.id
        self._last_episode_by_session[episode.session_id] = episode.id
