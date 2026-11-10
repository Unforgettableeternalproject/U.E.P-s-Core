"""module.* 就緒事件訂閱（切片 F，註冊表端）。

暫定：事件名（``module.ready``／``module.warming``／``module.unavailable``）與 payload
``{module_id, revision, epoch?}`` 待 module coordinator 設計時確認（Agent架構與工具契約提案 §5.8）；
coordinator 本體不在此處。亂序／重送／未知模組由 ``ToolRegistry.apply_module_event`` 以 ``(epoch, revision)`` 處理。
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from uep.events.envelope import UEPEvent
from uep.tools.registry import MODULE_EVENT_STATES

logger = logging.getLogger("uep.tools.module_events")


def subscribe_module_events(bus: Any, registry: Any) -> Callable[[], None]:
    """把 ``registry.apply_module_event`` 訂到 Bus 的 module.* 事件；回傳冪等的取消函式。

    handler 例外只記型別名稱（與 Bus 一致），不外溢給 Bus 與其他訂閱者。"""

    def handler(event: UEPEvent) -> None:
        try:
            registry.apply_module_event(event.event_type, event.payload)
        except Exception as exc:  # noqa: BLE001  錯誤隔離
            logger.error("模組事件處理失敗（%s）：%s", event.event_type, type(exc).__name__)

    types = tuple(MODULE_EVENT_STATES)
    for event_type in types:
        bus.subscribe(event_type, handler)
    cancelled = False

    def unsubscribe() -> None:
        nonlocal cancelled
        if cancelled:
            return
        cancelled = True
        for event_type in types:
            bus.unsubscribe(event_type, handler)

    return unsubscribe
