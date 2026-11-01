"""Tool Executor 協定（契約 CC-6 §6.5）。

Executor 只負責執行與簽回 receipt，不寫 ActionIntent.status；
狀態推進由 Task Manager 依 receipt 或查核結果決定。
receipt_id 使用 uep.ids 的 rcpt 前綴。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol, runtime_checkable

SideEffectClass = Literal["internal_read", "reversible_local", "external_effect", "high_impact"]
Idempotency = Literal["native", "via_action_id", "none"]


@dataclass(frozen=True)
class ToolSpec:
    """工具宣告（契約 TG-1、D54）：分類由註冊表決定，呼叫端不得自行宣告。

    必要欄位之外的新欄位一律帶預設值（D91 相容約束：只加不改）。``tool_version`` 進 action hash 的域
    （D55）：升版後舊 approval 與尚未執行的舊版 action 自動失效。
    """

    executor: str
    side_effect_class: SideEffectClass
    idempotency: Idempotency
    presence_bound: bool
    tool_version: int
    origin: str = "builtin"
    provider: str = "core"
    targeting: str = "core_local"


@runtime_checkable
class ToolSpecResolver(Protocol):
    """工具名 -> ToolSpec。每次呼叫都查當前資料，呼叫端不得快取結果。

    ``resolve`` 必須是記憶體內查詢，不得做 I/O：``execute_action`` 在 FOR UPDATE 交易內呼叫它。

    可見集合（capability_set 投影）是 Registry／Gateway 的另一個介面，不屬於這個協定。
    """

    def resolve(self, tool: str) -> ToolSpec:
        """解析不到拋 ``PolicyViolation``（code 例如 ``tool_unknown``）。"""
        ...


@runtime_checkable
class EnumerableToolSpecResolver(ToolSpecResolver, Protocol):
    """可列舉全部宣告的 resolver（靜態註冊表）；組裝層的 TG-5 檢查只對這類 resolver 做。"""

    def specs(self) -> Mapping[str, ToolSpec]:
        ...


@dataclass(frozen=True)
class ToolCall:
    """交給 executor 的一次呼叫；同一 logical action 重試沿用同一 action_id。"""

    action_id: str
    tool: str
    params: Mapping[str, Any]
    params_hash: str
    side_effect_class: SideEffectClass
    idempotency: Idempotency
    approval_id: str | None = None
    tool_version: int | None = None


@dataclass(frozen=True)
class ToolReceipt:
    """Executor 簽回的執行證據。result_ref 指向結果內容，不內嵌原文。"""

    receipt_id: str
    action_id: str
    succeeded: bool
    result_ref: str | None
    recorded_at: datetime
    source_verified: bool
    error_code: str | None = None
    meta: Mapping[str, Any] = field(default_factory=dict)


class ToolResponseLost(Exception):
    """呼叫已送出但回覆遺失（逾時、連線中斷）；執行結果未知，須查 receipt。"""


LookupOutcome = Literal["found", "confirmed_not_executed", "undetermined"]


@dataclass(frozen=True)
class ReceiptLookup:
    """查核結果三態（契約 CC-6 T-3）。

    - found：查到 receipt，依 receipt 推進。
    - confirmed_not_executed：executor 以權威紀錄確認從未執行；
      只有 executor 宣告 lookup_authoritative 且 idempotency 非 none 時，Core 才可據此重新執行。
    - undetermined：查不到或無法確認；action 維持 UNKNOWN，交規則或人工，不得重送。
    """

    outcome: LookupOutcome
    receipt: ToolReceipt | None = None


@runtime_checkable
class ToolExecutor(Protocol):
    @property
    def lookup_authoritative(self) -> bool:
        """executor 的執行紀錄是否持久且完整，足以「確認未執行」。"""
        ...

    def execute(self, call: ToolCall) -> ToolReceipt:
        """執行一次呼叫。回覆遺失時拋 ToolResponseLost，不得自行重試。"""
        ...

    def lookup_receipt(self, action_id: str) -> ReceiptLookup:
        """以 action_id 查核是否已執行。查核本身失聯時回 undetermined，不拋例外。"""
        ...
