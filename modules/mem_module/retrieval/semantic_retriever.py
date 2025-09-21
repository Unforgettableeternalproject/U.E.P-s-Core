# modules/mem_module/retrieval/semantic_retriever.py
"""
語義檢索器 - 增強的RAG算法實現

功能：
- 語義相似度檢索
- 多向量檢索策略
- 上下文感知檢索
- 檢索結果重排序
- 混合檢索算法
"""

import math
import numpy as np
from typing import Dict, Any, List, Optional, Tuple, Set
from datetime import datetime, timedelta

from utils.debug_helper import debug_log, info_log, error_log
from ..schemas import (
    MemoryEntry, MemoryQuery, MemorySearchResult, 
    MemoryType, MemoryImportance
)
from ..storage.storage_manager import MemoryStorageManager


class SemanticRetriever:
    """語義檢索器"""
    
    def __init__(self, config: Dict[str, Any], storage_manager: MemoryStorageManager):
        self.config = config
        self.storage_manager = storage_manager
        
        # 檢索配置
        self.similarity_weights = config.get("similarity_weights", {
            "semantic": 0.6,
            "temporal": 0.2,
            "context": 0.1,
            "importance": 0.1
        })
        
        self.retrieval_strategies = config.get("retrieval_strategies", [
            "semantic_search",
            "context_aware",
            "temporal_proximity",
            "importance_based"
        ])
        
        # 重排序配置
        self.rerank_enabled = config.get("rerank_enabled", True)
        self.diversity_factor = config.get("diversity_factor", 0.3)
        self.recency_boost = config.get("recency_boost", 0.2)
        
        # 緩存配置
        self.cache_enabled = config.get("cache_enabled", True)
        self.cache_size = config.get("cache_size", 100)
        self._search_cache: Dict[str, List[MemorySearchResult]] = {}
        
        # 統計資訊
        self.stats = {
            "searches_performed": 0,
            "cache_hits": 0,
            "cache_misses": 0,
            "total_results_returned": 0,
            "avg_similarity_score": 0.0
        }
        
        self.is_initialized = False
    
    def initialize(self) -> bool:
        """初始化語義檢索器"""
        try:
            info_log("[SemanticRetriever] 初始化語義檢索器...")
            
            # 驗證存儲管理器
            if not self.storage_manager.is_initialized:
                error_log("[SemanticRetriever] 存儲管理器未初始化")
                return False
            
            # 載入檢索模型（如果需要）
            self._load_retrieval_models()
            
            self.is_initialized = True
            info_log("[SemanticRetriever] 語義檢索器初始化完成")
            return True
            
        except Exception as e:
            error_log(f"[SemanticRetriever] 初始化失敗: {e}")
            return False
    
    def _load_retrieval_models(self):
        """載入檢索模型"""
        try:
            debug_log(3, "[SemanticRetriever] 載入檢索模型...")
            
            # 這裡可以載入預訓練的檢索模型
            # 例如：sentence-transformers, BERT等
            
        except Exception as e:
            info_log("WARNING", f"[SemanticRetriever] 載入檢索模型失敗: {e}")
    
    def search_memories(self, query: MemoryQuery) -> List[MemorySearchResult]:
        """搜尋記憶"""
        try:
            self.stats["searches_performed"] += 1
            
            # 檢查緩存
            cache_key = self._generate_cache_key(query)
            if self.cache_enabled and cache_key in self._search_cache:
                self.stats["cache_hits"] += 1
                debug_log(4, f"[SemanticRetriever] 緩存命中: {cache_key[:20]}...")
                return self._search_cache[cache_key]
            
            self.stats["cache_misses"] += 1
            
            # 執行多策略檢索
            all_results = []
            
            for strategy in self.retrieval_strategies:
                strategy_results = self._execute_strategy(strategy, query)
                all_results.extend(strategy_results)
            
            # 合併和去重
            merged_results = self._merge_and_deduplicate(all_results)
            
            # 重排序
            if self.rerank_enabled:
                merged_results = self._rerank_results(merged_results, query)
            
            # 限制結果數量
            final_results = merged_results[:query.max_results]
            
            # 更新統計
            self.stats["total_results_returned"] += len(final_results)
            if final_results:
                avg_score = sum(r.similarity_score for r in final_results) / len(final_results)
                self.stats["avg_similarity_score"] = avg_score
            
            # 緩存結果
            if self.cache_enabled:
                self._cache_results(cache_key, final_results)
            
            debug_log(3, f"[SemanticRetriever] 檢索完成: {len(final_results)} 個結果")
            return final_results
            
        except Exception as e:
            error_log(f"[SemanticRetriever] 檢索失敗: {e}")
            return []
    
    def _execute_strategy(self, strategy: str, query: MemoryQuery) -> List[MemorySearchResult]:
        """執行檢索策略"""
        try:
            if strategy == "semantic_search":
                return self._semantic_search(query)
            elif strategy == "context_aware":
                return self._context_aware_search(query)
            elif strategy == "temporal_proximity":
                return self._temporal_proximity_search(query)
            elif strategy == "importance_based":
                return self._importance_based_search(query)
            else:
                debug_log(1, f"[SemanticRetriever] 未知檢索策略: {strategy}")
                return []
                
        except Exception as e:
            error_log(f"[SemanticRetriever] 策略 {strategy} 執行失敗: {e}")
            return []
    
    def _semantic_search(self, query: MemoryQuery) -> List[MemorySearchResult]:
        """語義搜尋"""
        try:
            # 直接使用存儲管理器的記憶搜索方法
            return self.storage_manager.search_memories(query)
            
        except Exception as e:
            error_log(f"[SemanticRetriever] 語義搜尋失敗: {e}")
            return []
    
    def _context_aware_search(self, query: MemoryQuery) -> List[MemorySearchResult]:
        """上下文感知檢索"""
        try:
            # 如果有當前意圖，使用Intent過濾的查詢
            if query.current_intent:
                # 創建一個帶意圖過濾的查詢副本
                intent_query = MemoryQuery(
                    memory_token=query.memory_token,
                    query_text=query.query_text + f" {query.current_intent}",
                    memory_types=query.memory_types,
                    max_results=query.max_results,
                    similarity_threshold=query.similarity_threshold,
                    current_intent=query.current_intent
                )
                return self.storage_manager.search_memories(intent_query)
            else:
                return self.storage_manager.search_memories(query)
            
        except Exception as e:
            error_log(f"[SemanticRetriever] 上下文檢索失敗: {e}")
            return []
    
    def _temporal_proximity_search(self, query: MemoryQuery) -> List[MemorySearchResult]:
        """時間鄰近性檢索"""
        try:
            # 使用時間範圍過濾
            recent_time = datetime.now() - timedelta(hours=24)
            time_query = MemoryQuery(
                memory_token=query.memory_token,
                query_text=query.query_text,
                memory_types=query.memory_types,
                max_results=query.max_results,
                similarity_threshold=query.similarity_threshold,
                time_range={"start": recent_time, "end": datetime.now()}
            )
            return self.storage_manager.search_memories(time_query)
            
        except Exception as e:
            error_log(f"[SemanticRetriever] 時間鄰近檢索失敗: {e}")
            return []
    
    def _importance_based_search(self, query: MemoryQuery) -> List[MemorySearchResult]:
        """重要性檢索"""
        try:
            # 創建重要性過濾查詢
            importance_query = MemoryQuery(
                memory_token=query.memory_token,
                query_text=query.query_text,
                memory_types=query.memory_types,
                max_results=query.max_results,
                similarity_threshold=query.similarity_threshold,
                importance_filter=[MemoryImportance.HIGH, MemoryImportance.CRITICAL]
            )
            return self.storage_manager.search_memories(importance_query)
            
        except Exception as e:
            error_log(f"[SemanticRetriever] 重要性檢索失敗: {e}")
            return []
    
    def _calculate_context_relevance(self, memory_entry: MemoryEntry, query: MemoryQuery) -> float:
        """計算上下文相關性"""
        try:
            relevance_score = 0.0
            
            # 意圖標籤匹配
            if query.current_intent and memory_entry.intent_tags:
                if query.current_intent in memory_entry.intent_tags:
                    relevance_score += 0.5
            
            # 主題匹配
            if memory_entry.topic and query.query_text:
                if memory_entry.topic.lower() in query.query_text.lower():
                    relevance_score += 0.3
            
            # 會話連續性
            if hasattr(query, 'session_id') and memory_entry.session_id == getattr(query, 'session_id', None):
                relevance_score += 0.2
            
            return min(relevance_score, 1.0)
            
        except Exception as e:
            debug_log(1, f"[SemanticRetriever] 上下文相關性計算失敗: {e}")
            return 0.0
    
    def _merge_and_deduplicate(self, all_results: List[MemorySearchResult]) -> List[MemorySearchResult]:
        """合併和去重結果"""
        try:
            # 使用記憶ID去重
            seen_memory_ids = set()
            merged_results = []
            
            for result in all_results:
                memory_id = result.memory_entry.memory_id
                
                if memory_id not in seen_memory_ids:
                    seen_memory_ids.add(memory_id)
                    merged_results.append(result)
                else:
                    # 如果已存在，合併分數
                    for existing_result in merged_results:
                        if existing_result.memory_entry.memory_id == memory_id:
                            # 使用加權平均合併分數
                            weight1 = self.similarity_weights.get(existing_result.retrieval_method, 0.5)
                            weight2 = self.similarity_weights.get(result.retrieval_method, 0.5)
                            
                            combined_score = (existing_result.similarity_score * weight1 + 
                                            result.similarity_score * weight2) / (weight1 + weight2)
                            
                            existing_result.similarity_score = combined_score
                            existing_result.metadata.update(result.metadata)
                            break
            
            debug_log(4, f"[SemanticRetriever] 合併去重: {len(merged_results)} 個結果")
            return merged_results
            
        except Exception as e:
            error_log(f"[SemanticRetriever] 合併去重失敗: {e}")
            return all_results
    
    def _rerank_results(self, results: List[MemorySearchResult], query: MemoryQuery) -> List[MemorySearchResult]:
        """重排序結果"""
        try:
            # 計算綜合分數
            for result in results:
                comprehensive_score = self._calculate_comprehensive_score(result, query)
                result.similarity_score = comprehensive_score
            
            # 多樣性重排序
            if self.diversity_factor > 0:
                results = self._diversity_rerank(results)
            
            # 按分數排序
            results.sort(key=lambda x: x.similarity_score, reverse=True)
            
            debug_log(4, f"[SemanticRetriever] 重排序完成: {len(results)} 個結果")
            return results
            
        except Exception as e:
            error_log(f"[SemanticRetriever] 重排序失敗: {e}")
            return results
    
    def _calculate_comprehensive_score(self, result: MemorySearchResult, query: MemoryQuery) -> float:
        """計算綜合分數"""
        try:
            scores = {
                "semantic": result.similarity_score,
                "temporal": self._calculate_temporal_score(result.memory_entry),
                "context": result.context_relevance,
                "importance": self._calculate_importance_score(result.memory_entry)
            }
            
            # 加權計算
            comprehensive_score = 0.0
            for score_type, weight in self.similarity_weights.items():
                if score_type in scores:
                    comprehensive_score += scores[score_type] * weight
            
            return comprehensive_score
            
        except Exception as e:
            debug_log(1, f"[SemanticRetriever] 綜合分數計算失敗: {e}")
            return result.similarity_score
    
    def _calculate_temporal_score(self, memory_entry: MemoryEntry) -> float:
        """計算時間分數"""
        try:
            current_time = datetime.now()
            time_diff = (current_time - memory_entry.created_at).total_seconds()
            
            # 最近的記憶分數較高
            temporal_score = math.exp(-time_diff / (24 * 3600))  # 24小時衰減
            
            # 新近性提升
            if self.recency_boost > 0 and time_diff < 3600:  # 1小時內
                temporal_score += self.recency_boost
            
            return min(temporal_score, 1.0)
            
        except Exception as e:
            debug_log(1, f"[SemanticRetriever] 時間分數計算失敗: {e}")
            return 0.5
    
    def _calculate_importance_score(self, memory_entry: MemoryEntry) -> float:
        """計算重要性分數"""
        importance_mapping = {
            MemoryImportance.LOW: 0.2,
            MemoryImportance.MEDIUM: 0.5,
            MemoryImportance.HIGH: 0.8,
            MemoryImportance.CRITICAL: 1.0
        }
        return importance_mapping.get(memory_entry.importance, 0.5)
    
    def _diversity_rerank(self, results: List[MemorySearchResult]) -> List[MemorySearchResult]:
        """多樣性重排序"""
        try:
            if len(results) <= 1:
                return results
            
            reranked = [results[0]]  # 保留最高分結果
            remaining = results[1:]
            
            while remaining and len(reranked) < len(results):
                best_candidate = None
                best_score = -1
                
                for candidate in remaining:
                    # 計算與已選結果的多樣性
                    diversity_score = self._calculate_diversity_score(candidate, reranked)
                    combined_score = (candidate.similarity_score * (1 - self.diversity_factor) + 
                                    diversity_score * self.diversity_factor)
                    
                    if combined_score > best_score:
                        best_score = combined_score
                        best_candidate = candidate
                
                if best_candidate:
                    reranked.append(best_candidate)
                    remaining.remove(best_candidate)
            
            return reranked
            
        except Exception as e:
            error_log(f"[SemanticRetriever] 多樣性重排序失敗: {e}")
            return results
    
    def _calculate_diversity_score(self, candidate: MemorySearchResult, 
                                  selected: List[MemorySearchResult]) -> float:
        """計算多樣性分數"""
        try:
            if not selected:
                return 1.0
            
            # 主題多樣性
            candidate_topic = candidate.memory_entry.topic or ""
            selected_topics = {result.memory_entry.topic or "" for result in selected}
            
            topic_diversity = 1.0 if candidate_topic not in selected_topics else 0.5
            
            # 時間多樣性
            candidate_time = candidate.memory_entry.created_at
            time_differences = [abs((candidate_time - result.memory_entry.created_at).total_seconds()) 
                              for result in selected]
            avg_time_diff = sum(time_differences) / len(time_differences)
            time_diversity = min(avg_time_diff / 3600, 1.0)  # 正規化到小時
            
            return (topic_diversity + time_diversity) / 2
            
        except Exception as e:
            debug_log(1, f"[SemanticRetriever] 多樣性分數計算失敗: {e}")
            return 0.5
    
    def _generate_cache_key(self, query: MemoryQuery) -> str:
        """生成緩存鍵"""
        key_parts = [
            query.memory_token,
            query.query_text,
            str(sorted(query.memory_types) if query.memory_types else ""),
            str(query.max_results),
            str(query.similarity_threshold)
        ]
        return "|".join(key_parts)
    
    def _cache_results(self, cache_key: str, results: List[MemorySearchResult]):
        """緩存結果"""
        if len(self._search_cache) >= self.cache_size:
            # 移除最舊的緩存項
            oldest_key = next(iter(self._search_cache))
            del self._search_cache[oldest_key]
        
        self._search_cache[cache_key] = results
    
    def clear_cache(self):
        """清理緩存"""
        self._search_cache.clear()
        debug_log(3, "[SemanticRetriever] 緩存已清理")
    
    def get_stats(self) -> Dict[str, Any]:
        """獲取統計資訊"""
        cache_hit_rate = (self.stats["cache_hits"] / 
                         max(self.stats["searches_performed"], 1)) * 100
        
        return {
            **self.stats,
            "cache_hit_rate": cache_hit_rate,
            "cached_queries": len(self._search_cache),
            "enabled_strategies": len(self.retrieval_strategies)
        }
