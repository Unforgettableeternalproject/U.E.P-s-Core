# modules/mem_module/core/snapshot_key_manager.py
"""
快照鍵值管理器 - 使用summarizer生成快照鍵值並管理快照查詢

功能：
- 使用summarizer為對話生成描述性鍵值
- 基於鍵值查詢相關快照
- 管理快照的創建決策邏輯
"""

import time
from typing import Dict, Any, Optional, List, Set, Tuple
from datetime import datetime

from utils.debug_helper import debug_log, info_log, error_log
from ..schemas import ConversationSnapshot, MemoryOperationResult


class SnapshotKeyManager:
    """快照鍵值管理器"""
    
    def __init__(self, config: Dict[str, Any], memory_summarizer: Optional[Any] = None):
        self.config = config
        self.memory_summarizer = memory_summarizer
        
        # 鍵值生成配置
        self.min_content_length = config.get("min_content_length", 20)
        self.key_similarity_threshold = config.get("key_similarity_threshold", 0.7)
        self.max_key_length = config.get("max_key_length", 100)
        
        # 快照鍵值緩存
        self._snapshot_keys: Dict[str, str] = {}  # snapshot_id -> key
        self._key_snapshots: Dict[str, List[str]] = {}  # key -> [snapshot_ids]
        
        # 統計資訊
        self.stats = {
            "keys_generated": 0,
            "queries_processed": 0,
            "cache_hits": 0,
            "new_snapshots_created": 0
        }
        
        self.is_initialized = False
    
    def initialize(self) -> bool:
        """初始化快照鍵值管理器"""
        try:
            info_log("[SnapshotKeyManager] 初始化快照鍵值管理器...")
            
            if not self.memory_summarizer:
                error_log("[SnapshotKeyManager] 缺少MemorySummarizer實例")
                return False
            
            self.is_initialized = True
            info_log("[SnapshotKeyManager] 快照鍵值管理器初始化完成")
            return True
            
        except Exception as e:
            error_log(f"[SnapshotKeyManager] 初始化失敗: {e}")
            return False
    
    def generate_snapshot_key(self, content: str, context: Dict[str, Any] = None) -> str:  # type: ignore
        """使用summarizer生成快照鍵值"""
        try:
            self.stats["keys_generated"] += 1
            
            if not content or len(content.strip()) < self.min_content_length:
                return "general_conversation"
            
            # 使用summarizer生成摘要作為鍵值
            if self.memory_summarizer and self.memory_summarizer.is_initialized:
                # 生成簡短摘要
                summary = self.memory_summarizer.chunk_and_summarize_memories([content])
                
                if summary and len(summary.strip()) > 5:
                    # 清理和格式化摘要作為鍵值
                    key = self._format_summary_as_key(summary)
                    debug_log(3, f"[SnapshotKeyManager] 生成快照鍵值: {key}")
                    return key
            
            # 回退到簡單邏輯
            return self._generate_simple_key(content, context)
            
        except Exception as e:
            error_log(f"[SnapshotKeyManager] 生成快照鍵值失敗: {e}")
            return "conversation_" + str(int(time.time()))
    
    def _format_summary_as_key(self, summary: str) -> str:
        """將摘要格式化為快照鍵值"""
        try:
            # 清理摘要文本
            key = summary.strip()
            
            # 移除不必要的標點符號
            key = key.replace("\n", " ").replace("\r", " ")
            key = " ".join(key.split())  # 標準化空格
            
            # 限制長度
            if len(key) > self.max_key_length:
                key = key[:self.max_key_length].rstrip()
                # 確保不在單詞中間截斷
                last_space = key.rfind(" ")
                if last_space > self.max_key_length * 0.8:  # 如果最後一個空格不太遠
                    key = key[:last_space]
            
            # 如果結果太短，添加後綴
            if len(key) < 10:
                key += f"_conv_{int(time.time())}"
            
            return key.lower()
            
        except Exception as e:
            error_log(f"[SnapshotKeyManager] 格式化鍵值失敗: {e}")
            return f"summary_conv_{int(time.time())}"
    
    def _generate_simple_key(self, content: str, context: Dict[str, Any] = None) -> str: # type: ignore
        """生成簡單鍵值 - 回退方法"""
        try:
            # 提取關鍵詞
            words = content.lower().split()
            keywords = []
            
            # 常見關鍵詞
            important_words = [
                "help", "problem", "question", "learn", "work", "project", 
                "code", "debug", "error", "fix", "create", "build"
            ]
            
            for word in words:
                if word in important_words and len(word) > 3:
                    keywords.append(word)
                    if len(keywords) >= 3:
                        break
            
            if keywords:
                key = "_".join(keywords)
            else:
                # 使用前幾個有意義的詞
                meaningful_words = [w for w in words if len(w) > 3][:3]
                key = "_".join(meaningful_words) if meaningful_words else "general"
            
            return f"{key}_conv"
            
        except Exception as e:
            error_log(f"[SnapshotKeyManager] 生成簡單鍵值失敗: {e}")
            return f"conv_{int(time.time())}"
    
    def query_related_snapshots(self, content: str, memory_token: str) -> Tuple[List[str], bool]:
        """
        查詢相關快照
        
        Returns:
            Tuple[List[str], bool]: (相關快照ID列表, 是否需要創建新快照)
        """
        try:
            self.stats["queries_processed"] += 1
            
            # 生成查詢鍵值
            query_key = self.generate_snapshot_key(content)
            
            # 查找完全匹配的快照
            exact_matches = self._key_snapshots.get(query_key, [])
            
            if exact_matches:
                self.stats["cache_hits"] += 1
                debug_log(3, f"[SnapshotKeyManager] 找到完全匹配快照: {len(exact_matches)} 個")
                return exact_matches, False  # 找到匹配，不需要新快照
            
            # 查找相似的快照
            similar_snapshots = self._find_similar_snapshots(query_key)
            
            if similar_snapshots:
                debug_log(3, f"[SnapshotKeyManager] 找到相似快照: {len(similar_snapshots)} 個")
                return similar_snapshots, False  # 找到相似，不需要新快照
            
            # 沒有找到相關快照，需要創建新的
            debug_log(2, f"[SnapshotKeyManager] 未找到相關快照，需要創建新快照")
            return [], True
            
        except Exception as e:
            error_log(f"[SnapshotKeyManager] 查詢相關快照失敗: {e}")
            return [], True  # 出錯時創建新快照
    
    def _find_similar_snapshots(self, query_key: str) -> List[str]:
        """查找相似的快照"""
        try:
            similar_snapshots = []
            query_words = set(query_key.lower().split())
            
            for existing_key, snapshot_ids in self._key_snapshots.items():
                existing_words = set(existing_key.lower().split())
                
                # 計算詞彙重疊度
                if query_words and existing_words:
                    overlap = len(query_words.intersection(existing_words))
                    total_unique = len(query_words.union(existing_words))
                    similarity = overlap / total_unique if total_unique > 0 else 0
                    
                    if similarity >= self.key_similarity_threshold:
                        similar_snapshots.extend(snapshot_ids)
                        debug_log(4, f"[SnapshotKeyManager] 相似鍵值: {existing_key} (相似度: {similarity:.2f})")
            
            return similar_snapshots
            
        except Exception as e:
            error_log(f"[SnapshotKeyManager] 查找相似快照失敗: {e}")
            return []
    
    def register_snapshot(self, snapshot_id: str, content: str, context: Dict[str, Any] = None): # type: ignore
        """註冊新的快照及其鍵值"""
        try:
            key = self.generate_snapshot_key(content, context)
            
            # 註冊快照
            self._snapshot_keys[snapshot_id] = key
            
            if key not in self._key_snapshots:
                self._key_snapshots[key] = []
            self._key_snapshots[key].append(snapshot_id)
            
            self.stats["new_snapshots_created"] += 1
            debug_log(3, f"[SnapshotKeyManager] 註冊快照: {snapshot_id} -> {key}")
            
        except Exception as e:
            error_log(f"[SnapshotKeyManager] 註冊快照失敗: {e}")
    
    def unregister_snapshot(self, snapshot_id: str):
        """註銷快照"""
        try:
            if snapshot_id in self._snapshot_keys:
                key = self._snapshot_keys[snapshot_id]
                
                # 從鍵值映射中移除
                if key in self._key_snapshots:
                    self._key_snapshots[key] = [
                        sid for sid in self._key_snapshots[key] if sid != snapshot_id
                    ]
                    # 如果鍵值下沒有快照了，移除鍵值
                    if not self._key_snapshots[key]:
                        del self._key_snapshots[key]
                
                # 移除快照映射
                del self._snapshot_keys[snapshot_id]
                
                debug_log(3, f"[SnapshotKeyManager] 註銷快照: {snapshot_id}")
                
        except Exception as e:
            error_log(f"[SnapshotKeyManager] 註銷快照失敗: {e}")
    
    def get_snapshot_key(self, snapshot_id: str) -> Optional[str]:
        """獲取快照的鍵值"""
        return self._snapshot_keys.get(snapshot_id)
    
    def get_snapshots_by_key(self, key: str) -> List[str]:
        """根據鍵值獲取快照ID列表"""
        return self._key_snapshots.get(key, [])
    
    def analyze_conversation_flow(self, memory_token: str, content: str) -> Dict[str, Any]:
        """
        分析對話流程，決定快照管理策略
        
        Returns:
            Dict包含：
            - should_create_new: 是否應該創建新快照
            - related_snapshots: 相關的快照ID列表
            - flow_decision: 流程決策說明
        """
        try:
            # 查詢相關快照
            related_snapshots, should_create = self.query_related_snapshots(content, memory_token)
            
            # 分析決策
            if should_create:
                flow_decision = "沒有找到相關對話歷史，創建新快照開始記錄"
            else:
                flow_decision = f"找到 {len(related_snapshots)} 個相關快照，繼續現有對話"
            
            result = {
                "should_create_new": should_create,
                "related_snapshots": related_snapshots,
                "flow_decision": flow_decision,
                "query_key": self.generate_snapshot_key(content)
            }
            
            debug_log(2, f"[SnapshotKeyManager] 對話流程分析: {flow_decision}")
            return result
            
        except Exception as e:
            error_log(f"[SnapshotKeyManager] 分析對話流程失敗: {e}")
            return {
                "should_create_new": True,
                "related_snapshots": [],
                "flow_decision": "分析失敗，默認創建新快照",
                "query_key": "error_fallback"
            }
    
    def get_stats(self) -> Dict[str, Any]:
        """獲取統計資訊"""
        return {
            **self.stats,
            "total_keys": len(self._key_snapshots),
            "total_snapshots": len(self._snapshot_keys),
            "average_snapshots_per_key": (
                len(self._snapshot_keys) / len(self._key_snapshots) 
                if self._key_snapshots else 0
            )
        }
    
    def cleanup_expired_keys(self, valid_snapshot_ids: Set[str]):
        """清理過期的鍵值映射"""
        try:
            expired_snapshots = []
            
            for snapshot_id in list(self._snapshot_keys.keys()):
                if snapshot_id not in valid_snapshot_ids:
                    expired_snapshots.append(snapshot_id)
            
            for snapshot_id in expired_snapshots:
                self.unregister_snapshot(snapshot_id)
            
            if expired_snapshots:
                debug_log(2, f"[SnapshotKeyManager] 清理了 {len(expired_snapshots)} 個過期鍵值映射")
                
        except Exception as e:
            error_log(f"[SnapshotKeyManager] 清理過期鍵值失敗: {e}")