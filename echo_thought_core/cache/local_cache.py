"""
Cache Manager — 基於 Gemini 2.5 Flash-Lite 的顯性/隱性快取管理

從 U.E.P Core 的 cache_manager.py 搬遷並解耦：
- 移除所有 U.E.P 全域 config_loader、debug_helper、registry 依賴
- 改用標準 logging；config 由外部傳入
- PromptManager 依賴改為外部注入的 content_builders
- 移除全域單例，生命週期由外部管理

實作策略：
1. 顯性快取：長期穩定內容（persona、functions、style_policy）
2. 隱性快取：短期重複內容的自動快取
3. CHAT/WORK 模式的不同 TTL 策略
4. 成本優化和命中率統計
"""

import time
import os
import hashlib
import logging
from typing import Dict, Any, Optional, List, Callable, TYPE_CHECKING
from dataclasses import dataclass, field
from enum import Enum

# Gemini / google-genai 在需要時才動態導入，避免硬依賴
# from google import genai
# from google.genai import types

if TYPE_CHECKING:
    # LLMOutput 在本模組中僅用於型別標注
    # 若 echo_thought_core 日後有對應 schema，請在此替換導入路徑
    # from echo_thought_core.schemas.session import LLMOutput
    pass

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────
# 資料類型定義
# ─────────────────────────────────────────

class CacheType(Enum):
    """快取類型"""
    PERSONA = "persona"               # 系統人格/身份 — 預設 30 分鐘 TTL
    FUNCTIONS = "functions"           # 函數規格 — 預設 30 分鐘 TTL
    STYLE_POLICY = "style_policy"     # 風格策略 — 預設 30 分鐘 TTL
    SESSION_ANCHOR = "session_anchor" # 會話錨點 — 預設 15 分鐘 TTL
    TASK_SPEC = "task_spec"           # 任務規格 — 預設 10 分鐘 TTL


@dataclass
class CacheInfo:
    """單一快取項目的元資料"""
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
        """檢查是否已超過 TTL"""
        return time.time() - self.timestamp > ttl_seconds


@dataclass
class CacheStats:
    """快取統計快照"""
    # Gemini 顯性快取統計
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


# ─────────────────────────────────────────
# 主類別
# ─────────────────────────────────────────

class CacheManager:
    """
    Context Caching 管理器 — Gemini 2.5 Flash-Lite 專用

    功能：
    1. 顯性快取管理（系統提示、函數規格、風格政策）
    2. TTL 策略（CHAT 長期，WORK 短期）
    3. 成本追蹤和統計
    4. 自動快取更新和版本管理
    5. 本地 LRU 快取（輕量回應快取）

    設計原則（與 U.E.P Core 解耦後）：
    - config 由外部傳入，不再讀取全域 config_loader
    - debug/test 模式從 config dict 判斷
    - PromptManager 等外部依賴改為 content_builders 注入
    - 全域單例移除，實例生命週期由外部管理
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None, client=None):
        """
        初始化 CacheManager

        Args:
            config: 快取配置 dict，包含以下可選鍵：
                - model_name (str): Gemini 模型名稱，預設 "gemini-2.5-flash-lite"
                - max_local_entries (int): 本地快取最大條目數，預設 100
                - local_ttl_seconds (int): 本地快取 TTL，預設 1800 秒
                - min_cache_size (int): 顯性快取最小 token 數，預設 2048
                - smart_cache_threshold (int): 建立顯性快取的 token 下限，預設 1000
                - usage_frequency_threshold (int): 建立顯性快取的頻率下限（次/小時），預設 10
                - test_mode (bool): 禁用顯性快取（測試/除錯用），預設 False
                - debug (dict): 含 enabled/debug_level 的全域除錯設定
            client: 外部注入的 Gemini/Vertex AI client（可選）。
                    若提供，直接使用此 client 而不自建連線。
                    可與 GeminiBackend 共享同一個 client，避免重複建立。
        """
        # 從外部接收 config，不再讀取全域 config_loader
        self.config: Dict[str, Any] = config or {}

        # ── Vertex AI 客戶端初始化 ──
        if client is not None:
            # 使用外部注入的 client（與 GeminiBackend 共享）
            self.client = client
            logger.info("[CacheManager] 使用外部注入的 Gemini client")
        else:
            # 自建 Vertex AI client（原有邏輯）
            project_id = os.getenv("GCP_PROJECT_ID")
            location = os.getenv("GCP_LOCATION")

            if not project_id:
                logger.error("[CacheManager] GCP_PROJECT_ID 環境變數未設定，顯性快取功能將受限")
                self.client = None
            else:
                try:
                    from google import genai  # type: ignore
                    self.client = genai.Client(
                        vertexai=True,
                        project=project_id,
                        location=location,
                    )
                    logger.info(
                        "[CacheManager] Vertex AI 客戶端初始化成功 (專案: %s, 區域: %s)",
                        project_id, location,
                    )
                except Exception as exc:
                    logger.error("[CacheManager] Vertex AI 客戶端初始化失敗: %s", exc)
                    self.client = None

        # 使用支援快取的模型
        self.model_name: str = self.config.get("model_name", "gemini-2.5-flash-lite")

        # ── Gemini 顯性快取管理 ──
        self.caches: Dict[str, CacheInfo] = {}
        self.stats = CacheStats()

        # ── 本地快取管理 ──
        self.local_cache: Dict[str, LocalCacheEntry] = {}
        self.max_local_entries: int = self.config.get("max_local_entries", 100)
        self.local_ttl_seconds: int = self.config.get("local_ttl_seconds", 1800)  # 30 分鐘

        # ── 各類型預設 TTL（秒）──
        self.default_ttl: Dict[CacheType, int] = {
            CacheType.PERSONA:        30 * 60,  # 30 分鐘（頻繁使用的基礎人格）
            CacheType.FUNCTIONS:      30 * 60,  # 30 分鐘（系統功能規格）
            CacheType.STYLE_POLICY:   30 * 60,  # 30 分鐘（風格策略）
            CacheType.SESSION_ANCHOR: 15 * 60,  # 15 分鐘（會話錨點）
            CacheType.TASK_SPEC:      10 * 60,  # 10 分鐘（任務規格）
        }

        # Vertex AI 要求最少 2048 tokens
        self.min_cache_size: int = self.config.get("min_cache_size", 2048)

        # ── 智能快取策略參數 ──
        self.smart_cache_threshold: int = self.config.get("smart_cache_threshold", 1000)
        self.usage_frequency_threshold: int = self.config.get("usage_frequency_threshold", 10)

        # ── 測試/除錯模式：從傳入的 config 判斷，不讀全域設定 ──
        debug_cfg = self.config.get("debug", {})
        self.test_mode: bool = (
            self.config.get("test_mode", False)
            or os.getenv("UEP_TEST_MODE", "false").lower() == "true"
            or debug_cfg.get("enabled", False)
        )

        if self.test_mode:
            debug_level = debug_cfg.get("debug_level", 1)
            logger.info(
                "[CacheManager] 測試/除錯模式：禁用顯性快取功能（除錯等級: %d）",
                debug_level,
            )

        # ── 使用頻率追蹤 ──
        # 結構：{content_hash: {"count": int, "last_hour": int, "timestamps": [float]}}
        self.usage_tracker: Dict[str, Dict[str, Any]] = {}

        logger.debug("[CacheManager] 初始化完成")

    # ─────────────────────────────────────────
    # 智能快取決策
    # ─────────────────────────────────────────

    def should_create_explicit_cache(self, content: str, content_hash: str) -> bool:
        """
        智能決策：是否應該建立顯性快取

        策略：
        - 測試/除錯模式 → 禁用顯性快取
        - < smart_cache_threshold tokens → 使用隱性快取（Gemini 2.5 自動處理）
        - >= smart_cache_threshold tokens 且高頻（>= usage_frequency_threshold 次/小時）
          → 建立顯性快取
        """
        if self.test_mode:
            logger.debug("[CacheManager] 測試/除錯模式：跳過顯性快取")
            return False

        estimated_tokens = max(1, len(content) // 4)

        if estimated_tokens < self.smart_cache_threshold:
            logger.debug(
                "[CacheManager] 內容較小 (%d tokens)，使用隱性快取", estimated_tokens
            )
            return False

        usage_freq = self._get_usage_frequency(content_hash)
        if usage_freq < self.usage_frequency_threshold:
            logger.debug(
                "[CacheManager] 使用頻率低 (%d/小時)，使用隱性快取", usage_freq
            )
            return False

        logger.debug(
            "[CacheManager] 高頻內容 (%d/小時, %d tokens)，建議顯性快取",
            usage_freq, estimated_tokens,
        )
        return True

    def _get_usage_frequency(self, content_hash: str) -> int:
        """獲取內容在過去一小時內的使用次數"""
        current_time = time.time()
        current_hour = int(current_time // 3600)

        if content_hash not in self.usage_tracker:
            self.usage_tracker[content_hash] = {
                "count": 0,
                "last_hour": current_hour,
                "timestamps": [],
            }

        tracker = self.usage_tracker[content_hash]

        # 清理超過一小時的舊記錄
        one_hour_ago = current_time - 3600
        tracker["timestamps"] = [ts for ts in tracker["timestamps"] if ts > one_hour_ago]

        # 記錄本次使用
        tracker["timestamps"].append(current_time)
        tracker["count"] = len(tracker["timestamps"])
        tracker["last_hour"] = current_hour

        return tracker["count"]

    def _get_config_source(self) -> str:
        """回傳目前 test_mode 的啟用來源（供統計報告用）"""
        sources = []

        if self.config.get("test_mode", False):
            sources.append("模組配置")

        if os.getenv("UEP_TEST_MODE", "false").lower() == "true":
            sources.append("環境變數")

        if self.config.get("debug", {}).get("enabled", False):
            sources.append("全域除錯")

        return " + ".join(sources) if sources else "預設值"

    # ─────────────────────────────────────────
    # Gemini 顯性快取管理
    # ─────────────────────────────────────────

    def get_or_create_cache(
        self,
        name: str,
        cache_type: CacheType,
        content_builder: Callable[[], str],
        ttl_seconds: Optional[int] = None,
        force_rebuild: bool = False,
    ) -> Optional[str]:
        """
        獲取或建立顯性快取

        Args:
            name: 快取名稱（建議格式 "uep:{type}:{version}"）
            cache_type: 快取類型
            content_builder: 無參數 callable，回傳要快取的字串內容
            ttl_seconds: 自訂 TTL，None 則使用該類型的預設值
            force_rebuild: 強制重建（忽略現有快取）

        Returns:
            cached_content_id（顯性快取 ID）或 None（使用隱性快取）
        """
        try:
            current_time = time.time()

            # 1. 檢查現有快取是否仍有效
            if not force_rebuild and name in self.caches:
                cache_info = self.caches[name]

                if current_time - cache_info.created_at < cache_info.ttl_seconds:
                    cache_info.usage_count += 1
                    cache_info.last_used = current_time
                    self.stats.gemini_hit_count += 1
                    logger.debug("[CacheManager] 快取命中: %s", name)
                    return cache_info.cached_content_id
                else:
                    # TTL 過期，刪除舊快取
                    logger.debug("[CacheManager] 快取過期，刪除: %s", name)
                    self.delete_cache(name)

            # 2. 生成新內容
            content = content_builder()
            if not content:
                logger.error("[CacheManager] 快取內容生成失敗: %s", name)
                return None

            # 3. 計算內容雜湊值
            content_hash = hashlib.md5(content.encode()).hexdigest()

            # 4. 智能決策：是否建立顯性快取
            if not self.should_create_explicit_cache(content, content_hash):
                logger.debug("[CacheManager] 使用隱性快取: %s", name)
                return None  # None 表示由 Gemini 隱性快取處理

            # 5. 確認符合 Vertex AI 最小 token 要求
            estimated_tokens = max(1, len(content) // 4)
            if estimated_tokens < self.min_cache_size:
                logger.debug(
                    "[CacheManager] 內容未達 Vertex AI 最小要求: %s (%d tokens)",
                    name, estimated_tokens,
                )
                return None

            # 6. 建立顯性快取
            ttl = ttl_seconds or self.default_ttl.get(cache_type, 3600)
            cached_content = self._create_cached_content(content, name, ttl)

            if cached_content:
                cache_info = CacheInfo(
                    name=name,
                    cache_type=cache_type,
                    model=self.model_name,
                    created_at=current_time,
                    ttl_seconds=ttl,
                    content_hash=content_hash,
                    cached_content_id=cached_content.name,
                )

                self.caches[name] = cache_info
                self.stats.total_caches += 1
                self.stats.active_caches += 1
                self.stats.total_tokens_cached += estimated_tokens

                logger.info(
                    "[CacheManager] 顯性快取建立成功: %s, TTL: %ds, Tokens: ~%d",
                    name, ttl, estimated_tokens,
                )
                return cached_content.name

            return None

        except Exception as exc:
            logger.error("[CacheManager] 快取操作失敗 %s: %s", name, exc)
            self.stats.gemini_miss_count += 1
            return None

    def get_cached_content_for_generation(self, cache_names: List[str]) -> Optional[str]:
        """
        從已知快取名稱清單中，取出可用於生成請求的 cached_content_id

        重要：
        - 回傳 cached_content_id 時，不能同時傳送 system_instructions 或 tools
        - 回傳 None 表示使用隱性快取或無快取，可正常傳送 system_instructions

        Args:
            cache_names: 快取名稱清單，按優先級排序

        Returns:
            cached_content_id 或 None
        """
        try:
            current_time = time.time()

            for cache_name in cache_names:
                if cache_name not in self.caches:
                    continue

                cache_info = self.caches[cache_name]

                if current_time - cache_info.created_at < cache_info.ttl_seconds:
                    cache_info.usage_count += 1
                    cache_info.last_used = current_time
                    self.stats.gemini_hit_count += 1
                    logger.debug("[CacheManager] 使用顯性快取進行生成: %s", cache_name)
                    return cache_info.cached_content_id
                else:
                    logger.debug("[CacheManager] 顯性快取已過期: %s", cache_name)

            # 無可用顯性快取，交由 Gemini 隱性快取處理
            logger.debug("[CacheManager] 無顯性快取，使用隱性快取: %s", cache_names)
            return None

        except Exception as exc:
            logger.error("[CacheManager] 獲取生成快取失敗: %s", exc)
            return None

    def _create_cached_content(self, content: str, display_name: str, ttl_seconds: int):
        """在 Gemini（Vertex AI）上建立顯性快取，回傳 cache 物件或 None"""
        if not self.client:
            logger.warning("[CacheManager] client 未初始化，無法建立顯性快取")
            return None

        try:
            from google.genai import types  # type: ignore

            cache = self.client.caches.create(  # type: ignore
                model=self.model_name,
                config=types.CreateCachedContentConfig(
                    display_name=display_name,
                    system_instruction=content,  # 主要快取內容
                    contents=[],                 # 保留欄位（未來擴展用）
                    ttl=f"{ttl_seconds}s",
                ),
            )

            logger.debug(
                "[CacheManager] Gemini 快取建立: %s (TTL: %ds)", cache.name, ttl_seconds
            )
            return cache

        except Exception as exc:
            logger.error("[CacheManager] Gemini 快取建立失敗: %s", exc)
            return None

    def update_ttl(self, name: str, ttl_seconds: int) -> bool:
        """更新指定快取的 TTL"""
        if not self.client:
            logger.warning("[CacheManager] client 未初始化，無法更新 TTL")
            return False

        try:
            if name not in self.caches:
                return False

            cache_info = self.caches[name]

            from google.genai import types  # type: ignore

            self.client.caches.update(  # type: ignore
                name=cache_info.cached_content_id,  # type: ignore
                config=types.UpdateCachedContentConfig(ttl=f"{ttl_seconds}s"),
            )

            cache_info.ttl_seconds = ttl_seconds
            logger.debug("[CacheManager] TTL 更新成功: %s -> %ds", name, ttl_seconds)
            return True

        except Exception as exc:
            logger.error("[CacheManager] TTL 更新失敗 %s: %s", name, exc)
            return False

    def delete_cache(self, name: str) -> bool:
        """刪除顯性快取（同時從 Gemini 和本地記錄移除）"""
        try:
            if name not in self.caches:
                return False

            cache_info = self.caches[name]

            if cache_info.cached_content_id and self.client:
                self.client.caches.delete(name=cache_info.cached_content_id)  # type: ignore

            del self.caches[name]
            self.stats.active_caches -= 1

            logger.debug("[CacheManager] 快取刪除成功: %s", name)
            return True

        except Exception as exc:
            logger.error("[CacheManager] 快取刪除失敗 %s: %s", name, exc)
            return False

    def cleanup_expired_caches(self) -> int:
        """清理所有已過期的顯性快取，回傳清理數量"""
        current_time = time.time()

        expired_names = [
            name
            for name, info in self.caches.items()
            if current_time - info.created_at >= info.ttl_seconds
        ]

        expired_count = sum(1 for name in expired_names if self.delete_cache(name))

        if expired_count > 0:
            logger.info("[CacheManager] 清理過期快取: %d 個", expired_count)

        return expired_count

    # ─────────────────────────────────────────
    # 預載系統快取（content builders 由外部注入）
    # ─────────────────────────────────────────

    def preload_system_caches(
        self,
        content_builders: Optional[Dict[str, Callable[[], str]]] = None,
    ) -> Dict[str, str]:
        """
        預載系統快取（persona、functions、style_policy）

        與 U.E.P Core 版本的差異：
        原版從 PromptManager / registry 取得 content builder；
        此版改為接受外部傳入的 content_builders dict，完全解耦。

        Args:
            content_builders: {快取類型名稱 -> callable}，例如：
                {
                    "persona":      lambda: "...",
                    "functions":    lambda: "...",
                    "style_policy": lambda: "...",
                }
                未提供的項目將跳過預載。

        Returns:
            {快取類型名稱 -> cached_content_id} 的 dict（僅包含成功建立的項目）
        """
        builders = content_builders or {}
        preloaded: Dict[str, str] = {}

        cache_specs = [
            ("persona",      "uep:persona:v1",      CacheType.PERSONA),
            ("functions",    "uep:functions:v1",     CacheType.FUNCTIONS),
            ("style_policy", "uep:style_policy:v1",  CacheType.STYLE_POLICY),
        ]

        try:
            for key, cache_name, cache_type in cache_specs:
                builder = builders.get(key)
                if builder is None:
                    logger.debug("[CacheManager] 未提供 %s 的 content_builder，跳過", key)
                    continue

                result = self.get_or_create_cache(
                    name=cache_name,
                    cache_type=cache_type,
                    content_builder=builder,
                )
                if result:
                    preloaded[key] = result

            logger.info("[CacheManager] 系統快取預載完成: %d 個", len(preloaded))
            return preloaded

        except Exception as exc:
            logger.error("[CacheManager] 系統快取預載失敗: %s", exc)
            return preloaded

    # ─────────────────────────────────────────
    # 本地 LRU 快取
    # ─────────────────────────────────────────

    def generate_cache_key(self, cache_type: str, content: str, **kwargs) -> str:
        """生成本地快取鍵（MD5 雜湊）"""
        key_parts = [cache_type, content]
        for key, value in sorted(kwargs.items()):
            key_parts.append(f"{key}:{value}")
        return hashlib.md5("|".join(key_parts).encode("utf-8")).hexdigest()

    def get_local_cache(self, cache_key: str) -> Optional[Any]:
        """從本地快取取得內容；過期或不存在時回傳 None"""
        if cache_key not in self.local_cache:
            self.stats.local_miss_count += 1
            return None

        entry = self.local_cache[cache_key]

        if entry.is_expired(self.local_ttl_seconds):
            del self.local_cache[cache_key]
            self.stats.local_miss_count += 1
            logger.debug("[CacheManager] 本地快取過期: %s...", cache_key[:8])
            return None

        entry.hit_count += 1
        entry.last_access = time.time()
        self.stats.local_hit_count += 1

        logger.debug(
            "[CacheManager] 本地快取命中: %s... (命中次數: %d)",
            cache_key[:8], entry.hit_count,
        )
        return entry.content

    def put_local_cache(
        self,
        cache_key: str,
        content: Any,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """存入本地快取；容量滿時以 LRU 策略淘汰舊條目"""
        if len(self.local_cache) >= self.max_local_entries:
            self._evict_lru_local_cache()

        self.local_cache[cache_key] = LocalCacheEntry(
            content=content,
            timestamp=time.time(),
            metadata=metadata or {},
        )
        self.stats.local_total_entries += 1

        logger.debug("[CacheManager] 本地快取存入: %s...", cache_key[:8])

    def _evict_lru_local_cache(self) -> None:
        """淘汰 last_access 最舊的本地快取條目"""
        if not self.local_cache:
            return

        oldest_key = min(
            self.local_cache.keys(),
            key=lambda k: self.local_cache[k].last_access,
        )
        del self.local_cache[oldest_key]
        self.stats.local_evictions += 1

        logger.debug("[CacheManager] 淘汰本地快取條目: %s...", oldest_key[:8])

    def get_cached_response(self, text: str, mode: str = "chat") -> Optional[Any]:
        """
        獲取快取的 LLM 回應

        注意：回應型別在 U.E.P Core 為 LLMOutput；
        此模組已解耦，回傳 Any，由呼叫端自行確認型別。
        """
        cache_key = self.generate_cache_key("llm_response", text, mode=mode)
        return self.get_local_cache(cache_key)

    def cache_response(self, cache_key: str, response: Any) -> None:
        """快取 LLM 回應"""
        self.put_local_cache(cache_key, response, {"type": "llm_response"})

    def cache_identity_context(self, identity_data: Dict[str, Any]) -> str:
        """快取身份上下文；已快取時直接回傳，否則格式化後存入"""
        identity_str = str(identity_data)
        cache_key = self.generate_cache_key("identity", identity_str)

        cached = self.get_local_cache(cache_key)
        if cached:
            return cached  # type: ignore[return-value]

        formatted = self._format_identity_context(identity_data)
        self.put_local_cache(
            cache_key,
            formatted,
            {"type": "identity", "identity_id": identity_data.get("current_identity")},
        )
        return formatted

    def cache_memory_context(
        self,
        memory_content: str,
        identity_id: Optional[str] = None,
    ) -> str:
        """快取記憶上下文；已快取時直接回傳，否則格式化後存入"""
        cache_key = self.generate_cache_key(
            "memory", memory_content, identity_id=identity_id or ""
        )

        cached = self.get_local_cache(cache_key)
        if cached:
            return cached  # type: ignore[return-value]

        formatted = f"相關記憶：\n{memory_content}"
        self.put_local_cache(
            cache_key,
            formatted,
            {"type": "memory", "identity_id": identity_id},
        )
        return formatted

    def _format_identity_context(self, identity_data: Dict[str, Any]) -> str:
        """將身份資料 dict 格式化為易讀字串"""
        try:
            current_identity = identity_data.get("current_identity")
            traits = identity_data.get("identity_traits", {})
            preferences = identity_data.get("identity_preferences", {})

            parts = []

            if current_identity:
                parts.append(f"當前身份：{current_identity}")

            if traits:
                trait_strs = [f"{k}: {v}" for k, v in traits.items()]
                parts.append(f"身份特徵：{', '.join(trait_strs)}")

            if preferences:
                pref_strs = [f"{k}: {v}" for k, v in preferences.items()]
                parts.append(f"偏好設定：{', '.join(pref_strs)}")

            return "\n".join(parts) if parts else "無特定身份上下文"

        except Exception as exc:
            logger.error("[CacheManager] 格式化身份上下文失敗: %s", exc)
            return "身份上下文處理失敗"

    def cleanup_expired_local_cache(self) -> int:
        """清理所有過期的本地快取條目，回傳清理數量"""
        expired_keys = [
            key
            for key, entry in self.local_cache.items()
            if entry.is_expired(self.local_ttl_seconds)
        ]

        for key in expired_keys:
            del self.local_cache[key]

        if expired_keys:
            logger.debug(
                "[CacheManager] 清理本地快取: %d 個過期條目", len(expired_keys)
            )

        return len(expired_keys)

    # ─────────────────────────────────────────
    # 快取統計
    # ─────────────────────────────────────────

    def get_cache_statistics(self, name: Optional[str] = None) -> Dict[str, Any]:
        """
        獲取快取統計

        Args:
            name: 指定快取名稱時回傳單一快取資訊；None 時回傳全域統計

        Returns:
            統計資訊 dict
        """
        if name and name in self.caches:
            info = self.caches[name]
            return {
                "name": info.name,
                "type": info.cache_type.value,
                "created_at": info.created_at,
                "ttl_seconds": info.ttl_seconds,
                "usage_count": info.usage_count,
                "last_used": info.last_used,
            }

        # ── 全域統計 ──
        total_hits = self.stats.gemini_hit_count + self.stats.local_hit_count
        total_misses = self.stats.gemini_miss_count + self.stats.local_miss_count
        overall_hit_rate = (
            total_hits / (total_hits + total_misses)
            if (total_hits + total_misses) > 0
            else 0.0
        )

        gemini_total = self.stats.gemini_hit_count + self.stats.gemini_miss_count
        local_total = self.stats.local_hit_count + self.stats.local_miss_count

        return {
            # 系統狀態
            "system": {
                "test_mode": self.test_mode,
                "debug_mode": self.test_mode,  # 別名，與原版保持相容
                "smart_cache_threshold": self.smart_cache_threshold,
                "usage_frequency_threshold": self.usage_frequency_threshold,
                "model_name": self.model_name,
                "config_source": self._get_config_source(),
            },
            # Gemini 顯性快取統計
            "explicit_cache": {
                "total_caches": self.stats.total_caches,
                "active_caches": self.stats.active_caches,
                "hit_count": self.stats.gemini_hit_count,
                "miss_count": self.stats.gemini_miss_count,
                "hit_rate": (
                    self.stats.gemini_hit_count / gemini_total
                    if gemini_total > 0
                    else 0.0
                ),
                "total_tokens_cached": self.stats.total_tokens_cached,
                "cache_names": list(self.caches.keys()),
            },
            # 本地快取統計
            "local_cache": {
                "active_entries": len(self.local_cache),
                "max_entries": self.max_local_entries,
                "hit_count": self.stats.local_hit_count,
                "miss_count": self.stats.local_miss_count,
                "hit_rate": (
                    self.stats.local_hit_count / local_total
                    if local_total > 0
                    else 0.0
                ),
                "evictions": self.stats.local_evictions,
                "ttl_seconds": self.local_ttl_seconds,
            },
            # 使用頻率追蹤
            "usage_tracking": {
                "tracked_content_count": len(self.usage_tracker),
                "high_frequency_content": sum(
                    1
                    for t in self.usage_tracker.values()
                    if t["count"] >= self.usage_frequency_threshold
                ),
            },
            # 總體統計
            "overall": {
                "total_hit_count": total_hits,
                "total_miss_count": total_misses,
                "overall_hit_rate": overall_hit_rate,
                "total_tokens_saved": self.stats.total_tokens_saved,
                "implicit_cache_usage": "大部分內容使用 Gemini 隱性快取",
            },
        }
