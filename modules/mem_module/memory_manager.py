# modules/mem_module/memory_manager_v2.py
"""
重構的記憶管理器 - MEM模組的主要協調介面

這是一個重構版本，將大型記憶管理器分解為更小的專門化模組：
- IdentityManager: 身份管理
- SnapshotManager: 快照管理  
- SemanticRetriever: 語義檢索
- MemoryAnalyzer: 記憶分析

功能：
- 作為各子模組的協調器
- 提供統一的對外介面
- 整合各模組功能
- 管理模組間的資料流
"""

import time
import json
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional, Set, Tuple
from dataclasses import dataclass

from utils.debug_helper import debug_log, info_log, error_log
from .schemas import (
    MemoryEntry, MemoryQuery, MemorySearchResult, MemoryOperationResult,
    MemoryType, MemoryImportance, LLMMemoryInstruction, ConversationSnapshot
)
from .storage.storage_manager import MemoryStorageManager

# 導入子模組
from .core.identity_manager import IdentityManager
from .core.snapshot_manager import SnapshotManager
from .retrieval.semantic_retriever import SemanticRetriever
from .analysis.memory_analyzer import MemoryAnalyzer


@dataclass
class MemoryContext:
    """記憶上下文 - 重構版本"""
    current_session_id: str
    current_intent: str
    user_profile: Dict[str, Any]
    recent_memories: List[str]  # memory_ids
    active_topics: Set[str]
    conversation_depth: int
    

class MemoryManager:
    """重構的記憶管理器 - 協調器模式"""
    
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        
        # 初始化存儲管理器
        storage_config = config.get("storage", {})
        self.storage_manager = MemoryStorageManager(storage_config)
        
        # 初始化子模組
        identity_config = config.get("identity", {})
        self.identity_manager = IdentityManager(identity_config)
        
        snapshot_config = config.get("snapshot", {})
        self.snapshot_manager = SnapshotManager(snapshot_config)
        
        retrieval_config = config.get("retrieval", {})
        self.semantic_retriever = SemanticRetriever(retrieval_config, self.storage_manager)
        
        analysis_config = config.get("analysis", {})
        self.memory_analyzer = MemoryAnalyzer(analysis_config)
        
        # 初始化記憶總結器
        summarization_config = config.get("summarization", {})
        self.memory_summarizer = None  # 延遲初始化
        self.summarization_config = summarization_config
        
        # 記憶管理配置
        self.max_short_term_memories = config.get("max_short_term_memories", 50)
        self.consolidation_interval = config.get("consolidation_interval", 7200)  # 2小時
        self.importance_threshold = config.get("importance_threshold", 0.7)
        
        # 當前上下文
        self.current_context: Optional[MemoryContext] = None
        
        # 統計資訊
        self.stats = {
            "memories_stored": 0,
            "memories_retrieved": 0,
            "memories_consolidated": 0,
            "sessions_managed": 0,
            "last_consolidation": None
        }
        
        self.is_initialized = False
    
    def initialize(self) -> bool:
        """初始化記憶管理器"""
        try:
            info_log("[MemoryManagerV2] 初始化重構記憶管理器...")
            
            # 初始化存儲管理器
            if not self.storage_manager.initialize():
                error_log("[MemoryManagerV2] 存儲管理器初始化失敗")
                return False
            
            # 初始化身份管理器
            if not self.identity_manager.initialize():
                error_log("[MemoryManagerV2] 身份管理器初始化失敗")
                return False
            
            # 初始化快照管理器
            if not self.snapshot_manager.initialize():
                error_log("[MemoryManagerV2] 快照管理器初始化失敗")
                return False
            
            # 初始化語義檢索器
            if not self.semantic_retriever.initialize():
                error_log("[MemoryManagerV2] 語義檢索器初始化失敗")
                return False
            
            # 初始化記憶總結器 (可選功能)
            if self.summarization_config.get("enable_external_summarization", True):
                try:
                    from .analysis.memory_summarizer import MemorySummarizer
                    self.memory_summarizer = MemorySummarizer(self.summarization_config)
                    if self.memory_summarizer.initialize():
                        info_log("[MemoryManagerV2] 記憶總結器初始化成功")
                    else:
                        info_log("WARNING", "[MemoryManagerV2] 記憶總結器初始化失敗，將使用基本總結")
                        self.memory_summarizer = None
                except Exception as e:
                    info_log("WARNING", f"[MemoryManagerV2] 記憶總結器載入失敗: {e}")
                    self.memory_summarizer = None
            
            # 初始化記憶分析器
            if not self.memory_analyzer.initialize():
                error_log("[MemoryManagerV2] 記憶分析器初始化失敗")
                return False
            
            self.is_initialized = True
            info_log("[MemoryManagerV2] 重構記憶管理器初始化完成")
            return True
            
        except Exception as e:
            error_log(f"[MemoryManagerV2] 初始化失敗: {e}")
            return False
    
    def set_context(self, context: MemoryContext):
        """設定當前記憶上下文"""
        self.current_context = context
        debug_log(3, f"[MemoryManagerV2] 設定記憶上下文: {context.current_session_id}")
    
    def start_session(self, session_id: str, memory_token: str = None, 
                     initial_context: Dict[str, Any] = None) -> bool:
        """開始新會話"""
        try:
            # 如果沒有提供記憶令牌，從Working Context獲取
            if not memory_token:
                memory_token = self.identity_manager.get_current_memory_token()
            
            # 驗證記憶體存取權限
            if not self.identity_manager.validate_memory_access(memory_token, "write"):
                error_log(f"[MemoryManagerV2] 記憶體存取權限驗證失敗: {memory_token}")
                return False
            
            # 開始快照會話
            if not self.snapshot_manager.start_session_snapshot(session_id, memory_token, initial_context):
                error_log(f"[MemoryManagerV2] 快照會話開始失敗: {session_id}")
                return False
            
            # 設定記憶上下文
            context = MemoryContext(
                current_session_id=session_id,
                current_intent="一般對話",
                user_profile=initial_context or {},
                recent_memories=[],
                active_topics=set(),
                conversation_depth=0
            )
            self.set_context(context)
            
            self.stats["sessions_managed"] += 1
            info_log(f"[MemoryManagerV2] 會話開始: {session_id} (記憶體令牌: {memory_token})")
            return True
            
        except Exception as e:
            error_log(f"[MemoryManagerV2] 開始會話失敗: {e}")
            return False
    
    def store_memory(self, content: str, memory_token: str = None,
                    memory_type: MemoryType = MemoryType.SNAPSHOT,
                    importance: MemoryImportance = None,
                    topic: str = None,
                    intent_tags: List[str] = None,
                    metadata: Dict[str, Any] = None) -> MemoryOperationResult:
        """存儲新記憶 - 整合分析功能"""
        try:
            # 如果沒有提供記憶令牌，從Working Context獲取
            if not memory_token:
                memory_token = self.identity_manager.get_current_memory_token()
            
            # 驗證記憶體存取權限
            if not self.identity_manager.validate_memory_access(memory_token, "write"):
                return MemoryOperationResult(
                    success=False,
                    operation_type="store",
                    message="記憶體存取權限不足"
                )
            
            # 使用記憶分析器分析內容
            analysis_result = self.memory_analyzer.analyze_memory(
                MemoryEntry(
                    memory_id="temp_for_analysis",
                    memory_token=memory_token,
                    memory_type=memory_type,
                    content=content,
                    importance=importance or MemoryImportance.MEDIUM,
                    created_at=datetime.now(),
                    metadata=metadata or {}
                )
            )
            
            # 從分析結果提取資訊
            if "analysis" in analysis_result:
                analysis = analysis_result["analysis"]
                
                # 自動設定重要性（如果未指定）
                if importance is None:
                    importance = analysis.get("importance", {}).get("level", MemoryImportance.MEDIUM)
                
                # 自動提取主題（如果未指定）
                if topic is None:
                    topic = analysis.get("topics", {}).get("primary_topic", "一般")
                
                # 自動提取意圖標籤（如果未指定）
                if intent_tags is None:
                    intent_info = analysis.get("intents", {})
                    intent_tags = [intent_info.get("primary_intent", "陳述")]
                
                # 豐富元資料
                if metadata is None:
                    metadata = {}
                metadata.update({
                    "analysis_score": analysis.get("importance", {}).get("confidence", 0.0),
                    "sentiment": analysis.get("sentiment", {}).get("sentiment", "neutral"),
                    "complexity": analysis.get("complexity", {}).get("level", "simple"),
                    "keywords": analysis.get("keywords", [])
                })
            
            # 創建記憶條目
            memory_entry = MemoryEntry(
                memory_id=self._generate_memory_id(),
                memory_token=memory_token,
                memory_type=memory_type,
                content=content,
                importance=importance,
                topic=topic,
                intent_tags=intent_tags,
                created_at=datetime.now(),
                metadata=metadata
            )
            
            # 自動設定上下文資訊
            if self.current_context:
                memory_entry.session_id = self.current_context.current_session_id
                
                # 更新上下文
                self.current_context.recent_memories.append(memory_entry.memory_id)
                if memory_entry.topic:
                    self.current_context.active_topics.add(memory_entry.topic)
                self.current_context.conversation_depth += 1
            
            # 存儲到持久化存儲
            result = self.storage_manager.store_memory(memory_entry)
            
            if result.success:
                # 添加到快照
                if self.current_context and memory_entry.session_id:
                    message_data = {
                        "speaker": memory_token,
                        "content": content,
                        "timestamp": datetime.now(),
                        "memory_id": memory_entry.memory_id,
                        "analysis": analysis_result.get("analysis", {})
                    }
                    self.snapshot_manager.add_message_to_snapshot(
                        memory_entry.session_id, message_data
                    )
                
                self.stats["memories_stored"] += 1
                debug_log(3, f"[MemoryManagerV2] 記憶存儲成功: {memory_entry.memory_id}")
            
            return result
            
        except Exception as e:
            error_log(f"[MemoryManagerV2] 存儲記憶失敗: {e}")
            return MemoryOperationResult(
                success=False,
                operation_type="store",
                message=f"存儲異常: {str(e)}"
            )
    
    def retrieve_memories(self, query_text: str, memory_token: str = None,
                         memory_types: List[MemoryType] = None,
                         max_results: int = 10,
                         similarity_threshold: float = 0.6,
                         include_context: bool = True) -> List[MemorySearchResult]:
        """檢索相關記憶 - 使用語義檢索器"""
        try:
            # 如果沒有提供記憶令牌，從Working Context獲取
            if not memory_token:
                memory_token = self.identity_manager.get_current_memory_token()
            
            # 驗證記憶體存取權限
            if not self.identity_manager.validate_memory_access(memory_token, "read"):
                error_log(f"[MemoryManagerV2] 記憶體讀取權限不足: {memory_token}")
                return []
            
            # 構建查詢
            query = MemoryQuery(
                memory_token=memory_token,
                query_text=query_text,
                memory_types=memory_types or [MemoryType.SNAPSHOT, MemoryType.EPISODE, MemoryType.SEMANTIC],
                max_results=max_results,
                similarity_threshold=similarity_threshold,
                current_intent=self.current_context.current_intent if self.current_context else None,
                include_archived=False
            )
            
            # 使用語義檢索器檢索
            results = self.semantic_retriever.search_memories(query)
            
            # 更新統計
            self.stats["memories_retrieved"] += len(results)
            
            debug_log(3, f"[MemoryManagerV2] 檢索到 {len(results)} 條記憶")
            return results
            
        except Exception as e:
            error_log(f"[MemoryManagerV2] 檢索記憶失敗: {e}")
            return []
    
    def process_memory_query(self, query: MemoryQuery) -> List[MemorySearchResult]:
        """處理記憶查詢請求 - 為MEMModule提供的統一介面"""
        return self.retrieve_memories(
            query_text=query.query_text,
            memory_token=query.memory_token,
            memory_types=query.memory_types,
            max_results=query.max_results,
            similarity_threshold=query.similarity_threshold
        )
    
    def create_conversation_snapshot(self, memory_token: str, conversation_text: str, 
                                   topic: str = "general") -> Optional[ConversationSnapshot]:
        """創建對話快照"""
        try:
            # 驗證記憶體存取權限
            if not self.identity_manager.validate_memory_access(memory_token, "write"):
                error_log(f"[MemoryManagerV2] 記憶體存取權限驗證失敗: {memory_token}")
                return None
            
            # 使用快照管理器創建快照
            snapshot = self.snapshot_manager.create_snapshot(memory_token, conversation_text, topic)
            
            if snapshot:
                info_log(f"[MemoryManagerV2] 對話快照創建成功: {snapshot.memory_id}")
                return snapshot
            else:
                error_log("[MemoryManagerV2] 對話快照創建失敗")
                return None
                
        except Exception as e:
            error_log(f"[MemoryManagerV2] 創建對話快照時發生錯誤: {e}")
            return None
    
    def create_manual_snapshot(self, session_id: str, memory_token: str = None,
                              snapshot_name: str = None) -> MemoryOperationResult:
        """創建手動快照"""
        try:
            # 如果沒有提供記憶令牌，從Working Context獲取
            if not memory_token:
                memory_token = self.identity_manager.get_current_memory_token()
            
            # 驗證記憶體存取權限
            if not self.identity_manager.validate_memory_access(memory_token, "write"):
                return MemoryOperationResult(
                    success=False,
                    operation_type="manual_snapshot",
                    message="記憶體存取權限不足"
                )
            
            # 使用快照管理器創建快照
            result = self.snapshot_manager.create_manual_snapshot(session_id, snapshot_name)
            
            if result.success:
                info_log(f"[MemoryManagerV2] 手動快照創建成功: {session_id}")
            
            return result
            
        except Exception as e:
            error_log(f"[MemoryManagerV2] 創建手動快照失敗: {e}")
            return MemoryOperationResult(
                success=False,
                operation_type="manual_snapshot",
                message=f"創建失敗: {str(e)}"
            )
    
    def end_session(self, session_id: str, memory_token: str,
                   create_final_snapshot: bool = True) -> MemoryOperationResult:
        """結束會話"""
        try:
            # 使用快照管理器結束會話
            result = self.snapshot_manager.end_session_snapshot(session_id, create_final_snapshot)
            
            # 清理當前上下文
            if self.current_context and self.current_context.current_session_id == session_id:
                self.current_context = None
            
            info_log(f"[MemoryManagerV2] 會話結束: {session_id}")
            return result
            
        except Exception as e:
            error_log(f"[MemoryManagerV2] 結束會話失敗: {e}")
            return MemoryOperationResult(
                success=False,
                operation_type="end_session",
                message=f"結束失敗: {str(e)}"
            )
    
    def analyze_user_patterns(self, memory_token: str = None, 
                             days_back: int = 30) -> Dict[str, Any]:
        """分析使用者記憶模式"""
        try:
            # 如果沒有提供記憶令牌，從Working Context獲取
            if not memory_token:
                memory_token = self.identity_manager.get_current_memory_token()
            
            # 驗證記憶體存取權限
            if not self.identity_manager.validate_memory_access(memory_token, "read"):
                return {"error": "記憶體存取權限不足"}
            
            # 獲取使用者記憶
            cutoff_date = datetime.now() - timedelta(days=days_back)
            memories = self.storage_manager.get_memories_by_timerange(
                memory_token=memory_token,
                start_time=cutoff_date,
                end_time=datetime.now()
            )
            
            # 使用記憶分析器分析模式
            pattern_analysis = self.memory_analyzer.analyze_memory_patterns(memories, memory_token)
            
            debug_log(3, f"[MemoryManagerV2] 使用者模式分析完成: {memory_token}")
            return pattern_analysis
            
        except Exception as e:
            error_log(f"[MemoryManagerV2] 使用者模式分析失敗: {e}")
            return {"error": str(e)}
    
    def consolidate_memories(self, memory_token: str = None) -> MemoryOperationResult:
        """整合記憶（簡化版本）"""
        try:
            # 如果沒有提供記憶令牌，從Working Context獲取
            if not memory_token:
                memory_token = self.identity_manager.get_current_memory_token()
            
            # 驗證記憶體存取權限
            if not self.identity_manager.validate_memory_access(memory_token, "write"):
                return MemoryOperationResult(
                    success=False,
                    operation_type="consolidate",
                    message="記憶體存取權限不足"
                )
            
            info_log(f"[MemoryManagerV2] 開始記憶整合: {memory_token}")
            
            # 獲取需要整合的記憶（例如，過去24小時的快照記憶）
            cutoff_time = datetime.now() - timedelta(hours=24)
            snapshot_memories = self.storage_manager.get_memories_by_type_and_time(
                memory_token=memory_token,
                memory_type=MemoryType.SNAPSHOT,
                start_time=cutoff_time,
                end_time=datetime.now()
            )
            
            if not snapshot_memories:
                return MemoryOperationResult(
                    success=True,
                    operation_type="consolidate",
                    message="無需整合的記憶"
                )
            
            # 分析記憶重要性
            important_memories = []
            for memory in snapshot_memories:
                analysis = self.memory_analyzer.analyze_memory(memory)
                importance_info = analysis.get("analysis", {}).get("importance", {})
                
                if (importance_info.get("confidence", 0) > self.importance_threshold or
                    memory.importance in [MemoryImportance.HIGH, MemoryImportance.CRITICAL]):
                    important_memories.append(memory)
            
            # 將重要記憶轉換為語義記憶
            consolidated_count = 0
            for memory in important_memories:
                # 創建語義記憶版本
                semantic_memory = MemoryEntry(
                    memory_id=self._generate_memory_id(),
                    memory_token=memory.memory_token,
                    memory_type=MemoryType.SEMANTIC,
                    content=memory.content,
                    importance=memory.importance,
                    topic=memory.topic,
                    intent_tags=memory.intent_tags,
                    created_at=datetime.now(),
                    metadata={
                        **memory.metadata,
                        "consolidated_from": memory.memory_id,
                        "consolidation_reason": "高重要性或高信心分數"
                    }
                )
                
                store_result = self.storage_manager.store_memory(semantic_memory)
                if store_result.success:
                    consolidated_count += 1
            
            self.stats["memories_consolidated"] += consolidated_count
            self.stats["last_consolidation"] = datetime.now()
            
            info_log(f"[MemoryManagerV2] 記憶整合完成: {consolidated_count} 條記憶")
            
            return MemoryOperationResult(
                success=True,
                operation_type="consolidate",
                message=f"成功整合 {consolidated_count} 條記憶",
                data={"consolidated_count": consolidated_count}
            )
            
        except Exception as e:
            error_log(f"[MemoryManagerV2] 記憶整合失敗: {e}")
            return MemoryOperationResult(
                success=False,
                operation_type="consolidate",
                message=f"整合失敗: {str(e)}"
            )
    
    def summarize_memories_for_llm(self, search_results: List[MemorySearchResult],
                                 current_query: str = "") -> Dict[str, Any]:
        """
        為 LLM 總結記憶內容 - 整合從 prompt_builder 遷移的功能
        
        Args:
            search_results: 記憶搜索結果
            current_query: 當前查詢文本
            
        Returns:
            結構化的記憶總結
        """
        try:
            if not search_results:
                return {
                    "summary": "",
                    "structured_context": {},
                    "has_memories": False
                }
            
            # 如果有外部總結器，使用高級總結功能
            if self.memory_summarizer:
                debug_log(2, "[MemoryManagerV2] 使用外部總結器生成記憶上下文")
                structured_context = self.memory_summarizer.create_llm_memory_context(
                    search_results, current_query
                )
                summary = self.memory_summarizer.summarize_search_results(
                    search_results, current_query
                )
            else:
                # 使用基本總結邏輯
                debug_log(2, "[MemoryManagerV2] 使用基本總結邏輯")
                structured_context = self._create_basic_memory_context(search_results)
                summary = self._create_basic_summary(search_results)
            
            return {
                "summary": summary,
                "structured_context": structured_context,
                "has_memories": True,
                "memory_count": len(search_results)
            }
            
        except Exception as e:
            error_log(f"[MemoryManagerV2] 記憶總結失敗: {e}")
            return {
                "summary": "",
                "structured_context": {},
                "has_memories": False,
                "error": str(e)
            }
    
    def chunk_and_summarize_memories(self, memories: List[str], 
                                   chunk_size: int = 3) -> str:
        """
        記憶切塊總結 - 從 prompt_builder.py 遷移的功能
        
        Args:
            memories: 記憶文本列表
            chunk_size: 切塊大小
            
        Returns:
            總結後的文本
        """
        try:
            if self.memory_summarizer:
                return self.memory_summarizer.chunk_and_summarize_memories(memories, chunk_size)
            else:
                # 基本的切塊邏輯，不使用外部模型
                debug_log(2, "[MemoryManagerV2] 使用基本切塊總結")
                chunks = [memories[i:i+chunk_size] for i in range(0, len(memories), chunk_size)]
                summaries = []
                
                for group in chunks:
                    # 簡單的文本截取作為「總結」
                    text_block = " ".join(group)
                    if len(text_block) > 200:
                        summary = text_block[:200] + "..."
                    else:
                        summary = text_block
                    summaries.append(summary)
                
                return "\n".join(summaries)
                
        except Exception as e:
            error_log(f"[MemoryManagerV2] 切塊總結失敗: {e}")
            return ""
    
    def _create_basic_memory_context(self, search_results: List[MemorySearchResult]) -> Dict[str, Any]:
        """創建基本記憶上下文（不依賴外部總結器）"""
        try:
            key_facts = []
            conversations = []
            reminders = []
            
            for result in search_results[:10]:  # 限制處理數量
                memory = result.memory_entry
                
                if memory.importance in [MemoryImportance.HIGH, MemoryImportance.CRITICAL]:
                    key_facts.append({
                        "content": memory.content[:150],
                        "importance": memory.importance.value
                    })
                
                if memory.memory_type == MemoryType.SNAPSHOT:
                    conversations.append({
                        "content": memory.content[:100],
                        "timestamp": memory.created_at.isoformat() if memory.created_at else None
                    })
                
                if memory.importance == MemoryImportance.CRITICAL:
                    reminders.append(memory.content[:80])
            
            return {
                "key_facts": key_facts[:5],
                "recent_conversations": conversations[-3:],
                "important_reminders": reminders[:3],
                "total_memories": len(search_results)
            }
            
        except Exception as e:
            error_log(f"[MemoryManagerV2] 基本上下文創建失敗: {e}")
            return {}
    
    def _create_basic_summary(self, search_results: List[MemorySearchResult]) -> str:
        """創建基本記憶總結（不依賴外部總結器）"""
        try:
            if not search_results:
                return ""
            
            # 提取前幾條記憶的摘要
            summaries = []
            for result in search_results[:5]:
                memory = result.memory_entry
                summary_part = memory.content[:100]
                if memory.topic:
                    summary_part = f"[{memory.topic}] {summary_part}"
                summaries.append(summary_part)
            
            return " | ".join(summaries)
            
        except Exception as e:
            error_log(f"[MemoryManagerV2] 基本總結創建失敗: {e}")
            return ""
    
    def generate_llm_instruction(self, relevant_memories: List[MemorySearchResult],
                                context: str = "") -> LLMMemoryInstruction:
        """生成LLM記憶指示"""
        try:
            if not relevant_memories:
                return LLMMemoryInstruction(
                    context_summary="無相關記憶",
                    key_facts=[],
                    behavioral_notes=[],
                    conversation_history=[],
                    user_preferences={},
                    important_reminders=[],
                    confidence_score=0.0
                )
            
            # 提取關鍵事實
            key_facts = []
            behavioral_notes = []
            conversation_history = []
            user_preferences = {}
            important_reminders = []
            
            total_confidence = 0.0
            
            for result in relevant_memories:
                memory = result.memory_entry
                
                # 關鍵事實
                if memory.importance in [MemoryImportance.HIGH, MemoryImportance.CRITICAL]:
                    key_facts.append({
                        "content": memory.content[:200],
                        "topic": memory.topic,
                        "importance": memory.importance.value,
                        "created_at": memory.created_at.isoformat()
                    })
                
                # 行為記錄
                if memory.intent_tags:
                    behavioral_notes.extend(memory.intent_tags)
                
                # 對話歷史
                if memory.memory_type == MemoryType.SNAPSHOT:
                    conversation_history.append({
                        "content": memory.content[:100],
                        "timestamp": memory.created_at.isoformat(),
                        "session_id": memory.session_id
                    })
                
                # 重要提醒
                if memory.importance == MemoryImportance.CRITICAL:
                    important_reminders.append(memory.content[:150])
                
                total_confidence += result.similarity_score
            
            # 去重行為記錄
            behavioral_notes = list(set(behavioral_notes))
            
            # 計算平均信心分數
            avg_confidence = total_confidence / len(relevant_memories) if relevant_memories else 0.0
            
            # 創建上下文摘要
            topics = [m.memory_entry.topic for m in relevant_memories if m.memory_entry.topic]
            top_topics = list(set(topics))[:3]
            
            context_summary = f"基於 {len(relevant_memories)} 條相關記憶，主要涉及: {', '.join(top_topics)}"
            if context:
                context_summary += f"。當前上下文: {context}"
            
            return LLMMemoryInstruction(
                context_summary=context_summary,
                key_facts=key_facts[:10],  # 最多10個關鍵事實
                behavioral_notes=behavioral_notes[:5],  # 最多5個行為記錄
                conversation_history=conversation_history[-5:],  # 最近5個對話
                user_preferences=user_preferences,
                important_reminders=important_reminders[:3],  # 最多3個重要提醒
                confidence_score=avg_confidence
            )
            
        except Exception as e:
            error_log(f"[MemoryManagerV2] 生成LLM指示失敗: {e}")
            return LLMMemoryInstruction(
                context_summary="記憶處理錯誤",
                key_facts=[],
                behavioral_notes=[],
                conversation_history=[],
                user_preferences={},
                important_reminders=[],
                confidence_score=0.0
            )
    
    def _generate_memory_id(self) -> str:
        """生成記憶ID"""
        import uuid
        return f"mem_{int(time.time())}_{str(uuid.uuid4())[:8]}"
    
    def get_memory_stats(self) -> Dict[str, Any]:
        """獲取記憶統計資訊"""
        try:
            # 整合各子模組的統計
            base_stats = self.stats.copy()
            
            # 添加子模組統計
            base_stats.update({
                "identity_stats": self.identity_manager.get_stats(),
                "snapshot_stats": self.snapshot_manager.get_stats(), 
                "retrieval_stats": self.semantic_retriever.get_stats(),
                "analysis_stats": self.memory_analyzer.get_stats(),
                "storage_stats": self.storage_manager.get_stats() if hasattr(self.storage_manager, 'get_stats') else {}
            })
            
            # 添加當前狀態
            base_stats.update({
                "current_session": self.current_context.current_session_id if self.current_context else None,
                "active_sessions": len(self.snapshot_manager.list_active_sessions()),
                "is_initialized": self.is_initialized
            })
            
            return base_stats
            
        except Exception as e:
            error_log(f"[MemoryManagerV2] 獲取統計失敗: {e}")
            return self.stats
    
    def cleanup_resources(self):
        """清理資源"""
        try:
            info_log("[MemoryManagerV2] 清理資源...")
            
            # 清理各子模組
            self.identity_manager.cleanup_expired_identities()
            self.snapshot_manager.cleanup_old_snapshots()
            self.semantic_retriever.clear_cache()
            self.memory_analyzer.clear_cache()
            
            debug_log(3, "[MemoryManagerV2] 資源清理完成")
            
        except Exception as e:
            error_log(f"[MemoryManagerV2] 清理資源失敗: {e}")
