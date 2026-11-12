"""module.* 就緒事件訂閱（切片 F，註冊表端）。

事件為單一類型 ``module.state_changed``（payload 含 ``readiness``，D108 R2；Module Coordinator 設計稿 §8）；
coordinator 本體不在此處。亂序／重送／未知模組由 ``ToolRegistry.apply_module_event`` 以 ``(epoch, revision)`` 處理。
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any

from uep.events.envelope import UEPEvent
from uep.lifecycle.states import unavailable_payload
from uep.tools.registry import MODULE_EVENT_TYPE

logger = logging.getLogger("uep.tools.module_events")


def apply_config_gates(coordinator: Any, gates: Mapping[str, str | None]) -> None:
    """把設定層的結論（未設定／禁止）寫進 coordinator 的閘：``gates`` 為 module_id -> reason_code（None 表示允許）。

    只處理 coordinator 已登記的 id（沒登記的模組不在 ``known_ids()``，工具恆為 unavailable）。閘一旦關閉，probe 成功
    也不會把模組變成可用；必須在 ``coordinator.start()`` 之前呼叫。"""
    registered = coordinator.known_ids()
    for module_id, reason in gates.items():
        if reason is not None and module_id in registered:
            coordinator.close_gate(module_id, reason)


def seed_fail_closed(registry: Any, coordinator: Any) -> None:
    """在**最終**組出的註冊表上，把 coordinator 登記的每個模組先標為 unavailable（epoch 0、revision 0）。

    registry 的預設是「已知模組、無事件 ⇒ ready」（非 warm_required），所以訂閱後、第一個快照被套用前（或快照發布失敗時）
    會 fail-open；這裡補上 fail-closed 的底。coordinator 啟動後 revision 從 1 起算，一定大於這個標記。
    每個 ``with_*`` 都會重建註冊表，所以必須在全部併入之後、只做一次。"""
    blocked = coordinator.blocked_ids()
    for module_id in sorted(coordinator.known_ids()):
        reason = blocked.get(module_id, "not_started")
        registry.apply_module_event(
            MODULE_EVENT_TYPE, unavailable_payload(module_id, reason_code=reason, revision=0, epoch=0),
        )


def subscribe_module_events(bus: Any, registry: Any) -> Callable[[], None]:
    """把 ``registry.apply_module_event`` 訂到 Bus 的 module.* 事件；回傳冪等的取消函式。

    handler 例外只記型別名稱（與 Bus 一致），不外溢給 Bus 與其他訂閱者。"""

    def handler(event: UEPEvent) -> None:
        try:
            registry.apply_module_event(event.event_type, event.payload)
        except Exception as exc:  # noqa: BLE001  錯誤隔離
            logger.error("模組事件處理失敗（%s）：%s", event.event_type, type(exc).__name__)

    types = (MODULE_EVENT_TYPE,)
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
