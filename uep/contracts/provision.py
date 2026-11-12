"""預載提議的中立契約型別（Module Coordinator 設計稿 §5.2、D108）。

決策側（產出提議）與 ``uep.lifecycle``（收提議）共用；本檔零依賴，不得 import 兩者。
``install`` 不在動作集合內（ADR-04：決策中樞不裝新模組）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, get_args

ProvisionAction = Literal["keep", "warm", "load", "unload"]
ProvisionUrgency = Literal["low", "normal", "high"]
ProvisionOnNotReady = Literal["interim_then_wait", "degrade", "fail"]
# 一項提議的處置結果（module.provision_resolved 的 outcome）
ProvisionOutcome = Literal["accepted", "accepted_best_effort", "deferred", "rejected", "dropped", "noop"]

PROVISION_ACTIONS: frozenset[str] = frozenset(get_args(ProvisionAction))
PROVISION_URGENCIES: frozenset[str] = frozenset(get_args(ProvisionUrgency))
PROVISION_ON_NOT_READY: frozenset[str] = frozenset(get_args(ProvisionOnNotReady))
PROVISION_OUTCOMES: frozenset[str] = frozenset(get_args(ProvisionOutcome))


@dataclass(frozen=True)
class ProvisionItem:
    """決策中樞對單一模組的提議（無副作用，coordinator 可拒絕）。

    ``urgency`` 只是提示，不是優先序（R8 待裁，暫以提示處理）；``on_not_ready`` 給 Orchestrator 處理，coordinator 忽略。
    工具與模組的對映以 ``ToolEntry.requires`` 為準，不在此重複存放。"""

    module_id: str
    action: ProvisionAction
    ready_by_ms: int | None = None
    urgency: ProvisionUrgency = "normal"
    on_not_ready: ProvisionOnNotReady = "degrade"

    def __post_init__(self) -> None:
        if not isinstance(self.module_id, str) or not self.module_id:
            raise ValueError("module_id 必須是非空字串")
        if self.action not in PROVISION_ACTIONS:
            raise ValueError(f"不支援的 action：{self.action!r}")
        if self.urgency not in PROVISION_URGENCIES:
            raise ValueError(f"不支援的 urgency：{self.urgency!r}")
        if self.on_not_ready not in PROVISION_ON_NOT_READY:
            raise ValueError(f"不支援的 on_not_ready：{self.on_not_ready!r}")
        if self.ready_by_ms is not None and (
            isinstance(self.ready_by_ms, bool) or not isinstance(self.ready_by_ms, int) or self.ready_by_ms < 0
        ):
            raise ValueError("ready_by_ms 必須是非負整數或 None")
