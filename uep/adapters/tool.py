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
Origin = Literal["builtin", "mcp_static", "platform_approved", "discovered", "platform_declared"]
Targeting = Literal["core_local", "platform_remote"]
ApprovalLevel = Literal["none", "per_action", "standing"]  # standing 首版不實作（D65）
InterruptKind = Literal["interruptible", "checkpoint_only", "non_interruptible"]
Concurrency = Literal["parallel_safe", "parallel_one_shot", "serial"]
LoadCost = Literal["none", "light", "heavy"]
PresenceMode = Literal["any", "bound"]

# 可進 LLM 可見集合的來源；discovered／platform_declared 是候選，永不可見（RA-3）。
VISIBLE_ORIGINS: frozenset[str] = frozenset({"builtin", "mcp_static", "platform_approved"})

# 副作用嚴重度排序（RA-2、RA-6、RA-14 比較用）。
SIDE_EFFECT_ORDER: tuple[str, ...] = ("internal_read", "reversible_local", "external_effect", "high_impact")
APPROVAL_ORDER: tuple[str, ...] = ("none", "per_action", "standing")

# 能力標籤閉集（§5.4）：標籤 -> 該標籤允許的副作用上限。新增標籤＝改程式碼＋測試＋審核；無萬用字元。
CAPABILITY_CEILING: Mapping[str, SideEffectClass] = {
    "memory.read": "internal_read",
    "memory.write": "reversible_local",
    "lore.read": "internal_read",
    "lore.write": "reversible_local",
    "chatroom.read": "internal_read",
    "chatroom.post": "external_effect",
    "desktop.file_read": "internal_read",
    "desktop.file_write": "reversible_local",
    "web.fetch": "internal_read",
    "presentation.propose": "reversible_local",
    "decision.consult": "internal_read",
}


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
    origin: Origin = "builtin"
    provider: str = "core"
    targeting: Targeting = "core_local"
    approval_level: ApprovalLevel = "none"
    interruptible: InterruptKind = "non_interruptible"  # fail-closed：預設最嚴
    capability: str = ""  # 能力標籤（§5.4）；由 ToolRegistry 載入時要求必填且屬閉集


@dataclass(frozen=True)
class ToolDescriptor:
    """LLM 面：不進 resolver、不進 TaskManager。改 ``description``／``usage_hint`` 不需升 ``tool_version``。"""

    description: str
    input_schema: Mapping[str, Any]  # JSON Schema 子集；即送給 LLM 的 ``parameters``
    usage_hint: str = ""
    injected_params: tuple[str, ...] = ()  # Gateway 注入、LLM 看不到的欄位（PD-12）


@dataclass(frozen=True)
class ToolVariant:
    """平台變體（§5.6）：只能覆寫 description／收窄後的 input_schema／executor 路由／transport_timeout。
    分類欄不在這裡；宣告任何其他欄位的變體不可能被表達（RA-13 由型別結構保證，另驗 schema 只能收窄）。"""

    description: str | None = None
    input_schema: Mapping[str, Any] | None = None
    provider_ref: str | None = None
    transport_timeout: float | None = None


@dataclass(frozen=True)
class ToolScope:
    """可見性維度（§5.6）：空 ``platforms`` 即 global。本片只宣告並驗證，投影在切片 B。"""

    platforms: frozenset[str] = frozenset()
    presence: PresenceMode = "any"
    requires_context: frozenset[str] = frozenset()
    data_scope: str = "private"
    audience_min: str = "private"
    variants: Mapping[str, ToolVariant] = field(default_factory=dict)


@dataclass(frozen=True)
class CompositeStep:
    """composite 的一步：引用既有工具（§5.7.4）。"""

    tool: str
    params: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CompositeSpec:
    """線性多步驟宣告；不是引擎。"""

    steps: tuple[CompositeStep, ...]


@dataclass(frozen=True)
class ToolEntry:
    """註冊表唯一單位（§5.1）。所有並行／依賴欄位預設 fail-closed。"""

    name: str
    spec: ToolSpec
    descriptor: ToolDescriptor
    scope: ToolScope = field(default_factory=ToolScope)
    result_scope: str = "private"
    concurrency: Concurrency = "serial"
    conflict_keys: tuple[str, ...] = ()
    timeout_s: float | None = None
    cancellable: bool = False
    output_schema: Mapping[str, Any] | None = None
    composite: CompositeSpec | None = None
    requires: tuple[str, ...] = ()
    load_cost: LoadCost = "none"
    warm_required: bool = False
    degradable: bool = False
    schema_hash: str = ""  # 由 ToolRegistry 重算並與 lockfile 比對，不信任手填
    provider_ref: str | None = None
    transport_timeout: float | None = None
    disabled: bool = False
    venue_policy: Mapping[str, Any] = field(default_factory=lambda: {"mode": "origin_venue"})


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
