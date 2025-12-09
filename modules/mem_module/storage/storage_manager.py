# modules/mem_module/storage/storage_manager.py
"""
統一存儲管理器 - 整合向量索引、元資料存儲和身份隔離

功能：
- 統一的存儲介面
- 向量與元資料的同步管理
- 身份隔離的一致性保證
- 存儲的備份與恢復
"""

import os
import time
import threading
from typing import List, Dict, Any, Optional, Tuple
from sentence_transformers import SentenceTransformer
import numpy as np

from utils.debug_helper import debug_log, info_log, error_log
from ..schemas import (
    MemoryEntry, MemoryQuery, MemorySearchResult, MemoryOperationResult,
    MemoryType, MemoryImportance
)

from .vector_index import VectorIndexManager
from .metadata_storage import MetadataStorageManager
from .identity_isolation import IdentityIsolationManager


class MemoryStorageManager:
    """統一記憶存儲管理器"""
    
    def __init__(self, config: Dict[str, Any], identity_manager=None):
        self.config = config
        
        # 子系統配置
        vector_config = config.get("vector", {})
        metadata_config = config.get("metadata", {})
        identity_config = config.get("identity", {})
        
        # 初始化子系統
        self.vector_manager = VectorIndexManager(vector_config)
        self.metadata_manager = MetadataStorageManager(metadata_config)
        
        # 使用傳入的 identity_manager 或創建新的
        if identity_manager:
            self.identity_manager = identity_manager
            debug_log(2, "[StorageManager] 使用共享的 IdentityManager")
        else:
            self.identity_manager = IdentityIsolationManager(identity_config)
            debug_log(2, "[StorageManager] 創建獨立的 IdentityIsolationManager")
        
        # 嵌入模型
        self.embedding_model_name = config.get("embedding_model", "all-MiniLM-L6-v2")
        self.embedding_model: Optional[SentenceTransformer] = None
        
        # 同步管理
        self._sync_lock = threading.RLock()
        self._index_metadata_map: Dict[int, str] = {}  # vector_index -> memory_id
        self._memory_vector_map: Dict[str, int] = {}   # memory_id -> vector_index
        
        # 性能配置
        self.batch_size = config.get("batch_size", 32)
        self.auto_save_interval = config.get("auto_save_interval", 300)  # 5分鐘
        
        # 狀態追蹤
        self.is_initialized = False
        self.last_sync_time = None
        
    def initialize(self) -> bool:
        """初始化存儲管理器"""
        try:
            info_log("[StorageManager] 初始化統一存儲管理器...")
            
            # 初始化嵌入模型
            info_log(f"[StorageManager] 載入嵌入模型: {self.embedding_model_name}")
            self.embedding_model = SentenceTransformer(self.embedding_model_name)
            
            # 初始化子系統
            if not self.identity_manager.initialize():
                error_log("[StorageManager] 身份隔離管理器初始化失敗")
                return False
            
            if not self.metadata_manager.initialize():
                error_log("[StorageManager] 元資料管理器初始化失敗")
                return False
            
            if not self.vector_manager.initialize():
                error_log("[StorageManager] 向量索引管理器初始化失敗")
                return False
            
            # 重建索引映射
            if not self._rebuild_index_mapping():
                info_log("WARNING", "[StorageManager] 重建索引映射失敗")
            
            self.is_initialized = True
            self.last_sync_time = time.time()
            
            info_log("[StorageManager] 統一存儲管理器初始化完成")
            return True
            
        except Exception as e:
            error_log(f"[StorageManager] 初始化失敗: {e}")
            return False
    
    def _rebuild_index_mapping(self) -> bool:
        """重建向量索引與元資料的映射關係"""
        try:
            with self._sync_lock:
                debug_log(2, "[StorageManager] 重建索引映射...")
                
                self._index_metadata_map = {}
                self._memory_vector_map = {}
                
                # 獲取所有記憶條目
                all_memories = self.metadata_manager.metadata_cache
                
                vector_index = 0
                for memory_data in all_memories:
                    memory_id = memory_data.get('memory_id')
                    if memory_id and memory_data.get('embedding_vector'):
                        self._index_metadata_map[vector_index] = memory_id
                        self._memory_vector_map[memory_id] = vector_index
                        vector_index += 1
                
                debug_log(3, f"[StorageManager] 索引映射重建完成，條目數: {len(self._memory_vector_map)}")
                return True
                
        except Exception as e:
            error_log(f"[StorageManager] 重建索引映射失敗: {e}")
            return False
    
    def store_memory(self, memory_entry: MemoryEntry) -> MemoryOperationResult:
        """存儲記憶條目"""
        start_time = time.time()
        
        try:
            with self._sync_lock:
                # 驗證記憶令牌
                if not self.identity_manager.validate_memory_token(memory_entry.memory_token):
                    return MemoryOperationResult(
                        success=False,
                        operation_type="store",
                        message="無效的記憶令牌",
                        execution_time=time.time() - start_time
                    )
                
                # 檢查操作權限
                if not self.identity_manager.check_operation_permission(memory_entry.memory_token, "write"):
                    return MemoryOperationResult(
                        success=False,
                        operation_type="store",
                        message="沒有寫入權限",
                        execution_time=time.time() - start_time
                    )
                
                # 生成嵌入向量
                if not memory_entry.embedding_vector:
                    embedding_vector = self._generate_embedding(memory_entry.content)
                    if embedding_vector is None:
                        return MemoryOperationResult(
                            success=False,
                            operation_type="store",
                            memory_id=memory_entry.memory_id,
                            message="生成嵌入向量失敗",
                            execution_time=time.time() - start_time
                        )
                    memory_entry.embedding_vector = embedding_vector.tolist()
                
                # 存儲到元資料管理器
                if not self.metadata_manager.add_memory(memory_entry):
                    return MemoryOperationResult(
                        success=False,
                        operation_type="store",
                        memory_id=memory_entry.memory_id,
                        message="元資料存儲失敗",
                        execution_time=time.time() - start_time
                    )
                
                # 添加到向量索引
                vector_array = np.array([memory_entry.embedding_vector], dtype=np.float32)
                if not self.vector_manager.add_vectors(vector_array, [memory_entry.memory_id]):
                    # 如果向量存儲失敗，需要回滾元資料
                    self.metadata_manager.delete_memory(memory_entry.memory_id)
                    return MemoryOperationResult(
                        success=False,
                        operation_type="store",
                        memory_id=memory_entry.memory_id,
                        message="向量索引存儲失敗",
                        execution_time=time.time() - start_time
                    )
                
                # 更新映射關係
                vector_index = self.vector_manager._vector_count - 1
                self._index_metadata_map[vector_index] = memory_entry.memory_id
                self._memory_vector_map[memory_entry.memory_id] = vector_index
                
                debug_log(3, f"[StorageManager] 記憶存儲成功: {memory_entry.memory_id}")
                
                return MemoryOperationResult(
                    success=True,
                    operation_type="store",
                    memory_id=memory_entry.memory_id,
                    message="記憶存儲成功",
                    affected_count=1,
                    execution_time=time.time() - start_time
                )
                
        except Exception as e:
            error_log(f"[StorageManager] 存儲記憶失敗: {e}")
            return MemoryOperationResult(
                success=False,
                operation_type="store",
                memory_id=getattr(memory_entry, 'memory_id', None),
                message=f"存儲異常: {str(e)}",
                execution_time=time.time() - start_time
            )
    
    def search_memories(self, query: MemoryQuery) -> List[MemorySearchResult]:
        """搜索記憶"""
        try:
            with self._sync_lock:
                # 驗證記憶令牌
                if not self.identity_manager.validate_memory_token(query.memory_token):
                    error_log(f"[StorageManager] 無效的記憶令牌: {query.memory_token[:8]}...")
                    return []
                
                # 檢查查詢權限
                if not self.identity_manager.check_operation_permission(query.memory_token, "query"):
                    debug_log(2, f"[StorageManager] 記憶令牌: {query.memory_token}")
                    error_log("[StorageManager] 沒有查詢權限")
                    return []
                
                # 從元資料篩選候選記憶
                metadata_filters = {}
                
                # 只添加非 None 的過濾器
                if query.memory_types:
                    metadata_filters['memory_types'] = [t.value for t in query.memory_types]
                if query.topic_filter:
                    metadata_filters['topic_filter'] = query.topic_filter
                if query.importance_filter:
                    metadata_filters['importance_filter'] = [i.value for i in query.importance_filter]
                if query.time_range:
                    metadata_filters['time_range'] = query.time_range
                if hasattr(query, 'include_archived') and query.include_archived is not None:
                    metadata_filters['include_archived'] = query.include_archived
                
                candidate_memories = self.metadata_manager.search_memories(
                    query.memory_token, 
                    metadata_filters
                )
                
                if not candidate_memories:
                    debug_log(3, "[StorageManager] 沒有找到候選記憶")
                    return []
                
                # 語意向量搜索
                semantic_results = self._perform_semantic_search(
                    query.query_text, 
                    candidate_memories, 
                    query.similarity_threshold,
                    query.max_results
                )
                
                # 構建搜索結果
                search_results = []
                for memory_data, similarity_score in semantic_results:
                    try:
                        # 重構MemoryEntry物件
                        memory_entry = self._reconstruct_memory_entry(memory_data)
                        
                        # 計算相關性評分
                        relevance_score = self._calculate_relevance_score(
                            memory_entry, query, similarity_score
                        )
                        
                        # 生成檢索原因
                        retrieval_reason = self._generate_retrieval_reason(
                            memory_entry, query, similarity_score
                        )
                        
                        # 計算上下文相關性（給 SemanticRetriever 使用）
                        context_relevance = self._calculate_context_relevance(memory_entry, query)
                        
                        search_result = MemorySearchResult(
                            memory_entry=memory_entry,
                            similarity_score=similarity_score,
                            relevance_score=relevance_score,
                            retrieval_reason=retrieval_reason,
                            retrieval_method="semantic",  # 設置檢索方法
                            context_relevance=context_relevance,  # 設置上下文相關性
                            context_match=self._check_context_match(memory_entry, query),
                            metadata={}  # 初始化元資料字典
                        )
                        
                        search_results.append(search_result)
                        
                        # 更新存取記錄
                        self.metadata_manager.update_memory(
                            memory_entry.memory_id,
                            {
                                'accessed_at': time.time(),
                                'access_count': memory_data.get('access_count', 0) + 1
                            }
                        )
                        
                    except Exception as e:
                        info_log("WARNING", f"[StorageManager] 處理搜索結果失敗: {e}")
                        continue
                
                debug_log(3, f"[StorageManager] 記憶搜索完成，結果數: {len(search_results)}")
                return search_results
                
        except Exception as e:
            error_log(f"[StorageManager] 記憶搜索失敗: {e}")
            return []
    
    def _generate_embedding(self, text: str) -> Optional[np.ndarray]:
        """生成文本嵌入向量"""
        try:
            if not self.embedding_model:
                error_log("[StorageManager] 嵌入模型未初始化")
                return None
            
            embedding = self.embedding_model.encode(text, convert_to_numpy=True)
            return embedding.astype(np.float32)
            
        except Exception as e:
            error_log(f"[StorageManager] 生成嵌入向量失敗: {e}")
            return None
    
    def _perform_semantic_search(self, query_text: str, candidate_memories: List[Dict], 
                                similarity_threshold: float, max_results: int) -> List[Tuple[Dict, float]]:
        """執行語意搜索"""
        try:
            # 生成查詢向量
            query_embedding = self._generate_embedding(query_text)
            if query_embedding is None:
                return []
            
            # 準備候選向量
            candidate_vectors = []
            valid_memories = []
            
            debug_log(3, f"[StorageManager] 準備處理 {len(candidate_memories)} 個候選記憶")
            
            for idx, memory_data in enumerate(candidate_memories):
                embedding_vector = memory_data.get('embedding_vector')
                debug_log(3, f"[StorageManager] 候選 {idx}: embedding_vector type={type(embedding_vector)}, is_list={isinstance(embedding_vector, list)}")
                
                if embedding_vector:
                    # 確保向量是numpy array格式
                    if isinstance(embedding_vector, list):
                        debug_log(3, f"[StorageManager] 轉換 list 到 numpy array (長度: {len(embedding_vector)})")
                        embedding_vector = np.array(embedding_vector, dtype=np.float32)
                        debug_log(3, f"[StorageManager] 轉換後 shape={embedding_vector.shape}, dtype={embedding_vector.dtype}")
                    elif not isinstance(embedding_vector, np.ndarray):
                        debug_log(3, f"[StorageManager] 跳過無效格式: {type(embedding_vector)}")
                        continue  # 跳過無效格式
                    candidate_vectors.append(embedding_vector)
                    valid_memories.append(memory_data)
                    debug_log(3, f"[StorageManager] 成功添加候選向量 {idx}")
                else:
                    debug_log(3, f"[StorageManager] 候選 {idx} 沒有 embedding_vector")
            
            if not candidate_vectors:
                return []
            
            debug_log(3, f"[StorageManager] 準備計算相似度，候選向量數: {len(candidate_vectors)}")
            
            # 計算相似度 - 使用 vstack 而非 array，因為元素已經是 numpy 數組
            candidate_array = np.vstack(candidate_vectors)
            query_array = query_embedding.reshape(1, -1)
            
            debug_log(4, f"[StorageManager] candidate_array shape: {candidate_array.shape}, query_array shape: {query_array.shape}")
            
            # 正規化向量
            from sklearn.metrics.pairwise import cosine_similarity
            similarities = cosine_similarity(query_array, candidate_array)[0]
            
            debug_log(3, f"[StorageManager] 相似度計算完成，相似度範圍: {similarities.min():.3f} ~ {similarities.max():.3f}")
            
            # 過濾和排序結果
            results = []
            for memory_data, similarity in zip(valid_memories, similarities):
                if similarity >= similarity_threshold:
                    results.append((memory_data, float(similarity)))
            
            debug_log(3, f"[StorageManager] 過濾後結果數 (threshold={similarity_threshold}): {len(results)}")
            
            # 按相似度排序
            results.sort(key=lambda x: x[1], reverse=True)
            
            # 限制結果數量
            return results[:max_results]
            
        except Exception as e:
            error_log(f"[StorageManager] 語意搜索失敗: {e}")
            return []
    
    def _reconstruct_memory_entry(self, memory_data: Dict[str, Any]) -> MemoryEntry:
        """從字典重構MemoryEntry物件"""
        try:
            # 處理datetime字段
            if 'created_at' in memory_data and isinstance(memory_data['created_at'], str):
                from datetime import datetime
                memory_data['created_at'] = datetime.fromisoformat(memory_data['created_at'].replace('Z', '+00:00'))
            
            # 確保必要字段存在，提供預設值
            if 'embedding_vector' not in memory_data:
                memory_data['embedding_vector'] = None
            if 'importance' not in memory_data:
                memory_data['importance'] = MemoryImportance.MEDIUM
            if 'access_count' not in memory_data:
                memory_data['access_count'] = 0
            
            return MemoryEntry(**memory_data)
            
        except Exception as e:
            error_log(f"[StorageManager] 重構記憶條目失敗: {e}")
            # 提供完整的預設值
            return MemoryEntry(
                memory_id=memory_data.get('memory_id', 'unknown'),
                memory_token=memory_data.get('memory_token', ''),
                memory_type=MemoryType(memory_data.get('memory_type', 'snapshot')),
                content=memory_data.get('content', ''),
                importance=MemoryImportance.MEDIUM,
                access_count=0,
                embedding_vector=None
            )
    
    def _calculate_relevance_score(self, memory_entry: MemoryEntry, query: MemoryQuery, 
                                 similarity_score: float) -> float:
        """計算相關性評分"""
        try:
            relevance = similarity_score
            
            # 重要性加權 - 使用 importance_score 屬性
            importance_score = getattr(memory_entry, 'importance_score', 0.5)
            # 將 0-1 的分數轉換為 0.8-1.2 的權重
            importance_weight = 0.8 + (importance_score * 0.4)
            relevance *= importance_weight
            
            # 時間衰減（最近的記憶更相關） - 安全地獲取 created_at
            created_at = getattr(memory_entry, 'created_at', None)
            if created_at:
                # 安全地將 created_at 轉換為時間戳
                created_timestamp = created_at.timestamp() if hasattr(created_at, 'timestamp') else created_at
                time_diff = (time.time() - created_timestamp) / 86400  # 天數
                time_decay = max(0.5, 1.0 - (time_diff * 0.01))  # 每天衰減1%
                relevance *= time_decay
            
            # 存取頻率加權 - 安全地獲取 access_count
            access_count = getattr(memory_entry, 'access_count', 0)
            access_boost = min(1.2, 1.0 + (access_count * 0.01))
            relevance *= access_boost
            
            return min(1.0, relevance)
            
        except Exception as e:
            info_log("WARNING", f"[StorageManager] 計算相關性評分失敗: {e}")
            return similarity_score
    
    def _calculate_context_relevance(self, memory_entry: MemoryEntry, query: MemoryQuery) -> float:
        """計算上下文相關性（為 SemanticRetriever 提供）"""
        try:
            context_score = 0.0
            
            # 檢查記憶類型匹配
            if hasattr(query, 'memory_types') and query.memory_types:
                if memory_entry.memory_type in query.memory_types:
                    context_score += 0.3
            
            # 檢查主題匹配
            if hasattr(query, 'topic_filter') and query.topic_filter:
                memory_topic = getattr(memory_entry, 'topic', '')
                if memory_topic and query.topic_filter.lower() in memory_topic.lower():
                    context_score += 0.3
            
            # 檢查重要性匹配
            if hasattr(query, 'importance_filter') and query.importance_filter:
                # 使用 importance_score 而不是 importance 枚舉
                importance_score = getattr(memory_entry, 'importance_score', 0.5)
                # 如果有 importance_filter，嘗試轉換為分數範圍檢查
                if importance_score >= 0.8:  # 對應 HIGH/CRITICAL
                    context_score += 0.2
            
            # 檢查時間範圍匹配
            if hasattr(query, 'time_range') and query.time_range:
                created_at = getattr(memory_entry, 'created_at', None)
                if created_at and 'start' in query.time_range and 'end' in query.time_range:
                    # 確保所有時間都轉換為時間戳再比較
                    created_timestamp = created_at.timestamp() if hasattr(created_at, 'timestamp') else created_at
                    
                    start_time = query.time_range['start']
                    start_timestamp = start_time.timestamp() if hasattr(start_time, 'timestamp') else start_time
                    
                    end_time = query.time_range['end']
                    end_timestamp = end_time.timestamp() if hasattr(end_time, 'timestamp') else end_time
                    
                    if start_timestamp <= created_timestamp <= end_timestamp:
                        context_score += 0.2
            
            return min(1.0, context_score)
            
        except Exception as e:
            debug_log(2, f"[StorageManager] 計算上下文相關性失敗: {e}")
            return 0.0
    
    def _generate_retrieval_reason(self, memory_entry: MemoryEntry, query: MemoryQuery, 
                                 similarity_score: float) -> str:
        """生成檢索原因"""
        reasons = []
        
        if similarity_score > 0.9:
            reasons.append("高度語意相似")
        elif similarity_score > 0.8:
            reasons.append("語意相似")
        else:
            reasons.append("部分相似")
        
        # 安全地獲取 topic 屬性
        topic = getattr(memory_entry, 'topic', None)
        if query.topic_filter and topic and query.topic_filter.lower() in topic.lower():
            reasons.append("主題匹配")
        
        # 安全地獲取 importance 屬性
        importance = getattr(memory_entry, 'importance', MemoryImportance.MEDIUM)
        if importance in [MemoryImportance.CRITICAL, MemoryImportance.HIGH]:
            reasons.append("高重要性")
        
        # 安全地獲取 access_count 屬性
        access_count = getattr(memory_entry, 'access_count', 0)
        if access_count > 5:
            reasons.append("常用記憶")
        
        return "、".join(reasons)
    
    def _check_context_match(self, memory_entry: MemoryEntry, query: MemoryQuery) -> bool:
        """檢查上下文匹配"""
        try:
            if not query.current_intent:
                return False
            
            return query.current_intent.lower() in " ".join(memory_entry.intent_tags).lower()
            
        except:
            return False
    
    def delete_memory(self, memory_id: str, memory_token: str) -> MemoryOperationResult:
        """刪除記憶條目"""
        start_time = time.time()
        
        try:
            with self._sync_lock:
                # 驗證權限
                if not self.identity_manager.validate_memory_token(memory_token):
                    return MemoryOperationResult(
                        success=False,
                        operation_type="delete",
                        message="無效的記憶令牌",
                        execution_time=time.time() - start_time
                    )
                
                if not self.identity_manager.check_operation_permission(memory_token, "delete"):
                    return MemoryOperationResult(
                        success=False,
                        operation_type="delete",
                        message="沒有刪除權限",
                        execution_time=time.time() - start_time
                    )
                
                # 從元資料刪除
                if not self.metadata_manager.delete_memory(memory_id):
                    return MemoryOperationResult(
                        success=False,
                        operation_type="delete",
                        memory_id=memory_id,
                        message="元資料刪除失敗",
                        execution_time=time.time() - start_time
                    )
                
                # 清理映射關係
                if memory_id in self._memory_vector_map:
                    vector_index = self._memory_vector_map[memory_id]
                    del self._memory_vector_map[memory_id]
                    if vector_index in self._index_metadata_map:
                        del self._index_metadata_map[vector_index]
                
                debug_log(3, f"[StorageManager] 記憶刪除成功: {memory_id}")
                
                return MemoryOperationResult(
                    success=True,
                    operation_type="delete",
                    memory_id=memory_id,
                    message="記憶刪除成功",
                    affected_count=1,
                    execution_time=time.time() - start_time
                )
                
        except Exception as e:
            error_log(f"[StorageManager] 刪除記憶失敗: {e}")
            return MemoryOperationResult(
                success=False,
                operation_type="delete",
                memory_id=memory_id,
                message=f"刪除異常: {str(e)}",
                execution_time=time.time() - start_time
            )
    
    def rebuild_index(self) -> bool:
        """重建向量索引"""
        try:
            info_log("[StorageManager] 開始重建向量索引...")
            
            # 重建向量索引
            if not self.vector_manager.rebuild_index():
                error_log("[StorageManager] 向量索引重建失敗")
                return False
            
            # 重建映射關係
            if not self._rebuild_index_mapping():
                error_log("[StorageManager] 索引映射重建失敗")
                return False
            
            # 儲存索引
            if not self.vector_manager.save_index():
                error_log("[StorageManager] 索引儲存失敗")
                return False
            
            info_log("[StorageManager] 向量索引重建完成")
            return True
            
        except Exception as e:
            error_log(f"[StorageManager] 重建索引失敗: {e}")
            return False
    
    def get_stats(self) -> Dict[str, Any]:
        """獲取存儲統計資訊"""
        try:
            vector_stats = self.vector_manager.get_stats()
            identity_stats = self.identity_manager.get_stats()
            metadata_stats = self.metadata_manager.get_memory_stats()
            
            return {
                "vector_index": vector_stats,
                "identity_isolation": identity_stats,
                "metadata_storage": metadata_stats,
                "index_mapping": {
                    "total_mappings": len(self._memory_vector_map),
                    "consistent": len(self._index_metadata_map) == len(self._memory_vector_map)
                },
                "embedding_model": self.embedding_model_name,
                "is_initialized": self.is_initialized,
                "last_sync_time": self.last_sync_time
            }
            
        except Exception as e:
            error_log(f"[StorageManager] 獲取統計失敗: {e}")
            return {}
