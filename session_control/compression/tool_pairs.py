"""
Tool Pair Integrity — 工具呼叫完整性保護

從 Destiny-Weavers 的 context_compressor.py 搬遷並泛化。
確保壓縮邊界不會切割 tool_call / tool_result 配對，
並修復因壓縮產生的孤立工具訊息。

核心職責：
- 邊界前對齊：向前推移邊界，越過孤立的 tool_result
- 邊界後對齊：向後拉回邊界，避免切割 tool_call 與其 result 之間
- 孤立修復：為孤立的 tool_call 插入 stub result，移除孤立的 tool_result
"""

from __future__ import annotations

import logging
from typing import List, Optional

from session_control.schemas.message import Message

logger = logging.getLogger(__name__)

# 舊工具輸出被剪裁後的佔位字串（與 micro_pruning.py 共用）
PRUNED_TOOL_PLACEHOLDER = "[Old tool output cleared to save context space]"

# 為孤立 tool_call 插入的 stub 回應內容
_ORPHAN_STUB_CONTENT = "[Tool result unavailable — was compressed out of context]"


# ── 邊界對齊 ──────────────────────────────────────────────────────────────────


def align_boundary_forward(messages: List[Message], idx: int) -> int:
    """向前推移邊界，越過索引處開頭的孤立 tool_result。

    若邊界恰好落在一個或多個 tool_result 訊息的起始位置，
    這些 result 對應的 call 已被劃入「要壓縮」的一側，
    因此需要把邊界向前推（增大 idx），讓這些孤立的 result 也被壓縮掉。

    Args:
        messages: 完整訊息列表
        idx: 目前邊界索引（tail 起始位置）

    Returns:
        調整後的邊界索引
    """
    n = len(messages)
    while idx < n and messages[idx].role == "tool":
        logger.debug("align_boundary_forward: 跳過孤立 tool_result at index %d", idx)
        idx += 1
    return idx


def align_boundary_backward(messages: List[Message], idx: int) -> int:
    """向後拉回邊界，避免把 tool_call 和其對應 result 分割在兩側。

    若邊界前一條訊息是 assistant 且含有 tool_call，
    其對應的 tool_result 可能在邊界後（tail 側），
    這樣會造成 head/summary 側有孤立的 tool_call。
    向後縮減邊界（減小 idx）直到前一條不是 tool_call 觸發訊息。

    Args:
        messages: 完整訊息列表
        idx: 目前邊界索引

    Returns:
        調整後的邊界索引
    """
    # 向後退直到邊界前的訊息不是「觸發工具呼叫的 assistant 訊息」
    while idx > 0:
        prev = messages[idx - 1]
        # 判斷：assistant 訊息帶有 tool_call_id 欄位（表示它呼叫了工具）
        # 或者前一條就是 tool_result（和更前面的 call 成對，不該被拆開）
        if prev.role == "tool":
            # 繼續往前找，這個 result 需要和它的 call 留在同一側
            idx -= 1
            logger.debug("align_boundary_backward: 退過 tool_result at index %d", idx)
        elif prev.role == "assistant" and prev.tool_call_id is not None:
            # 這個 assistant 訊息是個 tool_call 觸發者，不能讓它孤立
            idx -= 1
            logger.debug("align_boundary_backward: 退過 tool_call assistant at index %d", idx)
        else:
            break
    return idx


# ── 孤立修復 ──────────────────────────────────────────────────────────────────


def sanitize_tool_pairs(messages: List[Message]) -> List[Message]:
    """修復訊息列表中孤立的 tool_call 或 tool_result。

    壓縮完成後，head+summary+tail 拼接可能產生：
    1. 孤立 tool_result：對應的 call 被摘要掉了 → 直接移除此 result
    2. 孤立 tool_call：對應的 result 被摘要掉了 → 插入 stub result

    Args:
        messages: 壓縮後拼接的訊息列表

    Returns:
        修復後的訊息列表
    """
    result: List[Message] = []
    # 追蹤尚未配對的 tool_call_id（assistant 已發出但尚未看到 result 的）
    pending_call_ids: set[str] = set()

    for msg in messages:
        if msg.role == "assistant" and msg.tool_call_id is not None:
            # 記錄這個 call 正在等待配對 result
            pending_call_ids.add(msg.tool_call_id)
            result.append(msg)

        elif msg.role == "tool":
            if msg.tool_call_id and msg.tool_call_id in pending_call_ids:
                # 正常配對
                pending_call_ids.discard(msg.tool_call_id)
                result.append(msg)
            elif msg.tool_call_id and msg.tool_call_id not in pending_call_ids:
                # 孤立 tool_result（call 已被壓縮掉）→ 移除
                logger.warning(
                    "sanitize_tool_pairs: 移除孤立 tool_result，tool_call_id=%s",
                    msg.tool_call_id,
                )
            else:
                # tool_call_id 為 None 的 tool_result，無法判斷，保留
                result.append(msg)

        else:
            # 非工具相關訊息直接保留
            result.append(msg)

    # 為所有仍在等待的孤立 tool_call 插入 stub result
    if pending_call_ids:
        stub_messages: List[Message] = []
        for call_id in pending_call_ids:
            logger.warning(
                "sanitize_tool_pairs: 為孤立 tool_call 插入 stub result，tool_call_id=%s",
                call_id,
            )
            stub_messages.append(
                Message(
                    role="tool",
                    content=_ORPHAN_STUB_CONTENT,
                    tool_call_id=call_id,
                    metadata={"is_stub": True, "reason": "orphaned_after_compression"},
                )
            )

        # stub 插入到列表末尾（孤立的 call 應在尾部附近）
        # 更精確的做法是插到對應 call 之後，但考量到壓縮後順序已重排，
        # 追加到尾部是最安全的降級策略
        result.extend(stub_messages)

    return result


# ── 輔助：找出最後一條 tool_call 的索引 ──────────────────────────────────────


def find_last_complete_tool_group_end(messages: List[Message], before: int) -> int:
    """在 before 索引之前，找到最後一個完整 tool 組的結尾索引。

    「完整 tool 組」= 一條 assistant tool_call 訊息 + 緊接的一條 tool_result 訊息。
    若最後一組不完整（只有 call 沒有 result），則退到更前面的完整組。

    用途：協助 full_compaction 計算安全的壓縮邊界。

    Args:
        messages: 訊息列表
        before: 搜尋範圍上限（不含）

    Returns:
        安全的結尾索引（exclusive），-1 表示找不到任何完整組
    """
    idx = before - 1
    while idx >= 0:
        msg = messages[idx]
        if msg.role == "tool":
            # 找到一條 result，往前確認有對應的 call
            call_idx = idx - 1
            while call_idx >= 0 and messages[call_idx].role == "tool":
                call_idx -= 1
            if call_idx >= 0 and messages[call_idx].role == "assistant" and messages[call_idx].tool_call_id is not None:
                # 完整組，結尾是這條 result 的下一個位置
                return idx + 1
        idx -= 1
    return -1
