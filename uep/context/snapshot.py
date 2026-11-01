"""ContextSnapshot 模型（契約 CC-2）。

- 單一型別，以 ``purpose`` 區分用途（D24）。
- 建立後不可變（frozen）；變更一律產生新 ``context_revision``。
- X-1：memory_slices／history_entries 必須滿足 §0.6 放入規則；建構時即檢查。
- X-1：commitments、task_refs、history summary 各帶 scope，一律走 ``scope_admits``（public 快照不得帶 private 項）。
- X-4：歷史條目 ``delivery_state`` 為 unknown／cancelled／rejected／suppressed 時不得宣稱完整說出。
- D33：只帶 ``*_ref`` 引用，不內嵌私人原文。
- 深度凍結：dict／list 欄位轉為 MappingProxyType／tuple，建立後無法就地修改；序列化時還原為 dict／list。
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Annotated, Any

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    model_validator,
)

from uep.events.envelope import scope_rank
from uep.output.delivery import DeliveryStatus


def deep_freeze(value: Any) -> Any:
    """遞迴把 Mapping 轉為唯讀 MappingProxyType、序列轉為 tuple。"""
    if isinstance(value, Mapping):
        return MappingProxyType({k: deep_freeze(v) for k, v in value.items()})
    if isinstance(value, (list, tuple, set, frozenset)):
        return tuple(deep_freeze(v) for v in value)
    return value


def deep_thaw(value: Any) -> Any:
    """``deep_freeze`` 的反向：序列化（DB 存檔、JSON）時還原為 dict／list。"""
    if isinstance(value, Mapping):
        return {k: deep_thaw(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [deep_thaw(v) for v in value]
    return value


FrozenMap = Annotated[Mapping[str, Any], AfterValidator(deep_freeze), PlainSerializer(deep_thaw)]
OptFrozenMap = Annotated[Mapping[str, Any] | None, AfterValidator(deep_freeze), PlainSerializer(deep_thaw)]
FrozenCounts = Annotated[Mapping[str, int], AfterValidator(deep_freeze), PlainSerializer(deep_thaw)]


class SnapshotPurpose(StrEnum):
    CONVERSATION_TURN = "conversation_turn"
    TASK_RUN = "task_run"
    PROACTIVE_EVAL = "proactive_eval"
    SUMMARIZE = "summarize"


class MemoryStatus(StrEnum):
    OK = "ok"
    DEGRADED_NO_MEMORY = "degraded_no_memory"
    TIMEOUT = "timeout"
    UNAVAILABLE = "unavailable"


class HistoryOwner(StrEnum):
    CORE_PROJECTION = "core_projection"
    SDK_STORE = "sdk_store"


class ScopeViolation(ValueError):
    """內容 scope 不可進入該 audience（X-1）。"""


def scope_admits(audience_scope: str, content_scope: str) -> bool:
    """§0.6 放入規則：``rank(audience) <= rank(內容)``，同級的 private／group 另需歸屬 id 相同。

    scope 字串格式 ``scope_<level>[_<id>]``。audience 比內容更窄時（例 private audience 讀 public 內容）
    不需比對歸屬；group 內容進 private audience 的成員資格由 MEM ACL 於來源端過濾。
    """
    a_rank, c_rank = scope_rank(audience_scope), scope_rank(content_scope)
    if a_rank > c_rank:
        return False
    if a_rank == c_rank and c_rank < 2:
        return audience_scope == content_scope
    return True


_COMMITMENT_KEYS = frozenset({"commitment_id", "kind", "task_ref", "due_at", "status", "text_ref", "scope"})
_MAX_VALUE_LEN = 256
_REASON_CODE = re.compile(r"[a-z][a-z0-9_]{0,31}")  # excluded_summary 的鍵：只能是原因碼，不能是自由文字


def _check_commitment(item: Mapping[str, Any]) -> None:
    """commitments 只收白名單鍵與有長度上限的純量值（D33：不夾帶私人原文）；必須帶 scope（X-1）。"""
    extra = set(item) - _COMMITMENT_KEYS
    if extra:
        raise ValueError(f"commitment 含未允許的欄位：{sorted(extra)}")
    if not item.get("scope"):
        raise ValueError("commitment 必須帶 scope")
    for key, value in item.items():
        if value is not None and not isinstance(value, str):
            raise ValueError(f"commitment.{key} 必須是字串")
        if value is not None and len(value) > _MAX_VALUE_LEN:
            raise ValueError(f"commitment.{key} 超過 {_MAX_VALUE_LEN} 字元")


class HistoryWindow(BaseModel):
    model_config = ConfigDict(frozen=True)

    from_turn: str | None = None
    to_turn: str | None = None
    summary_ref: str | None = None
    summary_scope: str | None = None  # summary_ref 所屬 scope；有 summary_ref 時必填（X-1）
    summary_source_revision: int | None = None

    @model_validator(mode="after")
    def _summary_needs_scope(self) -> HistoryWindow:
        if self.summary_ref is not None and not self.summary_scope:
            raise ValueError("有 summary_ref 時必須帶 summary_scope")
        return self


class TaskRef(BaseModel):
    model_config = ConfigDict(frozen=True)

    task_id: str
    scope: str


_INCOMPLETE_STATES = frozenset({
    DeliveryStatus.CANCELLED, DeliveryStatus.UNKNOWN, DeliveryStatus.REJECTED, DeliveryStatus.SUPPRESSED,
})


class HistoryEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    turn_id: str
    role: str
    generated_text_ref: str
    scope: str
    delivered_range: OptFrozenMap = None
    delivery_state: DeliveryStatus

    @model_validator(mode="after")
    def _no_phantom_speech(self) -> HistoryEntry:
        # X-4：未完整交付者必須帶 delivered_range 才能表達「說到哪裡」，不得無依據視為完整說出
        incomplete = self.delivery_state in _INCOMPLETE_STATES
        if incomplete and (self.delivered_range or {}).get("complete") is True:
            raise ValueError("cancelled／unknown／rejected／suppressed 的歷史不得標示為完整說出（X-4）")
        return self


class MemorySlice(BaseModel):
    model_config = ConfigDict(frozen=True)

    memory_id: str
    scope: str
    source_refs: tuple[str, ...] = ()
    embedding_version: str | None = None
    retrieved_at: datetime
    relevance: float


class ContextSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    snapshot_id: str
    context_revision: int = Field(ge=1)
    purpose: SnapshotPurpose
    persona_id: str
    persona_version: int
    policy_version: int
    conversation_id: str | None = None
    segment_id: str | None = None
    turn_id: str | None = None
    run_id: str | None = None
    audience_scope: str
    actor_ref: str
    history_window: HistoryWindow
    history_entries: tuple[HistoryEntry, ...] = ()
    memory_slices: tuple[MemorySlice, ...] = ()
    memory_status: MemoryStatus
    excluded_summary: FrozenCounts  # {count_by_reason}：只有筆數，不含內容
    task_refs: tuple[TaskRef, ...] = ()
    commitments: tuple[FrozenMap, ...] = ()
    capabilities: FrozenMap
    data_egress: FrozenMap
    token_budget: FrozenMap
    history_owner: HistoryOwner
    sdk_session_ref: str | None = None
    built_at: datetime
    valid_until: datetime | None = None

    @model_validator(mode="after")
    def _check_invariants(self) -> ContextSnapshot:
        scope_rank(self.audience_scope)  # 格式不合法即拋錯
        for commitment in self.commitments:
            _check_commitment(commitment)
        for reason, count in self.excluded_summary.items():
            if not _REASON_CODE.fullmatch(reason) or isinstance(count, bool) or count < 0:
                raise ValueError("excluded_summary 只能是 {原因碼: 非負筆數}，不得夾帶自由文字")
        # 互動用途必須有 conversation／segment；背景用途（proactive_eval、summarize、task_run）可為空
        if self.purpose is SnapshotPurpose.CONVERSATION_TURN and not (
            self.conversation_id and self.segment_id and self.turn_id
        ):
            raise ValueError("conversation_turn 必須帶 conversation_id／segment_id／turn_id")
        if self.purpose is SnapshotPurpose.TASK_RUN and not self.run_id:
            raise ValueError("task_run 必須帶 run_id")
        # X-1：放入規則
        for item in self.memory_slices:
            if not scope_admits(self.audience_scope, item.scope):
                raise ScopeViolation(f"記憶 {item.memory_id} 的 scope 不可進入 {self.audience_scope}")
        for entry in self.history_entries:
            if not scope_admits(self.audience_scope, entry.scope):
                raise ScopeViolation(f"歷史 {entry.turn_id} 的 scope 不可進入 {self.audience_scope}")
        if self.history_window.summary_ref is not None and not scope_admits(
            self.audience_scope, self.history_window.summary_scope or ""
        ):
            raise ScopeViolation(f"歷史摘要的 scope 不可進入 {self.audience_scope}")
        for ref in self.task_refs:
            if not scope_admits(self.audience_scope, ref.scope):
                raise ScopeViolation(f"task {ref.task_id} 的 scope 不可進入 {self.audience_scope}")
        for commitment in self.commitments:
            if not scope_admits(self.audience_scope, commitment["scope"]):
                raise ScopeViolation(f"承諾 {commitment.get('commitment_id')} 的 scope 不可進入 {self.audience_scope}")
        # X-6／VC-2-3：非 ok 時不得帶記憶切片
        if self.memory_status is not MemoryStatus.OK and self.memory_slices:
            raise ValueError("memory_status != ok 時 memory_slices 必須為空")
        if self.valid_until is not None and self.valid_until < self.built_at:
            raise ValueError("valid_until 不得早於 built_at")
        return self
