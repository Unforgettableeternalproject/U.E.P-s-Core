# modules/mem_module/analysis/memory_summarizer.py
"""
記憶總結器 - 為 LLM 提供記憶總結服務

功能：
- 對話記憶切塊總結
- 多層次記憶摘要生成
- LLM 上下文優化
- 記憶重要性評估與排序
"""

import time
from typing import List, Dict, Any, Optional
from transformers import pipeline

from utils.debug_helper import debug_log, info_log, error_log
from ..schemas import MemorySearchResult, MemoryEntry, MemoryType, MemoryImportance


class MemorySummarizer:
    """記憶總結器 - 將記憶內容總結為 LLM 可用的上下文"""
    
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        
        # 總結模型配置
        self.summarization_model = config.get("summarization_model", "philschmid/bart-large-cnn-samsum")
        self.chunk_size = config.get("chunk_size", 3)
        self.max_summary_length = config.get("max_summary_length", 120)
        self.min_summary_length = config.get("min_summary_length", 20)
        
        # 總結器延遲初始化
        self._summarizer = None
        
        # 上下文優化配置
        self.max_context_length = config.get("max_context_length", 4000)
        self.importance_weight = config.get("importance_weight", 0.3)
        self.recency_weight = config.get("recency_weight", 0.4)
        self.relevance_weight = config.get("relevance_weight", 0.3)
        
        # 統計資訊
        self.stats = {
            "summaries_generated": 0,
            "chunks_processed": 0,
            "average_compression_ratio": 0.0,
            "last_summarization_time": None
        }
        
        self.is_initialized = False
    
    def initialize(self) -> bool:
        """初始化記憶總結器"""
        try:
            info_log("[MemorySummarizer] 初始化記憶總結器...")
            
            # 初始化總結模型
            info_log(f"[MemorySummarizer] 載入總結模型: {self.summarization_model}")
            self._summarizer = pipeline(
                "summarization", 
                model=self.summarization_model,
                device=-1  # 使用 CPU，避免 GPU 依賴
            )
            
            self.is_initialized = True
            info_log("[MemorySummarizer] 記憶總結器初始化完成")
            return True
            
        except Exception as e:
            error_log(f"[MemorySummarizer] 初始化失敗: {e}")
            return False
    
    def chunk_and_summarize_memories(self, memories: List[str], 
                                   chunk_size: Optional[int] = None) -> str:
        """
        將記憶內容切塊並總結 - 從 prompt_builder.py 遷移的功能
        
        Args:
            memories: 記憶內容列表
            chunk_size: 切塊大小，None 則使用預設值
            
        Returns:
            總結後的記憶文本
        """
        try:
            if not self.is_initialized:
                error_log("[MemorySummarizer] 總結器未初始化")
                return ""
            
            if not memories:
                debug_log(2, "[MemorySummarizer] 無記憶內容需要總結")
                return ""
            
            chunk_size = chunk_size or self.chunk_size
            chunks = [memories[i:i+chunk_size] for i in range(0, len(memories), chunk_size)]
            summaries = []
            
            debug_log(2, f"[MemorySummarizer] 記憶切塊大小: {chunk_size}")
            debug_log(3, f"[MemorySummarizer] 記憶切塊數量: {len(chunks)}")
            
            start_time = time.time()
            
            for group in chunks:
                text_block = "\n".join(group)
                
                # 檢查文本長度，避免過短的文本
                if len(text_block.strip()) < 10:
                    continue
                
                try:
                    summary = self._summarizer(
                        text_block, 
                        max_length=self.max_summary_length, 
                        min_length=self.min_summary_length, 
                        do_sample=False
                    )[0]["summary_text"]
                    summaries.append(summary)
                    
                except Exception as e:
                    debug_log(1, f"[MemorySummarizer] 單塊總結失敗，使用原文: {e}")
                    # 如果總結失敗，使用截取的原文
                    summaries.append(text_block[:self.max_summary_length] + "...")
            
            # 更新統計
            self.stats["summaries_generated"] += 1
            self.stats["chunks_processed"] += len(chunks)
            self.stats["last_summarization_time"] = time.time()
            
            # 計算壓縮比
            original_length = sum(len(m) for m in memories)
            summary_length = sum(len(s) for s in summaries)
            compression_ratio = summary_length / original_length if original_length > 0 else 0.0
            self.stats["average_compression_ratio"] = (
                self.stats["average_compression_ratio"] * (self.stats["summaries_generated"] - 1) + compression_ratio
            ) / self.stats["summaries_generated"]
            
            processing_time = time.time() - start_time
            debug_log(2, f"[MemorySummarizer] 總結完成，耗時: {processing_time:.2f}s")
            debug_log(3, f"[MemorySummarizer] 記憶摘要數量: {len(summaries)}")
            
            return "\n".join(summaries)
            
        except Exception as e:
            error_log(f"[MemorySummarizer] 記憶總結失敗: {e}")
            return ""
    
    def summarize_search_results(self, search_results: List[MemorySearchResult],
                               context: str = "") -> str:
        """
        總結記憶搜索結果為 LLM 上下文
        
        Args:
            search_results: 記憶搜索結果
            context: 額外上下文信息
            
        Returns:
            為 LLM 優化的記憶上下文
        """
        try:
            if not search_results:
                return ""
            
            # 按重要性和相關性排序
            sorted_results = self._prioritize_memories(search_results)
            
            # 提取記憶內容
            memory_contents = []
            for result in sorted_results:
                memory = result.memory_entry
                
                # 構建記憶條目描述
                memory_desc = f"[{memory.memory_type.value}] {memory.content}"
                
                # 添加重要性標註
                if memory.importance in [MemoryImportance.HIGH, MemoryImportance.CRITICAL]:
                    memory_desc = f"❗ {memory_desc}"
                
                # 添加時間信息
                if memory.created_at:
                    time_info = memory.created_at.strftime("%Y-%m-%d")
                    memory_desc += f" ({time_info})"
                
                memory_contents.append(memory_desc)
            
            # 進行分塊總結
            summary = self.chunk_and_summarize_memories(memory_contents)
            
            # 添加上下文前綴
            if context:
                summary = f"當前上下文: {context}\n\n相關記憶:\n{summary}"
            
            return summary
            
        except Exception as e:
            error_log(f"[MemorySummarizer] 搜索結果總結失敗: {e}")
            return ""
    
    def create_llm_memory_context(self, search_results: List[MemorySearchResult],
                                current_query: str = "", 
                                max_length: Optional[int] = None) -> Dict[str, Any]:
        """
        為 LLM 創建結構化記憶上下文
        
        Args:
            search_results: 記憶搜索結果
            current_query: 當前查詢
            max_length: 最大上下文長度
            
        Returns:
            結構化的記憶上下文
        """
        try:
            max_length = max_length or self.max_context_length
            
            if not search_results:
                return {
                    "summary": "暫無相關記憶",
                    "key_facts": [],
                    "recent_conversations": [],
                    "important_reminders": [],
                    "context_confidence": 0.0
                }
            
            # 分類記憶
            key_facts = []
            conversations = []
            reminders = []
            
            total_confidence = 0.0
            
            for result in search_results:
                memory = result.memory_entry
                confidence = result.similarity_score
                total_confidence += confidence
                
                # 關鍵事實 (高重要性記憶)
                if memory.importance in [MemoryImportance.HIGH, MemoryImportance.CRITICAL]:
                    key_facts.append({
                        "content": memory.content[:200],
                        "importance": memory.importance.value,
                        "confidence": confidence
                    })
                
                # 對話記憶
                if memory.memory_type == MemoryType.SNAPSHOT:
                    conversations.append({
                        "content": memory.content[:150],
                        "timestamp": memory.created_at.isoformat() if memory.created_at else None,
                        "confidence": confidence
                    })
                
                # 重要提醒
                if memory.importance == MemoryImportance.CRITICAL:
                    reminders.append(memory.content[:100])
            
            # 生成總結
            memory_texts = [result.memory_entry.content for result in search_results[:10]]
            summary = self.chunk_and_summarize_memories(memory_texts, chunk_size=2)
            
            # 計算平均信心度
            avg_confidence = total_confidence / len(search_results) if search_results else 0.0
            
            return {
                "summary": summary or "記憶總結生成中...",
                "key_facts": key_facts[:5],  # 最多5個關鍵事實
                "recent_conversations": conversations[-3:],  # 最近3個對話
                "important_reminders": reminders[:3],  # 最多3個提醒
                "context_confidence": avg_confidence,
                "total_memories": len(search_results),
                "query": current_query
            }
            
        except Exception as e:
            error_log(f"[MemorySummarizer] 創建LLM上下文失敗: {e}")
            return {
                "summary": "記憶處理異常",
                "key_facts": [],
                "recent_conversations": [],
                "important_reminders": [],
                "context_confidence": 0.0
            }
    
    def _prioritize_memories(self, search_results: List[MemorySearchResult]) -> List[MemorySearchResult]:
        """按優先級排序記憶"""
        try:
            def calculate_priority(result: MemorySearchResult) -> float:
                memory = result.memory_entry
                similarity = result.similarity_score
                
                # 重要性分數
                importance_scores = {
                    MemoryImportance.CRITICAL: 1.0,
                    MemoryImportance.HIGH: 0.8,
                    MemoryImportance.MEDIUM: 0.6,
                    MemoryImportance.LOW: 0.4,
                    MemoryImportance.TEMPORARY: 0.2
                }
                importance_score = importance_scores.get(memory.importance, 0.6)
                
                # 時間新近性分數 (簡化計算)
                recency_score = 0.8  # 暫時給固定值
                
                # 綜合評分
                priority = (
                    similarity * self.relevance_weight +
                    importance_score * self.importance_weight +
                    recency_score * self.recency_weight
                )
                
                return priority
            
            return sorted(search_results, key=calculate_priority, reverse=True)
            
        except Exception as e:
            error_log(f"[MemorySummarizer] 記憶排序失敗: {e}")
            return search_results
    
    def get_stats(self) -> Dict[str, Any]:
        """獲取總結器統計資訊"""
        return {
            "summarizer_stats": self.stats.copy(),
            "config": {
                "model": self.summarization_model,
                "chunk_size": self.chunk_size,
                "max_context_length": self.max_context_length
            },
            "is_initialized": self.is_initialized
        }