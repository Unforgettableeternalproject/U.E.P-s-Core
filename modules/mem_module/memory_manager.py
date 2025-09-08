# modules/mem_module/memory_manager.py
"""
核心記憶管理器 - MEM模組的主要控制介面

功能：
- 短期與長期記憶管理
- 記憶檢索與存儲
- 記憶類型分類
- 上下文相關記憶整合
"""

import time
import json
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional, Set, Tuple
from dataclasses import dataclass

from utils.debug_helper import debug_log, info_log, error_log
from .schemas import (
    MemoryEntry, MemoryQuery, MemorySearchResult, MemoryOperationResult,
    MemoryType, MemoryImportance, LLMMemoryInstruction
)
from .storage.storage_manager import MemoryStorageManager


@dataclass
class MemoryContext:
    """記憶上下文"""
    current_session_id: str
    current_intent: str
    user_profile: Dict[str, Any]
    recent_memories: List[str]  # memory_ids
    active_topics: Set[str]
    conversation_depth: int
    
    
class MemoryManager:
    """核心記憶管理器"""
    
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        
        # 存儲管理器
        storage_config = config.get("storage", {})
        self.storage_manager = MemoryStorageManager(storage_config)
        
        # 記憶管理配置
        self.max_short_term_memories = config.get("max_short_term_memories", 50)
        self.short_term_duration = config.get("short_term_duration", 3600)  # 1小時
        self.consolidation_interval = config.get("consolidation_interval", 7200)  # 2小時
        self.importance_threshold = config.get("importance_threshold", 0.7)
        
        # 記憶分類配置
        self.topic_extraction_enabled = config.get("topic_extraction", True)
        self.intent_tracking_enabled = config.get("intent_tracking", True)
        
        # 當前上下文
        self.current_context: Optional[MemoryContext] = None
        
        # 短期記憶緩存
        self._short_term_cache: Dict[str, MemoryEntry] = {}
        self._last_consolidation = time.time()
        
        # 統計資訊
        self.stats = {
            "memories_stored": 0,
            "memories_retrieved": 0,
            "memories_consolidated": 0,
            "last_consolidation": None
        }
        
        self.is_initialized = False
    
    def initialize(self) -> bool:
        """初始化記憶管理器"""
        try:
            info_log("[MemoryManager] 初始化記憶管理器...")
            
            # 初始化存儲管理器
            if not self.storage_manager.initialize():
                error_log("[MemoryManager] 存儲管理器初始化失敗")
                return False
            
            # 載入最近的短期記憶到緩存
            self._load_recent_memories()
            
            self.is_initialized = True
            info_log("[MemoryManager] 記憶管理器初始化完成")
            return True
            
        except Exception as e:
            error_log(f"[MemoryManager] 初始化失敗: {e}")
            return False
    
    def _load_recent_memories(self):
        """載入最近的短期記憶到緩存"""
        try:
            debug_log(2, "[MemoryManager] 載入最近短期記憶...")
            
            # 查詢最近的短期記憶
            recent_query = MemoryQuery(
                identity_token="system",  # 暫時使用系統令牌
                query_text="",
                memory_types=[MemoryType.SNAPSHOT],
                time_range=(time.time() - self.short_term_duration, time.time()),
                max_results=self.max_short_term_memories,
                similarity_threshold=0.0
            )
            
            # 這裡需要系統身份令牌，暫時跳過載入
            debug_log(3, "[MemoryManager] 跳過載入最近記憶（需要系統令牌）")
            
        except Exception as e:
            info_log("WARNING", f"[MemoryManager] 載入最近記憶失敗: {e}")
    
    def set_context(self, context: MemoryContext):
        """設定當前記憶上下文"""
        self.current_context = context
        debug_log(3, f"[MemoryManager] 設定記憶上下文: {context.current_session_id}")
    
    def store_memory(self, content: str, memory_type: MemoryType = MemoryType.SNAPSHOT,
                    importance: MemoryImportance = MemoryImportance.MEDIUM,
                    identity_token: str = "", topic: str = None,
                    intent_tags: List[str] = None, metadata: Dict[str, Any] = None) -> MemoryOperationResult:
        """存儲新記憶"""
        try:
            # 創建記憶條目
            memory_entry = MemoryEntry(
                memory_id=self._generate_memory_id(),
                identity_token=identity_token,
                memory_type=memory_type,
                content=content,
                importance=importance,
                topic=topic or self._extract_topic(content),
                intent_tags=intent_tags or self._extract_intent_tags(content),
                created_at=datetime.now(),
                metadata=metadata or {}
            )
            
            # 自動設定上下文資訊
            if self.current_context:
                memory_entry.session_id = self.current_context.current_session_id
                if not memory_entry.intent_tags:
                    memory_entry.intent_tags = [self.current_context.current_intent]
                
                # 更新元資料
                memory_entry.metadata.update({
                    "conversation_depth": self.current_context.conversation_depth,
                    "active_topics": list(self.current_context.active_topics)
                })
            
            # 存儲到持久化存儲
            result = self.storage_manager.store_memory(memory_entry)
            
            if result.success:
                # 短期記憶加入緩存
                if memory_type == MemoryType.SNAPSHOT:
                    self._short_term_cache[memory_entry.memory_id] = memory_entry
                    self._manage_short_term_cache()
                
                # 更新統計
                self.stats["memories_stored"] += 1
                
                # 更新上下文
                if self.current_context:
                    self.current_context.recent_memories.append(memory_entry.memory_id)
                    if memory_entry.topic:
                        self.current_context.active_topics.add(memory_entry.topic)
                
                debug_log(3, f"[MemoryManager] 記憶存儲成功: {memory_entry.memory_id}")
            
            return result
            
        except Exception as e:
            error_log(f"[MemoryManager] 存儲記憶失敗: {e}")
            return MemoryOperationResult(
                success=False,
                operation_type="store",
                message=f"存儲異常: {str(e)}"
            )
    
    def retrieve_memories(self, query_text: str, identity_token: str,
                         memory_types: List[MemoryType] = None,
                         max_results: int = 10,
                         similarity_threshold: float = 0.6,
                         include_context: bool = True) -> List[MemorySearchResult]:
        """檢索相關記憶"""
        try:
            # 構建查詢
            query = MemoryQuery(
                identity_token=identity_token,
                query_text=query_text,
                memory_types=memory_types or [MemoryType.SNAPSHOT, MemoryType.EPISODE, MemoryType.SEMANTIC],
                max_results=max_results,
                similarity_threshold=similarity_threshold,
                current_intent=self.current_context.current_intent if self.current_context else None,
                include_archived=False
            )
            
            # 從存儲檢索
            results = self.storage_manager.search_memories(query)
            
            # 包含上下文相關記憶
            if include_context and self.current_context:
                context_results = self._get_context_relevant_memories(query)
                results = self._merge_results(results, context_results, max_results)
            
            # 更新統計
            self.stats["memories_retrieved"] += len(results)
            
            debug_log(3, f"[MemoryManager] 檢索到 {len(results)} 條記憶")
            return results
            
        except Exception as e:
            error_log(f"[MemoryManager] 檢索記憶失敗: {e}")
            return []
    
    def _get_context_relevant_memories(self, query: MemoryQuery) -> List[MemorySearchResult]:
        """獲取上下文相關記憶"""
        try:
            context_results = []
            
            if not self.current_context:
                return context_results
            
            # 檢索相同會話的記憶
            if self.current_context.recent_memories:
                session_query = MemoryQuery(
                    identity_token=query.identity_token,
                    query_text="",
                    memory_ids=self.current_context.recent_memories[-5:],  # 最近5條
                    similarity_threshold=0.0
                )
                
                session_results = self.storage_manager.search_memories(session_query)
                context_results.extend(session_results)
            
            # 檢索相同主題的記憶
            if self.current_context.active_topics:
                for topic in list(self.current_context.active_topics)[:3]:  # 最多3個主題
                    topic_query = MemoryQuery(
                        identity_token=query.identity_token,
                        query_text=query.query_text,
                        topic_filter=topic,
                        max_results=3,
                        similarity_threshold=query.similarity_threshold * 0.8  # 降低閾值
                    )
                    
                    topic_results = self.storage_manager.search_memories(topic_query)
                    context_results.extend(topic_results)
            
            return context_results
            
        except Exception as e:
            info_log("WARNING", f"[MemoryManager] 獲取上下文記憶失敗: {e}")
            return []
    
    def _merge_results(self, primary_results: List[MemorySearchResult], 
                      context_results: List[MemorySearchResult], 
                      max_results: int) -> List[MemorySearchResult]:
        """合併搜索結果"""
        try:
            # 去重
            seen_ids = set()
            merged_results = []
            
            # 優先加入主要結果
            for result in primary_results:
                if result.memory_entry.memory_id not in seen_ids:
                    merged_results.append(result)
                    seen_ids.add(result.memory_entry.memory_id)
            
            # 加入上下文結果
            for result in context_results:
                if (result.memory_entry.memory_id not in seen_ids and 
                    len(merged_results) < max_results):
                    # 標記為上下文相關
                    result.retrieval_reason += "、上下文相關"
                    merged_results.append(result)
                    seen_ids.add(result.memory_entry.memory_id)
            
            # 重新排序（相關性優先）
            merged_results.sort(key=lambda x: x.relevance_score, reverse=True)
            
            return merged_results[:max_results]
            
        except Exception as e:
            info_log("WARNING", f"[MemoryManager] 合併結果失敗: {e}")
            return primary_results[:max_results]
    
    def consolidate_memories(self, identity_token: str) -> MemoryOperationResult:
        """整合記憶（短期轉長期）"""
        try:
            info_log("[MemoryManager] 開始記憶整合...")
            
            current_time = time.time()
            cutoff_time = current_time - self.short_term_duration
            
            # 查詢需要整合的短期記憶
            query = MemoryQuery(
                identity_token=identity_token,
                query_text="",
                memory_types=[MemoryType.SNAPSHOT],
                time_range=(0, cutoff_time),
                similarity_threshold=0.0,
                max_results=100
            )
            
            candidate_memories = self.storage_manager.search_memories(query)
            
            if not candidate_memories:
                debug_log(3, "[MemoryManager] 沒有需要整合的記憶")
                return MemoryOperationResult(
                    success=True,
                    operation_type="consolidate",
                    message="沒有需要整合的記憶"
                )
            
            # 記憶重要性評估和分組
            important_memories = []
            grouped_memories = {}
            
            for result in candidate_memories:
                memory = result.memory_entry
                
                # 重要性評估
                importance_score = self._evaluate_importance(memory)
                
                if importance_score >= self.importance_threshold:
                    important_memories.append(memory)
                else:
                    # 按主題分組
                    topic = memory.topic or "general"
                    if topic not in grouped_memories:
                        grouped_memories[topic] = []
                    grouped_memories[topic].append(memory)
            
            consolidated_count = 0
            
            # 重要記憶直接轉為語意記憶
            for memory in important_memories:
                if self._convert_to_semantic_memory(memory, identity_token):
                    consolidated_count += 1
            
            # 分組記憶合併為情節記憶
            for topic, memories in grouped_memories.items():
                if len(memories) >= 3:  # 至少3條記憶才合併
                    if self._create_episode_memory(memories, topic, identity_token):
                        consolidated_count += len(memories)
            
            self.stats["memories_consolidated"] += consolidated_count
            self.stats["last_consolidation"] = datetime.now().isoformat()
            self._last_consolidation = current_time
            
            info_log(f"[MemoryManager] 記憶整合完成，整合了 {consolidated_count} 條記憶")
            
            return MemoryOperationResult(
                success=True,
                operation_type="consolidate",
                message=f"整合了 {consolidated_count} 條記憶",
                affected_count=consolidated_count
            )
            
        except Exception as e:
            error_log(f"[MemoryManager] 記憶整合失敗: {e}")
            return MemoryOperationResult(
                success=False,
                operation_type="consolidate",
                message=f"整合異常: {str(e)}"
            )
    
    def _evaluate_importance(self, memory: MemoryEntry) -> float:
        """評估記憶重要性"""
        score = 0.0
        
        try:
            # 基礎重要性
            importance_scores = {
                MemoryImportance.CRITICAL: 1.0,
                MemoryImportance.HIGH: 0.8,
                MemoryImportance.MEDIUM: 0.6,
                MemoryImportance.LOW: 0.4,
                MemoryImportance.TEMPORARY: 0.2
            }
            score += importance_scores.get(memory.importance, 0.6)
            
            # 存取頻率
            access_score = min(0.3, memory.access_count * 0.05)
            score += access_score
            
            # 內容長度（較長的內容可能更重要）
            content_score = min(0.2, len(memory.content) / 1000)
            score += content_score
            
            # 關鍵字詞檢測
            important_keywords = ["重要", "記住", "不要忘記", "關鍵", "核心"]
            if any(keyword in memory.content for keyword in important_keywords):
                score += 0.3
            
            return min(1.0, score)
            
        except Exception as e:
            info_log("WARNING", f"[MemoryManager] 評估重要性失敗: {e}")
            return 0.5
    
    def _convert_to_semantic_memory(self, memory: MemoryEntry, identity_token: str) -> bool:
        """轉換為語意記憶"""
        try:
            # 創建語意記憶
            semantic_memory = MemoryEntry(
                memory_id=self._generate_memory_id(),
                identity_token=identity_token,
                memory_type=MemoryType.SEMANTIC,
                content=memory.content,
                importance=MemoryImportance.HIGH,
                topic=memory.topic,
                intent_tags=memory.intent_tags,
                created_at=datetime.now(),
                metadata={
                    **memory.metadata,
                    "original_memory_id": memory.memory_id,
                    "consolidation_time": datetime.now().isoformat(),
                    "consolidation_reason": "high_importance"
                }
            )
            
            # 存儲語意記憶
            result = self.storage_manager.store_memory(semantic_memory)
            
            if result.success:
                # 刪除原始快照記憶
                self.storage_manager.delete_memory(memory.memory_id, identity_token)
                debug_log(3, f"[MemoryManager] 轉換為語意記憶: {memory.memory_id} -> {semantic_memory.memory_id}")
                return True
            
            return False
            
        except Exception as e:
            info_log("WARNING", f"[MemoryManager] 轉換語意記憶失敗: {e}")
            return False
    
    def _create_episode_memory(self, memories: List[MemoryEntry], topic: str, identity_token: str) -> bool:
        """創建情節記憶"""
        try:
            # 合併內容
            combined_content = self._summarize_memories(memories)
            
            # 創建情節記憶
            episode_memory = MemoryEntry(
                memory_id=self._generate_memory_id(),
                identity_token=identity_token,
                memory_type=MemoryType.EPISODE,
                content=combined_content,
                importance=MemoryImportance.MEDIUM,
                topic=topic,
                intent_tags=list(set(tag for memory in memories for tag in memory.intent_tags)),
                created_at=datetime.now(),
                metadata={
                    "original_memories": [m.memory_id for m in memories],
                    "consolidation_time": datetime.now().isoformat(),
                    "consolidation_reason": "topic_grouping",
                    "memory_count": len(memories)
                }
            )
            
            # 存儲情節記憶
            result = self.storage_manager.store_memory(episode_memory)
            
            if result.success:
                # 刪除原始記憶
                for memory in memories:
                    self.storage_manager.delete_memory(memory.memory_id, identity_token)
                
                debug_log(3, f"[MemoryManager] 創建情節記憶: {episode_memory.memory_id}，合併 {len(memories)} 條記憶")
                return True
            
            return False
            
        except Exception as e:
            info_log("WARNING", f"[MemoryManager] 創建情節記憶失敗: {e}")
            return False
    
    def _summarize_memories(self, memories: List[MemoryEntry]) -> str:
        """摘要記憶內容"""
        try:
            # 簡單的內容合併，未來可以使用LLM進行智慧摘要
            contents = [memory.content for memory in memories]
            
            if len(contents) <= 3:
                return " | ".join(contents)
            
            # 取前三條和最後一條
            summary_parts = contents[:3] + ["..."] + contents[-1:]
            return " | ".join(summary_parts)
            
        except Exception as e:
            info_log("WARNING", f"[MemoryManager] 摘要記憶失敗: {e}")
            return "記憶摘要失敗"
    
    def _manage_short_term_cache(self):
        """管理短期記憶緩存"""
        try:
            current_time = time.time()
            cutoff_time = current_time - self.short_term_duration
            
            # 移除過期記憶
            expired_ids = []
            for memory_id, memory in self._short_term_cache.items():
                if memory.created_at.timestamp() < cutoff_time:
                    expired_ids.append(memory_id)
            
            for memory_id in expired_ids:
                del self._short_term_cache[memory_id]
            
            # 限制緩存大小
            if len(self._short_term_cache) > self.max_short_term_memories:
                # 移除最舊的記憶
                sorted_memories = sorted(
                    self._short_term_cache.items(),
                    key=lambda x: x[1].created_at
                )
                
                for memory_id, _ in sorted_memories[:-self.max_short_term_memories]:
                    del self._short_term_cache[memory_id]
                    
            debug_log(3, f"[MemoryManager] 短期記憶緩存: {len(self._short_term_cache)} 條")
            
        except Exception as e:
            info_log("WARNING", f"[MemoryManager] 管理短期緩存失敗: {e}")
    
    def _generate_memory_id(self) -> str:
        """生成記憶ID"""
        import uuid
        return f"mem_{int(time.time())}_{uuid.uuid4().hex[:8]}"
    
    def _extract_topic(self, content: str) -> str:
        """提取主題（簡單實現）"""
        if not self.topic_extraction_enabled:
            return "general"
        
        try:
            # 簡單的關鍵字匹配
            topics = {
                "學習": ["學習", "教學", "課程", "知識"],
                "工作": ["工作", "任務", "項目", "會議"],
                "生活": ["生活", "日常", "家庭", "朋友"],
                "技術": ["程式", "代碼", "開發", "技術"],
                "娛樂": ["遊戲", "電影", "音樂", "娛樂"]
            }
            
            for topic, keywords in topics.items():
                if any(keyword in content for keyword in keywords):
                    return topic
            
            return "general"
            
        except Exception as e:
            info_log("WARNING", f"[MemoryManager] 提取主題失敗: {e}")
            return "general"
    
    def _extract_intent_tags(self, content: str) -> List[str]:
        """提取意圖標籤（簡單實現）"""
        if not self.intent_tracking_enabled:
            return ["general"]
        
        try:
            intents = []
            
            # 意圖關鍵字
            intent_patterns = {
                "question": ["什麼", "如何", "為什麼", "怎麼", "?", "？"],
                "request": ["請", "幫", "能否", "可以"],
                "information": ["是", "有", "沒有", "關於"],
                "expression": ["我覺得", "我認為", "我想", "我希望"]
            }
            
            for intent, patterns in intent_patterns.items():
                if any(pattern in content for pattern in patterns):
                    intents.append(intent)
            
            return intents if intents else ["general"]
            
        except Exception as e:
            info_log("WARNING", f"[MemoryManager] 提取意圖失敗: {e}")
            return ["general"]
    
    def generate_llm_instruction(self, relevant_memories: List[MemorySearchResult]) -> LLMMemoryInstruction:
        """為LLM生成記憶指令"""
        try:
            if not relevant_memories:
                return LLMMemoryInstruction(
                    has_relevant_memory=False,
                    memory_summary="沒有找到相關記憶",
                    usage_instruction="基於當前對話回應",
                    confidence_level=0.0
                )
            
            # 生成記憶摘要
            memory_contents = []
            topics = set()
            total_confidence = 0.0
            
            for result in relevant_memories:
                memory = result.memory_entry
                memory_contents.append(f"[{memory.memory_type.value}] {memory.content}")
                if memory.topic:
                    topics.add(memory.topic)
                total_confidence += result.relevance_score
            
            avg_confidence = total_confidence / len(relevant_memories)
            
            memory_summary = " | ".join(memory_contents[:5])  # 最多5條
            topic_list = ", ".join(topics) if topics else "一般"
            
            # 生成使用指令
            if avg_confidence > 0.8:
                usage_instruction = "高度相關記憶，請直接參考並整合到回應中"
            elif avg_confidence > 0.6:
                usage_instruction = "中度相關記憶，請適當參考並結合當前對話"
            else:
                usage_instruction = "低度相關記憶，請謹慎參考，以當前對話為主"
            
            return LLMMemoryInstruction(
                has_relevant_memory=True,
                memory_summary=memory_summary,
                topic_context=topic_list,
                memory_count=len(relevant_memories),
                usage_instruction=usage_instruction,
                confidence_level=avg_confidence
            )
            
        except Exception as e:
            error_log(f"[MemoryManager] 生成LLM指令失敗: {e}")
            return LLMMemoryInstruction(
                has_relevant_memory=False,
                memory_summary="記憶處理錯誤",
                usage_instruction="基於當前對話回應",
                confidence_level=0.0
            )
    
    def get_memory_stats(self) -> Dict[str, Any]:
        """獲取記憶統計資訊"""
        try:
            storage_stats = self.storage_manager.get_storage_stats()
            
            return {
                "manager_stats": self.stats,
                "storage_stats": storage_stats,
                "short_term_cache": {
                    "count": len(self._short_term_cache),
                    "max_capacity": self.max_short_term_memories
                },
                "consolidation": {
                    "last_consolidation": self._last_consolidation,
                    "next_consolidation": self._last_consolidation + self.consolidation_interval
                },
                "context": {
                    "has_context": self.current_context is not None,
                    "session_id": self.current_context.current_session_id if self.current_context else None
                },
                "is_initialized": self.is_initialized
            }
            
        except Exception as e:
            error_log(f"[MemoryManager] 獲取統計失敗: {e}")
            return {}
