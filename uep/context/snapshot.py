"""ContextSnapshot 模型（契約 CC-2）。

- 單一型別，以 ``purpose`` 區分用途（D24）。
- 建立後不可變（frozen）；變更一律產生新 ``context_revision``。
- X-1：memory_slices／history_entries 必須滿足 §0.6 放入規則；建構時即檢查。
- X-4：歷史條目 ``delivery_state`` 為 unknown／cancelled 時不得宣稱完整說出。
- D33：只帶 ``*_ref`` 引用，不內嵌私人原文。
"""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from uep.events.envelope import scope_rank


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


_COMMITMENT_KEYS = frozenset({"commitment_id", "kind", "task_ref", "due_at", "status", "text_ref"})
_MAX_VALUE_LEN = 256


def _check_commitment(item: dict[str, Any]) -> None:
    """commitments 只收白名單鍵與有長度上限的純量值（D33：不夾帶私人原文）。"""
    extra = set(item) - _COMMITMENT_KEYS
    if extra:
        raise ValueError(f"commitment 含未允許的欄位：{sorted(extra)}")
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
    summary_source_revision: int | None = None


class HistoryEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    turn_id: str
    role: str
    generated_text_ref: str
    scope: str
    delivered_range: dict[str, Any] | None = None
    delivery_state: str

    @model_validator(mode="after")
    def _no_phantom_speech(self) -> HistoryEntry:
        # X-4：未完整交付者必須帶 delivered_range 才能表達「說到哪裡」，不得無依據視為完整說出
        incomplete = self.delivery_state in {"cancelled", "unknown"}
        if incomplete and (self.delivered_range or {}).get("complete") is True:
            raise ValueError("cancelled／unknown 的歷史不得標示為完整說出（X-4）")
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
    excluded_summary: dict[str, int]  # {count_by_reason}：只有筆數，不含內容
    task_refs: tuple[str, ...] = ()
    commitments: tuple[dict[str, Any], ...] = ()
    capabilities: dict[str, Any]
    data_egress: dict[str, Any]
    token_budget: dict[str, Any]
    history_owner: HistoryOwner
    sdk_session_ref: str | None = None
    built_at: datetime
    valid_until: datetime | None = None

    @model_validator(mode="after")
    def _check_invariants(self) -> ContextSnapshot:
        scope_rank(self.audience_scope)  # 格式不合法即拋錯
        for commitment in self.commitments:
            _check_commitment(commitment)
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
        # X-6／VC-2-3：非 ok 時不得帶記憶切片
        if self.memory_status is not MemoryStatus.OK and self.memory_slices:
            raise ValueError("memory_status != ok 時 memory_slices 必須為空")
        if self.valid_until is not None and self.valid_until < self.built_at:
            raise ValueError("valid_until 不得早於 built_at")
        return self
