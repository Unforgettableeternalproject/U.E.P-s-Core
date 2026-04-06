"""
EpisodicMemory - 情節記憶存儲

使用 FAISS 向量索引 + JSON 元數據存儲。

Phase 1 實作：
- 基本 CRUD 操作
- FAISS 向量檢索
- JSON 元數據持久化
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import numpy as np

from echo_memory.schemas.episode import Episode

logger = logging.getLogger("echo_memory.episodic")


class EpisodicMemory:
    """
    情節記憶管理器 - MVP 實作。
    
    存儲結構：
    - episodes.json: 所有情節的元數據
    - embeddings.npy: FAISS 索引的向量數據
    """
    
    def __init__(
        self,
        storage_path: str | Path,
        embedding_model: str = "all-MiniLM-L6-v2",
        device: str = "cpu",
    ):
        self.storage_path = Path(storage_path)
        self.storage_path.mkdir(parents=True, exist_ok=True)
        
        self.embedding_model = embedding_model
        self.device = device
        
        # 數據文件路徑
        self.metadata_file = self.storage_path / "episodes.json"
        self.embeddings_file = self.storage_path / "embeddings.npy"
        
        # 內存緩存
        self._episodes: dict[str, Episode] = {}
        self._embeddings: np.ndarray | None = None
        self._id_to_index: dict[str, int] = {}
        
        # 延遲加載 embedding 生成器
        self._embedder = None
        
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
        
        # 持久化
        self._save()
        
        logger.debug(f"[EpisodicMemory] 寫入情節: {episode.id}")
        return episode.id
    
    def get(self, episode_id: str) -> Episode | None:
        """根據 ID 獲取情節"""
        return self._episodes.get(episode_id)
    
    def search(self, query_embedding: list[float], top_k: int = 10) -> list[Episode]:
        """
        向量相似度搜索。
        
        Args:
            query_embedding: 查詢向量
            top_k: 返回數量
        
        Returns:
            最相似的 Episode 列表
        """
        if self._embeddings is None or len(self._episodes) == 0:
            return []
        
        # 計算餘弦相似度
        query_vec = np.array(query_embedding, dtype=np.float32)
        query_vec = query_vec / np.linalg.norm(query_vec)
        
        similarities = np.dot(self._embeddings, query_vec)
        top_indices = np.argsort(similarities)[::-1][:top_k]
        
        # 轉換為 Episode 對象
        results = []
        for idx in top_indices:
            ep_id = self._index_to_id(idx)
            if ep_id and ep_id in self._episodes:
                results.append(self._episodes[ep_id])
        
        return results
    
    def count(self) -> int:
        """返回情節總數"""
        return len(self._episodes)
    
    def clear(self):
        """清空所有情節（測試用）"""
        self._episodes.clear()
        self._embeddings = None
        self._id_to_index.clear()
        self._save()
    
    # ─── 內部方法 ────────────────────────────────────────────────────────────
    
    def _generate_embedding(self, text: str) -> list[float]:
        """生成文本嵌入向量"""
        if self._embedder is None:
            from sentence_transformers import SentenceTransformer
            self._embedder = SentenceTransformer(
                self.embedding_model,
                device=self.device,
            )
        
        embedding = self._embedder.encode(text, convert_to_numpy=True)
        return embedding.tolist()
    
    def _update_embeddings(self, episode: Episode):
        """更新向量索引"""
        new_vec = np.array(episode.embedding, dtype=np.float32)
        
        if self._embeddings is None:
            self._embeddings = new_vec.reshape(1, -1)
            self._id_to_index[episode.id] = 0
        else:
            idx = len(self._embeddings)
            self._embeddings = np.vstack([self._embeddings, new_vec])
            self._id_to_index[episode.id] = idx
    
    def _index_to_id(self, index: int) -> str | None:
        """索引 → Episode ID"""
        for ep_id, idx in self._id_to_index.items():
            if idx == index:
                return ep_id
        return None
    
    def _load(self):
        """從磁盤載入數據"""
        if self.metadata_file.exists():
            with open(self.metadata_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                for ep_dict in data:
                    ep = Episode.from_dict(ep_dict)
                    self._episodes[ep.id] = ep
        
        if self.embeddings_file.exists():
            self._embeddings = np.load(self.embeddings_file)
            # 重建索引映射
            for i, ep in enumerate(self._episodes.values()):
                self._id_to_index[ep.id] = i
    
    def _save(self):
        """持久化到磁盤"""
        # 保存元數據
        with open(self.metadata_file, "w", encoding="utf-8") as f:
            data = [ep.to_dict() for ep in self._episodes.values()]
            json.dump(data, f, ensure_ascii=False, indent=2)
        
        # 保存向量
        if self._embeddings is not None:
            np.save(self.embeddings_file, self._embeddings)
