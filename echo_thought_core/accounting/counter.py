"""
Token Counter — 多後端精確 token 計數

支援 tiktoken (OpenAI/Anthropic) 和 transformers tokenizer (本地模型)。
同時包含輕量估算工具函數、模型 context length 資料庫，以及尾部保護計算。
"""

import logging
from enum import Enum
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any

from echo_thought_core.schemas.message import Message

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# 常數
# ──────────────────────────────────────────────

# 每則訊息的固定額外開銷（role 標記、分隔符等）
MESSAGE_OVERHEAD_TOKENS: int = 4

# 整段對話的固定起始開銷
CONVERSATION_BASE_TOKENS: int = 10


# ──────────────────────────────────────────────
# ModelFamily 枚舉
# ──────────────────────────────────────────────

class ModelFamily(Enum):
    """模型提供商家族"""
    ANTHROPIC = "anthropic"
    OPENAI    = "openai"
    GOOGLE    = "google"
    ALIBABA   = "alibaba"   # Qwen 系列
    DEEPSEEK  = "deepseek"
    META      = "meta"      # Llama 系列
    UNKNOWN   = "unknown"


# ──────────────────────────────────────────────
# ModelInfo 資料類
# ──────────────────────────────────────────────

@dataclass
class ModelInfo:
    """單一模型的完整規格"""
    name: str
    family: ModelFamily
    context_window: int           # 輸入 context 長度（tokens）
    max_output_tokens: int        # 最大輸出長度（tokens）
    input_cost_per_1m: float      # 每百萬輸入 token 的美元成本
    output_cost_per_1m: float     # 每百萬輸出 token 的美元成本
    encoding: str = "cl100k_base" # tiktoken 編碼名稱
    aliases: List[str] = field(default_factory=list)  # 模糊匹配用的別名


# ──────────────────────────────────────────────
# MODEL_DATABASE — 模型定價與規格資料庫
# ──────────────────────────────────────────────

MODEL_DATABASE: Dict[str, ModelInfo] = {
    # ── Anthropic Claude ──────────────────────
    "claude-opus-4-5": ModelInfo(
        name="claude-opus-4-5",
        family=ModelFamily.ANTHROPIC,
        context_window=200_000,
        max_output_tokens=32_000,
        input_cost_per_1m=15.0,
        output_cost_per_1m=75.0,
        aliases=["claude-opus-4.5", "opus-4-5"],
    ),
    "claude-sonnet-4-5": ModelInfo(
        name="claude-sonnet-4-5",
        family=ModelFamily.ANTHROPIC,
        context_window=200_000,
        max_output_tokens=16_000,
        input_cost_per_1m=3.0,
        output_cost_per_1m=15.0,
        aliases=["claude-sonnet-4.5", "sonnet-4-5"],
    ),
    "claude-haiku-4-5": ModelInfo(
        name="claude-haiku-4-5",
        family=ModelFamily.ANTHROPIC,
        context_window=200_000,
        max_output_tokens=16_000,
        input_cost_per_1m=0.8,
        output_cost_per_1m=4.0,
        aliases=["claude-haiku-4.5", "haiku-4-5"],
    ),
    "claude-sonnet-4-6": ModelInfo(
        name="claude-sonnet-4-6",
        family=ModelFamily.ANTHROPIC,
        context_window=1_000_000,
        max_output_tokens=32_000,
        input_cost_per_1m=3.0,
        output_cost_per_1m=15.0,
        aliases=["claude-sonnet-4.6", "sonnet-4-6", "claude-4-6"],
    ),
    "claude-opus-4": ModelInfo(
        name="claude-opus-4",
        family=ModelFamily.ANTHROPIC,
        context_window=200_000,
        max_output_tokens=32_000,
        input_cost_per_1m=15.0,
        output_cost_per_1m=75.0,
        aliases=["claude-opus-4.0", "opus-4"],
    ),
    # ── OpenAI GPT ────────────────────────────
    "gpt-5.4": ModelInfo(
        name="gpt-5.4",
        family=ModelFamily.OPENAI,
        context_window=1_050_000,
        max_output_tokens=65_536,
        input_cost_per_1m=2.5,
        output_cost_per_1m=10.0,
        aliases=["gpt-5-4", "gpt5.4"],
    ),
    "gpt-4o": ModelInfo(
        name="gpt-4o",
        family=ModelFamily.OPENAI,
        context_window=128_000,
        max_output_tokens=16_384,
        input_cost_per_1m=2.5,
        output_cost_per_1m=10.0,
        aliases=["gpt4o", "gpt-4o-latest"],
    ),
    "gpt-4o-mini": ModelInfo(
        name="gpt-4o-mini",
        family=ModelFamily.OPENAI,
        context_window=128_000,
        max_output_tokens=16_384,
        input_cost_per_1m=0.15,
        output_cost_per_1m=0.6,
        aliases=["gpt4o-mini"],
    ),
    "o3": ModelInfo(
        name="o3",
        family=ModelFamily.OPENAI,
        context_window=200_000,
        max_output_tokens=100_000,
        input_cost_per_1m=10.0,
        output_cost_per_1m=40.0,
        aliases=["openai-o3"],
    ),
    # ── Google Gemini ─────────────────────────
    "gemini-2.0-flash": ModelInfo(
        name="gemini-2.0-flash",
        family=ModelFamily.GOOGLE,
        context_window=2_000_000,
        max_output_tokens=8_192,
        input_cost_per_1m=0.1,
        output_cost_per_1m=0.4,
        encoding="cl100k_base",   # Gemini 無官方 tiktoken，使用 fallback
        aliases=["gemini-2.0-flash-exp", "gemini-flash-2"],
    ),
    "gemini-2.5-flash": ModelInfo(
        name="gemini-2.5-flash",
        family=ModelFamily.GOOGLE,
        context_window=1_000_000,
        max_output_tokens=65_536,
        input_cost_per_1m=0.15,
        output_cost_per_1m=0.6,
        encoding="cl100k_base",
        aliases=["gemini-2.5-flash-preview", "gemini-flash-2.5"],
    ),
    "gemini-2.5-pro": ModelInfo(
        name="gemini-2.5-pro",
        family=ModelFamily.GOOGLE,
        context_window=2_000_000,
        max_output_tokens=65_536,
        input_cost_per_1m=1.25,
        output_cost_per_1m=10.0,
        encoding="cl100k_base",
        aliases=["gemini-2.5-pro-preview", "gemini-pro-2.5"],
    ),
    # ── Alibaba Qwen ──────────────────────────
    "qwen3-235b": ModelInfo(
        name="qwen3-235b",
        family=ModelFamily.ALIBABA,
        context_window=131_072,
        max_output_tokens=8_192,
        input_cost_per_1m=0.6,
        output_cost_per_1m=2.4,
        encoding="cl100k_base",
        aliases=["qwen3-235b-a22b", "qwen-3-235b"],
    ),
    "qwen3-72b": ModelInfo(
        name="qwen3-72b",
        family=ModelFamily.ALIBABA,
        context_window=131_072,
        max_output_tokens=8_192,
        input_cost_per_1m=0.4,
        output_cost_per_1m=1.2,
        encoding="cl100k_base",
        aliases=["qwen-3-72b"],
    ),
    # ── DeepSeek ──────────────────────────────
    "deepseek-v3": ModelInfo(
        name="deepseek-v3",
        family=ModelFamily.DEEPSEEK,
        context_window=128_000,
        max_output_tokens=8_192,
        input_cost_per_1m=0.27,
        output_cost_per_1m=1.1,
        encoding="cl100k_base",
        aliases=["deepseek-chat", "deepseek-v3-0324"],
    ),
    "deepseek-r1": ModelInfo(
        name="deepseek-r1",
        family=ModelFamily.DEEPSEEK,
        context_window=128_000,
        max_output_tokens=32_768,
        input_cost_per_1m=0.55,
        output_cost_per_1m=2.19,
        encoding="cl100k_base",
        aliases=["deepseek-reasoner"],
    ),
    # ── Meta Llama ────────────────────────────
    "llama-3.3-70b": ModelInfo(
        name="llama-3.3-70b",
        family=ModelFamily.META,
        context_window=131_072,
        max_output_tokens=8_192,
        input_cost_per_1m=0.2,
        output_cost_per_1m=0.2,
        encoding="cl100k_base",
        aliases=["llama3.3-70b", "meta-llama-3.3-70b"],
    ),
    "llama-4-maverick": ModelInfo(
        name="llama-4-maverick",
        family=ModelFamily.META,
        context_window=1_000_000,
        max_output_tokens=16_384,
        input_cost_per_1m=0.2,
        output_cost_per_1m=0.6,
        encoding="cl100k_base",
        aliases=["llama4-maverick", "meta-llama-4-maverick"],
    ),
}


# ──────────────────────────────────────────────
# get_model_info — 模糊匹配查詢
# ──────────────────────────────────────────────

def get_model_info(model_name: str) -> Optional[ModelInfo]:
    """
    依模型名稱查詢 ModelInfo，支援模糊匹配（別名、部分名稱）。

    匹配優先序：
    1. 完整名稱精確匹配
    2. 別名精確匹配
    3. 部分名稱包含匹配（model_name 是 key 的子字串）
    4. 返回 None（找不到）
    """
    if not model_name:
        return None

    normalized = model_name.strip().lower()

    # 1. 精確匹配 key
    if normalized in MODEL_DATABASE:
        return MODEL_DATABASE[normalized]

    # 2. 別名精確匹配
    for info in MODEL_DATABASE.values():
        if normalized in [a.lower() for a in info.aliases]:
            return info

    # 3. 部分匹配——model_name 是 key 的子字串或 key 是 model_name 的子字串
    for key, info in MODEL_DATABASE.items():
        if normalized in key or key in normalized:
            return info

    logger.debug("找不到模型資訊：%s", model_name)
    return None


# ──────────────────────────────────────────────
# get_model_context_length — 模型 context 長度查詢
# ──────────────────────────────────────────────

def get_model_context_length(model: str, provider: Optional[str] = None) -> int:
    """
    查詢模型的 context window 長度。

    優先使用 MODEL_DATABASE，若找不到則依 provider 返回通用預設值。
    """
    info = get_model_info(model)
    if info is not None:
        return info.context_window

    # 依 provider 返回通用預設值
    if provider:
        p = provider.lower()
        if p in ("anthropic", "claude"):
            return 200_000
        if p in ("openai", "gpt"):
            return 128_000
        if p in ("google", "gemini"):
            return 1_000_000
        if p in ("deepseek",):
            return 128_000
        if p in ("meta", "llama"):
            return 131_072

    # 完全未知，保守返回 128K
    logger.warning("無法查詢模型 context 長度，使用預設 128K：model=%s", model)
    return 128_000


# ──────────────────────────────────────────────
# 輕量估算工具函數
# ──────────────────────────────────────────────

def estimate_tokens(text: str) -> int:
    """
    輕量 token 估算：chars/4（含中文感知加權）。
    適合快速估算，不需要 tokenizer。
    """
    if not text:
        return 0
    cjk_count = sum(1 for c in text if "一" <= c <= "鿿")
    ascii_count = len(text) - cjk_count
    return (ascii_count // 4) + (cjk_count * 2 // 3) + 1


def estimate_message_tokens(message: Dict[str, Any]) -> int:
    """
    估算單則訊息的 token 數（含固定 overhead、tool_calls、reasoning）。

    接受 dict 格式：{"role": ..., "content": ..., "tool_calls": [...], ...}
    """
    total = MESSAGE_OVERHEAD_TOKENS

    # 主要內容
    content = message.get("content") or ""
    if isinstance(content, str):
        total += estimate_tokens(content)
    elif isinstance(content, list):
        # 多模態內容塊
        for block in content:
            if isinstance(block, dict):
                block_text = block.get("text") or ""
                total += estimate_tokens(block_text)
                # 圖片塊的固定開銷（粗估）
                if block.get("type") == "image":
                    total += 1000

    # tool_calls（工具呼叫的 JSON 本身也佔 tokens）
    tool_calls = message.get("tool_calls") or []
    for tc in tool_calls:
        if isinstance(tc, dict):
            import json
            total += estimate_tokens(json.dumps(tc, ensure_ascii=False))

    # reasoning / thinking 內容（某些模型有）
    reasoning = message.get("reasoning") or message.get("thinking") or ""
    if reasoning:
        total += estimate_tokens(str(reasoning))

    return total


def estimate_messages_tokens(messages: List[Dict[str, Any]]) -> int:
    """
    估算整段對話的 token 數（含對話固定起始開銷）。
    """
    if not messages:
        return 0
    total = CONVERSATION_BASE_TOKENS
    for msg in messages:
        total += estimate_message_tokens(msg)
    return total


# ──────────────────────────────────────────────
# count_tokens_in_tail — 尾部保護計算
# ──────────────────────────────────────────────

def count_tokens_in_tail(
    messages: List[Dict[str, Any]],
    token_budget: int,
) -> int:
    """
    從訊息列表尾部往前掃，計算在 token_budget 內可以保護多少則訊息。

    返回「應保護的訊息數量」（從尾部算起）。
    若所有訊息都在預算內，返回 len(messages)。
    """
    if not messages or token_budget <= 0:
        return 0

    accumulated = 0
    protected_count = 0

    for msg in reversed(messages):
        msg_tokens = estimate_message_tokens(msg)
        if accumulated + msg_tokens > token_budget:
            break
        accumulated += msg_tokens
        protected_count += 1

    return protected_count


# ──────────────────────────────────────────────
# TokenCounter 類（原有，保留完整）
# ──────────────────────────────────────────────

class TokenCounter:
    """精確 token 計數器 — 後端感知"""

    def __init__(self, backend: str = "anthropic", model: Optional[str] = None):
        self.backend = backend
        self.model = model
        self._tokenizer = None
        self._load_tokenizer()

    def _load_tokenizer(self) -> None:
        """延遲載入 tokenizer"""
        try:
            if self.backend in ("anthropic", "openai"):
                import tiktoken

                # Claude 和 GPT-4o 都使用 cl100k_base 或類似的 tokenizer
                encoding_name = "cl100k_base"
                self._tokenizer = tiktoken.get_encoding(encoding_name)
            elif self.backend == "local":
                # Phase 3 計畫：本地模型使用 transformers tokenizer
                logger.debug("[TokenCounter] local backend：使用 fallback 估算")
            else:
                # Gemini 等其他後端無專用 tokenizer
                logger.debug("[TokenCounter] %s backend：使用 fallback 估算", self.backend)
        except ImportError:
            self._tokenizer = None

    def count(self, text: str) -> int:
        """計算文字的 token 數量"""
        if not text:
            return 0

        if self._tokenizer is not None:
            return len(self._tokenizer.encode(text))

        # Fallback: 字元數 / 4 估算（英文）+ 中文字加權
        cjk_count = sum(1 for c in text if "一" <= c <= "鿿")
        ascii_count = len(text) - cjk_count
        return (ascii_count // 4) + (cjk_count * 2 // 3) + 1

    def count_message(self, message: Message) -> int:
        """計算單則訊息的 token 數量（含 role overhead）"""
        # 每則訊息有固定的 role/formatting overhead
        overhead = 4  # <role>...\n 約 4 tokens
        content_tokens = self.count(message.content)
        total = overhead + content_tokens

        # 更新訊息上的 token_count
        message.token_count = total
        return total

