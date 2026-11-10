"""平行排程器（Agent 架構提案 §6.4，Q-C）：純函式，輸入相同則輸出相同。

``plan_waves`` 把同一則模型訊息內、已通過逐項前置（capability／readiness／schema／注入）的 ``tool_intent`` 分成「波」：

- 資格：``concurrency`` 為 ``parallel_safe`` 或 ``parallel_one_shot`` 且 ``approval_level == "none"``。**只看註冊表欄位，不看工具名稱，也不看模型的
  side_effect_hint**（TG-2）。``parallel_one_shot`` 的啟用條件（``cancellable``＋``timeout_s`` 等）由註冊表 RA-11／RA-12 與組裝層檢查把關，
  這裡只信任已通過驗證的欄位；逾時與取消的執行語意在 ``AgentLoop``（§6.4）。
- ``serial`` 項（含需核准者、composite、未知工具、``conflict_keys`` 無法代入者）自成單獨一波，前後各為屏障。
- 可並行項依 ``index`` 貪婪加入當前波；資源鍵與當前波任一成員相交（同鍵互斥）就關閉當前波、以它開新波。
  所以衝突項依 ``index`` 先後序列化，不會被重排到更前面。
- 波的大小受 ``max_parallel`` 限制（滿了就開新波）。
- ``conflict_keys`` 以**驗證並注入後**的參數代入範本（不是模型原始 args）；範本的 ``{param}`` 缺值或非純量 ⇒ 該項
  fail-closed 當 serial。

執行語意（單一失敗不影響兄弟、依 index 回餵）在 ``AgentLoop``；這裡只決定分組。
"""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from uep.adapters.tool import ToolEntry
from uep.tasks.manager import PolicyViolation

_PLACEHOLDER = re.compile(r"\{(\w+)\}")
_SCALARS = (str, int, float, bool)

DEFERRED_BATCH_LIMIT = "deferred_batch_limit"
DEFERRED_BUDGET = "deferred_budget"
DEFERRED_BY_APPROVAL_WAIT = "deferred_by_approval_wait"
UNKNOWN_UNRESOLVED = "unknown_unresolved"


class EntryLookup(Protocol):
    def entry(self, tool: str) -> ToolEntry:
        ...


@dataclass(frozen=True)
class SchedItem:
    index: int
    tool: str
    params: Mapping[str, Any]  # Gateway 回傳的（已驗證、已注入）參數


@dataclass(frozen=True)
class Wave:
    items: tuple[SchedItem, ...]
    parallel: bool  # False：serial／屏障（恆為單一項）

    @property
    def indexes(self) -> tuple[int, ...]:
        return tuple(i.index for i in self.items)


def parallel_eligible(entry: ToolEntry) -> bool:
    return (
        entry.concurrency in ("parallel_safe", "parallel_one_shot")
        and entry.spec.approval_level == "none" and entry.composite is None
    )


def resource_keys(entry: ToolEntry, params: Mapping[str, Any]) -> frozenset[str] | None:
    """代入 ``conflict_keys`` 範本得到實際資源鍵集合；任一範本無法代入回 None（呼叫端當 serial）。"""
    keys: set[str] = set()
    for template in entry.conflict_keys:
        if template == "tool:{name}":
            keys.add(f"tool:{entry.name}")
            continue
        failed = False

        def fill(match: re.Match[str]) -> str:
            nonlocal failed
            value = params.get(match.group(1))
            if not isinstance(value, _SCALARS):
                failed = True
                return ""
            return str(value).lower() if isinstance(value, bool) else str(value)

        key = _PLACEHOLDER.sub(fill, template)
        if failed:
            return None
        keys.add(key)
    return frozenset(keys)


def plan_waves(batch: Sequence[SchedItem], registry: EntryLookup, *, max_parallel: int = 1) -> list[Wave]:
    """貪婪、保序的分波（§6.4-5）。``batch`` 內的 index 必須唯一；內部依 index 排序。"""
    if max_parallel < 1:
        raise ValueError("max_parallel 必須 >= 1")
    ordered = sorted(batch, key=lambda item: item.index)
    if len({item.index for item in ordered}) != len(ordered):
        raise ValueError("batch 的 index 不可重複")
    waves: list[Wave] = []
    current: list[SchedItem] = []
    current_keys: set[str] = set()

    def close() -> None:
        nonlocal current, current_keys
        if current:
            waves.append(Wave(tuple(current), parallel=True))
        current, current_keys = [], set()

    for item in ordered:
        keys: frozenset[str] | None = None
        try:
            entry = registry.entry(item.tool)
            if parallel_eligible(entry):
                keys = resource_keys(entry, item.params)
        except PolicyViolation:
            keys = None
        if keys is None:  # serial：關閉當前波，自己成為屏障
            close()
            waves.append(Wave((item,), parallel=False))
            continue
        if (keys & current_keys) or len(current) >= max_parallel:
            close()
        current.append(item)
        current_keys |= keys
    close()
    return waves


def defer_tail[T](items: Sequence[T], limit: int, reason: str) -> tuple[list[T], list[tuple[T, str]]]:
    """保留前 ``limit`` 項，其餘以 ``reason`` 標記為不執行（``deferred_batch_limit``／``deferred_budget``）。"""
    limit = max(limit, 0)
    return list(items[:limit]), [(item, reason) for item in items[limit:]]
