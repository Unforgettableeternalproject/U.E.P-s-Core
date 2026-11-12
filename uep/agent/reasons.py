"""AgentLoop 的固定原因碼集合（``LoopOutcome.reason_code`` 與最後 checkpoint 的 ``end:<reason>`` 只能使用這裡的值）。

原因碼是短碼、可進 checkpoint 的值層級白名單；不含例外文字或外部內容。耗盡的維度另放在 ``end:<reason>:<dim>`` 的第三段。
"""
from __future__ import annotations

from enum import StrEnum


class LoopReason(StrEnum):
    CAPABILITY_EMPTY = "capability_empty"
    CHECKPOINT_INCOMPLETE = "checkpoint_incomplete"
    CONTEXT_LOST = "context_lost"
    BUDGET_EXHAUSTED = "budget_exhausted"
    BUDGET_FINALIZED = "budget_finalized"
    NO_PROGRESS = "no_progress"
    RUN_DEADLINE = "run_deadline"
    TASK_DEADLINE = "task_deadline"
    STEP_TIMEOUT = "step_timeout"
    CANCELLED = "cancelled"
    ESCALATED = "escalated"
    CONSULT_FINALIZED = "consult_finalized"
    FINALIZE_FAILED = "finalize_failed"
    UNRESOLVED_ACTIONS = "unresolved_actions"
    EMPTY_RESULT = "empty_result"
    APPROVAL_REQUIRED = "approval_required"
    CAPABILITY_REVOKED = "capability_revoked"
    APPROVAL_INVALID = "approval_invalid"
    TOOL_VERSION_CHANGED = "tool_version_changed"
    INTENT_REJECTED = "intent_rejected"
    PARAMS_MISMATCH = "params_mismatch"
    ACTION_BLOCKED = "action_blocked"
    SNAPSHOT_MISSING = "snapshot_missing"
    STREAM_ENDED = "runtime_stream_ended"
    FINALIZE_TOOL_INTENT = "finalize_tool_intent"
    RUNTIME_TIMEOUT = "runtime_timeout"
    RUNTIME_CANCELLED = "runtime_cancelled"
    RUNTIME_PROVIDER_UNAVAILABLE = "runtime_provider_unavailable"
    RUNTIME_EGRESS_DENIED = "runtime_egress_denied"
    RUNTIME_INVALID_TOOL_ARGS = "runtime_invalid_tool_args"
    RUNTIME_CAPABILITY_MISSING = "runtime_capability_missing"
    RUNTIME_RATE_LIMITED = "runtime_rate_limited"


def runtime_reason(code: str) -> LoopReason:
    """Runtime ``error.code`` -> 固定原因碼（RuntimeEvent 已保證 code 在 ``ERROR_CODES`` 內）。"""
    return LoopReason(f"runtime_{code.lower()}")
