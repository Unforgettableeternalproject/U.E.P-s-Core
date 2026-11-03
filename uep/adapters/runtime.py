"""Runtime Adapter 協定（契約 CC-3）。

Runtime 是可替換的推理／工具迴圈，只能提出 RuntimeEvent；run／attempt 狀態、對外輸出、
工具授權與副作用都屬於 Core。R-2：``cancel`` 後晚到的 text_delta／result 由 Core 丟棄，
已發生的工具副作用 receipt 不丟棄。
"""
from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
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


def check_tools_match_capability(
    request: RuntimeRequest, tools: Sequence[Mapping[str, Any]], expected: Sequence[Mapping[str, Any]], *,
    seq: int = 0, builtin_tools: Sequence[Any] = (),
) -> RuntimeEvent | None:
    """D60 啟動斷言（Agent 架構提案 §6.2、TC-1）：attempt 開始前，Runtime 把**將送給 provider 的 tools**（``tools``）
    與 ``expected``（呼叫端傳入 ``uep.tools.render.render_tools(request.capability_set)`` 的結果）做**整份**比對：
    名稱集合、每個工具的 ``type``／``description``／``parameters`` 都要相等，不重複，且沒有任何 hosted／built-in
    工具（``builtin_tools`` 必須為空）；``expected`` 的名稱集合本身也必須等於 ``capability_set`` 的名稱集合。
    項目必須是映射，裸字串之類一律不合法。全部符合回傳 None（可以啟動）。

    否則回傳 ``error`` 事件 ``CAPABILITY_MISSING``（``retryable=False``），Runtime 須 yield 它並拒絕啟動，
    不得送出請求。多給、少給、內容被改過都算。``detail`` 只含工具名稱與固定說明，不含 description／schema 內容。
    這個檔案不 import uep.tools，避免協定層反向依賴；渲染由呼叫端負責。"""
    capability_names = _capability_names(request.capability_set)
    problems: list[str] = []
    sent, sent_problem = _index_tools(tools)
    wanted, wanted_problem = _index_tools(expected)
    problems += sent_problem + [f"預期清單{p}" for p in wanted_problem]
    if builtin_tools:
        problems.append("hosted／built-in 工具必須為空")
    if set(wanted) != capability_names:
        problems.append("預期清單與 capability_set 的工具名稱不一致")
    missing, extra = sorted(set(wanted) - set(sent)), sorted(set(sent) - set(wanted))
    if missing:
        problems.append(f"少給：{missing}")
    if extra:
        problems.append(f"多給：{extra}")
    changed = sorted(n for n in set(sent) & set(wanted) if _plain(sent[n]) != _plain(wanted[n]))
    if changed:
        problems.append(f"內容與預期不符：{changed}")
    if not problems:
        return None
    return RuntimeEvent(
        attempt_id=request.attempt_id, seq=seq, kind="error",
        error={"code": "CAPABILITY_MISSING", "retryable": False, "detail": "；".join(problems)},
    )


def _index_tools(tools: Sequence[Any]) -> tuple[dict[str, Mapping[str, Any]], list[str]]:
    """name -> 工具項目；回報非映射、非 function、無名稱、重複的項目（不含內容）。"""
    index: dict[str, Mapping[str, Any]] = {}
    problems: list[str] = []
    for tool in tools:
        function = tool.get("function") if isinstance(tool, Mapping) else None
        name = function.get("name") if isinstance(function, Mapping) else None
        if not isinstance(tool, Mapping) or tool.get("type") != "function" or not isinstance(name, str) or not name:
            problems.append("含非 function 型別或格式不合的工具項目")
        elif name in index:
            problems.append("tools 含重複名稱")
        else:
            index[name] = tool
    return index, problems


def _plain(node: Any) -> Any:
    """映射／序列正規化成 dict／list，使唯讀視圖與一般容器可以比較。"""
    if isinstance(node, Mapping):
        return {k: _plain(v) for k, v in node.items()}
    if isinstance(node, (list, tuple)):
        return [_plain(v) for v in node]
    return node


def _capability_names(capability_set: Mapping[str, Any]) -> frozenset[str]:
    tools = capability_set.get("tools", ()) if isinstance(capability_set, Mapping) else ()
    names = (item.get("tool") if isinstance(item, Mapping) else item for item in tools)
    return frozenset(n for n in names if isinstance(n, str) and n)
