# modules/mem_module/storage/vector_index.py
"""
向量索引管理器 - 基於FAISS的向量存儲與檢索

功能：
- FAISS索引的創建、載入、儲存
- 向量的添加、搜索、刪除
- 索引重建與優化
- 身份隔離的向量檢索
"""

import faiss
import numpy as np
import os
import pickle
import threading
from typing import List, Tuple, Optional, Dict, Any
from pathlib import Path

from utils.debug_helper import debug_log, info_log, error_log
from ..schemas import MemoryEntry, MemorySearchResult


class VectorIndexManager:
    """FAISS向量索引管理器"""
    
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        
        # 基本配置
        self.vector_dimension = config.get("vector_dimension", 384)
        self.index_file = config.get("index_file", "memory/mem_faiss_index")
        self.index_backup_file = f"{self.index_file}.backup"
        
        # FAISS相關
        self.index: Optional[faiss.Index] = None
        self.index_type = config.get("index_type", "IndexFlatIP")  # 內積索引
        self.nprobe = config.get("nprobe", 10)  # IVF索引參數
        
        # 性能配置
        self.batch_size = config.get("batch_size", 100)
        self.enable_gpu = config.get("enable_gpu", False)
        
        # 線程安全
        self._lock = threading.RLock()
        
        # 狀態追蹤
        self._vector_count = 0
        self._index_version = "1.0"
        self.is_initialized = False
        
    def initialize(self) -> bool:
        """初始化向量索引"""
        try:
            with self._lock:
                info_log(f"[VectorIndex] 初始化向量索引管理器...")
                
                # 確保目錄存在
                index_dir = Path(self.index_file).parent
                index_dir.mkdir(parents=True, exist_ok=True)
                
                # 嘗試載入現有索引
                if self._index_file_exists():
                    if self._load_index():
                        info_log(f"[VectorIndex] 成功載入現有索引，向量數量: {self._vector_count}")
                    else:
                        info_log("WARNING", "[VectorIndex] 載入索引失敗，創建新索引")
                        self._create_new_index()
                else:
                    info_log("[VectorIndex] 索引文件不存在，創建新索引")
                    self._create_new_index()
                
                # GPU支援檢查
                if self.enable_gpu and faiss.get_num_gpus() > 0:
                    self._setup_gpu_index()
                    info_log("[VectorIndex] GPU加速已啟用")
                
                self.is_initialized = True
                info_log(f"[VectorIndex] 向量索引初始化完成，類型: {self.index_type}")
                return True
                
        except Exception as e:
            error_log(f"[VectorIndex] 初始化失敗: {e}")
            return False
    
    def _index_file_exists(self) -> bool:
        """檢查索引文件是否存在"""
        return os.path.exists(self.index_file)
    
    def _create_new_index(self) -> bool:
        """創建新的FAISS索引"""
        try:
            debug_log(2, f"[VectorIndex] 創建新索引，維度: {self.vector_dimension}")
            
            if self.index_type == "IndexFlatIP":
                # 內積索引 (適合歸一化向量)
                self.index = faiss.IndexFlatIP(self.vector_dimension)
            elif self.index_type == "IndexFlatL2":
                # L2距離索引
                self.index = faiss.IndexFlatL2(self.vector_dimension)
            elif self.index_type == "IndexIVFFlat":
                # IVF索引 (適合大量向量)
                nlist = min(100, max(1, int(self.vector_dimension / 4)))
                quantizer = faiss.IndexFlatIP(self.vector_dimension)
                self.index = faiss.IndexIVFFlat(quantizer, self.vector_dimension, nlist)
            else:
                error_log(f"[VectorIndex] 不支援的索引類型: {self.index_type}")
                return False
            
            self._vector_count = 0
            return True
            
        except Exception as e:
            error_log(f"[VectorIndex] 創建索引失敗: {e}")
            return False
    
    def _load_index(self) -> bool:
        """載入現有的FAISS索引"""
        try:
            debug_log(2, f"[VectorIndex] 載入索引文件: {self.index_file}")
            
            # 載入FAISS索引
            self.index = faiss.read_index(self.index_file)
            self._vector_count = self.index.ntotal
            
            # 設定IVF參數
            if hasattr(self.index, 'nprobe'):
                self.index.nprobe = self.nprobe
            
            debug_log(3, f"[VectorIndex] 索引載入成功，向量數量: {self._vector_count}")
            return True
            
        except Exception as e:
            error_log(f"[VectorIndex] 載入索引失敗: {e}")
            return False
    
    def save_index(self) -> bool:
        """儲存FAISS索引"""
        try:
            with self._lock:
                if not self.index:
                    info_log("WARNING", "[VectorIndex] 索引為空，跳過儲存")
                    return False
                
                debug_log(2, f"[VectorIndex] 儲存索引到: {self.index_file}")
                
                # 創建備份
                if os.path.exists(self.index_file):
                    os.rename(self.index_file, self.index_backup_file)
                
                # 儲存索引
                faiss.write_index(self.index, self.index_file)
                
                # 清理舊備份
                if os.path.exists(self.index_backup_file):
                    os.remove(self.index_backup_file)
                
                info_log(f"[VectorIndex] 索引儲存成功，向量數量: {self._vector_count}")
                return True
                
        except Exception as e:
            error_log(f"[VectorIndex] 儲存索引失敗: {e}")
            
            # 恢復備份
            if os.path.exists(self.index_backup_file):
                os.rename(self.index_backup_file, self.index_file)
                info_log("WARNING", "[VectorIndex] 已恢復備份索引")
            
            return False
    
    def add_vectors(self, vectors: np.ndarray, vector_ids: List[str] = None) -> bool:
        """添加向量到索引"""
        try:
            with self._lock:
                if not self.is_initialized or not self.index:
                    error_log("[VectorIndex] 索引未初始化")
                    return False
                
                if vectors.shape[1] != self.vector_dimension:
                    error_log(f"[VectorIndex] 向量維度不匹配: {vectors.shape[1]} != {self.vector_dimension}")
                    return False
                
                # 確保向量是float32格式
                if vectors.dtype != np.float32:
                    vectors = vectors.astype(np.float32)
                
                # 歸一化向量（如果使用內積索引）
                if self.index_type == "IndexFlatIP":
                    faiss.normalize_L2(vectors)
                
                # 添加向量
                if hasattr(self.index, 'add_with_ids') and vector_ids:
                    # 轉換ID為整數
                    ids = np.array([hash(vid) % (2**31) for vid in vector_ids], dtype=np.int64)
                    self.index.add_with_ids(vectors, ids)
                else:
                    self.index.add(vectors)
                
                self._vector_count += vectors.shape[0]
                debug_log(3, f"[VectorIndex] 添加 {vectors.shape[0]} 個向量，總數: {self._vector_count}")
                
                return True
                
        except Exception as e:
            error_log(f"[VectorIndex] 添加向量失敗: {e}")
            return False
    
    def search_vectors(self, query_vector: np.ndarray, top_k: int = 10, 
                      similarity_threshold: float = 0.7) -> Tuple[List[float], List[int]]:
        """搜索相似向量"""
        try:
            with self._lock:
                if not self.is_initialized or not self.index:
                    error_log("[VectorIndex] 索引未初始化")
                    return [], []
                
                if self._vector_count == 0:
                    debug_log(3, "[VectorIndex] 索引中沒有向量")
                    return [], []
                
                # 確保查詢向量格式正確
                if query_vector.ndim == 1:
                    query_vector = query_vector.reshape(1, -1)
                
                if query_vector.dtype != np.float32:
                    query_vector = query_vector.astype(np.float32)
                
                # 歸一化查詢向量
                if self.index_type == "IndexFlatIP":
                    faiss.normalize_L2(query_vector)
                
                # 搜索
                k = min(top_k, self._vector_count)
                scores, indices = self.index.search(query_vector, k)
                
                # 過濾結果
                valid_scores = []
                valid_indices = []
                
                for score, idx in zip(scores[0], indices[0]):
                    if idx >= 0:  # 有效索引
                        # 轉換相似度
                        if self.index_type == "IndexFlatIP":
                            similarity = float(score)  # 內積已經是相似度
                        else:
                            similarity = 1.0 / (1.0 + float(score))  # L2距離轉相似度
                        
                        if similarity >= similarity_threshold:
                            valid_scores.append(similarity)
                            valid_indices.append(int(idx))
                
                debug_log(3, f"[VectorIndex] 搜索完成，找到 {len(valid_scores)} 個結果")
                return valid_scores, valid_indices
                
        except Exception as e:
            error_log(f"[VectorIndex] 向量搜索失敗: {e}")
            return [], []
    
    def rebuild_index(self) -> bool:
        """重建索引（優化性能）"""
        try:
            with self._lock:
                info_log("[VectorIndex] 開始重建索引...")
                
                if not self.index or self._vector_count == 0:
                    info_log("WARNING", "[VectorIndex] 沒有需要重建的向量")
                    return True
                
                # 獲取所有向量
                all_vectors = np.zeros((self._vector_count, self.vector_dimension), dtype=np.float32)
                for i in range(self._vector_count):
                    all_vectors[i] = self.index.reconstruct(i)
                
                # 創建新索引
                old_index_type = self.index_type
                self._create_new_index()
                
                # 重新添加向量
                if self.add_vectors(all_vectors):
                    # 儲存重建後的索引
                    if self.save_index():
                        info_log(f"[VectorIndex] 索引重建完成，類型: {old_index_type}")
                        return True
                
                error_log("[VectorIndex] 索引重建失敗")
                return False
                
        except Exception as e:
            error_log(f"[VectorIndex] 重建索引異常: {e}")
            return False
    
    def _setup_gpu_index(self):
        """設定GPU加速索引"""
        try:
            if self.index and faiss.get_num_gpus() > 0:
                gpu_resource = faiss.StandardGpuResources()
                self.index = faiss.index_cpu_to_gpu(gpu_resource, 0, self.index)
                debug_log(2, "[VectorIndex] GPU索引設定完成")
        except Exception as e:
            info_log("WARNING", f"[VectorIndex] GPU設定失敗，使用CPU: {e}")
    
    def get_stats(self) -> Dict[str, Any]:
        """獲取索引統計資訊"""
        return {
            "vector_count": self._vector_count,
            "vector_dimension": self.vector_dimension,
            "index_type": self.index_type,
            "index_version": self._index_version,
            "is_initialized": self.is_initialized,
            "enable_gpu": self.enable_gpu,
            "index_file": self.index_file
        }
    
    def clear_index(self) -> bool:
        """清空索引"""
        try:
            with self._lock:
                info_log("[VectorIndex] 清空向量索引")
                self._create_new_index()
                return True
        except Exception as e:
            error_log(f"[VectorIndex] 清空索引失敗: {e}")
            return False
