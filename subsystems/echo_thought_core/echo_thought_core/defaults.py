"""
Shared Defaults — 全套件共用的預設常數

所有 magic numbers 集中管理，避免散落各檔案造成不一致。
修改預設值只需改這裡一處。
"""

# ── 模型 ─────────────────────────────────────────────────────────────────────

DEFAULT_MODEL: str = "gemini-2.5-flash-lite"
"""預設 LLM 模型名稱（Gemini 2.5 Flash-Lite）"""

DEFAULT_GCP_LOCATION: str = "us-central1"
"""預設 GCP / Vertex AI 區域"""

# ── 生成參數 ──────────────────────────────────────────────────────────────────

DEFAULT_TEMPERATURE: float = 0.8
"""預設 temperature（控制回應隨機性）"""

DEFAULT_MAX_OUTPUT_TOKENS: int = 8192
"""Gemini backend 的預設最大輸出 token 數"""

DEFAULT_CONTEXT_WINDOW: int = 200_000
"""預設 context window 大小（tokens）"""

DEFAULT_RESERVE_FOR_OUTPUT: int = 4096
"""預設保留給輸出回應的 token 數"""

# ── 壓縮 ─────────────────────────────────────────────────────────────────────

LLMLINGUA_MIN_TOKEN_THRESHOLD: int = 2000
"""LLMLingua-2 觸發的最低 token 門檻（低於此值不壓縮）"""

DEFAULT_EMERGENCY_KEEP: int = 4
"""Layer 4 緊急模式保留的最後幾則訊息"""

# ── Token 估算 ───────────────────────────────────────────────────────────────

IMAGE_TOKEN_ESTIMATE: int = 1000
"""多模態圖片塊的固定 token 估算值（粗估）"""
