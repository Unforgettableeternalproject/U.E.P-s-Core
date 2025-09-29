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

# Google genai imports - 與 gemini_client.py 保持一致
# from google import genai  # 在需要時才導入
# from google.genai import types
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
        
        # Gemini客戶端 - 與 gemini_client.py 使用相同配置
        project_id = os.getenv("GCP_PROJECT_ID")
        location = os.getenv("GCP_LOCATION")
        
        if not project_id:
            error_log("[CacheManager] GCP_PROJECT_ID 環境變數未設定，快取功能將受限")
            self.client = None
        else:
            try:
                from google import genai
                self.client = genai.Client(
                    vertexai=True,
                    project=project_id,
                    location=location,
                )
                info_log(f"[CacheManager] Vertex AI 客戶端初始化成功 (專案: {project_id}, 區域: {location})")
            except Exception as e:
                error_log(f"[CacheManager] Vertex AI 客戶端初始化失敗: {e}")
                self.client = None
        
        # 使用支援快取的模型（確認 gemini-2.5-flash-lite 支援快取功能）
        self.model_name = self.config.get("model_name", "gemini-2.5-flash-lite")
        
        # 引用 PromptManager 來獲取真實內容
        from .prompt_manager import PromptManager
        self._prompt_manager = None  # 延遲初始化
        
        # Gemini顯性快取管理
        self.caches: Dict[str, CacheInfo] = {}
        self.stats = CacheStats()
        
        # 本地快取管理
        self.local_cache: Dict[str, LocalCacheEntry] = {}
        self.max_local_entries = self.config.get("max_local_entries", 100)
        self.local_ttl_seconds = self.config.get("local_ttl_seconds", 1800)  # 30分鐘
        
        # TTL設定（基於實際使用頻率，優化成本效益）
        self.default_ttl = {
            CacheType.PERSONA: 30 * 60,           # 30分鐘（頻繁使用的基礎人格）
            CacheType.FUNCTIONS: 30 * 60,         # 30分鐘（系統功能規格）  
            CacheType.STYLE_POLICY: 30 * 60,      # 30分鐘（風格策略）
            CacheType.SESSION_ANCHOR: 15 * 60,    # 15分鐘（會話錨點）
            CacheType.TASK_SPEC: 10 * 60          # 10分鐘（任務規格）
        }
        
        # 最小快取大小（Vertex AI 要求最少 2048 tokens）
        self.min_cache_size = self.config.get("min_cache_size", 2048)
        
        # 智能快取策略設定
        self.smart_cache_threshold = self.config.get("smart_cache_threshold", 1000)  # tokens
        self.usage_frequency_threshold = self.config.get("usage_frequency_threshold", 10)  # 次/小時
        
        # 測試模式控制（從全域配置讀取 debug 模式）
        from configs.config_loader import load_config
        global_config = load_config()
        
        # 優先順序：環境變數 > 模組配置 > 全域 debug 設定
        self.test_mode = (
            self.config.get("test_mode", False) or 
            os.getenv("UEP_TEST_MODE", "false").lower() == "true" or
            global_config.get("debug", {}).get("enabled", False)
        )
        
        if self.test_mode:
            info_log("[CacheManager] 測試/除錯模式：禁用顯性快取功能")
            debug_level = global_config.get("debug", {}).get("debug_level", 1)
            debug_log(2, f"[CacheManager] 除錯等級: {debug_level}")
        
        # 使用頻率追蹤
        self.usage_tracker: Dict[str, Dict[str, Any]] = {}  # {content_hash: {count, last_hour, timestamps}}
        
        debug_log(1, "[CacheManager] 初始化完成")
    
    def should_create_explicit_cache(self, content: str, content_hash: str) -> bool:
        """
        智能決策：是否應該創建顯性快取
        
        策略：
        - < 1000 tokens：使用隱性快取（Gemini 2.5 自動處理）
        - ≥ 1000 tokens 且高頻使用（≥10次/小時）：建顯性快取
        - 測試/除錯模式：禁用顯性快取
        """
        if self.test_mode:
            debug_log(3, "[CacheManager] 測試/除錯模式：跳過顯性快取")
            return False
        
        # 估算 token 數量
        estimated_tokens = max(1, len(content) // 4)
        
        # 小於閾值：使用隱性快取
        if estimated_tokens < self.smart_cache_threshold:
            debug_log(3, f"[CacheManager] 內容較小 ({estimated_tokens} tokens)，使用隱性快取")
            return False
        
        # 檢查使用頻率
        usage_freq = self._get_usage_frequency(content_hash)
        if usage_freq < self.usage_frequency_threshold:
            debug_log(3, f"[CacheManager] 使用頻率低 ({usage_freq}/小時)，使用隱性快取")
            return False
        
        debug_log(2, f"[CacheManager] 高頻內容 ({usage_freq}/小時, {estimated_tokens} tokens)，建議顯性快取")
        return True
    
    def _get_usage_frequency(self, content_hash: str) -> int:
        """獲取內容的使用頻率（次/小時）"""
        current_time = time.time()
        current_hour = int(current_time // 3600)
        
        if content_hash not in self.usage_tracker:
            self.usage_tracker[content_hash] = {
                "count": 0,
                "last_hour": current_hour,
                "timestamps": []
            }
        
        tracker = self.usage_tracker[content_hash]
        
        # 清理超過1小時的記錄
        one_hour_ago = current_time - 3600
        tracker["timestamps"] = [ts for ts in tracker["timestamps"] if ts > one_hour_ago]
        
        # 記錄當前使用
        tracker["timestamps"].append(current_time)
        tracker["count"] = len(tracker["timestamps"])
        tracker["last_hour"] = current_hour
        
        return tracker["count"]
    
    def _get_config_source(self) -> str:
        """獲取測試模式的配置來源"""
        from configs.config_loader import load_config
        global_config = load_config()
        
        sources = []
        
        if self.config.get("test_mode", False):
            sources.append("模組配置")
        
        if os.getenv("UEP_TEST_MODE", "false").lower() == "true":
            sources.append("環境變數")
        
        if global_config.get("debug", {}).get("enabled", False):
            sources.append("全域除錯")
        
        if not sources:
            sources.append("預設值")
        
        return " + ".join(sources)
    
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
            
            # 3. 生成內容雜湊值
            content_hash = hashlib.md5(content.encode()).hexdigest()
            
            # 4. 智能決策：是否建立顯性快取
            if not self.should_create_explicit_cache(content, content_hash):
                debug_log(2, f"[CacheManager] 使用隱性快取: {name}")
                return None  # 返回 None 表示使用隱性快取
            
            # 5. 檢查最小大小（雙重檢查）
            estimated_tokens = max(1, len(content) // 4)
            if estimated_tokens < self.min_cache_size:
                debug_log(1, f"[CacheManager] 內容未達 Vertex AI 最小要求: {name} ({estimated_tokens} tokens)")
                return None
            
            # 6. 創建顯性快取
            ttl = ttl_seconds or self.default_ttl.get(cache_type, 3600)
            cached_content = self._create_cached_content(content, name, ttl)
            
            if cached_content:
                # 7. 記錄快取信息
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
                
                info_log(f"[CacheManager] 顯性快取建立成功: {name}, TTL: {ttl}s, Tokens: ~{estimated_tokens}")
                return cached_content.name
            
            return None
            
        except Exception as e:
            error_log(f"[CacheManager] 快取操作失敗 {name}: {e}")
            self.stats.gemini_miss_count += 1
            return None
    
    def get_cached_content_for_generation(self, cache_names: List[str]) -> Optional[str]:
        """
        獲取用於生成請求的快取內容ID
        
        重要：
        - 返回 cached_content_id 時不能同時送 system_instructions 或 tools
        - 返回 None 表示使用隱性快取或無快取，可以正常送 system_instructions
        
        Args:
            cache_names: 快取名稱列表，按優先級排序
            
        Returns:
            cached_content_id 或 None（None = 使用隱性快取）
        """
        try:
            current_time = time.time()
            
            for cache_name in cache_names:
                if cache_name in self.caches:
                    cache_info = self.caches[cache_name]
                    
                    # 檢查是否過期
                    if current_time - cache_info.created_at < cache_info.ttl_seconds:
                        # 更新使用統計
                        cache_info.usage_count += 1
                        cache_info.last_used = current_time
                        self.stats.gemini_hit_count += 1
                        
                        debug_log(2, f"[CacheManager] 使用顯性快取進行生成: {cache_name}")
                        return cache_info.cached_content_id
                    else:
                        debug_log(2, f"[CacheManager] 顯性快取已過期: {cache_name}")
            
            # 沒有可用的顯性快取 - 使用隱性快取
            debug_log(2, f"[CacheManager] 無顯性快取，使用隱性快取: {cache_names}")
            return None
            
        except Exception as e:
            error_log(f"[CacheManager] 獲取生成快取失敗: {e}")
            return None
    
    def _create_cached_content(self, content: str, display_name: str, ttl_seconds: int):
        """創建Gemini顯性快取 (使用 genai.Client)"""
        try:
            from google.genai import types
            
            # 使用 genai.Client 創建快取
            cache = self.client.caches.create(
                model=self.model_name,
                config=types.CreateCachedContentConfig(
                    display_name=display_name,
                    system_instruction=content,  # 主要快取內容
                    contents=[],                 # 保留欄位（未來擴展用）
                    ttl=f"{ttl_seconds}s"
                )
            )
            
            debug_log(2, f"[CacheManager] Gemini 快取建立: {cache.name} (TTL: {ttl_seconds}s)")
            return cache
            
        except Exception as e:
            error_log(f"[CacheManager] Gemini 快取建立失敗: {e}")
            return None
    
    def update_ttl(self, name: str, ttl_seconds: int) -> bool:
        """更新快取TTL"""
        try:
            if name not in self.caches:
                return False
                
            cache_info = self.caches[name]
            
            # 更新 Gemini 快取TTL
            from google.genai import types
            
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
            
            # 刪除 Gemini 快取
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
                # 系統狀態
                "system": {
                    "test_mode": self.test_mode,
                    "debug_mode": self.test_mode,  # 別名，更清楚表達除錯模式
                    "smart_cache_threshold": self.smart_cache_threshold,
                    "usage_frequency_threshold": self.usage_frequency_threshold,
                    "model_name": self.model_name,
                    "config_source": self._get_config_source()
                },
                # Gemini顯性快取統計
                "explicit_cache": {
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
                # 使用頻率追蹤
                "usage_tracking": {
                    "tracked_content_count": len(self.usage_tracker),
                    "high_frequency_content": sum(1 for tracker in self.usage_tracker.values() 
                                                 if tracker["count"] >= self.usage_frequency_threshold)
                },
                # 總體統計
                "overall": {
                    "total_hit_count": total_hits,
                    "total_miss_count": total_misses,
                    "overall_hit_rate": overall_hit_rate,
                    "total_tokens_saved": self.stats.total_tokens_saved,
                    "implicit_cache_usage": "大部分內容使用 Gemini 隱性快取"
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
        """構建persona內容 - 從 PromptManager 獲取真實系統提示詞"""
        try:
            prompt_manager = self._get_prompt_manager()
            
            # 獲取基礎人格設定
            base_personality = prompt_manager.system_instructions.get("base_personality", "")
            
            if not base_personality:
                error_log("[CacheManager] 未找到 base_personality 系統提示詞")
                return self._get_fallback_persona()
            
            # 替換系統數值占位符（如果有的話）
            if "{system_values}" in base_personality:
                base_personality = prompt_manager._replace_system_values_placeholder(base_personality)
            
            debug_log(2, f"[CacheManager] 成功獲取 persona 內容，長度: {len(base_personality)}")
            return base_personality
            
        except Exception as e:
            error_log(f"[CacheManager] 獲取 persona 內容失敗: {e}")
            return self._get_fallback_persona()
    
    def _build_functions_content(self) -> str:
        """構建functions內容 - 從配置文件獲取系統功能規格"""
        try:
            # TODO: 實際從 functions.yaml 或相關配置讀取
            # 這裡應該讀取實際的系統功能定義
            
            fallback_content = """系統功能規格：
- 記憶管理：儲存、檢索、整理對話記憶和使用者資訊
- 狀態管理：更新情緒數值 (Mood/Pride/Helpfulness/Boredom)
- 會話控制：建立、切換、結束不同類型的會話
- 身份管理：處理多重身份和個人化設定
- 系統監控：追蹤模組狀態和效能指標
- 錯誤處理：捕捉和回報系統異常情況"""
            
            debug_log(2, f"[CacheManager] 使用預設 functions 內容，長度: {len(fallback_content)}")
            return fallback_content
            
        except Exception as e:
            error_log(f"[CacheManager] 獲取 functions 內容失敗: {e}")
            return "系統功能規格載入失敗"
    
    def _build_style_policy_content(self) -> str:
        """構建風格策略內容 - 從 PromptManager 獲取樣式規則"""
        try:
            prompt_manager = self._get_prompt_manager()
            
            # 獲取對話模式和工作模式的指示
            chat_mode = prompt_manager.system_instructions.get("chat_mode", "")
            work_mode = prompt_manager.system_instructions.get("work_mode", "")
            
            style_parts = []
            
            if chat_mode:
                style_parts.append(f"對話模式規則：\n{chat_mode}")
            
            if work_mode:
                style_parts.append(f"工作模式規則：\n{work_mode}")
            
            # 添加狀態數值影響規則
            style_parts.append("""
狀態數值影響規則：
1. Mood (-1到+1)：影響語氣活潑度和情緒表達
2. Pride (0到+1)：影響回應的自信度和積極性
3. Helpfulness (0到+1)：影響協助意願和主動性
4. Boredom (0到+1)：影響回應的豐富度和創意性

JSON 輸出格式：嚴格遵循指定的 schema
安全規則：不提供有害、不當或危險的內容""")
            
            content = "\n\n".join(style_parts)
            debug_log(2, f"[CacheManager] 成功獲取 style policy 內容，長度: {len(content)}")
            return content
            
        except Exception as e:
            error_log(f"[CacheManager] 獲取 style policy 內容失敗: {e}")
            return self._get_fallback_style_policy()
    
    def _get_fallback_persona(self) -> str:
        """備用的 persona 內容"""
        return """你是 U.E.P (Unified Experience Partner)，一個智能助理系統。
你的特點：友善、專業、樂於助人，具有學習和記憶能力。
你會根據系統狀態調整回應風格和行為模式，提供個人化的協助。"""
    
    def _get_fallback_style_policy(self) -> str:
        """備用的風格策略內容"""
        return """基本回應規則：
1. 使用 Traditional Chinese (zh-TW)
2. 保持友善和專業的語調
3. 提供清晰、有用的回應
4. 遵循 JSON 格式要求"""

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
    
    def _get_prompt_manager(self):
        """獲取 PromptManager 實例"""
        try:
            # 嘗試從註冊表獲取
            from core.registry import get_module
            llm_module = get_module('llm_module')
            
            if llm_module and hasattr(llm_module, 'prompt_manager'):
                debug_log(3, "[CacheManager] 從註冊表獲取 PromptManager")
                return llm_module.prompt_manager
            else:
                # 直接創建實例（備用方案）
                from .prompt_manager import PromptManager
                debug_log(2, "[CacheManager] 創建新的 PromptManager 實例")
                return PromptManager()
                
        except Exception as e:
            error_log(f"[CacheManager] 無法獲取 PromptManager: {e}")
            # 返回 None，調用方需要處理此情況
            return None


# 全局快取管理器實例
cache_manager = CacheManager()