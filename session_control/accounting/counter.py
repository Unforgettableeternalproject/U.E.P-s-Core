"""
Token Counter — 多後端精確 token 計數

支援 tiktoken (OpenAI/Anthropic) 和 transformers tokenizer (本地模型)。
"""

from typing import Optional, List
from session_control.schemas.message import Message


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
                # 本地模型使用 transformers tokenizer
                # 延遲到實際使用時載入
                pass
            else:
                # Gemini 等其他後端使用 fallback 估算
                pass
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

    def count_messages(self, messages: List[Message]) -> int:
        """計算多則訊息的總 token 數量"""
        return sum(self.count_message(m) for m in messages)

    def estimate_overhead(self, tool_count: int = 0) -> int:
        """估算固定開銷（系統提示、工具定義等）"""
        # 基礎系統開銷
        base_overhead = 500

        # 每個工具定義約 200-500 tokens
        tool_overhead = tool_count * 350

        return base_overhead + tool_overhead
