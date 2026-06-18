"""
多模態內容管理 — 圖片 token 估算與舊圖片清理

處理對話中的多模態內容（特別是 base64 圖片），避免
舊圖片佔用過多 context window。

功能：
  - estimate_image_tokens: 估算圖片的 token 消耗
  - strip_old_images: 清理舊的 base64 圖片數據
  - measure_content_length: 多模態內容的 token 估算
"""

from __future__ import annotations

import logging
import re
from typing import List, Tuple

from echo_thought_core.schemas.message import Message

logger = logging.getLogger(__name__)

# 每張圖片的 token 估算（取各平台的合理上限）
# Anthropic ≈ width×height/750, GPT-4o up to ~1700, Gemini 258/tile
IMAGE_TOKEN_ESTIMATE = 1600

# 圖片被清理後的佔位字串
IMAGE_PLACEHOLDER = "[image removed to save context space]"
IMAGE_PLACEHOLDER_TOKENS = 10

# base64 圖片的正則匹配（允許折行的 base64 字串）
_BASE64_IMAGE_RE = re.compile(
    r"data:image/[a-zA-Z]+;base64,[A-Za-z0-9+/=\s]{100,}",
)


def estimate_image_tokens(content: str) -> int:
    """估算文字中嵌入的 base64 圖片所消耗的 token 數。

    Returns:
        估算的圖片 token 數（每張圖片固定估算 IMAGE_TOKEN_ESTIMATE）
    """
    matches = _BASE64_IMAGE_RE.findall(content)
    return len(matches) * IMAGE_TOKEN_ESTIMATE


def strip_old_images(
    messages: List[Message],
    keep_last_n: int = 3,
) -> Tuple[List[Message], int]:
    """清理舊的 base64 圖片，保留最近 N 張。

    從尾部往前數，保留最後 keep_last_n 個含圖片的訊息，
    更早的訊息中的 base64 數據替換為佔位字串。

    Args:
        messages: 訊息列表
        keep_last_n: 保留最近幾個含圖片的訊息

    Returns:
        (處理後的訊息列表, 估算節省的 token 數)
    """
    # 找出所有含 base64 圖片的訊息索引
    image_indices = [
        i
        for i, m in enumerate(messages)
        if _BASE64_IMAGE_RE.search(m.content)
    ]

    if len(image_indices) <= keep_last_n:
        return messages, 0  # 圖片不多，不需要清理

    # 需要清理的索引（保留最後 keep_last_n 個）
    to_strip = image_indices[:-keep_last_n] if keep_last_n > 0 else image_indices

    result = list(messages)
    total_saved = 0

    for idx in to_strip:
        msg = result[idx]
        matches = _BASE64_IMAGE_RE.findall(msg.content)
        if not matches:
            continue

        # 替換所有 base64 圖片為佔位字串
        new_content = _BASE64_IMAGE_RE.sub(IMAGE_PLACEHOLDER, msg.content)
        images_removed = len(matches)
        saved = images_removed * IMAGE_TOKEN_ESTIMATE

        result[idx] = Message(
            role=msg.role,
            content=new_content,
            timestamp=msg.timestamp,
            token_count=max(
                IMAGE_PLACEHOLDER_TOKENS,
                (msg.token_count or 0) - saved,
            ),
            is_compressed=msg.is_compressed,
            original_turn_range=msg.original_turn_range,
            tool_call_id=msg.tool_call_id,
            tool_name=msg.tool_name,
            is_reproducible=msg.is_reproducible,
            metadata={
                **msg.metadata,
                "images_stripped": images_removed,
                "original_image_tokens": saved,
            },
        )

        total_saved += saved
        logger.debug(
            "multimodal: stripped %d images from message %d, saved ~%d tokens",
            images_removed, idx, saved,
        )

    return result, total_saved


def measure_content_length(content: str) -> int:
    """估算多模態內容的 token 數。

    文字部分用 len/4 估算，base64 圖片用 IMAGE_TOKEN_ESTIMATE 估算。

    Returns:
        估算的 token 數
    """
    image_tokens = estimate_image_tokens(content)
    # 移除 base64 數據後估算文字部分
    text_only = _BASE64_IMAGE_RE.sub("", content)
    text_tokens = len(text_only) // 4
    return text_tokens + image_tokens
