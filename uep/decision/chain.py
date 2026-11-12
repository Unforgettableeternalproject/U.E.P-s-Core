"""複合句 chain 的決策點規格與常數（Chain 契約 §3.2、§3.8）。葉節點，只有常數與 spec，沒有 engine。

``turn.chain_segment``（段落的 route 判定列）與 ``turn.chain``（chain 生命週期列，publish 模式）目前**不掛任何 engine**，
也不被任何元件註冊或消費；本檔只先固定名稱、列舉與 reason_code 閉集，讓後續 PR（拆分器 shadow、段落重入）共用單一來源。
"""
from __future__ import annotations

from typing import Final

from uep.decision.registry import DecisionPointSpec

KIND_TURN_CHAIN_SEGMENT: Final = "turn.chain_segment"
KIND_TURN_CHAIN: Final = "turn.chain"

# ChainState 終態與進行中狀態（契約 §3.2 生命週期）；turn.chain 列的 outcome 取此閉集
CHAIN_STATES: Final[tuple[str, ...]] = (
    "splitting", "running", "suspended", "completed", "cancelled", "truncated", "fell_back",
)

# turn.chain 列的 sub_kind（契約 §3.8）
CHAIN_SUB_KINDS: Final[tuple[str, ...]] = (
    "begin", "split_done", "fell_back", "truncated", "suspended", "resumed", "cancelled", "completed",
)

# turn.chain_segment 列的 sub_kind：route 的判定來源（契約 §3.8）
SEGMENT_SUB_KINDS: Final[tuple[str, ...]] = ("laya_route", "fallback")

# reason_code 閉集（暫定，契約 §3.8）
CHAIN_REASON_CODES: Final[tuple[str, ...]] = (
    "split_timeout_first", "split_timeout_total", "split_error", "split_invalid", "split_single_segment",
    "split_over_limit", "split_busy", "chain_disabled", "not_respond", "not_addressed", "chain_active",
    "segment_not_addressed", "no_addressed_segment", "global_stop", "control_stop", "presence_lost",
    "user_interrupt", "chain_ttl", "chain_abandoned_restart", "segment_vetoed", "approval_pending", "completed",
)

# 段落數上限暫定值（契約 Q-J；實測後定案）
MAX_SEGMENTS: Final = 4


def chain_segment_spec() -> DecisionPointSpec:
    """``turn.chain_segment``：段落重入的 route 判定（outcome 複製 reply_mode）。尚未掛 engine。"""
    return DecisionPointSpec(
        kind=KIND_TURN_CHAIN_SEGMENT, mode="sync", owner="turn",
        outcomes=("respond", "passive", "pass", "defer"),
        budget_ms=None, allow_native=False, shadow=False,
    )


def chain_spec() -> DecisionPointSpec:
    """``turn.chain``：chain 生命週期列（publish 模式，比照 ``policy.snapshot``）。尚未掛 engine。"""
    return DecisionPointSpec(
        kind=KIND_TURN_CHAIN, mode="publish", owner="turn", outcomes=CHAIN_STATES,
        budget_ms=None, allow_native=False, shadow=False,
    )
