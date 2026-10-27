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
        self.embedding_query_timeout = float(os.getenv("EMBEDDING_QUERY_TIMEOUT", "5"))
        self.embedding_write_timeout = float(os.getenv("EMBEDDING_WRITE_TIMEOUT", "30"))
        self.embedding_warmup_timeout = float(os.getenv("EMBEDDING_WARMUP_TIMEOUT", "120"))

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
        self.dream_replay_da_threshold = float(
            os.getenv("DREAM_REPLAY_DA_THRESHOLD", "0.6")
        )

        # === Patch Gate held 重審 ===
        # held patch 每次重審失敗（LLM 仍不可用 / 仍無法判定）計一次，達上限即標記為最終拒絕
        self.concept_held_max_retries = int(os.getenv("CONCEPT_HELD_MAX_RETRIES", "3"))
        # held 超過此天數仍未解決視為逾期，標記為最終拒絕
        self.concept_held_max_age_days = float(os.getenv("CONCEPT_HELD_MAX_AGE_DAYS", "7"))
        # 單次觸發（對話 patch / Dream 週期）最多重審幾筆，避免拖慢對話路徑
        self.concept_held_review_batch = int(os.getenv("CONCEPT_HELD_REVIEW_BATCH", "3"))

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
