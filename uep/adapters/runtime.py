"""Runtime Adapter 協定（契約 CC-3）。

Runtime 是可替換的推理／工具迴圈，只能提出 RuntimeEvent；run／attempt 狀態、對外輸出、
工具授權與副作用都屬於 Core。R-2：``cancel`` 後晚到的 text_delta／result 由 Core 丟棄，
已發生的工具副作用 receipt 不丟棄。
"""
from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol, get_args, runtime_checkable

RuntimeMode = Literal["direct_chat", "agent_loop"]
EventKind = Literal["text_delta", "tool_intent", "approval_request", "checkpoint", "result", "error"]
CancelAck = Literal["stopped", "isolated"]
ErrorCode = Literal[
    "TIMEOUT", "CANCELLED", "PROVIDER_UNAVAILABLE", "EGRESS_DENIED",
    "INVALID_TOOL_ARGS", "CAPABILITY_MISSING", "RATE_LIMITED",
]
ERROR_CODES: frozenset[str] = frozenset(get_args(ErrorCode))
EVENT_KINDS: tuple[str, ...] = get_args(EventKind)


@dataclass(frozen=True)
class RuntimeRequest:
    run_id: str
    attempt_id: str
    snapshot_ref: str
    capability_set: Mapping[str, Any]
    cancel_scope: Mapping[str, Any]  # {turn_generation, task_run, process}
    deadline: datetime
    mode: RuntimeMode
    fence: Mapping[str, Any] | None = None  # 互動回合必填；Runtime 只回帶、不解讀
    checkpoint_ref: str | None = None  # resume 必填

    def __post_init__(self) -> None:
        if self.mode not in ("direct_chat", "agent_loop"):
            raise ValueError(f"未知 mode：{self.mode!r}")


@dataclass(frozen=True)
class RuntimeEvent:
    attempt_id: str
    seq: int
    kind: EventKind
    text_delta: str | None = None
    tool_intent: Mapping[str, Any] | None = None  # {tool, args_complete, args, side_effect_hint}
    approval_request: Mapping[str, Any] | None = None  # {action_hash, reason}
    checkpoint: Mapping[str, Any] | None = None
    result: Mapping[str, Any] | None = None  # {status, output_ref, usage}
    error: Mapping[str, Any] | None = None  # {code, retryable, detail}

    def __post_init__(self) -> None:
        # kind 必須是列舉值：否則 getattr(self, kind) 會取到 seq、attempt_id 等不相干欄位
        if self.kind not in EVENT_KINDS:
            raise ValueError(f"未知的 kind：{self.kind!r}")
        payload = getattr(self, self.kind)
        if payload is None:
            raise ValueError(f"kind={self.kind} 必須帶同名欄位")
        others = [k for k in ("text_delta", "tool_intent", "approval_request", "checkpoint", "result", "error")
                  if k != self.kind and getattr(self, k) is not None]
        if others:
            raise ValueError(f"kind={self.kind} 不得同時帶 {others}")
        if self.kind == "tool_intent":
            intent = self.tool_intent
            if not isinstance(intent, Mapping) or not isinstance(intent.get("tool"), str) or not intent["tool"]:
                raise ValueError("tool_intent 必須帶非空的 tool")
            if not isinstance(intent.get("args_complete"), bool):
                raise ValueError("tool_intent 必須帶布林 args_complete（false 者 Core 不得提交）")
        if self.kind == "error" and self.error.get("code") not in ERROR_CODES:  # type: ignore[union-attr]
            raise ValueError(f"error.code 不合法：{self.error.get('code')!r}")  # type: ignore[union-attr]


@dataclass(frozen=True)
class RuntimeStatus:
    attempt_id: str
    state: Literal["running", "completed", "failed", "cancelled", "isolated", "unknown"]
    last_seq: int = 0
    meta: Mapping[str, Any] = field(default_factory=dict)


@runtime_checkable
class Runtime(Protocol):
    def start(self, request: RuntimeRequest) -> str:
        """開始一次 attempt，回傳 attempt_id；事件以 ``stream`` 取得。"""
        ...

    def resume(self, request: RuntimeRequest) -> str:
        """自 Core 持有的 checkpoint 繼續（request.checkpoint_ref 必填）。"""
        ...

    def stream(self, attempt_id: str) -> Iterator[RuntimeEvent]:
        """依序產生 RuntimeEvent（seq 單調）。"""
        ...

    def cancel(self, attempt_id: str, reason: str) -> CancelAck:
        """冪等。無法立即停止時回 isolated，Core 丟棄晚到結果。"""
        ...

    def status(self, attempt_id: str) -> RuntimeStatus:
        """只讀。"""
        ...

    def submit_tool_result(self, attempt_id: str, action_id: str, result: Mapping[str, Any]) -> None:
        """把工具結果交回進行中的 attempt 使其續跑（D61 選項 A；崩潰／fallback 走 ``resume``）。

        ``result`` 為交付當下依 TG-9 過濾的視圖（不持久化）：``items``（逐項 ref／scope／內容引用）、
        ``status``、``degraded``、``partial``；不含 excluded_summary。
        """
        ...
