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


class SnapshotManager:
    """快照管理器"""
    
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        
        # 快照配置
        self.max_snapshot_size = config.get("max_snapshot_size", 1000)
        self.auto_snapshot_interval = config.get("auto_snapshot_interval", 300)  # 5分鐘
        self.snapshot_retention_days = config.get("snapshot_retention_days", 30)
        self.compression_enabled = config.get("compression_enabled", True)
        
        # 快照緩存
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
    
    def initialize(self) -> bool:
        """初始化快照管理器"""
        try:
            info_log("[SnapshotManager] 初始化快照管理器...")
            
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
                snapshot_id=f"snapshot_{session_id}_{int(time.time())}",
                session_id=session_id,
                memory_token=memory_token,
                start_time=datetime.now(),
                end_time=None,
                messages=[],
                summary="",
                key_topics=[],
                                participant_info={memory_token: {"role": "user", "name": "User"}},
                context_data=initial_context or {},
                metadata={"auto_generated": False}
            )
            
            self._active_snapshots[session_id] = snapshot
            self._snapshot_contexts[session_id] = context
            self._session_messages[session_id] = []
            self._last_auto_snapshot[session_id] = time.time()
            
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
            context = self._snapshot_contexts[session_id]
            
            # 添加訊息
            snapshot.messages.append(message_data)
            self._session_messages[session_id].append(message_data)
            
            # 更新上下文
            context.message_count += 1
            context.interaction_depth = len(snapshot.messages)
            
            # 更新參與者資訊
            speaker = message_data.get("speaker", "unknown")
            if speaker not in snapshot.participant_info:
                snapshot.participant_info[speaker] = {"message_count": 0}
                context.participant_count += 1
            
            snapshot.participant_info[speaker]["message_count"] += 1
            
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
    
    def _extract_topics_from_message(self, content: str) -> Set[str]:
        """從訊息內容提取主題"""
        # 簡單主題提取邏輯
        topics = set()
        
        # 關鍵字提取
        keywords = [
            "問題", "解決", "建議", "幫助", "學習", "工作", "技術", "程式",
            "資料", "分析", "設計", "開發", "測試", "部署", "維護", "優化"
        ]
        
        for keyword in keywords:
            if keyword in content:
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
                topic=topic,
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
                snapshot_id=snapshot_name or f"manual_{session_id}_{int(time.time())}",
                session_id=session_id,
                identity_token=snapshot.identity_token,
                start_time=snapshot.start_time,
                end_time=datetime.now(),
                messages=snapshot.messages.copy() if include_full_context else snapshot.messages[-20:],
                summary=self._create_session_summary(snapshot, context),
                key_topics=list(context.primary_topics),
                participant_info=snapshot.participant_info.copy(),
                context_data=snapshot.context_data.copy(),
                metadata={
                    "auto_generated": False,
                    "manual_creation": True,
                    "message_count": context.message_count,
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
        return {
            **self.stats,
            "active_sessions": len(self._active_snapshots),
            "total_messages": sum(len(msgs) for msgs in self._session_messages.values())
        }
