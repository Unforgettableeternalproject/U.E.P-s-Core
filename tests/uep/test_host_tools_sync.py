"""Host 在 Lease 變動後重投影工具曝光（_sync_tools）：忙碌時延後、輪結束補做、Lease 遺失也觸發。純記憶體測試。"""
from __future__ import annotations

import threading
from collections import OrderedDict
from types import SimpleNamespace
from typing import Any

from uep.contracts.presence import AcquireResult
from uep.echo.endpoint import StopNotice
from uep.echo.host import EchoPipelineHost
from uep.echo.resume_return import ResumePending


def _sync_host(*, tickets: list | None = None, switching: bool = False, closed: bool = False) -> Any:
    host = EchoPipelineHost.__new__(EchoPipelineHost)
    host._intake_lock = threading.Lock()
    # 續跑回覆的 pending 佇列（R2）：_finish_turn 與取得 Lease 會讀它，不經 __init__ 時要補上空的欄位
    host._resume_lock = threading.Lock()
    host._resume_flush_lock = threading.Lock()
    host._resume_flush_queued = False
    host._resume_settled = OrderedDict()
    host._resume_pending = ResumePending(ttl_s=1800.0, max_items=8)
    host._closed, host._switching, host._tools_refresh_pending = closed, switching, False
    host.orchestrator = SimpleNamespace(
        live_tickets=lambda: list(tickets or ()), cancel_turn=lambda *a, **k: None,
        cancel_reason_of=lambda turn_id: None, on_turn_ended=lambda *a, **k: None,
    )
    host.coordinator = None
    host.refreshed = 0
    host._refresh_tools = lambda: setattr(host, "refreshed", host.refreshed + 1)  # type: ignore[method-assign]
    return host


def test_sync_tools_refreshes_immediately_when_idle():
    host = _sync_host()
    host._sync_tools()
    assert host.refreshed == 1 and not host._tools_refresh_pending


def test_sync_tools_defers_when_busy_and_finish_turn_flushes_it():
    tickets = [SimpleNamespace(turn_id="t1")]
    host = _sync_host(tickets=tickets)
    host._tool_seq = {}
    host._store_memory = lambda *a, **k: None  # type: ignore[method-assign]
    host._sync_tools()
    assert host.refreshed == 0 and host._tools_refresh_pending  # 忙碌：不跳過，記為待辦
    ticket = SimpleNamespace(turn_id="t1")
    host._finish_turn(ticket, SimpleNamespace(), [])  # type: ignore[arg-type]
    assert host.refreshed == 0  # ticket 尚在 orchestrator：仍忙碌，繼續延後
    tickets.clear()
    host._finish_turn(ticket, SimpleNamespace(), [])  # type: ignore[arg-type]
    assert host.refreshed == 1 and not host._tools_refresh_pending


def test_sync_tools_defers_while_switching_and_skips_when_closed():
    switching = _sync_host(switching=True)
    switching._sync_tools()
    assert switching.refreshed == 0 and switching._tools_refresh_pending
    closed = _sync_host(closed=True)
    closed._sync_tools()
    assert closed.refreshed == 0


def test_acquire_lease_granted_syncs_tools_and_denied_does_not():
    host = _sync_host()
    host._stopped_reason = "global_stop"
    host.endpoint = SimpleNamespace(start_lease=lambda: AcquireResult.GRANTED)
    assert host.acquire_lease() is AcquireResult.GRANTED and host.refreshed == 1 and host._stopped_reason is None
    host.endpoint = SimpleNamespace(start_lease=lambda: AcquireResult.DENIED)
    assert host.acquire_lease() is AcquireResult.DENIED and host.refreshed == 1


def test_presence_lost_notice_resyncs_tools_but_other_stops_do_not():
    host = _sync_host()
    host._on_endpoint_stop(StopNotice("presence_lost"))
    assert host.refreshed == 1
    host._on_endpoint_stop(StopNotice("global_stop"))
    assert host.refreshed == 1  # global_stop 不改 Lease，投影平台不變
