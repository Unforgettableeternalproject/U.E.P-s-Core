# modules/mem_module/core/snapshot_manager.py
"""
快照管理器 - 處理對話快照和記憶快照功能

功能：
- 對話快照創建與管理
- 會話記憶整合
- 快照搜尋與檢索
- 快照歸檔與清理
"""

import time
import json
from typing import Dict, Any, Optional, List, Set
from datetime import datetime, timedelta
from dataclasses import dataclass

from utils.debug_helper import debug_log, info_log, error_log
from ..schemas import (
    MemoryEntry, MemoryType, MemoryImportance, 
    ConversationSnapshot, MemoryOperationResult
)
from .snapshot_key_manager import SnapshotKeyManager


@dataclass
class SnapshotContext:
    """快照上下文"""
    session_id: str
    start_time: datetime
    end_time: Optional[datetime]
    participant_count: int
    message_count: int
    primary_topics: Set[str]
    interaction_depth: int
    participant_info: Dict[str, Dict[str, Any]] = None
    
    def __post_init__(self):
        if self.participant_info is None:
            self.participant_info = {}


class SnapshotManager:
    """快照管理器"""
    
    def __init__(self, config: Dict[str, Any], memory_summarizer: Optional[Any] = None):
        self.config = config
        self.memory_summarizer = memory_summarizer
        
        # 初始化快照鍵值管理器
        self.key_manager = SnapshotKeyManager(config.get("key_manager", {}), memory_summarizer)
        
        # 快照配置
        self.max_snapshot_size = config.get("max_snapshot_size", 1000)
        self.auto_snapshot_interval = config.get("auto_snapshot_interval", 300)  # 5分鐘
        self.snapshot_retention_days = config.get("snapshot_retention_days", 30)
        self.compression_enabled = config.get("compression_enabled", True)
        
        # GSID過期配置
        self.max_general_sessions = config.get("max_general_sessions", 10)  # 保留最近10個GS
        self.current_gsid = 0  # 當前General Session ID
        
        # 快照存儲
        self._active_snapshots: Dict[str, ConversationSnapshot] = {}
        self._snapshot_contexts: Dict[str, SnapshotContext] = {}
        
        # 自動快照追蹤
        self._last_auto_snapshot: Dict[str, float] = {}
        self._session_messages: Dict[str, List[Dict[str, Any]]] = {}
        
        # 統計資訊
        self.stats = {
            "snapshots_created": 0,
            "snapshots_retrieved": 0,
            "snapshots_archived": 0,
            "auto_snapshots": 0,
            "manual_snapshots": 0
        }
        
        self.is_initialized = False
        
    def advance_general_session(self) -> int:
        """前進到下一個General Session"""
        self.current_gsid += 1
        debug_log(2, f"[SnapshotManager] 前進到General Session: {self.current_gsid}")
        
        # 檢查並清理過期的快照
        self._cleanup_expired_snapshots()
        
        return self.current_gsid
    
    def _cleanup_expired_snapshots(self):
        """清理過期的快照（根據GSID過期規則）"""
        try:
            expired_snapshots = []
            
            # 檢查所有快照的GSID是否過期
            for snapshot_id, snapshot in self._active_snapshots.items():
                if hasattr(snapshot, 'gsid'):
                    # 如果快照的GSID距離當前GSID太遠，標記為過期
                    gsid_distance = self.current_gsid - snapshot.gsid
                    if gsid_distance >= self.max_general_sessions:
                        expired_snapshots.append(snapshot_id)
                        debug_log(2, f"[SnapshotManager] 快照過期: {snapshot_id} (GSID: {snapshot.gsid}, 距離: {gsid_distance})")
            
            # 刪除過期的快照
            for snapshot_id in expired_snapshots:
                if snapshot_id in self._active_snapshots:
                    # 從鍵值管理器中註銷
                    self.key_manager.unregister_snapshot(snapshot_id)
                    del self._active_snapshots[snapshot_id]
                if snapshot_id in self._snapshot_contexts:
                    del self._snapshot_contexts[snapshot_id]
                if snapshot_id in self._session_messages:
                    del self._session_messages[snapshot_id]
                if snapshot_id in self._last_auto_snapshot:
                    del self._last_auto_snapshot[snapshot_id]
            
            # 清理鍵值管理器中的過期映射
            valid_snapshot_ids = set(self._active_snapshots.keys())
            self.key_manager.cleanup_expired_keys(valid_snapshot_ids)
            
            if expired_snapshots:
                debug_log(1, f"[SnapshotManager] 清理了 {len(expired_snapshots)} 個過期快照")
                
        except Exception as e:
            error_log(f"[SnapshotManager] 清理過期快照失敗: {e}")
    
    def get_current_gsid(self) -> int:
        """獲取當前General Session ID"""
        return self.current_gsid
    
    def initialize(self) -> bool:
        """初始化快照管理器"""
        try:
            info_log("[SnapshotManager] 初始化快照管理器...")
            
            # 初始化鍵值管理器
            if not self.key_manager.initialize():
                error_log("[SnapshotManager] 鍵值管理器初始化失敗")
                return False
            
            # 載入活躍快照
            self._load_active_snapshots()
            
            # 設定自動清理
            self._setup_cleanup_timer()
            
            self.is_initialized = True
            info_log("[SnapshotManager] 快照管理器初始化完成")
            return True
            
        except Exception as e:
            error_log(f"[SnapshotManager] 初始化失敗: {e}")
            return False
    
    def _load_active_snapshots(self):
        """載入活躍快照"""
        try:
            debug_log(3, "[SnapshotManager] 載入活躍快照...")
            
            # 這裡可以從存儲載入活躍快照
            # 暫時跳過載入
            
        except Exception as e:
            info_log("WARNING", f"[SnapshotManager] 載入活躍快照失敗: {e}")
    
    def _setup_cleanup_timer(self):
        """設定清理計時器"""
        debug_log(3, "[SnapshotManager] 設定自動清理計時器")
        # 這裡可以設定定期清理任務
    
    def query_and_decide_snapshot_creation(self, memory_token: str, content: str, 
                                         context: Dict[str, Any] = None) -> Dict[str, Any]:
        """
        查詢相關快照並決定是否創建新快照
        
        這是新的主要入口點，實現你要求的流程：
        1. 先查詢有沒有對應的快照與對話歷史
        2. 如果沒有就新增快照來記錄接下來的對話
        
        Returns:
            Dict包含：
            - need_new_snapshot: 是否需要創建新快照
            - related_snapshots: 相關快照列表
            - suggested_session_id: 建議的會話ID
            - flow_analysis: 流程分析結果
        """
        try:
            debug_log(2, f"[SnapshotManager] 查詢快照並決定創建策略: {memory_token}")
            
            # 使用鍵值管理器分析對話流程
            flow_analysis = self.key_manager.analyze_conversation_flow(memory_token, content)
            
            # 獲取相關快照的詳細信息
            related_snapshot_details = []
            for snapshot_id in flow_analysis["related_snapshots"]:
                if snapshot_id in self._active_snapshots:
                    snapshot = self._active_snapshots[snapshot_id]
                    related_snapshot_details.append({
                        "snapshot_id": snapshot_id,
                        "memory_token": snapshot.memory_token,
                        "session_id": getattr(snapshot, 'session_id', None),
                        "summary": getattr(snapshot, 'summary', ''),
                        "message_count": getattr(snapshot, 'message_count', 0),
                        "last_updated": getattr(snapshot, 'updated_at', snapshot.created_at)
                    })
            
            # 決定會話ID策略
            suggested_session_id = None
            if not flow_analysis["should_create_new"] and related_snapshot_details:
                # 使用最近的相關快照的會話ID
                latest_snapshot = max(related_snapshot_details, 
                                    key=lambda x: x["last_updated"])
                suggested_session_id = latest_snapshot["session_id"]
            else:
                # 生成新的會話ID
                suggested_session_id = f"session_{memory_token}_{int(time.time())}"
            
            result = {
                "need_new_snapshot": flow_analysis["should_create_new"],
                "related_snapshots": related_snapshot_details,
                "suggested_session_id": suggested_session_id,
                "flow_analysis": flow_analysis,
                "query_key": flow_analysis["query_key"]
            }
            
            debug_log(2, f"[SnapshotManager] 決策結果: {'創建新快照' if result['need_new_snapshot'] else '使用現有快照'}")
            debug_log(3, f"[SnapshotManager] 建議會話ID: {suggested_session_id}")
            
            return result
            
        except Exception as e:
            error_log(f"[SnapshotManager] 查詢並決定快照創建失敗: {e}")
            return {
                "need_new_snapshot": True,
                "related_snapshots": [],
                "suggested_session_id": f"error_session_{int(time.time())}",
                "flow_analysis": {"flow_decision": "錯誤回退，創建新快照"},
                "query_key": "error"
            }
    
    def start_session_snapshot(self, session_id: str, memory_token: str,
                              initial_context: Dict[str, Any] = None) -> bool:
        """開始會話快照"""
        try:
            if session_id in self._active_snapshots:
                debug_log(2, f"[SnapshotManager] 會話快照已存在: {session_id}")
                return True
            
            # 創建快照上下文
            context = SnapshotContext(
                session_id=session_id,
                start_time=datetime.now(),
                end_time=None,
                participant_count=1,
                message_count=0,
                primary_topics=set(),
                interaction_depth=0
            )
            
            # 創建會話快照
            snapshot = ConversationSnapshot(
                memory_id=f"snapshot_{session_id}_{int(time.time())}",
                memory_token=memory_token,
                content="",  # 初始為空，會在添加消息時填充
                stage_number=1,  # 初始階段
                snapshot_id=f"snapshot_{session_id}_{int(time.time())}",
                session_id=session_id,
                start_time=datetime.now(),
                end_time=None,
                messages=[],
                summary="",
                key_topics=[],
                participant_info={memory_token: {"role": "user", "name": "User"}},
                context_data=initial_context or {},
                metadata={"auto_generated": False},
                gsid=self.current_gsid  # 設置當前GSID
            )
            
            self._active_snapshots[session_id] = snapshot
            self._snapshot_contexts[session_id] = context
            self._session_messages[session_id] = []
            self._last_auto_snapshot[session_id] = time.time()
            
            # 註冊到鍵值管理器  
            content_for_key = ""
            if initial_context:
                content_for_key = initial_context.get("content", "") or str(initial_context)
            self.key_manager.register_snapshot(snapshot.memory_id, content_for_key)
            
            debug_log(3, f"[SnapshotManager] 開始會話快照: {session_id}")
            return True
            
        except Exception as e:
            error_log(f"[SnapshotManager] 開始會話快照失敗: {e}")
            return False
    
    def add_message_to_snapshot(self, session_id: str, message_data: Dict[str, Any]) -> bool:
        """添加訊息到快照"""
        try:
            if session_id not in self._active_snapshots:
                debug_log(2, f"[SnapshotManager] 會話快照不存在: {session_id}")
                return False
            
            snapshot = self._active_snapshots[session_id]
            
            # 確保上下文存在
            if session_id not in self._snapshot_contexts:
                debug_log(2, f"[SnapshotManager] 創建會話上下文: {session_id}")
                self._snapshot_contexts[session_id] = SnapshotContext(
                    session_id=session_id,
                    start_time=snapshot.start_time or datetime.now(),
                    end_time=None,
                    participant_count=snapshot.participant_count or 1,
                    message_count=snapshot.message_count or 0,
                    primary_topics=set(snapshot.key_topics or []),
                    interaction_depth=len(snapshot.messages) if snapshot.messages else 0,
                    participant_info=dict(snapshot.participant_info) if snapshot.participant_info else {}
                )
                self._session_messages[session_id] = snapshot.messages.copy() if snapshot.messages else []
            
            context = self._snapshot_contexts[session_id]
            
            # 更新上下文
            context.message_count += 1
            context.interaction_depth = len(snapshot.messages) if snapshot.messages else 0
            
            # 實際添加消息到快照
            message_entry = {
                "speaker": message_data.get("speaker", "unknown"),
                "content": message_data.get("content", ""),
                "timestamp": message_data.get("timestamp", datetime.now().isoformat()),
                "intent": message_data.get("intent", []),
                "message_id": f"msg_{int(time.time() * 1000000)}"  # 唯一消息ID
            }
            
            # 確保snapshot.messages是列表
            if not hasattr(snapshot, 'messages') or snapshot.messages is None:
                snapshot.messages = []
            
            snapshot.messages.append(message_entry)
            
            # 更新快照內容摘要
            updated_snapshot = self._update_snapshot_content(snapshot, message_entry)
            # 更新活躍快照
            self._active_snapshots[session_id] = updated_snapshot
            snapshot = updated_snapshot
            
            # 更新參與者資訊 - 只更新context，不修改snapshot
            speaker = message_data.get("speaker", "unknown")
            if speaker not in context.participant_info:
                context.participant_info[speaker] = {"message_count": 0}
                context.participant_count += 1
            else:
                context.participant_info[speaker]["message_count"] += 1
            
            # 提取主題
            content = message_data.get("content", "")
            topics = self._extract_topics_from_message(content)
            context.primary_topics.update(topics)
            
            # 檢查是否需要自動快照
            self._check_auto_snapshot(session_id)
            
            debug_log(4, f"[SnapshotManager] 添加訊息到快照: {session_id}")
            return True
            
        except Exception as e:
            error_log(f"[SnapshotManager] 添加訊息失敗: {e}")
            return False
    
    def _update_snapshot_content(self, snapshot: ConversationSnapshot, message_entry: Dict[str, Any]) -> ConversationSnapshot:
        """更新快照內容摘要 - 簡單版本，不依賴LLM"""
        try:
            # 使用model_dump和model_validate來安全地更新Pydantic模型
            snapshot_data = snapshot.model_dump()
            
            # 將消息添加到快照內容
            speaker = message_entry.get("speaker", "unknown")
            content = message_entry.get("content", "")
            
            # 格式化消息
            formatted_message = f"{speaker}: {content}"
            
            # 如果是第一條消息，初始化內容
            if not snapshot_data.get("content") or snapshot_data["content"] == "":
                snapshot_data["content"] = formatted_message
            else:
                # 添加到現有內容
                snapshot_data["content"] += f"\n{formatted_message}"
            
            # 限制內容長度 (簡單版本：保留最近的內容)
            max_length = snapshot_data.get("max_context_length", 2000)
            if len(snapshot_data["content"]) > max_length:
                # 保留最後max_length個字符
                snapshot_data["content"] = snapshot_data["content"][-max_length:]
                snapshot_data["compression_level"] = 1  # 標記為已壓縮
            
            # 簡單的主題提取和更新
            if "key_topics" in snapshot_data and snapshot_data["key_topics"] is not None:
                new_topics = self._extract_topics_from_message(content)
                existing_topics = set(snapshot_data["key_topics"])
                existing_topics.update(new_topics)
                snapshot_data["key_topics"] = list(existing_topics)
            
            # 生成簡單的總結 (基於關鍵字)
            if len(snapshot_data["messages"]) > 1:
                topics_str = ", ".join(snapshot_data["key_topics"][:3]) if snapshot_data["key_topics"] else "一般對話"
                snapshot_data["summary"] = f"涉及{topics_str}的對話，共有{len(snapshot_data['messages'])}條消息"
            
            # 使用model_validate創建更新後的實例
            return ConversationSnapshot.model_validate(snapshot_data)
            
        except Exception as e:
            error_log(f"[SnapshotManager] 更新快照內容失敗: {e}")
            return snapshot  # 返回原始snapshot
    
    def _extract_topics_from_message(self, content: str) -> Set[str]:
        """從訊息內容提取主題 - 使用summarizer生成主題標籤"""
        try:
            # 優先使用summarizer生成主題標籤
            if self.memory_summarizer and self.memory_summarizer.is_initialized:
                topics = self._extract_topics_with_summarizer(content)
                if topics:
                    return topics
            
            # 回退到關鍵字匹配
            return self._extract_topics_with_keywords(content)
            
        except Exception as e:
            error_log(f"[SnapshotManager] 主題提取失敗，使用關鍵字回退: {e}")
            return self._extract_topics_with_keywords(content)
    
    def _extract_topics_with_summarizer(self, content: str) -> Set[str]:
        """使用summarizer生成主題標籤"""
        try:
            if not content or len(content.strip()) < 10:
                return set()
            
            # 使用summarizer生成簡短摘要，然後從摘要中提取關鍵主題
            # 這裡我們使用總結功能來生成主題描述
            summary = self.memory_summarizer.chunk_and_summarize_memories([content])
            
            if not summary:
                return set()
            
            # 從摘要中提取主題詞
            topics = set()
            summary_lower = summary.lower()
            
            # 常見主題指示詞
            topic_indicators = [
                "about", "regarding", "concerning", "related to", "focused on",
                "discussing", "talking about", "working on", "dealing with"
            ]
            
            # 簡單的主題提取邏輯：查找主題指示詞後的詞彙
            for indicator in topic_indicators:
                if indicator in summary_lower:
                    # 提取指示詞後的內容作為主題
                    idx = summary_lower.find(indicator)
                    remaining = summary[idx + len(indicator):].strip()
                    # 提取前幾個詞作為主題
                    words = remaining.split()[:3]  # 取前3個詞
                    if words:
                        topic = " ".join(words).strip(".,!?")
                        if len(topic) > 2:  # 避免太短的主題
                            topics.add(topic.lower())
            
            # 如果沒有找到主題指示詞，嘗試從摘要中提取名詞詞組
            if not topics:
                # 簡單的詞組提取：連續的大寫詞或特定模式
                words = summary.split()
                for i in range(len(words) - 1):
                    if words[i][0].isupper() and len(words[i]) > 3:
                        # 可能的專有名詞
                        topics.add(words[i].lower())
                    elif words[i].lower() in ["the", "a", "an"] and i + 1 < len(words):
                        # "the X" 模式
                        if len(words[i+1]) > 3:
                            topics.add(words[i+1].lower())
            
            # 清理主題：移除常見的停用詞
            stop_words = {"the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for", "of", "with", "by"}
            cleaned_topics = {topic for topic in topics if topic not in stop_words and len(topic) > 2}
            
            debug_log(4, f"[SnapshotManager] Summarizer提取主題: {cleaned_topics}")
            return cleaned_topics
            
        except Exception as e:
            error_log(f"[SnapshotManager] Summarizer主題提取失敗: {e}")
            return set()
    
    def _extract_topics_with_keywords(self, content: str) -> Set[str]:
        """使用關鍵字匹配提取主題 - 回退方法"""
        # 英文關鍵字提取 - 對應中文版本但適配英文內容
        topics = set()
        
        # 英文關鍵字提取 - 改為英文關鍵字以匹配系統語言
        keywords = [
            "problem", "solution", "suggestion", "help", "learning", "work", "technology", "programming",
            "data", "analysis", "design", "development", "testing", "deployment", "maintenance", "optimization",
            "question", "answer", "code", "debug", "error", "fix", "feature", "function"
        ]
        
        content_lower = content.lower()
        for keyword in keywords:
            if keyword in content_lower:
                topics.add(keyword)
        
        return topics
    
    def _check_auto_snapshot(self, session_id: str):
        """檢查是否需要自動快照"""
        try:
            current_time = time.time()
            last_snapshot = self._last_auto_snapshot.get(session_id, 0)
            
            if current_time - last_snapshot >= self.auto_snapshot_interval:
                self._create_auto_snapshot(session_id)
                
        except Exception as e:
            debug_log(1, f"[SnapshotManager] 自動快照檢查失敗: {e}")
    
    def _create_auto_snapshot(self, session_id: str):
        """創建自動快照"""
        try:
            if session_id not in self._active_snapshots:
                return
            
            snapshot = self._active_snapshots[session_id]
            recent_messages = self._session_messages[session_id][-10:]  # 最近10條訊息
            
            # 創建自動快照記憶
            auto_snapshot_content = self._summarize_recent_messages(recent_messages)
            
            if auto_snapshot_content:
                self.stats["auto_snapshots"] += 1
                self._last_auto_snapshot[session_id] = time.time()
                
                debug_log(3, f"[SnapshotManager] 創建自動快照: {session_id}")
                
        except Exception as e:
            error_log(f"[SnapshotManager] 創建自動快照失敗: {e}")
    
    def _summarize_recent_messages(self, messages: List[Dict[str, Any]]) -> str:
        """總結最近訊息"""
        if not messages:
            return ""
        
        # 簡單總結邏輯
        content_parts = []
        for msg in messages:
            speaker = msg.get("speaker", "user")
            content = msg.get("content", "")
            if content:
                content_parts.append(f"{speaker}: {content[:100]}")
        
        return " | ".join(content_parts)
    
    def create_snapshot(self, memory_token: str, content: str, topic: str = "general") -> Optional[ConversationSnapshot]:
        """創建簡單快照 - 用於測試和直接創建"""
        try:
            import uuid
            
            memory_id = f"mem_{int(time.time())}_{str(uuid.uuid4())[:8]}"
            
            # 創建快照對象
            # 確保 keywords 列表中沒有 None 值
            keywords_list = [kw for kw in [topic, "conversation", "snapshot"] if kw is not None]
            
            snapshot = ConversationSnapshot(
                memory_id=memory_id,
                memory_token=memory_token,
                memory_type=MemoryType.SNAPSHOT,
                content=content,
                summary=content[:200] + "..." if len(content) > 200 else content,
                keywords=keywords_list,
                gsid=self.current_gsid,
                stage_number=1,
                message_count=1,
                participant_count=1,
                created_at=datetime.now(),
                importance_score=0.5,
                metadata={
                    "auto_generated": True,
                    "content_length": len(content),
                    "creation_method": "direct"
                }
            )
            
            # 添加到緩存 - 使用memory_id作為key
            self._active_snapshots[memory_id] = snapshot
            
            # 註冊到鍵值管理器
            self.key_manager.register_snapshot(memory_id, content)
            
            # 更新統計
            self.stats["snapshots_created"] += 1
            
            debug_log(3, f"[SnapshotManager] 創建快照: {memory_id}")
            return snapshot
            
        except Exception as e:
            error_log(f"[SnapshotManager] 創建快照失敗: {e}")
            return None
    
    def get_active_snapshots(self, memory_token: str) -> List[ConversationSnapshot]:
        """獲取指定記憶令牌的活躍快照"""
        try:
            active_snapshots = []
            
            for memory_id, snapshot in self._active_snapshots.items():
                if snapshot.memory_token == memory_token and snapshot.is_active:
                    active_snapshots.append(snapshot)
            
            debug_log(3, f"[SnapshotManager] 找到 {len(active_snapshots)} 個活躍快照 (令牌: {memory_token})")
            return active_snapshots
            
        except Exception as e:
            error_log(f"[SnapshotManager] 獲取活躍快照失敗: {e}")
            return []
    
    def create_manual_snapshot(self, session_id: str, 
                             snapshot_name: str = None,
                             include_full_context: bool = True) -> MemoryOperationResult:
        """創建手動快照"""
        try:
            if session_id not in self._active_snapshots:
                return MemoryOperationResult(
                    success=False,
                    operation_type="manual_snapshot",
                    message=f"會話不存在: {session_id}"
                )
            
            snapshot = self._active_snapshots[session_id]
            context = self._snapshot_contexts[session_id]
            
            # 創建完整快照
            full_snapshot = ConversationSnapshot(
                memory_id=snapshot_name or f"manual_{session_id}_{int(time.time())}",
                memory_token=snapshot.memory_token,
                content=self._create_session_summary(snapshot, context),
                stage_number=snapshot.stage_number,
                gsid=snapshot.gsid,
                message_count=context.message_count,
                participant_count=context.participant_count,
                messages=snapshot.messages.copy() if include_full_context else snapshot.messages[-20:],
                participant_info=snapshot.participant_info.copy(),
                start_time=snapshot.start_time,
                key_topics=list(context.primary_topics),
                created_at=datetime.now(),
                importance_score=snapshot.importance_score,
                metadata={
                    "auto_generated": False,
                    "manual_creation": True,
                    "session_id": session_id,
                    "duration_minutes": (datetime.now() - snapshot.start_time).total_seconds() / 60
                }
            )
            
            self.stats["manual_snapshots"] += 1
            self.stats["snapshots_created"] += 1
            
            debug_log(3, f"[SnapshotManager] 創建手動快照: {full_snapshot.snapshot_id}")
            
            return MemoryOperationResult(
                success=True,
                operation_type="manual_snapshot",
                message="手動快照創建成功",
                data={"snapshot_id": full_snapshot.snapshot_id}
            )
            
        except Exception as e:
            error_log(f"[SnapshotManager] 創建手動快照失敗: {e}")
            return MemoryOperationResult(
                success=False,
                operation_type="manual_snapshot",
                message=f"創建失敗: {str(e)}"
            )
    
    def _create_session_summary(self, snapshot: ConversationSnapshot, 
                               context: SnapshotContext) -> str:
        """創建會話總結"""
        try:
            duration = (datetime.now() - snapshot.start_time).total_seconds() / 60
            
            summary_parts = [
                f"會話時長: {duration:.1f}分鐘",
                f"訊息數量: {context.message_count}",
                f"參與者: {context.participant_count}",
                f"主要主題: {', '.join(list(context.primary_topics)[:5])}"
            ]
            
            return " | ".join(summary_parts)
            
        except Exception as e:
            error_log(f"[SnapshotManager] 創建會話總結失敗: {e}")
            return "總結生成失敗"
    
    def end_session_snapshot(self, session_id: str, 
                           create_final_snapshot: bool = True) -> MemoryOperationResult:
        """結束會話快照"""
        try:
            if session_id not in self._active_snapshots:
                return MemoryOperationResult(
                    success=False,
                    operation_type="end_snapshot",
                    message=f"會話快照不存在: {session_id}"
                )
            
            result = None
            if create_final_snapshot:
                result = self.create_manual_snapshot(session_id, f"final_{session_id}")
            
            # 清理活躍快照
            del self._active_snapshots[session_id]
            del self._snapshot_contexts[session_id]
            if session_id in self._session_messages:
                del self._session_messages[session_id]
            if session_id in self._last_auto_snapshot:
                del self._last_auto_snapshot[session_id]
            
            debug_log(3, f"[SnapshotManager] 結束會話快照: {session_id}")
            
            return MemoryOperationResult(
                success=True,
                operation_type="end_snapshot",
                message="會話快照已結束",
                data={"final_snapshot": result.data if result and result.success else None}
            )
            
        except Exception as e:
            error_log(f"[SnapshotManager] 結束會話快照失敗: {e}")
            return MemoryOperationResult(
                success=False,
                operation_type="end_snapshot",
                message=f"結束失敗: {str(e)}"
            )
    
    def get_session_snapshot(self, session_id: str) -> Optional[ConversationSnapshot]:
        """獲取會話快照"""
        return self._active_snapshots.get(session_id)
    
    def list_active_sessions(self) -> List[str]:
        """列出活躍會話"""
        return list(self._active_snapshots.keys())
    
    def get_session_stats(self, session_id: str) -> Optional[Dict[str, Any]]:
        """獲取會話統計"""
        if session_id not in self._snapshot_contexts:
            return None
        
        context = self._snapshot_contexts[session_id]
        snapshot = self._active_snapshots[session_id]
        
        duration = (datetime.now() - snapshot.start_time).total_seconds() / 60
        
        return {
            "session_id": session_id,
            "duration_minutes": duration,
            "message_count": context.message_count,
            "participant_count": context.participant_count,
            "primary_topics": list(context.primary_topics),
            "interaction_depth": context.interaction_depth,
            "last_auto_snapshot": self._last_auto_snapshot.get(session_id, 0)
        }
    
    def cleanup_old_snapshots(self):
        """清理舊快照"""
        try:
            cutoff_date = datetime.now() - timedelta(days=self.snapshot_retention_days)
            
            # 清理邏輯
            debug_log(3, f"[SnapshotManager] 清理 {cutoff_date} 之前的快照")
            
            # 這裡可以實現實際的清理邏輯
            
        except Exception as e:
            error_log(f"[SnapshotManager] 清理舊快照失敗: {e}")
    
    def get_stats(self) -> Dict[str, Any]:
        """獲取統計資訊"""
        base_stats = {
            **self.stats,
            "active_sessions": len(self._active_snapshots),
            "total_messages": sum(len(msgs) for msgs in self._session_messages.values())
        }
        
        # 添加鍵值管理器統計
        if self.key_manager:
            key_manager_stats = self.key_manager.get_stats()
            base_stats["key_manager"] = key_manager_stats
        
        return base_stats
