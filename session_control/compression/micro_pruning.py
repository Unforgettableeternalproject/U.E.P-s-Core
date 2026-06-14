"""
Layer 1: Micro-Pruning — 規則式輕量清理

觸發條件：token 使用率 > 60%
策略：
- 清理可重現的工具輸出（file read 結果等）
- 壓縮連續相似的確認訊息
- 移除空白/無意義的訊息
- 可選：LLMLingua-2 對長訊息做 token 刪減

零 LLM 呼叫，規則式處理。
"""

from typing import List, Tuple
from session_control.schemas.message import Message


class MicroPruner:
    """Layer 1 微清理器"""

    def __init__(self, use_llmlingua: bool = False):
        self.use_llmlingua = use_llmlingua
        self._llmlingua_compressor = None

    def prune(self, messages: List[Message]) -> Tuple[List[Message], int]:
        """執行微清理

        Returns:
            (清理後的訊息列表, 節省的 token 數)
        """
        tokens_saved = 0
        result = []

        for i, msg in enumerate(messages):
            # 規則 1：跳過可重現的工具輸出（替換為 marker）
            if msg.role == "tool" and msg.is_reproducible and msg.token_count and msg.token_count > 200:
                marker = Message(
                    role=msg.role,
                    content=f"[Tool output from {msg.tool_name or 'unknown'} — {msg.token_count} tokens, pruned]",
                    tool_call_id=msg.tool_call_id,
                    tool_name=msg.tool_name,
                    is_reproducible=msg.is_reproducible,
                    metadata={**msg.metadata, "pruned": True, "original_tokens": msg.token_count},
                )
                tokens_saved += (msg.token_count or 0) - 20  # marker 約 20 tokens
                result.append(marker)
                continue

            # 規則 2：壓縮連續的短確認訊息
            if (
                msg.role == "assistant"
                and msg.token_count
                and msg.token_count < 15
                and i > 0
                and result
                and result[-1].role == "assistant"
                and result[-1].token_count
                and result[-1].token_count < 15
            ):
                # 合併兩則短確認
                merged_content = f"{result[-1].content} | {msg.content}"
                result[-1].content = merged_content
                tokens_saved += 4  # 省下一組 role overhead
                continue

            result.append(msg)

        return result, tokens_saved
