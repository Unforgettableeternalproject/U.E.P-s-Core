# modules/llm_module/context_cache.py
"""
上下文快取管理器

實現 Context Caching 功能，提升對話連續性和效率：
- 系統提示快取
- 身份資訊快取  
- 記憶上下文快取
- 對話歷史快取
"""

import time
import hashlib
from typing import Dict, Any, Optional, List, Tuple, TYPE_CHECKING
from dataclasses import dataclass

if TYPE_CHECKING:
    from .schemas import LLMOutput
from utils.debug_helper import debug_log, info_log, error_log


@dataclass
class CacheEntry:
    """快取條目"""
    content: str
    timestamp: float
    hit_count: int = 0
    last_access: float = 0.0
    metadata: Dict[str, Any] = None
    
    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {}
        if self.last_access == 0.0:
            self.last_access = self.timestamp


class ContextCache:
    """上下文快取管理器"""
    
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        
        # 快取配置
        self.max_entries = config.get("max_entries", 100)
        self.ttl_seconds = config.get("ttl_seconds", 3600)  # 1小時過期
        self.max_content_length = config.get("max_content_length", 10000)
        
        # 快取存儲
        self._cache: Dict[str, CacheEntry] = {}
        
        # 統計數據
        self.stats = {
            "hits": 0,
            "misses": 0,
            "evictions": 0,
            "total_entries": 0
        }
        
        debug_log(2, f"[ContextCache] 初始化完成，最大條目數: {self.max_entries}")
    
    def get_cache_key(self, cache_type: str, content: str, **kwargs) -> str:
        """生成快取鍵"""
        # 組合所有相關資訊
        key_parts = [cache_type, content]
        
        # 添加額外的鍵值對參數
        for key, value in sorted(kwargs.items()):
            key_parts.append(f"{key}:{str(value)}")
        
        key_string = "|".join(key_parts)
        
        # 使用 MD5 生成短鍵
        return hashlib.md5(key_string.encode('utf-8')).hexdigest()
    
    def get(self, cache_key: str) -> Optional[str]:
        """從快取獲取內容"""
        current_time = time.time()
        
        if cache_key not in self._cache:
            self.stats["misses"] += 1
            return None
        
        entry = self._cache[cache_key]
        
        # 檢查是否過期
        if current_time - entry.timestamp > self.ttl_seconds:
            del self._cache[cache_key]
            self.stats["misses"] += 1
            debug_log(3, f"[ContextCache] 快取條目過期: {cache_key[:8]}...")
            return None
        
        # 更新訪問統計
        entry.hit_count += 1
        entry.last_access = current_time
        self.stats["hits"] += 1
        
        debug_log(3, f"[ContextCache] 快取命中: {cache_key[:8]}... (命中次數: {entry.hit_count})")
        return entry.content
    
    def put(self, cache_key: str, content: str, metadata: Optional[Dict] = None) -> bool:
        """將內容存入快取"""
        if len(content) > self.max_content_length:
            debug_log(2, f"[ContextCache] 內容過長，跳過快取: {len(content)} > {self.max_content_length}")
            return False
        
        current_time = time.time()
        
        # 檢查快取容量
        if len(self._cache) >= self.max_entries:
            self._evict_lru()
        
        # 創建新條目
        entry = CacheEntry(
            content=content,
            timestamp=current_time,
            metadata=metadata or {}
        )
        
        self._cache[cache_key] = entry
        self.stats["total_entries"] += 1
        
        debug_log(3, f"[ContextCache] 添加快取條目: {cache_key[:8]}... (大小: {len(content)})")
        return True
    
    def cache_system_prompt(self, system_prompt: str, mode: str = "chat") -> str:
        """快取系統提示"""
        cache_key = self.get_cache_key("system_prompt", system_prompt, mode=mode)
        
        cached = self.get(cache_key)
        if cached:
            return cached
        
        # 存入快取
        self.put(cache_key, system_prompt, {"type": "system_prompt", "mode": mode})
        return system_prompt
    
    def cache_identity_context(self, identity_data: Dict[str, Any]) -> str:
        """快取身份上下文"""
        # 創建身份的字符串表示
        identity_str = str(sorted(identity_data.items()))
        cache_key = self.get_cache_key("identity", identity_str)
        
        cached = self.get(cache_key)
        if cached:
            return cached
        
        # 格式化身份資訊
        formatted = self._format_identity_context(identity_data)
        self.put(cache_key, formatted, {"type": "identity", "identity_id": identity_data.get("identity", {}).get("id")})
        return formatted
    
    def cache_memory_context(self, memory_content: str, identity_id: Optional[str] = None) -> str:
        """快取記憶上下文"""
        cache_key = self.get_cache_key("memory", memory_content, identity_id=identity_id or "")
        
        cached = self.get(cache_key)
        if cached:
            return cached
        
        # 格式化記憶內容
        formatted = f"相關記憶：\n{memory_content}"
        self.put(cache_key, formatted, {"type": "memory", "identity_id": identity_id})
        return formatted
    
    def cache_conversation_history(self, history: List[Dict], max_entries: int = 5) -> str:
        """快取對話歷史"""
        # 只快取最近的對話
        recent_history = history[-max_entries:] if len(history) > max_entries else history
        history_str = str(recent_history)
        cache_key = self.get_cache_key("conversation", history_str)
        
        cached = self.get(cache_key)
        if cached:
            return cached
        
        # 格式化對話歷史
        formatted = self._format_conversation_history(recent_history)
        self.put(cache_key, formatted, {"type": "conversation", "entries": len(recent_history)})
        return formatted
    
    def _format_identity_context(self, identity_data: Dict[str, Any]) -> str:
        """格式化身份上下文"""
        try:
            identity = identity_data.get("identity", {})
            preferences = identity_data.get("preferences", {})
            
            parts = []
            
            # 基本身份資訊
            name = identity.get("name", "使用者")
            parts.append(f"使用者：{name}")
            
            # 對話偏好
            conversation_prefs = preferences.get("conversation", {})
            if conversation_prefs:
                formality = conversation_prefs.get("formality", "neutral")
                verbosity = conversation_prefs.get("verbosity", "moderate")
                parts.append(f"偏好：{formality} 正式度，{verbosity} 詳細度")
            
            return "；".join(parts)
            
        except Exception as e:
            error_log(f"[ContextCache] 格式化身份上下文失敗: {e}")
            return "使用者身份資訊"
    
    def _format_conversation_history(self, history: List[Dict]) -> str:
        """格式化對話歷史"""
        try:
            if not history:
                return ""
            
            formatted_entries = []
            for entry in history:
                role = entry.get("role", "unknown")
                content = entry.get("content", "")
                
                if role == "user":
                    formatted_entries.append(f"使用者：{content}")
                elif role == "assistant":
                    formatted_entries.append(f"U.E.P：{content}")
            
            if formatted_entries:
                return "最近對話：\n" + "\n".join(formatted_entries)
            return ""
            
        except Exception as e:
            error_log(f"[ContextCache] 格式化對話歷史失敗: {e}")
            return ""
    
    def _evict_lru(self):
        """移除最少使用的快取條目"""
        if not self._cache:
            return
        
        # 找到最少使用的條目
        lru_key = min(self._cache.keys(), 
                     key=lambda k: (self._cache[k].hit_count, self._cache[k].last_access))
        
        del self._cache[lru_key]
        self.stats["evictions"] += 1
        
        debug_log(3, f"[ContextCache] 移除 LRU 快取條目: {lru_key[:8]}...")
    
    def clear_cache(self, cache_type: Optional[str] = None):
        """清理快取"""
        if cache_type is None:
            # 清理全部快取
            cleared_count = len(self._cache)
            self._cache.clear()
            debug_log(2, f"[ContextCache] 清理所有快取，移除 {cleared_count} 個條目")
        else:
            # 清理特定類型的快取
            keys_to_remove = []
            for key, entry in self._cache.items():
                if entry.metadata.get("type") == cache_type:
                    keys_to_remove.append(key)
            
            for key in keys_to_remove:
                del self._cache[key]
                
            debug_log(2, f"[ContextCache] 清理 {cache_type} 類型快取，移除 {len(keys_to_remove)} 個條目")
    
    def cleanup_expired(self):
        """清理過期快取"""
        current_time = time.time()
        expired_keys = []
        
        for key, entry in self._cache.items():
            if current_time - entry.timestamp > self.ttl_seconds:
                expired_keys.append(key)
        
        for key in expired_keys:
            del self._cache[key]
        
        if expired_keys:
            debug_log(2, f"[ContextCache] 清理過期快取，移除 {len(expired_keys)} 個條目")
    
    def get_cache_stats(self) -> Dict[str, Any]:
        """獲取快取統計"""
        current_entries = len(self._cache)
        hit_rate = self.stats["hits"] / (self.stats["hits"] + self.stats["misses"]) if (self.stats["hits"] + self.stats["misses"]) > 0 else 0
        
        return {
            **self.stats,
            "current_entries": current_entries,
            "hit_rate": hit_rate,
            "memory_usage_mb": sum(len(entry.content.encode('utf-8')) for entry in self._cache.values()) / (1024 * 1024)
        }
    
    def get_cache_info(self) -> List[Dict[str, Any]]:
        """獲取快取詳細資訊"""
        return [
            {
                "key": key[:12] + "...",
                "type": entry.metadata.get("type", "unknown"),
                "size": len(entry.content),
                "age_seconds": time.time() - entry.timestamp,
                "hit_count": entry.hit_count,
                "last_access": entry.last_access
            }
            for key, entry in self._cache.items()
        ]
    
    def get_cached_response(self, cache_key: str) -> Optional["LLMOutput"]:
        """獲取快取的 LLM 回應"""
        from .schemas import LLMOutput
        import json
        
        cached_content = self.get(cache_key)
        if cached_content:
            try:
                # 假設快取的是 LLMOutput 的 JSON 序列化
                cached_data = json.loads(cached_content)
                return LLMOutput(**cached_data)
            except Exception as e:
                debug_log(1, f"[ContextCache] 快取反序列化失敗: {e}")
                return None
        return None
    
    def cache_response(self, cache_key: str, llm_output: "LLMOutput") -> bool:
        """快取 LLM 回應"""
        try:
            # 序列化 LLMOutput
            serialized = llm_output.model_dump_json()
            return self.put(cache_key, serialized, cache_type="llm_response")
        except Exception as e:
            debug_log(1, f"[ContextCache] 快取序列化失敗: {e}")
            return False
    
    def get_cache_statistics(self) -> Dict[str, Any]:
        """獲取快取統計（兼容性方法）"""
        return self.get_statistics()
    
    def get_statistics(self) -> Dict[str, Any]:
        """獲取快取統計資訊"""
        return {
            "total_entries": len(self._cache),
            "max_size": self.max_size,
            "access_order_count": len(self.access_order),
            "cache_info": f"{len(self._cache)}/{self.max_size} entries"
        }