"""
配置管理器 - 載入環境變數與配置

統一處理 .env 文件與配置參數。
"""

import os
from pathlib import Path
from typing import Any

try:
    from dotenv import load_dotenv
    DOTENV_AVAILABLE = True
except ImportError:
    DOTENV_AVAILABLE = False


class Config:
    """配置管理器"""

    def __init__(self):
        # 載入 .env 文件
        if DOTENV_AVAILABLE:
            env_file = Path(__file__).parent.parent / ".env"
            if env_file.exists():
                load_dotenv(env_file)

        # === LLM Provider 配置 ===
        self.gcp_project_id = os.getenv("GCP_PROJECT_ID", "")
        self.gcp_location = os.getenv("GCP_LOCATION", "asia-east1")
        self.google_api_key = os.getenv("GOOGLE_API_KEY", "")
        self.anthropic_api_key = os.getenv("ANTHROPIC_API_KEY", "")
        self.system_prompt = os.getenv(
            "SYSTEM_PROMPT",
            "你是一個有記憶的智能助手。請根據提供的記憶脈絡，提供更個性化的回應。"
        )

        # === 嵌入模型配置 ===
        self.embedding_model = os.getenv("EMBEDDING_MODEL", "BAAI/bge-small-zh-v1.5")
        self.embedding_device = os.getenv("EMBEDDING_DEVICE", "cpu")
        self.embedding_query_prefix = os.getenv(
            "EMBEDDING_QUERY_PREFIX",
            ""
        )
        # Ollama embedding backend (set EMBEDDING_BACKEND=ollama to use)
        self.embedding_backend = os.getenv("EMBEDDING_BACKEND", "sentence-transformers")
        self.ollama_base_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
        # Embedding 逾時（秒）：查詢路徑用短逾時以便快速降級；
        # 寫入路徑容忍較久；warmup 允許冷啟動載入模型。
        self.embedding_query_timeout = float(os.getenv("EMBEDDING_QUERY_TIMEOUT", "2"))
        self.embedding_write_timeout = float(os.getenv("EMBEDDING_WRITE_TIMEOUT", "30"))
        self.embedding_warmup_timeout = float(os.getenv("EMBEDDING_WARMUP_TIMEOUT", "120"))
        # Ollama 模型常駐時間（傳給 /api/embed 的 keep_alive）；預設 30m，
        # 避免對話間隔超過 Ollama 預設 5 分鐘時模型被卸載而冷啟動逾時。
        # 設 -1 表示永久常駐，設 0 表示用完即卸載。
        self.embedding_keep_alive = os.getenv("EMBEDDING_KEEP_ALIVE", "30m")

        # === 記憶系統配置 ===
        self.memory_storage_path = os.getenv("MEMORY_STORAGE_PATH", "./data")
        self.memory_namespace_id = os.getenv("MEMORY_NAMESPACE_ID", "local-default")
        self.memory_max_episodes = int(os.getenv("MEMORY_MAX_EPISODES", "10000"))
        self.memory_max_concepts = int(os.getenv("MEMORY_MAX_CONCEPTS", "5000"))

        # === 檢索參數 ===
        self.retrieval_seed_k = int(os.getenv("RETRIEVAL_SEED_K", "5"))
        self.retrieval_breadth_k = int(os.getenv("RETRIEVAL_BREADTH_K", "12"))
        # bge-m3 的 cosine sim 分布偏高（無關文本也可能 0.25-0.30），
        # 預設閾值需要 0.35 才能有效過濾無關結果。
        self.retrieval_min_similarity = float(
            os.getenv("RETRIEVAL_MIN_SIMILARITY", "0.35")
        )
        self.retrieval_weight_threshold = float(
            os.getenv("RETRIEVAL_WEIGHT_THRESHOLD", "0.225")
        )

        # === Lexical 檢索路（BM25 + RRF 融合）===
        # 向量路之外的第二條 Episode 檢索路；只影響種子排序，不影響存儲決策
        self.retrieval_lexical_enabled = (
            os.getenv("RETRIEVAL_LEXICAL_ENABLED", "false").lower() == "true"
        )
        self.retrieval_rrf_k = int(os.getenv("RETRIEVAL_RRF_K", "60"))
        # 兩路各取 top N 後融合
        self.retrieval_lexical_top_n = int(os.getenv("RETRIEVAL_LEXICAL_TOP_N", "20"))
        # lexical 路自己的最低 BM25 分數（向量門檻 min_similarity 只套用在向量路）
        self.retrieval_lexical_min_score = float(
            os.getenv("RETRIEVAL_LEXICAL_MIN_SCORE", "1.0")
        )
        # 相對於 lexical 最高分的比例下限：濾掉只靠常見詞彙擦邊命中的長尾
        self.retrieval_lexical_rel_min = float(
            os.getenv("RETRIEVAL_LEXICAL_REL_MIN", "0.5")
        )
        # df 占文件數比例超過此值的詞視為無鑑別力而剔除（語料統計；文件數 < 30 不套用）
        self.retrieval_lexical_max_df = float(
            os.getenv("RETRIEVAL_LEXICAL_MAX_DF", "0.2")
        )
        # 文件須命中「有效強詞」的最低比例
        self.retrieval_lexical_min_coverage = float(
            os.getenv("RETRIEVAL_LEXICAL_MIN_COVERAGE", "0.5")
        )
        # lexical-only 命中（向量路門檻外）的最低 cosine；向量不可用時不適用
        self.retrieval_lexical_min_cosine = float(
            os.getenv("RETRIEVAL_LEXICAL_MIN_COSINE", "0.25")
        )

        # === 邊緣權重參數 ===
        self.edge_weight_alpha = float(os.getenv("EDGE_WEIGHT_ALPHA", "0.3"))
        self.edge_weight_beta = float(os.getenv("EDGE_WEIGHT_BETA", "0.4"))
        self.edge_weight_gamma = float(os.getenv("EDGE_WEIGHT_GAMMA", "0.3"))
        self.edge_recency_lambda = float(os.getenv("EDGE_RECENCY_LAMBDA", "0.01"))

        # === 圖譜修剪 ===
        self.pruning_min_weight = float(os.getenv("PRUNING_MIN_WEIGHT", "0.05"))
        self.pruning_cold_storage = float(os.getenv("PRUNING_COLD_STORAGE", "0.15"))

        # === Dream Engine ===
        self.dream_enabled = os.getenv("DREAM_ENABLED", "true").lower() == "true"  # 默認啟用
        self.dream_idle_minutes = int(os.getenv("DREAM_IDLE_MINUTES", "30"))
        self.dream_cron = os.getenv("DREAM_CRON", "0 3 * * *")
        self.dream_enable_idle = os.getenv("DREAM_ENABLE_IDLE", "true").lower() == "true"
        self.dream_enable_cron = os.getenv("DREAM_ENABLE_CRON", "true").lower() == "true"
        self.dream_lease_ttl_seconds = float(os.getenv("DREAM_LEASE_TTL_SECONDS", "3600"))
        self.dream_cron_window_seconds = float(os.getenv("DREAM_CRON_WINDOW_SECONDS", "900"))
        self.dream_replay_da_threshold = float(
            os.getenv("DREAM_REPLAY_DA_THRESHOLD", "0.6")
        )

        # === Patch Gate held 重審 ===
        # held patch 每次重審失敗（LLM 仍不可用 / 仍無法判定）計一次，達上限即標記為最終拒絕
        self.concept_held_max_retries = int(os.getenv("CONCEPT_HELD_MAX_RETRIES", "3"))
        # held 超過此天數仍未解決視為逾期，標記為最終拒絕
        self.concept_held_max_age_days = float(os.getenv("CONCEPT_HELD_MAX_AGE_DAYS", "7"))
        # === 寫入冪等索引（store_episode idempotency_key）===
        self.episode_idempotency_max_keys = int(os.getenv("EPISODE_IDEMPOTENCY_MAX_KEYS", "10000"))
        self.episode_idempotency_retention_days = float(
            os.getenv("EPISODE_IDEMPOTENCY_RETENTION_DAYS", "30")
        )

        # 單次觸發（對話 patch / Dream 週期）最多重審幾筆，避免拖慢對話路徑
        self.concept_held_review_batch = int(os.getenv("CONCEPT_HELD_REVIEW_BATCH", "3"))

        # === M4 證據審核 ===
        # Dream 週期單次最多審核幾個概念；單次送審的 evidence unit 上限
        self.evidence_review_batch = int(os.getenv("EVIDENCE_REVIEW_BATCH", "8"))
        self.evidence_review_max_units = int(os.getenv("EVIDENCE_REVIEW_MAX_UNITS", "12"))
        # Dream 週期單次最多重判幾對既有 CONTRADICTS 邊
        self.contradiction_reevaluate_batch = int(os.getenv("CONTRADICTION_REEVALUATE_BATCH", "3"))
        # 審核後 Concept.confidence 的上限（依審核狀態）
        self.review_confidence_cap_supported = float(
            os.getenv("REVIEW_CONFIDENCE_CAP_SUPPORTED", "0.95")
        )
        self.review_confidence_cap_insufficient = float(
            os.getenv("REVIEW_CONFIDENCE_CAP_INSUFFICIENT", "0.45")
        )
        self.review_confidence_cap_contradicted = float(
            os.getenv("REVIEW_CONFIDENCE_CAP_CONTRADICTED", "0.30")
        )
        # confidence 重算時 support_source 的係數
        self.review_source_factor_user = float(os.getenv("REVIEW_SOURCE_FACTOR_USER", "1.0"))
        self.review_source_factor_legacy = float(os.getenv("REVIEW_SOURCE_FACTOR_LEGACY", "0.7"))
        self.review_source_factor_agent = float(os.getenv("REVIEW_SOURCE_FACTOR_AGENT", "0.5"))
        # 檢索：審核狀態因子（relevance 與 support 分開相乘）
        self.review_status_factor_supported = float(
            os.getenv("REVIEW_STATUS_FACTOR_SUPPORTED", "1.0")
        )
        self.review_status_factor_not_checked = float(
            os.getenv("REVIEW_STATUS_FACTOR_NOT_CHECKED", "0.95")
        )
        self.review_status_factor_insufficient = float(
            os.getenv("REVIEW_STATUS_FACTOR_INSUFFICIENT", "0.80")
        )
        self.review_status_factor_contradicted = float(
            os.getenv("REVIEW_STATUS_FACTOR_CONTRADICTED", "0.45")
        )
        # standing = confidence * status_factor * source_rank
        self.standing_source_rank_user = float(os.getenv("STANDING_SOURCE_RANK_USER", "1.0"))
        self.standing_source_rank_legacy = float(os.getenv("STANDING_SOURCE_RANK_LEGACY", "0.8"))
        self.standing_source_rank_agent = float(os.getenv("STANDING_SOURCE_RANK_AGENT", "0.6"))
        # 側向抑制：m = max(FLOOR, 1 - GAMMA * w * (1 - share))
        self.contradicts_gamma = float(os.getenv("CONTRADICTS_GAMMA", "0.6"))
        self.contradicts_floor = float(os.getenv("CONTRADICTS_FLOOR", "0.4"))
        # 橋接展開遇 CONTRADICTS 邊的乘數
        self.bridge_conflict_factor = float(os.getenv("BRIDGE_CONFLICT_FACTOR", "0.6"))

        # === M4 學習程度（§8）===
        # 基礎權重：在場人類人數（含說話者）-> w；dm 一律視為 1 人
        self.learning_weight_n1 = float(os.getenv("LEARNING_WEIGHT_N1", "1.0"))
        self.learning_weight_n2 = float(os.getenv("LEARNING_WEIGHT_N2", "0.6"))
        self.learning_weight_n3 = float(os.getenv("LEARNING_WEIGHT_N3", "0.4"))
        self.learning_weight_n4 = float(os.getenv("LEARNING_WEIGHT_N4", "0.25"))
        self.learning_weight_n5plus = float(os.getenv("LEARNING_WEIGHT_N5PLUS", "0.08"))
        # 對 UEP 說的加權：w' = w + (1 - w) * beta(n)
        self.learning_beta_n1 = float(os.getenv("LEARNING_BETA_N1", "0.0"))
        self.learning_beta_n2 = float(os.getenv("LEARNING_BETA_N2", "0.5"))
        self.learning_beta_n3 = float(os.getenv("LEARNING_BETA_N3", "0.35"))
        self.learning_beta_n4 = float(os.getenv("LEARNING_BETA_N4", "0.2"))
        self.learning_beta_n5plus = float(os.getenv("LEARNING_BETA_N5PLUS", "0.1"))
        # 缺 audience 資訊（舊資料、未接線宿主）的預設權重
        self.learning_weight_unknown = float(os.getenv("LEARNING_WEIGHT_UNKNOWN", "0.6"))
        # noisy-OR 合成的 support_strength 達此門檻才可 supported
        self.support_threshold = float(os.getenv("SUPPORT_THRESHOLD", "0.9"))

        # === 跨使用者分享（M10）===
        # 總開關：關閉時 Gate 拒絕 project（retract／restrict 仍可執行，只收緊永遠安全）
        self.sharing_enabled = os.getenv("SHARING_ENABLED", "true").lower() == "true"
        # 可分享的最小在場人數（含說話者）；裁決：>=2 且非 dm 即整個場域可分享。下限鎖 2
        self.sharing_min_audience_size = max(2, int(os.getenv("SHARING_MIN_AUDIENCE_SIZE", "2")))
        # 隱私意圖掃描時機：post_turn（群組輪次回覆後非同步）| dream（只在 Dream 內）
        _scan = os.getenv("SHARING_INTENT_SCAN", "post_turn").strip().lower()
        self.sharing_intent_scan = _scan if _scan in ("post_turn", "dream") else "post_turn"
        # shareable_summary 長度上限（字元）
        self.sharing_summary_max_chars = int(os.getenv("SHARING_SUMMARY_MAX_CHARS", "120"))
        # retrieve_shared：上限筆數與最低相似度（高於一般檢索，避免亂帶）
        self.sharing_retrieve_top_k = int(os.getenv("SHARING_RETRIEVE_TOP_K", "3"))
        self.sharing_retrieve_min_similarity = float(
            os.getenv("SHARING_RETRIEVE_MIN_SIMILARITY", "0.45")
        )
        # 交叉佐證候選：前 N 筆、最低相似度
        self.sharing_corroboration_top_k = int(os.getenv("SHARING_CORROBORATION_TOP_K", "5"))
        self.sharing_corroboration_min_similarity = float(
            os.getenv("SHARING_CORROBORATION_MIN_SIMILARITY", "0.5")
        )
        # 開發者 uid 白名單（逗號分隔），MCP 入口二次核對用；空＝無人具開發者權限
        self.sharing_developer_uids = tuple(
            x.strip() for x in os.getenv("SHARING_DEVELOPER_UIDS", "").split(",") if x.strip()
        )
        # MCP 是否允許讀取數字使用者 namespace（預設拒絕，僅供開發者讀自己的測試 namespace）
        self.mcp_allow_user_namespaces = (
            os.getenv("MCP_ALLOW_USER_NAMESPACES", "false").lower() == "true"
        )

        # === Concept 語意相近提示 ===
        # 新概念要 create 前，以 embedding 找 active 概念中 cosine 高於此值者（top 3），
        # 作為候選提示交給 Gate 的 LLM 判斷「同一件事（expand）」或「不同概念（create）」
        self.concept_similar_hint_threshold = float(
            os.getenv("CONCEPT_SIMILAR_HINT_THRESHOLD", "0.85")
        )

        # === Concept 關聯邊（RELATES_TO 蒸餾建邊 / LATENT_BRIDGE 檢索） ===
        # 蒸餾來源 Episode 的 Jaccard 重疊度低於此值不建 RELATES_TO
        self.relates_to_min_jaccard = float(os.getenv("RELATES_TO_MIN_JACCARD", "0.2"))
        # RELATES_TO 邊權重 = base + (1 - base) * jaccard；base 讓有意義的邊跨得過擴散門檻
        self.relates_to_weight_base = float(os.getenv("RELATES_TO_WEIGHT_BASE", "0.4"))
        # LATENT_BRIDGE 為探索式關聯，檢索擴散時權重再乘此折扣
        self.latent_bridge_discount = float(os.getenv("LATENT_BRIDGE_DISCOUNT", "0.8"))
        # bridge 半衰期（天）：自建立或最近一次 Dream 重新確認起算，權重指數衰減
        self.latent_bridge_half_life_days = float(
            os.getenv("LATENT_BRIDGE_HALF_LIFE_DAYS", "21")
        )
        # bridge 逾期（天）：超過未被重新確認即由 Dream 清除
        self.latent_bridge_expire_days = float(
            os.getenv("LATENT_BRIDGE_EXPIRE_DAYS", "60")
        )

        # === M5-D Hypothesis（LATENT_BRIDGE -> 待驗證假說）===
        # 總開關：關閉時 Dream 不產生也不審核假說（既有紀錄仍可查詢）
        self.hypothesis_enabled = os.getenv("HYPOTHESIS_ENABLED", "true").lower() == "true"
        # bridge 跨 Dream 週期被確認幾次以上才可提案
        self.hypothesis_min_confirm_count = int(os.getenv("HYPOTHESIS_MIN_CONFIRM_COUNT", "2"))
        # 每個 Dream 週期最多新提案數、開放（proposed／under_review）總量上限
        self.hypothesis_max_new_per_cycle = int(os.getenv("HYPOTHESIS_MAX_NEW_PER_CYCLE", "3"))
        self.hypothesis_max_open = int(os.getenv("HYPOTHESIS_MAX_OPEN", "20"))
        # proposed 存活天數（逾期 expired）；refuted／expired 保留天數（逾期清除）
        self.hypothesis_proposed_ttl_days = float(os.getenv("HYPOTHESIS_PROPOSED_TTL_DAYS", "30"))
        self.hypothesis_refuted_retention_days = float(
            os.getenv("HYPOTHESIS_REFUTED_RETENTION_DAYS", "180")
        )
        # 審核 insufficient 幾次後 expired（insufficient_exhausted）
        self.hypothesis_max_attempts = int(os.getenv("HYPOTHESIS_MAX_ATTEMPTS", "3"))
        # Dream 週期單次最多審核幾筆假說；審核租約秒數；失敗／不足後的退避基準（小時，指數）
        self.hypothesis_review_batch = int(os.getenv("HYPOTHESIS_REVIEW_BATCH", "3"))
        self.hypothesis_lease_seconds = float(os.getenv("HYPOTHESIS_LEASE_SECONDS", "900"))
        self.hypothesis_backoff_base_hours = float(os.getenv("HYPOTHESIS_BACKOFF_BASE_HOURS", "6"))
        # 審核者填的 statement 長度上限；昇格 RELATES_TO 邊的初始權重
        self.hypothesis_statement_max_chars = int(os.getenv("HYPOTHESIS_STATEMENT_MAX_CHARS", "200"))
        self.hypothesis_promoted_weight = float(os.getenv("HYPOTHESIS_PROMOTED_WEIGHT", "0.5"))
        # retrieve(include_hypotheses=True) 最多帶出幾筆
        self.hypothesis_retrieve_max = int(os.getenv("HYPOTHESIS_RETRIEVE_MAX", "2"))

        # === 神經化學 ===
        self.neurochem_dopamine = float(os.getenv("NEUROCHEM_DOPAMINE", "0.5"))
        self.neurochem_serotonin = float(os.getenv("NEUROCHEM_SEROTONIN", "0.5"))

        # === 日誌 ===
        self.log_level = os.getenv("LOG_LEVEL", "INFO")
        self.log_file = os.getenv("LOG_FILE", "./data/echo_memory.log")

        # === 開發模式 ===
        self.debug = os.getenv("DEBUG", "false").lower() == "true"
        self.verbose = os.getenv("VERBOSE", "false").lower() == "true"

    def get(self, key: str, default: Any = None) -> Any:
        """獲取配置值"""
        return getattr(self, key, default)

    def validate(self) -> tuple[bool, list[str]]:
        """
        驗證記憶系統必要配置是否存在。

        記憶系統本身無硬性必要的外部配置（存儲路徑有預設值），
        因此此方法直接回傳合法狀態。LLM 相關驗證請使用 validate_llm()。

        Returns:
            (is_valid, missing_keys)
        """
        return (True, [])

    def validate_llm(self) -> tuple[bool, list[str]]:
        """
        驗證 LLM API 配置是否存在（至少需要一個 provider）。

        Returns:
            (is_valid, missing_keys)
        """
        missing = []

        # 檢查 LLM 配置（目前 LLMClient 僅支援 Google provider）
        if not self.gcp_project_id and not self.google_api_key:
            missing.append("GCP_PROJECT_ID 或 GOOGLE_API_KEY")

        return (len(missing) == 0, missing)

    def summary(self) -> str:
        """配置摘要（用於調試）"""
        return f"""
Echo Memory System - 配置摘要
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
LLM Provider:
  - GCP Project: {self.gcp_project_id or '(未設定)'}
  - GCP Location: {self.gcp_location}
  - Gemini API: {'✓' if self.google_api_key else '✗'}
  - System Prompt: {self.system_prompt[:50]}...

Memory System:
  - Storage Path: {self.memory_storage_path}
  - Max Episodes: {self.memory_max_episodes:,}
  - Max Concepts: {self.memory_max_concepts:,}

Retrieval:
  - Seed K: {self.retrieval_seed_k}
  - Breadth K: {self.retrieval_breadth_k}
  - Threshold: {self.retrieval_weight_threshold}

Development:
  - Debug: {self.debug}
  - Log Level: {self.log_level}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""


# 全域配置實例
config = Config()


def get_config() -> Config:
    """獲取全域配置實例"""
    return config
