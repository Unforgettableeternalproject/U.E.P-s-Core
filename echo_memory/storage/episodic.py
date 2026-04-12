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
import os
from pathlib import Path
from typing import Any

import numpy as np

from echo_memory.schemas.episode import Episode
from echo_memory.storage.graph import MemoryGraph

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
        
        # 記憶圖（Phase 1.5）
        self.graph = MemoryGraph(storage_path)
        
        # 追蹤最後一個 Episode（用於 PRECEDES 邊）
        self._last_episode_id: str | None = None
        
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
        
        # 計算餘弦相似度
        query_vec = np.array(query_embedding, dtype=np.float32)
        query_vec = query_vec / np.linalg.norm(query_vec)
        
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
        
        # 清空圖結構
        self.graph.clear()
        
        self._save()
    
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 內部方法
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    
    def _generate_embedding(self, text: str) -> list[float]:
        """生成文本嵌入向量"""
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
        """索引 -> Episode ID"""
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
        
        # 保存圖結構
        self.graph.save()
    
    def _update_graph(self, episode: Episode):
        """
        更新圖結構(Phase 1.5)。
        
        自動維護記憶圖：
        1. 添加節點
        2. 創建 PRECEDES 邊(與前一個 Episode 的時序關係)
        """
        # 添加節點
        if not self.graph.has_node(episode.id):
            self.graph.add_episode_node(episode)
        
        # 創建 PRECEDES 邊(與前一個 Episode)
        if self._last_episode_id is not None:
            # 計算時間間隔
            last_ep = self._episodes.get(self._last_episode_id)
            if last_ep:
                time_delta = (episode.timestamp - last_ep.timestamp).total_seconds()
                
                # 只為同一 session 的對話創建邊
                if episode.session_id == last_ep.session_id:
                    self.graph.add_precedes_edge(
                        self._last_episode_id,
                        episode.id,
                        time_delta,
                    )
        
        # 更新最後一個 Episode
        self._last_episode_id = episode.id
    
    def _save(self):
        """保存元數據和向量到磁盤"""
        # 保存元數據
        with open(self.metadata_file, "w", encoding="utf-8") as f:
            data = [ep.to_dict() for ep in self._episodes.values()]
            json.dump(data, f, ensure_ascii=False, indent=2)
        
        # 保存向量
        if self._embeddings is not None:
            np.save(self.embeddings_file, self._embeddings)
