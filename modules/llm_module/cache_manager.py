# modules/llm_module/cache_manager.py
"""
Context Caching Manager - 基於Gemini 2.5 Flash-Lite的顯性/隱性快取管理

根據文件建議實現：
1. 顯性快取：長期穩定內容（persona、functions、style_policy）
2. 隱性快取：短期重複內容的自動快取
3. CHAT/WORK模式的不同TTL策略
4. 成本優化和命中率統計
"""

import time, os
import hashlib
from typing import Dict, Any, Optional, List, Callable, TYPE_CHECKING
from dataclasses import dataclass, field
from enum import Enum

from google import genai
from google.genai import types
from configs.config_loader import load_module_config
from utils.debug_helper import debug_log, info_log, error_log

if TYPE_CHECKING:
    from .schemas import LLMOutput


class CacheType(Enum):
    """快取類型"""
    PERSONA = "persona"              # 系統人格/身份 - 24h TTL
    FUNCTIONS = "functions"          # 函數規格 - 12h TTL  
    STYLE_POLICY = "style_policy"    # 風格策略 - 24h TTL
    SESSION_ANCHOR = "session_anchor" # 會話錨點 - 30分鐘 TTL
    TASK_SPEC = "task_spec"         # 任務規格 - 5-30分鐘 TTL


@dataclass
class CacheInfo:
    """快取信息"""
    name: str
    cache_type: CacheType
    model: str
    created_at: float
    ttl_seconds: int
    content_hash: str
    usage_count: int = 0
    last_used: Optional[float] = None
    cached_content_id: Optional[str] = None


@dataclass
class LocalCacheEntry:
    """本地快取條目"""
    content: Any
    timestamp: float
    hit_count: int = 0
    last_access: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def is_expired(self, ttl_seconds: int) -> bool:
        """檢查是否過期"""
        return time.time() - self.timestamp > ttl_seconds


@dataclass 
class CacheStats:
    """快取統計"""
    # Gemini顯性快取統計
    total_caches: int = 0
    active_caches: int = 0
    gemini_hit_count: int = 0
    gemini_miss_count: int = 0
    total_tokens_cached: int = 0
    total_tokens_saved: int = 0
    storage_cost_tokens: int = 0
    
    # 本地快取統計
    local_hit_count: int = 0
    local_miss_count: int = 0
    local_evictions: int = 0
    local_total_entries: int = 0


class CacheManager:
    """
    Context Caching管理器 - Gemini 2.5 Flash-Lite專用
    
    功能：
    1. 顯性快取管理（系統提示、函數規格、風格政策）
    2. TTL策略（CHAT長期，WORK短期）
    3. 成本追蹤和統計
    4. 自動快取更新和版本管理
    """
    
    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.config = config or load_module_config("llm_module").get("cache_manager", {})
        
        # Gemini客戶端
        self.client = genai.Client(
            vertexai=True,
            project=os.getenv("GCP_PROJECT_ID"),
            location=os.getenv("GCP_LOCATION"),
        )
        self.model_name = self.config.get("model_name", "gemini-2.5-flash-lite")
        
        # Gemini顯性快取管理
        self.caches: Dict[str, CacheInfo] = {}
        self.stats = CacheStats()
        
        # 本地快取管理
        self.local_cache: Dict[str, LocalCacheEntry] = {}
        self.max_local_entries = self.config.get("max_local_entries", 100)
        self.local_ttl_seconds = self.config.get("local_ttl_seconds", 1800)  # 30分鐘
        
        # TTL設定
        self.default_ttl = {
            CacheType.PERSONA: 24 * 3600,        # 24小時
            CacheType.FUNCTIONS: 12 * 3600,       # 12小時  
            CacheType.STYLE_POLICY: 24 * 3600,    # 24小時
            CacheType.SESSION_ANCHOR: 30 * 60,    # 30分鐘
            CacheType.TASK_SPEC: 15 * 60          # 15分鐘
        }
        
        # 最小快取大小（tokens）
        self.min_cache_size = self.config.get("min_cache_size", 1024)
        
        debug_log(1, "[CacheManager] 初始化完成")
    
    def get_or_create_cache(self, 
                           name: str,
                           cache_type: CacheType,
                           content_builder: Callable[[], str],
                           ttl_seconds: Optional[int] = None,
                           force_rebuild: bool = False) -> Optional[str]:
        """
        獲取或創建快取
        
        Args:
            name: 快取名稱 (格式: uep:{type}:{version})
            cache_type: 快取類型
            content_builder: 內容生成函數
            ttl_seconds: TTL秒數，None則使用預設
            force_rebuild: 強制重建
            
        Returns:
            cached_content_id 或 None
        """
        try:
            current_time = time.time()
            
            # 1. 檢查現有快取
            if not force_rebuild and name in self.caches:
                cache_info = self.caches[name]
                
                # 檢查TTL
                if current_time - cache_info.created_at < cache_info.ttl_seconds:
                    # 更新使用統計
                    cache_info.usage_count += 1
                    cache_info.last_used = current_time
                    self.stats.gemini_hit_count += 1
                    
                    debug_log(2, f"[CacheManager] 快取命中: {name}")
                    return cache_info.cached_content_id
                else:
                    # TTL過期，刪除舊快取
                    debug_log(2, f"[CacheManager] 快取過期，刪除: {name}")
                    self.delete_cache(name)
            
            # 2. 生成新內容
            content = content_builder()
            if not content:
                error_log(f"[CacheManager] 快取內容生成失敗: {name}")
                return None
            
            # 3. 檢查最小大小
            estimated_tokens = max(1, len(content) // 4)
            if estimated_tokens < self.min_cache_size:
                debug_log(1, f"[CacheManager] 內容過小，不建立快取: {name} ({estimated_tokens} tokens)")
                return None
            
            # 4. 創建顯性快取
            ttl = ttl_seconds or self.default_ttl.get(cache_type, 3600)
            cached_content = self._create_cached_content(content, name, ttl)
            
            if cached_content:
                # 5. 記錄快取信息
                content_hash = hashlib.md5(content.encode()).hexdigest()
                cache_info = CacheInfo(
                    name=name,
                    cache_type=cache_type,
                    model=self.model_name,
                    created_at=current_time,
                    ttl_seconds=ttl,
                    content_hash=content_hash,
                    cached_content_id=cached_content.name
                )
                
                self.caches[name] = cache_info
                self.stats.total_caches += 1
                self.stats.active_caches += 1
                self.stats.total_tokens_cached += estimated_tokens
                
                info_log(f"[CacheManager] 快取建立成功: {name}, TTL: {ttl}s, Tokens: ~{estimated_tokens}")
                return cached_content.name
            
            return None
            
        except Exception as e:
            error_log(f"[CacheManager] 快取操作失敗 {name}: {e}")
            self.stats.gemini_miss_count += 1
            return None
    
    def _create_cached_content(self, content: str, display_name: str, ttl_seconds: int):
        """創建Gemini顯性快取"""
        try:
            cache = self.client.caches.create(
                model=self.model_name,
                config=types.CreateCachedContentConfig(
                    display_name=display_name,
                    system_instruction=content,     # 主要塞在 system_instruction
                    contents=[],                    # 保留欄位（未來要掛檔案/規格可以用）
                    ttl=f"{ttl_seconds}s"
                )
            )
            
            debug_log(2, f"[CacheManager] Gemini快取建立: {cache.name}")
            return cache
            
        except Exception as e:
            error_log(f"[CacheManager] Gemini快取建立失敗: {e}")
            return None
    
    def update_ttl(self, name: str, ttl_seconds: int) -> bool:
        """更新快取TTL"""
        try:
            if name not in self.caches:
                return False
                
            cache_info = self.caches[name]
            
            # 更新Gemini快取TTL
            self.client.caches.update(
                name=cache_info.cached_content_id,
                config=types.UpdateCachedContentConfig(
                    ttl=f"{ttl_seconds}s"
                )
            )
            
            # 更新本地記錄
            cache_info.ttl_seconds = ttl_seconds
            
            debug_log(2, f"[CacheManager] TTL更新成功: {name} -> {ttl_seconds}s")
            return True
            
        except Exception as e:
            error_log(f"[CacheManager] TTL更新失敗 {name}: {e}")
            return False
    
    def delete_cache(self, name: str) -> bool:
        """刪除快取"""
        try:
            if name not in self.caches:
                return False
                
            cache_info = self.caches[name]
            
            # 刪除Gemini快取
            if cache_info.cached_content_id:
                self.client.caches.delete(name=cache_info.cached_content_id)
            
            # 更新統計
            del self.caches[name]
            self.stats.active_caches -= 1
            
            debug_log(2, f"[CacheManager] 快取刪除成功: {name}")
            return True
            
        except Exception as e:
            error_log(f"[CacheManager] 快取刪除失敗 {name}: {e}")
            return False
    
    def get_cache_statistics(self, name: Optional[str] = None) -> Dict[str, Any]:
        """獲取快取統計"""
        if name and name in self.caches:
            cache_info = self.caches[name]
            return {
                "name": cache_info.name,
                "type": cache_info.cache_type.value,
                "created_at": cache_info.created_at,
                "ttl_seconds": cache_info.ttl_seconds,
                "usage_count": cache_info.usage_count,
                "last_used": cache_info.last_used
            }
        else:
            # 計算總命中率
            total_hits = self.stats.gemini_hit_count + self.stats.local_hit_count
            total_misses = self.stats.gemini_miss_count + self.stats.local_miss_count
            overall_hit_rate = total_hits / (total_hits + total_misses) if (total_hits + total_misses) > 0 else 0.0
            
            return {
                # Gemini顯性快取統計
                "gemini_caches": {
                    "total_caches": self.stats.total_caches,
                    "active_caches": self.stats.active_caches,
                    "hit_count": self.stats.gemini_hit_count,
                    "miss_count": self.stats.gemini_miss_count,
                    "hit_rate": self.stats.gemini_hit_count / (self.stats.gemini_hit_count + self.stats.gemini_miss_count) if (self.stats.gemini_hit_count + self.stats.gemini_miss_count) > 0 else 0.0,
                    "total_tokens_cached": self.stats.total_tokens_cached,
                    "cache_names": list(self.caches.keys())
                },
                # 本地快取統計
                "local_cache": {
                    "active_entries": len(self.local_cache),
                    "max_entries": self.max_local_entries,
                    "hit_count": self.stats.local_hit_count,
                    "miss_count": self.stats.local_miss_count,
                    "hit_rate": self.stats.local_hit_count / (self.stats.local_hit_count + self.stats.local_miss_count) if (self.stats.local_hit_count + self.stats.local_miss_count) > 0 else 0.0,
                    "evictions": self.stats.local_evictions,
                    "ttl_seconds": self.local_ttl_seconds
                },
                # 總體統計
                "overall": {
                    "total_hit_count": total_hits,
                    "total_miss_count": total_misses,
                    "overall_hit_rate": overall_hit_rate,
                    "total_tokens_saved": self.stats.total_tokens_saved
                }
            }
    
    def cleanup_expired_caches(self) -> int:
        """清理過期快取"""
        expired_count = 0
        current_time = time.time()
        
        expired_names = []
        for name, cache_info in self.caches.items():
            if current_time - cache_info.created_at >= cache_info.ttl_seconds:
                expired_names.append(name)
        
        for name in expired_names:
            if self.delete_cache(name):
                expired_count += 1
        
        if expired_count > 0:
            info_log(f"[CacheManager] 清理過期快取: {expired_count}個")
        
        return expired_count
    
    def preload_system_caches(self) -> Dict[str, str]:
        """預載系統快取（persona、functions、style_policy）"""
        preloaded = {}
        
        try:
            # 預載persona快取
            persona_cache = self.get_or_create_cache(
                name="uep:persona:v1",
                cache_type=CacheType.PERSONA,
                content_builder=self._build_persona_content
            )
            if persona_cache:
                preloaded["persona"] = persona_cache
            
            # 預載functions快取  
            functions_cache = self.get_or_create_cache(
                name="uep:functions:v1", 
                cache_type=CacheType.FUNCTIONS,
                content_builder=self._build_functions_content
            )
            if functions_cache:
                preloaded["functions"] = functions_cache
            
            # 預載style_policy快取
            style_cache = self.get_or_create_cache(
                name="uep:style_policy:v1",
                cache_type=CacheType.STYLE_POLICY, 
                content_builder=self._build_style_policy_content
            )
            if style_cache:
                preloaded["style_policy"] = style_cache
            
            info_log(f"[CacheManager] 系統快取預載完成: {len(preloaded)}個")
            return preloaded
            
        except Exception as e:
            error_log(f"[CacheManager] 系統快取預載失敗: {e}")
            return {}
    
    def _build_persona_content(self) -> str:
        """構建persona內容"""
        # TODO: 實際的persona內容構建
        return """
        你是U.E.P (Unified Experience Partner)，一個智能助理系統。
        你的特點：友善、專業、樂於助人，具有學習和記憶能力。
        你會根據系統狀態調整回應風格和行為模式。
        """
    
    def _build_functions_content(self) -> str:
        """構建functions內容（從functions.yaml展開）"""
        # TODO: 實際從functions.yaml讀取和展開
        return """
        可用系統功能：
        - 檔案操作：開啟、建立、刪除、複製檔案
        - 系統指令：執行程式、搜尋檔案、查詢資訊
        - 記憶管理：儲存、檢索對話記憶
        - 狀態管理：更新系統情緒和行為參數
        """
    
    def _build_style_policy_content(self) -> str:
        """構建風格策略內容"""
        # TODO: 實際的風格策略規則
        return """
        回應風格規則：
        1. 使用Traditional Chinese (zh-TW)
        2. 根據Mood值調整語氣：高=活潑，低=沉穩
        3. 根據Pride值調整自信度：高=積極，低=謙遜  
        4. 根據Boredom值決定主動性：高=主動建議，低=被動回應
        JSON輸出格式規範和安全規則...
        """

    # ========== 本地快取功能 (整合自ContextCache) ==========
    
    def generate_cache_key(self, cache_type: str, content: str, **kwargs) -> str:
        """生成快取鍵"""
        key_parts = [cache_type, content]
        
        # 添加額外的鍵值對參數
        for key, value in sorted(kwargs.items()):
            key_parts.append(f"{key}:{str(value)}")
        
        key_string = "|".join(key_parts)
        return hashlib.md5(key_string.encode('utf-8')).hexdigest()
    
    def get_local_cache(self, cache_key: str) -> Optional[Any]:
        """從本地快取獲取內容"""
        current_time = time.time()
        
        if cache_key not in self.local_cache:
            self.stats.local_miss_count += 1
            return None
        
        entry = self.local_cache[cache_key]
        
        # 檢查是否過期
        if entry.is_expired(self.local_ttl_seconds):
            del self.local_cache[cache_key]
            self.stats.local_miss_count += 1
            debug_log(3, f"[CacheManager] 本地快取過期: {cache_key[:8]}...")
            return None
        
        # 更新訪問統計
        entry.hit_count += 1
        entry.last_access = current_time
        self.stats.local_hit_count += 1
        
        debug_log(3, f"[CacheManager] 本地快取命中: {cache_key[:8]}... (命中次數: {entry.hit_count})")
        return entry.content
    
    def put_local_cache(self, cache_key: str, content: Any, metadata: Optional[Dict[str, Any]] = None) -> None:
        """存入本地快取"""
        current_time = time.time()
        
        # 檢查是否需要淘汰舊條目
        if len(self.local_cache) >= self.max_local_entries:
            self._evict_lru_local_cache()
        
        # 創建快取條目
        entry = LocalCacheEntry(
            content=content,
            timestamp=current_time,
            metadata=metadata or {}
        )
        
        self.local_cache[cache_key] = entry
        self.stats.local_total_entries += 1
        
        debug_log(3, f"[CacheManager] 本地快取存入: {cache_key[:8]}...")
    
    def _evict_lru_local_cache(self) -> None:
        """淘汰最久未使用的本地快取條目"""
        if not self.local_cache:
            return
        
        # 找到最久未使用的條目
        oldest_key = min(self.local_cache.keys(), 
                        key=lambda k: self.local_cache[k].last_access)
        
        del self.local_cache[oldest_key]
        self.stats.local_evictions += 1
        
        debug_log(3, f"[CacheManager] 淘汰本地快取條目: {oldest_key[:8]}...")
    
    def get_cached_response(self, text: str, mode: str = "chat") -> Optional["LLMOutput"]:
        """獲取快取的LLM回應"""
        cache_key = self.generate_cache_key("llm_response", text, mode=mode)
        return self.get_local_cache(cache_key)
    
    def cache_response(self, cache_key: str, response: "LLMOutput") -> None:
        """快取LLM回應"""
        self.put_local_cache(cache_key, response, {"type": "llm_response"})
    
    def cache_identity_context(self, identity_data: Dict[str, Any]) -> str:
        """快取身份上下文"""
        identity_str = str(identity_data)
        cache_key = self.generate_cache_key("identity", identity_str)
        
        cached = self.get_local_cache(cache_key)
        if cached:
            return cached
        
        # 格式化身份上下文
        formatted = self._format_identity_context(identity_data)
        self.put_local_cache(cache_key, formatted, {
            "type": "identity", 
            "identity_id": identity_data.get("current_identity")
        })
        return formatted
    
    def cache_memory_context(self, memory_content: str, identity_id: Optional[str] = None) -> str:
        """快取記憶上下文"""
        cache_key = self.generate_cache_key("memory", memory_content, identity_id=identity_id or "")
        
        cached = self.get_local_cache(cache_key)
        if cached:
            return cached
        
        # 格式化記憶內容
        formatted = f"相關記憶：\n{memory_content}"
        self.put_local_cache(cache_key, formatted, {"type": "memory", "identity_id": identity_id})
        return formatted
    
    def _format_identity_context(self, identity_data: Dict[str, Any]) -> str:
        """格式化身份上下文"""
        try:
            current_identity = identity_data.get("current_identity")
            traits = identity_data.get("identity_traits", {})
            preferences = identity_data.get("identity_preferences", {})
            
            parts = []
            
            # 基本身份資訊
            if current_identity:
                parts.append(f"當前身份：{current_identity}")
            
            # 身份特徵
            if traits:
                trait_strs = [f"{k}: {v}" for k, v in traits.items()]
                parts.append(f"身份特徵：{', '.join(trait_strs)}")
            
            # 偏好設定
            if preferences:
                pref_strs = [f"{k}: {v}" for k, v in preferences.items()]
                parts.append(f"偏好設定：{', '.join(pref_strs)}")
            
            return "\n".join(parts) if parts else "無特定身份上下文"
            
        except Exception as e:
            error_log(f"[CacheManager] 格式化身份上下文失敗: {e}")
            return "身份上下文處理失敗"
    
    def cleanup_expired_local_cache(self) -> int:
        """清理過期的本地快取"""
        expired_count = 0
        expired_keys = []
        
        for key, entry in self.local_cache.items():
            if entry.is_expired(self.local_ttl_seconds):
                expired_keys.append(key)
        
        for key in expired_keys:
            del self.local_cache[key]
            expired_count += 1
        
        if expired_count > 0:
            debug_log(2, f"[CacheManager] 清理本地快取: {expired_count}個過期條目")
        
        return expired_count


# 全局快取管理器實例
cache_manager = CacheManager()