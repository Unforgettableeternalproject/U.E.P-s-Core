"""
LLMLingua-2 整合 — Token 級別刪減壓縮

使用 Microsoft LLMLingua-2 的 PromptCompressor，透過 XLM-RoBERTa 分類器
對文本做 token 級別的冗餘刪減。不需要 LLM 呼叫，純本地推論。

設計：
- Lazy loading：首次使用時才載入模型（約 1-2 秒 + 首次下載 ~200MB）
- 可配置壓縮率（預設 0.5 = 壓縮到原文的 50%）
- 最小門檻：content 低於 min_token_threshold 時不壓縮（避免短訊息浪費 GPU）
- Graceful fallback：llmlingua 未安裝時回傳原文不報錯

參考：
- LLMLingua-2 論文：arXiv:2403.12968 (ACL 2024)
- PyPI：pip install llmlingua
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# 嘗試 import llmlingua，失敗時 graceful fallback
try:
    from llmlingua import PromptCompressor

    _HAS_LLMLINGUA = True
except ImportError:
    PromptCompressor = None  # type: ignore[misc,assignment]
    _HAS_LLMLINGUA = False


@dataclass
class LLMLinguaConfig:
    """LLMLingua-2 壓縮配置"""

    # 壓縮率：0.5 = 保留 50% 的 token（2x 壓縮）
    rate: float = 0.5

    # 最小 token 門檻：低於此值不壓縮
    min_token_threshold: int = 2000

    # 模型名稱（LLMLingua-2 推薦的 XLM-RoBERTa 模型）
    model_name: str = "microsoft/llmlingua-2-xlm-roberta-large-meetingbank"

    # 是否使用 GPU
    use_gpu: bool = True

    # 強制 token 保留（保留原文的前 N 個 token 不壓縮）
    force_keep_first_tokens: int = 0


class LLMLinguaCompressor:
    """LLMLingua-2 壓縮器 — Lazy Loading 單例模式

    首次呼叫 compress() 時才載入模型。之後重複使用同一個 PromptCompressor。
    若 llmlingua 未安裝，所有呼叫都回傳原文。
    """

    def __init__(self, config: Optional[LLMLinguaConfig] = None):
        self.config = config or LLMLinguaConfig()
        self._compressor: Optional[PromptCompressor] = None
        self._initialized = False
        self._available = _HAS_LLMLINGUA

    @property
    def is_available(self) -> bool:
        """LLMLingua 是否可用（已安裝）"""
        return self._available

    def _ensure_loaded(self) -> bool:
        """Lazy load 模型。回傳是否成功。"""
        if self._initialized:
            return self._compressor is not None

        self._initialized = True

        if not self._available:
            logger.warning("[LLMLingua] llmlingua 未安裝，壓縮功能停用")
            return False

        try:
            device = "cuda" if self.config.use_gpu else "cpu"
            logger.info(
                "[LLMLingua] 載入模型: %s (device=%s)",
                self.config.model_name,
                device,
            )

            self._compressor = PromptCompressor(
                model_name=self.config.model_name,
                use_llmlingua2=True,
                device_map=device,
            )

            logger.info("[LLMLingua] 模型載入完成")
            return True

        except Exception as e:
            logger.error("[LLMLingua] 模型載入失敗: %s", e)
            self._compressor = None
            return False

    def compress(
        self,
        text: str,
        rate: Optional[float] = None,
        force_context_ids: Optional[list] = None,
    ) -> tuple[str, dict]:
        """壓縮文本

        Args:
            text: 要壓縮的文本
            rate: 壓縮率（覆蓋 config 預設值）。0.5 = 保留 50%
            force_context_ids: 強制保留的 context token 索引（進階用法）

        Returns:
            (compressed_text, stats_dict)
            stats_dict 包含：
                - original_tokens: 原始 token 數
                - compressed_tokens: 壓縮後 token 數
                - ratio: 實際壓縮比
                - saving: 節省的 token 數
        """
        # 估算 token 數（粗估）
        estimated_tokens = len(text) // 4

        # 低於門檻不壓縮
        if estimated_tokens < self.config.min_token_threshold:
            return text, {
                "original_tokens": estimated_tokens,
                "compressed_tokens": estimated_tokens,
                "ratio": 1.0,
                "saving": 0,
                "skipped": True,
                "reason": "below_threshold",
            }

        # 確保模型已載入
        if not self._ensure_loaded():
            return text, {
                "original_tokens": estimated_tokens,
                "compressed_tokens": estimated_tokens,
                "ratio": 1.0,
                "saving": 0,
                "skipped": True,
                "reason": "model_unavailable",
            }

        target_rate = rate if rate is not None else self.config.rate

        try:
            result = self._compressor.compress_prompt(
                text,
                rate=target_rate,
                force_tokens=force_context_ids or [],
            )

            compressed = result.get("compressed_prompt", text)
            origin_tokens = result.get("origin_tokens", estimated_tokens)
            compressed_tokens = result.get("compressed_tokens", len(compressed) // 4)

            saving = origin_tokens - compressed_tokens
            actual_ratio = compressed_tokens / origin_tokens if origin_tokens > 0 else 1.0

            logger.info(
                "[LLMLingua] 壓縮完成: %d → %d tokens (%.1fx, 節省 %d)",
                origin_tokens,
                compressed_tokens,
                1.0 / actual_ratio if actual_ratio > 0 else 1.0,
                saving,
            )

            return compressed, {
                "original_tokens": origin_tokens,
                "compressed_tokens": compressed_tokens,
                "ratio": actual_ratio,
                "saving": saving,
                "skipped": False,
            }

        except Exception as e:
            logger.error("[LLMLingua] 壓縮失敗: %s", e)
            return text, {
                "original_tokens": estimated_tokens,
                "compressed_tokens": estimated_tokens,
                "ratio": 1.0,
                "saving": 0,
                "skipped": True,
                "reason": f"error: {e}",
            }

    def get_stats(self) -> dict:
        """取得壓縮器狀態"""
        return {
            "available": self._available,
            "initialized": self._initialized,
            "model_loaded": self._compressor is not None,
            "model_name": self.config.model_name,
            "default_rate": self.config.rate,
            "min_threshold": self.config.min_token_threshold,
        }
